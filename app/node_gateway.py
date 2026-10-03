"""Server-side gateway for a hub console's linked remote Craftarr nodes."""

import base64
import json
import re
from dataclasses import dataclass

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .database import SessionLocal
from .instance_identity import get_node_id
from .models import RemoteNode, RemoteServer, Server, User
from .node_security import decrypt_remote_token
from .permissions import ALL_PERMISSIONS, has_permission
from .web_context import get_available_servers


SERVER_REF = re.compile(r"(?P<node>[0-9a-fA-F-]{36}):(?P<server_id>[1-9][0-9]*)(?=/|$)")
HTML_SERVER_PATH = re.compile(rb"(?P<prefix>/servers/)(?P<id>[0-9]+)(?=[/?#&\"'\s])")
HTML_SERVER_DATA = re.compile(rb"(?P<prefix>data-server-id=[\"'])(?P<id>[0-9]+)(?P<suffix>[\"'])")
HTML_ACTIVE_SERVER = re.compile(rb"(?P<prefix>active_server_id=)(?P<id>[0-9]+)")

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "set-cookie",
}


@dataclass(frozen=True)
class HubServerChoice:
    """Hub-wide server entry used by a managed console's page shell."""

    id: str
    name: str
    node_name: str
    minecraft_version: str | None
    memory: str


def _required_permissions(path: str, method: str) -> set[str] | None:
    match = SERVER_REF.search(path)
    if not match:
        return None
    suffix = path[match.end():].strip("/")
    parts = suffix.split("/") if suffix else []
    method = method.upper()
    first = parts[0] if parts else ""
    second = parts[1] if len(parts) > 1 else ""

    if first in {"", "performance", "audit"}:
        return {"servers.view"}
    if first in {"start", "stop", "restart"}:
        return {"servers.control"}
    if first == "delete":
        return {"servers.delete"}
    if first in {"console", "console-data", "logs"}:
        return {"console.view"}
    if first == "command":
        return {"console.command"}
    if first == "players":
        return {"players.view"} if method in {"GET", "HEAD"} and second not in {"action", "ip-bans"} else {"players.manage"}
    if first == "ip-bans" or first == "whitelist-enabled":
        return {"players.manage"}
    if first == "plugins":
        if method in {"GET", "HEAD"} or second in {"check-updates", "check-update", "update-progress"}:
            return {"plugins.view"}
        return {"plugins.manage"}
    if first == "paper":
        if method in {"GET", "HEAD"} and second == "update-status":
            return {"servers.view"}
        return {"servers.properties"}
    if first in {"properties", "advanced-properties", "systemd-enabled", "name", "settings-export"}:
        return {"servers.properties"}
    if first == "port-warning":
        return {"servers.properties"}
    if first == "settings" and second == "import":
        return {"servers.properties"}
    if first in {"files", "export"}:
        if first == "export" or method in {"GET", "HEAD"}:
            return {"files.view"}
        return {"files.manage"}
    if first == "backups":
        return {"backups.view"} if method in {"GET", "HEAD"} and second not in {"restore", "delete", "create", "jobs"} else {"backups.manage"}
    if first in {"automation", "scheduling", "schedules"}:
        return {"automation.manage", "backups.view"} if method in {"GET", "HEAD"} else {"automation.manage"}
    if first in {"status", "process-stats", "metrics"}:
        return {"servers.view"}
    return None


def _permitted(user: User, required: set[str], path: str) -> bool:
    match = SERVER_REF.search(path)
    suffix = path[match.end():].strip("/") if match else ""
    parts = suffix.split("/") if suffix else []
    if parts[:3] == ["plugins", "monitoring", "global"]:
        return all(has_permission(user, permission) for permission in {"plugins.manage", "settings.manage"})
    return any(has_permission(user, permission) for permission in required)


def _forbidden(request: Request, status_code: int, message: str):
    if request.url.path.startswith("/api/") or request.headers.get("HX-Request") == "true":
        return JSONResponse({"error": message}, status_code=status_code)
    if status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return Response(message, status_code=status_code, media_type="text/plain")


def _proxy_user_claim(
    user: User,
    node_id: str,
    all_server_ids: list[int],
    hub_node_id: str,
    db: Session,
) -> str:
    permissions = sorted(key for key in ALL_PERMISSIONS if has_permission(user, key))
    if has_permission(user, "servers.view_all"):
        server_ids = all_server_ids
    else:
        server_ids = [server.server_id for server in user.remote_servers if server.node_id == node_id]
    hub_servers = []
    for server in get_available_servers(db, user):
        if isinstance(server, RemoteServer) and server.node_id == node_id:
            continue
        if isinstance(server, Server):
            server_ref = f"{hub_node_id}:{server.id}"
            node_name = "Local"
        else:
            server_ref = server.server_ref
            node_name = server.node_name
        hub_servers.append({
            "id": server_ref,
            "name": server.name,
            "node_name": node_name,
            "minecraft_version": server.minecraft_version,
            "memory": server.memory,
        })
    payload = {
        "username": user.username,
        "permissions": permissions,
        "servers": sorted(set(server_ids)),
        "hub_servers": hub_servers,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")


async def maybe_proxy_remote_server(request: Request):
    match = SERVER_REF.search(request.url.path)
    if not match:
        return None

    try:
        server_id = int(match.group("server_id"))
    except ValueError:
        return _forbidden(request, 404, "Remote server not found")
    node_id = match.group("node").lower()
    local_db: Session = SessionLocal()
    try:
        if node_id == get_node_id(local_db):
            rewritten_path = (
                request.url.path[:match.start("node")]
                + match.group("server_id")
                + request.url.path[match.end("server_id"):]
            )
            request.scope["path"] = rewritten_path
            request.scope["raw_path"] = rewritten_path.encode("ascii")
            return None
    finally:
        local_db.close()

    required = _required_permissions(request.url.path, request.method)
    if required is None:
        return _forbidden(request, 404, "Remote route not found")

    db: Session = SessionLocal()
    try:
        user_id = request.session.get("user_id")
        user = db.get(User, user_id) if user_id else None
        if not user or not user.enabled:
            return _forbidden(request, 401, "Not authenticated")
        if not has_permission(user, "servers.view"):
            return _forbidden(request, 403, "Server view permission required")

        node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
        if not node:
            return _forbidden(request, 404, "Remote node is not linked")
        remote_server = (
            db.query(RemoteServer)
            .filter(RemoteServer.node_id == node_id, RemoteServer.server_id == server_id)
            .first()
        )
        if not remote_server:
            return _forbidden(request, 404, "Remote server is not in the linked inventory")
        if not has_permission(user, "servers.view_all") and remote_server not in user.remote_servers:
            return _forbidden(request, 403, "You do not have access to this server")
        if not _permitted(user, required, request.url.path):
            return _forbidden(request, 403, "Permission required for this server action")

        try:
            token = decrypt_remote_token(node.token_ciphertext)
        except ValueError as error:
            return _forbidden(request, 502, str(error))
        base_url = node.base_url.rstrip("/")
        target_path = (
            request.url.path[:match.start("node")]
            + str(server_id)
            + request.url.path[match.end("server_id"):]
        )
        target_url = f"{base_url}{target_path}"
        if request.url.query:
            target_url += f"?{request.url.query}"
        proxy_claim = _proxy_user_claim(
            user,
            node_id,
            [server.server_id for server in node.servers],
            get_node_id(db),
            db,
        )
        actor_name = user.username
    finally:
        db.close()

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in {
            "accept", "accept-language", "content-type", "hx-current-url", "hx-request",
            "hx-target", "hx-trigger", "hx-trigger-name", "range", "user-agent",
        }
    }
    headers["Authorization"] = f"Bearer {token}"
    headers["X-Craftarr-User"] = proxy_claim
    headers["X-Craftarr-Actor"] = actor_name

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=8.0), follow_redirects=False)
    try:
        outgoing = client.build_request(
            request.method,
            target_url,
            headers=headers,
            content=request.stream(),
        )
        response = await client.send(outgoing, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return _forbidden(request, 504, "The linked Craftarr did not respond in time")
    except httpx.HTTPError:
        await client.aclose()
        return _forbidden(request, 502, "Could not connect to the linked Craftarr")

    if response.status_code == 401:
        await response.aclose()
        await client.aclose()
        return JSONResponse(
            {"error": "The linked Craftarr rejected its saved token. Update the token in App settings."},
            status_code=502,
        )

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length"
    }
    response_headers["cache-control"] = "no-store"
    if "location" in response_headers:
        response_headers["location"] = _rewrite_remote_server_ids(response_headers["location"], node_id)

    if "text/html" in response.headers.get("content-type", "").lower():
        try:
            body = await response.aread()
        finally:
            await response.aclose()
            await client.aclose()
        body = _rewrite_remote_html(body, node_id)
        response_headers["content-length"] = str(len(body))
        response_headers.pop("content-encoding", None)
        return Response(
            body,
            status_code=response.status_code,
            headers=response_headers,
            media_type=None,
        )

    async def close_upstream():
        await response.aclose()
        await client.aclose()

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=response_headers,
        background=BackgroundTask(close_upstream),
    )


def _rewrite_remote_server_ids(value: str, node_id: str) -> str:
    return re.sub(r"(?P<prefix>/servers/)(?P<id>[0-9]+)(?=[/?#&\"'\s]|$)", rf"\g<prefix>{node_id}:\g<id>", value)


def _rewrite_remote_html(body: bytes, node_id: str) -> bytes:
    body = HTML_SERVER_PATH.sub(lambda match: match.group("prefix") + node_id.encode("ascii") + b":" + match.group("id"), body)
    body = HTML_SERVER_DATA.sub(lambda match: match.group("prefix") + node_id.encode("ascii") + b":" + match.group("id") + match.group("suffix"), body)
    body = HTML_ACTIVE_SERVER.sub(lambda match: match.group("prefix") + node_id.encode("ascii") + b":" + match.group("id"), body)
    return body
