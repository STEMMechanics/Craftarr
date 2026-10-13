from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import web_backups
from app.database import get_db


def test_backup_download_supports_resuming_with_byte_ranges(monkeypatch, tmp_path):
    server = SimpleNamespace(directory=str(tmp_path))
    user = SimpleNamespace(
        enabled=True,
        role="admin",
        access_role=None,
        can=lambda _permission: True,
    )
    backup = tmp_path / "backups" / "daily.zip"
    backup.parent.mkdir()
    backup.write_bytes(b"0123456789")

    monkeypatch.setattr(web_backups, "get_accessible_server", lambda *_args: (user, server))
    app = FastAPI()
    app.include_router(web_backups.router)
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).get(
            "/servers/1/backups/download?filename=daily.zip",
            headers={"Range": "bytes=6-9"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 206
    assert response.content == b"6789"
    assert response.headers["content-range"] == "bytes 6-9/10"
    assert response.headers["content-length"] == "4"
    assert response.headers["accept-ranges"] == "bytes"
