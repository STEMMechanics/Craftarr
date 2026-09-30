from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from sqlalchemy.orm import Session

from .database import get_db

from .player_manager import (
    ban_player,
    deop_player,
    get_player_data,
    kick_player,
    op_player,
    pardon_player,
    remove_whitelist,
    set_whitelist_enabled,
    whitelist_player,
    ban_ip,
    pardon_ip,
)

from .web_context import (
    build_web_context,
)

from .web_render import (
    render_page,
)

from .web_servers import (
    current_web_user,
    get_accessible_server,
)
from .permissions import has_permission


router = APIRouter()


@router.get(
    "/servers/{server_id}/players",
    response_class=HTMLResponse,
)
def players_page(
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

    if not server or not has_permission(user, "players.view"):
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
        "page_title": "Players",
        "active_page": "players",
    })

    return render_page(
        request,
        "server_players.html",
        "partials/server_players.html",
        context,
    )


def required_reason(data: dict) -> tuple[str | None, str | None]:
    raw_reason = data.get("reason")
    if not isinstance(raw_reason, str):
        return None, "A reason is required"
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_reason):
        return None, "Reason cannot contain control characters"
    # Collapse whitespace, including newlines, before passing a ban reason to
    # the Minecraft console command.
    reason = " ".join(raw_reason.split())
    if not reason:
        return None, "A reason is required"
    if len(reason) > 500:
        return None, "Reason cannot exceed 500 characters"
    return reason, None


@router.get(
    "/api/web/servers/{server_id}/players"
)
def players_data(
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

    if not server:
        return JSONResponse(
            {
                "error":
                    "Access denied"
            },
            status_code=403,
        )

    if not server or not has_permission(user, "players.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    return get_player_data(
        server
    )


@router.post(
    "/api/web/servers/{server_id}/whitelist-enabled"
)
async def whitelist_enabled(
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

    if not server or not has_permission(user, "players.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()

    enabled = bool(
        data.get(
            "enabled"
        )
    )

    try:

        set_whitelist_enabled(
            server,
            enabled,
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_action = "Whitelist enabled" if enabled else "Whitelist disabled"

    return {
        "success": True,
        "enabled": enabled,
    }


@router.post(
    "/api/web/servers/{server_id}/players/action"
)
async def player_action(
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

    if not server or not has_permission(user, "players.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()

    player = (
        data.get(
            "player",
            ""
        )
        .strip()
    )

    action = (
        data.get(
            "action",
            ""
        )
        .strip()
    )


    if not player:

        return JSONResponse(
            {"error": "Player required"},
            status_code=400,
        )


    actions = {
        "whitelist":
            whitelist_player,

        "unwhitelist":
            remove_whitelist,

        "op":
            op_player,

        "deop":
            deop_player,

        "kick":
            kick_player,

        "ban":
            ban_player,

        "pardon":
            pardon_player,
    }


    handler = actions.get(
        action
    )

    if not handler:

        return JSONResponse(
            {"error": "Invalid action"},
            status_code=400,
        )

    reason = None
    if action in {"ban", "pardon"}:
        reason, reason_error = required_reason(data)
        if reason_error:
            return JSONResponse({"error": reason_error}, status_code=400)
    action_labels = {
        "whitelist": "Player whitelisted",
        "unwhitelist": "Player removed from whitelist",
        "op": "Player made OP",
        "deop": "Player removed as OP",
        "kick": "Player kicked",
        "ban": "Player banned",
        "pardon": "Player unbanned",
    }


    try:
        if action == "ban":
            handler(server, player, reason)
        else:
            handler(server, player)

    except RuntimeError as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_action = action_labels[action]
    request.state.audit_details = player
    request.state.audit_reason = reason


    return {
        "success": True
    }


@router.post("/api/web/servers/{server_id}/ip-bans/action")
async def ip_ban_action(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "players.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    data = await request.json()
    action = str(data.get("action", "")).strip()
    address = str(data.get("ip", "")).strip()
    handler = {"ban": ban_ip, "pardon": pardon_ip}.get(action)
    if not handler:
        return JSONResponse({"error": "Invalid action"}, status_code=400)
    reason = None
    if action in {"ban", "pardon"}:
        reason, reason_error = required_reason(data)
        if reason_error:
            return JSONResponse({"error": reason_error}, status_code=400)
    try:
        if action == "ban":
            handler(server, address, reason)
        else:
            handler(server, address)
    except RuntimeError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    request.state.audit_action = (
        "IP address banned" if action == "ban" else "IP address unbanned"
    )
    request.state.audit_details = address
    request.state.audit_reason = reason
    return {"success": True}
