"""Safe rclone integration for optional off-site backup copies."""

import json
import os
import re
import shutil
import subprocess
import time
import configparser
import tempfile
import threading
from pathlib import Path
from .env import getenv


class OffsiteBackupError(RuntimeError):
    pass


_remote_cache: tuple[float, list[str]] = (0, [])
_DESTINATION = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_config_lock = threading.RLock()
_TRANSFER_PERCENT = re.compile(r",\s*(\d{1,3})%[,\s]")


def managed_config_path() -> Path:
    configured = getenv("CRAFTARR_RCLONE_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    database = Path(getenv("CRAFTARR_CONSOLE_DATABASE", "craftarr.db")).expanduser().resolve()
    return database.parent / "rclone.conf"


def _rclone_command(*args: str) -> list[str]:
    executable = shutil.which("rclone")
    if not executable:
        raise OffsiteBackupError("rclone is not installed on the console server")
    command = [executable, *args]
    command.extend(["--config", str(managed_config_path())])
    return command


def _run_rclone(*args: str, input_text=None, timeout_seconds=None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            _rclone_command(*args), capture_output=True, text=True, check=False,
            input=input_text,
            timeout=(timeout_seconds if timeout_seconds is not None else
                     max(30, int(getenv("CRAFTARR_RCLONE_TIMEOUT_SECONDS", "3600")))),
        )
    except subprocess.TimeoutExpired as error:
        raise OffsiteBackupError("Off-site transfer timed out") from error
    except OSError as error:
        raise OffsiteBackupError("rclone is installed but could not be started") from error
    if result.returncode:
        detail = (result.stderr or result.stdout or "rclone failed").strip().splitlines()[-1]
        raise OffsiteBackupError(detail[:500])
    return result


def _transfer_percent(line: str) -> int | None:
    match = _TRANSFER_PERCENT.search(line)
    return min(100, int(match.group(1))) if match else None


def _run_rclone_with_progress(*args: str, progress_callback) -> None:
    timeout = max(30, int(getenv("CRAFTARR_RCLONE_TIMEOUT_SECONDS", "3600")))
    command = _rclone_command(
        *args, "--stats", "1s", "--stats-one-line", "--stats-unit", "bytes",
        "--stats-log-level", "NOTICE",
    )
    recent_errors = []
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as error:
        raise OffsiteBackupError("rclone is installed but could not be started") from error
    deadline = time.monotonic() + timeout
    assert process.stderr is not None
    try:
        while True:
            line = process.stderr.readline()
            if line:
                recent_errors.append(line.strip())
                recent_errors = recent_errors[-20:]
                percent = _transfer_percent(line)
                if percent is not None:
                    progress_callback(percent)
            elif process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                process.kill()
                raise OffsiteBackupError("Off-site transfer timed out")
        return_code = process.wait()
    finally:
        process.stderr.close()
    if return_code:
        detail = next((line for line in reversed(recent_errors) if line), "rclone failed")
        raise OffsiteBackupError(detail[:500])


def configured_remotes(refresh=False) -> list[str]:
    global _remote_cache
    now = time.monotonic()
    if not refresh and now - _remote_cache[0] < 60:
        return list(_remote_cache[1])
    remotes = sorted(line.strip().removesuffix(":") for line in _run_rclone("listremotes").stdout.splitlines() if line.strip())
    _remote_cache = (now, remotes)
    return list(remotes)


def _read_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    path = managed_config_path()
    if path.exists():
        parser.read(path, encoding="utf-8")
    return parser


def remote_settings() -> list[dict]:
    parser = _read_config()
    settings = []
    for name in parser.sections():
        remote_type = parser.get(name, "type", fallback="")
        provider = parser.get(name, "provider", fallback="")
        backend = "storj" if remote_type == "s3" and provider.lower() == "storj" else remote_type
        item = {"name": name, "type": remote_type, "backend": backend}
        for key in ("account", "host", "user", "port", "endpoint", "access_key_id"):
            if parser.has_option(name, key):
                item[key] = parser.get(name, key)
        settings.append(item)
    return sorted(settings, key=lambda item: item["name"].lower())


def _obscure(secret: str) -> str:
    if not secret:
        return ""
    return _run_rclone("obscure", "-", input_text=f"{secret}\n").stdout.strip()


def save_remote(data: dict) -> dict:
    global _remote_cache
    name = str(data.get("name", "")).strip()
    backend = str(data.get("backend", "")).strip()
    if not _REMOTE_NAME.fullmatch(name):
        raise OffsiteBackupError("Destination names may use letters, numbers, dashes and underscores")
    if backend not in {"b2", "storj", "sftp"}:
        raise OffsiteBackupError("Choose Backblaze B2, Storj or SFTP")
    with _config_lock:
        parser = _read_config()
        existing = dict(parser[name]) if parser.has_section(name) else {}
        values = {}
        if backend == "b2":
            account = str(data.get("account", "")).strip()
            secret = str(data.get("secret", ""))
            if not account or (not secret and not existing.get("key")):
                raise OffsiteBackupError("Backblaze key ID and application key are required")
            values = {"type": "b2", "account": account, "key": _obscure(secret) if secret else existing["key"]}
        elif backend == "storj":
            access_key = str(data.get("access_key", "")).strip()
            secret = str(data.get("secret", ""))
            endpoint = str(data.get("endpoint", "https://gateway.storjshare.io")).strip()
            if not access_key or not endpoint or (not secret and not existing.get("secret_access_key")):
                raise OffsiteBackupError("Storj access key, secret key and endpoint are required")
            values = {"type": "s3", "provider": "Storj", "access_key_id": access_key,
                      "secret_access_key": _obscure(secret) if secret else existing["secret_access_key"],
                      "endpoint": endpoint, "acl": "private"}
        else:
            host = str(data.get("host", "")).strip()
            user = str(data.get("user", "")).strip()
            secret = str(data.get("secret", ""))
            try:
                port = int(data.get("port", 22))
            except (TypeError, ValueError) as error:
                raise OffsiteBackupError("SFTP port must be a number") from error
            if not host or not user or not 1 <= port <= 65535 or (not secret and not existing.get("pass")):
                raise OffsiteBackupError("SFTP host, user, valid port and password are required")
            values = {"type": "sftp", "host": host, "user": user, "port": str(port),
                      "pass": _obscure(secret) if secret else existing["pass"]}
        if parser.has_section(name):
            parser.remove_section(name)
        parser.add_section(name)
        for key, value in values.items():
            parser.set(name, key, value)
        path = managed_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".rclone-", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                parser.write(handle)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _remote_cache = (0, [])
    return next(item for item in remote_settings() if item["name"] == name)


def delete_remote(name: str) -> None:
    global _remote_cache
    if not _REMOTE_NAME.fullmatch(name):
        raise OffsiteBackupError("Invalid destination name")
    with _config_lock:
        parser = _read_config()
        if not parser.remove_section(name):
            raise OffsiteBackupError("Destination not found")
        path = managed_config_path()
        with path.open("w", encoding="utf-8") as handle:
            parser.write(handle)
        os.chmod(path, 0o600)
        _remote_cache = (0, [])


def validate_destination(destination: str, check_configured=True) -> str:
    destination = destination.strip().rstrip("/")
    match = _DESTINATION.fullmatch(destination)
    if not match or any(character in destination for character in "\r\n\0"):
        raise OffsiteBackupError("Use an rclone destination such as b2:bucket/backups")
    if check_configured and match.group(1) not in configured_remotes():
        raise OffsiteBackupError(f"rclone remote {match.group(1)!r} is not configured")
    return destination


def destination_from_parts(remote: str, path: str = "") -> str:
    """Build a destination from UI fields without treating path text as a remote."""
    remote = remote.strip()
    path = path.strip().strip("/")
    if not _REMOTE_NAME.fullmatch(remote):
        raise OffsiteBackupError("Choose a configured rclone destination")
    return validate_destination(f"{remote}:{path}")


def remote_backup_directory(destination: str, server) -> str:
    server_folder = Path(server.directory).resolve().name
    destination = validate_destination(destination)
    if destination.endswith(":"):
        destination += "."
    return f"{destination}/{server_folder}"


def upload_backup(server, filename: str, destination: str, progress_callback=None) -> str:
    from .backup_manager import safe_backup_path

    local_path = safe_backup_path(server, filename)
    remote_directory = remote_backup_directory(destination, server)
    remote_file = f"{remote_directory}/{local_path.name}"
    arguments = ("copyto", str(local_path), remote_file, "--transfers", "1", "--checkers", "1")
    if progress_callback:
        _run_rclone_with_progress(*arguments, progress_callback=progress_callback)
        progress_callback(100)
    else:
        _run_rclone(*arguments)
    return remote_file


def test_destination(destination: str) -> None:
    _run_rclone(
        "lsd", validate_destination(destination), "--max-depth", "1",
        "--contimeout", "5s", "--timeout", "10s", "--retries", "1",
        "--low-level-retries", "1", timeout_seconds=20,
    )


def enforce_remote_retention(server, destination: str, keep: int | None) -> list[str]:
    if not keep or keep < 1:
        return []
    remote_directory = remote_backup_directory(destination, server)
    result = _run_rclone("lsjson", remote_directory, "--files-only")
    try:
        files = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as error:
        raise OffsiteBackupError("rclone returned an invalid file list") from error
    backups = sorted(
        (item for item in files if str(item.get("Name", "")).lower().endswith(".zip")),
        key=lambda item: str(item.get("ModTime", "")), reverse=True,
    )
    deleted = []
    for item in backups[keep:]:
        name = Path(str(item["Name"])).name
        _run_rclone("deletefile", f"{remote_directory}/{name}")
        deleted.append(name)
    return deleted
