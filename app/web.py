import shutil
import psutil

from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
)

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .version import APP_VERSION
from .auth import verify_password
from .database import get_db
from .models import Server, User
from .processes import register_server, server_status
from .player_manager import get_online_players
from .permissions import has_permission
from .settings_manager import get_login_message
from .java_runtime import discover_java_runtimes, resolve_java_path

from .auth import (
    hash_password,
    verify_password,
)

from .emailer import send_email

from .models import (
    PasswordResetToken,
    User,
)

from .tfa import (
    use_recovery_code,
    verify_totp,
)

from .password_reset import (
    create_reset_token,
    get_valid_reset,
)

from .version import APP_VERSION

from .web_context import (
    build_web_context,
)

from .web_render import (
    render_page,
)

router = APIRouter()

templates = Jinja2Templates(
    directory="app/templates"
)


def _finish_web_login(request: Request, user: User) -> RedirectResponse:
    request.session["user_id"] = user.id
    request.session.pop("pending_tfa_user_id", None)
    request.session.pop("pending_tfa_expires", None)
    return RedirectResponse(
        "/change-password" if user.must_change_password else "/dashboard",
        status_code=303,
    )


@router.get(
    "/login",
    response_class=HTMLResponse,
)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
):

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "app_version": APP_VERSION,
            "login_message": get_login_message(db),
        },
    )


@router.post("/login")
def login_web(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):

    user = (
        db.query(User)
        .filter(User.username == username)
        .first()
    )

    if (
        not user
        or not user.enabled
        or not verify_password(
            password,
            user.password_hash,
        )
    ):

        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error":
                    "Incorrect username or password",
                "app_version":
                    APP_VERSION,
                "login_message":
                    get_login_message(db),
            },
            status_code=401,
        )

    if user.totp_enabled:

        request.session[
            "pending_tfa_user_id"
        ] = user.id

        request.session[
            "pending_tfa_expires"
        ] = (
            datetime.now(timezone.utc)
            + timedelta(minutes=5)
        ).isoformat()

        request.session.pop(
            "user_id",
            None,
        )

        return RedirectResponse(
            "/login/tfa",
            status_code=303,
        )

    return _finish_web_login(request, user)


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.enabled:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    if not user.must_change_password:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="change_password.html",
        context={"app_version": APP_VERSION},
    )


@router.post("/change-password", response_class=HTMLResponse)
def change_password(
    request: Request,
    password: str = Form(),
    confirm_password: str = Form(),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.enabled:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    error = None
    if len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords do not match."
    elif verify_password(password, user.password_hash):
        error = "Choose a password different from the temporary password."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="change_password.html",
            context={"app_version": APP_VERSION, "error": error},
            status_code=400,
        )

    user.password_hash = hash_password(password)
    user.must_change_password = False
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = request.session.get(
        "user_id"
    )

    if not user_id:
        return RedirectResponse(
            "/login"
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    context = build_web_context(
        db,
        user,
    )

    active_server = context[
        "active_server"
    ]

    if active_server:
        return RedirectResponse(
            f"/servers/{active_server.id}",
            status_code=302,
        )

    return RedirectResponse(
        "/system",
        status_code=302,
    )


@router.get("/logout")
def logout(request: Request):

    request.session.clear()

    return RedirectResponse(
        "/login"
    )

@router.get("/api/system/stats")
def web_system_stats(
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = request.session.get(
        "user_id"
    )

    if not user_id:

        return JSONResponse(
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:

        return JSONResponse(
            {
                "error":
                    "Not authenticated"
            },
            status_code=401,
        )

    memory = psutil.virtual_memory()

    disk = shutil.disk_usage("/")

    cpu = psutil.cpu_percent(
        interval=None
    )

    def gb(value):
        return round(
            value
            / 1024
            / 1024
            / 1024,
            1,
        )

    if not has_permission(user, "system.view"):
        return JSONResponse({"error": "System view permission required"}, status_code=403)

    servers = db.query(Server).order_by(Server.name).all() if has_permission(user, "servers.view_all") else sorted(user.servers, key=lambda item: item.name.lower())
    java_runtimes = discover_java_runtimes()
    java_by_path = {runtime["path"]: runtime for runtime in java_runtimes}
    instances = []
    total_players = 0
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
        total_players += players
        try:
            configured_java = resolve_java_path(server.java_path)
        except ValueError:
            configured_java = server.java_path
        instances.append({
            "id": server.id, "name": server.name, "version": server.minecraft_version,
            "running": running, "state": state,
            "console_available": console_available, "players": players,
            "java": java_by_path.get(configured_java, {}).get("major"),
        })

    return {
        "cpu": {
            "percent": cpu,
            "cores":
                psutil.cpu_count(),
        },

        "memory": {
            "used": gb(
                memory.used
            ),
            "total": gb(
                memory.total
            ),
            "percent":
                memory.percent,
        },

        "storage": {
            "used": gb(
                disk.used
            ),
            "total": gb(
                disk.total
            ),
            "percent": round(
                disk.used
                / disk.total
                * 100,
                1,
            ),
        },
        "minecraft": {
            "installed": len(instances), "running": running_count,
            "players_online": total_players, "instances": instances,
        },
        "java_runtimes": java_runtimes,
    }

@router.get(
    "/system",
    response_class=HTMLResponse,
)
def system_page(
    request: Request,
    db: Session = Depends(get_db),
    active_server_id: str | None = None,
):
    user_id = request.session.get(
        "user_id"
    )

    if not user_id:
        return RedirectResponse(
            "/login"
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return RedirectResponse(
            "/login"
        )

    if not has_permission(user, "system.view"):
        return RedirectResponse("/dashboard")

    context = build_web_context(
        db,
        user,
    )
    context["active_server"] = next(
        (
            server for server in context["available_servers"]
            if str(server.id) == str(active_server_id)
        ),
        context["active_server"],
    )

    return render_page(
        request,
        "system.html",
        "partials/system.html",
        context,
    )

@router.get(
    "/forgot-password",
    response_class=HTMLResponse,
)
def forgot_password_page(
    request: Request,
):

    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            "app_version":
                APP_VERSION,
        },
    )

@router.post(
    "/forgot-password",
    response_class=HTMLResponse,
)
def forgot_password(
    request: Request,
    email: str = Form(),
    db: Session = Depends(get_db),
):

    user = (
        db.query(User)
        .filter(
            User.email == email.strip()
        )
        .first()
    )


    if user:

        try:

            token = create_reset_token(
                db,
                user,
            )

            reset_url = (
                str(request.base_url)
                + "reset-password?token="
                + token
            )

            send_email(
                db,
                user.email,
                "Reset your Craftarr password",
                (
                    "A password reset was requested "
                    "for your Craftarr account.\n\n"
                    f"{reset_url}\n\n"
                    "This link expires in 30 minutes."
                ),
            )

        except Exception:
            pass


    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={
            "app_version":
                APP_VERSION,

            "sent":
                True,
        },
    )

@router.get(
    "/reset-password",
    response_class=HTMLResponse,
)
def reset_password_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):

    valid = get_valid_reset(
        db,
        token,
    )

    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "app_version":
                APP_VERSION,

            "token":
                token,

            "valid":
                valid is not None,
        },
    )

@router.post(
    "/reset-password",
    response_class=HTMLResponse,
)
def reset_password(
    request: Request,

    token: str = Form(),
    password: str = Form(),

    db: Session = Depends(get_db),
):

    record = get_valid_reset(
        db,
        token,
    )

    if not record:

        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "app_version":
                    APP_VERSION,

                "valid":
                    False,
            },
            status_code=400,
        )


    if len(password) < 8:

        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "app_version":
                    APP_VERSION,

                "token":
                    token,

                "valid":
                    True,

                "error":
                    "Password must be at least 8 characters.",
            },
            status_code=400,
        )


    user = db.get(
        User,
        record.user_id,
    )

    if not user:

        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "app_version":
                    APP_VERSION,

                "valid":
                    False,
            },
            status_code=400,
        )


    user.password_hash = (
        hash_password(
            password
        )
    )
    user.must_change_password = False

    record.used_at = (
        datetime.utcnow()
    )

    db.commit()


    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "app_version":
                APP_VERSION,

            "success":
                True,

            "valid":
                False,
        },
    )

@router.get(
    "/login/tfa",
    response_class=HTMLResponse,
)
def login_tfa_page(
    request: Request,
    db: Session = Depends(get_db),
):

    user = get_pending_tfa_user(
        request,
        db,
    )

    if not user:

        return RedirectResponse(
            "/login"
        )

    return templates.TemplateResponse(
        request=request,
        name="login_tfa.html",
        context={
            "app_version":
                APP_VERSION,
        },
    )


@router.post(
    "/login/tfa",
    response_class=HTMLResponse,
)
def login_tfa(
    request: Request,
    code: str = Form(),
    db: Session = Depends(get_db),
):

    user = get_pending_tfa_user(
        request,
        db,
    )

    if not user:

        return RedirectResponse(
            "/login"
        )


    if (
        not user.enabled
        or not user.totp_enabled
        or not user.totp_secret
    ):

        request.session.clear()

        return RedirectResponse(
            "/login"
        )


    valid = verify_totp(
        user.totp_secret,
        code,
    )


    if not valid:

        valid = use_recovery_code(
            db,
            user,
            code,
        )


    if not valid:

        return templates.TemplateResponse(
            request=request,
            name="login_tfa.html",
            context={
                "app_version":
                    APP_VERSION,

                "error":
                    "Invalid authentication code.",
            },
            status_code=401,
        )


    return _finish_web_login(request, user)

def get_pending_tfa_user(
    request: Request,
    db: Session,
):

    user_id = request.session.get(
        "pending_tfa_user_id"
    )

    expires = request.session.get(
        "pending_tfa_expires"
    )

    if not user_id or not expires:
        return None

    try:

        expires_at = datetime.fromisoformat(
            expires
        )

    except ValueError:

        request.session.pop(
            "pending_tfa_user_id",
            None,
        )

        request.session.pop(
            "pending_tfa_expires",
            None,
        )

        return None


    if datetime.now(
        timezone.utc
    ) >= expires_at:

        request.session.pop(
            "pending_tfa_user_id",
            None,
        )

        request.session.pop(
            "pending_tfa_expires",
            None,
        )

        return None


    user = db.get(
        User,
        int(user_id),
    )

    if not user:
        return None

    return user
