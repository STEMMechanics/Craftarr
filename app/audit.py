import re
from datetime import datetime

from sqlalchemy.orm import Session

from .models import ServerAuditEvent


SERVER_ROUTE = re.compile(r"^/(?:api/web/servers|servers)/(\d+)(?:/|$)")

AUDIT_ACTIONS = {
    "players/action": "Player action",
    "ip-bans/action": "IP ban action",
    "whitelist-enabled": "Whitelist setting changed",
    "properties": "Server settings changed",
    "advanced-properties": "Advanced server settings changed",
    "settings/import": "Server settings imported",
    "systemd-enabled": "Automatic startup setting changed",
    "name": "Server renamed",
    "plugins/upload": "Plugin uploaded",
    "plugins/url": "Plugin installed from URL",
    "plugins/update": "Plugin updated",
    "plugins/action": "Plugin changed",
    "plugins/correct-filename": "Plugin filename corrected",
    "plugins/duplicates/resolve": "Duplicate plugins resolved",
    "plugins/monitoring": "Plugin monitoring settings changed",
    "plugins/monitoring/global": "Plugin monitoring settings changed",
    "paper": "Paper server updated",
    "backups/create": "Backup created",
    "backups/delete": "Backup deleted",
    "backups/restore": "Backup restored",
    "backups/jobs": "Backup job changed",
    "schedules": "Scheduled task changed",
    "files/create": "Server file created",
    "files/save": "Server file saved",
    "files/move": "Server file moved",
    "files/delete": "Server file deleted",
    "files/upload": "Server file uploaded",
    "files/zip": "Server files archived",
    "files/extract": "Server archive extracted",
    "start": "Server started",
    "stop": "Server stopped",
    "restart": "Server restarted",
    "command": "Server command sent",
    "delete": "Server deleted",
}


def server_id_from_request(request) -> int | None:
    match = SERVER_ROUTE.match(request.url.path)
    return int(match.group(1)) if match else None


def describe_request_action(request) -> str:
    explicit_action = getattr(request.state, "audit_action", None)
    if explicit_action:
        return str(explicit_action)[:255]

    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    suffix = re.sub(
        r"^/(?:api/web/servers|servers)/\{server_id(?::int)?\}/?",
        "",
        route_path,
    ).strip("/")
    if suffix == route_path:
        path_match = SERVER_ROUTE.match(request.url.path)
        suffix = request.url.path[path_match.end():].strip("/") if path_match else "server change"

    if suffix in AUDIT_ACTIONS:
        return AUDIT_ACTIONS[suffix]

    # Collapse dynamic IDs to a readable resource name for endpoints without a
    # dedicated label, such as backup cancellation and schedule runs.
    parts = [part for part in suffix.split("/") if part and not part.startswith("{")]
    if parts:
        resource = " ".join(part.replace("-", " ") for part in parts[-2:])
        return f"Server change: {resource}"[:255]
    return "Server changed"


def record_audit_event(
    db: Session,
    *,
    server_id: int,
    server_name: str,
    actor_user_id: int | None,
    actor_username: str,
    action: str,
    details: str | None = None,
    reason: str | None = None,
) -> ServerAuditEvent:
    event = ServerAuditEvent(
        server_id=server_id,
        server_name=(server_name or f"Server {server_id}")[:100],
        actor_user_id=actor_user_id,
        actor_username=(actor_username or "Unknown user")[:64],
        action=(action or "Server changed")[:255],
        details=details[:2000] if details else None,
        reason=reason[:2000] if reason else None,
        created_at=datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    return event
