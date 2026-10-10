from email.header import decode_header, make_header
from email.message import Message
from email.utils import collapse_rfc2231_value
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

import regex
import yaml

from .env import getenv

from pathlib import Path


MAX_PLUGIN_BYTES = int(getenv("CRAFTARR_MAX_PLUGIN_BYTES", str(128 * 1024 * 1024)))
MAX_PLUGIN_METADATA_BYTES = 256 * 1024


class PluginFileExistsError(FileExistsError):
    def __init__(self, filename: str, enabled: bool):
        self.filename = filename
        self.enabled = enabled
        state = "enabled" if enabled else "disabled"
        super().__init__(f"{filename} already exists and is currently {state}")


def plugins_directory(server) -> Path:
    return Path(server.directory) / "plugins"


def read_plugin_yml(jar_path: Path, metadata_source: str = "auto") -> dict:
    """
    Read the small amount of metadata we need from a plugin descriptor.
    """

    if metadata_source not in {"auto", "plugin.yml", "paper-plugin.yml"}:
        return {}

    try:
        with zipfile.ZipFile(jar_path) as jar:
            names = set(jar.namelist())
            metadata_name = metadata_source
            if metadata_name == "auto":
                metadata_name = "plugin.yml" if "plugin.yml" in names else "paper-plugin.yml"
            if metadata_name not in names:
                return {}
            if jar.getinfo(metadata_name).file_size > MAX_PLUGIN_METADATA_BYTES:
                return {}
            with jar.open(metadata_name) as file:
                text = file.read(
                    MAX_PLUGIN_METADATA_BYTES + 1
                ).decode("utf-8", errors="ignore")

    except (
        OSError,
        KeyError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
    ):
        return {}


    try:
        metadata = yaml.load(text, Loader=yaml.BaseLoader)
    except (yaml.YAMLError, RecursionError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value for key in ("name", "version")
        if isinstance(value := metadata.get(key), str)
        and 0 < len(value) <= 200
        and not any(ord(character) < 32 for character in value)
    }


def plugin_config_directory(
    jar_path: Path,
) -> Path | None:

    metadata = read_plugin_yml(
        jar_path
    )

    name = metadata.get("name")

    if not name:
        return None

    # Prevent metadata from escaping plugins/
    safe_name = Path(name).name

    return (
        jar_path.parent
        / safe_name
    )


def _filename_plugin_version(
    filename: str,
    plugin_name: str | None,
) -> str | None:
    """Read a version suffix only when the JAR name matches the plugin name."""
    if not plugin_name:
        return None

    stem = Path(filename).stem
    if not stem.casefold().startswith(plugin_name.casefold()):
        return None

    suffix = stem[len(plugin_name):]
    match = re.fullmatch(
        r"[-_ ]+(v?\d[A-Za-z0-9.+_-]*)",
        suffix,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


_PLUGIN_BUILD_FILENAME = re.compile(
    r"(?i)(?:\(build\s+|[- ]build[ .-]*|[-+]b|-SNAPSHOT[-+]b?)(?P<build>\d+)(?:\)?(?:\+[a-z0-9]+)?)(?=\.jar$)"
)


def filename_for_plugin_build(filename: str, build: str) -> str | None:
    """Return a versioned JAR filename with its detected build number corrected."""
    build = str(build or "")
    if Path(filename).name != filename or not re.fullmatch(r"\d+", build):
        return None
    disabled = filename.endswith(".jar.disabled")
    display_filename = filename[:-9] if disabled else filename
    match = _PLUGIN_BUILD_FILENAME.search(display_filename)
    if not match or match.group("build") == build:
        return None
    corrected = (
        display_filename[:match.start("build")]
        + build
        + display_filename[match.end("build"):]
    )
    return corrected + (".disabled" if disabled else "")


def filename_for_installed_pattern(filename: str, pattern: str, version: str) -> str | None:
    """Replace the version captured from the installed JAR filename."""
    if (not isinstance(filename, str) or Path(filename).name != filename
            or not isinstance(pattern, str) or not pattern
            or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version)):
        return None
    disabled = filename.endswith(".jar.disabled")
    display_filename = filename[:-9] if disabled else filename
    try:
        compiled = regex.compile(pattern)
        if compiled.groups < 1:
            return None
        match = compiled.search(display_filename, timeout=0.1)
    except (regex.error, RecursionError, TimeoutError):
        return None
    if not match:
        return None
    group = "version" if "version" in compiled.groupindex else 1
    start, end = match.span(group)
    if start < 0 or end <= start or match.group(group) == version:
        return None
    corrected = display_filename[:start] + version + display_filename[end:]
    if Path(corrected).name != corrected or not corrected.lower().endswith(".jar"):
        return None
    return corrected + (".disabled" if disabled else "")


def plugin_info(
    path: Path,
) -> dict:

    file_stat = path.stat()

    disabled = (
        path.name.endswith(
            ".jar.disabled"
        )
    )

    metadata = read_plugin_yml(
        path
    )
    version_sources = {
        source: read_plugin_yml(path, source).get("version")
        for source in ("plugin.yml", "paper-plugin.yml")
    }

    filename = path.name

    if disabled:
        display_filename = (
            filename[:-9]
        )
    else:
        display_filename = filename


    name = metadata.get("name")

    if not name:
        name = re.sub(
            r"[-_ ]v?\d.*$",
            "",
            display_filename[:-4],
        )

    metadata_version = metadata.get("version")
    filename_version = _filename_plugin_version(
        display_filename,
        name,
    )
    version = metadata_version or filename_version
    if metadata_version:
        version_source = "metadata"
    elif filename_version:
        version_source = "filename"
    else:
        version_source = None


    config_dir = (
        plugin_config_directory(
            path
        )
    )

    config_files = []
    config_file_modified_ns = {}
    if config_dir and config_dir.is_dir():
        plugin_root = path.parent.resolve()
        for config_file in config_dir.rglob("*"):
            if not config_file.is_file() or config_file.suffix.lower() not in {".yml", ".yaml"}:
                continue
            try:
                resolved = config_file.resolve()
                resolved.relative_to(config_dir.resolve())
                relative = resolved.relative_to(plugin_root.parent)
                config_file_modified_ns[relative.as_posix()] = str(resolved.stat().st_mtime_ns)
            except (OSError, ValueError):
                continue
            config_files.append(relative.as_posix())


    return {
        "filename": filename,

        "previous_version": bool(re.search(r"\.(?:previous|rollback)-[0-9a-f]{12}\.jar\.disabled$", filename, re.IGNORECASE)),

        "name":
            name
            or display_filename,

        "version":
            version,

        "version_source":
            version_source,

        "version_sources":
            version_sources,

        "enabled":
            not disabled,

        "size":
            file_stat.st_size,

        "modified_ns":
            str(file_stat.st_mtime_ns),

        "config_directory":
            (
                config_dir.name
                if (
                    config_dir
                    and config_dir.is_dir()
                )
                else None
            ),

        "config_files": sorted(
            config_files,
            key=lambda config_path: (
                0 if Path(config_path).name.lower() == "config.yml" else 1,
                len(Path(config_path).parts) if Path(config_path).name.lower() == "config.yml" else 0,
                config_path.lower(),
            ),
        ),

        "config_file_modified_ns": config_file_modified_ns,
    }


def geyser_status(server, plugins: list[dict] | None = None) -> dict:
    plugins = plugins if plugins is not None else list_plugins(server)
    geyser = next((plugin for plugin in plugins if "geyser" in plugin["name"].casefold()), None)
    if not geyser:
        return {"installed": False, "enabled": False, "port": None}

    port = None
    config_directory = geyser.get("config_directory")
    if config_directory:
        config = plugins_directory(server) / config_directory / "config.yml"
        try:
            in_bedrock = False
            for raw_line in config.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(raw_line) - len(raw_line.lstrip())
                if indent == 0:
                    in_bedrock = stripped == "bedrock:"
                    continue
                if in_bedrock:
                    match = re.fullmatch(r"port:\s*(\d+)", stripped)
                    if match:
                        candidate = int(match.group(1))
                        if 1 <= candidate <= 65535:
                            port = candidate
                        break
        except OSError:
            pass

    return {"installed": True, "enabled": bool(geyser["enabled"]), "port": port}


def duplicate_plugin_groups(plugins: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    display_names: dict[str, str] = {}
    for plugin in plugins:
        if not plugin.get("enabled"):
            continue
        name = str(plugin.get("name") or "").strip()
        key = name.casefold()
        if not key:
            continue
        display_names.setdefault(key, name)
        item = {
            "filename": plugin["filename"],
            "version": plugin.get("version"),
        }
        if plugin.get("size") is not None:
            item["size"] = plugin["size"]
        if plugin.get("modified_ns") is not None:
            item["modified_ns"] = plugin["modified_ns"]
        grouped.setdefault(key, []).append(item)
    return [
        {"name": display_names[key], "plugins": sorted(items, key=lambda item: item["filename"].lower())}
        for key, items in sorted(grouped.items())
        if len(items) > 1
    ]


def list_plugins(server) -> list[dict]:

    directory = plugins_directory(
        server
    )

    if not directory.exists():
        return []

    plugins = []

    for path in directory.iterdir():

        if not path.is_file():
            continue

        if not (
            path.name.endswith(".jar")
            or path.name.endswith(
                ".jar.disabled"
            )
        ):
            continue

        plugins.append(
            plugin_info(path)
        )


    return sorted(
        plugins,
        key=lambda plugin: (
            plugin["name"].casefold(),
            plugin["filename"].removesuffix(".disabled").casefold(),
        ),
    )


def validate_plugin_archive(path: Path) -> None:
    if path.stat().st_size > MAX_PLUGIN_BYTES:
        raise ValueError("Plugin exceeds the configured size limit")
    try:
        with zipfile.ZipFile(path) as archive:
            if "plugin.yml" not in archive.namelist() and "paper-plugin.yml" not in archive.namelist():
                raise ValueError("JAR does not contain plugin.yml or paper-plugin.yml")
    except zipfile.BadZipFile as error:
        raise ValueError("Plugin is not a valid JAR archive") from error


def install_plugin_file(server, source: Path, filename: str, replace: bool = False) -> dict:
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith(".jar"):
        raise ValueError("Plugin filename must be a local .jar filename")
    validate_plugin_archive(source)
    directory = plugins_directory(server)
    directory.mkdir(parents=True, exist_ok=True)
    destination = safe_plugin_path(server, safe_name)
    disabled_destination = destination.with_name(destination.name + ".disabled")
    existing = destination if destination.exists() else disabled_destination if disabled_destination.exists() else None
    if existing and not replace:
        raise PluginFileExistsError(safe_name, existing == destination)
    temporary = directory / f".{safe_name}.upload"
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
        if disabled_destination.exists():
            disabled_destination.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return plugin_info(destination)


def _validate_public_https_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Plugin URL must be a public HTTPS URL")
    if parsed.port not in {None, 443}:
        raise ValueError("Plugin URL must use the standard HTTPS port")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError("Plugin host could not be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Plugin URL may not resolve to a private network")
    return parsed


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        _validate_public_https_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def install_plugin_url(server, url: str, replace: bool = False) -> dict:
    parsed = _validate_public_https_url(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name
    if not filename.lower().endswith(".jar"):
        raise ValueError("Plugin URL path must end in .jar")
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Craftarr-Console"})
    with tempfile.NamedTemporaryFile(prefix="craftarr-plugin-", suffix=".jar") as temporary:
        with opener.open(request, timeout=120) as response:
            final = _validate_public_https_url(response.geturl())
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_PLUGIN_BYTES:
                raise ValueError("Plugin exceeds the configured size limit")
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_PLUGIN_BYTES:
                    raise ValueError("Plugin exceeds the configured size limit")
                temporary.write(chunk)
        final_name = Path(urllib.parse.unquote(final.path)).name
        if final_name.lower().endswith(".jar"):
            filename = final_name
        temporary.flush()
        return install_plugin_file(server, Path(temporary.name), filename, replace=replace)


def safe_plugin_path(
    server,
    filename: str,
) -> Path:

    directory = (
        plugins_directory(server)
        .resolve()
    )

    path = (
        directory
        / Path(filename).name
    ).resolve()

    if path.parent != directory:
        raise ValueError(
            "Invalid plugin path"
        )

    if not (
        path.name.endswith(".jar")
        or path.name.endswith(
            ".jar.disabled"
        )
    ):
        raise ValueError(
            "Invalid plugin file"
        )

    return path


def normalize_previous_plugin_filenames(server) -> None:
    """Rename legacy disabled rollback JARs to neutral previous-version names."""
    directory = plugins_directory(server)
    if not directory.is_dir():
        return
    legacy_name = re.compile(r"^(?P<stem>.+)\.rollback-(?P<token>[0-9a-f]{12})\.jar\.disabled$", re.IGNORECASE)
    for path in directory.iterdir():
        match = legacy_name.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        token = match.group("token")
        target = directory / f"{match.group('stem')}.previous-{token}.jar.disabled"
        while target.exists():
            target = directory / f"{match.group('stem')}.previous-{uuid.uuid4().hex[:12]}.jar.disabled"
        try:
            path.rename(target)
        except OSError:
            continue


def _download_response_filename(response) -> str | None:
    candidates = []
    headers = getattr(response, "headers", None)
    if headers is not None:
        disposition = headers.get("Content-Disposition")
        if disposition:
            message = Message()
            message["Content-Disposition"] = str(disposition)
            try:
                parameters = message.get_params(header="Content-Disposition", unquote=True) or []
            except (TypeError, ValueError):
                parameters = []
            extended_candidates = []
            regular_candidates = []
            for parameter, value in parameters[1:]:
                parameter = str(parameter).casefold()
                if parameter not in {"filename", "filename*"}:
                    continue
                is_extended = parameter.endswith("*") or isinstance(value, tuple)
                if isinstance(value, tuple):
                    try:
                        value = collapse_rfc2231_value(value, errors="replace")
                    except (LookupError, UnicodeError, ValueError):
                        continue
                elif is_extended and isinstance(value, str):
                    encoded_value = re.match(r"^[^']*'[^']*'(.*)$", value)
                    if encoded_value:
                        value = urllib.parse.unquote(encoded_value.group(1))
                (extended_candidates if is_extended else regular_candidates).append(value)
            candidates.extend(extended_candidates)
            candidates.extend(regular_candidates)
        if hasattr(headers, "get_filename"):
            candidates.append(headers.get_filename())

    final_url = response.geturl()
    candidates.append(Path(urllib.parse.unquote(urllib.parse.urlsplit(final_url).path)).name)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            candidate = str(make_header(decode_header(str(candidate))))
        except (LookupError, UnicodeError, ValueError):
            candidate = str(candidate)
        candidate = urllib.parse.unquote(candidate).replace("\\", "/").rsplit("/", 1)[-1]
        if (len(candidate) <= 255 and candidate.lower().endswith(".jar")
                and not any(ord(character) < 32 for character in candidate)):
            return candidate
    return None


def _filename_from_download_template(template: str, source_filename: str | None, version: str | None) -> str:
    if not isinstance(template, str) or not template or len(template) > 255:
        raise ValueError("Download rename template is invalid")
    if not source_filename:
        raise ValueError("The download did not provide a JAR filename for the rename template")
    source = Path(source_filename)
    extension = source.suffix.lstrip(".")
    if not extension:
        raise ValueError("The download filename has no extension")
    try:
        renamed = template.format(
            filename=source.stem,
            version=version or "",
            extension=extension,
        )
    except (KeyError, ValueError, IndexError):
        raise ValueError("Download rename supports only {filename}, {version} and {extension}") from None
    if (Path(renamed).name != renamed or "/" in renamed or "\\" in renamed
            or len(renamed) > 255 or not renamed.lower().endswith(".jar")
            or any(ord(character) < 32 for character in renamed)):
        raise ValueError("Download rename must produce a valid JAR filename")
    return renamed


def install_plugin_update(server, filename: str, url: str, *, delete_previous: bool = False, expected_name: str | None = None, expected_version: str | None = None, provider_name: str | None = None, installed_pattern: str | None = None, installed_detection: str = "auto", download_rename: str = "") -> dict:
    """Install a validated release, retaining the old JAR disabled unless deletion is requested."""
    if Path(filename).name != filename or not filename.lower().endswith((".jar", ".jar.disabled")):
        raise ValueError("Select a valid installed plugin file")

    current_path = safe_plugin_path(server, filename)
    if current_path.name != filename or not current_path.is_file():
        raise FileNotFoundError("Plugin file changed or no longer exists")

    current = plugin_info(current_path)
    expected_name = expected_name or current["name"]
    if current["name"].casefold() != expected_name.casefold():
        raise ValueError("Plugin identity changed; reload the plugin list and try again")

    _validate_public_https_url(url)
    directory = plugins_directory(server).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Craftarr-Console"})
    temporary_path = None
    previous_path = None
    replaced_path = None
    moved_current = False
    downloaded_filename = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".craftarr-update-", suffix=".jar", dir=directory, delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with opener.open(request, timeout=120) as response:
                _validate_public_https_url(response.geturl())
                downloaded_filename = _download_response_filename(response)
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_PLUGIN_BYTES:
                    raise ValueError("Plugin exceeds the configured size limit")
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_PLUGIN_BYTES:
                        raise ValueError("Plugin exceeds the configured size limit")
                    temporary.write(chunk)

        validate_plugin_archive(temporary_path)
        updated_metadata = read_plugin_yml(temporary_path)
        updated_name = updated_metadata.get("name")
        if not updated_name:
            raise ValueError("Downloaded JAR does not contain a readable plugin name")
        if updated_name.casefold() != expected_name.casefold():
            raise ValueError(
                f"Downloaded JAR is for {updated_name}, not {expected_name}"
            )

        target_filename = filename
        version_token = re.sub(r"[^A-Za-z0-9.+_-]", "", expected_version or "").strip(".-_")
        current_version = _filename_plugin_version(filename.removesuffix(".disabled"), expected_name)
        if download_rename:
            target_filename = _filename_from_download_template(download_rename, downloaded_filename, version_token)
        elif expected_version:
            corrected_build_filename = (
                filename_for_installed_pattern(filename, installed_pattern, version_token)
                if installed_detection == "filename" and installed_pattern else None
            ) or filename_for_plugin_build(filename, version_token)
            if corrected_build_filename:
                target_filename = corrected_build_filename
            elif (
                not updated_metadata.get("version")
                and re.fullmatch(r"v?\d[A-Za-z0-9.+_-]*", version_token, re.IGNORECASE)
                and current_version != version_token
            ):
                plugin_stem = re.sub(r"[^A-Za-z0-9._ -]", "", expected_name).strip(" .")
                if plugin_stem:
                    target_filename = f"{plugin_stem}-{version_token}.jar"
        if not current["enabled"] and not target_filename.endswith(".disabled"):
            target_filename += ".disabled"
        target_path = safe_plugin_path(server, target_filename)
        if target_path != current_path and target_path.exists():
            raise ValueError("A JAR for this plugin version already exists; resolve the duplicate before updating")

        updated_plugin = plugin_info(temporary_path)
        updated_plugin["filename"] = target_filename
        updated_plugin["enabled"] = current["enabled"]
        updated_plugin["previous_version"] = False
        if not updated_metadata.get("version"):
            filename_version = _filename_plugin_version(target_filename.removesuffix(".disabled"), expected_name)
            updated_plugin["version"] = filename_version
            updated_plugin["version_source"] = "filename" if filename_version else None

        if not delete_previous:
            base_name = filename[:-9] if filename.endswith(".jar.disabled") else filename
            stem = base_name[:-4]
            while previous_path is None or previous_path.exists():
                previous_name = f"{stem}.previous-{uuid.uuid4().hex[:12]}.jar.disabled"
                previous_path = safe_plugin_path(server, previous_name)
            os.replace(current_path, previous_path)
            moved_current = True
        elif target_path != current_path:
            while replaced_path is None or replaced_path.exists():
                replaced_path = safe_plugin_path(server, f".craftarr-replaced-{uuid.uuid4().hex}.jar")
            os.replace(current_path, replaced_path)
            moved_current = True

        os.replace(temporary_path, target_path)
        temporary_path = None
        if replaced_path:
            replaced_path.unlink(missing_ok=True)
            moved_current = False
        return {
            "plugin": updated_plugin,
            "previous_filename": previous_path.name if previous_path else None,
            "deleted_previous": delete_previous,
        }
    except Exception:
        if moved_current:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            retained_path = previous_path if not delete_previous else replaced_path
            if retained_path and retained_path.exists() and not current_path.exists():
                os.replace(retained_path, current_path)
        raise
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def disable_plugin(
    server,
    filename: str,
):

    path = safe_plugin_path(
        server,
        filename,
    )

    if not path.exists():
        raise FileNotFoundError(
            "Plugin not found"
        )

    if path.name.endswith(
        ".jar.disabled"
    ):
        return

    destination = path.with_name(
        path.name + ".disabled"
    )

    if destination.exists():
        raise FileExistsError(
            "Disabled plugin already exists"
        )

    path.rename(destination)


def enable_plugin(
    server,
    filename: str,
):

    path = safe_plugin_path(
        server,
        filename,
    )

    if not path.exists():
        raise FileNotFoundError(
            "Plugin not found"
        )

    if not path.name.endswith(
        ".jar.disabled"
    ):
        return

    destination = path.with_name(
        path.name[:-9]
    )

    if destination.exists():
        raise FileExistsError(
            "Enabled plugin already exists"
        )

    path.rename(destination)


def remove_plugin(
    server,
    filename: str,
    remove_config: bool = False,
):

    path = safe_plugin_path(
        server,
        filename,
    )

    if not path.exists():
        raise FileNotFoundError(
            "Plugin not found"
        )


    config_dir = None

    if remove_config:
        config_dir = (
            plugin_config_directory(
                path
            )
        )


    path.unlink()


    if (
        config_dir
        and config_dir.exists()
        and config_dir.is_dir()
    ):
        import shutil

        shutil.rmtree(
            config_dir
        )
