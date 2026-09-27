import secrets
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .env import getenv


def _session_secret() -> str:
    configured = getenv("CRAFTARR_CONSOLE_SECRET")
    if configured:
        if len(configured) < 32:
            warnings.warn(
                "CRAFTARR_CONSOLE_SECRET should contain at least 32 characters",
                RuntimeWarning,
                stacklevel=2,
            )
        return configured

    warnings.warn(
        "CRAFTARR_CONSOLE_SECRET is not set; using a temporary secret. "
        "Sessions and API tokens will be invalidated when the app restarts.",
        RuntimeWarning,
        stacklevel=2,
    )
    return secrets.token_urlsafe(48)


SECRET_KEY = _session_secret()
COOKIE_SECURE = getenv(
    "CRAFTARR_CONSOLE_COOKIE_SECURE", "false"
).lower() in {"1", "true", "yes", "on"}
MAX_UPLOAD_BYTES = int(
    getenv("CRAFTARR_CONSOLE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))
)


def _system_timezone_name() -> str | None:
    """Best-effort IANA name discovery on Linux and macOS."""
    for path in (Path("/etc/localtime"), Path("/var/db/timezone/localtime")):
        try:
            targets = (str(path.readlink()), str(path.resolve()))
            for target in targets:
                for marker in ("zoneinfo.default/", "zoneinfo/"):
                    if marker in target:
                        return target.split(marker, 1)[1]
        except OSError:
            pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        return name or None
    except OSError:
        return None


def _schedule_timezone():
    configured = getenv("CRAFTARR_TIMEZONE", "").strip()
    system_name = _system_timezone_name()
    for name in filter(None, (configured, system_name)):
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError:
            if name == configured:
                warnings.warn(
                    f"Unknown CRAFTARR_TIMEZONE {name!r}; using the system timezone",
                    RuntimeWarning,
                    stacklevel=2,
                )
    system_zone = datetime.now().astimezone().tzinfo
    return system_zone, datetime.now().astimezone().tzname() or "System time"


SCHEDULE_TIMEZONE, SCHEDULE_TIMEZONE_NAME = _schedule_timezone()
