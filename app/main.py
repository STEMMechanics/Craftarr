from .env import getenv

from dotenv import load_dotenv

load_dotenv(
    getenv(
        "CRAFTARR_CONSOLE_ENV",
        ".env",
    )
)

from fastapi import FastAPI

from .version import APP_VERSION
from .config import COOKIE_SECURE, SECRET_KEY
from .migrations import upgrade_database
from .processes import register_server

from .database import (
    SessionLocal,
)
from .models import User

from .routers_auth import (
    router as auth_router,
)

from .routers_servers import (
    router as servers_router,
)

from .routers_users import (
    router as users_router,
)

from .routers_paper import (
    router as paper_router,
)

from .web_users import (
    router as web_users_router,
)

from .web_servers import (
    router as web_servers_router,
)

from .web_players import (
    router as web_players_router,
)

from .web_plugins import (
    router as web_plugins_router,
)

from .web_files import (
    router as web_files_router,
)

from .web_backups import (
    router as web_backups_router,
)

from .web_logs import (
    router as web_logs_router,
)

from .web_properties import (
    router as web_properties_router,
)

from .web_settings import (
    router as web_settings_router,
)
from .web_roles import router as web_roles_router
from .web_automation import router as web_automation_router
from .web_notifications import router as web_notifications_router
from .automation import start_automation, stop_automation
from .backup_jobs import fail_abandoned_backup_jobs

from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from .system_operation import current_operation

from .web import router as web_router

app = FastAPI(
    title="Craftarr Server Console",
    version=APP_VERSION,
)

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
    )
    return response


@app.middleware("http")
async def system_operation_lock(request, call_next):
    operation = current_operation()
    if operation and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        return JSONResponse(
            {
                "error": "A system update or restart is in progress",
                "operation": operation,
            },
            status_code=423,
        )
    return await call_next(request)


@app.middleware("http")
async def force_password_change(request, call_next):
    allowed_paths = {
        "/change-password", "/logout", "/login", "/login/tfa",
        "/forgot-password", "/reset-password",
    }
    user_id = request.session.get("user_id")
    if user_id and request.url.path not in allowed_paths and not request.url.path.startswith("/static/"):
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            must_change = bool(user and user.enabled and user.must_change_password)
        finally:
            db.close()
        if must_change:
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    {"error": "Change your temporary password before continuing"},
                    status_code=403,
                )
            return RedirectResponse("/change-password", status_code=303)
    return await call_next(request)


# This must wrap the function middleware above so request.session is populated
# before forced-password enforcement runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="strict",
    https_only=COOKIE_SECURE,
)

app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)

app.include_router(web_router)

app.include_router(
    auth_router
)

app.include_router(
    users_router
)

app.include_router(
    servers_router
)

app.include_router(
    paper_router
)

app.include_router(
    web_users_router
)

app.include_router(
    web_servers_router
)

app.include_router(
    web_players_router
)

app.include_router(
    web_plugins_router
)

app.include_router(
    web_files_router
)

app.include_router(
    web_backups_router
)

app.include_router(
    web_logs_router
)

app.include_router(
    web_properties_router
)

app.include_router(
    web_settings_router
)

app.include_router(web_roles_router)

app.include_router(web_automation_router)

app.include_router(web_notifications_router)

@app.on_event("startup")
def startup():
    upgrade_database()
    db = SessionLocal()
    try:
        from .models import Server
        from .java_runtime import discover_java_runtimes, reconcile_java_path
        fail_abandoned_backup_jobs(db)
        servers = db.query(Server).all()
        runtimes = discover_java_runtimes()
        for server in servers:
            selected = reconcile_java_path(
                server.java_path, runtimes, server.minecraft_version,
            )
            if selected:
                server.java_path = selected
        db.commit()
        for server in servers:
            register_server(server)
    finally:
        db.close()
    start_automation()


@app.on_event("shutdown")
def shutdown():
    stop_automation()


@app.get("/")
def root():
    return RedirectResponse(
        "/dashboard",
        status_code=302,
    )

@app.get("/health")
def health():

    return {
        "status": "ok"
    }

@app.get("/api/version")
def version():
    return {
        "version": APP_VERSION,
    }
