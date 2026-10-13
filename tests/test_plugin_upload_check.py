import zipfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import web_plugins
from app.database import get_db
from app.plugin_manager import install_plugin_file


def make_plugin(path, version="1.0"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.yml", f"name: Example\nversion: {version}\n")


def test_plugin_upload_check_detects_existing_plugin_before_file_upload(
    monkeypatch, tmp_path,
):
    server = SimpleNamespace(directory=str(tmp_path / "server"))
    source = tmp_path / "example.jar"
    make_plugin(source)
    install_plugin_file(server, source, "Example.jar")

    app = FastAPI()
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: None
    monkeypatch.setattr(
        web_plugins,
        "get_accessible_server",
        lambda *_args: (SimpleNamespace(), server),
    )
    monkeypatch.setattr(web_plugins, "has_permission", lambda *_args: True)

    with TestClient(app) as client:
        conflict = client.post(
            "/api/web/servers/1/plugins/upload-check",
            json={"filename": "Example.jar"},
        )
        plugin_path = Path(server.directory) / "plugins" / "Example.jar"
        plugin_path.rename(plugin_path.with_name("Example.jar.disabled"))
        disabled_conflict = client.post(
            "/api/web/servers/1/plugins/upload-check",
            json={"filename": "Example.jar"},
        )
        available = client.post(
            "/api/web/servers/1/plugins/upload-check",
            json={"filename": "NewPlugin.jar"},
        )
        invalid = client.post(
            "/api/web/servers/1/plugins/upload-check",
            json={"filename": "readme.txt"},
        )

    app.dependency_overrides.clear()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "plugin_file_exists"
    assert conflict.json()["filename"] == "Example.jar"
    assert conflict.json()["enabled"] is True
    assert disabled_conflict.status_code == 409
    assert disabled_conflict.json()["enabled"] is False
    assert available.status_code == 200
    assert available.json() == {"exists": False}
    assert invalid.status_code == 400
