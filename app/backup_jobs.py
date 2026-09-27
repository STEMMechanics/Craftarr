import threading

from datetime import datetime

from .backup_manager import (
    check_backup_space,
    create_backup,
)

from .database import (
    SessionLocal,
)

from .models import (
    BackupJob,
    Server,
    TaskRun,
)

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
