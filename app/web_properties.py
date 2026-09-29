from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)

from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from sqlalchemy.orm import Session
from pathlib import Path
import re

from .database import get_db
from .models import Server

from .properties_manager import (
    get_properties_view,
    save_properties,
)
from .processes import (
    build_java_command,
    normalize_memory,
    register_server,
    resolve_server_jar,
    server_status,
    set_systemd_enabled,
    start_server,
    stop_server_and_wait,
    systemd_available,
)

from .web_context import (
    build_web_context,
)

from .web_render import (
    render_page,
)

from .web_servers import (
    get_accessible_server,
    port_assignment_conflict,
)
from .permissions import has_permission
from .java_runtime import (
    discover_java_runtimes, java_runtime_choices, resolve_java_path,
    select_java_major,
)
from .advanced_properties import discover_advanced_properties, save_advanced_property
from .file_manager import yaml_sanity_warning
from .server_archives import (
    SETTINGS_EXPORT_FILENAME,
    MAX_SETTINGS_EXPORT_BYTES,
    parse_settings_document,
    server_settings_document,
    validate_portable_settings,
)


router = APIRouter()


def _record_plugin_session_start(db, server, pid) -> None:
    server.plugins_dirty = False
    server.plugin_session_pid = pid
    db.commit()


def server_name_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.casefold().replace(" ", "-"))
    slug = re.sub(r"-+", "-", slug).strip("-")[:100]
    if not slug:
        raise ValueError("Server name must contain at least one letter or number")
    return slug


@router.get("/api/web/servers/{server_id}/advanced-properties")
def advanced_properties_data(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    return {"groups": discover_advanced_properties(server)}


@router.post("/api/web/servers/{server_id}/advanced-properties")
async def save_advanced_properties_api(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    data = await request.json()
    content = str(data.get("content", ""))
    if data.get("validate_only") is True:
        return {"success": True, "warning": yaml_sanity_warning(content)}
    try:
        warning = save_advanced_property(
            server,
            str(data.get("path", "")),
            content,
        )
    except (OSError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    running = bool(server_status(server.id).get("running"))
    return {
        "success": True,
        "running": running,
        "restart_required": running,
        "warning": warning,
    }


@router.get(
    "/servers/{server_id}/properties",
    response_class=HTMLResponse,
)
def properties_page(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):

    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if not server or not has_permission(user, "servers.properties"):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    context = build_web_context(
        db,
        user,
        active_server=server,
    )

    context.update({
        "server": server,
        "page_title": "Properties",
        "active_page": "properties",
    })

    return render_page(
        request,
        "server_properties.html",
        "partials/server_properties.html",
        context,
    )


@router.get(
    "/servers/{server_id}/advanced-properties",
    response_class=HTMLResponse,
)
def advanced_properties_page(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "servers.properties"):
        raise HTTPException(status_code=403, detail="Access denied")
    context = build_web_context(db, user, active_server=server)
    context.update({
        "server": server,
        "page_title": "Advanced Properties",
        "active_page": "properties",
    })
    return render_page(
        request,
        "server_advanced_properties.html",
        "partials/server_advanced_properties.html",
        context,
    )


@router.get(
    "/api/web/servers/{server_id}/properties"
)
def properties_data(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):

    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    jar_files = []
    for path in Path(server.directory).glob("*.jar"):
        try:
            resolve_server_jar(server.directory, path.name)
        except ValueError:
            continue
        jar_files.append(path.name)

    try:
        selected_java_path = resolve_java_path(server.java_path)
    except ValueError:
        selected_java_path = server.java_path

    runtimes = discover_java_runtimes()
    selected_runtime = next(
        (runtime for runtime in runtimes if runtime["path"] == selected_java_path),
        None,
    )
    command = build_java_command(
        server.memory, server.jar_name, server.java_args, server.min_memory,
        selected_java_path,
    )
    command[0] = "java"

    status = server_status(server.id)

    return {
        "properties":
            get_properties_view(server),
        "startup": {
            "min_memory": server.min_memory,
            "max_memory": server.memory,
            "jar_name": server.jar_name,
            "java_args": server.java_args,
            "stop_commands": server.stop_commands,
            "java_major": selected_runtime["major"] if selected_runtime else None,
            "java_runtimes": java_runtime_choices(runtimes),
            "jar_files": sorted(jar_files),
            "command": command,
        },
        "management": {
            "name": server.name,
            "directory": server.directory,
            "process_backend": server.process_backend,
            "service_name": server.service_name,
            "unit_name": status.get("unit_name"),
            "running": bool(status.get("running")),
            "enabled_at_boot": status.get("enabled_at_boot"),
            "systemd_available": systemd_available(),
        },
    }


@router.get("/servers/{server_id:int}/settings-export")
def export_server_settings(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "servers.properties"):
        raise HTTPException(status_code=403, detail="Access denied")

    filename = SETTINGS_EXPORT_FILENAME
    return JSONResponse(
        server_settings_document(server),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/web/servers/{server_id}/settings/import")
async def import_server_settings(
    server_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    try:
        content = await file.read(MAX_SETTINGS_EXPORT_BYTES + 1)
        settings = parse_settings_document(content)
        defaults = {
            key: getattr(server, key)
            for key in (
                "minecraft_version", "paper_build", "memory", "min_memory",
                "jar_name", "java_args", "stop_commands",
            )
        }
        values = validate_portable_settings(
            settings,
            server.directory,
            defaults=defaults,
        )
    except (OSError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    finally:
        await file.close()

    running = bool(server_status(server.id).get("running"))
    old_values = {
        key: getattr(server, key)
        for key in values
    }
    try:
        for key, value in values.items():
            setattr(server, key, value)
        register_server(server)
        db.commit()
    except Exception as error:
        db.rollback()
        for key, value in old_values.items():
            setattr(server, key, value)
        register_server(server)
        return JSONResponse(
            {"error": f"Unable to import settings: {error}"},
            status_code=400,
        )

    return {
        "success": True,
        "running": running,
        "restart_required": running,
        "message": (
            "Settings imported. Restart the server to apply them."
            if running
            else "Settings imported. They will apply the next time the server starts."
        ),
    }


@router.post("/api/web/servers/{server_id}/systemd-enabled")
async def set_systemd_enabled_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    data = await request.json()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse({"error": "Enabled must be true or false"}, status_code=400)
    try:
        set_systemd_enabled(server.id, enabled)
    except RuntimeError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {
        "success": True,
        "message": "Automatic startup enabled." if enabled else "Automatic startup disabled.",
        "enabled_at_boot": enabled,
    }


@router.post("/api/web/servers/{server_id}/name")
async def rename_server_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name:
        return JSONResponse(
            {"error": "Server name is required", "field": "server_name"},
            status_code=400,
        )
    if len(name) > 100:
        return JSONResponse(
            {"error": "Server name cannot exceed 100 characters", "field": "server_name"},
            status_code=400,
        )
    conflict = db.query(Server).filter(Server.name == name, Server.id != server.id).first()
    if conflict:
        return JSONResponse(
            {"error": "Server name already exists", "field": "server_name"},
            status_code=409,
        )

    try:
        slug = server_name_slug(name)
    except ValueError as error:
        return JSONResponse(
            {"error": str(error), "field": "server_name"},
            status_code=400,
        )

    old_directory = Path(server.directory)
    new_directory = old_directory.with_name(slug)
    new_service_name = slug
    directory_conflict = (
        db.query(Server)
        .filter(Server.directory == str(new_directory), Server.id != server.id)
        .first()
    )
    service_conflict = (
        db.query(Server)
        .filter(Server.service_name == new_service_name, Server.id != server.id)
        .first()
    )
    if directory_conflict or (new_directory != old_directory and new_directory.exists()):
        return JSONResponse(
            {"error": f"Server directory already exists: {new_directory}", "field": "server_name"},
            status_code=409,
        )
    if service_conflict:
        return JSONResponse(
            {"error": f"Systemd service name already exists: {new_service_name}", "field": "server_name"},
            status_code=409,
        )
    if not old_directory.exists():
        return JSONResponse(
            {"error": f"Server directory does not exist: {old_directory}", "field": "server_name"},
            status_code=400,
        )

    register_server(server)
    status = server_status(server.id)
    was_running = bool(status.get("running"))
    was_enabled_at_boot = server.process_backend == "systemd" and status.get("enabled_at_boot") is True
    confirmed = data.get("confirm") is True
    if not confirmed:
        return JSONResponse({
            "error": "Renaming changes the server directory and service name.",
            "rename_confirmation_required": True,
            "running": was_running,
            "directory": str(new_directory),
            "service_name": new_service_name,
            "suppress_toast": True,
        }, status_code=409)

    old_name = server.name
    old_service_name = server.service_name
    directory_moved = False
    new_registration = False
    try:
        if was_running:
            stop_server_and_wait(server.id)
        if was_enabled_at_boot:
            set_systemd_enabled(server.id, False)
        if new_directory != old_directory:
            old_directory.rename(new_directory)
            directory_moved = True
        server.name = name
        server.directory = str(new_directory)
        server.service_name = new_service_name
        register_server(server)
        new_registration = True
        if was_enabled_at_boot:
            set_systemd_enabled(server.id, True)
        db.commit()
    except Exception as error:
        db.rollback()
        if was_enabled_at_boot and new_registration:
            try:
                set_systemd_enabled(server.id, False)
            except Exception:
                pass
        server.name = old_name
        server.directory = str(old_directory)
        server.service_name = old_service_name
        if directory_moved and new_directory.exists() and not old_directory.exists():
            try:
                new_directory.rename(old_directory)
            except OSError:
                pass
        register_server(server)
        if was_enabled_at_boot:
            try:
                set_systemd_enabled(server.id, True)
            except Exception:
                pass
        if was_running and not server_status(server.id).get("running"):
            try:
                pid = start_server(
                    server.id,
                    server.directory,
                    server.memory,
                    server.jar_name,
                    server.java_args,
                    server.min_memory,
                    server.java_path,
                )
                _record_plugin_session_start(db, server, pid)
            except Exception:
                pass
        return JSONResponse({"error": f"Unable to rename server: {error}"}, status_code=400)

    restart_warning = None
    if was_running:
        try:
            pid = start_server(
                server.id,
                server.directory,
                server.memory,
                server.jar_name,
                server.java_args,
                server.min_memory,
                server.java_path,
            )
            _record_plugin_session_start(db, server, pid)
        except Exception as error:
            restart_warning = (
                f"Server renamed, but it could not be restarted automatically: {error}"
            )

    return {
        "success": True,
        "message": f"Server renamed to {name}.",
        "server_name": name,
        "directory": str(new_directory),
        "service_name": new_service_name,
        "restarted": was_running and restart_warning is None,
        "warning": restart_warning,
    }


@router.post(
    "/api/web/servers/{server_id}/properties"
)
async def save_properties_api(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):

    user, server = (
        get_accessible_server(
            server_id,
            request,
            db,
        )
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()
    error_field = None
    original_backend = server.process_backend
    current_status = server_status(server.id)
    restart_if_running = data.pop("restart_if_running", False)
    if not isinstance(restart_if_running, bool):
        return JSONResponse(
            {"error": "Restart confirmation must be true or false"},
            status_code=400,
        )
    if current_status.get("running") and not restart_if_running:
        return JSONResponse(
            {
                "error": "Saving these changes requires restarting the server.",
                "restart_confirmation_required": True,
                "suppress_toast": True,
            },
            status_code=409,
        )
    stopped_for_save = False
    changes_committed = False
    port_warning = None

    try:

        process_backend = str(data.pop("process_backend", server.process_backend))
        enabled_at_boot = data.pop("enabled_at_boot", None)
        if enabled_at_boot is not None and not isinstance(enabled_at_boot, bool):
            error_field = "enabled_at_boot"
            raise ValueError("Automatic startup must be true or false")
        if process_backend not in {"systemd", "subprocess"}:
            error_field = "process_backend"
            raise ValueError("Select a valid process backend")
        if (
            process_backend == "systemd"
            and original_backend != "systemd"
            and not systemd_available()
        ):
            error_field = "process_backend"
            raise ValueError(
                "Systemd services are only available on Linux hosts running systemd"
            )
        if process_backend != server.process_backend:
            error_field = "process_backend"
            if (
                server.process_backend == "systemd"
                and current_status.get("enabled_at_boot")
                and enabled_at_boot is not False
            ):
                raise ValueError(
                    "Disable automatic startup before changing to a panel-owned process"
                )

        error_field = "min_memory"
        min_memory = normalize_memory(
            data.pop("min_memory", server.min_memory), "Initial RAM"
        )
        error_field = "max_memory"
        max_memory = normalize_memory(
            data.pop("max_memory", server.memory), "Maximum RAM"
        )
        jar_name = str(data.pop("jar_name", server.jar_name)).strip()
        java_args = str(data.pop("java_args", server.java_args)).strip()
        stop_commands = str(data.pop("stop_commands", server.stop_commands)).strip()
        try:
            java_major = int(data.pop("java_major"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Select an installed Java runtime") from error
        java_path = select_java_major(java_major)

        units = {"K": 1, "M": 1024, "G": 1024 * 1024}
        def memory_kib(value: str) -> int:
            return int(value[:-1]) * units[value[-1]]

        if memory_kib(min_memory) > memory_kib(max_memory):
            error_field = "min_memory"
            raise ValueError("Initial RAM cannot be greater than maximum RAM")

        # Validate syntax before touching the filesystem, then only inspect a
        # basename contained by the configured server directory.
        error_field = "jar_name"
        resolve_server_jar(server.directory, jar_name)
        error_field = "java_args"
        if len(java_args) > 1000:
            raise ValueError("Java startup options cannot exceed 1000 characters")
        build_java_command(max_memory, jar_name, java_args, min_memory, java_path)
        if len(stop_commands) > 4000 or len([line for line in stop_commands.splitlines() if line.strip()]) > 20:
            error_field = "stop_commands"
            raise ValueError("Use no more than 20 pre-stop commands and 4000 characters")

        if current_status.get("running"):
            error_field = None
            from .processes import run_pre_stop_commands
            run_pre_stop_commands(server.id, stop_commands)
            stop_server_and_wait(server.id)
            stopped_for_save = True

        error_field = None
        save_properties(
            server,
            data,
        )

        server.min_memory = min_memory
        server.memory = max_memory
        server.jar_name = jar_name
        server.java_args = java_args
        server.stop_commands = stop_commands
        server.java_path = java_path
        server.process_backend = process_backend

        # Keep DB copy of port in sync.
        if "server_port" in data:

            server.port = int(
                data["server_port"]
            )
            conflict = port_assignment_conflict(db, server.port, server.id)
            if conflict:
                port_warning = (
                    f"{conflict.name} is also assigned port {server.port}. "
                    "Only one of these servers can run at a time."
                )

        register_server(server)

        if (
            process_backend == "systemd"
            and enabled_at_boot is not None
            and enabled_at_boot != current_status.get("enabled_at_boot")
        ):
            error_field = "enabled_at_boot"
            set_systemd_enabled(server.id, enabled_at_boot)
        elif (
            original_backend == "systemd"
            and process_backend == "subprocess"
            and enabled_at_boot is False
            and current_status.get("enabled_at_boot") is True
        ):
            # The server is still registered as systemd until this save, so use
            # its original registration to remove the old boot policy first.
            server.process_backend = "systemd"
            register_server(server)
            set_systemd_enabled(server.id, False)
            server.process_backend = process_backend
            register_server(server)

        error_field = None
        db.commit()
        changes_committed = True

        if stopped_for_save:
            pid = start_server(
                server.id,
                server.directory,
                server.memory,
                server.jar_name,
                server.java_args,
                server.min_memory,
                server.java_path,
            )
            _record_plugin_session_start(db, server, pid)

    except Exception as error:

        db.rollback()
        if not changes_committed:
            server.process_backend = original_backend
            register_server(server)
        else:
            register_server(server)

        if stopped_for_save and not changes_committed:
            try:
                pid = start_server(
                    server.id,
                    server.directory,
                    server.memory,
                    server.jar_name,
                    server.java_args,
                    server.min_memory,
                    server.java_path,
                )
                _record_plugin_session_start(db, server, pid)
            except Exception:
                pass

        return JSONResponse(
            {"error": str(error), "field": error_field},
            status_code=400,
        )

    running = bool(server_status(server.id).get("running"))

    return {
        "success": True,
        "message": (
            "Changes saved and the server was restarted."
            if stopped_for_save
            else "Changes saved. They will apply when the server is next started."
        ),
        "warning": port_warning,
        "running": running,
        "restart_required": running and not stopped_for_save,
        "restarted": stopped_for_save,
    }
