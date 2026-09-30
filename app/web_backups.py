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

from .backup_jobs import request_backup_cancellation, start_backup_job

from .models import BackupJob

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

    return {
        "running":
            server_status(
                server.id
            ).get(
                "running",
                False,
            ),

        "backups":
            list_backups(server),
    }


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
