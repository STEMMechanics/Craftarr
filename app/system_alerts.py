import logging
import shutil
from datetime import datetime, timedelta

import psutil

from .emailer import send_email
from .models import User
from .settings_manager import get_setting, get_system_alert_settings, set_setting


logger = logging.getLogger(__name__)


def _admin_addresses(db) -> list[str]:
    return sorted({
        user.email.strip()
        for user in db.query(User).filter(User.enabled.is_(True)).all()
        if user.email and user.can("settings.manage")
    })


def check_system_alerts(db, now: datetime | None = None) -> list[str]:
    now = now or datetime.utcnow()
    settings = get_system_alert_settings(db)
    if not settings["enabled"]:
        return []
    disk = shutil.disk_usage("/")
    readings = {
        "memory": float(psutil.virtual_memory().percent),
        "storage": round(disk.used / disk.total * 100, 1),
    }
    thresholds = {
        "memory": settings["memory_percent"],
        "storage": settings["storage_percent"],
    }
    recipients = _admin_addresses(db)
    sent = []
    for resource, percent in readings.items():
        if percent < thresholds[resource] or not recipients:
            continue
        key = f"system_alert_last_sent_{resource}"
        raw_last = get_setting(db, key)
        try:
            last = datetime.fromisoformat(raw_last) if raw_last else None
        except ValueError:
            last = None
        if last and now - last < timedelta(minutes=settings["cooldown_minutes"]):
            continue
        subject = f"Craftarr alert: {resource} usage is {percent:.1f}%"
        body = (
            f"{resource.title()} usage reached {percent:.1f}%.\n"
            f"Configured threshold: {thresholds[resource]}%.\n"
            f"Cooldown: {settings['cooldown_minutes']} minutes."
        )
        try:
            for address in recipients:
                send_email(db, address, subject, body)
        except Exception:
            logger.exception("Unable to send %s system alert", resource)
            continue
        set_setting(db, key, now.isoformat())
        db.commit()
        sent.append(resource)
    return sent
