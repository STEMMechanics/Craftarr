from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .instance_identity import get_node_id
from .models import RemoteNode, User
from .node_security import (
    decrypt_remote_token,
    encrypt_remote_token,
    generate_node_token,
    node_token_status,
)
from .permissions import has_permission
from .remote_nodes import (
    RemoteNodeError,
    fetch_remote_inventory,
    normalize_remote_url,
    refresh_remote_node,
    run_linked_plugin_settings_sync,
    sync_node_servers,
)
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
        "last_connected_at": node.last_connected_at.isoformat() if node.last_connected_at else None,
        "last_error": node.last_error,
    }


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
        return JSONResponse({"error": "Enter valid console details"}, status_code=400)
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
        return JSONResponse({"error": "This is the current Craftarr instance"}, status_code=400)
    if db.query(RemoteNode).filter(RemoteNode.node_id == str(identity.node_id)).first():
        return JSONResponse({"error": "This Craftarr node is already linked"}, status_code=409)

    node = RemoteNode(
        node_id=str(identity.node_id),
        name=name,
        base_url=base_url,
        token_ciphertext=encrypt_remote_token(token),
    )
    db.add(node)
    try:
        db.flush()
        sync_node_servers(db, node, inventory)
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse({"error": "A node with this name, URL, or identity is already linked"}, status_code=409)

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
        return JSONResponse({"error": "Remote node not found"}, status_code=404)

    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "Enter valid console details"}, status_code=400)
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
                {"error": "The Craftarr at this URL has a different node ID. Remove and add it again to avoid changing server assignments."},
                status_code=409,
            )
    except (RemoteNodeError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    node.name = name
    node.base_url = base_url
    if token:
        node.token_ciphertext = encrypt_remote_token(token)
    try:
        sync_node_servers(db, node, inventory)
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse({"error": "A node with this name or URL is already linked"}, status_code=409)
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
        return JSONResponse({"error": "Remote node not found"}, status_code=404)
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
        return JSONResponse({"error": "Remote node not found"}, status_code=404)
    try:
        from .remote_nodes import _request

        token = decrypt_remote_token(node.token_ciphertext)
        _request(node.base_url, token, "/api/node/system-alerts/unlink", method="POST", payload={})
    except (RemoteNodeError, ValueError):
        pass
    db.delete(node)
    db.commit()
    return {"success": True}
