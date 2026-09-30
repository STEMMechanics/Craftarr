from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import get_db
from .models import ServerAuditEvent
from .permissions import has_permission
from .web_context import build_web_context
from .web_render import render_page
from .web_servers import get_accessible_server


router = APIRouter()
AUDIT_PAGE_SIZE = 50


@router.get("/servers/{server_id}/audit", response_class=HTMLResponse)
def server_audit_page(
    server_id: int,
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "servers.view"):
        raise HTTPException(status_code=403, detail="Access denied")

    query = db.query(ServerAuditEvent).filter(ServerAuditEvent.server_id == server.id)
    total_events = query.with_entities(func.count(ServerAuditEvent.id)).scalar() or 0
    total_pages = max(1, (total_events + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(max(page, 1), total_pages)
    events = (
        query.order_by(ServerAuditEvent.created_at.desc(), ServerAuditEvent.id.desc())
        .offset((page - 1) * AUDIT_PAGE_SIZE)
        .limit(AUDIT_PAGE_SIZE)
        .all()
    )

    context = build_web_context(db, user, active_server=server)
    context.update({
        "server": server,
        "page_title": "Audit",
        "active_page": "audit",
        "events": events,
        "audit_page": page,
        "audit_total_pages": total_pages,
        "audit_total_events": total_events,
    })
    return render_page(request, "server_audit.html", "partials/server_audit.html", context)
