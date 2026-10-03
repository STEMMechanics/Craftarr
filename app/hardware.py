"""Hardware and Minecraft runtime summary shared by the web and node APIs."""

import shutil

import psutil

from .java_runtime import discover_java_runtimes, resolve_java_path
from .player_manager import get_online_players
from .processes import register_server, server_status


def collect_hardware_stats(servers) -> dict:
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    runtimes = discover_java_runtimes()
    java_by_path = {runtime["path"]: runtime for runtime in runtimes}
    instances = []
    players_online = 0
    running_count = 0

    for server in servers:
        try:
            register_server(server)
            status = server_status(server.id)
            running = bool(status.get("running"))
            state = status.get("state", "running" if running else "stopped")
            console_available = bool(status.get("console_available"))
            players = len(get_online_players(server.id)) if state == "running" else 0
        except Exception:
            running = False
            state = "unknown"
            console_available = False
            players = 0

        running_count += int(running)
        players_online += players
        try:
            configured_java = resolve_java_path(server.java_path)
        except ValueError:
            configured_java = server.java_path
        instances.append({
            "id": server.id,
            "name": server.name,
            "version": server.minecraft_version,
            "running": running,
            "state": state,
            "console_available": console_available,
            "players": players,
            "java": java_by_path.get(configured_java, {}).get("major"),
        })

    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "cores": psutil.cpu_count(),
        },
        "memory": {
            "used": round(memory.used / 1024 / 1024 / 1024, 1),
            "total": round(memory.total / 1024 / 1024 / 1024, 1),
            "percent": memory.percent,
        },
        "storage": {
            "used": round(disk.used / 1024 / 1024 / 1024, 1),
            "total": round(disk.total / 1024 / 1024 / 1024, 1),
            "percent": round(disk.used / disk.total * 100, 1),
        },
        "minecraft": {
            "installed": len(instances),
            "running": running_count,
            "players_online": players_online,
            "instances": instances,
        },
        "java_runtimes": runtimes,
    }
