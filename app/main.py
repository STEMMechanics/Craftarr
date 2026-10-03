import logging

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

from .database import SessionLocal
from .models import Server, User
from .audit import (
    describe_request_action,
    record_audit_event,
    server_id_from_request,
)

from .routers_auth import (
    router as auth_router,
)

from .routers_instance import router as instance_router
from .routers_node_api import router as node_api_router

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
from .web_audit import router as web_audit_router

from .web_properties import (
    router as web_properties_router,
)

from .web_settings import (
    router as web_settings_router,
)
from .web_roles import router as web_roles_router
from .web_automation import router as web_automation_router
from .web_notifications import router as web_notifications_router
from .web_nodes import router as nodes_router
from .automation import start_automation, stop_automation
from .backup_jobs import fail_abandoned_backup_jobs

from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from .system_operation import current_operation

from .web import router as web_router
from .node_gateway import maybe_proxy_remote_server
from .node_security import verify_node_token

app = FastAPI(
    title="Craftarr Server Console",
    version=APP_VERSION,
)

@app.middleware("http")
async def security_headers(request, call_next):
    response = await maybe_proxy_remote_server(request)
    if response is None:
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


@app.middleware("http")
async def record_server_audit_events(request, call_next):
    method = request.method.upper()
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return await call_next(request)

    server_id = server_id_from_request(request)
    if server_id is None:
        return await call_next(request)

    path = request.url.path
    if path.endswith((
        "/files/yaml-check",
        "/plugins/check-updates",
        "/plugins/check-update",
        "/plugins/monitoring/preview",
        "/paper/check-updates",
    )):
        return await call_next(request)

    actor_id = request.session.get("user_id")
    forwarded_actor = request.headers.get("x-craftarr-actor", "").strip()
    authorization_scheme, _, authorization_token = request.headers.get("authorization", "").partition(" ")
    trusted_forwarded_actor = bool(
        forwarded_actor
        and authorization_scheme.lower() == "bearer"
    )

    server_name = f"Server {server_id}"
    actor_username = None
    db = SessionLocal()
    try:
        if trusted_forwarded_actor:
            trusted_forwarded_actor = verify_node_token(db, authorization_token)
        server = db.get(Server, server_id)
        if server:
            server_name = server.name
        if actor_id:
            actor = db.get(User, actor_id)
            if actor and actor.enabled:
                actor_username = actor.username
        elif trusted_forwarded_actor:
            actor_username = forwarded_actor[:64]
    except Exception:
        pass
    finally:
        db.close()

    if not actor_username:
        return await call_next(request)

    response = await call_next(request)
    if getattr(request.state, "skip_audit", False):
        return response
    successful_response = 200 <= response.status_code < 300
    if 300 <= response.status_code < 400 and getattr(request.state, "audit_action", None):
        successful_response = True
    if not successful_response or not actor_username:
        return response

    try:
        db = SessionLocal()
        record_audit_event(
            db,
            server_id=server_id,
            server_name=server_name,
            actor_user_id=actor_id,
            actor_username=actor_username,
            action=describe_request_action(request),
            details=getattr(request.state, "audit_details", None),
            reason=getattr(request.state, "audit_reason", None),
        )
    except Exception:
        # Audit failures must not turn a completed server action into a failed
        # response. Keep the original action outcome intact.
        logging.getLogger(__name__).exception("Unable to record server audit event")
    finally:
        if "db" in locals():
            db.close()
    return response


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

app.include_router(instance_router)
app.include_router(node_api_router)

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

app.include_router(web_audit_router)

app.include_router(
    web_properties_router
)

app.include_router(
    web_settings_router
)

app.include_router(web_roles_router)

app.include_router(web_automation_router)

app.include_router(web_notifications_router)
app.include_router(nodes_router)

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
