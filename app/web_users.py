import base64
import json
import re
import uuid
from dataclasses import dataclass

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates

from sqlalchemy.orm import Session

from .auth import hash_password
from .audit import record_audit_event
from .database import get_db
from .models import AccessRole, Server, User
from .node_security import verify_node_token
from .permissions import has_permission
from .server_access import apply_server_assignments, assigned_server_keys, list_server_choices
from .web_context import build_web_context


router = APIRouter()

templates = Jinja2Templates(
    directory="app/templates"
)

_NODE_SERVER_PATH = re.compile(r"^/(?:api/web/servers|servers)/\d+(?:/|$)")


@dataclass(frozen=True)
class HubServerChoice:
    """A namespaced server entry from the authenticated hub's menu."""

    id: str
    name: str
    node_name: str
    minecraft_version: str | None
    memory: str


class GatewayUser:
    """Request-scoped user asserted by an authenticated Craftarr hub."""

    id = None
    role = "user"
    role_id = None
    access_role = None
    enabled = True

    def __init__(
        self,
        username: str,
        permissions: set[str],
        servers: list[Server],
        hub_servers: list[HubServerChoice],
    ):
        self.username = username[:64]
        self.permissions = permissions
        self.servers = servers
        self.hub_servers = hub_servers

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def role_name(self) -> str:
        return "Linked Node user"


def _gateway_user(request: Request, db: Session):
    path = request.url.path
    if not _NODE_SERVER_PATH.match(path):
        return None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not verify_node_token(db, token):
        return None

    encoded = request.headers.get("x-craftarr-user", "")
    if not encoded or len(encoded) > 8192:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        if not isinstance(payload, dict):
            return None
        username = str(payload.get("username", "")).strip()
        permissions = {str(item) for item in payload.get("permissions", [])}
        server_ids = {int(item) for item in payload.get("servers", [])}
        raw_hub_servers = payload.get("hub_servers", [])
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not username
        or len(username) > 64
        or len(permissions) > 64
        or len(server_ids) > 10000
        or any(server_id <= 0 for server_id in server_ids)
        or not isinstance(raw_hub_servers, list)
        or len(raw_hub_servers) > 1000
    ):
        return None

    hub_servers = []
    try:
        for item in raw_hub_servers:
            if not isinstance(item, dict):
                return None
            server_ref = str(item.get("id", ""))
            node_part, server_part = server_ref.rsplit(":", 1)
            uuid.UUID(node_part)
            if int(server_part) <= 0:
                return None
            name = str(item.get("name", "")).strip()
            node_name = str(item.get("node_name", "Remote")).strip()
            if not name or len(name) > 100 or len(node_name) > 100:
                return None
            minecraft_version = (
                str(item["minecraft_version"])
                if item.get("minecraft_version")
                else None
            )
            if minecraft_version and len(minecraft_version) > 40:
                return None
            hub_servers.append(HubServerChoice(
                id=server_ref,
                name=name,
                node_name=node_name or "Remote",
                minecraft_version=minecraft_version,
                memory=str(item.get("memory", "2G"))[:20],
            ))
    except (ValueError, TypeError, AttributeError):
        return None

    servers = (
        db.query(Server).filter(Server.id.in_(server_ids)).all()
        if server_ids else []
    )
    gateway_user = GatewayUser(username, permissions, servers, hub_servers)
    request.state.craftarr_gateway_user = gateway_user
    request.state.craftarr_actor_username = gateway_user.username
    return gateway_user


def current_web_user(
    request: Request,
    db: Session,
):
    gateway_user = _gateway_user(request, db)
    if gateway_user is not None:
        return gateway_user

    user_id = request.session.get(
        "user_id"
    )

    if not user_id:
        return None

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return None

    if not user.enabled:
        return None

    return user


def require_web_admin(
    request: Request,
    db: Session,
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return None

    if not has_permission(user, "users.manage"):
        return None

    return user


@router.get(
    "/users",
    response_class=HTMLResponse,
)
def users_page(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    users = (
        db.query(User)
        .order_by(User.username)
        .all()
    )

    context = build_web_context(db, admin)
    context.update({
        "users": users,
        "roles": db.query(AccessRole).order_by(AccessRole.name).all(),
    })

    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context=context,
    )


@router.post("/users/create")
def create_user(
    request: Request,

    username: str = Form(),

    password: str = Form(),

    role_id: int = Form(),

    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    username = username.strip()

    role = db.get(AccessRole, role_id)
    if not role:
        return RedirectResponse("/users?error=role", status_code=303)

    existing = (
        db.query(User)
        .filter(
            User.username == username
        )
        .first()
    )

    if existing:
        return RedirectResponse(
            "/users?error=username",
            status_code=303,
        )

    new_user = User(
        username=username,
        password_hash=hash_password(
            password
        ),
        role="admin" if role.name == "Administrator" else "user",
        role_id=role.id,
        enabled=True,
    )

    db.add(new_user)
    db.commit()

    return RedirectResponse(
        "/users",
        status_code=303,
    )


@router.get(
    "/users/{user_id}",
    response_class=HTMLResponse,
)
def edit_user_page(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    edit_user = db.get(
        User,
        user_id,
    )

    if not edit_user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    servers = list_server_choices(db)
    assigned_ids = assigned_server_keys(edit_user)

    context = build_web_context(db, admin)
    context.update({
        "edit_user": edit_user,
        "servers": servers,
        "assigned_ids": assigned_ids,
        "roles": db.query(AccessRole).order_by(AccessRole.name).all(),
    })

    return templates.TemplateResponse(
        request=request,
        name="user_edit.html",
        context=context,
    )


@router.post(
    "/users/{user_id}/save"
)
def save_user(
    user_id: int,

    request: Request,

    username: str = Form(),

    role_id: int = Form(),

    enabled: str | None = Form(
        default=None
    ),

    password: str = Form(
        default=""
    ),

    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    edit_user = db.get(
        User,
        user_id,
    )

    if not edit_user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    username = username.strip()

    existing = (
        db.query(User)
        .filter(
            User.username == username,
            User.id != user_id,
        )
        .first()
    )

    if existing:
        return RedirectResponse(
            f"/users/{user_id}"
            "?error=username",
            status_code=303,
        )

    role = db.get(AccessRole, role_id)
    if not role:
        return RedirectResponse(f"/users/{user_id}?error=role", status_code=303)

    edit_user.username = username
    edit_user.role_id = role.id
    edit_user.role = "admin" if role.name == "Administrator" else "user"
    edit_user.enabled = (
        enabled == "on"
    )

    if password.strip():
        edit_user.password_hash = (
            hash_password(
                password
            )
        )

    db.commit()

    return RedirectResponse(
        f"/users/{user_id}",
        status_code=303,
    )


@router.post(
    "/users/{user_id}/servers"
)
async def save_server_access(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    edit_user = db.get(
        User,
        user_id,
    )

    if not edit_user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    form = await request.form()

    previous_servers = {server.id: server for server in edit_user.servers}
    try:
        apply_server_assignments(db, edit_user, form.getlist("servers"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    selected_servers = {server.id: server for server in edit_user.servers}

    db.commit()

    for server_id in sorted(selected_servers.keys() - previous_servers.keys()):
        server = selected_servers[server_id]
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server access assigned",
            details=f"Access assigned to {edit_user.username}",
        )
    for server_id in sorted(previous_servers.keys() - selected_servers.keys()):
        server = previous_servers[server_id]
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server access revoked",
            details=f"Access revoked from {edit_user.username}",
        )

    return RedirectResponse(
        f"/users/{user_id}",
        status_code=303,
    )


@router.post(
    "/users/{user_id}/delete"
)
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = require_web_admin(
        request,
        db,
    )

    if not admin:
        return RedirectResponse(
            "/dashboard"
        )

    edit_user = db.get(
        User,
        user_id,
    )

    if not edit_user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    if edit_user.id == admin.id:
        return RedirectResponse(
            "/users?error=self_delete",
            status_code=303,
        )

    assigned_servers = list(edit_user.servers)
    deleted_username = edit_user.username
    db.delete(edit_user)
    db.commit()

    for server in assigned_servers:
        record_audit_event(
            db,
            server_id=server.id,
            server_name=server.name,
            actor_user_id=admin.id,
            actor_username=admin.username,
            action="Server access revoked",
            details=f"Access revoked from deleted user {deleted_username}",
        )

    return RedirectResponse(
        "/users",
        status_code=303,
    )


@router.get(
    "/profile",
    response_class=HTMLResponse,
)
def profile_page(
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

    context = build_web_context(db, user)

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context=context,
    )


@router.post("/profile")
def save_profile(
    request: Request,

    username: str = Form(),

    password: str = Form(
        default=""
    ),

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

    username = username.strip()

    existing = (
        db.query(User)
        .filter(
            User.username == username,
            User.id != user.id,
        )
        .first()
    )

    if existing:
        return RedirectResponse(
            "/profile?error=username",
            status_code=303,
        )

    user.username = username

    if password.strip():
        user.password_hash = (
            hash_password(
                password
            )
        )

    db.commit()

    return RedirectResponse(
        "/profile?saved=1",
        status_code=303,
    )
