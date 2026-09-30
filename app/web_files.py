from pathlib import Path
import tempfile
import zipfile
from urllib.parse import quote
from starlette.background import BackgroundTask

from .file_manager import (
    create_file,
    create_folder,
    create_zip,
    delete_entry,
    extract_zip,
    list_directory,
    move_entry,
    read_text_file,
    rename_entry,
    safe_path,
    write_text_file,
    yaml_sanity_warning,
)
from .config import MAX_UPLOAD_BYTES

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from sqlalchemy.orm import Session

from .database import get_db

from .web_context import (
    build_web_context,
)

from .web_render import (
    render_page,
)

from .web_servers import (
    get_accessible_server,
)
from .permissions import has_permission


router = APIRouter()


@router.post("/api/web/servers/{server_id}/files/yaml-check")
async def check_yaml(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "files.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    data = await request.json()
    return {
        "success": True,
        "warning": yaml_sanity_warning(str(data.get("content", ""))),
    }


@router.post("/api/web/servers/{server_id}/files/create")
async def create_server_file(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "files.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    data = await request.json()
    try:
        path = create_file(server, str(data.get("path", "")), str(data.get("name", "")))
    except FileExistsError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except (OSError, ValueError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    request.state.audit_action = "Server file created"
    request.state.audit_details = path
    return {"success": True, "path": path}


@router.get(
    "/servers/{server_id}/files",
    response_class=HTMLResponse,
)
def files_page(
    server_id: int,
    request: Request,
    path: str = "",
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

    if not server or not has_permission(user, "files.view"):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )


    try:
        data = list_directory(
            server,
            path,
        )

    except (
        ValueError,
        FileNotFoundError,
    ) as error:

        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


    context = build_web_context(
        db,
        user,
        active_server=server,
    )

    context.update({
        "server": server,
        "page_title": "Files",
        "active_page": "files",
        **data,
    })


    return render_page(
        request,
        "server_files.html",
        "partials/server_files.html",
        context,
    )


@router.post(
    "/servers/{server_id}/files/upload"
)
async def upload_file(
    server_id: int,
    request: Request,

    path: str = Form(
        default=""
    ),

    file: UploadFile = File(...),
    relative_path: str = Form(default=""),
    replace: bool = Form(default=False),

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

    if not server or not has_permission(user, "files.manage"):
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )


    directory = safe_path(
        server,
        path,
    )


    upload_name = relative_path or file.filename or ""
    relative_parts = Path(upload_name).parts
    if not relative_parts or Path(upload_name).is_absolute() or ".." in relative_parts:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filename = Path(upload_name).name

    if not filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename",
        )


    destination = directory.joinpath(*relative_parts)
    try:
        destination.resolve().relative_to(directory.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise HTTPException(
            status_code=409,
            detail="A file blocks the uploaded folder path",
        )

    if destination.exists() and destination.is_dir():
        raise HTTPException(status_code=409, detail="A folder with that name already exists")
    if destination.exists() and not replace:
        raise HTTPException(status_code=409, detail="A file with that name already exists")


    written = 0
    temporary = None
    try:
        temporary_file = tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".upload",
            dir=destination.parent,
            delete=False,
        )
        temporary = Path(temporary_file.name)
        with temporary_file as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Uploaded file is too large",
                    )
                output.write(chunk)
        temporary.replace(destination)
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail="A file with that name already exists",
        )
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


    request.state.audit_action = "Server file uploaded"
    request.state.audit_details = str(Path(path) / Path(*relative_parts))
    return RedirectResponse(
        (
            f"/servers/{server_id}/files"
            f"?path={path}"
        ),
        status_code=303,
    )


@router.get(
    "/servers/{server_id}/files/preview"
)
def preview_png_file(
    server_id: int,
    request: Request,
    path: str,
    db: Session = Depends(get_db),
):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        raise HTTPException(status_code=401)
    if not server or not has_permission(user, "files.view"):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        target = safe_path(server, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if target.suffix.casefold() != ".png" or not target.is_file():
        raise HTTPException(status_code=404, detail="PNG file not found")
    try:
        with target.open("rb") as image_file:
            signature = image_file.read(8)
    except OSError:
        raise HTTPException(status_code=404, detail="PNG file not found")
    if signature != b"\x89PNG\r\n\x1a\n":
        raise HTTPException(status_code=415, detail="File is not a valid PNG image")

    return FileResponse(
        target,
        media_type="image/png",
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/servers/{server_id}/files/download"
)
def download_file(
    server_id: int,
    request: Request,
    path: str,
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
        raise HTTPException(
            status_code=401
        )

    if not server or not has_permission(user, "files.view"):
        raise HTTPException(
            status_code=403
        )


    target = safe_path(
        server,
        path,
    )

    if not target.is_file() and not target.is_dir():

        raise HTTPException(
            status_code=404,
            detail="File or folder not found",
        )


    if target.is_file():
        return FileResponse(target, filename=target.name)

    temporary = tempfile.NamedTemporaryFile(
        prefix="craftarr-folder-", suffix=".zip", delete=False,
    )
    temporary.close()
    archive_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in target.rglob("*"):
                if item.is_symlink() or not item.is_file():
                    continue
                archive.write(item, item.relative_to(target.parent))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path,
        filename=f"{target.name}.zip",
        media_type="application/zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@router.get(
    "/servers/{server_id}/files/edit",
    response_class=HTMLResponse,
)
def edit_file_page(
    server_id: int,
    request: Request,
    path: str,
    confirm_unknown: bool = False,
    yaml_warning: str = "",
    warning_line: int = 1,
    warning_column: int = 1,
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

    if not server or not has_permission(user, "files.manage"):
        raise HTTPException(
            status_code=403
        )


    try:
        contents = read_text_file(
            server,
            path,
            allow_unknown=confirm_unknown,
        )

    except (
        ValueError,
        FileNotFoundError,
    ) as error:

        raise HTTPException(
            status_code=400,
            detail=str(error),
        )


    context = build_web_context(
        db,
        user,
        active_server=server,
    )

    context.update({
        "server": server,
        "page_title": "Edit File",
        "file_path": path,
        "filename":
            Path(path).name,
        "contents": contents,
        "parent_path": "" if str(Path(path).parent) == "." else str(Path(path).parent),
        "confirm_unknown": confirm_unknown,
        "yaml_warning": yaml_warning,
        "warning_line": warning_line,
        "warning_column": warning_column,
    })


    return render_page(
        request,
        "file_edit.html",
        "partials/file_edit.html",
        context,
    )


@router.post(
    "/servers/{server_id}/files/edit"
)
def save_file(
    server_id: int,
    request: Request,

    path: str = Form(),
    contents: str = Form(),
    close: bool = Form(default=False),
    confirm_unknown: bool = Form(default=False),

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

    if not server or not has_permission(user, "files.manage"):
        raise HTTPException(
            status_code=403
        )


    warning = write_text_file(
        server,
        path,
        contents,
        allow_unknown=confirm_unknown,
    )


    parent = str(
        Path(path).parent
    )

    if parent == ".":
        parent = ""


    warning_query = ""
    if warning:
        warning_query = (
            f"&yaml_warning={quote(warning['message'])}"
            f"&warning_line={warning['line']}"
            f"&warning_column={warning['column']}"
        )

    request.state.audit_action = "Server file saved"
    request.state.audit_details = path
    destination = (
        f"/servers/{server_id}/files?path={quote(parent)}"
        if close and not warning else
        f"/servers/{server_id}/files/edit?path={quote(path)}&saved=true"
        f"{'&confirm_unknown=true' if confirm_unknown else ''}"
        f"{warning_query}"
    )
    return RedirectResponse(destination, status_code=303)


@router.post(
    "/api/web/servers/{server_id}/files/mkdir"
)
async def mkdir(
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

    if not server or not has_permission(user, "files.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )


    data = await request.json()


    try:

        create_folder(
            server,
            data.get(
                "path",
                "",
            ),
            data.get(
                "name",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_action = "Server folder created"
    request.state.audit_details = "/".join(
        part for part in (str(data.get("path", "")).strip("/"), str(data.get("name", "")).strip("/")) if part
    )

    return {
        "success": True
    }


@router.post("/api/web/servers/{server_id}/files/zip")
async def zip_entry(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "files.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    data = await request.json()
    try:
        archive_path = create_zip(server, data.get("path", ""))
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    request.state.audit_action = "Server files archived"
    request.state.audit_details = archive_path
    return {"success": True, "path": archive_path}


@router.post("/api/web/servers/{server_id}/files/extract")
async def extract_entry(server_id: int, request: Request, db: Session = Depends(get_db)):
    user, server = get_accessible_server(server_id, request, db)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not server or not has_permission(user, "files.manage"):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    data = await request.json()
    try:
        conflicts = extract_zip(
            server,
            data.get("path", ""),
            data.get("mode", "check"),
            MAX_UPLOAD_BYTES,
        )
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    request.state.audit_action = "Server archive extracted"
    request.state.audit_details = str(data.get("path", ""))
    return {"success": True, "conflicts": conflicts}


@router.post(
    "/api/web/servers/{server_id}/files/rename"
)
async def rename(
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

    if not server or not has_permission(user, "files.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )


    data = await request.json()


    try:

        rename_entry(
            server,
            data.get(
                "path",
                "",
            ),
            data.get(
                "name",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_action = "Server file renamed"
    request.state.audit_details = f"{data.get('path', '')} → {data.get('name', '')}"

    return {
        "success": True
    }


@router.post(
    "/api/web/servers/{server_id}/files/delete"
)
async def delete(
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

    if not server or not has_permission(user, "files.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )


    data = await request.json()


    try:

        delete_entry(
            server,
            data.get(
                "path",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )

    request.state.audit_action = "Server file deleted"
    request.state.audit_details = str(data.get("path", ""))

    return {
        "success": True
    }

@router.post(
    "/api/web/servers/{server_id}/files/move"
)
async def move(
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

    if not server or not has_permission(user, "files.manage"):
        return JSONResponse(
            {"error": "Access denied"},
            status_code=403,
        )

    data = await request.json()

    try:
        move_entry(
            server,
            data.get(
                "source",
                "",
            ),
            data.get(
                "destination",
                "",
            ),
        )

    except Exception as error:

        return JSONResponse(
            {"error": str(error)},
            status_code=400,
        )
    request.state.audit_action = "Server file moved"
    request.state.audit_details = f"{data.get('source', '')} → {data.get('destination', '')}"

    return {
        "success": True
    }
