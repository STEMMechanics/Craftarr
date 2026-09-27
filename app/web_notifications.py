"""In-app notifications assembled from the latest monitored update results."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import get_db
from .models import BackupJob, ServerUpdateCheck
from .permissions import has_permission
from .web_context import get_available_servers
from .web_users import current_web_user
from .update_monitor import paper_result, plugin_results


router = APIRouter()


@router.get("/api/web/notifications")
def notifications(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    servers = get_available_servers(db, user)
    if not servers:
        return {"notifications": []}

    server_by_id = {server.id: server for server in servers}
    server_ids = list(server_by_id)
    checks = (
        db.query(ServerUpdateCheck)
        .filter(ServerUpdateCheck.server_id.in_(server_ids))
        .all()
    )
    plugin_candidates = set()
    paper_candidates = set()
    for check in checks:
        try:
            snapshot = json.loads(check.payload)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(snapshot, dict)
            and snapshot.get("update_available") is True
            and snapshot.get("status") in {"Update available", "Compatibility unknown"}
        ):
            (paper_candidates if check.component == "@paper" else plugin_candidates).add(check.server_id)

    backup_events = []
    if has_permission(user, "backups.view"):
        recent_cutoff = datetime.utcnow() - timedelta(days=30)
        jobs = (
            db.query(BackupJob)
            .filter(
                BackupJob.server_id.in_(server_ids),
                BackupJob.status.in_(("complete", "failed", "cancelled")),
                BackupJob.finished_at >= recent_cutoff,
            )
            .order_by(BackupJob.finished_at.desc())
            .limit(100)
            .all()
        )
        for job in jobs:
            server = server_by_id.get(job.server_id)
            if not server or not job.finished_at:
                continue
            status = job.status
            label = job.label or job.filename or "Backup"
            title = {
                "complete": "Backup completed",
                "failed": "Backup failed",
                "cancelled": "Backup cancelled",
            }[status]
            message = f"{server.name} · {label}"
            if status != "complete" and job.message:
                message += f" · {job.message}"
            backup_events.append({
                "id": f"backup:{job.id}:{status}",
                "kind": f"backup-{status}",
                "server": server.name,
                "title": title,
                "message": message,
                "url": f"/servers/{server.id}/backups",
                "checked_at": job.finished_at.isoformat() + "Z",
            })

    items = []
    for server_id in plugin_candidates | paper_candidates:
        server = server_by_id.get(server_id)
        if not server:
            continue
        results = []
        if server_id in plugin_candidates and has_permission(user, "plugins.view"):
            try:
                results.extend(plugin_results(db, server))
            except (OSError, TypeError, ValueError):
                pass
        if server_id in paper_candidates:
            results.append(paper_result(db, server))

        for result in results:
            if not isinstance(result, dict) or result.get("update_available") is not True:
                continue
            if result.get("status") not in {"Update available", "Compatibility unknown"}:
                continue

            is_paper = result.get("component") == "@paper"
            name = "Paper" if is_paper else str(result.get("plugin") or "Plugin")
            latest = str(result.get("latest_version") or "a newer version")
            installed = str(result.get("installed_version") or "unknown")
            target = f"/servers/{server.id}" if is_paper else f"/servers/{server.id}/plugins"
            component = str(result.get("component") or name)
            items.append({
                "id": f"{server.id}:{component}:{latest}",
                "kind": "paper-update" if is_paper else "plugin-update",
                "server": server.name,
                "title": f"{name} update available",
                "message": f"{server.name} · {installed} → {latest}",
                "url": target,
                "checked_at": result.get("checked_at"),
            })

    items.extend(backup_events)
    items.sort(key=lambda item: item.get("checked_at") or "", reverse=True)
    return {"notifications": items[:50]}
