import math
import shutil
import zipfile

from datetime import datetime
from pathlib import Path


BACKUP_SPACE_MIN_BUFFER_BYTES = 256 * 1024 * 1024
BACKUP_SPACE_BUFFER_RATIO = 0.05


class InsufficientBackupSpaceError(RuntimeError):
    """Raised when there is not enough free disk space for a backup."""


def _backup_source_files(root: Path):
    backups = root / "backups"

    for path in root.rglob("*"):
        # Never include the backups directory itself, including older archives.
        if path == backups or backups in path.parents:
            continue

        if path.is_file():
            yield path


def _backup_source_size(root: Path) -> int:
    total_bytes = 0

    for path in _backup_source_files(root):
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue

    return total_bytes


def check_backup_space(
    server,
    source_bytes: int | None = None,
    storage_path: Path | None = None,
) -> dict:
    """Check for room to store a conservative, uncompressed-size backup."""
    root = Path(server.directory).resolve()

    if source_bytes is None:
        source_bytes = _backup_source_size(root)

    buffer_bytes = max(
        BACKUP_SPACE_MIN_BUFFER_BYTES,
        math.ceil(source_bytes * BACKUP_SPACE_BUFFER_RATIO),
    )
    required_bytes = source_bytes + buffer_bytes

    if storage_path is None:
        candidate = root / "backups"
        storage_path = candidate if candidate.exists() else root

    try:
        available_bytes = shutil.disk_usage(storage_path).free
    except OSError as error:
        raise RuntimeError(
            f"Could not check free disk space for the backup destination: {error}"
        ) from error

    if available_bytes < required_bytes:
        raise InsufficientBackupSpaceError(
            "Not enough free disk space for this backup. "
            f"The server files use about {format_size(source_bytes)}; "
            f"at least {format_size(required_bytes)} is needed, including "
            f"a {format_size(buffer_bytes)} safety buffer, but only "
            f"{format_size(available_bytes)} is available."
        )

    return {
        "source_bytes": source_bytes,
        "buffer_bytes": buffer_bytes,
        "required_bytes": required_bytes,
        "available_bytes": available_bytes,
    }


def backup_directory(server) -> Path:
    directory = (
        Path(server.directory)
        / "backups"
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def safe_backup_path(
    server,
    filename: str,
) -> Path:

    directory = (
        backup_directory(server)
        .resolve()
    )

    path = (
        directory
        / Path(filename).name
    ).resolve()

    if path.parent != directory:
        raise ValueError(
            "Invalid backup path"
        )

    if path.suffix.lower() != ".zip":
        raise ValueError(
            "Invalid backup file"
        )

    return path


def format_size(
    size: int,
) -> str:

    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    if size < 1024 * 1024 * 1024:
        return (
            f"{size / 1024 / 1024:.1f} MB"
        )

    return (
        f"{size / 1024 / 1024 / 1024:.1f} GB"
    )


def list_backups(server) -> list[dict]:

    directory = backup_directory(
        server
    )

    backups = []

    for path in directory.glob(
        "*.zip"
    ):

        try:
            stat = path.stat()

        except OSError:
            continue

        created = datetime.fromtimestamp(
            stat.st_mtime
        )

        backups.append({
            "filename":
                path.name,

            "size":
                stat.st_size,

            "size_display":
                format_size(
                    stat.st_size
                ),

            "created":
                created.isoformat(),

            "created_display":
                created.strftime(
                    "%d %b %Y, %I:%M %p"
                ),
        })

    backups.sort(
        key=lambda item:
            item["created"],
        reverse=True,
    )

    return backups


def create_backup(
    server,
    label: str | None = None,
    progress_callback=None,
) -> dict:

    root = Path(
        server.directory
    ).resolve()

    backups = backup_directory(server).resolve()

    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    safe_label = ""

    if label:

        safe_label = "".join(
            character
            for character in label.strip()
            if (
                character.isalnum()
                or character in (
                    "-",
                    "_",
                )
            )
        )

    filename = timestamp

    if safe_label:
        filename += (
            "-" + safe_label
        )

    filename += ".zip"

    destination = (
        backups / filename
    )
    temporary = destination.with_suffix(".zip.part")


    files = list(_backup_source_files(root))


    total_bytes = 0

    for path in files:

        try:
            total_bytes += (
                path.stat().st_size
            )

        except OSError:
            pass

    # Check again immediately before opening the archive. The first check in
    # the backup worker avoids pausing Minecraft when space is already low;
    # this one accounts for files saved or space consumed since that check.
    check_backup_space(
        server,
        source_bytes=total_bytes,
        storage_path=backups,
    )


    processed_bytes = 0

    last_progress = -1


    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
        ) as archive:

            for path in files:

                try:

                    size = path.stat().st_size

                    archive.write(path, path.relative_to(root))

                    processed_bytes += size


                except (FileNotFoundError, PermissionError):
                    continue


                if progress_callback:

                    if total_bytes > 0:

                        progress = int(processed_bytes / total_bytes * 100)

                    else:

                        progress = 100


                # Don't hammer SQLite with
                # identical progress updates.
                    if progress != last_progress:

                        progress_callback(progress)

                        last_progress = progress

        temporary.replace(destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


    if progress_callback:
        progress_callback(100)


    stat = destination.stat()


    return {
        "filename":
            destination.name,

        "size":
            stat.st_size,

        "size_display":
            format_size(
                stat.st_size
            ),
    }

def delete_backup(
    server,
    filename: str,
):

    path = safe_backup_path(
        server,
        filename,
    )

    if not path.exists():
        raise FileNotFoundError(
            "Backup not found"
        )

    path.unlink()


def restore_backup(
    server,
    filename: str,
):

    archive_path = safe_backup_path(
        server,
        filename,
    )

    if not archive_path.exists():
        raise FileNotFoundError(
            "Backup not found"
        )

    root = Path(
        server.directory
    ).resolve()

    backups = backup_directory(
        server
    ).resolve()

    temp = (
        root.parent
        / f".{root.name}-restore"
    )

    if temp.exists():
        shutil.rmtree(
            temp
        )

    temp.mkdir(
        parents=True
    )

    try:

        with zipfile.ZipFile(
            archive_path,
            "r",
        ) as archive:

            for member in archive.infolist():

                member_path = (
                    temp / member.filename
                ).resolve()

                try:
                    member_path.relative_to(
                        temp.resolve()
                    )

                except ValueError:
                    raise ValueError(
                        "Backup contains unsafe paths"
                    )

            archive.extractall(
                temp
            )


        for item in root.iterdir():

            if item.resolve() == backups:
                continue

            if item.is_dir():
                shutil.rmtree(
                    item
                )

            else:
                item.unlink()


        for item in temp.iterdir():

            destination = (
                root / item.name
            )

            shutil.move(
                str(item),
                str(destination),
            )

    finally:

        shutil.rmtree(
            temp,
            ignore_errors=True,
        )
