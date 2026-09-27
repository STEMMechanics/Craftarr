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
from .database import get_db
from .models import AccessRole, Server, User
from .permissions import has_permission
from .web_context import build_web_context


router = APIRouter()

templates = Jinja2Templates(
    directory="app/templates"
)


def current_web_user(
    request: Request,
    db: Session,
):
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

    servers = (
        db.query(Server)
        .order_by(Server.name)
        .all()
    )

    assigned_ids = {
        server.id
        for server
        in edit_user.servers
    }

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

    selected_ids = {
        int(value)
        for value
        in form.getlist("servers")
    }

    servers = (
        db.query(Server)
        .filter(
            Server.id.in_(
                selected_ids
            )
        )
        .all()
        if selected_ids
        else []
    )

    edit_user.servers = servers

    db.commit()

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

    db.delete(edit_user)
    db.commit()

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
