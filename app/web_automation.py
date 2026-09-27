from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, not_
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import get_db
from .models import BackupJob, ScheduledTask, ServerMetric, TaskRun
from .web_servers import get_accessible_server
from .permissions import has_permission
from .automation import manual_backup_starting, next_task_run, start_task_now, validate_cron_expression
from .web_context import build_web_context
from .web_render import render_page
from .config import SCHEDULE_TIMEZONE_NAME
from .offsite_backups import OffsiteBackupError, configured_remotes, validate_destination


router = APIRouter()


@router.get("/servers/{server_id:int}/performance", response_class=HTMLResponse)
def server_performance_page(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "servers.view"):
        raise HTTPException(status_code=404, detail="Server not found")

    context = build_web_context(db, user, active_server=server)
    context.update({"server": server, "page_title": "Performance", "active_page": "performance"})
    return render_page(request, "server_performance.html", "partials/server_performance.html", context)


def _utc_iso(value: datetime | None) -> str | None:
    """Serialize database UTC datetimes with an explicit UTC designator."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _task_json(task):
    return {
        "id": task.id, "name": task.name, "task_type": task.task_type,
        "command": task.command, "interval_minutes": task.interval_minutes,
        "frequency": task.frequency, "run_hour": task.run_hour,
        "run_weekday": task.run_weekday,
        "cron_expression": task.cron_expression,
        "schedule_timezone": task.schedule_timezone or SCHEDULE_TIMEZONE_NAME,
        "remote_destination": task.remote_destination,
        "remote_retention_count": task.remote_retention_count,
        "retention_count": task.retention_count, "enabled": task.enabled,
        "next_run_at": _utc_iso(task.next_run_at),
        "last_run_at": _utc_iso(task.last_run_at),
    }


def _schedule_values(data: dict) -> dict:
    task_type = str(data.get("task_type", ""))
    name = str(data.get("name", "")).strip()
    command = str(data.get("command", "")).strip() or None
    frequency = str(data.get("frequency", "")).strip()
    cron_expression = str(data.get("cron_expression", "")).strip() or None
    schedule_timezone = str(data.get("schedule_timezone", "")).strip()[:100] or SCHEDULE_TIMEZONE_NAME
    try:
        ZoneInfo(schedule_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Choose a valid timezone") from error
    try:
        interval = int(data.get("interval_minutes", 0) or 0)
        retention = int(data["retention_count"]) if data.get("retention_count") else None
        run_hour = int(data["run_hour"]) if data.get("run_hour") not in (None, "") else None
        run_weekday = int(data["run_weekday"]) if data.get("run_weekday") not in (None, "") else None
        remote_retention = int(data["remote_retention_count"]) if data.get("remote_retention_count") else None
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid interval or retention") from error
    if frequency in {"hourly", "daily", "weekly", "monthly", "custom"}:
        interval = {"hourly": 60, "daily": 1440, "weekly": 10080, "monthly": 43200, "custom": 1}[frequency]
    elif not frequency:
        frequency = None
    else:
        raise ValueError("Choose a valid schedule")
    if frequency in {"daily", "weekly", "monthly"} and (run_hour is None or not 0 <= run_hour <= 23):
        raise ValueError("Choose an hour from 0 to 23")
    if frequency == "weekly" and (run_weekday is None or not 0 <= run_weekday <= 6):
        raise ValueError("Choose a day of the week")
    if frequency == "custom":
        validate_cron_expression(cron_expression or "")
    if task_type not in {"backup", "command"} or not name or interval < 1 or interval > 525600:
        raise ValueError("Type, name and a positive interval are required")
    if task_type == "command" and not command:
        raise ValueError("Command required")
    if task_type == "backup" and retention is not None and not 1 <= retention <= 10000:
        raise ValueError("Retention must be between 1 and 10000")
    if command and (len(command) > 500 or "\n" in command or "\r" in command):
        raise ValueError("Invalid command")
    remote_destination = str(data.get("remote_destination", "")).strip() or None
    if task_type == "backup" and remote_destination:
        remote_destination = validate_destination(remote_destination)
        if remote_retention is not None and not 1 <= remote_retention <= 10000:
            raise ValueError("Off-site retention must be between 1 and 10000")
    return {
        "task_type": task_type, "name": name[:100], "command": command,
        "frequency": frequency, "cron_expression": cron_expression,
        "schedule_timezone": schedule_timezone,
        "interval_minutes": interval, "retention_count": retention,
        "run_hour": run_hour, "run_weekday": run_weekday,
        "remote_destination": remote_destination,
        "remote_retention_count": remote_retention if remote_destination else None,
    }


@router.get("/servers/{server_id}/scheduling", response_class=HTMLResponse)
def automation_page(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not (
        has_permission(user, "automation.manage")
        or has_permission(user, "backups.view")
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    context = build_web_context(db, user, active_server=server)
    context.update({
        "server": server,
        "page_title": "Scheduling",
        "active_page": "scheduling",
        "schedule_timezone": SCHEDULE_TIMEZONE_NAME,
    })
    return render_page(request, "server_automation.html", "partials/server_automation.html", context)


@router.get("/servers/{server_id}/automation")
def old_automation_page(server_id: int):
    return RedirectResponse(f"/servers/{server_id}/scheduling", status_code=301)


@router.get("/api/web/servers/{server_id}/schedules")
def schedules(server_id: int, request: Request, runs_page: int = 1, runs_per_page: int = 10, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    tasks = db.query(ScheduledTask).filter(ScheduledTask.server_id == server.id).order_by(ScheduledTask.name).all()
    runs_page = max(1, runs_page)
    runs_per_page = min(50, max(1, runs_per_page))
    runs_query = db.query(TaskRun).filter(
        TaskRun.server_id == server.id,
        not_(and_(
            TaskRun.task_type == "command",
            TaskRun.status == "failed",
            func.lower(func.coalesce(TaskRun.detail, "")).contains("server is not running"),
        )),
    )
    runs_total = runs_query.count()
    runs_pages = max(1, (runs_total + runs_per_page - 1) // runs_per_page)
    runs_page = min(runs_page, runs_pages)
    runs = runs_query.order_by(TaskRun.id.desc()).offset((runs_page - 1) * runs_per_page).limit(runs_per_page).all()
    try:
        remotes, remote_error = configured_remotes(), None
    except OffsiteBackupError as error:
        remotes, remote_error = [], str(error)
    backup_jobs = [{
        "id": job.id, "status": job.status, "progress": job.progress,
        "message": job.message, "label": job.label,
    } for job in db.query(BackupJob).filter(
        BackupJob.server_id == server.id,
        BackupJob.status.in_(["queued", "saving", "archiving", "uploading"]),
    ).order_by(BackupJob.id.desc()).all()]
    if not backup_jobs and manual_backup_starting(server.id):
        backup_jobs.append({
            "id": None, "status": "queued", "progress": 0,
            "message": "Starting scheduled backup", "label": "Scheduled backup",
        })
    return {
        "tasks": [_task_json(task) for task in tasks],
        "runs": [{
            "id": run.id, "task_id": run.task_id, "task_type": run.task_type,
            "status": run.status, "detail": run.detail,
            "started_at": _utc_iso(run.started_at),
            "finished_at": _utc_iso(run.finished_at),
        } for run in runs],
        "runs_pagination": {"page": runs_page, "pages": runs_pages, "total": runs_total},
        "offsite_remotes": remotes,
        "offsite_error": remote_error,
        "backup_jobs": backup_jobs,
    }


@router.post("/api/web/servers/{server_id}/schedules")
async def create_schedule(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "automation.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        values = _schedule_values(await request.json())
    except (OffsiteBackupError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    task = ScheduledTask(server_id=server.id, enabled=True, next_run_at=datetime.utcnow(), **values)
    task.next_run_at = next_task_run(task, datetime.utcnow())
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_json(task)


@router.put("/api/web/servers/{server_id}/schedules/{task_id}")
async def update_schedule(server_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "automation.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    task = db.get(ScheduledTask, task_id)
    if not task or task.server_id != server.id or not task.enabled:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)
    try:
        values = _schedule_values(await request.json())
    except (OffsiteBackupError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    if values["task_type"] != task.task_type:
        return JSONResponse({"error": "Schedule type cannot be changed"}, status_code=400)
    for key, value in values.items():
        setattr(task, key, value)
    task.next_run_at = next_task_run(task, datetime.utcnow())
    db.commit()
    return _task_json(task)


@router.post("/api/web/servers/{server_id}/schedules/{task_id}/run")
def run_schedule_now(server_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "automation.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    task = db.get(ScheduledTask, task_id)
    if not task or task.server_id != server.id or not task.enabled or task.task_type != "backup":
        return JSONResponse({"error": "Backup schedule not found"}, status_code=404)
    existing = db.query(BackupJob).filter(
        BackupJob.server_id == server.id,
        BackupJob.status.in_(["queued", "saving", "archiving", "uploading"]),
    ).first()
    if existing:
        return JSONResponse({"error": "A backup is already running"}, status_code=409)
    if not start_task_now(task.id, backup_server_id=server.id):
        return JSONResponse({"error": "A backup is already starting"}, status_code=409)
    return {"success": True}


@router.delete("/api/web/servers/{server_id}/schedules/{task_id}")
def delete_schedule(server_id: int, task_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "automation.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    task = db.get(ScheduledTask, task_id)
    if not task or task.server_id != server.id:
        return JSONResponse({"error": "Schedule not found"}, status_code=404)
    # Keep the task row so its immutable execution audit remains available.
    task.enabled = False
    db.commit()
    return {"success": True}


@router.get("/api/web/servers/{server_id}/metrics")
def metrics(server_id: int, request: Request, hours: int = 24, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server:
        return JSONResponse({"error": "Access denied"}, status_code=403)
    hours = min(24 * 30, max(1, hours))
    rows = db.query(ServerMetric).filter(
        ServerMetric.server_id == server.id,
        ServerMetric.recorded_at >= datetime.utcnow() - timedelta(hours=hours),
    ).order_by(ServerMetric.recorded_at).limit(5000).all()
    return {"metrics": [{
        "recorded_at": _utc_iso(row.recorded_at), "running": row.running,
        "cpu_percent": row.cpu_percent, "memory_bytes": row.memory_bytes,
        "player_count": row.player_count, "uptime_seconds": row.uptime_seconds,
    } for row in rows]}
