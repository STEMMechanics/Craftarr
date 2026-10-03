from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .instance_identity import get_node_id
from .hardware import collect_hardware_stats
from .models import RemoteNode, Server, User
from .node_security import (
    decrypt_remote_token,
    encrypt_remote_token,
    generate_node_token,
    node_token_status,
)
from .permissions import has_permission
from .remote_nodes import (
    RemoteFunctionUnavailable,
    RemoteNodeUnavailable,
    _request,
    mark_remote_node_offline,
    mark_remote_node_online,
)
from .remote_nodes import (
    RemoteNodeError,
    fetch_remote_inventory,
    normalize_remote_url,
    refresh_remote_node,
    run_linked_plugin_settings_sync,
    sync_node_servers,
)
from .version import APP_VERSION
from .web_users import current_web_user


router = APIRouter()


def _settings_admin(request: Request, db: Session) -> tuple[User | None, JSONResponse | None]:
    user = current_web_user(request, db)
    if not user:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "settings.manage"):
        return None, JSONResponse({"error": "Settings management permission required"}, status_code=403)
    return user, None


@router.get("/api/web/settings/node-token")
def get_node_token_status(request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    return JSONResponse(
        node_token_status(db),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/web/settings/node-token/regenerate")
def regenerate_node_access_token(request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    return JSONResponse(
        {"success": True, "token": generate_node_token(db)},
        headers={"Cache-Control": "no-store"},
    )


def _node_summary(node: RemoteNode) -> dict:
    return {
        "node_id": node.node_id,
        "name": node.name,
        "base_url": node.base_url,
        "server_count": len(node.servers),
        "app_version": node.app_version,
        "offline": node.outage_started_at is not None,
        "offline_since": node.outage_started_at.isoformat() + "Z" if node.outage_started_at else None,
        "last_connected_at": node.last_connected_at.isoformat() if node.last_connected_at else None,
        "last_error": node.last_error,
    }


def _hardware_viewer(request: Request, db: Session) -> tuple[User | None, JSONResponse | None]:
    user = current_web_user(request, db)
    if not user:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "system.view"):
        return None, JSONResponse({"error": "System view permission required"}, status_code=403)
    return user, None


def _remote_node_is_accessible(user: User, node: RemoteNode) -> bool:
    return has_permission(user, "servers.view_all") or any(
        server.node_id == node.node_id for server in getattr(user, "remote_servers", [])
    )


@router.get("/api/web/hardware/nodes")
def hardware_nodes(request: Request, db: Session = Depends(get_db)):
    user, error = _hardware_viewer(request, db)
    if error:
        return error
    nodes = [{
        "node_id": get_node_id(db),
        "name": "This Node",
        "app_version": APP_VERSION,
        "offline": False,
    }]
    nodes.extend(
        {
            "node_id": node.node_id,
            "name": node.name,
            "app_version": node.app_version,
            "offline": node.outage_started_at is not None,
        }
        for node in db.query(RemoteNode).order_by(RemoteNode.name).all()
        if _remote_node_is_accessible(user, node)
    )
    return {"nodes": nodes}


@router.get("/api/web/hardware/stats")
def hardware_stats(
    request: Request,
    node_id: str | None = None,
    db: Session = Depends(get_db),
):
    user, error = _hardware_viewer(request, db)
    if error:
        return error
    local_node_id = get_node_id(db)
    selected_node_id = node_id or local_node_id
    if selected_node_id == local_node_id:
        servers = (
            db.query(Server).order_by(Server.name).all()
            if has_permission(user, "servers.view_all")
            else sorted(user.servers, key=lambda item: item.name.casefold())
        )
        stats = collect_hardware_stats(servers)
        stats.update({
            "app_version": APP_VERSION,
            "node": {"node_id": local_node_id, "name": "This Node", "app_version": APP_VERSION, "offline": False},
        })
        return stats

    node = db.query(RemoteNode).filter(RemoteNode.node_id == selected_node_id).first()
    if not node or not _remote_node_is_accessible(user, node):
        return JSONResponse({"error": "Node not found or access denied"}, status_code=404)

    try:
        token = decrypt_remote_token(node.token_ciphertext)
        stats = _request(node.base_url, token, "/api/node/hardware")
    except RemoteNodeUnavailable as remote_error:
        mark_remote_node_offline(db, node, str(remote_error))
        db.commit()
        return JSONResponse(
            {"error": "Node offline, try again later", "node_offline": True, "node_version": node.app_version},
            status_code=503,
        )
    except RemoteFunctionUnavailable:
        version = node.app_version or "unknown version"
        return JSONResponse(
            {
                "error": f"Hardware details are not available on this Node (Craftarr {version}). Some functions require a newer version.",
                "function_unavailable": True,
                "node_version": node.app_version,
            },
            status_code=501,
        )
    except (RemoteNodeError, ValueError) as remote_error:
        return JSONResponse({"error": str(remote_error), "node_version": node.app_version}, status_code=502)

    if not isinstance(stats, dict) or not isinstance(stats.get("minecraft"), dict):
        return JSONResponse({"error": "The remote Node returned invalid hardware details"}, status_code=502)

    mark_remote_node_online(db, node, stats.get("app_version"))
    allowed_ids = None if has_permission(user, "servers.view_all") else {
        server.server_id for server in user.remote_servers if server.node_id == node.node_id
    }
    instances = [
        {**instance, "id": f"{node.node_id}:{instance['id']}"}
        for instance in stats["minecraft"].get("instances", [])
        if isinstance(instance, dict)
        and (allowed_ids is None or instance.get("id") in allowed_ids)
    ]
    stats["minecraft"]["instances"] = instances
    stats["minecraft"]["installed"] = len(instances)
    stats["minecraft"]["running"] = sum(
        instance.get("state") == "running" or instance.get("running") is True
        for instance in instances
    )
    stats["minecraft"]["players_online"] = sum(int(instance.get("players") or 0) for instance in instances)
    stats["app_version"] = node.app_version or stats.get("app_version")
    stats["node"] = {
        "node_id": node.node_id,
        "name": node.name,
        "app_version": stats["app_version"],
        "offline": False,
    }
    db.commit()
    return stats


@router.get("/api/web/settings/nodes")
def list_remote_nodes(request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    nodes = db.query(RemoteNode).order_by(RemoteNode.name).all()
    return {"nodes": [_node_summary(node) for node in nodes]}


@router.post("/api/web/settings/nodes")
async def add_remote_node(request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "Enter valid Node details"}, status_code=400)
    name = str(data.get("name", "")).strip()
    token = str(data.get("token", "")).strip()
    if not 2 <= len(name) <= 100:
        return JSONResponse({"error": "Name must be 2 to 100 characters"}, status_code=400)
    if len(token) < 20 or len(token) > 200:
        return JSONResponse({"error": "Enter a valid node token"}, status_code=400)
    try:
        base_url = normalize_remote_url(
            str(data.get("base_url", "")),
            allow_insecure_http=data.get("allow_insecure_http") is True,
        )
        identity, inventory = fetch_remote_inventory(base_url, token)
    except RemoteNodeError as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    if str(identity.node_id) == get_node_id(db):
        return JSONResponse({"error": "This is the current Node"}, status_code=400)
    if db.query(RemoteNode).filter(RemoteNode.node_id == str(identity.node_id)).first():
        return JSONResponse({"error": "This Node is already linked"}, status_code=409)

    node = RemoteNode(
        node_id=str(identity.node_id),
        name=name,
        base_url=base_url,
        token_ciphertext=encrypt_remote_token(token),
    )
    db.add(node)
    try:
        db.flush()
        sync_node_servers(db, node, inventory, identity.app_version)
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse({"error": "A Node with this name, URL, or identity is already linked"}, status_code=409)

    db.refresh(node)
    run_linked_plugin_settings_sync(force=True)
    db.refresh(node)
    return {"success": True, "node": _node_summary(node)}


@router.put("/api/web/settings/nodes/{node_id}")
async def update_remote_node(node_id: str, request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
    if not node:
        return JSONResponse({"error": "Node not found"}, status_code=404)

    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "Enter valid Node details"}, status_code=400)
    name = str(data.get("name", node.name)).strip()
    if not 2 <= len(name) <= 100:
        return JSONResponse({"error": "Name must be 2 to 100 characters"}, status_code=400)
    token = str(data.get("token", "")).strip()
    try:
        base_url = normalize_remote_url(
            str(data.get("base_url", node.base_url)),
            allow_insecure_http=data.get("allow_insecure_http") is True,
        )
        secret = token if token else decrypt_remote_token(node.token_ciphertext)
        if token and not 20 <= len(token) <= 200:
            return JSONResponse({"error": "Enter a valid node token"}, status_code=400)
        identity, inventory = fetch_remote_inventory(base_url, secret)
        if str(identity.node_id) != node.node_id:
            return JSONResponse(
                {"error": "The Node at this URL has a different ID. Remove and add it again to avoid changing server assignments."},
                status_code=409,
            )
    except (RemoteNodeError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    node.name = name
    node.base_url = base_url
    if token:
        node.token_ciphertext = encrypt_remote_token(token)
    try:
        sync_node_servers(db, node, inventory, identity.app_version)
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse({"error": "A Node with this name or URL is already linked"}, status_code=409)
    db.refresh(node)
    run_linked_plugin_settings_sync(force=True)
    db.refresh(node)
    return {"success": True, "node": _node_summary(node)}


@router.post("/api/web/settings/nodes/{node_id}/refresh")
def refresh_node_inventory(node_id: str, request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
    if not node:
        return JSONResponse({"error": "Node not found"}, status_code=404)
    try:
        servers = refresh_remote_node(db, node)
    except RemoteNodeError as error:
        return JSONResponse({"error": str(error)}, status_code=502)
    return {"success": True, "server_count": len(servers), "node": _node_summary(node)}


@router.delete("/api/web/settings/nodes/{node_id}")
def delete_remote_node(node_id: str, request: Request, db: Session = Depends(get_db)):
    _user, error = _settings_admin(request, db)
    if error:
        return error
    node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
    if not node:
        return JSONResponse({"error": "Node not found"}, status_code=404)
    try:
        from .remote_nodes import _request

        token = decrypt_remote_token(node.token_ciphertext)
        _request(node.base_url, token, "/api/node/system-alerts/unlink", method="POST", payload={})
    except (RemoteNodeError, ValueError):
        pass
    db.delete(node)
    db.commit()
    return {"success": True}
