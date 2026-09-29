import pytest

from types import SimpleNamespace

from app import processes
from app.processes import (
    build_java_command,
    normalize_memory,
    register_server,
    resolve_server_jar,
)


def test_build_java_command_keeps_arguments_separate():
    assert build_java_command("4G", "server.jar", '-Dname="Test Server" --add-modules jdk.incubator.vector') == [
        "java", "-Xms4G", "-Xmx4G", "-Dname=Test Server",
        "--add-modules", "jdk.incubator.vector", "-jar", "server.jar", "--nogui",
    ]


def test_build_java_command_supports_separate_initial_and_maximum_memory():
    command = build_java_command("6G", "server.jar", "", "2G")

    assert command[:3] == ["java", "-Xms2G", "-Xmx6G"]


def test_build_java_command_rejects_initial_memory_above_maximum():
    with pytest.raises(ValueError, match="Initial memory"):
        build_java_command("2G", "server.jar", "", "4G")


@pytest.mark.parametrize("memory", ["", "0G", "2GBXX", "-1G", "$(id)"])
def test_build_java_command_rejects_invalid_memory(memory):
    with pytest.raises(ValueError, match="Maximum RAM"):
        build_java_command(memory)


@pytest.mark.parametrize("jar_name", ["../paper.jar", "..\\paper.jar", "/tmp/paper.jar", "paper.txt"])
def test_build_java_command_rejects_unsafe_jar_name(jar_name):
    with pytest.raises(ValueError, match="JAR filename"):
        build_java_command(jar_name=jar_name)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("4GB", "4G"), ("2048mb", "2048M"), ("512 KB", "512K")],
)
def test_memory_units_are_normalized(value, expected):
    assert normalize_memory(value) == expected


def test_server_jar_must_be_a_regular_file_inside_server_directory(tmp_path):
    server_directory = tmp_path / "server"
    server_directory.mkdir()
    jar = server_directory / "paper.jar"
    jar.write_bytes(b"jar")

    assert resolve_server_jar(server_directory, "paper.jar") == jar
    with pytest.raises(ValueError, match="existing JAR"):
        resolve_server_jar(server_directory, "missing.jar")
    with pytest.raises(ValueError, match="existing JAR"):
        resolve_server_jar(server_directory, "../../secret.jar")


def test_server_jar_rejects_symlink_to_external_file(tmp_path):
    server_directory = tmp_path / "server"
    server_directory.mkdir()
    external = tmp_path / "secret.jar"
    external.write_bytes(b"secret")
    (server_directory / "paper.jar").symlink_to(external)

    with pytest.raises(ValueError, match="existing JAR"):
        resolve_server_jar(server_directory, "paper.jar")


@pytest.mark.parametrize(
    "java_args",
    ["-Xmx8G", "-Xms2G", "-jar other.jar", "@/etc/java.args"],
)
def test_java_options_cannot_override_managed_values_or_use_argument_files(java_args):
    with pytest.raises(ValueError, match="cannot override memory"):
        build_java_command("4G", "paper.jar", java_args)


def test_java_options_allow_paths_and_urls_in_flag_values():
    java_args = (
        "-Xlog:gc*:logs/gc.log:time,uptime:filecount=5,filesize=10M "
        "-Dusing.aikars.flags=https://mcflags.emc.gs "
        "-Daikars.new.flags=true"
    )

    command = build_java_command("4G", "paper.jar", java_args)

    assert command[3:-3] == [
        "-Xlog:gc*:logs/gc.log:time,uptime:filecount=5,filesize=10M",
        "-Dusing.aikars.flags=https://mcflags.emc.gs",
        "-Daikars.new.flags=true",
    ]


def test_register_server_rejects_unsafe_systemd_instance():
    server = SimpleNamespace(
        id=1, process_backend="systemd", service_name="bad/name", directory="/srv/server",
        memory="2G", jar_name="paper.jar", java_args="",
    )
    with pytest.raises(ValueError, match="service name"):
        register_server(server)


def test_systemd_availability_requires_linux_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(processes.sys, "platform", "darwin")
    monkeypatch.setattr(processes.shutil, "which", lambda _command: "/usr/bin/systemctl")
    assert processes.systemd_available() is False

    monkeypatch.setattr(processes.sys, "platform", "linux")
    monkeypatch.setattr(processes.shutil, "which", lambda _command: None)
    assert processes.systemd_available() is False


def test_systemctl_uses_argument_list_without_shell(monkeypatch):
    captured = {}
    monkeypatch.setattr(processes.subprocess, "run", lambda command, **kwargs: captured.update(command=command, kwargs=kwargs))
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    processes._systemctl(config, "start")
    assert captured["command"] == [
        "systemctl", "start", "craftarr-server@survival.service",
    ]
    assert "shell" not in captured["kwargs"]


def test_start_rejects_port_used_by_another_running_server(monkeypatch):
    first = processes.ServerProcessConfig(
        "subprocess", "first", "/srv/first", "2G", "paper.jar", "",
        port=25565, display_name="Survival",
    )
    second = processes.ServerProcessConfig(
        "subprocess", "second", "/srv/second", "2G", "paper.jar", "",
        port=25565, display_name="Creative",
    )
    monkeypatch.setattr(processes, "server_configs", {1: first, 2: second})
    monkeypatch.setattr(
        processes, "server_status", lambda server_id: {"running": server_id == 1},
    )

    with pytest.raises(RuntimeError, match="Port 25565.*Survival"):
        processes.start_server(2, "/srv/second")


def test_panel_owned_command_is_echoed_to_console(monkeypatch):
    class Input:
        def write(self, _value):
            pass

        def flush(self):
            pass

    process = SimpleNamespace(stdin=Input(), poll=lambda: None)
    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: None)
    monkeypatch.setitem(processes.processes, 7, process)
    processes.console_buffers.pop(7, None)

    processes.send_command(7, "say Hello")

    assert list(processes.console_buffers[7]) == ["[Panel command] > say Hello"]


def test_systemctl_stop_does_not_disable_instance(monkeypatch):
    captured = {}
    monkeypatch.setattr(processes.subprocess, "run", lambda command, **kwargs: captured.update(command=command))
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")

    processes._systemctl(config, "stop")

    assert captured["command"] == [
        "systemctl", "stop", "craftarr-server@survival.service",
    ]


def test_systemd_stop_sends_minecraft_command_and_waits_for_clean_exit(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    actions = []
    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: config)
    statuses = iter([{"running": True}, {"running": True}, {"running": False}])
    monkeypatch.setattr(processes, "_systemd_status", lambda _config: next(statuses))
    monkeypatch.setattr(processes, "_systemctl", lambda _config, action: actions.append(action))
    monkeypatch.setattr(processes, "send_command", lambda _server_id, command: actions.append(command))
    monkeypatch.setattr(processes.time, "sleep", lambda _seconds: None)

    processes.stop_server(1)

    assert actions == ["stop"]


def test_systemd_stop_falls_back_when_console_socket_is_unavailable(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    actions = []
    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: config)
    monkeypatch.setattr(processes, "_systemd_status", lambda _config: {"running": True})
    monkeypatch.setattr(
        processes, "send_command", lambda *_args: (_ for _ in ()).throw(RuntimeError("socket unavailable")),
    )
    monkeypatch.setattr(processes, "_systemctl", lambda _config, action: actions.append(action))

    processes.stop_server(1)

    assert actions == ["stop"]


def test_stop_server_and_wait_waits_for_panel_owned_process(monkeypatch):
    waited = []
    process = SimpleNamespace(wait=lambda timeout: waited.append(timeout))
    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: None)
    monkeypatch.setitem(processes.processes, 1, process)
    monkeypatch.setattr(processes, "stop_server", lambda _server_id: None)

    processes.stop_server_and_wait(1)

    assert waited == [30]


def test_pre_stop_commands_are_sent_in_order(monkeypatch):
    sent = []
    monkeypatch.setattr(processes, "send_command", lambda server_id, command: sent.append((server_id, command)))
    monkeypatch.setattr(processes.time, "sleep", lambda _seconds: None)

    processes.run_pre_stop_commands(7, "citizens save\n\nstemcraft save\nsave-all")

    assert sent == [(7, "citizens save"), (7, "stemcraft save"), (7, "save-all")]

def test_systemd_status_parses_properties_by_name(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    results = iter([
        SimpleNamespace(
        returncode=0,
        # systemctl does not guarantee the order of selected properties.
        stdout="MainPID=4321\nActiveState=active\n",
        ),
        SimpleNamespace(returncode=0, stdout="enabled\n"),
    ])
    monkeypatch.setattr(processes, "_systemctl", lambda *args, **kwargs: next(results))

    status = processes._systemd_status(config)

    assert status == {
        "running": True, "state": "starting", "console_available": False,
        "pid": 4321, "backend": "systemd", "active_state": "active",
        "service_name": "survival",
        "unit_name": "craftarr-server@survival.service",
        "enabled_at_boot": True,
    }


def test_systemd_status_treats_zero_pid_as_missing(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    results = iter([
        SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nMainPID=0\n"),
        SimpleNamespace(returncode=1, stdout="disabled\n"),
    ])
    monkeypatch.setattr(processes, "_systemctl", lambda *args, **kwargs: next(results))

    status = processes._systemd_status(config)

    assert status == {
        "running": False, "state": "stopped", "console_available": False,
        "pid": None, "backend": "systemd", "active_state": "inactive",
        "service_name": "survival",
        "unit_name": "craftarr-server@survival.service",
        "enabled_at_boot": False,
    }


def test_set_systemd_enabled_changes_boot_policy_without_runtime_action(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    actions = []
    monkeypatch.setattr(processes, "systemd_available", lambda: True)
    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: config)
    monkeypatch.setattr(processes, "_systemctl", lambda _config, action: actions.append(action))

    processes.set_systemd_enabled(1, True)
    processes.set_systemd_enabled(1, False)

    assert actions == ["enable", "disable"]


def test_systemd_console_wait_reads_messages_after_journal_cursor(monkeypatch):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if "--show-cursor" in command:
            return SimpleNamespace(returncode=0, stdout="-- cursor: s=before-save\n")
        return SimpleNamespace(returncode=0, stdout="[Server thread/INFO]: Saved the game\n")

    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: config)
    monkeypatch.setattr(processes.subprocess, "run", run)

    cursor = processes.console_cursor(7)
    assert processes.wait_for_console_message(7, "Saved the game", timeout=0.1, cursor=cursor)
    assert calls[1] == [
        "journalctl", "--unit", "craftarr-server@survival.service",
        "--after-cursor=s=before-save", "--no-pager", "--output", "cat",
    ]


def test_unsupported_supervisor_player_query_is_not_repeated(monkeypatch, tmp_path):
    config = processes.ServerProcessConfig("systemd", "survival", "/srv/server", "2G", "paper.jar", "")
    endpoint = tmp_path / "survival.sock"
    endpoint.touch()
    calls = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            calls.append(path)

        def sendall(self, _data):
            pass

        def recv(self, _size):
            return b"ok\n"

    monkeypatch.setattr(processes, "_systemd_config", lambda _server_id: config)
    monkeypatch.setattr(processes, "SYSTEMD_SOCKET_DIR", tmp_path)
    monkeypatch.setattr(processes.socket, "socket", lambda *_args: FakeSocket())
    processes.unsupported_player_query_sockets.pop(7, None)

    assert processes.get_runtime_online_players(7) is None
    assert processes.get_runtime_online_players(7) is None
    assert calls == [str(endpoint)]


def test_process_stats_reuses_psutil_process_for_cpu_deltas(monkeypatch):
    created = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self.calls = 0
            created.append(self)

        def cpu_percent(self, interval=None):
            self.calls += 1
            return 0 if self.calls == 1 else 12.5

        def create_time(self):
            return 1000

        def memory_info(self):
            return SimpleNamespace(rss=1024)

        def memory_percent(self):
            return 1.0

    monkeypatch.setattr(processes, "server_status", lambda server_id: {"running": True, "pid": 4321})
    monkeypatch.setattr(processes.psutil, "Process", FakeProcess)
    processes.stats_processes.pop(99, None)
    processes.stats_process_roots.pop(99, None)
    processes.stats_process_started_at.pop(99, None)
    monkeypatch.setattr(processes.time, "time", lambda: 1123)

    processes.server_process_stats(99)
    result = processes.server_process_stats(99)

    assert len(created) == 1
    assert result["cpu_percent"] == 12.5
    assert result["uptime_seconds"] == 123


def test_systemd_process_stats_measure_minecraft_child(monkeypatch):
    class FakeProcess:
        def __init__(self, pid, cpu=0):
            self.pid = pid
            self.cpu = cpu
            self.calls = 0

        def children(self, recursive=False):
            return [minecraft]

        def cpu_percent(self, interval=None):
            self.calls += 1
            return 0 if self.calls == 1 else self.cpu

        def create_time(self):
            return 2000

        def memory_info(self):
            return SimpleNamespace(rss=2048)

        def memory_percent(self):
            return 2.0

    supervisor = FakeProcess(100)
    minecraft = FakeProcess(101, cpu=37.5)
    monkeypatch.setattr(processes, "server_status", lambda server_id: {"running": True, "pid": 100})
    monkeypatch.setattr(processes, "_systemd_config", lambda server_id: object())
    monkeypatch.setattr(processes.psutil, "Process", lambda pid: supervisor)
    processes.stats_processes.pop(1000, None)
    processes.stats_process_roots.pop(1000, None)
    processes.stats_process_started_at.pop(1000, None)
    monkeypatch.setattr(processes.time, "time", lambda: 2125)

    processes.server_process_stats(1000)
    result = processes.server_process_stats(1000)

    assert result["cpu_percent"] == 37.5
    assert result["memory_used"] == 2048
    assert result["uptime_seconds"] == 125
