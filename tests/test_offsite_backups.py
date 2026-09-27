import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import offsite_backups
from app.offsite_backups import OffsiteBackupError


def completed(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_lists_configured_rclone_remotes(monkeypatch):
    monkeypatch.setattr(offsite_backups.shutil, "which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr(offsite_backups.subprocess, "run", lambda *args, **kwargs: completed("storj:\nb2:\n"))

    assert offsite_backups.configured_remotes(refresh=True) == ["b2", "storj"]


def test_destination_must_use_a_configured_remote(monkeypatch):
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["b2"])

    assert offsite_backups.validate_destination("b2:bucket/backups/") == "b2:bucket/backups"
    with pytest.raises(OffsiteBackupError, match="not configured"):
        offsite_backups.validate_destination("sftp:backups")


def test_destination_is_built_from_separate_remote_and_path(monkeypatch):
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["sftp"])

    assert offsite_backups.destination_from_parts("sftp", "/backups/worlds/") == "sftp:backups/worlds"
    with pytest.raises(OffsiteBackupError, match="Choose a configured"):
        offsite_backups.destination_from_parts("username:", "backups")


def test_sftp_destination_root_is_valid_when_optional_path_is_blank(monkeypatch):
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["truenas"])

    assert offsite_backups.destination_from_parts("truenas", "") == "truenas:"


def test_connection_test_has_short_timeouts(monkeypatch):
    calls = []
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["sftp"])
    monkeypatch.setattr(
        offsite_backups, "_run_rclone",
        lambda *args, **kwargs: calls.append((args, kwargs)) or completed(),
    )

    offsite_backups.test_destination("sftp:backups")

    assert calls[0][1] == {"timeout_seconds": 20}
    assert "--contimeout" in calls[0][0]
    assert "--retries" in calls[0][0]


def test_upload_uses_copyto_without_a_shell(monkeypatch, tmp_path):
    server_root = tmp_path / "creative"
    backup_dir = server_root / "backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / "save.zip").write_bytes(b"backup")
    server = SimpleNamespace(directory=str(server_root))
    commands = []
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["b2"])
    monkeypatch.setattr(offsite_backups, "_run_rclone", lambda *args: commands.append(args) or completed())

    remote = offsite_backups.upload_backup(server, "save.zip", "b2:bucket/panel")

    assert remote == "b2:bucket/panel/creative/save.zip"
    assert commands == [("copyto", str(backup_dir / "save.zip"), remote, "--transfers", "1", "--checkers", "1")]


def test_upload_uses_relative_home_path_when_destination_path_is_blank(monkeypatch, tmp_path):
    server_root = tmp_path / "creative"
    backup_dir = server_root / "backups"
    backup_dir.mkdir(parents=True)
    (backup_dir / "save.zip").write_bytes(b"backup")
    server = SimpleNamespace(directory=str(server_root))
    commands = []
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["sftp"])
    monkeypatch.setattr(offsite_backups, "_run_rclone", lambda *args: commands.append(args) or completed())

    remote = offsite_backups.upload_backup(server, "save.zip", "sftp:")

    assert remote == "sftp:./creative/save.zip"
    assert commands == [("copyto", str(backup_dir / "save.zip"), remote, "--transfers", "1", "--checkers", "1")]


def test_remote_retention_deletes_only_old_zip_files(monkeypatch, tmp_path):
    server = SimpleNamespace(directory=str(tmp_path / "survival"))
    deleted = []
    listing = json.dumps([
        {"Name": "new.zip", "ModTime": "2026-08-09T02:00:00Z"},
        {"Name": "notes.txt", "ModTime": "2026-08-09T01:30:00Z"},
        {"Name": "old.zip", "ModTime": "2026-08-09T01:00:00Z"},
    ])
    monkeypatch.setattr(offsite_backups, "configured_remotes", lambda: ["sftp"])

    def run(*args):
        if args[0] == "lsjson":
            return completed(listing)
        deleted.append(args)
        return completed()

    monkeypatch.setattr(offsite_backups, "_run_rclone", run)

    assert offsite_backups.enforce_remote_retention(server, "sftp:backups", 1) == ["old.zip"]
    assert deleted == [("deletefile", "sftp:backups/survival/old.zip")]


def test_missing_rclone_has_actionable_error(monkeypatch):
    monkeypatch.setattr(offsite_backups.shutil, "which", lambda name: None)

    with pytest.raises(OffsiteBackupError, match="not installed"):
        offsite_backups.configured_remotes(refresh=True)


def test_website_can_create_and_update_b2_remote(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAFTARR_RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setattr(
        offsite_backups, "_run_rclone",
        lambda *args, input_text=None: completed("obscured-value\n"),
    )

    saved = offsite_backups.save_remote({
        "name": "family-b2", "backend": "b2", "account": "key-id", "secret": "top-secret",
    })

    config_path = tmp_path / "rclone.conf"
    assert saved == {"name": "family-b2", "type": "b2", "backend": "b2", "account": "key-id"}
    assert "top-secret" not in config_path.read_text()
    assert config_path.stat().st_mode & 0o777 == 0o600

    offsite_backups.save_remote({
        "name": "family-b2", "backend": "b2", "account": "new-key-id", "secret": "",
    })
    text = config_path.read_text()
    assert "new-key-id" in text
    assert "obscured-value" in text


def test_website_can_remove_remote(monkeypatch, tmp_path):
    config_path = tmp_path / "rclone.conf"
    config_path.write_text("[old]\ntype = sftp\nhost = example.test\n")
    monkeypatch.setenv("CRAFTARR_RCLONE_CONFIG", str(config_path))

    offsite_backups.delete_remote("old")

    assert offsite_backups.remote_settings() == []


def test_parses_rclone_one_line_transfer_percentage():
    assert offsite_backups._transfer_percent(
        "Transferred: 5242880 / 10485760 Bytes, 50%, 1048576 Bytes/s, ETA 5s"
    ) == 50
    assert offsite_backups._transfer_percent("Checking files") is None
