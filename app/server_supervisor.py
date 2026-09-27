"""Systemd-owned Minecraft process with a local Unix command socket."""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from .processes import build_java_command, resolve_server_jar
from .processes import SYSTEMD_PLAYERS_QUERY
from .processes import SERVER_READY_PATTERN


JOIN_PATTERN = re.compile(r": ([A-Za-z0-9_]{1,16}) joined the game")
LOGIN_PATTERN = re.compile(r"\]:\s+([A-Za-z0-9_]{1,16})(?:\[/[^\]]+\])? logged in with entity id")
LEAVE_PATTERN = re.compile(r": ([A-Za-z0-9_]{1,16}) left the game")
DISCONNECT_PATTERN = re.compile(r"\]:\s+([A-Za-z0-9_]{1,16})(?:\s+\([^)]*\))? lost connection:")


def _echo_command(command: str) -> None:
    print(f"[Panel command] > {command}", flush=True)


def _update_online_players(line: str, online_players: set[str]) -> None:
    joined = JOIN_PATTERN.search(line) or LOGIN_PATTERN.search(line)
    if joined:
        online_players.add(joined.group(1))
        return
    left = LEAVE_PATTERN.search(line) or DISCONNECT_PATTERN.search(line)
    if left:
        online_players.discard(left.group(1))


def _write_lifecycle_status(status_path: Path, state: str) -> None:
    temporary = status_path.with_name(
        f".{status_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps({"state": state, "pid": os.getpid()}),
            encoding="utf-8",
        )
        os.replace(temporary, status_path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _forward_output(
    process,
    online_players: set[str],
    player_lock: threading.Lock,
    stopping: threading.Event,
    status_path: Path,
) -> None:
    if not process.stdout:
        return
    for raw_line in iter(process.stdout.readline, b""):
        try:
            sys.stdout.buffer.write(raw_line)
            sys.stdout.buffer.flush()
        except OSError:
            pass
        line = raw_line.decode("utf-8", "replace")
        if SERVER_READY_PATTERN.search(line) and not stopping.is_set():
            _write_lifecycle_status(status_path, "running")
        with player_lock:
            _update_online_players(line, online_players)


def _serve_commands(
    server,
    process,
    stopping: threading.Event,
    status_path: Path,
    online_players: set[str] | None = None,
    player_lock=None,
) -> None:
    online_players = online_players if online_players is not None else set()
    player_lock = player_lock or threading.Lock()
    while not stopping.is_set() and process.poll() is None:
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            if stopping.is_set() or process.poll() is not None:
                return
            raise
        try:
            with connection:
                data = connection.recv(4096).decode("utf-8", "replace").strip()
                if data.encode() == SYSTEMD_PLAYERS_QUERY:
                    with player_lock:
                        connection.sendall(json.dumps(sorted(online_players)).encode("utf-8"))
                    continue
                if data and "\n" not in data and process.stdin:
                    if data.casefold() == "stop":
                        stopping.set()
                        _write_lifecycle_status(status_path, "stopping")
                    _echo_command(data)
                    process.stdin.write((data + "\n").encode())
                    process.stdin.flush()
                    connection.sendall(b"ok\n")
        except OSError:
            if stopping.is_set() or process.poll() is not None:
                return
            raise


def supervise(directory: str, socket_path: str, memory: str, min_memory: str, jar_name: str, java_args: str, java_path: str = "java") -> int:
    root = Path(directory).resolve()
    resolve_server_jar(root, jar_name)
    endpoint = Path(socket_path)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    status_path = endpoint.with_suffix(".status")
    _write_lifecycle_status(status_path, "starting")
    endpoint.unlink(missing_ok=True)
    process = subprocess.Popen(
        build_java_command(memory, jar_name, java_args, min_memory, java_path),
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stopping = threading.Event()
    online_players: set[str] = set()
    player_lock = threading.Lock()
    output_thread = threading.Thread(
        target=_forward_output,
        args=(process, online_players, player_lock, stopping, status_path),
        name="minecraft-console-output",
        daemon=True,
    )
    output_thread.start()

    def request_stop(*_args):
        if stopping.is_set():
            return
        stopping.set()
        _write_lifecycle_status(status_path, "stopping")
        if process.poll() is None and process.stdin:
            process.stdin.write(b"stop\n")
            process.stdin.flush()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    os.chmod(endpoint, 0o660)
    server.listen(8)
    server.settimeout(1)

    thread = threading.Thread(
        target=_serve_commands,
        args=(server, process, stopping, status_path, online_players, player_lock),
        name="minecraft-command-socket",
    )
    thread.start()
    try:
        return process.wait()
    finally:
        _write_lifecycle_status(status_path, "stopped")
        # A clean Minecraft exit can race the socket thread's accept call. Stop
        # and join it before interpreter finalization so it cannot raise while
        # Python is tearing down buffered stderr (which turns a clean systemd
        # stop into SIGABRT and triggers Restart=on-failure).
        stopping.set()
        server.close()
        thread.join(timeout=2)
        output_thread.join(timeout=2)
        endpoint.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-name")
    parser.add_argument("--directory")
    parser.add_argument("--socket")
    parser.add_argument("--memory", default="2G")
    parser.add_argument("--min-memory", default="2G")
    parser.add_argument("--jar", default="paper.jar")
    parser.add_argument("--java-args", default="")
    parser.add_argument("--java-path", default="java")
    options = parser.parse_args()
    if options.service_name:
        from .database import SessionLocal
        from .models import Server
        from .processes import SERVICE_PATTERN, SYSTEMD_SOCKET_DIR
        if not SERVICE_PATTERN.fullmatch(options.service_name):
            parser.error("invalid service name")
        db = SessionLocal()
        try:
            server = db.query(Server).filter(Server.service_name == options.service_name).one_or_none()
            if not server or server.process_backend != "systemd":
                parser.error("systemd server configuration not found")
            options.directory = server.directory
            options.memory = server.memory
            options.min_memory = server.min_memory
            options.jar = server.jar_name
            options.java_args = server.java_args
            from .java_runtime import discover_java_runtimes, reconcile_java_path
            options.java_path = reconcile_java_path(
                server.java_path,
                discover_java_runtimes(),
                server.minecraft_version,
            )
            if not options.java_path:
                parser.error("no installed Java runtime found")
            if options.java_path != server.java_path:
                server.java_path = options.java_path
                db.commit()
            options.socket = str(SYSTEMD_SOCKET_DIR / f"{server.service_name}.sock")
        finally:
            db.close()
    if not options.directory or not options.socket:
        parser.error("directory and socket are required")
    raise SystemExit(supervise(options.directory, options.socket, options.memory, options.min_memory, options.jar, options.java_args, options.java_path))


if __name__ == "__main__":
    main()
