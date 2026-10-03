from datetime import datetime, timedelta
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import get_db
from .instance_identity import get_node_id
from .models import BackupJob, Server, ServerUpdateCheck
from .node_security import verify_node_token
from .schemas import NodeIdentityOut, NodeServerOut
from .settings_manager import get_system_alert_settings, save_system_alert_settings, set_setting
from .version import APP_VERSION


router = APIRouter(prefix="/api/node", tags=["Node connection"])


def require_node_token(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not verify_node_token(db, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid node token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/identity", response_model=NodeIdentityOut)
def node_identity(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return {"node_id": get_node_id(db), "app_version": APP_VERSION}


@router.get("/servers", response_model=list[NodeServerOut])
def node_servers(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "server_id": server.id,
            "name": server.name,
            "minecraft_version": server.minecraft_version,
            "paper_build": server.paper_build,
            "memory": server.memory,
            "min_memory": server.min_memory,
            "port": server.port,
            "enabled": server.enabled,
        }
        for server in db.query(Server).order_by(Server.name).all()
    ]


@router.get("/hardware")
def node_hardware(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    from .hardware import collect_hardware_stats

    response.headers["Cache-Control"] = "no-store"
    stats = collect_hardware_stats(db.query(Server).order_by(Server.name).all())
    stats["app_version"] = APP_VERSION
    return stats


@router.get("/plugin-monitoring-settings")
def node_plugin_monitoring_settings(
    response: Response,
    _authorized: None = Depends(require_node_token),
):
    from .monitoring_defaults import read_repository_snapshot

    response.headers["Cache-Control"] = "no-store"
    try:
        return read_repository_snapshot()
    except ValueError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@router.put("/plugin-monitoring-settings")
async def update_node_plugin_monitoring_settings(
    request: Request,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    from .monitoring_defaults import MAX_REPOSITORY_BYTES, save_repository_text
    from .models import UpdateMonitorLease
    from .update_monitor import CheckInProgress, acquire_lease

    raw = await request.body()
    if len(raw) > MAX_REPOSITORY_BYTES * 2 + 1024:
        return JSONResponse({"error": "The shared plugin settings file must be 256 KiB or smaller"}, status_code=413)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Invalid plugin settings document")
        content = data.get("content")
        modified_at_ns = data.get("modified_at_ns")
        if type(modified_at_ns) is not int or modified_at_ns < 0:
            raise ValueError("Invalid plugin settings revision")
    except (ValueError, TypeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    try:
        acquire_lease(db, datetime.utcnow())
    except CheckInProgress as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    try:
        try:
            result = save_repository_text(content, modified_at_ns=modified_at_ns)
            return JSONResponse({"success": True, "plugins": result["plugins"],
                                 "modified_at_ns": result["modified_at_ns"]})
        except (ValueError, TypeError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
    finally:
        db.rollback()
        db.query(UpdateMonitorLease).filter_by(id=1).update({"expires_at": datetime.min})
        db.commit()


@router.get("/notifications")
def node_notifications(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    servers = {server.id: server for server in db.query(Server).all()}
    items = []
    for check in db.query(ServerUpdateCheck).all():
        server = servers.get(check.server_id)
        if not server:
            continue
        try:
            result = json.loads(check.payload)
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(result, dict)
            or result.get("update_available") is not True
            or result.get("status") not in {"Update available", "Compatibility unknown"}
        ):
            continue
        is_paper = check.component == "@paper"
        name = "Paper" if is_paper else str(result.get("plugin") or "Plugin")
        latest = str(result.get("latest_version") or "a newer version")
        installed = str(result.get("installed_version") or "unknown")
        items.append({
            "id": f"update:{server.id}:{check.component}:{latest}",
            "kind": "paper-update" if is_paper else "plugin-update",
            "server_id": server.id,
            "server": server.name,
            "component": check.component,
            "title": f"{name} update available",
            "message": f"{server.name} · {installed} → {latest}",
            "installed_version": installed,
            "latest_version": latest,
            "checked_at": result.get("checked_at"),
        })

    recent_cutoff = datetime.utcnow() - timedelta(days=30)
    jobs = db.query(BackupJob).filter(
        BackupJob.status.in_(("complete", "failed", "cancelled")),
        BackupJob.finished_at >= recent_cutoff,
    ).order_by(BackupJob.finished_at.desc()).limit(100).all()
    for job in jobs:
        server = servers.get(job.server_id)
        if not server or not job.finished_at:
            continue
        offsite_failed = (
            job.status == "complete"
            and "off-site copy failed" in (job.message or "").casefold()
        )
        notification_status = "warning" if offsite_failed else job.status
        title = {
            "complete": "Backup completed",
            "failed": "Backup failed",
            "cancelled": "Backup cancelled",
            "warning": "Backup needs attention",
        }[notification_status]
        message = f"{server.name} · {job.label or job.filename or 'Backup'}"
        if (job.status != "complete" or offsite_failed) and job.message:
            message += f" · {job.message}"
        items.append({
            "id": f"backup:{job.id}:{notification_status}",
            "kind": f"backup-{notification_status}",
            "server_id": server.id,
            "server": server.name,
            "title": title,
            "message": message,
            "checked_at": job.finished_at.isoformat() + "Z",
        })

    items.sort(key=lambda item: item.get("checked_at") or "", reverse=True)
    return {"notifications": items[:100]}


@router.post("/system-alerts")
async def node_system_alerts(
    request: Request,
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    """Apply hub alert policy and report this host's current resource usage."""
    import shutil

    import psutil

    response.headers["Cache-Control"] = "no-store"
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid system alert settings")
        settings = {
            "enabled": data.get("enabled") is True,
            "memory_percent": int(data.get("memory_percent", 95)),
            "storage_percent": int(data.get("storage_percent", 80)),
            "cooldown_minutes": int(data.get("cooldown_minutes", 60)),
            "node_offline_delay_minutes": int(data.get("node_offline_delay_minutes", 5)),
        }
        current = get_system_alert_settings(db)
        if current != settings:
            save_system_alert_settings(db, settings)
        managed_by_hub = data.get("managed_by_hub") is True
        set_setting(db, "notifications_managed_by_hub", "true" if managed_by_hub else "false")
        if managed_by_hub:
            set_setting(db, "notifications_hub_seen_at", datetime.utcnow().isoformat())
        db.commit()
    except (TypeError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    readings = {"memory": round(float(psutil.virtual_memory().percent), 1)}
    disk = shutil.disk_usage("/")
    readings["storage"] = round(disk.used / disk.total * 100, 1)
    thresholds = {"memory": settings["memory_percent"], "storage": settings["storage_percent"]}
    alerts = [
        {"resource": resource, "percent": percent, "threshold": thresholds[resource]}
        for resource, percent in readings.items()
        if settings["enabled"] and percent >= thresholds[resource]
    ]
    return {"alerts": alerts}


@router.post("/system-alerts/unlink")
def unlink_node_system_alerts(
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    set_setting(db, "notifications_managed_by_hub", "false")
    db.commit()
    return {"success": True}


@router.get("/offsite-backups")
def node_offsite_backup_settings(
    response: Response,
    _authorized: None = Depends(require_node_token),
):
    from .offsite_backups import OffsiteBackupError, configured_remotes, managed_config_path, remote_settings

    response.headers["Cache-Control"] = "no-store"
    try:
        return {"available": True, "remotes": configured_remotes(refresh=True),
                "destinations": remote_settings(), "config": str(managed_config_path())}
    except OffsiteBackupError as error:
        message = str(error)
        return {
            "available": False,
            "remotes": [],
            "destinations": [],
            "reason": "not_installed" if "not installed" in message.lower() else "unavailable",
            "error": message,
        }


@router.post("/offsite-backups/test")
async def test_node_offsite_backup(request: Request, _authorized: None = Depends(require_node_token)):
    from .offsite_backups import OffsiteBackupError, destination_from_parts, test_destination

    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise OffsiteBackupError("Enter a valid destination")
        if "remote" in data:
            destination = destination_from_parts(str(data.get("remote", "")), str(data.get("path", "")))
        else:
            destination = str(data.get("destination", ""))
        test_destination(destination)
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"success": True}


@router.post("/offsite-backups/remotes")
async def save_node_offsite_remote(request: Request, _authorized: None = Depends(require_node_token)):
    from .offsite_backups import OffsiteBackupError, save_remote

    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise OffsiteBackupError("Enter valid destination settings")
        remote = save_remote(data)
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"success": True, "remote": remote}


@router.delete("/offsite-backups/remotes/{name}")
def delete_node_offsite_remote(name: str, _authorized: None = Depends(require_node_token)):
    from .offsite_backups import OffsiteBackupError, delete_remote

    try:
        delete_remote(name)
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {"success": True}


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def node_function_unavailable(
    path: str,
    response: Response,
    _authorized: None = Depends(require_node_token),
):
    """Give linked nodes a stable response for API functions they do not have."""
    response.headers["Cache-Control"] = "no-store"
    return JSONResponse(
        {
            "error": "This function is not available on this Node.",
            "function_unavailable": True,
            "function": path,
            "app_version": APP_VERSION,
        },
        status_code=501,
        headers={"Cache-Control": "no-store"},
    )
