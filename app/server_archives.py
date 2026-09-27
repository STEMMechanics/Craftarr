import json
import re
import stat
import zipfile
import zlib

from pathlib import Path, PurePosixPath

from .processes import build_java_command, normalize_memory, resolve_server_jar


SETTINGS_EXPORT_FILENAME = "craftarr-server-settings.json"
SETTINGS_EXPORT_FORMAT = "craftarr-server-settings"
SETTINGS_EXPORT_VERSION = 1
MAX_SETTINGS_EXPORT_BYTES = 1024 * 1024
MAX_SERVER_ARCHIVE_ENTRIES = 100_000

PORTABLE_SETTING_KEYS = {
    "minecraft_version",
    "paper_build",
    "memory",
    "min_memory",
    "jar_name",
    "java_args",
    "stop_commands",
}


def server_settings_document(server) -> dict:
    """Return only panel-managed settings that can move between hosts."""
    return {
        "format": SETTINGS_EXPORT_FORMAT,
        "version": SETTINGS_EXPORT_VERSION,
        "settings": {
            "minecraft_version": server.minecraft_version,
            "paper_build": server.paper_build,
            "memory": server.memory,
            "min_memory": server.min_memory,
            "jar_name": server.jar_name,
            "java_args": server.java_args,
            "stop_commands": server.stop_commands,
        },
    }


def parse_settings_document(value: bytes | str | dict) -> dict:
    if isinstance(value, bytes):
        if len(value) > MAX_SETTINGS_EXPORT_BYTES:
            raise ValueError("The settings export file is too large")
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("The settings export file must be UTF-8 JSON") from error
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SETTINGS_EXPORT_BYTES:
            raise ValueError("The settings export file is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("The settings export file is not valid JSON") from error

    if not isinstance(value, dict):
        raise ValueError("The settings export must be a JSON object")
    if value.get("format") != SETTINGS_EXPORT_FORMAT:
        raise ValueError("This is not a Craftarr server settings export")
    version = value.get("version")
    if type(version) is not int or version != SETTINGS_EXPORT_VERSION:
        raise ValueError("This Craftarr settings export version is not supported")

    settings = value.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("The settings export is missing its settings object")
    unknown = set(settings) - PORTABLE_SETTING_KEYS
    if unknown:
        raise ValueError("The settings export contains unsupported fields")
    if not settings:
        raise ValueError("The settings export does not contain any settings")

    for key in ("memory", "min_memory", "jar_name", "java_args", "stop_commands"):
        if key in settings and not isinstance(settings[key], str):
            raise ValueError(f"The exported {key.replace('_', ' ')} setting must be text")
    for key in ("minecraft_version", "paper_build"):
        if key in settings and settings[key] is not None and not isinstance(settings[key], str):
            raise ValueError(f"The exported {key.replace('_', ' ')} setting must be text or null")

    return settings


def validate_portable_settings(
    settings: dict,
    server_directory: str | Path,
    *,
    defaults: dict | None = None,
) -> dict:
    """Merge, normalize, and validate portable settings against a server folder."""
    unknown = set(settings) - PORTABLE_SETTING_KEYS
    if unknown:
        raise ValueError("The settings export contains unsupported fields")

    values = {
        "minecraft_version": None,
        "paper_build": None,
        "memory": "4G",
        "min_memory": "4G",
        "jar_name": "paper.jar",
        "java_args": "",
        "stop_commands": "",
    }
    if defaults:
        values.update(defaults)
    values.update(settings)

    values["memory"] = normalize_memory(values["memory"], "Maximum RAM")
    values["min_memory"] = normalize_memory(values["min_memory"], "Initial RAM")
    values["jar_name"] = str(values["jar_name"]).strip()
    if len(values["jar_name"]) > 255:
        raise ValueError("The selected JAR filename is too long")
    resolve_server_jar(server_directory, values["jar_name"])

    values["java_args"] = str(values["java_args"])
    if len(values["java_args"]) > 1000:
        raise ValueError("Java startup options cannot exceed 1000 characters")
    build_java_command(
        values["memory"], values["jar_name"], values["java_args"],
        values["min_memory"], "java",
    )

    values["stop_commands"] = str(values["stop_commands"])
    if (
        len(values["stop_commands"]) > 4000
        or len([line for line in values["stop_commands"].splitlines() if line.strip()]) > 20
    ):
        raise ValueError("Use no more than 20 pre-stop commands and 4000 characters")

    for key in ("minecraft_version", "paper_build"):
        value = values[key]
        if value is not None and (not isinstance(value, str) or len(value) > 40):
            raise ValueError(f"The {key.replace('_', ' ')} setting is invalid")

    return values


def extract_server_archive(
    archive_path: str | Path,
    destination: str | Path,
    *,
    max_uncompressed_bytes: int,
) -> dict:
    """Safely extract the single Minecraft server in an archive and return settings."""
    archive_path = Path(archive_path)
    destination = Path(destination)
    if not zipfile.is_zipfile(archive_path):
        raise ValueError("Upload a valid ZIP archive")

    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("Unable to read the uploaded ZIP archive") from error

    with archive:
        infos = archive.infolist()
        if not infos:
            raise ValueError("The ZIP archive is empty")
        if len(infos) > MAX_SERVER_ARCHIVE_ENTRIES:
            raise ValueError("The ZIP archive contains too many files")

        members = []
        seen_paths = set()
        total_size = 0
        for info in infos:
            name = info.filename
            if not name or "\x00" in name or "\\" in name:
                raise ValueError("The ZIP archive contains an invalid path")
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError("The ZIP archive contains an absolute path")
            member = PurePosixPath(name)
            if ".." in member.parts:
                raise ValueError("The ZIP archive contains an unsafe path")
            if not member.parts:
                continue

            normalized = member.as_posix().rstrip("/")
            if normalized in seen_paths:
                raise ValueError("The ZIP archive contains duplicate paths")
            seen_paths.add(normalized)

            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("The ZIP archive contains a symbolic link or special file")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted ZIP files are not supported")
            if info.file_size < 0 or info.file_size > max_uncompressed_bytes:
                raise ValueError("The ZIP archive expands beyond the upload size limit")
            total_size += info.file_size
            if total_size > max_uncompressed_bytes:
                raise ValueError("The ZIP archive expands beyond the upload size limit")
            members.append((info, member))

        jar_directories = {
            member.parent.parts
            for info, member in members
            if not info.is_dir() and member.suffix.lower() == ".jar"
        }
        candidates = {
            member.parent.parts
            for info, member in members
            if not info.is_dir()
            and member.name == "server.properties"
            and member.parent.parts in jar_directories
        }
        if not candidates:
            raise ValueError(
                "The ZIP must contain server.properties and a server JAR in the same folder"
            )
        if len(candidates) != 1:
            raise ValueError("The ZIP contains more than one possible Minecraft server")
        server_parts = next(iter(candidates))

        settings_members = [
            (info, member)
            for info, member in members
            if not info.is_dir()
            and member.name in {SETTINGS_EXPORT_FILENAME, *LEGACY_SETTINGS_EXPORT_FILENAMES}
            and member.parent.parts in {(), server_parts}
        ]
        if len(settings_members) > 1:
            raise ValueError("The ZIP contains more than one Craftarr settings export")

        settings = None
        if settings_members:
            settings_info, _ = settings_members[0]
            if settings_info.file_size > MAX_SETTINGS_EXPORT_BYTES:
                raise ValueError("The embedded settings export file is too large")
            try:
                with archive.open(settings_info, "r") as source:
                    settings_content = source.read(MAX_SETTINGS_EXPORT_BYTES + 1)
                    if len(settings_content) > MAX_SETTINGS_EXPORT_BYTES or source.read(1):
                        raise ValueError("The embedded settings export file is too large")
                settings = parse_settings_document(settings_content)
            except ValueError:
                raise
            except (OSError, EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error) as error:
                raise ValueError("Unable to read the embedded settings export") from error

        destination.mkdir(parents=True, exist_ok=False)
        root = destination.resolve()
        extracted_size = 0
        for info, member in members:
            if server_parts:
                if member.parts[:len(server_parts)] != server_parts:
                    continue
                relative_parts = member.parts[len(server_parts):]
            else:
                relative_parts = member.parts
            if not relative_parts:
                continue
            if (
                relative_parts[-1] in {SETTINGS_EXPORT_FILENAME, *LEGACY_SETTINGS_EXPORT_FILENAMES}
                and (member.parent.parts == server_parts)
            ):
                continue

            target = root.joinpath(*relative_parts)
            try:
                target.resolve().relative_to(root)
            except (OSError, ValueError):
                raise ValueError("The ZIP archive contains an unsafe path") from None

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                try:
                    with archive.open(info, "r") as source:
                        while chunk := source.read(1024 * 1024):
                            extracted_size += len(chunk)
                            if extracted_size > max_uncompressed_bytes:
                                raise ValueError("The ZIP archive expands beyond the upload size limit")
                            output.write(chunk)
                except (EOFError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zlib.error) as error:
                    raise ValueError("The ZIP archive contains corrupt or unsupported data") from error

    return {"settings": settings, "server_directory": destination}
