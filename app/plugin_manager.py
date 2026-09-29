import re
import zipfile
import ipaddress
import os
import socket
import urllib.parse
import urllib.request
import shutil
import tempfile
import uuid
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


def read_plugin_yml(jar_path: Path) -> dict:
    """
    Read the small amount of metadata we need
    from plugin.yml inside a Paper/Spigot plugin.
    """

    try:
        with zipfile.ZipFile(jar_path) as jar:

            metadata_name = "plugin.yml" if "plugin.yml" in jar.namelist() else "paper-plugin.yml"
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


def install_plugin_update(server, filename: str, url: str, *, keep_previous: bool = True, expected_name: str | None = None, expected_version: str | None = None, provider_name: str | None = None) -> dict:
    """Install a monitored release on the server, optionally retaining the old JAR disabled."""
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
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".craftarr-update-", suffix=".jar", dir=directory, delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with opener.open(request, timeout=120) as response:
                _validate_public_https_url(response.geturl())
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
        if expected_version:
            version_token = re.sub(r"[^A-Za-z0-9.+_-]", "", expected_version).strip(".-_")
            current_version = _filename_plugin_version(filename.removesuffix(".disabled"), expected_name)
            corrected_build_filename = filename_for_plugin_build(filename, version_token)
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
                    if not current["enabled"]:
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

        if keep_previous:
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
            "previous_filename": previous_path.name if keep_previous else None,
        }
    except Exception:
        if moved_current:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            retained_path = previous_path if keep_previous else replaced_path
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
