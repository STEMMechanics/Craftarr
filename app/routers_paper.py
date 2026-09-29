from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from sqlalchemy.orm import Session

from .auth import get_current_user

from .database import get_db

from .models import User

from .paper import (
    create_eula,
    create_server_properties,
    download_paper,
    get_versions,
    java_available,
)

from .permissions import (
    get_server_for_user,
    has_permission,
)

from .processes import (
    send_command,
    server_status,
    start_server,
    stop_server,
)

from .schemas import (
    ConsoleCommandRequest,
    PaperInstallRequest,
    ServerStartRequest,
)


router = APIRouter(
    prefix="/api/paper",
    tags=["Paper"],
)


@router.get("/versions")
def versions(
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.properties"):
        raise HTTPException(status_code=403, detail="Server properties permission required")
    try:
        return {
            "versions": get_versions()
        }

    except Exception as error:

        raise HTTPException(
            status_code=502,
            detail=str(error),
        )


@router.get("/java")
def java_status(
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.properties"):
        raise HTTPException(status_code=403, detail="Server properties permission required")
    return {
        "available": java_available()
    }


@router.post(
    "/{server_id}/install"
)
def install(
    server_id: int,
    payload: PaperInstallRequest,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    server = get_server_for_user(
        db,
        server_id,
        user,
    )

    # Only admins should install/replace Paper.
    if not has_permission(user, "servers.properties"):

        raise HTTPException(
            status_code=403,
            detail="Administrator access required",
        )

    if server_status(server.id).get("running"):
        raise HTTPException(
            status_code=409,
            detail="Stop the server before replacing its Paper JAR",
        )

    try:

        result = download_paper(
            payload.minecraft_version,
            server.directory,
            server.jar_name,
        )

        create_eula(
            server.directory
        )

        create_server_properties(
            server.directory,
            server.port,
        )

        server.minecraft_version = (
            result["version"]
        )

        server.paper_build = (
            result["build"]
        )

        db.commit()

        return result

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error),
        )


@router.get(
    "/{server_id}/status"
)
def status(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.view"):
        raise HTTPException(status_code=403, detail="Server view permission required")
    get_server_for_user(
        db,
        server_id,
        user,
    )

    return server_status(
        server_id
    )


@router.post(
    "/{server_id}/start"
)
def start(
    server_id: int,
    payload: ServerStartRequest,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.control"):
        raise HTTPException(status_code=403, detail="Server control permission required")
    server = get_server_for_user(
        db,
        server_id,
        user,
    )

    try:

        pid = start_server(
            server.id,
            server.directory,
            payload.memory,
            server.jar_name,
            server.java_args,
            server.min_memory,
            server.java_path,
        )

        server.plugins_dirty = False
        server.plugin_session_pid = pid
        db.commit()

        return {
            "running": True,
            "pid": pid,
        }

    except RuntimeError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


@router.post(
    "/{server_id}/stop"
)
def stop(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.control"):
        raise HTTPException(status_code=403, detail="Server control permission required")
    get_server_for_user(
        db,
        server_id,
        user,
    )

    try:

        stop_server(
            server_id
        )

        return {
            "stopping": True
        }

    except RuntimeError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


@router.post(
    "/{server_id}/command"
)
def command(
    server_id: int,
    payload: ConsoleCommandRequest,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "console.command"):
        raise HTTPException(status_code=403, detail="Console command permission required")
    get_server_for_user(
        db,
        server_id,
        user,
    )

    try:

        send_command(
            server_id,
            payload.command,
        )

        return {
            "sent": True
        }

    except RuntimeError as error:

        raise HTTPException(
            status_code=400,
            detail=str(error),
        )
