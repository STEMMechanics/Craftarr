import socket
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from .env import getenv
from .models import AppSetting


SMTP_DEFAULTS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_security": "starttls",
    "smtp_from_name": "Craftarr",
    "smtp_from_address": "",
}

SYSTEM_ALERT_DEFAULTS = {
    "system_alerts_enabled": "false",
    "system_alert_memory_percent": "95",
    "system_alert_storage_percent": "80",
    "system_alert_cooldown_minutes": "60",
    "node_offline_alert_delay_minutes": "5",
}

LOGIN_MESSAGE_KEY = "login_message"
DEFAULT_LOGIN_MESSAGE = "Sign in to manage your Minecraft servers."


def get_instance_settings(db: Session) -> dict:
    name_default = getenv("CRAFTARR_INSTANCE_NAME", "").strip() or socket.gethostname() or "Craftarr"
    url_default = getenv("CRAFTARR_PUBLIC_URL", "").strip()
    raw_url = get_setting(db, "public_url", url_default).strip()
    try:
        public_url = validate_public_url(raw_url)
    except ValueError:
        public_url = ""
    return {
        "instance_name": get_setting(db, "instance_name", name_default).strip() or name_default,
        "public_url": public_url,
    }


def validate_public_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) > 2048 or any(ord(char) <= 32 for char in value) or "\\" in value:
        raise ValueError("Enter a valid console URL")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError()
        _ = parts.port
    except ValueError:
        raise ValueError("Use an HTTP or HTTPS URL without credentials, query strings or fragments") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def save_instance_settings(db: Session, data: dict) -> dict:
    name = str(data.get("instance_name", "")).strip()
    if not name or len(name) > 100:
        raise ValueError("Enter an instance name of 1 to 100 characters")
    public_url = validate_public_url(str(data.get("public_url", "")))
    set_setting(db, "instance_name", name)
    set_setting(db, "public_url", public_url)
    db.commit()
    return get_instance_settings(db)


def get_login_message(db: Session) -> str:
    return get_setting(db, LOGIN_MESSAGE_KEY, DEFAULT_LOGIN_MESSAGE)


def save_login_message(db: Session, message: str) -> str:
    value = message.strip() or DEFAULT_LOGIN_MESSAGE
    set_setting(db, LOGIN_MESSAGE_KEY, value)
    db.commit()
    return value


def get_setting(
    db: Session,
    key: str,
    default: str = "",
) -> str:

    setting = (
        db.query(AppSetting)
        .filter(
            AppSetting.key == key
        )
        .first()
    )

    if not setting:
        return default

    return setting.value or ""


def set_setting(
    db: Session,
    key: str,
    value: str,
):

    setting = (
        db.query(AppSetting)
        .filter(
            AppSetting.key == key
        )
        .first()
    )

    if not setting:

        setting = AppSetting(
            key=key,
            value=value,
        )

        db.add(setting)

    else:

        setting.value = value


def get_smtp_settings(
    db: Session,
) -> dict:

    return {
        key:
            get_setting(
                db,
                key,
                default,
            )
        for key, default
        in SMTP_DEFAULTS.items()
    }


def save_smtp_settings(
    db: Session,
    data: dict,
):

    for key, default in SMTP_DEFAULTS.items():

        value = str(
            data.get(
                key,
                default,
            )
        )

        set_setting(
            db,
            key,
            value,
        )

    db.commit()


def get_system_alert_settings(db: Session) -> dict:
    values = {
        key: get_setting(db, key, default)
        for key, default in SYSTEM_ALERT_DEFAULTS.items()
    }
    return {
        "enabled": values["system_alerts_enabled"].lower() == "true",
        "memory_percent": int(values["system_alert_memory_percent"]),
        "storage_percent": int(values["system_alert_storage_percent"]),
        "cooldown_minutes": int(values["system_alert_cooldown_minutes"]),
        "node_offline_delay_minutes": int(values["node_offline_alert_delay_minutes"]),
    }


def save_system_alert_settings(db: Session, data: dict) -> dict:
    memory = int(data.get("memory_percent", 95))
    storage = int(data.get("storage_percent", 80))
    cooldown = int(data.get("cooldown_minutes", 60))
    node_offline_delay = int(data.get("node_offline_delay_minutes", 5))
    if not 1 <= memory <= 100 or not 1 <= storage <= 100:
        raise ValueError("Alert thresholds must be between 1 and 100 percent")
    if not 1 <= cooldown <= 10080:
        raise ValueError("Cooldown must be between 1 minute and 7 days")
    if not 1 <= node_offline_delay <= 1440:
        raise ValueError("Node offline email delay must be between 1 minute and 24 hours")
    values = {
        "system_alerts_enabled": "true" if data.get("enabled") is True else "false",
        "system_alert_memory_percent": str(memory),
        "system_alert_storage_percent": str(storage),
        "system_alert_cooldown_minutes": str(cooldown),
        "node_offline_alert_delay_minutes": str(node_offline_delay),
    }
    for key, value in values.items():
        set_setting(db, key, value)
    db.commit()
    return get_system_alert_settings(db)
