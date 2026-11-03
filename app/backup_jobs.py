import logging
import threading

from datetime import datetime

from .backup_manager import (
    check_backup_space,
    create_backup,
    safe_backup_path,
)

from .database import (
    SessionLocal,
)

from .models import (
    BackupJob,
    Server,
    ScheduledTask,
    TaskRun,
)

from .offsite_backups import (
    OffsiteBackupError,
    enforce_remote_retention,
    upload_backup,
)

from .system_alerts import send_backup_failure_alert

from .processes import (
    console_cursor,
    send_command,
    server_status,
    wait_for_console_message,
)


class BackupCancelled(Exception):
    pass


_cancel_events: dict[int, threading.Event] = {}
_cancel_lock = threading.Lock()
_remote_retry_jobs: set[int] = set()
_remote_retry_lock = threading.Lock()
logger = logging.getLogger(__name__)


def request_backup_cancellation(job_id: int) -> bool:
    with _cancel_lock:
        event = _cancel_events.get(job_id)
        if not event:
            return False
        event.set()
        return True


def fail_abandoned_backup_jobs(db) -> int:
    now = datetime.utcnow()
    message = "Backup interrupted because the console service restarted"
    jobs = db.query(BackupJob).filter(BackupJob.status.in_([
        "queued", "saving", "archiving", "uploading",
    ])).all()
    for job in jobs:
        if job.status == "uploading" and job.remote_destination and job.filename:
            job.status = "warning"
            job.message = "Local backup may be complete; remote copy was interrupted by a console restart"
            server = db.get(Server, job.server_id)
            if server:
                email_sent = send_backup_failure_alert(
                    db, server, job, job.remote_destination,
                    "Console service restarted during the remote copy",
                )
                logger.warning(
                    "Remote backup copy was interrupted for server %s (job %s, destination %s); email_sent=%s",
                    server.name, job.id, job.remote_destination, email_sent,
                )
        else:
            job.status = "failed"
            job.message = message
        job.finished_at = now
    for run in db.query(TaskRun).filter(
        TaskRun.task_type == "backup", TaskRun.status == "running",
    ).all():
        run.status = "failed"
        run.detail = message
        run.finished_at = now
    db.commit()
    return len(jobs)


def start_backup_job(
    job_id: int,
):
    thread = threading.Thread(
        target=run_backup_job,
        args=(job_id,),
        daemon=True,
        name=f"backup-job-{job_id}",
    )

    thread.start()


def _remote_failure_message(destination: str, error) -> str:
    return f"Local backup complete; remote copy to {destination} failed: {error}"[:255]


def start_remote_backup_retry(job_id: int) -> bool:
    """Retry only the remote copy for an existing local backup archive."""
    with _remote_retry_lock:
        if job_id in _remote_retry_jobs:
            return False
        _remote_retry_jobs.add(job_id)
    thread = threading.Thread(
        target=run_remote_backup_retry,
        args=(job_id,),
        daemon=True,
        name=f"remote-backup-retry-{job_id}",
    )
    try:
        thread.start()
    except Exception:
        with _remote_retry_lock:
            _remote_retry_jobs.discard(job_id)
        raise
    return True


def run_remote_backup_retry(job_id: int) -> None:
    db = SessionLocal()
    job = None
    try:
        job = db.get(BackupJob, job_id)
        if not job or job.status != "warning" or not job.remote_destination:
            return
        server = db.get(Server, job.server_id)
        if not server or not job.filename:
            if job:
                job.status = "failed"
                job.message = "Remote retry could not find the local backup archive"
                job.finished_at = datetime.utcnow()
                db.commit()
            return
        local_path = safe_backup_path(server, job.filename)
        if not local_path.is_file():
            job.status = "failed"
            job.message = "Remote retry could not find the local backup archive"
            job.finished_at = datetime.utcnow()
            db.commit()
            return

        destination = job.remote_destination
        scheduled_task = db.query(ScheduledTask).filter(
            ScheduledTask.server_id == server.id,
            ScheduledTask.task_type == "backup",
            ScheduledTask.remote_destination == destination,
        ).order_by(ScheduledTask.enabled.desc(), ScheduledTask.id.desc()).first()
        remote_retention = scheduled_task.remote_retention_count if scheduled_task else None

        job.status = "uploading"
        job.progress = 0
        job.message = f"Retrying remote copy to {destination}"[:255]
        job.finished_at = None
        db.commit()

        def update_upload_progress(percent):
            current = db.get(BackupJob, job_id)
            if not current:
                return
            current.progress = max(0, min(100, int(percent)))
            current.message = f"Retrying remote copy to {destination} · {current.progress}%"[:255]
            db.commit()

        try:
            remote_file = upload_backup(
                server, job.filename, destination,
                progress_callback=update_upload_progress,
            )
            enforce_remote_retention(server, destination, remote_retention)
            job = db.get(BackupJob, job_id)
            if not job:
                return
            job.status = "complete"
            job.progress = 100
            job.message = "Remote copy retry complete"
            job.finished_at = datetime.utcnow()
            db.commit()
            logger.info(
                "Remote backup retry succeeded for server %s (job %s): %s",
                server.name, job_id, remote_file,
            )
        except Exception as error:
            if not isinstance(error, OffsiteBackupError):
                logger.exception("Remote backup retry failed unexpectedly for job %s", job_id)
            job = db.get(BackupJob, job_id)
            if not job:
                return
            job.status = "warning"
            job.message = _remote_failure_message(destination, error)
            job.finished_at = datetime.utcnow()
            db.commit()
            email_sent = send_backup_failure_alert(db, server, job, destination, error)
            logger.warning(
                "Remote backup retry failed for server %s (job %s, destination %s): %s; email_sent=%s",
                server.name, job_id, destination, error, email_sent,
            )
    finally:
        with _remote_retry_lock:
            _remote_retry_jobs.discard(job_id)
        db.close()


def run_backup_job(
    job_id: int,
):
    db = SessionLocal()

    job = None

    running = False
    saves_disabled = False
    cancel_event = threading.Event()
    with _cancel_lock:
        _cancel_events[job_id] = cancel_event

    try:

        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:
            return


        server = db.get(
            Server,
            job.server_id,
        )

        if not server:

            job.status = "failed"
            job.message = (
                "Server not found"
            )
            job.finished_at = (
                datetime.utcnow()
            )

            db.commit()

            return


        job.status = "saving"
        job.progress = 0
        job.message = (
            "Checking available disk space"
        )
        job.started_at = (
            datetime.utcnow()
        )

        db.commit()

        # Fail before issuing any save commands if the server volume cannot
        # hold a conservative estimate of the resulting archive.
        check_backup_space(server)

        job.message = (
            "Preparing world save"
        )

        db.commit()


        running = server_status(
            server.id
        ).get(
            "running",
            False,
        )


        if running:

            save_cursor = console_cursor(server.id)

            send_command(
                server.id,
                "save-all flush",
            )


            saved = (
                wait_for_console_message(
                    server.id,
                    [
                        "Saved the game",
                        "Saved the world",
                        "Saving complete",
                    ],
                    timeout=15,
                    cursor=save_cursor,
                )
            )

            if not saved:

                raise RuntimeError(
                    "Timed out waiting "
                    "for Minecraft to "
                    "finish saving."
                )


            send_command(
                server.id,
                "save-off",
            )

            saves_disabled = True


        job.status = "archiving"
        job.progress = 0
        job.message = (
            "Creating backup archive"
        )

        db.commit()


        def update_progress(
            value: int,
        ):

            if cancel_event.is_set():
                raise BackupCancelled("Backup cancelled by an administrator")

            # Refresh in case another
            # session modified the row.
            current_job = db.get(
                BackupJob,
                job_id,
            )

            if not current_job:
                return

            current_job.progress = (
                max(
                    0,
                    min(
                        100,
                        int(value),
                    ),
                )
            )

            current_job.message = (
                "Creating backup archive"
            )

            db.commit()


        result = create_backup(
            server,
            job.label,
            progress_callback=
                update_progress,
        )


        job = db.get(
            BackupJob,
            job_id,
        )

        if not job:
            return


        job.filename = result[
            "filename"
        ]

        job.progress = 100
        job.status = "complete"

        job.message = (
            "Backup complete"
        )

        job.finished_at = (
            datetime.utcnow()
        )

        db.commit()


    except Exception as error:

        if job is None:

            job = db.get(
                BackupJob,
                job_id,
            )


        if job:

            job.status = "cancelled" if isinstance(error, BackupCancelled) else "failed"

            job.message = (
                str(error)
            )

            job.finished_at = (
                datetime.utcnow()
            )

            db.commit()


    finally:

        with _cancel_lock:
            _cancel_events.pop(job_id, None)

        if (
            running
            and saves_disabled
        ):

            try:

                server = db.get(
                    Server,
                    job.server_id
                    if job
                    else None,
                )

                if server:

                    send_command(
                        server.id,
                        "save-on",
                    )

            except Exception:
                pass


        db.close()
