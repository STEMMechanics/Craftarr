"""Shared helpers for local and linked-node server assignments."""

import uuid

from sqlalchemy.orm import Session

from .models import RemoteServer, Server, User


def list_server_choices(db: Session) -> list[Server | RemoteServer]:
    servers: list[Server | RemoteServer] = list(
        db.query(Server).order_by(Server.name).all()
    )
    servers.extend(
        db.query(RemoteServer).order_by(RemoteServer.name, RemoteServer.node_id).all()
    )
    return sorted(servers, key=lambda server: (server.name.casefold(), str(getattr(server, "node_name", "Local")).casefold()))


def assignment_key(server: Server | RemoteServer) -> str:
    return str(server.id)


def assigned_server_keys(user: User) -> set[str]:
    return {
        *(str(server.id) for server in user.servers),
        *(server.server_ref for server in user.remote_servers),
    }


def apply_server_assignments(db: Session, user: User, values) -> None:
    local_ids: set[int] = set()
    remote_refs: set[tuple[str, int]] = set()
    try:
        for value in values or []:
            value = str(value)
            if ":" not in value:
                local_ids.add(int(value))
                continue
            node_part, server_part = value.rsplit(":", 1)
            node_id = str(uuid.UUID(node_part))
            server_id = int(server_part)
            if server_id <= 0:
                raise ValueError
            remote_refs.add((node_id, server_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Invalid server selection") from error

    local_servers = (
        db.query(Server).filter(Server.id.in_(local_ids)).all()
        if local_ids else []
    )
    remote_servers = []
    for node_id, server_id in sorted(remote_refs):
        remote = (
            db.query(RemoteServer)
            .filter(RemoteServer.node_id == node_id, RemoteServer.server_id == server_id)
            .first()
        )
        if remote is None:
            raise ValueError("A selected server is no longer available")
        remote_servers.append(remote)
    if len(local_servers) != len(local_ids):
        raise ValueError("A selected server is no longer available")

    user.servers = local_servers
    user.remote_servers = remote_servers
