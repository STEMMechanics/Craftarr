import os
import threading
import time
from pathlib import Path


RESTART_EXIT_CODE = 75


def is_containerized() -> bool:
    mode = os.environ.get("CRAFTARR_DEPLOYMENT_MODE", "").strip().lower()
    return (
        mode in {"container", "docker", "podman"}
        or Path("/.dockerenv").is_file()
        or Path("/run/.containerenv").is_file()
    )


def console_restart_unavailable_reason() -> str | None:
    if is_containerized():
        return (
            "This Craftarr runs in a Docker container. Update or restart the "
            "container image from TrueNAS or your container manager."
        )
    if not os.environ.get("INVOCATION_ID"):
        return "Console restart is only available when the panel is running as a systemd service"
    return None


def console_restart_available() -> bool:
    return console_restart_unavailable_reason() is None


def _exit_for_systemd_restart(delay: float) -> None:
    time.sleep(delay)
    os._exit(RESTART_EXIT_CODE)


def schedule_console_restart(delay: float = 2.0) -> None:
    """Restart the console through systemd after the current response is sent."""
    reason = console_restart_unavailable_reason()
    if reason:
        raise RuntimeError(reason)

    threading.Thread(
        target=_exit_for_systemd_restart,
        args=(delay,),
        daemon=True,
        name="console-systemd-restart",
    ).start()
