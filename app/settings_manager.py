from sqlalchemy.orm import Session

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
}

LOGIN_MESSAGE_KEY = "login_message"
DEFAULT_LOGIN_MESSAGE = "Sign in to manage your Minecraft servers."


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
    }


def save_system_alert_settings(db: Session, data: dict) -> dict:
    memory = int(data.get("memory_percent", 95))
    storage = int(data.get("storage_percent", 80))
    cooldown = int(data.get("cooldown_minutes", 60))
    if not 1 <= memory <= 100 or not 1 <= storage <= 100:
        raise ValueError("Alert thresholds must be between 1 and 100 percent")
    if not 1 <= cooldown <= 10080:
        raise ValueError("Cooldown must be between 1 minute and 7 days")
    values = {
        "system_alerts_enabled": "true" if data.get("enabled") is True else "false",
        "system_alert_memory_percent": str(memory),
        "system_alert_storage_percent": str(storage),
        "system_alert_cooldown_minutes": str(cooldown),
    }
    for key, value in values.items():
        set_setting(db, key, value)
    db.commit()
    return get_system_alert_settings(db)
