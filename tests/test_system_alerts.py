from datetime import datetime
from types import SimpleNamespace

from app import system_alerts


class Query:
    def filter(self, *_args):
        return self

    def all(self):
        return [SimpleNamespace(
            email="admin@example.com", enabled=True,
            can=lambda permission: permission == "settings.manage",
        )]


class DB:
    def query(self, *_args):
        return Query()

    def commit(self):
        pass


def test_threshold_alert_has_persisted_cooldown(monkeypatch):
    values = {}
    sent = []
    monkeypatch.setattr(system_alerts, "get_system_alert_settings", lambda _db: {
        "enabled": True, "memory_percent": 95, "storage_percent": 80,
        "cooldown_minutes": 60,
    })
    monkeypatch.setattr(system_alerts.psutil, "virtual_memory", lambda: SimpleNamespace(percent=96))
    monkeypatch.setattr(system_alerts.shutil, "disk_usage", lambda _path: SimpleNamespace(used=81, total=100))
    monkeypatch.setattr(system_alerts, "get_setting", lambda _db, key: values.get(key, ""))
    monkeypatch.setattr(system_alerts, "set_setting", lambda _db, key, value: values.__setitem__(key, value))
    monkeypatch.setattr(system_alerts, "send_email", lambda _db, address, subject, body: sent.append((address, subject, body)))
    now = datetime(2026, 8, 18, 12, 0)

    assert system_alerts.check_system_alerts(DB(), now) == ["memory", "storage"]
    assert system_alerts.check_system_alerts(DB(), now) == []
    assert len(sent) == 2


def test_remote_backup_failure_alert_emails_administrators(monkeypatch):
    sent = []
    monkeypatch.setattr(system_alerts, "hub_manages_alerts", lambda _db: False)
    monkeypatch.setattr(system_alerts, "get_smtp_settings", lambda _db: {"smtp_host": "smtp.example.test"})
    monkeypatch.setattr(system_alerts, "send_email", lambda *args: sent.append(args))

    result = system_alerts.send_backup_failure_alert(
        DB(),
        SimpleNamespace(name="Survival"),
        SimpleNamespace(filename="daily.zip"),
        "sftp:backups",
        "Off-site transfer timed out",
    )

    assert result is True
    assert sent[0][1] == "admin@example.com"
    assert "Remote backup failed for Survival" in sent[0][2]
    assert "Off-site transfer timed out" in sent[0][3]
