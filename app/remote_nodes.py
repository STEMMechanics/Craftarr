"""Remote Craftarr link validation, inventory, and HTTP client helpers."""

import ipaddress
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from .models import RemoteNode, RemoteServer
from .node_security import decrypt_remote_token
from .schemas import NodeIdentityOut, NodeServerOut


class RemoteNodeError(ValueError):
    pass


def normalize_remote_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise RemoteNodeError("Enter a valid Craftarr URL") from error
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise RemoteNodeError("Enter a Craftarr URL beginning with https://")
    if parsed.username or parsed.password:
        raise RemoteNodeError("Do not include a username or password in the Craftarr URL")
    if parsed.query or parsed.fragment:
        raise RemoteNodeError("The Craftarr URL cannot include a query string or fragment")

    if parsed.scheme != "https":
        host = parsed.hostname.lower()
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise RemoteNodeError("Remote Craftarr connections must use HTTPS")

    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    normalized = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    if len(normalized) > 1000:
        raise RemoteNodeError("Craftarr URL cannot exceed 1000 characters")
    return normalized


def _request(base_url: str, token: str, path: str) -> dict | list:
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False) as client:
            response = client.get(
                f"{base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as error:
        raise RemoteNodeError("The remote Craftarr did not respond before the timeout") from error
    except httpx.HTTPError as error:
        raise RemoteNodeError("Could not connect to the remote Craftarr") from error

    if response.status_code == 401:
        raise RemoteNodeError("The remote Craftarr rejected this token")
    if response.status_code != 200:
        raise RemoteNodeError(f"The remote Craftarr returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise RemoteNodeError("The remote Craftarr returned an invalid response") from error


def fetch_remote_inventory(base_url: str, token: str) -> tuple[NodeIdentityOut, list[NodeServerOut]]:
    identity_data = _request(base_url, token, "/api/node/identity")
    servers_data = _request(base_url, token, "/api/node/servers")
    if not isinstance(identity_data, dict) or not isinstance(servers_data, list):
        raise RemoteNodeError("The remote Craftarr returned an invalid identity or server list")
    try:
        identity = NodeIdentityOut.model_validate(identity_data)
        servers = [NodeServerOut.model_validate(server) for server in servers_data]
    except Exception as error:
        raise RemoteNodeError("The remote Craftarr returned an unsupported API response") from error
    return identity, servers


def sync_node_servers(
    db: Session,
    node: RemoteNode,
    inventory: list[NodeServerOut] | None = None,
) -> list[RemoteServer]:
    if inventory is None:
        token = decrypt_remote_token(node.token_ciphertext)
        _identity, inventory = fetch_remote_inventory(node.base_url, token)

    existing = {server.server_id: server for server in node.servers}
    seen: set[int] = set()
    for remote in inventory:
        if remote.server_id <= 0:
            raise RemoteNodeError("The remote Craftarr returned an invalid server ID")
        if remote.server_id in seen:
            raise RemoteNodeError("The remote Craftarr returned a duplicate server ID")
        seen.add(remote.server_id)
        server = existing.get(remote.server_id)
        if server is None:
            server = RemoteServer(node_id=node.node_id, server_id=remote.server_id)
            db.add(server)
        server.name = remote.name
        server.minecraft_version = remote.minecraft_version
        server.paper_build = remote.paper_build
        server.memory = remote.memory
        server.min_memory = remote.min_memory
        server.port = remote.port
        server.enabled = remote.enabled

    for server_id, server in existing.items():
        if server_id not in seen:
            db.delete(server)

    node.last_connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    node.last_error = None
    db.flush()
    db.expire(node, ["servers"])
    return list(node.servers)


def refresh_remote_node(db: Session, node: RemoteNode) -> list[RemoteServer]:
    try:
        token = decrypt_remote_token(node.token_ciphertext)
        identity, inventory = fetch_remote_inventory(node.base_url, token)
        if str(identity.node_id) != node.node_id:
            raise RemoteNodeError(
                "The Craftarr at this URL has a different node ID. Remove and link it again to avoid mixing server access."
            )
        servers = sync_node_servers(db, node, inventory)
        db.commit()
        return servers
    except ValueError as error:
        node.last_error = str(error)[:255]
        db.commit()
        if isinstance(error, RemoteNodeError):
            raise
        raise RemoteNodeError(str(error)) from error
