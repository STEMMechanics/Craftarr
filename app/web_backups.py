from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from sqlalchemy.orm import Session

from .backup_manager import (
    create_backup,
    delete_backup,
    list_backups,
    restore_backup,
    safe_backup_path,
)

from .database import get_db
from .web_context import (
    build_web_context,
)
from .web_render import (
    render_page,
)
from .web_servers import (
    get_accessible_server,
)
from .permissions import has_permission

from .backup_jobs import (
    request_backup_cancellation,
    start_backup_job,
    start_remote_backup_retry,
)

from .models import BackupJob, ScheduledTask

from .offsite_backups import OffsiteBackupError, list_remote_backups

from .processes import (
    send_command,
    server_status,
    wait_for_console_message,
)
router = APIRouter()


@router.post("/api/web/servers/{server_id}/backups/jobs/{job_id}/cancel")
def cancel_backup_job(server_id: int, job_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "backups.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    job = db.get(BackupJob, job_id)
    if not job or job.server_id != server.id:
        return JSONResponse({"error": "Backup job not found"}, status_code=404)
    if job.status not in {"queued", "saving", "archiving", "uploading"}:
        return JSONResponse({"error": "Backup is no longer running"}, status_code=409)
    if not request_backup_cancellation(job.id):
        return JSONResponse({"error": "Backup worker is no longer available"}, status_code=409)
    job.message = "Cancellation requested"
    db.commit()
    request.state.audit_action = "Backup cancellation requested"
    request.state.audit_details = job.label or f"Backup job {job.id}"
    return {"success": True}


@router.get(
    "/servers/{server_id}/backups",
    response_class=HTMLResponse,
)
def backups_page(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if not server or not has_permission(user, "backups.view"):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    context = build_web_context(
        db,
        user,
        active_server=server,
    )

    context.update({
        "server": server,
        "page_title": "Backups",
        "active_page": "backups",
    })

    return render_page(
        request,
        "server_backups.html",
        "partials/server_backups.html",
        context,
    )


@router.get(
    "/api/web/servers/{server_id}/backups"
)
def backups_data(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "backups.view"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    backups = list_backups(server)
    jobs = (
        db.query(BackupJob)
        .filter(
            BackupJob.server_id == server.id,
            BackupJob.filename.isnot(None),
        )
        .order_by(BackupJob.id.desc())
        .limit(100)
        .all()
    )
    latest_job_by_filename = {}
    for job in jobs:
        latest_job_by_filename.setdefault(job.filename, job)
    for backup in backups:
        job = latest_job_by_filename.get(backup.get("filename"))
        if not job:
            continue
        legacy_remote_failure = "off-site copy failed" in (job.message or "").casefold()
        if not job.remote_destination and not legacy_remote_failure:
            continue
        backup["remote_status"] = (
            "failed" if job.status == "warning" or legacy_remote_failure
            else "uploading" if job.status == "uploading"
            else "complete" if job.status == "complete"
            else None
        )
        backup["remote_destination"] = job.remote_destination
        backup["remote_job_id"] = job.id
        backup["remote_message"] = job.message

    return {
        "running":
            server_status(
                server.id
            ).get(
                "running",
                False,
            ),

        "backups": backups,
    }


@router.get("/api/web/servers/{server_id}/backups/remote")
def remote_backups_data(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "backups.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    grouped = {}
    tasks = db.query(ScheduledTask).filter(
        ScheduledTask.server_id == server.id,
        ScheduledTask.task_type == "backup",
        ScheduledTask.remote_destination.isnot(None),
    ).order_by(ScheduledTask.id.desc()).all()
    for task in tasks:
        destination = (task.remote_destination or "").strip()
        if not destination:
            continue
        item = grouped.setdefault(destination, {
            "destination": destination,
            "schedules": [],
        })
        item["schedules"].append({
            "name": task.name,
            "enabled": bool(task.enabled),
        })

    remotes = []
    for destination, item in grouped.items():
        try:
            inventory = list_remote_backups(server, destination)
        except OffsiteBackupError as error:
            inventory = {
                "destination": destination,
                "directory": None,
                "files": [],
                "summary": {"complete": 0, "partial": 0, "other": 0},
                "error": str(error),
            }
        candidate_jobs = (
            db.query(BackupJob)
            .filter(
                BackupJob.server_id == server.id,
                BackupJob.remote_destination == destination,
            )
            .order_by(BackupJob.id.desc())
            .limit(100)
            .all()
        )
        retry_jobs = []
        for job in candidate_jobs:
            message = (job.message or "").casefold()
            if job.status != "warning" and "off-site copy failed" not in message:
                continue
            retry_jobs.append({
                "id": job.id,
                "filename": job.filename,
                "label": job.label,
                "message": job.message,
            })
            if len(retry_jobs) >= 10:
                break
        inventory["schedules"] = item["schedules"]
        inventory["retry_jobs"] = retry_jobs
        remotes.append(inventory)
    return {"remotes": remotes}


@router.post("/api/web/servers/{server_id}/backups/jobs/{job_id}/retry-remote")
async def retry_remote_backup(
    server_id: int,
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "backups.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    job = db.get(BackupJob, job_id)
    if not job or job.server_id != server.id:
        return JSONResponse({"error": "Backup job not found"}, status_code=404)
    legacy_remote_failure = "off-site copy failed" in (job.message or "").casefold()
    if (job.status != "warning" and not (job.status == "complete" and legacy_remote_failure)) or not job.remote_destination:
        return JSONResponse({"error": "This backup has no remote copy to retry"}, status_code=409)
    if not job.filename or not safe_backup_path(server, job.filename).is_file():
        return JSONResponse({"error": "The local backup archive is no longer available"}, status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if isinstance(data, dict) and data.get("destination"):
        if str(data["destination"]).strip() != job.remote_destination:
            return JSONResponse({"error": "The retry destination does not match this backup"}, status_code=400)
    if legacy_remote_failure and job.status == "complete":
        job.status = "warning"
        db.commit()
    if not start_remote_backup_retry(job.id):
        return JSONResponse({"error": "A remote retry is already running"}, status_code=409)
    request.state.audit_action = "Remote backup retry queued"
    request.state.audit_details = job.filename
    return {"success": True}


@router.post(
    "/api/web/servers/{server_id}/backups/create"
)
async def create_backup_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "backups.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()

    existing = (
        db.query(BackupJob)
        .filter(
            BackupJob.server_id == server.id,
            BackupJob.status.in_(
                [
                    "queued",
                    "saving",
                    "archiving",
                    "uploading",
                ]
            ),
        )
        .first()
    )

    if existing:
        return JSONResponse(
            {
                "error":
                    "A backup is already running for this server."
            },
            status_code=409,
        )

    job = BackupJob(
        server_id=server.id,
        label=(
            data.get("label")
            or None
        ),
        status="queued",
        progress=0,
        message="Queued",
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    start_backup_job(
        job.id
    )

    request.state.audit_action = "Backup queued"
    request.state.audit_details = job.label or f"Backup job {job.id}"

    return {
        "success": True,
        "job_id": job.id,
    }

@router.post(
    "/api/web/servers/{server_id}/backups/delete"
)
async def delete_backup_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "backups.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()

    try:

        delete_backup(
            server,
            data.get(
                "filename",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_details = str(data.get("filename", ""))

    return {
        "success": True
    }


@router.post(
    "/api/web/servers/{server_id}/backups/restore"
)
async def restore_backup_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "backups.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    if server_status(
        server.id
    ).get(
        "running",
        False,
    ):

        return JSONResponse(
            {
                "error":
                    "Stop the server before restoring a backup."
            },
            status_code=400,
        )

    data = await request.json()

    try:

        restore_backup(
            server,
            data.get(
                "filename",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_details = str(data.get("filename", ""))

    return {
        "success": True
    }


@router.get(
    "/servers/{server_id}/backups/download"
)
def download_backup(
    server_id: int,
    request: Request,
    filename: str,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        raise HTTPException(
            status_code=401
        )

    if not server or not has_permission(user, "backups.view"):
        raise HTTPException(
            status_code=403
        )

    path = safe_backup_path(
        server,
        filename,
    )

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Backup not found",
        )

    return FileResponse(
        path,
        filename=path.name,
    )

@router.get(
    "/api/web/servers/{server_id}/backups/jobs"
)
def backup_jobs_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "backups.view"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    jobs = (
        db.query(BackupJob)
        .filter(
            BackupJob.server_id
            == server.id
        )
        .order_by(
            BackupJob.id.desc()
        )
        .limit(10)
        .all()
    )

    return {
        "jobs": [
            {
                "id": job.id,
                "status": job.status,
                "progress": job.progress,
                "message": job.message,
                "filename": job.filename,
                "label": job.label,
                "remote_destination": job.remote_destination,
                "created_at": (
                    job.created_at.isoformat()
                    if job.created_at
                    else None
                ),
                "started_at": (
                    job.started_at.isoformat()
                    if job.started_at
                    else None
                ),
                "finished_at": (
                    job.finished_at.isoformat()
                    if job.finished_at
                    else None
                ),
            }
            for job in jobs
        ]
    }
