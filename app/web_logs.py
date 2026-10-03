from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from .database import get_db
from .permissions import has_permission
from .server_logs import list_server_logs, read_latest_log, read_server_log, safe_log_path
from .web_context import build_web_context
from .web_render import render_page
from .web_servers import get_accessible_server


router = APIRouter()
LOGS_PER_PAGE = 5


def paginated_logs(server, page: int) -> tuple[list[dict], int, int]:
    logs = list_server_logs(server)
    total_pages = max(1, (len(logs) + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE)
    page = min(max(page, 1), total_pages)
    start = (page - 1) * LOGS_PER_PAGE
    return logs[start:start + LOGS_PER_PAGE], page, total_pages


@router.get("/api/web/servers/{server_id}/logs")
def logs_data(
    server_id: int,
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "console.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    logs, page, total_pages = paginated_logs(server, page)
    return {
        "logs": [
            {
                **log,
                "modified_display": log["modified"].strftime("%Y-%m-%d %H:%M:%S"),
            }
            for log in logs
        ],
        "page": page,
        "total_pages": total_pages,
        "total_logs": len(list_server_logs(server)),
    }


@router.get("/api/web/servers/{server_id}/logs/manifest")
def logs_manifest_data(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "console.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    return {"files": [log["name"] for log in list_server_logs(server)]}


@router.get("/api/web/servers/{server_id}/logs/latest")
def latest_log_data(
    server_id: int,
    request: Request,
    size: int | None = None,
    modified_ns: int | None = None,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "console.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    try:
        path = safe_log_path(server, "latest.log")
        stat = path.stat()
        if size == stat.st_size and modified_ns == stat.st_mtime_ns:
            return {
                "changed": False,
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
            }
        content, truncated = read_latest_log(server)
    except (OSError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=404)
    return {
        "changed": True,
        "content": content,
        "truncated": truncated,
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


@router.get("/servers/{server_id}/logs", response_class=HTMLResponse)
def logs_page(
    server_id: int,
    request: Request,
    file: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return RedirectResponse("/login")
    if not server or not has_permission(user, "console.view"):
        raise HTTPException(status_code=403, detail="Access denied")

    all_logs = list_server_logs(server)
    logs, page, total_pages = paginated_logs(server, page)
    selected = file or ("latest.log" if any(item["name"] == "latest.log" for item in all_logs) else None)
    if selected is None and all_logs:
        selected = all_logs[0]["name"]
    content = None
    truncated = False
    if selected:
        try:
            content, truncated = read_server_log(server, selected)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    context = build_web_context(db, user, active_server=server)
    context.update({
        "server": server,
        "page_title": "Logs",
        "active_page": "logs",
        "logs": logs,
        "total_logs": len(all_logs),
        "logs_page": page,
        "logs_total_pages": total_pages,
        "selected_log": selected,
        "log_content": content,
        "log_truncated": truncated,
    })
    return render_page(
        request,
        "server_logs.html",
        "partials/server_logs.html",
        context,
    )


@router.get("/servers/{server_id}/logs/download")
def download_log(
    server_id: int,
    file: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not server or not has_permission(user, "console.view"):
        raise HTTPException(status_code=403, detail="Access denied")
    try:
        path = safe_log_path(server, file)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return FileResponse(path, filename=path.name)


def _stream_spooled_file(file):
    try:
        while chunk := file.read(64 * 1024):
            yield chunk
    finally:
        file.close()


@router.post("/api/web/servers/{server_id}/logs/download")
async def download_selected_logs(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "console.view"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "Select log files to download."}, status_code=400)
    requested = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(requested, list) or not requested:
        return JSONResponse({"error": "Select log files to download."}, status_code=400)
    if len(requested) > 5000 or any(not isinstance(name, str) for name in requested):
        return JSONResponse({"error": "Select up to 5000 log files."}, status_code=400)

    available = {item["name"] for item in list_server_logs(server)}
    selected = list(dict.fromkeys(requested))
    if any(name not in available for name in selected):
        return JSONResponse({"error": "A selected log file is no longer available. Refresh the list and try again."}, status_code=404)

    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
            for name in selected:
                try:
                    path = safe_log_path(server, name)
                except ValueError as error:
                    raise HTTPException(status_code=404, detail=str(error)) from error
                zip_file.write(path, arcname=path.name)
        archive.seek(0)
    except Exception:
        archive.close()
        raise

    return StreamingResponse(
        _stream_spooled_file(archive),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="server-logs.zip"'},
    )
