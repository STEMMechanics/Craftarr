from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import backup_manager
from app.backup_jobs import fail_abandoned_backup_jobs, remote_copy_progress_message
from app.database import Base
from app.models import BackupJob, ScheduledTask, Server, TaskRun


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_restart_releases_abandoned_backup_and_task(db, tmp_path):
    server = Server(
        name="Survival", directory=str(tmp_path), service_name="survival",
        process_backend="systemd",
    )
    db.add(server)
    db.commit()
    task = ScheduledTask(
        server_id=server.id, task_type="backup", name="Daily",
        interval_minutes=1440, next_run_at=backup_manager.datetime.utcnow(),
    )
    db.add(task)
    db.commit()
    job = BackupJob(server_id=server.id, status="archiving", progress=84)
    run = TaskRun(task_id=task.id, server_id=server.id, task_type="backup", status="running")
    db.add_all([job, run])
    db.commit()

    assert fail_abandoned_backup_jobs(db) == 1
    assert job.status == "failed"
    assert job.progress == 84
    assert run.status == "failed"


def test_restart_keeps_remote_upload_retryable(db, tmp_path):
    server = Server(
        name="Survival", directory=str(tmp_path), service_name="survival",
        process_backend="systemd",
    )
    db.add(server)
    db.commit()
    job = BackupJob(
        server_id=server.id,
        filename="daily.zip",
        remote_destination="sftp:backups",
        status="uploading",
        progress=72,
    )
    db.add(job)
    db.commit()

    assert fail_abandoned_backup_jobs(db) == 1
    assert job.status == "warning"
    assert "remote copy was interrupted" in job.message


def test_remote_progress_message_uses_consistent_size_and_eta():
    message = remote_copy_progress_message(
        "Copying backup to sftp:backups",
        35,
        int(1.28 * 1024**3),
        4 * 1024**3,
        123,
    )

    assert message == "Copying backup to sftp:backups · 1.3 GB / 4.0 GB · 35% · About 2m 3s remaining"


def test_disk_full_fails_and_removes_partial_archive(tmp_path, monkeypatch):
    (tmp_path / "world.dat").write_bytes(b"world")
    server = SimpleNamespace(directory=str(tmp_path))
    monkeypatch.setattr(
        backup_manager.zipfile.ZipFile, "write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    with pytest.raises(OSError, match="No space"):
        backup_manager.create_backup(server)
    assert list((tmp_path / "backups").iterdir()) == []


def test_existing_archives_are_discovered_after_restart(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "daily.zip").write_bytes(b"archive")
    server = SimpleNamespace(directory=str(tmp_path))

    assert [item["filename"] for item in backup_manager.list_backups(server)] == ["daily.zip"]
