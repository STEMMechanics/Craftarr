import json
from .env import getenv

from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from sqlalchemy.orm import Session

from .auth import hash_password
from .database import get_db
from .models import AccessRole, RemoteNode, Server, User
from .permissions import PERMISSION_GROUPS, has_permission
from .web_context import build_web_context
from .web_render import render_page
from .web_users import current_web_user

from .emailer import send_email
from .settings_manager import (
    DEFAULT_LOGIN_MESSAGE,
    get_login_message,
    get_smtp_settings,
    save_login_message,
    save_smtp_settings,
    get_system_alert_settings,
    save_system_alert_settings,
    get_instance_settings,
    save_instance_settings,
)

from .tfa import (
    generate_recovery_codes,
    generate_totp_secret,
    provisioning_uri,
    qr_code_data_uri,
    verify_totp,
)

from .auth import (
    hash_password,
    verify_password,
)

from .models import (
    RecoveryCode,
    Server,
    User,
)
from .server_access import apply_server_assignments, list_server_choices

from .update_manager import (
    get_latest_release,
    install_release,
    rollback_release,
)
from .service_restart import console_restart_unavailable_reason, schedule_console_restart
from .system_operation import (
    begin_operation,
    clear_operation,
    current_operation,
    update_operation,
)
from .offsite_backups import (
    OffsiteBackupError, configured_remotes, delete_remote, destination_from_parts, managed_config_path,
    remote_settings, save_remote, test_destination,
)

router = APIRouter()


@router.get('/api/web/settings/plugin-monitoring-repository')
def get_plugin_monitoring_repository(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if not has_permission(admin, 'settings.manage'):
        return JSONResponse({'error': 'Admin required'}, status_code=403)
    from .monitoring_defaults import read_repository_text, repository_path
    try:
        content = read_repository_text()
    except ValueError as error:
        return JSONResponse({'error': str(error)}, status_code=400)
    using_bundled = not repository_path().exists() and not getenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', '').strip()
    return {'content': content, 'using_bundled_defaults': using_bundled}


@router.get('/api/web/settings/plugin-monitoring-repository/download')
def download_plugin_monitoring_repository(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if not has_permission(admin, 'settings.manage'):
        return JSONResponse({'error': 'Admin required'}, status_code=403)
    from .monitoring_defaults import read_repository_text
    try:
        content = read_repository_text()
    except ValueError as error:
        return JSONResponse({'error': str(error)}, status_code=400)
    return PlainTextResponse(content, headers={'Content-Disposition': 'attachment; filename="plugin-monitoring.yml"'})


@router.post('/api/web/settings/plugin-monitoring-repository/reset')
def reset_plugin_monitoring_repository(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if not has_permission(admin, 'settings.manage'):
        return JSONResponse({'error': 'Admin required'}, status_code=403)

    from .monitoring_defaults import bundled_defaults_path, save_repository_text, validate_repository_text
    try:
        content = bundled_defaults_path().read_text(encoding='utf-8')
        validate_repository_text(content)
    except OSError:
        return JSONResponse({'error': 'Unable to read the bundled plugin settings for this release'}, status_code=500)
    except ValueError as error:
        return JSONResponse({'error': str(error)}, status_code=400)

    from datetime import datetime
    from .models import UpdateMonitorLease
    from .update_monitor import acquire_lease, CheckInProgress
    try:
        acquire_lease(db, datetime.utcnow())
    except CheckInProgress as error:
        return JSONResponse({'error': str(error)}, status_code=409)
    try:
        result = save_repository_text(content)
        from .remote_nodes import synchronize_plugin_monitoring_repositories
        sync_result = synchronize_plugin_monitoring_repositories(db)
        result.update(
            content=content,
            linked_nodes=sync_result['linked_nodes'],
            sync_errors=sync_result['failed_nodes'],
        )
        return result
    except (ValueError, TypeError) as error:
        return JSONResponse({'error': str(error)}, status_code=400)
    finally:
        db.rollback()
        db.query(UpdateMonitorLease).filter_by(id=1).update({'expires_at': datetime.min})
        db.commit()


async def _save_plugin_monitoring_repository(request: Request, db: Session):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({'error': 'Not authenticated'}, status_code=401)
    if not has_permission(admin, 'settings.manage'):
        return JSONResponse({'error': 'Admin required'}, status_code=403)
    try:
        from .monitoring_defaults import MAX_REPOSITORY_BYTES, validate_repository_text
        raw = await request.body()
        if len(raw) > MAX_REPOSITORY_BYTES * 2 + 1024:
            return JSONResponse({'error': 'The shared plugin settings file must be 256 KiB or smaller'}, status_code=413)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Invalid plugin settings document')
        content = data.get('content')
        validate_repository_text(content)
    except (ValueError, TypeError) as error:
        return JSONResponse({'error': str(error)}, status_code=400)
    from .models import UpdateMonitorLease
    from .monitoring_defaults import save_repository_text
    from .update_monitor import acquire_lease, CheckInProgress
    from datetime import datetime
    try:
        acquire_lease(db, datetime.utcnow())
    except CheckInProgress as error:
        return JSONResponse({'error': str(error)}, status_code=409)
    try:
        result = save_repository_text(content)
        from .remote_nodes import synchronize_plugin_monitoring_repositories
        sync_result = synchronize_plugin_monitoring_repositories(db)
        result["linked_nodes"] = sync_result["linked_nodes"]
        result["sync_errors"] = sync_result["failed_nodes"]
        return result
    except (ValueError, TypeError) as error:
        return JSONResponse({'error': str(error)}, status_code=400)
    finally:
        db.rollback()
        db.query(UpdateMonitorLease).filter_by(id=1).update({'expires_at': datetime.min})
        db.commit()


@router.put('/api/web/settings/plugin-monitoring-repository')
async def save_plugin_monitoring_repository(request: Request, db: Session = Depends(get_db)):
    return await _save_plugin_monitoring_repository(request, db)


@router.post('/api/web/settings/plugin-monitoring-repository/import')
async def import_plugin_monitoring_repository(request: Request, db: Session = Depends(get_db)):
    return await _save_plugin_monitoring_repository(request, db)


@router.get("/api/web/settings/system-alerts")
def system_alert_settings(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    return get_system_alert_settings(db)


@router.post("/api/web/settings/system-alerts")
async def update_system_alert_settings(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        return save_system_alert_settings(db, await request.json())
    except (TypeError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@router.get("/api/web/settings/offsite-backups")
def offsite_backup_settings(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        return {"available": True, "remotes": configured_remotes(refresh=True),
                "destinations": remote_settings(), "config": str(managed_config_path())}
    except OffsiteBackupError as error:
        message = str(error)
        return {
            "available": False,
            "remotes": [],
            "destinations": [],
            "reason": "not_installed" if "not installed" in message.lower() else "unavailable",
            "error": message,
        }


@router.post("/api/web/settings/offsite-backups/test")
async def test_offsite_backup_settings(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    data = await request.json()
    try:
        if "remote" in data:
            destination = destination_from_parts(
                str(data.get("remote", "")), str(data.get("path", "")),
            )
        else:
            destination = str(data.get("destination", ""))
        test_destination(destination)
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"success": True}


@router.post("/api/web/settings/offsite-backups/remotes")
async def save_offsite_remote(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        remote = save_remote(await request.json())
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return {"success": True, "remote": remote}


@router.delete("/api/web/settings/offsite-backups/remotes/{name}")
def remove_offsite_remote(name: str, request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        delete_remote(name)
    except OffsiteBackupError as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {"success": True}


def _proxy_linked_offsite_settings(db: Session, node_id: str, path: str, *, method="GET", payload=None, timeout=8.0):
    from .node_security import decrypt_remote_token
    from .remote_nodes import RemoteNodeError, _request

    node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
    if not node:
        raise RemoteNodeError("Linked Node not found")
    token = decrypt_remote_token(node.token_ciphertext)
    return _request(node.base_url, token, path, method=method, payload=payload, timeout=timeout)


@router.get("/api/web/settings/nodes/{node_id}/offsite-backups")
def linked_offsite_backup_settings(node_id: str, request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        return _proxy_linked_offsite_settings(db, node_id, "/api/node/offsite-backups")
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.post("/api/web/settings/nodes/{node_id}/offsite-backups/test")
async def test_linked_offsite_backup(node_id: str, request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "Enter a valid destination"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Enter a valid destination"}, status_code=400)
    try:
        return _proxy_linked_offsite_settings(
            db, node_id, "/api/node/offsite-backups/test", method="POST", payload=payload, timeout=30.0,
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.post("/api/web/settings/nodes/{node_id}/offsite-backups/remotes")
async def save_linked_offsite_remote(node_id: str, request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "Enter valid destination settings"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Enter valid destination settings"}, status_code=400)
    try:
        return _proxy_linked_offsite_settings(
            db, node_id, "/api/node/offsite-backups/remotes", method="POST", payload=payload,
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.delete("/api/web/settings/nodes/{node_id}/offsite-backups/remotes/{name}")
def delete_linked_offsite_remote(node_id: str, name: str, request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    from urllib.parse import quote
    try:
        return _proxy_linked_offsite_settings(
            db, node_id, f"/api/node/offsite-backups/remotes/{quote(name, safe='')}", method="DELETE",
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=502)


@router.get(
    "/settings",
    response_class=HTMLResponse,
)
def settings_page(
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

    context = build_web_context(
        db,
        user,
    )

    database_path = Path(
        getenv(
            "CRAFTARR_CONSOLE_DATABASE",
            "craftarr.db",
        )
    ).expanduser()


    server_root = Path(
        getenv(
            "CRAFTARR_CONSOLE_SERVER_ROOT",
            "minecraft-servers",
        )
    ).expanduser()


    console_host = getenv(
        "CRAFTARR_CONSOLE_HOST",
        "127.0.0.1",
    )


    console_port = getenv(
        "CRAFTARR_CONSOLE_PORT",
        "8000",
    )

    context.update({
        "page_title": "Settings",
        "active_page": "settings",

        "database_path":
            str(database_path),

        "server_root":
            str(server_root),

        "console_host":
            console_host,

        "console_port":
            console_port,

        "login_message":
            get_login_message(db),
    })

    if has_permission(user, "users.manage"):

        context["users"] = (
            db.query(User)
            .order_by(User.username)
            .all()
        )

        context["servers"] = list_server_choices(db)

    if has_permission(user, "roles.manage"):
        context["permission_groups"] = PERMISSION_GROUPS

    if has_permission(user, "users.manage") or has_permission(user, "roles.manage"):
        context["roles"] = (
            db.query(AccessRole)
            .order_by(AccessRole.name)
            .all()
        )

    return render_page(
        request,
        "settings.html",
        "partials/settings.html",
        context,
    )


@router.post("/api/web/settings/login-message")
async def update_login_message(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(request, db)

    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)

    data = await request.json()
    message = str(data.get("login_message", "")).strip()

    if len(message) > 300:
        return JSONResponse(
            {"error": "Login message must be 300 characters or fewer"},
            status_code=400,
        )

    return {
        "success": True,
        "login_message": save_login_message(
            db,
            message or DEFAULT_LOGIN_MESSAGE,
        ),
    }


@router.post(
    "/api/web/settings/profile"
)
async def update_profile(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    data = await request.json()

    username = (
        data.get(
            "username",
            ""
        )
        .strip()
    )

    email = (
        data.get(
            "email",
            ""
        )
        .strip()
    )

    password = (
        data.get(
            "password",
            ""
        )
    )

    if len(username) < 3:
        return JSONResponse(
            {
                "error":
                    "Username must be at least 3 characters."
            },
            status_code=400,
        )

    existing = (
        db.query(User)
        .filter(
            User.username == username,
            User.id != user.id,
        )
        .first()
    )

    if existing:
        return JSONResponse(
            {
                "error":
                    "Username already exists."
            },
            status_code=409,
        )

    user.username = username

    if email:

        existing_email = (
            db.query(User)
            .filter(
                User.email == email,
                User.id != user.id,
            )
            .first()
        )

        if existing_email:

            return JSONResponse(
                {
                    "error":
                        "Email address is already in use."
                },
                status_code=409,
            )

        user.email = email

    else:

        user.email = None

    if password:

        if len(password) < 8:
            return JSONResponse(
                {
                    "error":
                        "Password must be at least 8 characters."
                },
                status_code=400,
            )

        user.password_hash = hash_password(
            password
        )
        user.must_change_password = False

    db.commit()

    return {
        "success": True
    }


@router.get(
    "/api/web/settings/users/{user_id}"
)
def get_user_settings(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "users.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return JSONResponse(
            {"error": "User not found"},
            status_code=404,
        )

    return {
        "id": user.id,
        "username": user.username,
        "role_id": user.role_id,
        "role": user.role_name,
        "server_access_all": has_permission(user, "servers.view_all"),
        "enabled": user.enabled,
        "must_change_password":
            user.must_change_password,

        "servers": [
            *[server.id for server in user.servers],
            *[server.server_ref for server in user.remote_servers],
        ],
    }


@router.post(
    "/api/web/settings/users"
)
async def create_user_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "users.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    data = await request.json()

    username = (
        data.get(
            "username",
            ""
        )
        .strip()
    )

    password = (
        data.get(
            "password",
            ""
        )
    )

    try:
        role_id = int(data.get("role_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Select a role"}, status_code=400)

    access_role = db.get(AccessRole, role_id)
    if not access_role:
        return JSONResponse({"error": "Invalid role"}, status_code=400)

    must_change_password = bool(
        data.get(
            "must_change_password",
            True,
        )
    )

    if len(username) < 3:
        return JSONResponse(
            {"error": "Invalid username"},
            status_code=400,
        )

    if len(password) < 8:
        return JSONResponse(
            {
                "error":
                    "Password must be at least 8 characters."
            },
            status_code=400,
        )

    existing = (
        db.query(User)
        .filter(
            User.username == username
        )
        .first()
    )

    if existing:
        return JSONResponse(
            {
                "error":
                    "Username already exists."
            },
            status_code=409,
        )

    user = User(
        username=username,
        password_hash=hash_password(
            password
        ),
        role="admin" if access_role.name == "Administrator" else "user",
        role_id=access_role.id,
        enabled=True,
        must_change_password=must_change_password,
    )

    db.add(user)
    db.flush()

    if any(permission.key == "servers.view_all" for permission in access_role.permissions):
        user.servers.clear()
        user.remote_servers.clear()
    else:
        try:
            apply_server_assignments(db, user, data.get("servers", []))
        except ValueError as error:
            db.rollback()
            return JSONResponse({"error": str(error)}, status_code=400)

    db.commit()

    return {
        "success": True,
        "user_id": user.id,
    }


@router.post(
    "/api/web/settings/users/{user_id}"
)
async def update_user_settings(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "users.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return JSONResponse(
            {"error": "User not found"},
            status_code=404,
        )

    data = await request.json()

    username = (
        data.get(
            "username",
            user.username,
        )
        .strip()
    )

    try:
        role_id = int(data.get("role_id", user.role_id))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Select a role"}, status_code=400)

    access_role = db.get(AccessRole, role_id)
    if not access_role:
        return JSONResponse({"error": "Invalid role"}, status_code=400)

    enabled = bool(
        data.get(
            "enabled",
            True,
        )
    )

    must_change_password = bool(
        data.get(
            "must_change_password",
            user.must_change_password,
        )
    )

    if (
        user.id == admin.id
        and role_id != user.role_id
    ):
        return JSONResponse(
            {
                "error":
                    "You cannot change your own role."
            },
            status_code=400,
        )

    existing = (
        db.query(User)
        .filter(
            User.username == username,
            User.id != user.id,
        )
        .first()
    )

    if existing:
        return JSONResponse(
            {
                "error":
                    "Username already exists."
            },
            status_code=409,
        )

    user.username = username
    user.role_id = access_role.id
    user.role = "admin" if access_role.name == "Administrator" else "user"
    user.enabled = enabled
    user.must_change_password = (
        must_change_password
    )

    password = data.get(
        "password",
        "",
    )

    if password:

        if len(password) < 8:
            return JSONResponse(
                {
                    "error":
                        "Password must be at least 8 characters."
                },
                status_code=400,
            )

        user.password_hash = hash_password(
            password
        )

    if any(permission.key == "servers.view_all" for permission in access_role.permissions):
        user.servers.clear()
        user.remote_servers.clear()
    else:
        try:
            apply_server_assignments(db, user, data.get("servers", []))
        except ValueError as error:
            db.rollback()
            return JSONResponse({"error": str(error)}, status_code=400)

    db.commit()

    return {
        "success": True
    }


@router.delete(
    "/api/web/settings/users/{user_id}"
)
def delete_user_settings(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "users.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    user = db.get(
        User,
        user_id,
    )

    if not user:
        return JSONResponse(
            {"error": "User not found"},
            status_code=404,
        )

    if user.id == admin.id:
        return JSONResponse(
            {
                "error":
                    "You cannot delete yourself."
            },
            status_code=400,
        )

    db.delete(user)
    db.commit()

    return {
        "success": True
    }

@router.get(
    "/api/web/settings/smtp"
)
def get_smtp_settings_api(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "settings.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    settings = get_smtp_settings(
        db
    )

    # Don't send the saved password back
    # to the browser.
    settings["smtp_password"] = ""

    return settings


@router.get("/api/web/settings/instance")
def get_instance_settings_api(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    return get_instance_settings(db)


@router.post("/api/web/settings/instance")
async def save_instance_settings_api(request: Request, db: Session = Depends(get_db)):
    admin = current_web_user(request, db)
    if not admin:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(admin, "settings.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid instance settings")
        return save_instance_settings(db, data)
    except (ValueError, TypeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@router.post(
    "/api/web/settings/smtp"
)
async def save_smtp_settings_api(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "settings.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    data = await request.json()

    existing = get_smtp_settings(
        db
    )

    # Blank password means keep the current one.
    if not data.get("smtp_password"):
        data["smtp_password"] = (
            existing["smtp_password"]
        )

    security = data.get(
        "smtp_security",
        "starttls",
    )

    if security not in (
        "starttls",
        "ssl",
        "none",
    ):
        return JSONResponse(
            {"error": "Invalid SMTP security mode"},
            status_code=400,
        )

    try:
        port = int(
            data.get(
                "smtp_port",
                587,
            )
        )

        if not (
            1 <= port <= 65535
        ):
            raise ValueError()

    except ValueError:

        return JSONResponse(
            {"error": "Invalid SMTP port"},
            status_code=400,
        )

    data["smtp_port"] = str(
        port
    )

    save_smtp_settings(
        db,
        data,
    )

    return {
        "success": True
    }


@router.post(
    "/api/web/settings/smtp/test"
)
async def test_smtp_settings(
    request: Request,
    db: Session = Depends(get_db),
):
    admin = current_web_user(
        request,
        db,
    )

    if not admin:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(admin, "settings.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    if not admin.email:

        return JSONResponse(
            {
                "error":
                    "Add an email address to your profile first."
            },
            status_code=400,
        )

    try:

        send_email(
            db,
            admin.email,
            "Craftarr SMTP Test",
            (
                "Your Craftarr SMTP settings "
                "are working correctly."
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    return {
        "success": True
    }

@router.get(
    "/api/web/settings/tfa"
)
def tfa_status(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    return {
        "enabled":
            user.totp_enabled,
    }

@router.post(
    "/api/web/settings/tfa/setup"
)
def tfa_setup(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if user.totp_enabled:

        return JSONResponse(
            {"error": "2FA is already enabled"},
            status_code=400,
        )


    secret = generate_totp_secret()

    user.totp_secret = secret

    db.commit()


    uri = provisioning_uri(
        user,
        secret,
    )


    return {
        "secret": secret,

        "qr_code":
            qr_code_data_uri(
                uri
            ),
    }

@router.post(
    "/api/web/settings/tfa/confirm"
)
async def tfa_confirm(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )


    data = await request.json()

    code = (
        data.get(
            "code",
            ""
        )
    )


    if not user.totp_secret:

        return JSONResponse(
            {"error": "2FA setup has not been started"},
            status_code=400,
        )


    if not verify_totp(
        user.totp_secret,
        code,
    ):

        return JSONResponse(
            {"error": "Invalid authentication code"},
            status_code=400,
        )


    user.totp_enabled = True

    db.commit()


    recovery_codes = (
        generate_recovery_codes(
            db,
            user,
        )
    )


    return {
        "success": True,

        "recovery_codes":
            recovery_codes,
    }

@router.post(
    "/api/web/settings/tfa/disable"
)
async def tfa_disable(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )


    data = await request.json()

    password = (
        data.get(
            "password",
            ""
        )
    )


    if not verify_password(
        password,
        user.password_hash,
    ):

        return JSONResponse(
            {"error": "Incorrect password"},
            status_code=400,
        )


    user.totp_enabled = False
    user.totp_secret = None

    db.query(
        RecoveryCode
    ).filter(
        RecoveryCode.user_id
        == user.id
    ).delete()

    db.commit()


    return {
        "success": True
    }

@router.post(
    "/api/web/settings/tfa/recovery-codes"
)
async def regenerate_recovery_codes(
    request: Request,
    db: Session = Depends(get_db),
):
    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not user.totp_enabled:

        return JSONResponse(
            {"error": "2FA is not enabled"},
            status_code=400,
        )


    codes = generate_recovery_codes(
        db,
        user,
    )


    return {
        "recovery_codes":
            codes,
    }

@router.get(
    "/api/web/settings/update"
)
def update_status(
    request: Request,
    db: Session = Depends(get_db),
):

    user = current_web_user(
        request,
        db,
    )

    if not user:
        return JSONResponse(
            {"error": "Not authenticated"},
            status_code=401,
        )

    if not has_permission(user, "system.manage"):
        return JSONResponse(
            {"error": "Admin required"},
            status_code=403,
        )

    try:

        return get_latest_release()

    except Exception as error:

        return JSONResponse(
            {
                "error":
                    "Unable to check GitHub releases",

                "detail":
                    str(error),
            },
            status_code=502,
        )


@router.post("/api/web/settings/update")
async def install_update(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "system.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    unavailable_reason = console_restart_unavailable_reason()
    if unavailable_reason:
        return JSONResponse({"error": unavailable_reason}, status_code=409)
    data = await request.json()
    rollback = data.get("action") == "rollback"
    operation = begin_operation(
        "rollback" if rollback else "update",
        "Rolling back Craftarr" if rollback else "Updating Craftarr",
        "Restoring the verified application backup." if rollback else "Downloading and verifying the selected release.",
        "installing",
    )
    if operation is None:
        return JSONResponse(
            {"error": "A system update or restart is already in progress", "operation": current_operation()},
            status_code=423,
        )
    try:
        if rollback:
            result = rollback_release(str(data.get("rollback_id", "")))
        else:
            result = install_release(str(data.get("tag", "")))
        update_operation(
            title="Restarting Craftarr",
            message="Waiting for the console service to return.",
            phase="restarting",
        )
        schedule_console_restart()
        result["restarting"] = True
        return result
    except Exception as error:
        clear_operation()
        return JSONResponse({"error": str(error)}, status_code=400)


@router.post("/api/web/settings/restart")
def restart_console(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not has_permission(user, "system.manage"):
        return JSONResponse({"error": "Admin required"}, status_code=403)
    unavailable_reason = console_restart_unavailable_reason()
    if unavailable_reason:
        return JSONResponse({"error": unavailable_reason}, status_code=409)
    operation = begin_operation(
        "restart",
        "Restarting Craftarr",
        "The console service is restarting. Waiting for it to return.",
        "restarting",
    )
    if operation is None:
        return JSONResponse(
            {"error": "A system update or restart is already in progress", "operation": current_operation()},
            status_code=423,
        )
    try:
        schedule_console_restart()
        return {"restarting": True}
    except Exception as error:
        clear_operation()
        return JSONResponse({"error": str(error)}, status_code=400)


@router.get("/api/web/settings/system-operation")
def system_operation_status(request: Request, db: Session = Depends(get_db)):
    user = current_web_user(request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return current_operation() or {"active": False}
