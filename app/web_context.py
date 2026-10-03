from sqlalchemy.orm import Session

from .models import RemoteServer, Server, User

from .version import APP_VERSION
from .permissions import has_permission
from .processes import systemd_available

def get_available_servers(
    db: Session,
    user: User,
):
    if not has_permission(user, "servers.view"):
        return []

    if has_permission(user, "servers.view_all"):
        local_servers = (
            db.query(Server)
            .order_by(Server.name)
            .all()
        )
        remote_servers = db.query(RemoteServer).order_by(RemoteServer.name).all()
    else:
        local_servers = list(user.servers)
        remote_servers = list(getattr(user, "remote_servers", []))
    hub_servers = list(getattr(user, "hub_servers", []))

    servers_by_id = {
        str(server.id): server
        for server in [*local_servers, *remote_servers, *hub_servers]
    }

    return sorted(
        servers_by_id.values(),
        key=lambda server: (server.name.casefold(), str(getattr(server, "node_name", "Local")).casefold()),
    )


def build_web_context(
    db: Session,
    user: User,
    active_server=None,
):
    available_servers = get_available_servers(
        db,
        user,
    )

    if (
        active_server is None
        and available_servers
    ):
        active_server = available_servers[0]

    return {
        "user": user,
        "available_servers": available_servers,
        "active_server": active_server,
        "app_version": APP_VERSION,
        "systemd_available": systemd_available(),
    }
