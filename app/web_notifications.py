"""In-app notifications assembled from the latest monitored update results."""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import get_db
from .models import BackupJob, RemoteNode, ServerUpdateCheck
from .permissions import has_permission
from .web_context import get_available_servers
from .web_users import current_web_user
from .update_monitor import paper_result, plugin_results


router = APIRouter()


def _notification_read_key(user_id: int) -> str:
    return f"notification_read_ids:user:{user_id}"


@router.get("/api/web/notifications/read")
def get_notification_read_state(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    from .settings_manager import get_setting

    try:
        values = json.loads(get_setting(db, _notification_read_key(user.id), "[]"))
    except (TypeError, ValueError):
        values = []
    read_ids = [value[:512] for value in values if isinstance(value, str) and value][:200] if isinstance(values, list) else []
    return JSONResponse({"read_ids": read_ids}, headers={"Cache-Control": "no-store"})


@router.post("/api/web/notifications/read")
async def save_notification_read_state(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        data = await request.json()
        values = data.get("read_ids") if isinstance(data, dict) else None
        if not isinstance(values, list):
            raise ValueError("Read notification IDs must be a list")
        values = values[-1000:]
        read_ids = list(dict.fromkeys(value[:512] for value in values if isinstance(value, str) and value))[-200:]
    except (TypeError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    from .settings_manager import set_setting

    set_setting(db, _notification_read_key(user.id), json.dumps(read_ids, separators=(",", ":")))
    db.commit()
    return {"success": True}


@router.get("/api/web/notifications")
def notifications(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    servers = get_available_servers(db, user)
    server_by_id = {server.id: server for server in servers}
    server_ids = list(server_by_id)
    checks = (
        db.query(ServerUpdateCheck)
        .filter(ServerUpdateCheck.server_id.in_(server_ids))
        .all()
    ) if server_ids else []
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
    if server_ids and has_permission(user, "backups.view"):
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
            offsite_failed = (
                status == "complete"
                and "off-site copy failed" in (job.message or "").casefold()
            )
            notification_status = "warning" if offsite_failed else status
            label = job.label or job.filename or "Backup"
            title = {
                "complete": "Backup completed",
                "failed": "Backup failed",
                "cancelled": "Backup cancelled",
                "warning": "Backup needs attention",
            }[notification_status]
            message = f"{server.name} · {label}"
            if (status != "complete" or offsite_failed) and job.message:
                message += f" · {job.message}"
            backup_events.append({
                "id": f"backup:{job.id}:{notification_status}",
                "kind": f"backup-{notification_status}",
                "server": server.name,
                "title": title,
                "message": message,
                "url": f"/servers/{server.id}/backups",
                "checked_at": job.finished_at.isoformat() + "Z",
            })

    items = []
    if has_permission(user, "settings.manage"):
        import shutil

        import psutil

        from .settings_manager import get_setting, get_system_alert_settings

        alert_settings = get_system_alert_settings(db)
        if alert_settings["enabled"]:
            disk = shutil.disk_usage("/")
            readings = {
                "memory": round(float(psutil.virtual_memory().percent), 1),
                "storage": round(disk.used / disk.total * 100, 1),
            }
            thresholds = {
                "memory": alert_settings["memory_percent"],
                "storage": alert_settings["storage_percent"],
            }
            for resource, percent in readings.items():
                if percent < thresholds[resource]:
                    continue
                last_sent = get_setting(db, f"system_alert_last_sent_{resource}") or "active"
                items.append({
                    "id": f"system-alert:local:{resource}:{last_sent}",
                    "kind": "system-alert",
                    "title": f"This Node: {resource} usage is high",
                    "message": f"{resource.title()} usage is {percent:.1f}% (threshold {thresholds[resource]}%).",
                    "url": "/settings",
                    "checked_at": datetime.utcnow().isoformat() + "Z",
                })

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
    from .settings_manager import get_setting
    for node in db.query(RemoteNode).order_by(RemoteNode.name).all():
        accessible_remote_servers = [
            server for server in getattr(user, "remote_servers", [])
            if server.node_id == node.node_id
        ]
        has_remote_access = has_permission(user, "servers.view_all") or bool(accessible_remote_servers)
        if not has_remote_access and not has_permission(user, "settings.manage"):
            continue
        if node.last_error and (has_remote_access or has_permission(user, "settings.manage")):
            items.append({
                "id": f"remote-offline:{node.node_id}",
                "kind": "remote-offline",
                "title": f"{node.name} is unreachable",
                "message": "The hub cannot contact this Node. Its server list may be out of date.",
                "url": "/servers",
                "checked_at": node.last_connected_at.isoformat() + "Z" if node.last_connected_at else None,
            })
        try:
            remote_items = json.loads(get_setting(db, f"remote_notifications:{node.node_id}", "[]"))
        except (TypeError, ValueError):
            remote_items = []
        if not isinstance(remote_items, list):
            continue
        for remote_item in remote_items:
            if not isinstance(remote_item, dict):
                continue
            if remote_item.get("kind") == "system-alert":
                if has_permission(user, "settings.manage"):
                    item = dict(remote_item)
                    item["url"] = "/settings"
                    items.append(item)
                continue
            try:
                remote_server_id = int(remote_item.get("server_id"))
            except (TypeError, ValueError):
                continue
            server_ref = f"{node.node_id}:{remote_server_id}"
            server = server_by_id.get(server_ref)
            if not server:
                continue
            kind = remote_item.get("kind")
            if kind in {"plugin-update", "paper-update"}:
                if kind == "plugin-update" and not has_permission(user, "plugins.view"):
                    continue
                url = f"/servers/{server_ref}/plugins" if kind == "plugin-update" else f"/servers/{server_ref}"
            elif str(kind).startswith("backup-"):
                if not has_permission(user, "backups.view"):
                    continue
                url = f"/servers/{server_ref}/backups"
            else:
                continue
            item = dict(remote_item)
            item["id"] = f"{node.node_id}:{remote_item.get('id', '')}"
            item["server"] = server.name
            item["message"] = remote_item.get("message") or server.name
            item["url"] = url
            items.append(item)

    items.sort(key=lambda item: item.get("checked_at") or "", reverse=True)
    return {"notifications": items[:50]}
