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


def hub_manages_alerts(db, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if get_setting(db, "notifications_managed_by_hub").lower() != "true":
        return False
    raw_seen = get_setting(db, "notifications_hub_seen_at")
    try:
        seen_at = datetime.fromisoformat(raw_seen) if raw_seen else None
    except ValueError:
        seen_at = None
    return bool(seen_at and now - seen_at <= timedelta(minutes=3))


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
    hub_managed = hub_manages_alerts(db, now)
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
        if hub_managed:
            # The linked hub reports this alert to its own users and SMTP.
            # Keep this host's cooldown state without mailing remote accounts.
            set_setting(db, key, now.isoformat())
            db.commit()
            sent.append(resource)
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
