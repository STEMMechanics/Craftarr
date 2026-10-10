"""Durable scheduled tasks and historical server metrics."""

import threading
import logging
import json
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from .env import getenv

import psutil
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .backup_jobs import run_backup_job
from .backup_manager import list_backups, delete_backup
from .database import SessionLocal
from .models import BackupJob, PendingAutomaticUpdate, PendingIdleRestart, ScheduledTask, Server, ServerMetric, ServerUpdateCheck, TaskRun, UpdateMonitorLease
from .player_manager import get_online_players
from .processes import (
    register_server, restart_server, run_pre_stop_commands, send_command,
    start_server, stop_server_and_wait,
    server_process_stats, server_status,
)
from .audit import record_audit_event
from .config import SCHEDULE_TIMEZONE
from .offsite_backups import OffsiteBackupError, enforce_remote_retention, upload_backup
from .system_alerts import check_system_alerts


_stop = threading.Event()
_idle_restart_wake = threading.Event()
_thread: threading.Thread | None = None
_manual_task_lock = threading.Lock()
_manual_backup_servers: set[int] = set()
logger = logging.getLogger(__name__)
POLL_SECONDS = max(5, int(getenv("CRAFTARR_AUTOMATION_POLL_SECONDS", "30")))
METRIC_SECONDS = max(15, int(getenv("CRAFTARR_METRIC_INTERVAL_SECONDS", "60")))
METRIC_RETENTION_DAYS = max(1, int(getenv("CRAFTARR_METRIC_RETENTION_DAYS", "30")))
IDLE_RESTART_EMPTY_SECONDS = 30


def wake_idle_restart_monitor() -> None:
    _idle_restart_wake.set()


def next_task_run(task, now: datetime, schedule_timezone=None) -> datetime:
    """Return the next naive UTC run in the configured local timezone."""
    if schedule_timezone is None and getattr(task, "schedule_timezone", None):
        try:
            schedule_timezone = ZoneInfo(task.schedule_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            schedule_timezone = None
    if task.frequency == "hourly":
        return now.replace(second=0, microsecond=0) + timedelta(hours=1)
    if task.frequency == "custom":
        return next_cron_run(task.cron_expression, now, schedule_timezone)
    if task.frequency in {"daily", "weekly", "monthly"}:
        local_zone = schedule_timezone or SCHEDULE_TIMEZONE
        local_now = now.replace(tzinfo=timezone.utc).astimezone(local_zone)
        candidate = local_now.replace(
            hour=task.run_hour or 0, minute=0, second=0, microsecond=0,
        )
        if task.frequency == "weekly":
            candidate += timedelta(days=((task.run_weekday or 0) - candidate.weekday()) % 7)
        elif task.frequency == "monthly":
            candidate = candidate.replace(day=1)
        if candidate <= local_now:
            if task.frequency == "weekly":
                candidate += timedelta(days=7)
            elif task.frequency == "monthly":
                candidate = (candidate.replace(day=28) + timedelta(days=4)).replace(day=1)
            else:
                candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return now + timedelta(minutes=task.interval_minutes)


def _cron_values(field: str, minimum: int, maximum: int, allow_sunday_7=False) -> set[int]:
    values = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Empty schedule value")
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step < 1:
                raise ValueError("Schedule step must be positive")
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
        else:
            start = end = int(part)
        permitted_max = 7 if allow_sunday_7 else maximum
        if start < minimum or end > permitted_max or start > end:
            raise ValueError("Schedule value is out of range")
        values.update(range(start, end + 1, step))
    if allow_sunday_7 and 7 in values:
        values.remove(7)
        values.add(0)
    return values


def validate_cron_expression(expression: str) -> tuple[set[int], ...]:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("A custom schedule needs five fields")
    return (
        _cron_values(fields[0], 0, 59),
        _cron_values(fields[1], 0, 23),
        _cron_values(fields[2], 1, 31),
        _cron_values(fields[3], 1, 12),
        _cron_values(fields[4], 0, 6, allow_sunday_7=True),
    )


def next_cron_run(expression: str, now: datetime, schedule_timezone=None) -> datetime:
    minutes, hours, month_days, months, week_days = validate_cron_expression(expression or "")
    zone = schedule_timezone or SCHEDULE_TIMEZONE
    local_now = now.replace(tzinfo=timezone.utc).astimezone(zone)
    start_date = local_now.date()
    fields = expression.split()
    dom_any, dow_any = fields[2] == "*", fields[4] == "*"
    for offset in range(366 * 5):
        day = start_date + timedelta(days=offset)
        if day.month not in months or day.day > monthrange(day.year, day.month)[1]:
            continue
        cron_weekday = (day.weekday() + 1) % 7
        dom_match, dow_match = day.day in month_days, cron_weekday in week_days
        if not ((dom_match and dow_match) if dom_any or dow_any else (dom_match or dow_match)):
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                if candidate > local_now:
                    return candidate.astimezone(timezone.utc).replace(tzinfo=None)
    raise ValueError("Custom schedule has no run time in the next five years")


def enforce_backup_retention(server, keep: int | None) -> None:
    if not keep or keep < 1:
        return
    for backup in list_backups(server)[keep:]:
        delete_backup(server, backup["filename"])


def can_execute_task(task, server_id: int) -> bool:
    """Commands and restarts wait for a running server; backups may run while stopped."""
    return task.task_type not in {"command", "restart"} or bool(server_status(server_id).get("running"))


def restart_managed_server(server) -> int | None:
    run_pre_stop_commands(server.id, server.stop_commands)
    pid = restart_server(
        server.id,
        server.directory,
        server.memory,
        server.jar_name,
        server.java_args,
        server.min_memory,
        server.java_path,
    )
    server.plugins_dirty = False
    server.plugin_session_pid = pid
    return pid


def apply_scheduled_updates(db, server) -> tuple[str, list[str]]:
    """Install available monitored plugin and Paper updates, retaining old JARs disabled."""
    from .plugin_manager import install_plugin_update, list_plugins
    from .paper import download_paper, get_builds, inspect_paper_jar, match_paper_build
    from .update_monitor import acquire_lease, plugin_results

    acquired = False
    updated = []
    errors = []
    try:
        acquire_lease(db, datetime.utcnow())
        acquired = True
        plugins = [plugin for plugin in list_plugins(server) if plugin.get("enabled") and not plugin.get("previous_version")]
        results = plugin_results(db, server, plugins, fetch=True, force=True)
        by_filename = {plugin["filename"]: plugin for plugin in plugins}
        for result in results:
            if result.get("update_available") is not True:
                continue
            plugin = by_filename.get(result.get("component"))
            url = result.get("download_url")
            if not plugin or not isinstance(url, str) or not url.startswith("https://"):
                continue
            monitoring = result.get("monitoring") or {}
            try:
                install_plugin_update(
                    server, plugin["filename"], url,
                    expected_name=plugin["name"],
                    expected_version=result.get("latest_version"),
                    provider_name=result.get("provider"),
                    installed_pattern=monitoring.get("installed_pattern"),
                    installed_detection=monitoring.get("installed_detection", "auto"),
                    download_rename=monitoring.get("download_rename", ""),
                    delete_previous=False,
                )
                updated.append(f"{plugin['name']} to {result.get('latest_version') or 'latest'}")
            except Exception as error:
                errors.append(f"{plugin['name']}: {str(error)[:180]}")

        paper_path = Path(server.directory) / server.jar_name
        try:
            installed_paper = inspect_paper_jar(paper_path)
            builds = get_builds(installed_paper["version"])
            stable = [build for build in builds if str(build.get("channel", "")).upper() in {"STABLE", "RECOMMENDED"}]
            latest = stable[0] if stable else (builds[0] if builds else None)
            if not latest:
                raise ValueError("No Paper builds are available")
            installed_build = match_paper_build(installed_paper["sha256"], builds)
            latest_build = str(int(latest["id"]))
            if installed_build != latest_build:
                result = download_paper(
                    installed_paper["version"], server.directory, server.jar_name,
                    int(latest["id"]), preserve_previous=True,
                )
                server.minecraft_version = result["version"]
                server.paper_build = result["build"]
                updated.append(f"Paper {result['version']} build {result['build']}")
                from .update_monitor import paper_result
                status = paper_result(db, server, fetch=True, force=True)
                row = db.get(ServerUpdateCheck, (server.id, "@paper"))
                if row is None:
                    row = ServerUpdateCheck(server_id=server.id, component="@paper")
                    db.add(row)
                row.payload = json.dumps(status)
        except Exception as error:
            # A non-Paper/custom server JAR should not prevent plugin updates.
            errors.append(f"Paper: {str(error)[:180]}")
        db.commit()
    finally:
        if acquired:
            db.query(UpdateMonitorLease).filter_by(id=1).update({"expires_at": datetime.min})
            db.commit()

    detail = "Installed " + (", ".join(updated) if updated else "no available updates")
    if errors:
        detail += ". Issues: " + "; ".join(errors)
    return detail, errors


def scheduled_updates_available(db, server) -> tuple[bool, list[str]]:
    """Check for installable changes before stopping a running server."""
    from .plugin_manager import list_plugins
    from .paper import get_builds, inspect_paper_jar, match_paper_build
    from .update_monitor import acquire_lease, plugin_results

    acquired = False
    available = False
    errors = []
    try:
        acquire_lease(db, datetime.utcnow())
        acquired = True
        plugins = [plugin for plugin in list_plugins(server) if plugin.get("enabled") and not plugin.get("previous_version")]
        for result in plugin_results(db, server, plugins, fetch=True, force=True):
            if (result.get("update_available") is True
                    and isinstance(result.get("download_url"), str)
                    and result["download_url"].startswith("https://")):
                available = True
                break

        try:
            installed = inspect_paper_jar(Path(server.directory) / server.jar_name)
            builds = get_builds(installed["version"])
            stable = [build for build in builds if str(build.get("channel", "")).upper() in {"STABLE", "RECOMMENDED"}]
            latest = stable[0] if stable else (builds[0] if builds else None)
            if not latest:
                raise ValueError("No Paper builds are available")
            available = available or match_paper_build(installed["sha256"], builds) != str(int(latest["id"]))
        except Exception as error:
            errors.append(f"Paper: {str(error)[:180]}")
    finally:
        if acquired:
            db.query(UpdateMonitorLease).filter_by(id=1).update({"expires_at": datetime.min})
            db.commit()
    return available, errors


def execute_task(task_id: int, *, reschedule: bool = True) -> None:
    db = SessionLocal()
    run = None
    try:
        task = db.get(ScheduledTask, task_id)
        if not task or not task.enabled:
            return
        server = db.get(Server, task.server_id)
        if not server:
            return
        register_server(server)
        now = datetime.utcnow()
        task.last_run_at = now
        if reschedule:
            task.next_run_at = next_task_run(task, now)
        if not can_execute_task(task, server.id):
            # Advance the schedule quietly. A command is only meaningful while
            # the Minecraft process is running and should not create a failed run.
            db.commit()
            return
        run = TaskRun(
            task_id=task.id, server_id=server.id, task_type=task.task_type,
            status="running", started_at=now,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        if task.task_type == "command":
            if not task.command:
                raise RuntimeError("Scheduled command is empty")
            send_command(server.id, task.command)
            run.detail = f"Sent: {task.command}"
        elif task.task_type == "restart":
            restart_managed_server(server)
            run.detail = "Restarted the server"
        elif task.task_type == "update":
            state = server_status(server.id).get("state")
            if state == "stopped":
                run.detail, errors = apply_scheduled_updates(db, server)
                run.status = "warning" if errors else "complete"
                run.finished_at = datetime.utcnow()
            else:
                if db.get(PendingAutomaticUpdate, server.id):
                    raise RuntimeError("An automatic update is already waiting for this server")
                pending_restart = db.get(PendingIdleRestart, server.id)
                if pending_restart:
                    db.delete(pending_restart)
                pending = PendingAutomaticUpdate(
                    server_id=server.id,
                    task_id=task.id,
                    run_id=run.id,
                    requested_at=now,
                    empty_since=None if get_online_players(server.id) else now,
                )
                db.add(pending)
                run.status = "waiting"
                run.detail = "Waiting for players to leave; updates install after the server has been empty for 30 seconds"
                db.commit()
                return
        elif task.task_type == "backup":
            existing = db.query(BackupJob).filter(
                BackupJob.server_id == server.id,
                BackupJob.status.in_(["queued", "saving", "archiving", "uploading"]),
            ).first()
            if existing:
                raise RuntimeError("A backup is already running")
            job = BackupJob(
                server_id=server.id, label=task.name,
                status="queued", progress=0, message="Scheduled",
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            run_backup_job(job.id)
            db.refresh(job)
            if job.status != "complete":
                raise RuntimeError(job.message or "Backup failed")
            enforce_backup_retention(server, task.retention_count)
            run.detail = f"Created {job.filename}"
            if task.remote_destination:
                job.status = "uploading"
                job.progress = 0
                job.message = f"Copying backup to {task.remote_destination}"
                job.finished_at = None
                db.commit()
                try:
                    def update_upload_progress(percent):
                        job.progress = max(0, min(100, int(percent)))
                        job.message = f"Copying backup to {task.remote_destination} · {job.progress}%"
                        db.commit()

                    remote_file = upload_backup(
                        server, job.filename, task.remote_destination,
                        progress_callback=update_upload_progress,
                    )
                    enforce_remote_retention(server, task.remote_destination, task.remote_retention_count)
                    run.detail += f" · copied to {remote_file}"
                    job.status = "complete"
                    job.message = "Backup and off-site copy complete"
                except OffsiteBackupError as error:
                    run.status = "warning"
                    run.detail += f" · off-site copy failed: {error}"
                    job.status = "complete"
                    job.message = f"Backup complete; off-site copy failed: {error}"
                job.finished_at = datetime.utcnow()
        else:
            raise RuntimeError("Unsupported scheduled task type")

        if run.status == "running":
            run.status = "complete"
        run.finished_at = datetime.utcnow()
        db.commit()
    except Exception as error:
        if run:
            run.status = "failed"
            run.detail = str(error)[:1000]
            run.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _run_manual_task(task_id: int, backup_server_id: int | None) -> None:
    try:
        execute_task(task_id, reschedule=False)
    finally:
        if backup_server_id is not None:
            with _manual_task_lock:
                _manual_backup_servers.discard(backup_server_id)


def manual_backup_starting(server_id: int) -> bool:
    with _manual_task_lock:
        return server_id in _manual_backup_servers


def start_task_now(task_id: int, *, backup_server_id: int | None = None) -> bool:
    """Run a task outside the scheduler without moving its next due time."""
    if backup_server_id is not None:
        with _manual_task_lock:
            if backup_server_id in _manual_backup_servers:
                return False
            _manual_backup_servers.add(backup_server_id)
    thread = threading.Thread(
        target=_run_manual_task,
        args=(task_id,),
        kwargs={"backup_server_id": backup_server_id},
        daemon=True,
        name=f"scheduled-task-{task_id}",
    )
    try:
        thread.start()
    except Exception:
        if backup_server_id is not None:
            with _manual_task_lock:
                _manual_backup_servers.discard(backup_server_id)
        raise
    return True


def collect_metrics() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        for server in db.query(Server).filter(Server.enabled.is_(True)).all():
            register_server(server)
            status = server_status(server.id)
            stats = server_process_stats(server.id)
            uptime = None
            pid = status.get("pid")
            if status.get("running") and pid:
                try:
                    uptime = max(0, int(now.timestamp() - psutil.Process(pid).create_time()))
                except psutil.Error:
                    pass
            db.add(ServerMetric(
                server_id=server.id,
                recorded_at=now,
                running=bool(status.get("running")),
                cpu_percent=int(round(stats.get("cpu_percent", 0))),
                memory_bytes=int(stats.get("memory_used", 0)),
                player_count=len(get_online_players(server.id)),
                uptime_seconds=uptime,
            ))
        cutoff = now - timedelta(days=METRIC_RETENTION_DAYS)
        db.query(ServerMetric).filter(ServerMetric.recorded_at < cutoff).delete(synchronize_session=False)
        db.commit()
        check_system_alerts(db, now)
    finally:
        db.close()


def run_due_tasks() -> None:
    db = SessionLocal()
    try:
        due = [task.id for task in db.query(ScheduledTask).filter(
            ScheduledTask.enabled.is_(True),
            ScheduledTask.next_run_at <= datetime.utcnow(),
        ).all()]
    finally:
        db.close()
    for task_id in due:
        execute_task(task_id)


def process_pending_idle_restarts() -> None:
    """Restart servers after their online player list stays empty for 30 seconds."""
    db = SessionLocal()
    try:
        requests = db.query(PendingIdleRestart).all()
        for pending in requests:
            server_id = pending.server_id
            server = db.get(Server, server_id)
            if not server:
                db.delete(pending)
                db.commit()
                continue

            try:
                register_server(server)
                state = server_status(server.id).get("state")
                if state == "stopped":
                    db.delete(pending)
                    db.commit()
                    continue
                if state != "running":
                    pending.empty_since = None
                    db.commit()
                    continue

                if get_online_players(server.id):
                    pending.empty_since = None
                    pending.last_error = None
                    db.commit()
                    continue

                now = datetime.utcnow()
                if pending.empty_since is None:
                    pending.empty_since = now
                    pending.last_error = None
                    db.commit()
                    continue
                if now - pending.empty_since < timedelta(seconds=IDLE_RESTART_EMPTY_SECONDS):
                    continue

                current_request = db.query(PendingIdleRestart).filter_by(
                    server_id=server_id,
                ).populate_existing().with_for_update().first()
                if current_request is None:
                    continue
                pending = current_request
                requested_by_user_id = pending.requested_by_user_id
                requested_by_username = pending.requested_by_username
                restart_managed_server(server)
                db.delete(pending)
                db.commit()
                record_audit_event(
                    db,
                    server_id=server.id,
                    server_name=server.name,
                    actor_user_id=requested_by_user_id,
                    actor_username=requested_by_username,
                    action="Server restarted after players left",
                    details="The server was restarted after it had no online players for 30 seconds following a plugin update.",
                )
            except Exception:
                logger.exception("Player-free restart failed for server %s", server_id)
                db.rollback()
                retry = db.get(PendingIdleRestart, server_id)
                if retry:
                    retry.empty_since = None
                    retry.last_error = "Restart failed; the empty-player timer will start again"
                    db.commit()
    finally:
        db.close()


def process_pending_automatic_updates() -> None:
    """Apply queued updates after the server is empty, then restart it cleanly."""
    from .update_monitor import CheckInProgress

    db = SessionLocal()
    try:
        requests = db.query(PendingAutomaticUpdate).all()
        for pending in requests:
            server = db.get(Server, pending.server_id)
            run = db.get(TaskRun, pending.run_id)
            if not server or not run:
                db.delete(pending)
                db.commit()
                continue
            stopped_for_update = False
            try:
                register_server(server)
                state = server_status(server.id).get("state")
                if state not in {"running", "stopped"}:
                    pending.empty_since = None
                    db.commit()
                    continue
                if state == "running" and get_online_players(server.id):
                    pending.empty_since = None
                    pending.last_error = None
                    run.detail = "Waiting for players to leave; updates install after the server has been empty for 30 seconds"
                    db.commit()
                    continue

                now = datetime.utcnow()
                if state == "running":
                    if pending.empty_since is None:
                        pending.empty_since = now
                        pending.last_error = None
                        db.commit()
                        continue
                    if now - pending.empty_since < timedelta(seconds=IDLE_RESTART_EMPTY_SECONDS):
                        continue
                    available, check_errors = scheduled_updates_available(db, server)
                    if not available:
                        detail = "No available updates"
                        if check_errors:
                            detail += ". Issues: " + "; ".join(check_errors)
                        run.detail = detail[:1000]
                        run.status = "warning" if check_errors else "complete"
                        run.finished_at = datetime.utcnow()
                        db.delete(pending)
                        db.commit()
                        continue
                    # The update check above may take long enough for players to
                    # join. Keep the server online if that happened during the
                    # check, and begin a fresh empty-server window.
                    if get_online_players(server.id):
                        pending.empty_since = None
                        pending.last_error = None
                        run.detail = "Waiting for players to leave; updates install after the server has been empty for 30 seconds"
                        db.commit()
                        continue
                    run_pre_stop_commands(server.id, server.stop_commands)
                    # Stop commands can include a delay, so make one final check
                    # before actually stopping the server.
                    if get_online_players(server.id):
                        pending.empty_since = None
                        pending.last_error = None
                        run.detail = "Waiting for players to leave; updates install after the server has been empty for 30 seconds"
                        db.commit()
                        continue
                    stopped_for_update = True
                    stop_server_and_wait(server.id)

                detail, errors = apply_scheduled_updates(db, server)
                pid = None
                if pending.restart_after_update and stopped_for_update:
                    pid = start_server(
                        server.id, server.directory, server.memory, server.jar_name,
                        server.java_args, server.min_memory, server.java_path,
                    )
                    server.plugins_dirty = False
                    server.plugin_session_pid = pid
                    detail += "; restarted the server"
                run.detail = detail[:1000]
                run.status = "warning" if errors else "complete"
                run.finished_at = datetime.utcnow()
                db.delete(pending)
                db.commit()
                record_audit_event(
                    db,
                    server_id=server.id,
                    server_name=server.name,
                    actor_user_id=None,
                    actor_username="Craftarr automation",
                    action="Scheduled automatic updates completed",
                    details=run.detail,
                )
            except CheckInProgress:
                db.rollback()
                if stopped_for_update:
                    try:
                        register_server(server)
                        pid = start_server(
                            server.id, server.directory, server.memory, server.jar_name,
                            server.java_args, server.min_memory, server.java_path,
                        )
                        server.plugins_dirty = False
                        server.plugin_session_pid = pid
                    except Exception:
                        logger.exception("Server restart while delaying an automatic update failed for %s", server.id)
                retry = db.get(PendingAutomaticUpdate, server.id)
                waiting_run = db.get(TaskRun, pending.run_id)
                if retry:
                    retry.empty_since = None
                    retry.last_error = "Another update check is running; the automatic update will retry shortly"
                if waiting_run:
                    waiting_run.status = "waiting"
                    waiting_run.detail = "Another update check is running; waiting before retrying automatic updates"
                db.commit()
            except Exception as error:
                logger.exception("Scheduled automatic update failed for server %s", server.id)
                db.rollback()
                if stopped_for_update:
                    try:
                        register_server(server)
                        pid = start_server(
                            server.id, server.directory, server.memory, server.jar_name,
                            server.java_args, server.min_memory, server.java_path,
                        )
                        server.plugins_dirty = False
                        server.plugin_session_pid = pid
                    except Exception:
                        logger.exception("Server restart after automatic update failure failed for %s", server.id)
                retry = db.get(PendingAutomaticUpdate, server.id)
                failed_run = db.get(TaskRun, pending.run_id)
                if retry:
                    db.delete(retry)
                if failed_run:
                    failed_run.status = "failed"
                    failed_run.detail = str(error)[:1000]
                    failed_run.finished_at = datetime.utcnow()
                db.commit()
    finally:
        db.close()


def _automation_loop() -> None:
    last_metrics = 0.0
    while not _stop.is_set():
        db = SessionLocal()
        try:
            has_pending_idle_restart = db.query(PendingIdleRestart.server_id).first() is not None
            has_pending_automatic_update = db.query(PendingAutomaticUpdate.server_id).first() is not None
        except Exception:
            logger.exception("Unable to check pending player-free restarts")
            has_pending_idle_restart = False
            has_pending_automatic_update = False
        finally:
            db.close()
        wait_seconds = min(POLL_SECONDS, 5) if has_pending_idle_restart or has_pending_automatic_update else POLL_SECONDS
        _idle_restart_wake.wait(wait_seconds)
        _idle_restart_wake.clear()
        if _stop.is_set():
            break
        try:
            run_due_tasks()
        except Exception:
            logger.exception("Scheduled task polling failed")
        try:
            process_pending_idle_restarts()
        except Exception:
            logger.exception("Player-free restart polling failed")
        try:
            process_pending_automatic_updates()
        except Exception:
            logger.exception("Scheduled automatic update polling failed")
        try:
            from .update_monitor import run_scheduled_check
            run_scheduled_check()
        except Exception:
            logger.warning("Scheduled update monitoring failed")
        try:
            from .remote_nodes import run_linked_plugin_settings_sync
            run_linked_plugin_settings_sync()
        except Exception:
            logger.exception("Linked plugin settings synchronization failed")
        now = datetime.now().timestamp()
        if now - last_metrics >= METRIC_SECONDS:
            try:
                collect_metrics()
            except Exception:
                logger.exception("Historical metric collection failed")
            last_metrics = now


def start_automation() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _idle_restart_wake.clear()
    try:
        collect_metrics()
    except Exception:
        # Metrics are auxiliary. A stale or temporarily unavailable Minecraft
        # process must not prevent the management panel from starting.
        logger.exception("Initial historical metric collection failed")
    _thread = threading.Thread(target=_automation_loop, name="automation", daemon=True)
    _thread.start()


def stop_automation() -> None:
    _stop.set()
    _idle_restart_wake.set()
    if _thread:
        _thread.join(timeout=5)
