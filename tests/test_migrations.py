import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app import migrations


def test_upgrade_database_targets_head(monkeypatch):
    called = {}

    def fake_upgrade(config, revision):
        called["script_location"] = config.get_main_option("script_location")
        called["revision"] = revision

    monkeypatch.setattr(migrations.command, "upgrade", fake_upgrade)

    migrations.upgrade_database()

    assert called["revision"] == "head"
    assert called["script_location"].endswith("/migrations")


def test_migration_scripts_are_packaged():
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    scripts = list(versions.glob("*.py"))

    assert scripts, "Release is missing migrations/versions/*.py"


def test_clean_database_migrates_to_application_schema(monkeypatch, tmp_path):
    database = tmp_path / "console.db"
    monkeypatch.setenv("CRAFTARR_CONSOLE_DATABASE", str(database))

    migrations.upgrade_database()

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        server_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(servers)")
        }

    assert {"users", "servers", "access_roles", "permissions", "role_permissions", "alembic_version"} <= tables
    assert "min_memory" in server_columns


def test_pre_020_users_are_migrated_to_single_built_in_roles(monkeypatch, tmp_path):
    database = tmp_path / "legacy.db"
    monkeypatch.setenv("CRAFTARR_CONSOLE_DATABASE", str(database))
    config = Config(str(migrations.PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(migrations.PROJECT_ROOT / "migrations"))
    command.upgrade(config, "c41f1e9a7320")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (username, password_hash, role, enabled, email, totp_enabled, must_change_password) VALUES (?, ?, ?, 1, NULL, 0, 0)",
            ("legacy-admin", "hash", "admin"),
        )
        connection.execute(
            "INSERT INTO users (username, password_hash, role, enabled, email, totp_enabled, must_change_password) VALUES (?, ?, ?, 1, NULL, 0, 0)",
            ("legacy-user", "hash", "user"),
        )

    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        migrated = dict(connection.execute(
            "SELECT users.username, access_roles.name FROM users JOIN access_roles ON access_roles.id = users.role_id"
        ))
        user_permissions = {
            row[0]
            for row in connection.execute(
                "SELECT permissions.key FROM permissions "
                "JOIN role_permissions ON role_permissions.permission_id = permissions.id "
                "JOIN access_roles ON access_roles.id = role_permissions.role_id "
                "WHERE access_roles.name = 'User'"
            )
        }

    assert migrated == {"legacy-admin": "Administrator", "legacy-user": "User"}
    assert {"servers.view", "console.command", "players.manage", "files.manage"} <= user_permissions
    assert "users.manage" not in user_permissions
