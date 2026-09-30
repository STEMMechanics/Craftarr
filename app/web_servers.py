import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from .env import getenv

from pathlib import Path

from dotenv import load_dotenv

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

from fastapi.responses import (
    HTMLResponse,
    FileResponse,
    JSONResponse,
    RedirectResponse,
)

from fastapi.templating import Jinja2Templates

from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .database import get_db
from .audit import record_audit_event

from .models import (
    Server,
    ServerUpdateCheck,
    User,
)

from .paper import (
    create_eula,
    create_server_properties,
    download_paper,
    inspect_paper_jar,
    match_paper_build,
    get_versions,
    get_latest_build,
    get_builds,
)

from .processes import (
    get_console,
    restart_server,
    run_pre_stop_commands,
    send_command,
    server_status,
    start_server,
    stop_server,
    register_server,
    console_cursor,
    wait_for_console_message,
    MEMORY_PATTERN,
    SERVICE_PATTERN,
    systemd_available,
)
from .server_import import detect_server_directories, inspect_server_directory
from .server_deletion import delete_managed_server
from .java_runtime import (
    discover_java_runtimes, java_runtime_choices, select_java_major,
    select_java_runtime,
)
from .permissions import has_permission

from .web_context import (
    build_web_context,
)

from .web_render import (
    render_page,
)

from .web_users import (
    current_web_user,
)

from .processes import (
    server_process_stats,
)
from .config import MAX_UPLOAD_BYTES
from .server_archives import (
    SETTINGS_EXPORT_FILENAME,
    extract_server_archive,
    server_settings_document,
    validate_portable_settings,
)


load_dotenv(
    getenv(
        "CRAFTARR_CONSOLE_ENV",
        ".env",
    )
)

router = APIRouter()
logger = logging.getLogger(__name__)

templates = Jinja2Templates(
    directory="app/templates"
)


SERVER_ROOT = Path(
    getenv(
        "CRAFTARR_CONSOLE_SERVER_ROOT",
        "minecraft-servers",
    )
).expanduser().resolve()


SERVER_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)


@router.post("/api/web/servers/{server_id}/delete")
async def web_delete_server(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "servers.delete"):
        return JSONResponse({"error": "Admin required"}, status_code=403)

    server = db.get(Server, server_id)
    if not server:
        return JSONResponse({"error": "Server not found"}, status_code=404)

    data = await request.json()
    if data.get("confirmed") is not True:
        return JSONResponse(
            {"error": "Confirm the server deletion"},
            status_code=400,
        )

    try:
        return delete_managed_server(
            db,
            server,
            delete_files=data.get("delete_files") is True,
            server_root=SERVER_ROOT,
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:
        return JSONResponse({"error": f"Unable to delete server: {error}"}, status_code=500)

# -------------------------------------------------------------------
# Server access helper
# -------------------------------------------------------------------

def get_accessible_server(
    server_id: int,
    request: Request,
    db: Session,
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return None, None

    server = db.get(
        Server,
        server_id,
    )

    if not server:
        return user, None

    if (
        not has_permission(user, "servers.view_all")
        and server not in user.servers
    ):
        return user, None

    register_server(server)

    return user, server


def port_assignment_conflict(
    db: Session,
    port: int,
    exclude_server_id: int | None = None,
):
    query = db.query(Server).filter(Server.port == port)
    if exclude_server_id is not None:
        query = query.filter(Server.id != exclude_server_id)
    return query.order_by(Server.name).first()


def downgrade_managed_port_conflict(db: Session, inspection: dict) -> None:
    port = inspection.get("port")
    if not isinstance(port, int):
        return
    conflict = port_assignment_conflict(db, port)
    if not conflict:
        return
    inspection["errors"] = [
        error for error in inspection.get("errors", [])
        if error != f"Port {port} is already in use"
    ]
    warning = f"{conflict.name} is also assigned port {port}. Only one can run at a time."
    if warning not in inspection.setdefault("warnings", []):
        inspection["warnings"].append(warning)
    inspection["ready"] = not inspection["errors"]


@router.get("/api/web/servers/port-warning")
def server_port_warning(
    port: int,
    request: Request,
    exclude_server_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "servers.create") and not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    conflict = port_assignment_conflict(db, port, exclude_server_id)
    return {
        "warning": (
            f'{conflict.name} is also assigned port {port}. Only one of these servers can run at a time.'
            if conflict else None
        )
    }


# -------------------------------------------------------------------
# Server list
# -------------------------------------------------------------------

@router.get(
    "/servers",
    response_class=HTMLResponse,
)
def servers_page(
    request: Request,
    db: Session = Depends(get_db),
    active_server_id: int | None = None,
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if has_permission(user, "servers.view_all"):

        servers = (
            db.query(Server)
            .order_by(Server.name)
            .all()
        )

    else:

        servers = user.servers

    context = build_web_context(db, user)
    active_server = next(
        (server for server in servers if server.id == active_server_id),
        context["active_server"],
    )
    context.update({
        "user": user,
        "servers": servers,
        "available_servers": servers,
        "active_server": active_server,
    })

    return render_page(
        request,
        "servers.html",
        "partials/servers.html",
        context,
    )


# -------------------------------------------------------------------
# Create server
# -------------------------------------------------------------------

@router.get(
    "/servers/new",
    response_class=HTMLResponse,
)
def new_server_page(
    request: Request,
    db: Session = Depends(get_db),
):

    user = current_web_user(
        request,
        db,
    )

    if not user:

        return RedirectResponse(
            "/login"
        )


    if not has_permission(user, "servers.create"):

        return RedirectResponse(
            "/dashboard"
        )


    try:

        versions = get_versions()

    except Exception:

        versions = []


    context = build_web_context(
        db,
        user,
    )

    context.update({
        "page_title":
            "Create Server",

        "active_page":
            "servers",

        "versions":
            versions,
        "java_runtimes": java_runtime_choices(),
    })


    return render_page(
        request,
        "server_new.html",
        "partials/server_new.html",
        context,
    )

@router.post(
    "/servers/new"
)
def create_server_web(
    request: Request,

    name: str = Form(),

    minecraft_version: str = Form(),

    java_major: int = Form(),

    memory: str = Form(
        default="4G"
    ),

    process_backend: str = Form(
        default="systemd"
    ),

    port: int = Form(
        default=25565
    ),

    max_players: int = Form(
        default=20
    ),

    difficulty: str = Form(
        default="normal"
    ),

    gamemode: str = Form(
        default="survival"
    ),

    view_distance: int = Form(
        default=10
    ),

    simulation_distance: int = Form(
        default=10
    ),

    world_name: str = Form(
        default="world"
    ),

    seed: str = Form(
        default=""
    ),

    world_type: str = Form(
        default="minecraft:normal"
    ),

    generate_structures: bool = Form(
        default=False
    ),

    spawn_animals: bool = Form(
        default=False
    ),

    spawn_monsters: bool = Form(
        default=False
    ),

    spawn_npcs: bool = Form(
        default=False
    ),

    online_mode: bool = Form(
        default=False
    ),

    whitelist: bool = Form(
        default=False
    ),

    pvp: bool = Form(
        default=False
    ),

    enable_command_blocks: bool = Form(
        default=False
    ),

    motd: str = Form(
        default="A Minecraft Server"
    ),

    accept_eula: bool = Form(
        default=False
    ),

    db: Session = Depends(
        get_db
    ),
):

    user = current_web_user(
        request,
        db,
    )


    if not user:

        return RedirectResponse(
            "/login"
        )


    if not has_permission(user, "servers.create"):

        return RedirectResponse(
            "/dashboard"
        )


    if not accept_eula:

        raise HTTPException(
            status_code=400,
            detail=(
                "You must accept the "
                "Minecraft EULA."
            ),
        )


    name = name.strip()

    world_name = (
        world_name.strip()
        or "world"
    )

    seed = seed.strip()

    motd = (
        motd.strip()
        or "A Minecraft Server"
    )


    if not name:

        raise HTTPException(
            status_code=400,
            detail="Server name required",
        )


    if not (
        1 <= port <= 65535
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid server port",
        )


    if not (
        1 <= max_players <= 1000
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid max players",
        )


    if not (
        2 <= view_distance <= 32
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid view distance",
        )


    if not (
        2 <= simulation_distance <= 32
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid simulation distance"
            ),
        )


    if difficulty not in (
        "peaceful",
        "easy",
        "normal",
        "hard",
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid difficulty",
        )


    if gamemode not in (
        "survival",
        "creative",
        "adventure",
        "spectator",
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid gamemode",
        )


    allowed_world_types = {
        "minecraft:normal",
        "minecraft:flat",
        "minecraft:large_biomes",
        "minecraft:amplified",
    }


    if (
        world_type
        not in allowed_world_types
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid world type",
        )


    existing = (
        db.query(Server)
        .filter(
            Server.name == name
        )
        .first()
    )


    if existing:

        raise HTTPException(
            status_code=409,
            detail=(
                "Server name already exists"
            ),
        )


    slug = (
        name
        .lower()
        .replace(
            " ",
            "-",
        )
    )


    slug = "".join(
        character
        for character in slug
        if (
            character.isalnum()
            or character == "-"
        )
    )


    if not slug:

        raise HTTPException(
            status_code=400,
            detail="Invalid server name",
        )


    directory = (
        SERVER_ROOT
        / slug
    )


    if directory.exists():

        raise HTTPException(
            status_code=409,
            detail=(
                "Server directory "
                "already exists"
            ),
        )


    directory.mkdir(
        parents=True
    )


    try:

        result = download_paper(
            minecraft_version,
            str(directory),
        )


        create_eula(
            str(directory)
        )


        create_server_properties(
            directory=
                str(directory),

            port=
                port,

            max_players=
                max_players,

            difficulty=
                difficulty,

            gamemode=
                gamemode,

            view_distance=
                view_distance,

            simulation_distance=
                simulation_distance,

            world_name=
                world_name,

            seed=
                seed,

            world_type=
                world_type,

            generate_structures=
                generate_structures,

            spawn_animals=
                spawn_animals,

            spawn_monsters=
                spawn_monsters,

            spawn_npcs=
                spawn_npcs,

            online_mode=
                online_mode,

            whitelist=
                whitelist,

            pvp=
                pvp,

            enable_command_blocks=
                enable_command_blocks,

            motd=
                motd,
        )


        if process_backend not in {"subprocess", "systemd"}:
            raise ValueError("Invalid process backend")
        if process_backend == "systemd" and not systemd_available():
            raise ValueError(
                "Systemd services are only available on Linux hosts running systemd"
            )

        java_path = select_java_major(java_major)

        server = Server(
            name=name,

            directory=
                str(directory),

            service_name=
                slug,

            minecraft_version=
                result["version"],

            paper_build=
                result["build"],

            memory=
                memory,

            min_memory=
                memory,

            process_backend=
                process_backend,

            java_path=java_path,

            port=
                port,

            enabled=
                True,
        )


        db.add(
            server
        )

        db.commit()

        db.refresh(
            server
        )


    except Exception as error:

        import shutil

        shutil.rmtree(
            directory,
            ignore_errors=True,
        )

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


    try:
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=user.id,
            actor_username=user.username,
            action="Server created",
        )
    except Exception:
        logger.exception("Unable to audit server creation for server %s", server.id)


    return RedirectResponse(
        f"/servers/{server.id}",
        status_code=303,
    )


# -------------------------------------------------------------------
# Import existing server
#
# Keep this static route above /servers/{server_id} so "import" can never
# be interpreted as a server identifier by the router.
# -------------------------------------------------------------------

@router.get(
    "/servers/import",
    response_class=HTMLResponse,
)
def import_servers_page(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if not has_permission(user, "servers.create"):
        return RedirectResponse(
            "/dashboard"
        )

    existing_dirs = {
        str(
            Path(
                server.directory
            ).resolve()
        )
        for server
        in db.query(Server).all()
    }

    detected = detect_server_directories(SERVER_ROOT, existing_dirs)

    context = build_web_context(
        db,
        user,
    )

    context.update({
        "detected_servers":
            detected,

        "max_upload_bytes":
            MAX_UPLOAD_BYTES,

        "page_title":
            "Import Server",

        "server_root": str(SERVER_ROOT),
    })

    return render_page(
        request,
        "server_import.html",
        "partials/server_import.html",
        context,
    )


# -------------------------------------------------------------------
# Server overview
# -------------------------------------------------------------------

@router.get(
    "/servers/{server_id:int}",
    response_class=HTMLResponse,
)
def server_detail(
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

    if not server or not has_permission(user, "servers.view"):
        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )


    context = build_web_context(
        db,
        user,
        active_server=server,
    )

    context.update({
        "server": server,
        "page_title": "Overview",
        "active_page": "overview",
    })


    return render_page(
        request,
        "server_detail.html",
        "partials/server_detail.html",
        context,
    )


@router.get("/servers/{server_id:int}/export")
def export_server_archive(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "files.view"):
        raise HTTPException(status_code=403, detail="Access denied")

    requested_root = Path(server.directory)
    if requested_root.is_symlink():
        raise HTTPException(status_code=400, detail="The server directory cannot be a symbolic link")
    try:
        server_root = requested_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="Server directory not found") from None
    if not server_root.is_dir():
        raise HTTPException(status_code=404, detail="Server directory not found")

    archive_root = re.sub(r"[^A-Za-z0-9_. -]+", "-", server_root.name).strip(" .")
    if not archive_root:
        archive_root = f"server-{server.id}"
    running = bool(server_status(server.id).get("running"))
    temporary = tempfile.NamedTemporaryFile(
        prefix="craftarr-server-export-",
        suffix=".zip",
        delete=False,
    )
    archive_path = Path(temporary.name)
    temporary.close()

    saves_disabled = False
    try:
        if running:
            cursor = console_cursor(server.id)
            send_command(server.id, "save-all flush")
            saved = wait_for_console_message(
                server.id,
                ["Saved the game", "Saved the world", "Saving complete"],
                timeout=15,
                cursor=cursor,
            )
            if not saved:
                raise HTTPException(
                    status_code=409,
                    detail="Minecraft did not finish saving. Try exporting again.",
                )
            send_command(server.id, "save-off")
            saves_disabled = True

        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=2,
            ) as archive:
            def raise_walk_error(error):
                raise error

            for current, directories, files in os.walk(
                server_root,
                followlinks=False,
                onerror=raise_walk_error,
            ):
                current_path = Path(current)
                if any((current_path / name).is_symlink() for name in directories):
                    raise HTTPException(
                        status_code=400,
                        detail="Replace symbolic links in the server folder before exporting it",
                    )
                relative_directory = current_path.relative_to(server_root)
                if any("\\" in part for part in relative_directory.parts):
                    raise HTTPException(
                        status_code=400,
                        detail="A server path contains a character that cannot be safely archived",
                    )
                if relative_directory.parts:
                    archive.writestr(
                        f"{archive_root}/{relative_directory.as_posix()}/",
                        b"",
                    )
                for filename in files:
                    path = current_path / filename
                    if path.is_symlink():
                        raise HTTPException(
                            status_code=400,
                            detail="Replace symbolic links in the server folder before exporting it",
                        )
                    if not path.is_file():
                        raise HTTPException(
                            status_code=400,
                            detail="The server folder contains a special file that cannot be exported",
                        )
                    relative_file = path.relative_to(server_root)
                    if any("\\" in part for part in relative_file.parts):
                        raise HTTPException(
                            status_code=400,
                            detail="A server path contains a character that cannot be safely archived",
                        )
                    archive.write(
                        path,
                        f"{archive_root}/{relative_file.as_posix()}",
                    )

            settings_data = json.dumps(
                server_settings_document(server),
                ensure_ascii=False,
                indent=2,
            )
            archive.writestr(SETTINGS_EXPORT_FILENAME, settings_data)
    except HTTPException:
        archive_path.unlink(missing_ok=True)
        raise
    except Exception as error:
        archive_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Unable to export server: {error}",
        ) from error
    finally:
        if saves_disabled:
            try:
                send_command(server.id, "save-on")
            except Exception:
                pass

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{archive_root}-server.zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(lambda: archive_path.unlink(missing_ok=True)),
    )


# -------------------------------------------------------------------
# Server status
# -------------------------------------------------------------------

@router.get(
    "/api/web/servers/{server_id}/status"
)
def web_server_status(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "servers.view"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )

    return server_status(
        server_id
    )


# -------------------------------------------------------------------
# Start
# -------------------------------------------------------------------

@router.post(
    "/api/web/servers/{server_id}/start"
)
def web_start_server(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "servers.control"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )


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


        # A successful start means pending
        # plugin changes have now been loaded.
        server.plugins_dirty = False
        server.plugin_session_pid = pid

        db.commit()


        return {
            "success": True,
            "pid": pid,
        }


    except (RuntimeError, ValueError) as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=400,
        )


    except Exception as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=500,
        )


# -------------------------------------------------------------------
# Stop
# -------------------------------------------------------------------

@router.post(
    "/api/web/servers/{server_id}/stop"
)
def web_stop_server(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "servers.control"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )


    try:

        run_pre_stop_commands(server.id, server.stop_commands)

        stop_server(
            server.id
        )

        return {
            "success": True
        }


    except (RuntimeError, ValueError) as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=400,
        )


# -------------------------------------------------------------------
# Restart
# -------------------------------------------------------------------

@router.post(
    "/api/web/servers/{server_id}/restart"
)
def web_restart_server(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "servers.control"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )


    try:

        run_pre_stop_commands(server.id, server.stop_commands)

        pid = restart_server(
            server.id,
            server.directory,
            server.memory,
            server.jar_name,
            server.java_args,
            server.min_memory,
            server.java_path,
        )


        # Restart loaded any changed plugins.
        server.plugins_dirty = False
        server.plugin_session_pid = pid

        db.commit()


        return {
            "success": True,
            "pid": pid,
        }


    except RuntimeError as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=400,
        )


    except Exception as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=500,
        )


# -------------------------------------------------------------------
# Send console command
# -------------------------------------------------------------------

@router.post(
    "/api/web/servers/{server_id}/command"
)
async def web_command(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "console.command"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )


    data = await request.json()

    command = (
        data
        .get(
            "command",
            "",
        )
        .strip()
    )


    if not command:

        return JSONResponse(
            {
                "error":
                    "Command required"
            },
            status_code=400,
        )


    try:

        send_command(
            server.id,
            command,
        )

        return {
            "success": True
        }


    except RuntimeError as error:

        return JSONResponse(
            {
                "error":
                    str(error)
            },
            status_code=400,
        )


# -------------------------------------------------------------------
# Console data
# -------------------------------------------------------------------

@router.get(
    "/api/web/servers/{server_id}/console-data"
)
def web_console_data(
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
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    if not server or not has_permission(user, "console.view"):
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )


    status = server_status(
        server_id
    )

    running = status.get(
        "running",
        False,
    )
    state = status.get("state", "running" if running else "stopped")
    console_available = bool(status.get("console_available"))


    # While running, use the live console
    # buffer owned by the panel.
    if running:

        return {
            "running": running,
            "state": state,
            "console_available": console_available,
            "source": "console",

            "lines":
                get_console(
                    server_id
                ),
        }


    # When stopped, fall back to latest.log.
    log_path = (
        Path(server.directory)
        / "logs"
        / "latest.log"
    )


    if not log_path.exists():

        return {
            "running": False,
            "state": state,
            "console_available": False,
            "source": "none",
            "lines": [],
        }


    try:

        lines = log_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()

        # Keep the response manageable.
        lines = lines[-500:]


    except OSError:

        lines = []


    return {
        "running": False,
        "state": state,
        "console_available": False,
        "source": "latest.log",
        "lines": lines,
    }


# -------------------------------------------------------------------
# Console page
# -------------------------------------------------------------------

@router.get(
    "/servers/{server_id:int}/console",
    response_class=HTMLResponse,
)
def console_page(
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

    if not server or not has_permission(user, "console.view"):
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
        "page_title": "Console",
        "active_page": "console",
    })


    return render_page(
        request,
        "console.html",
        "partials/console.html",
        context,
    )


@router.post("/api/web/servers/import/archive")
async def import_server_archive(
    request: Request,
    archive_file: UploadFile = File(...),
    name: str = Form(...),
    process_backend: str = Form(default="systemd"),
    db: Session = Depends(get_db),
):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "servers.create"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if process_backend not in {"subprocess", "systemd"}:
        return JSONResponse({"error": "Invalid process backend"}, status_code=400)
    if process_backend == "systemd" and not systemd_available():
        return JSONResponse(
            {"error": "Systemd services are only available on Linux hosts running systemd"},
            status_code=400,
        )

    name = name.strip()
    if not name:
        return JSONResponse({"error": "Server name is required"}, status_code=400)
    if len(name) > 100:
        return JSONResponse({"error": "Server name cannot exceed 100 characters"}, status_code=400)
    name_slug = re.sub(r"[^a-z0-9-]+", "-", name.casefold().replace(" ", "-"))
    name_slug = re.sub(r"-+", "-", name_slug).strip("-")[:100]
    if not name_slug:
        return JSONResponse(
            {"error": "Server name must contain at least one letter or number"},
            status_code=400,
        )

    if db.query(Server).filter(Server.name == name).first():
        return JSONResponse({"error": "Server name already exists"}, status_code=409)

    directory_path = SERVER_ROOT / name_slug
    service_name = name_slug
    if not SERVICE_PATTERN.fullmatch(service_name):
        return JSONResponse({"error": "Server name cannot be used for a service name"}, status_code=400)
    if (
        directory_path.is_symlink()
        or directory_path.exists()
        or db.query(Server).filter(Server.directory == str(directory_path)).first()
    ):
        return JSONResponse(
            {"error": f"Server directory already exists: {directory_path}"},
            status_code=409,
        )
    if db.query(Server).filter(Server.service_name == service_name).first():
        return JSONResponse({"error": "Systemd service name is already in use"}, status_code=409)

    temporary_archive = None
    staging_root = None
    installed_directory = False
    try:
        temporary = tempfile.NamedTemporaryFile(
            prefix=".craftarr-upload-",
            suffix=".zip",
            dir=SERVER_ROOT,
            delete=False,
        )
        temporary_archive = Path(temporary.name)
        written = 0
        with temporary as output:
            while chunk := await archive_file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ValueError("The uploaded ZIP is larger than the configured upload limit")
                output.write(chunk)
        if written == 0:
            raise ValueError("Choose a ZIP archive to upload")

        staging_root = SERVER_ROOT / f".craftarr-import-{uuid.uuid4().hex}"
        staging_root.mkdir(mode=0o700)
        extracted = extract_server_archive(
            temporary_archive,
            staging_root / "server",
            max_uncompressed_bytes=MAX_UPLOAD_BYTES * 4,
        )
        inspection = inspect_server_directory(
            str(extracted["server_directory"]),
            process_backend=process_backend,
            verify_write=True,
        )
        downgrade_managed_port_conflict(db, inspection)
        if not inspection["ready"]:
            raise ValueError("; ".join(inspection["errors"]))

        defaults = {
            "minecraft_version": None,
            "paper_build": None,
            "memory": "4G",
            "min_memory": "4G",
            "jar_name": inspection["jar_name"],
            "java_args": "",
            "stop_commands": "",
        }
        portable_settings = validate_portable_settings(
            extracted["settings"] or {},
            extracted["server_directory"],
            defaults=defaults,
        )

        extracted["server_directory"].rename(directory_path)
        installed_directory = True
        final_inspection = inspect_server_directory(
            directory_path,
            process_backend=process_backend,
            verify_write=True,
        )
        downgrade_managed_port_conflict(db, final_inspection)
        if not final_inspection["ready"]:
            raise ValueError("; ".join(final_inspection["errors"]))

        server = Server(
            name=name,
            directory=str(directory_path),
            service_name=service_name,
            minecraft_version=portable_settings["minecraft_version"],
            paper_build=portable_settings["paper_build"],
            port=final_inspection["port"],
            memory=portable_settings["memory"],
            min_memory=portable_settings["min_memory"],
            jar_name=portable_settings["jar_name"],
            java_args=portable_settings["java_args"],
            stop_commands=portable_settings["stop_commands"],
            java_path=select_java_runtime(
                discover_java_runtimes(),
                portable_settings["minecraft_version"],
            ) or "java",
            process_backend=process_backend,
            enabled=True,
        )
        db.add(server)
        try:
            db.commit()
        except Exception as error:
            db.rollback()
            raise ValueError("Server name, directory, or service name is already managed") from error
        db.refresh(server)

        try:
            record_audit_event(
                db,
                server_id=server.id,
                server_name=server.name,
                actor_user_id=user.id,
                actor_username=user.username,
                action="Server imported from archive",
            )
        except Exception:
            logger.exception("Unable to audit archive import for server %s", server.id)

        return JSONResponse({
            "success": True,
            "redirect_url": f"/servers/{server.id}",
            "settings_imported": extracted["settings"] is not None,
            "warnings": final_inspection.get("warnings", []),
        })
    except ValueError as error:
        if installed_directory:
            shutil.rmtree(directory_path, ignore_errors=True)
        return JSONResponse({"error": str(error)}, status_code=400)
    except (OSError, zipfile.BadZipFile) as error:
        if installed_directory:
            shutil.rmtree(directory_path, ignore_errors=True)
        return JSONResponse({"error": f"Unable to import the server ZIP: {error}"}, status_code=400)
    except Exception as error:
        db.rollback()
        if installed_directory:
            shutil.rmtree(directory_path, ignore_errors=True)
        return JSONResponse({"error": f"Unable to import the server ZIP: {error}"}, status_code=500)
    finally:
        await archive_file.close()
        if temporary_archive:
            temporary_archive.unlink(missing_ok=True)
        if staging_root:
            shutil.rmtree(staging_root, ignore_errors=True)


@router.post("/api/web/servers/import/inspect")
async def inspect_import_path(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "servers.create"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    data = await request.json()
    result = inspect_server_directory(
        str(data.get("directory", "")),
        process_backend=str(data.get("process_backend", "systemd")),
    )
    downgrade_managed_port_conflict(db, result)
    return result


@router.post(
    "/servers/import"
)
def import_server(
    request: Request,

    directory: str = Form(),
    name: str = Form(),
    memory: str = Form(default="2G"),
    process_backend: str = Form(default="systemd"),

    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if not has_permission(user, "servers.create"):
        return RedirectResponse(
            "/dashboard"
        )


    if process_backend not in {"subprocess", "systemd"}:
        raise HTTPException(status_code=400, detail="Invalid process backend")
    if process_backend == "systemd" and not systemd_available():
        raise HTTPException(
            status_code=400,
            detail="Systemd services are only available on Linux hosts running systemd",
        )
    if not MEMORY_PATTERN.fullmatch(memory):
        raise HTTPException(status_code=400, detail="Invalid memory allocation")

    inspection = inspect_server_directory(
        directory,
        process_backend=process_backend,
        verify_write=True,
    )
    downgrade_managed_port_conflict(db, inspection)
    if not inspection["ready"]:
        raise HTTPException(status_code=400, detail="; ".join(inspection["errors"]))
    directory_path = Path(inspection["directory"])
    jar_name = inspection["jar_name"]
    port = inspection["port"]


    existing = (
        db.query(Server)
        .filter(
            Server.directory
            == str(directory_path)
        )
        .first()
    )

    if existing:

        raise HTTPException(
            status_code=409,
            detail=(
                "Server is already imported"
            ),
        )


    service_name = re.sub(r"[^A-Za-z0-9_.@-]+", "-", directory_path.name).strip("-")[:128]
    if not SERVICE_PATTERN.fullmatch(service_name):
        raise HTTPException(status_code=400, detail="Directory cannot be converted to a valid service name")

    server = Server(
        name=name,

        directory=str(
            directory_path
        ),

        service_name=service_name,

        minecraft_version=None,

        paper_build=None,

        memory=memory,

        min_memory=memory,

        jar_name=jar_name,

        process_backend=process_backend,
        java_path=select_java_runtime(discover_java_runtimes(), None) or "java",

        port=port,

        enabled=True,
    )


    db.add(server)
    try:
        db.commit()
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Server name, directory, service name, or port is already managed") from error
    db.refresh(server)

    try:
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=user.id,
            actor_username=user.username,
            action="Server imported",
        )
    except Exception:
        logger.exception("Unable to audit server import for server %s", server.id)


    return RedirectResponse(
        f"/servers/{server.id}",
        status_code=303,
    )

@router.get("/api/web/servers/{server_id}/paper")
def web_paper_status(server_id: int, request: Request, version: str | None = None, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    try:
        versions = get_versions()
        detected = None
        try:
            detected = inspect_paper_jar(Path(server.directory) / server.jar_name)
            detected_builds = get_builds(detected["version"])
            detected["build"] = match_paper_build(detected["sha256"], detected_builds)
            if (
                detected["version"] != server.minecraft_version
                or detected["build"] != server.paper_build
            ):
                server.minecraft_version = detected["version"]
                server.paper_build = detected["build"]
                db.commit()
        except (ValueError, OSError):
            # A custom or currently unavailable JAR should not prevent release
            # information from loading; retain the last known metadata.
            detected = None
        current_version = server.minecraft_version
        latest_version = versions[0] if versions else current_version
        selected_version = version or current_version
        builds = get_builds(selected_version) if selected_version else []
        latest_build = builds[0] if builds else None
        latest_build_id = str(latest_build["id"]) if latest_build else None
        try:
            builds_behind = (
                max(0, int(latest_build_id) - int(server.paper_build))
                if latest_build_id and server.paper_build is not None
                else None
            )
        except (TypeError, ValueError):
            builds_behind = None
        return {
            "current_version": current_version,
            "current_build": server.paper_build,
            "latest_version": latest_version,
            "latest_build": latest_build_id,
            "builds_behind": builds_behind,
            "versions": versions,
            "selected_version": selected_version,
            "builds": [{"id": str(build["id"]), "channel": build.get("channel", "") } for build in builds],
            "running": server_status(server.id).get("running", False),
            "jar_inspected": detected is not None,
        }
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.post("/api/web/servers/{server_id}/paper")
async def web_install_paper(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "servers.properties"):
        return JSONResponse({"error": "Administrator access required"}, status_code=403)
    if server_status(server.id).get("running"):
        return JSONResponse({"error": "Stop the server before changing Paper"}, status_code=409)
    data = await request.json()
    version = str(data.get("version", "")).strip()
    try:
        build_id = int(data.get("build"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Select a valid Paper build"}, status_code=400)
    try:
        result = download_paper(version, server.directory, server.jar_name, build_id)
        server.minecraft_version = result["version"]
        server.paper_build = result["build"]
        db.commit()
        try:
            from .update_monitor import paper_result
            status = paper_result(db, server, fetch=True, force=True)
            row = db.get(ServerUpdateCheck, (server.id, "@paper"))
            if row is None:
                row = ServerUpdateCheck(server_id=server.id, component="@paper")
                db.add(row)
            row.payload = json.dumps(status)
            db.commit()
        except Exception:
            db.rollback()
        result["suppress_toast"] = True
        return result
    except (ValueError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.get(
    "/api/web/servers/{server_id}/process-stats"
)
def web_process_stats(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(
        server_id,
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not server or not has_permission(user, "servers.view"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    return server_process_stats(
        server.id
    )
