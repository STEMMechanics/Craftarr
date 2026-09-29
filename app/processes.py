import subprocess
import shutil
import socket
import json
import re
import shlex
import sys
import threading
import time
import psutil
from .env import getenv

from collections import deque
from pathlib import Path
from dataclasses import dataclass


processes: dict[int, subprocess.Popen] = {}
process_lifecycle_states: dict[int, str] = {}

console_buffers: dict[int, deque] = {}

console_threads: dict[int, threading.Thread] = {}

# psutil calculates CPU usage from the difference between calls on the same
# Process object. Recreating it for every request makes every sample the first
# sample and therefore 0%.
stats_processes: dict[int, psutil.Process] = {}
stats_process_roots: dict[int, int] = {}
stats_process_started_at: dict[int, float] = {}


@dataclass
class ServerProcessConfig:
    backend: str
    service_name: str
    directory: str
    memory: str
    jar_name: str
    java_args: str
    min_memory: str = "2G"
    java_path: str = "java"
    port: int = 25565
    display_name: str = "Server"


server_configs: dict[int, ServerProcessConfig] = {}
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
SYSTEMD_UNIT_PREFIX = getenv("CRAFTARR_SYSTEMD_UNIT_PREFIX", "craftarr-server@")
SYSTEMD_SOCKET_DIR = Path(getenv("CRAFTARR_SYSTEMD_SOCKET_DIR", "/run/craftarr"))
SYSTEMD_PLAYERS_QUERY = b"__craftarr_online_players__"
SERVER_READY_PATTERN = re.compile(r"\bDone\s+\(|For help,\s*type", re.IGNORECASE)
unsupported_player_query_sockets: dict[int, tuple[int, int]] = {}
player_query_lock = threading.Lock()


def systemd_available() -> bool:
    return (
        sys.platform.startswith("linux")
        and shutil.which("systemctl") is not None
        and Path("/run/systemd/system").is_dir()
    )


def register_server(server) -> None:
    backend = getattr(server, "process_backend", "subprocess") or "subprocess"
    if backend not in {"subprocess", "systemd"}:
        raise ValueError("Unsupported process backend")
    service_name = server.service_name
    if not SERVICE_PATTERN.fullmatch(service_name):
        raise ValueError("Invalid systemd service name")
    server_configs[server.id] = ServerProcessConfig(
        backend, service_name, server.directory, server.memory,
        server.jar_name, server.java_args, server.min_memory,
        getattr(server, "java_path", "java") or "java",
        getattr(server, "port", 25565),
        getattr(server, "name", "Server"),
    )


def unregister_server(server_id: int) -> None:
    server_configs.pop(server_id, None)
    processes.pop(server_id, None)
    process_lifecycle_states.pop(server_id, None)
    console_buffers.pop(server_id, None)
    console_threads.pop(server_id, None)
    stats_processes.pop(server_id, None)
    stats_process_roots.pop(server_id, None)
    stats_process_started_at.pop(server_id, None)
    unsupported_player_query_sockets.pop(server_id, None)


def _systemd_config(server_id: int) -> ServerProcessConfig | None:
    config = server_configs.get(server_id)
    return config if config and config.backend == "systemd" else None


def _systemd_unit_name(config: ServerProcessConfig) -> str:
    return f"{SYSTEMD_UNIT_PREFIX}{config.service_name}.service"


def _systemctl(config: ServerProcessConfig, action: str, check: bool = True):
    if action not in {
        "start", "stop", "restart", "show", "enable", "disable", "is-enabled",
    }:
        raise ValueError("Invalid systemctl action")
    unit = _systemd_unit_name(config)
    command = ["systemctl", action, unit]
    if action == "show":
        command.extend(["--property=ActiveState", "--property=MainPID"])
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=check,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "systemctl command failed").strip()
        raise RuntimeError(detail) from error


def _systemd_status(config: ServerProcessConfig) -> dict:
    result = _systemctl(config, "show", check=False)
    if result.returncode != 0:
        return {
            "running": False,
            "state": "stopped",
            "console_available": False,
            "pid": None,
            "backend": "systemd",
            "active_state": "unknown",
            "service_name": config.service_name,
            "unit_name": _systemd_unit_name(config),
            "enabled_at_boot": None,
        }
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()

    active = properties.get("ActiveState", "inactive")
    try:
        pid_value = int(properties.get("MainPID", "0"))
        pid = pid_value or None
    except ValueError:
        pid = None
    active_states = {"active", "activating", "deactivating"}
    running = active in active_states
    console_available = running and _systemd_console_available(config)
    if active == "activating":
        state = "starting"
    elif active == "deactivating":
        state = "stopping"
    elif active == "active":
        lifecycle_state = _read_systemd_lifecycle_state(config, pid)
        state = lifecycle_state or ("running" if console_available else "starting")
    elif active in {"inactive", "failed"}:
        state = "stopped"
    else:
        state = "unknown"
    enabled_result = _systemctl(config, "is-enabled", check=False)
    enabled = enabled_result.returncode == 0
    return {
        "running": running,
        "state": state,
        "console_available": console_available,
        "pid": pid,
        "backend": "systemd",
        "active_state": active,
        "service_name": config.service_name,
        "unit_name": _systemd_unit_name(config),
        "enabled_at_boot": enabled,
    }


def _read_systemd_lifecycle_state(
    config: ServerProcessConfig,
    main_pid: int | None,
) -> str | None:
    status_path = SYSTEMD_SOCKET_DIR / f"{config.service_name}.status"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(status, dict):
        return None
    state = status.get("state")
    if state not in {"starting", "running", "stopping"}:
        return None
    try:
        status_pid = int(status.get("pid"))
    except (TypeError, ValueError):
        return None
    if main_pid and status_pid != main_pid:
        return None
    return state


def _systemd_console_available(config: ServerProcessConfig) -> bool:
    endpoint = SYSTEMD_SOCKET_DIR / f"{config.service_name}.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(endpoint))
        return True
    except OSError:
        return False


def set_systemd_enabled(server_id: int, enabled: bool) -> None:
    if not systemd_available():
        raise RuntimeError("Systemd services are only available on Linux hosts running systemd")
    config = _systemd_config(server_id)
    if not config:
        raise RuntimeError("Automatic startup is only available for systemd services")
    _systemctl(config, "enable" if enabled else "disable")


MAX_CONSOLE_LINES = 1000


MEMORY_PATTERN = re.compile(r"^([1-9][0-9]*)\s*(K|KB|M|MB|G|GB)$", re.IGNORECASE)


def normalize_memory(value: str, label: str = "Memory") -> str:
    match = MEMORY_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise ValueError(
            f"{label} must be a whole number followed by K, KB, M, MB, G, or GB (for example, 4GB)"
        )
    return f"{match.group(1)}{match.group(2)[0].upper()}"


def resolve_server_jar(directory: str | Path, jar_name: str) -> Path:
    jar = Path(jar_name)
    if (
        jar.name != jar_name
        or jar.suffix.lower() != ".jar"
        or jar.is_absolute()
        or "/" in jar_name
        or "\\" in jar_name
    ):
        raise ValueError("Select an existing JAR file from the server directory")

    root = Path(directory).resolve()
    candidate = root / jar_name
    if candidate.is_symlink():
        raise ValueError("Select an existing JAR file from the server directory")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError):
        raise ValueError("Select an existing JAR file from the server directory") from None
    if not resolved.is_file():
        raise ValueError("Select an existing JAR file from the server directory")
    return resolved


def _validate_java_args(arguments: list[str]) -> None:
    for argument in arguments:
        lowered = argument.lower()
        if (
            lowered.startswith(("-xms", "-xmx"))
            or lowered == "-jar"
            or lowered.startswith("@")
        ):
            raise ValueError(
                "Java startup options cannot override memory or the server JAR, or use argument files"
            )


def build_java_command(
    memory: str = "2G",
    jar_name: str = "paper.jar",
    java_args: str = "",
    min_memory: str | None = None,
    java_path: str = "java",
) -> list[str]:
    """Build a shell-free Java command from validated server settings."""
    maximum_memory = normalize_memory(memory, "Maximum RAM")
    initial_memory = normalize_memory(min_memory or memory, "Initial RAM")

    multipliers = {"K": 1, "M": 1024, "G": 1024 * 1024}
    as_kib = lambda value: int(value[:-1]) * multipliers[value[-1]]
    if as_kib(initial_memory) > as_kib(maximum_memory):
        raise ValueError("Initial memory cannot be greater than maximum memory")

    jar = Path(jar_name)
    if (
        jar.name != jar_name
        or jar.suffix.lower() != ".jar"
        or jar.is_absolute()
        or "/" in jar_name
        or "\\" in jar_name
    ):
        raise ValueError("Select a JAR filename from the server directory")

    try:
        extra_args = shlex.split(java_args)
    except ValueError as error:
        raise ValueError("Invalid Java startup options") from error
    _validate_java_args(extra_args)

    return [
        java_path, f"-Xms{initial_memory}", f"-Xmx{maximum_memory}",
        *extra_args, "-jar", jar_name, "--nogui",
    ]


def _read_console(
    server_id: int,
    process: subprocess.Popen,
):
    """
    Continuously read Minecraft stdout so the
    process cannot block and keep recent console
    lines in memory.
    """

    if server_id not in console_buffers:
        console_buffers[server_id] = deque(
            maxlen=MAX_CONSOLE_LINES
        )

    if not process.stdout:
        return

    try:
        for line in process.stdout:

            line = line.rstrip()

            if (
                process_lifecycle_states.get(server_id) == "starting"
                and SERVER_READY_PATTERN.search(line)
            ):
                process_lifecycle_states[server_id] = "running"

            console_buffers[
                server_id
            ].append(line)

    finally:
        return_code = process.poll()

        console_buffers[
            server_id
        ].append(
            f"[Panel] Server process exited "
            f"with code {return_code}"
        )


def start_server(
    server_id: int,
    directory: str,
    memory: str = "2G",
    jar_name: str = "paper.jar",
    java_args: str = "",
    min_memory: str | None = None,
    java_path: str = "java",
):
    current_config = server_configs.get(server_id)
    if current_config:
        for other_id, other_config in server_configs.items():
            if other_id == server_id or other_config.port != current_config.port:
                continue
            if server_status(other_id).get("running"):
                raise RuntimeError(
                    f"Port {current_config.port} is currently in use by {other_config.display_name}"
                )
    config = _systemd_config(server_id)
    if config:
        if _systemd_status(config)["running"]:
            raise RuntimeError("Server is already running")
        _systemctl(config, "start")
        return _systemd_status(config).get("pid")
    existing = processes.get(
        server_id
    )

    if (
        existing
        and existing.poll() is None
    ):
        raise RuntimeError(
            "Server is already running"
        )

    directory_path = Path(
        directory
    )

    command = build_java_command(memory, jar_name, java_args, min_memory, java_path)
    try:
        resolve_server_jar(directory_path, jar_name)
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    console_buffers[server_id] = deque(
        maxlen=MAX_CONSOLE_LINES
    )

    console_buffers[
        server_id
    ].append(
        "[Panel] Starting server..."
    )

    process = subprocess.Popen(
        command,
        cwd=directory_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        stats_process = psutil.Process(process.pid)
        stats_process.cpu_percent(interval=None)
        stats_processes[server_id] = stats_process
    except psutil.Error:
        pass

    processes[
        server_id
    ] = process
    process_lifecycle_states[server_id] = "starting"

    thread = threading.Thread(
        target=_read_console,
        args=(
            server_id,
            process,
        ),
        daemon=True,
    )

    thread.start()

    console_threads[
        server_id
    ] = thread

    return process.pid


def stop_server(
    server_id: int,
):
    config = _systemd_config(server_id)
    if config:
        if not _systemd_status(config)["running"]:
            raise RuntimeError("Server is not running")
        try:
            # Ask Minecraft itself to save and shut down. The supervisor stays
            # alive until Java exits, so a successful exit leaves systemd with
            # a clean result and does not trigger Restart=on-failure.
            send_command(server_id, "stop")
        except RuntimeError:
            # Older/unavailable supervisor sockets still have the unit signal
            # path as a bounded fallback.
            _systemctl(config, "stop")
            return

        deadline = time.monotonic() + 85
        while time.monotonic() < deadline:
            if not _systemd_status(config)["running"]:
                return
            time.sleep(0.25)

        # Preserve systemd's final timeout/kill handling for a hung server.
        _systemctl(config, "stop")
        return
    process = processes.get(
        server_id
    )

    if (
        not process
        or process.poll() is not None
    ):
        raise RuntimeError(
            "Server is not running"
        )

    if not process.stdin:
        raise RuntimeError(
            "Server console unavailable"
        )

    console_buffers[
        server_id
    ].append(
        "[Panel] Stopping server..."
    )

    process.stdin.write(
        "stop\n"
    )

    process.stdin.flush()
    process_lifecycle_states[server_id] = "stopping"


def run_pre_stop_commands(server_id: int, commands: str) -> None:
    lines = [line.strip() for line in (commands or "").splitlines() if line.strip()]
    if len(lines) > 20:
        raise ValueError("No more than 20 pre-stop commands are allowed")
    for command in lines:
        if len(command) > 500 or command.casefold() == "stop":
            raise ValueError("Pre-stop commands must be under 500 characters and cannot be stop")
        send_command(server_id, command)
        time.sleep(0.25)


def stop_server_and_wait(server_id: int, timeout: float = 30) -> None:
    config = _systemd_config(server_id)
    process = processes.get(server_id)
    stop_server(server_id)
    if config or not process:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def restart_server(
    server_id: int,
    directory: str,
    memory: str = "2G",
    jar_name: str = "paper.jar",
    java_args: str = "",
    min_memory: str | None = None,
    java_path: str = "java",
):
    config = _systemd_config(server_id)
    if config:
        # Use Minecraft's own stop command and wait for a clean exit before
        # asking systemd to start it again.
        stop_server(server_id)
        _systemctl(config, "start")
        return _systemd_status(config).get("pid")
    process = processes.get(
        server_id
    )

    if (
        process
        and process.poll() is None
    ):
        stop_server(
            server_id
        )

        try:
            process.wait(
                timeout=30
            )

        except subprocess.TimeoutExpired:

            process.kill()

            process.wait(
                timeout=5
            )

    return start_server(
        server_id,
        directory,
        memory,
        jar_name,
        java_args,
        min_memory,
        java_path,
    )


def kill_server(
    server_id: int,
):
    process = processes.get(
        server_id
    )

    if not process:
        return

    if process.poll() is None:
        process.kill()

    processes.pop(
        server_id,
        None,
    )
    process_lifecycle_states.pop(server_id, None)
    stats_processes.pop(server_id, None)
    stats_process_roots.pop(server_id, None)


def server_status(
    server_id: int,
):
    config = _systemd_config(server_id)
    if config:
        return _systemd_status(config)
    process = processes.get(
        server_id
    )

    if not process:

        return {
            "running": False,
            "state": "stopped",
            "console_available": False,
            "pid": None,
            "backend": "subprocess",
        }

    return_code = (
        process.poll()
    )

    if return_code is not None:

        processes.pop(
            server_id,
            None,
        )
        process_lifecycle_states.pop(server_id, None)
        stats_processes.pop(server_id, None)
        stats_process_roots.pop(server_id, None)

        return {
            "running": False,
            "state": "stopped",
            "console_available": False,
            "pid": None,
            "backend": "subprocess",
            "exit_code":
                return_code,
        }

    state = process_lifecycle_states.get(server_id, "running")
    console_available = bool(
        process.stdin and not getattr(process.stdin, "closed", False)
    )
    return {
        "running": True,
        "state": state,
        "console_available": console_available,
        "pid": process.pid,
        "backend": "subprocess",
    }


def send_command(
    server_id: int,
    command: str,
):
    command = command.strip()
    if not command or "\n" in command or "\r" in command:
        raise RuntimeError("Invalid server command")
    config = _systemd_config(server_id)
    if config:
        endpoint = SYSTEMD_SOCKET_DIR / f"{config.service_name}.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(endpoint))
                client.sendall(command.encode("utf-8"))
                if client.recv(16).strip() != b"ok":
                    raise RuntimeError("Command was not accepted")
            return
        except OSError as error:
            raise RuntimeError("Systemd server console is unavailable") from error
    process = processes.get(
        server_id
    )

    if (
        not process
        or process.poll() is not None
    ):
        raise RuntimeError(
            "Server is not running"
        )

    if not process.stdin:
        raise RuntimeError(
            "Server console unavailable"
        )

    console_buffers.setdefault(
        server_id,
        deque(maxlen=MAX_CONSOLE_LINES),
    ).append(f"[Panel command] > {command}")

    process.stdin.write(
        command + "\n"
    )

    process.stdin.flush()


def get_runtime_online_players(server_id: int) -> set[str] | None:
    """Return supervisor-owned live player state for systemd servers.

    ``None`` means this is not a systemd server or its supervisor is from an
    older release, so callers can retain their console-log fallback.
    """
    config = _systemd_config(server_id)
    if not config:
        return None
    endpoint = SYSTEMD_SOCKET_DIR / f"{config.service_name}.sock"
    with player_query_lock:
        try:
            stat = endpoint.stat()
            socket_identity = (stat.st_dev, stat.st_ino)
        except OSError:
            return None
        if unsupported_player_query_sockets.get(server_id) == socket_identity:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(endpoint))
                client.sendall(SYSTEMD_PLAYERS_QUERY)
                response = client.recv(65536)
            data = json.loads(response.decode("utf-8"))
            if not isinstance(data, list):
                unsupported_player_query_sockets[server_id] = socket_identity
                return None
            unsupported_player_query_sockets.pop(server_id, None)
            return {name for name in data if isinstance(name, str)}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unsupported_player_query_sockets[server_id] = socket_identity
            return None


def get_console(
    server_id: int,
):
    config = _systemd_config(server_id)
    if config:
        unit = _systemd_unit_name(config)
        result = subprocess.run(
            ["journalctl", "--unit", unit, "--lines", "500", "--no-pager", "--output", "cat"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.splitlines() if result.returncode == 0 else []
    buffer = console_buffers.get(
        server_id
    )

    if not buffer:
        return []

    return list(buffer)


def console_cursor(server_id: int):
    """Capture a backend-specific position before issuing a console command."""
    config = _systemd_config(server_id)
    if config:
        unit = _systemd_unit_name(config)
        result = subprocess.run(
            ["journalctl", "--unit", unit, "--lines=0", "--show-cursor", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            for line in reversed(result.stdout.splitlines()):
                if line.startswith("-- cursor: "):
                    return ("systemd", line.removeprefix("-- cursor: ").strip())
        return ("systemd", None)
    return ("subprocess", len(console_buffers.get(server_id, ())))

def wait_for_console_message(
    server_id: int,
    texts,
    timeout: float = 10.0,
    cursor=None,
) -> bool:
    """
    Wait until a new console line contains
    any of the supplied text values.
    """

    import time

    if isinstance(
        texts,
        str,
    ):
        texts = [texts]

    wanted = [
        text.lower()
        for text in texts
    ]

    cursor = cursor or console_cursor(server_id)
    backend, position = cursor

    deadline = (
        time.monotonic()
        + timeout
    )

    while (
        time.monotonic()
        < deadline
    ):

        if backend == "systemd":
            config = _systemd_config(server_id)
            if not config or not position:
                lines = []
            else:
                unit = _systemd_unit_name(config)
                result = subprocess.run(
                    ["journalctl", "--unit", unit, f"--after-cursor={position}", "--no-pager", "--output", "cat"],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                lines = result.stdout.splitlines() if result.returncode == 0 else []
        else:
            lines = list(console_buffers.get(server_id, []))[int(position):]

        for line in lines:

            lower_line = (
                line.lower()
            )

            if any(
                text in lower_line
                for text in wanted
            ):
                return True

        time.sleep(0.1)

    return False


def server_process_stats(
    server_id: int,
):
    status = server_status(server_id)
    pid = status.get("pid")

    if not status.get("running") or not pid:
        stats_processes.pop(server_id, None)
        stats_process_roots.pop(server_id, None)
        stats_process_started_at.pop(server_id, None)
        return {
            "running": False,
            "cpu_percent": 0,
            "memory_used": 0,
            "memory_percent": 0,
            "uptime_seconds": 0,
        }

    try:
        proc = stats_processes.get(server_id)
        if proc is None or stats_process_roots.get(server_id) != pid:
            root_proc = psutil.Process(pid)
            proc = root_proc
            stats_process_started_at[server_id] = root_proc.create_time()

            # A systemd service owns the lightweight Python supervisor as its
            # MainPID. Minecraft runs as a child, so measure that workload
            # instead of reporting the supervisor's near-zero CPU usage.
            if _systemd_config(server_id):
                children = root_proc.children(recursive=True)
                if children:
                    proc = children[-1]

            proc.cpu_percent(interval=None)
            stats_processes[server_id] = proc
            stats_process_roots[server_id] = pid

        memory = proc.memory_info()

        return {
            "running": True,

            "cpu_percent":
                round(
                    proc.cpu_percent(
                        interval=None
                    ),
                    1,
                ),

            "memory_used":
                memory.rss,

            "memory_percent":
                round(
                    proc.memory_percent(),
                    1,
                ),

            "uptime_seconds": max(
                0,
                int(
                    time.time()
                    - stats_process_started_at.get(
                        server_id,
                        time.time(),
                    )
                ),
            ),
        }

    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
    ):

        stats_processes.pop(server_id, None)
        stats_process_roots.pop(server_id, None)
        stats_process_started_at.pop(server_id, None)

        return {
            "running": False,
            "cpu_percent": 0,
            "memory_used": 0,
            "memory_percent": 0,
            "uptime_seconds": 0,
        }
