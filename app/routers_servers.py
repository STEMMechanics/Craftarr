from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)

from sqlalchemy.orm import Session

from .auth import (
    get_current_user,
    require_servers_create,
    require_servers_delete,
    require_servers_properties,
    require_users_manage,
)

from .database import get_db
from .audit import record_audit_event

from .models import (
    Server,
    User,
)

from .permissions import (
    get_server_for_user,
    has_permission,
)
from .processes import register_server, server_status, systemd_available

from .schemas import (
    ServerAccessRequest,
    ServerCreate,
    ServerOut,
    ServerUpdate,
)


router = APIRouter(
    prefix="/api/servers",
    tags=["Servers"],
)


@router.get(
    "",
    response_model=list[ServerOut],
)
def list_servers(
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.view"):
        raise HTTPException(status_code=403, detail="Server view permission required")

    if has_permission(user, "servers.view_all"):

        return (
            db.query(Server)
            .order_by(Server.name)
            .all()
        )

    return sorted(
        user.servers,
        key=lambda server:
            server.name.lower(),
    )


@router.get(
    "/{server_id}",
    response_model=ServerOut,
)
def get_server(
    server_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(
        get_current_user
    ),
):
    if not has_permission(user, "servers.view"):
        raise HTTPException(status_code=403, detail="Server view permission required")

    return get_server_for_user(
        db,
        server_id,
        user,
    )


@router.post(
    "",
    response_model=ServerOut,
    status_code=201,
)
def create_server(
    payload: ServerCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(
        require_servers_create
    ),
):

    if payload.process_backend == "systemd" and not systemd_available():
        raise HTTPException(
            status_code=400,
            detail="Systemd services are only available on Linux hosts running systemd",
        )

    server = Server(
        **payload.model_dump()
    )

    db.add(server)

    try:
        db.commit()

    except Exception:

        db.rollback()

        raise HTTPException(
            status_code=409,
            detail=(
                "Server name, directory "
                "or service name already exists"
            ),
        )

    db.refresh(server)

    record_audit_event(
        db,
        server_id=server.id,
        server_name=server.name,
        actor_user_id=admin.id,
        actor_username=admin.username,
        action="Server created",
    )

    return server


@router.patch(
    "/{server_id}",
    response_model=ServerOut,
)
def update_server(
    server_id: int,
    payload: ServerUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(
        require_servers_properties
    ),
):

    server = db.get(
        Server,
        server_id,
    )

    if not server:

        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )

    changes = payload.model_dump(
        exclude_unset=True
    )
    changed_fields = [
        field for field, value in changes.items()
        if getattr(server, field) != value
    ]

    requested_backend = changes.get("process_backend", server.process_backend)
    if (
        requested_backend == "systemd"
        and server.process_backend != "systemd"
        and not systemd_available()
    ):
        raise HTTPException(
            status_code=400,
            detail="Systemd services are only available on Linux hosts running systemd",
        )
    if requested_backend != server.process_backend:
        status = server_status(server.id)
        if status.get("running"):
            raise HTTPException(
                status_code=409,
                detail="Stop the server before changing its process backend",
            )
        if server.process_backend == "systemd" and status.get("enabled_at_boot"):
            raise HTTPException(
                status_code=409,
                detail="Disable automatic startup before changing its process backend",
            )

    for field, value in changes.items():

        setattr(
            server,
            field,
            value,
        )

    db.commit()
    db.refresh(server)
    register_server(server)

    if changed_fields:
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server settings changed",
            details="Changed fields: " + ", ".join(changed_fields),
        )

    return server


@router.delete(
    "/{server_id}",
    status_code=204,
)
def delete_server(
    server_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(
        require_servers_delete
    ),
):

    server = db.get(
        Server,
        server_id,
    )

    if not server:

        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )

    server_name = server.name
    db.delete(server)
    db.commit()
    record_audit_event(
        db,
        server_id=server_id,
        server_name=server_name,
        actor_user_id=admin.id,
        actor_username=admin.username,
        action="Server deleted",
    )


@router.post(
    "/access/assign",
    status_code=204,
)
def assign_server_access(
    payload: ServerAccessRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(
        require_users_manage
    ),
):

    user = db.get(
        User,
        payload.user_id,
    )

    server = db.get(
        Server,
        payload.server_id,
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    if not server:

        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )

    if has_permission(user, "servers.view_all"):

        raise HTTPException(
            status_code=400,
            detail=(
                "Admins already have access "
                "to all servers"
            ),
        )

    if server not in user.servers:

        user.servers.append(server)

        db.commit()
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server access assigned",
            details=f"Access assigned to {user.username}",
        )


@router.post(
    "/access/revoke",
    status_code=204,
)
def revoke_server_access(
    payload: ServerAccessRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(
        require_users_manage
    ),
):

    user = db.get(
        User,
        payload.user_id,
    )

    server = db.get(
        Server,
        payload.server_id,
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    if not server:

        raise HTTPException(
            status_code=404,
            detail="Server not found",
        )

    if server in user.servers:

        user.servers.remove(server)

        db.commit()
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server access revoked",
            details=f"Access revoked from {user.username}",
        )
