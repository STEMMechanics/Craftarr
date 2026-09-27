import shutil
import stat
import zipfile
import yaml
from datetime import datetime

from pathlib import Path


EDITABLE_EXTENSIONS = {
    ".txt",
    ".properties",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
    ".conf",
    ".cfg",
    ".ini",
    ".md",
    ".log",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".map",
    ".xml",
    ".csv",
    ".tsv",
    ".sql",
    ".sh",
    ".bat",
}

TEXT_SAMPLE_BYTES = 8192


def server_root(server) -> Path:
    return Path(
        server.directory
    ).resolve()


def safe_path(
    server,
    relative_path: str = "",
) -> Path:

    root = server_root(server)

    requested = (
        root / relative_path
    ).resolve()

    try:
        requested.relative_to(root)

    except ValueError:
        raise ValueError(
            "Invalid path"
        )

    return requested


def relative_path(
    server,
    path: Path,
) -> str:

    return str(
        path.resolve().relative_to(
            server_root(server)
        )
    )


def format_size(
    size: int,
) -> str:

    if size < 1024:
        return f"{size} Bytes"

    if size < 1024 * 1024:
        return (
            f"{size / 1024:.1f} KB"
        )

    if size < 1024 * 1024 * 1024:
        return (
            f"{size / 1024 / 1024:.1f} MB"
        )

    return (
        f"{size / 1024 / 1024 / 1024:.1f} GB"
    )


def list_directory(
    server,
    relative: str = "",
):

    directory = safe_path(
        server,
        relative,
    )

    if not directory.exists():
        raise FileNotFoundError(
            "Directory not found"
        )

    if not directory.is_dir():
        raise ValueError(
            "Not a directory"
        )

    entries = []

    for item in directory.iterdir():

        try:
            stat = item.stat()

        except OSError:
            continue


        is_file = item.is_file()
        known_text = is_file and item.suffix.lower() in EDITABLE_EXTENSIONS

        entries.append({
            "name":
                item.name,

            "path":
                relative_path(
                    server,
                    item,
                ),

            "is_dir":
                item.is_dir(),

            "modified":
                datetime.fromtimestamp(
                    stat.st_mtime
                ),

            "size":
                (
                    None
                    if item.is_dir()
                    else stat.st_size
                ),

            "size_display":
                (
                    ""
                    if item.is_dir()
                    else format_size(
                        stat.st_size
                    )
                ),

            "editable":
                known_text,

            "text_candidate":
                is_file and (known_text or is_text_file(item)),

            "is_zip":
                is_file and item.suffix.lower() == ".zip",
        })


    entries.sort(
        key=lambda item: (
            not item["is_dir"],
            item["name"].lower(),
        )
    )


    parent = None

    root = server_root(server)

    if directory != root:

        parent = relative_path(
            server,
            directory.parent,
        )


    return {
        "entries": entries,
        "current_path":
            relative_path(
                server,
                directory,
            ),

        "parent_path":
            parent,
    }


def read_text_file(
    server,
    relative: str,
    allow_unknown: bool = False,
):

    path = safe_path(
        server,
        relative,
    )

    if not path.is_file():
        raise FileNotFoundError(
            "File not found"
        )

    if not is_text_file(path):
        raise ValueError(
            "This appears to be a binary file and cannot be edited as text"
        )

    if path.suffix.lower() not in EDITABLE_EXTENSIONS and not allow_unknown:
        raise ValueError(
            "Confirm that you want to open this unknown file type as text"
        )

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def write_text_file(
    server,
    relative: str,
    contents: str,
    allow_unknown: bool = False,
):

    path = safe_path(
        server,
        relative,
    )

    if not path.is_file():
        raise FileNotFoundError(
            "File not found"
        )

    if not is_text_file(path):
        raise ValueError(
            "This appears to be a binary file and cannot be edited as text"
        )

    if path.suffix.lower() not in EDITABLE_EXTENSIONS and not allow_unknown:
        raise ValueError(
            "Confirm that you want to edit this unknown file type as text"
        )

    path.write_text(
        contents,
        encoding="utf-8",
    )

    if path.suffix.lower() in {".yml", ".yaml"}:
        return yaml_sanity_warning(contents)
    return None


def yaml_sanity_warning(contents: str):
    try:
        document = yaml.safe_load(contents)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        return {
            "message": f"Potential YAML error: {getattr(error, 'problem', None) or 'invalid YAML'}",
            "line": mark.line + 1 if mark else 1,
            "column": mark.column + 1 if mark else 1,
        }
    if document is not None and not isinstance(document, (dict, list)):
        return {
            "message": "Potential YAML error: expected a settings object or list",
            "line": 1,
            "column": 1,
        }
    return None


def is_text_file(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            sample = file.read(TEXT_SAMPLE_BYTES)
    except OSError:
        return False

    if b"\x00" in sample:
        return False

    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False

    return True


def next_zip_path(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.zip")
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}-{number}.zip")
        number += 1
    return candidate


def create_zip(server, relative: str) -> str:
    source = safe_path(server, relative)
    if not source.exists():
        raise FileNotFoundError("File or folder not found")
    if source == server_root(server):
        raise ValueError("Cannot zip the server root")

    destination = next_zip_path(source)
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        if source.is_file():
            archive.write(source, source.name)
        else:
            for item in source.rglob("*"):
                if item.is_symlink():
                    continue
                archive_name = item.relative_to(source.parent)
                if item.is_dir():
                    archive.writestr(f"{archive_name}/", b"")
                elif item.is_file():
                    archive.write(item, archive_name)
    return relative_path(server, destination)


def _safe_zip_members(archive: zipfile.ZipFile, max_bytes: int):
    members = []
    total = 0
    for info in archive.infolist():
        member = Path(info.filename)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError("ZIP contains an unsafe path")
        if not member.parts:
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError("ZIP contains a symbolic link")
        total += info.file_size
        if total > max_bytes:
            raise ValueError("Extracted ZIP is too large")
        members.append((info, member))
    return members


def extract_zip(server, relative: str, mode: str, max_bytes: int):
    source = safe_path(server, relative)
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ValueError("A ZIP file is required")
    if mode not in {"check", "merge", "replace"}:
        raise ValueError("Invalid extraction mode")

    destination = source.parent
    with zipfile.ZipFile(source) as archive:
        members = _safe_zip_members(archive, max_bytes)
        for info, member in members:
            target = destination.joinpath(*member.parts)
            target.resolve().relative_to(destination.resolve())
            if target.resolve() == source.resolve():
                raise ValueError("ZIP cannot overwrite itself while extracting")
            if mode == "merge" and info.is_dir() and target.exists() and not target.is_dir():
                raise ValueError(f"Cannot replace file with folder: {member}")
            if mode == "merge" and not info.is_dir() and target.exists() and target.is_dir():
                raise ValueError(f"Cannot replace folder with file: {member}")

        corrupt_member = archive.testzip()
        if corrupt_member:
            raise ValueError(f"ZIP contains a corrupt file: {corrupt_member}")

        top_levels = {member.parts[0] for _, member in members}
        conflicts = sorted(name for name in top_levels if (destination / name).exists())
        if mode == "check":
            return conflicts
        if mode == "replace":
            for name in conflicts:
                target = destination / name
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()

        for info, member in members:
            target = destination.joinpath(*member.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as input_file, target.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
    return conflicts


def create_folder(
    server,
    parent: str,
    name: str,
):

    directory = safe_path(
        server,
        parent,
    )

    safe_name = Path(
        name
    ).name.strip()

    if not safe_name:
        raise ValueError(
            "Folder name required"
        )

    target = (
        directory / safe_name
    )

    target.mkdir(
        exist_ok=False
    )


def create_file(
    server,
    parent: str,
    name: str,
) -> str:
    """Create an empty file under an existing server directory."""
    directory = safe_path(server, parent)
    if not directory.is_dir():
        raise FileNotFoundError("Folder not found")

    safe_name = str(name or "").strip()
    if (
        not safe_name
        or safe_name in {".", ".."}
        or len(safe_name) > 255
        or Path(safe_name).name != safe_name
        or "/" in safe_name
        or "\\" in safe_name
        or any(ord(character) < 32 for character in safe_name)
    ):
        raise ValueError("Enter a valid file name")

    target = safe_path(server, str(Path(parent) / safe_name))
    try:
        with target.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        raise FileExistsError("A file or folder with that name already exists")
    return relative_path(server, target)


def rename_entry(
    server,
    relative: str,
    new_name: str,
):

    path = safe_path(
        server,
        relative,
    )

    if not path.exists():
        raise FileNotFoundError(
            "File not found"
        )

    safe_name = Path(
        new_name
    ).name.strip()

    if not safe_name:
        raise ValueError(
            "Name required"
        )

    destination = (
        path.parent / safe_name
    )

    if destination.exists():
        raise FileExistsError(
            "Destination already exists"
        )

    path.rename(
        destination
    )


def delete_entry(
    server,
    relative: str,
):

    path = safe_path(
        server,
        relative,
    )

    root = server_root(server)

    if path == root:
        raise ValueError(
            "Cannot delete server root"
        )

    if path.is_dir():

        shutil.rmtree(
            path
        )

    elif path.is_file():

        path.unlink()

    else:

        raise FileNotFoundError(
            "File not found"
        )

def move_entry(
    server,
    source_relative: str,
    destination_relative: str,
):
    source = safe_path(
        server,
        source_relative,
    )

    destination_directory = safe_path(
        server,
        destination_relative,
    )

    if not source.exists():
        raise FileNotFoundError(
            "Source not found"
        )

    if not destination_directory.is_dir():
        raise ValueError(
            "Destination is not a directory"
        )

    target = (
        destination_directory
        / source.name
    )

    if target.exists():
        raise FileExistsError(
            "Destination already exists"
        )

    # Prevent moving a folder into itself
    # or one of its own descendants.
    if source.is_dir():

        try:
            destination_directory.relative_to(
                source
            )

            raise ValueError(
                "Cannot move a folder into itself"
            )

        except ValueError as error:

            if str(error) == (
                "Cannot move a folder into itself"
            ):
                raise

    source.rename(
        target
    )
