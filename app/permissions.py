from fastapi import HTTPException

from sqlalchemy.orm import Session

from .models import Server, User


PERMISSION_GROUPS = {
    "Servers": {
        "servers.view": "View assigned servers",
        "servers.view_all": "View every server",
        "servers.create": "Create and import servers",
        "servers.delete": "Delete servers",
        "servers.control": "Start, stop, and restart servers",
        "servers.properties": "Edit server properties and versions",
    },
    "Console and players": {
        "console.view": "View server consoles",
        "console.command": "Send console commands",
        "players.view": "View players and bans",
        "players.manage": "Manage players, whitelist, and bans",
    },
    "Content and data": {
        "plugins.view": "View installed plugins",
        "plugins.manage": "Install, configure, and remove plugins",
        "files.view": "Browse and download server files",
        "files.manage": "Upload, edit, move, and delete files",
        "backups.view": "View and download backups",
        "backups.manage": "Create, restore, delete, and schedule backups",
        "automation.manage": "Manage schedules and automation",
    },
    "Administration": {
        "system.view": "View system information",
        "system.manage": "Control services and application updates",
        "users.manage": "Create and manage users",
        "roles.manage": "Create and manage roles",
        "settings.manage": "Manage global console settings",
    },
}

ALL_PERMISSIONS = {
    key: label
    for permissions in PERMISSION_GROUPS.values()
    for key, label in permissions.items()
}

# Matches the capabilities available to a pre-0.2.0 non-administrator.
LEGACY_USER_PERMISSIONS = {
    "servers.view",
    "servers.control",
    "servers.properties",
    "console.view",
    "console.command",
    "players.view",
    "players.manage",
    "plugins.view",
    "files.view",
    "files.manage",
    "backups.view",
    "backups.manage",
}


def has_permission(user: User | None, permission: str) -> bool:
    if not user or not user.enabled:
        return False
    if user.role == "admin" and user.access_role is None:
        return True
    if permission == "servers.view" and user.can("servers.view_all"):
        return True
    return user.can(permission)


def has_any_permission(user: User | None, *permissions: str) -> bool:
    return any(has_permission(user, permission) for permission in permissions)


def get_server_for_user(
    db: Session,
    server_id: int,
    user: User,
):

    server = db.get(
        Server,
        server_id,
    )

    if not server:

        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )

    if has_permission(user, "servers.view_all"):
        return server

    # Normal user requires assignment
    for assigned_server in user.servers:

        if assigned_server.id == server.id:
            return server

    raise HTTPException(
        status_code=403,
        detail="You do not have access to this server",
    )
