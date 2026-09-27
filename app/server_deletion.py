import shutil
import subprocess
import uuid
from pathlib import Path

from .models import (
    BackupJob,
    ServerUpdateCheck,
    PluginMonitoringSetting,
    UpdateNotification,
    ScheduledTask,
    Server,
    ServerMetric,
    TaskRun,
    user_server_access,
)
from . import processes


def delete_managed_server(
    db,
    server: Server,
    *,
    delete_files: bool,
    server_root: Path,
) -> dict:
    processes.register_server(server)
    if processes.server_status(server.id).get("running"):
        try:
            processes.stop_server(server.id)
        except RuntimeError as error:
            raise ValueError(f"Unable to stop the server: {error}") from error

        process = processes.processes.get(server.id)
        if process and process.poll() is None:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired as error:
                raise ValueError(
                    "The server did not stop within 30 seconds; deletion was cancelled"
                ) from error

        if processes.server_status(server.id).get("running"):
            raise ValueError("The server is still running; deletion was cancelled")

    server_path = Path(server.directory)
    staged_path = None
    if delete_files and server_path.exists():
        root = server_root.resolve()
        resolved = server_path.resolve()
        if server_path.is_symlink() or resolved == root or not resolved.is_relative_to(root):
            raise ValueError("Server files can only be deleted from the managed server directory")
        staged_path = resolved.with_name(f".craftarr-delete-{server.id}-{uuid.uuid4().hex}")
        resolved.rename(staged_path)

    try:
        db.query(PluginMonitoringSetting).filter_by(server_id=server.id).delete(synchronize_session=False)
        db.query(ServerUpdateCheck).filter_by(server_id=server.id).delete(synchronize_session=False)
        db.query(UpdateNotification).filter_by(server_id=server.id).delete(synchronize_session=False)
        db.query(TaskRun).filter(TaskRun.server_id == server.id).delete(synchronize_session=False)
        db.query(ScheduledTask).filter(ScheduledTask.server_id == server.id).delete(synchronize_session=False)
        db.query(BackupJob).filter(BackupJob.server_id == server.id).delete(synchronize_session=False)
        db.query(ServerMetric).filter(ServerMetric.server_id == server.id).delete(synchronize_session=False)
        db.execute(
            user_server_access.delete().where(user_server_access.c.server_id == server.id)
        )
        db.delete(server)
        db.commit()
    except Exception:
        db.rollback()
        if staged_path and staged_path.exists() and not server_path.exists():
            staged_path.rename(server_path)
        raise

    processes.unregister_server(server.id)
    warning = None
    if staged_path:
        try:
            shutil.rmtree(staged_path)
        except OSError:
            warning = f"Server was removed, but files remain at {staged_path}"

    return {
        "deleted": True,
        "files_deleted": bool(staged_path and not staged_path.exists()),
        "warning": warning,
    }
