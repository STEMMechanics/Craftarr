import io
import zipfile
from types import SimpleNamespace

import pytest

from app.plugin_manager import (
    _validate_public_https_url, duplicate_plugin_groups, geyser_status,
    enable_plugin, install_plugin_file, install_plugin_update, list_plugins,
    PluginFileExistsError,
)
from app.web_plugins import plugin_action_requires_restart


def make_plugin(path, version="1.0"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.yml", f"name: Example\nversion: {version}\n")


def stub_plugin_update_download(monkeypatch, jar_bytes):
    class Response:
        headers = {"Content-Length": str(len(jar_bytes))}

        def __init__(self):
            self.stream = io.BytesIO(jar_bytes)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://example.com/plugin.jar"

        def read(self, size):
            return self.stream.read(size)

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 120
            return Response()

    monkeypatch.setattr("app.plugin_manager._validate_public_https_url", lambda _url: None)
    monkeypatch.setattr("app.plugin_manager.urllib.request.build_opener", lambda *_args: Opener())


def plugin_bytes(version):
    jar = io.BytesIO()
    make_plugin(jar, version=version)
    return jar.getvalue()


@pytest.mark.parametrize(("action", "filenames", "running", "expected"), [
    ("remove", ["disabled.jar.disabled"], True, False),
    ("remove", ["active.jar"], True, True),
    ("remove", ["active.jar", "disabled.jar.disabled"], True, True),
    ("disable", ["active.jar"], True, True),
    ("enable", ["disabled.jar.disabled"], True, True),
    ("remove", ["active.jar"], False, False),
    ("disable", ["active.jar"], False, False),
])
def test_restart_requirement_matches_loaded_plugin_changes(
    action, filenames, running, expected,
):
    plugins = [
        {"filename": "active.jar", "enabled": True},
        {"filename": "disabled.jar.disabled", "enabled": False},
    ]

    assert plugin_action_requires_restart(
        action, filenames, plugins, running,
    ) is expected


def test_install_plugin_file_validates_and_installs_atomically(tmp_path):
    server_root = tmp_path / "server"
    source = tmp_path / "example.jar"
    make_plugin(source)

    result = install_plugin_file(SimpleNamespace(directory=str(server_root)), source, "example.jar")

    assert result["name"] == "Example"
    assert (server_root / "plugins" / "example.jar").is_file()


def test_install_plugin_file_requires_confirmation_before_replacing(tmp_path):
    server_root = tmp_path / "server"
    first = tmp_path / "first.jar"
    second = tmp_path / "second.jar"
    make_plugin(first)
    make_plugin(second, version="2.0")
    server = SimpleNamespace(directory=str(server_root))

    install_plugin_file(server, first, "example.jar")
    with pytest.raises(PluginFileExistsError) as conflict:
        install_plugin_file(server, second, "example.jar")

    assert conflict.value.filename == "example.jar"
    assert conflict.value.enabled is True

    result = install_plugin_file(server, second, "example.jar", replace=True)

    assert result["filename"] == "example.jar"
    assert result["version"] == "2.0"
    assert [plugin["filename"] for plugin in list_plugins(server)] == ["example.jar"]


def test_replacing_disabled_plugin_installs_new_build_as_enabled(tmp_path):
    server_root = tmp_path / "server"
    first = tmp_path / "first.jar"
    second = tmp_path / "second.jar"
    make_plugin(first)
    make_plugin(second, version="2.0")
    server = SimpleNamespace(directory=str(server_root))
    install_plugin_file(server, first, "example.jar")
    (server_root / "plugins" / "example.jar").rename(
        server_root / "plugins" / "example.jar.disabled"
    )

    with pytest.raises(PluginFileExistsError) as conflict:
        install_plugin_file(server, second, "example.jar")

    assert conflict.value.enabled is False

    result = install_plugin_file(server, second, "example.jar", replace=True)

    assert result["enabled"] is True
    assert result["version"] == "2.0"
    assert not (server_root / "plugins" / "example.jar.disabled").exists()


def test_install_plugin_file_rejects_non_plugin_jar(tmp_path):
    source = tmp_path / "not-plugin.jar"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("example.txt", "no metadata")
    with pytest.raises(ValueError, match="plugin.yml"):
        install_plugin_file(SimpleNamespace(directory=str(tmp_path / "server")), source, "bad.jar")


def test_plugin_update_keeps_previous_jar_disabled_by_default(tmp_path, monkeypatch):
    server = SimpleNamespace(directory=str(tmp_path / "server"))
    plugin_dir = tmp_path / "server" / "plugins"
    plugin_dir.mkdir(parents=True)
    current_path = plugin_dir / "example.jar"
    make_plugin(current_path, version="1.0")
    original_bytes = current_path.read_bytes()
    stub_plugin_update_download(monkeypatch, plugin_bytes("2.0"))

    result = install_plugin_update(
        server,
        "example.jar",
        "https://example.com/plugin.jar",
        expected_name="Example",
        expected_version="2.0",
    )

    previous_path = plugin_dir / result["previous_filename"]
    assert result["deleted_previous"] is False
    assert previous_path.name.endswith(".jar.disabled")
    assert previous_path.read_bytes() == original_bytes
    assert result["plugin"]["version"] == "2.0"
    assert result["plugin"]["enabled"] is True
    plugins = {plugin["filename"]: plugin for plugin in list_plugins(server)}
    assert plugins[previous_path.name]["enabled"] is False


def test_plugin_update_deletes_previous_jar_only_after_validation(tmp_path, monkeypatch):
    server = SimpleNamespace(directory=str(tmp_path / "server"))
    plugin_dir = tmp_path / "server" / "plugins"
    plugin_dir.mkdir(parents=True)
    current_path = plugin_dir / "example.jar"
    make_plugin(current_path, version="1.0")
    original_bytes = current_path.read_bytes()
    stub_plugin_update_download(monkeypatch, plugin_bytes("2.0"))

    result = install_plugin_update(
        server,
        "example.jar",
        "https://example.com/plugin.jar",
        delete_previous=True,
        expected_name="Example",
        expected_version="2.0",
    )

    assert result["deleted_previous"] is True
    assert result["previous_filename"] is None
    assert result["plugin"]["version"] == "2.0"
    assert current_path.read_bytes() != original_bytes
    assert not list(plugin_dir.glob("*.previous-*.jar.disabled"))


def test_plugin_update_preserves_current_jar_when_download_is_invalid(tmp_path, monkeypatch):
    server = SimpleNamespace(directory=str(tmp_path / "server"))
    plugin_dir = tmp_path / "server" / "plugins"
    plugin_dir.mkdir(parents=True)
    current_path = plugin_dir / "example.jar"
    make_plugin(current_path, version="1.0")
    original_bytes = current_path.read_bytes()
    stub_plugin_update_download(monkeypatch, b"not a valid JAR")

    with pytest.raises(ValueError, match="valid JAR archive"):
        install_plugin_update(
            server,
            "example.jar",
            "https://example.com/plugin.jar",
            delete_previous=True,
            expected_name="Example",
            expected_version="2.0",
        )

    assert current_path.read_bytes() == original_bytes
    assert sorted(path.name for path in plugin_dir.iterdir()) == ["example.jar"]


def test_plugin_url_rejects_non_https_before_dns():
    with pytest.raises(ValueError, match="public HTTPS"):
        _validate_public_https_url("http://example.com/plugin.jar")


def test_plugin_url_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr("app.plugin_manager.socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private network"):
        _validate_public_https_url("https://example.com/plugin.jar")


def test_plugin_info_discovers_yaml_configs_and_geyser_port(tmp_path):
    server_root = tmp_path / "server"
    plugin_dir = server_root / "plugins"
    plugin_dir.mkdir(parents=True)
    source = plugin_dir / "Geyser-Spigot.jar"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("plugin.yml", "name: Geyser-Spigot\nversion: 2.0\n")
    config_dir = plugin_dir / "Geyser-Spigot"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("bedrock:\n  port: 19132\n")
    (config_dir / "messages.yaml").write_text("hello: world\n")
    (config_dir / "advanced.yml").write_text("feature: true\n")
    (config_dir / "worlds").mkdir()
    (config_dir / "worlds/config.yml").write_text("world: true\n")
    server = SimpleNamespace(directory=str(server_root))

    plugins = list_plugins(server)

    assert plugins[0]["config_files"] == [
        "plugins/Geyser-Spigot/config.yml",
        "plugins/Geyser-Spigot/worlds/config.yml",
        "plugins/Geyser-Spigot/advanced.yml",
        "plugins/Geyser-Spigot/messages.yaml",
    ]
    assert geyser_status(server, plugins) == {
        "installed": True,
        "enabled": True,
        "port": 19132,
    }


def test_duplicate_plugins_only_include_multiple_enabled_versions():
    plugins = [
        {"name": "WorldEdit", "filename": "worldedit-1.jar", "version": "1", "enabled": True},
        {"name": "worldedit", "filename": "worldedit-2.jar", "version": "2", "enabled": True},
        {"name": "WorldEdit", "filename": "worldedit-old.jar.disabled", "version": "0", "enabled": False},
        {"name": "LuckPerms", "filename": "luckperms.jar", "version": "5", "enabled": True},
    ]

    assert duplicate_plugin_groups(plugins) == [{
        "name": "WorldEdit",
        "plugins": [
            {"filename": "worldedit-1.jar", "version": "1"},
            {"filename": "worldedit-2.jar", "version": "2"},
        ],
    }]


def test_duplicate_plugin_groups_include_file_identity_when_available():
    plugins = [
        {
            "name": "Example",
            "filename": "example-1.jar",
            "version": "1",
            "enabled": True,
            "size": 123,
            "modified_ns": "1000",
        },
        {
            "name": "Example",
            "filename": "example-2.jar",
            "version": "2",
            "enabled": True,
            "size": 456,
            "modified_ns": "2000",
        },
    ]

    group = duplicate_plugin_groups(plugins)[0]

    assert group["plugins"][0] == {
        "filename": "example-1.jar",
        "version": "1",
        "size": 123,
        "modified_ns": "1000",
    }


def test_plugin_order_does_not_change_when_enabled(tmp_path):
    server_root = tmp_path / "server"
    plugin_dir = server_root / "plugins"
    plugin_dir.mkdir(parents=True)
    make_plugin(plugin_dir / "zeta.jar")
    make_plugin(plugin_dir / "alpha.jar.disabled")
    server = SimpleNamespace(directory=str(server_root))

    assert [plugin["filename"] for plugin in list_plugins(server)] == [
        "alpha.jar.disabled",
        "zeta.jar",
    ]

    enable_plugin(server, "alpha.jar.disabled")

    assert [plugin["filename"] for plugin in list_plugins(server)] == [
        "alpha.jar",
        "zeta.jar",
    ]
