import gzip

from datetime import datetime, timezone
from pathlib import Path


MAX_LOG_VIEW_BYTES = 5 * 1024 * 1024


def logs_directory(server) -> Path:
    return Path(server.directory).resolve() / "logs"


def safe_log_path(server, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise ValueError("Invalid log filename")
    if not (filename.endswith(".log") or filename.endswith(".log.gz")):
        raise ValueError("Unsupported log file")
    root = logs_directory(server)
    candidate = root / filename
    if candidate.is_symlink():
        raise ValueError("Invalid log file")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError):
        raise ValueError("Log file not found") from None
    if not resolved.is_file():
        raise ValueError("Log file not found")
    return resolved


def list_server_logs(server) -> list[dict]:
    root = logs_directory(server)
    if root.is_symlink() or not root.is_dir():
        return []
    logs = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if not (path.name.endswith(".log") or path.name.endswith(".log.gz")):
            continue
        stat = path.stat()
        logs.append({
            "name": path.name,
            "size": stat.st_size,
            "size_display": _format_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        })
    return sorted(logs, key=lambda item: item["modified"], reverse=True)


def read_server_log(server, filename: str) -> tuple[str, bool]:
    path = safe_log_path(server, filename)
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as stream:
        content = stream.read(MAX_LOG_VIEW_BYTES + 1)
    truncated = len(content) > MAX_LOG_VIEW_BYTES
    return content[:MAX_LOG_VIEW_BYTES].decode("utf-8", "replace"), truncated


def read_latest_log(server) -> tuple[str, bool]:
    path = safe_log_path(server, "latest.log")
    size = path.stat().st_size
    with open(path, "rb") as stream:
        if size > MAX_LOG_VIEW_BYTES:
            stream.seek(-MAX_LOG_VIEW_BYTES, 2)
        content = stream.read(MAX_LOG_VIEW_BYTES)
    return content.decode("utf-8", "replace"), size > MAX_LOG_VIEW_BYTES


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
