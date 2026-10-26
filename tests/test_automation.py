from app import automation
from app.automation import can_execute_task, enforce_backup_retention, format_restart_announcement
from app.automation import next_cron_run, next_task_run, validate_cron_expression
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.web_automation import _utc_iso


def test_web_timestamps_are_explicitly_utc():
    assert _utc_iso(datetime(2026, 8, 10, 6, 15)) == "2026-08-10T06:15:00Z"


def test_backup_retention_deletes_only_backups_beyond_limit(monkeypatch):
    backups = [{"filename": name} for name in ("new.zip", "middle.zip", "old.zip")]
    deleted = []
    monkeypatch.setattr("app.automation.list_backups", lambda server: backups)
    monkeypatch.setattr("app.automation.delete_backup", lambda server, name: deleted.append(name))

    enforce_backup_retention(object(), 2)

    assert deleted == ["old.zip"]


def test_backup_retention_can_be_unlimited(monkeypatch):
    monkeypatch.setattr("app.automation.list_backups", lambda server: (_ for _ in ()).throw(AssertionError()))
    enforce_backup_retention(object(), None)


def test_command_waits_while_server_is_stopped(monkeypatch):
    monkeypatch.setattr(automation, "server_status", lambda server_id: {"running": False})

    assert can_execute_task(SimpleNamespace(task_type="command"), 12) is False
    assert can_execute_task(SimpleNamespace(task_type="backup"), 12) is True


def test_restart_announcement_identifies_reason_and_requester():
    assert format_restart_announcement("to apply updates: STEMCraft 1.2.3", "james") == (
        "Server restarting to apply updates: STEMCraft 1.2.3 (requested by james)."
    )
    assert format_restart_announcement("a scheduled restart") == "Server restarting: a scheduled restart."


def test_initial_metric_failure_does_not_prevent_automation_start(monkeypatch, caplog):
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def is_alive(self):
            return False

        def start(self):
            started.append(True)

    monkeypatch.setattr(automation, "collect_metrics", lambda: (_ for _ in ()).throw(RuntimeError("metrics unavailable")))
    monkeypatch.setattr(automation.threading, "Thread", FakeThread)
    monkeypatch.setattr(automation, "_thread", None)

    automation.start_automation()

    assert started == [True]
    assert "Initial historical metric collection failed" in caplog.text


def test_run_now_does_not_reschedule_task(monkeypatch):
    captured = {}

    class FakeThread:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(automation.threading, "Thread", FakeThread)

    automation.start_task_now(42)

    assert captured["target"] is automation._run_manual_task
    assert captured["args"] == (42,)
    assert captured["kwargs"] == {"backup_server_id": None}
    assert captured["daemon"] is True
    assert captured["started"] is True


def test_manual_backup_start_is_reserved_until_worker_finishes(monkeypatch):
    worker = {}

    class FakeThread:
        def __init__(self, **kwargs):
            worker.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(automation.threading, "Thread", FakeThread)
    automation._manual_backup_servers.discard(9)

    assert automation.start_task_now(42, backup_server_id=9) is True
    assert automation.start_task_now(42, backup_server_id=9) is False
    assert automation.manual_backup_starting(9) is True

    monkeypatch.setattr(automation, "execute_task", lambda *_args, **_kwargs: None)
    worker["target"](*worker["args"], **worker["kwargs"])
    assert automation.manual_backup_starting(9) is False


def test_daily_schedule_uses_selected_hour():
    task = SimpleNamespace(frequency="daily", run_hour=7, run_weekday=None)
    now = datetime(2026, 8, 9, 8, 30)

    assert next_task_run(task, now, ZoneInfo("UTC")) == datetime(2026, 8, 10, 7)


def test_weekly_schedule_uses_selected_day_and_hour():
    task = SimpleNamespace(frequency="weekly", run_hour=16, run_weekday=4)
    now = datetime(2026, 8, 9, 8, 30)  # Sunday

    assert next_task_run(task, now, ZoneInfo("UTC")) == datetime(2026, 8, 14, 16)


def test_daily_schedule_converts_local_hour_to_utc():
    task = SimpleNamespace(frequency="daily", run_hour=7, run_weekday=None)
    now = datetime(2026, 8, 9, 8, 30)

    assert next_task_run(task, now, ZoneInfo("Australia/Brisbane")) == datetime(2026, 8, 9, 21)


def test_task_uses_its_saved_timezone_by_default():
    task = SimpleNamespace(
        frequency="daily", run_hour=7, run_weekday=None,
        schedule_timezone="Australia/Brisbane",
    )
    now = datetime(2026, 8, 9, 8, 30)

    assert next_task_run(task, now) == datetime(2026, 8, 9, 21)


def test_monthly_schedule_runs_on_first_day():
    task = SimpleNamespace(frequency="monthly", run_hour=0, run_weekday=None)

    assert next_task_run(task, datetime(2026, 8, 9), ZoneInfo("UTC")) == datetime(2026, 9, 1)


def test_custom_schedule_finds_next_matching_local_time():
    now = datetime(2026, 8, 9, 8, 30)  # Sunday 18:30 in Brisbane

    assert next_cron_run("0 4 * * 1", now, ZoneInfo("Australia/Brisbane")) == datetime(2026, 8, 9, 18)


def test_custom_schedule_rejects_out_of_range_values():
    try:
        validate_cron_expression("70 4 * * *")
    except ValueError as error:
        assert "out of range" in str(error)
    else:
        raise AssertionError("Invalid schedule was accepted")
