"""Paper performance metrics bridge and snapshot reader.

Craftarr is a Python application, while Paper exposes TPS/MSPT inside the
Minecraft JVM.  A tiny, dependency-free Paper plugin is compiled against the
server's own Paper API on first start and writes an atomic JSON snapshot.  The
server process never receives a command and the snapshot is cheap to read from
the existing background metrics collector.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from time import time


logger = logging.getLogger(__name__)

BRIDGE_PLUGIN_NAME = "CraftarrMetrics"
BRIDGE_PLUGIN_VERSION = "1.0.0"
BRIDGE_SNAPSHOT_RELATIVE_PATH = Path("plugins") / BRIDGE_PLUGIN_NAME / "metrics.json"
MAX_SNAPSHOT_BYTES = 4096
MAX_SNAPSHOT_AGE_SECONDS = 180
MAX_TPS = 20.5
MAX_MSPT = 600_000.0
_VERSION_PATTERN = re.compile(r"(?m)^version\s*:\s*[\"']?([^\"'\s#]+)")


def metrics_snapshot_path(directory: str | Path) -> Path:
    """Return the bridge snapshot path inside a managed server directory."""
    return Path(directory).expanduser() / BRIDGE_SNAPSHOT_RELATIVE_PATH


def _number(value, *, maximum: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def read_metrics_snapshot(
    directory: str | Path,
    *,
    now: float | None = None,
    max_age_seconds: int = MAX_SNAPSHOT_AGE_SECONDS,
) -> dict | None:
    """Read a recent bridge snapshot, returning only validated values."""
    path = metrics_snapshot_path(directory)
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        recorded_at = float(data.get("recorded_at"))
    except (TypeError, ValueError):
        return None
    if recorded_at > 10_000_000_000:
        recorded_at /= 1000
    current_time = time() if now is None else now
    if recorded_at < current_time - max_age_seconds or recorded_at > current_time + 30:
        return None
    return {
        "recorded_at": recorded_at,
        "tps": _number(data.get("tps"), maximum=MAX_TPS),
        "mspt": _number(data.get("mspt"), maximum=MAX_MSPT),
        "source": str(data.get("source") or "paper-api")[:40],
    }


def _java_tool(java_path: str, name: str) -> str | None:
    configured = Path(str(java_path)).expanduser()
    if configured.name == "java" and configured.parent != Path("."):
        sibling = configured.with_name(name)
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    return shutil.which(name)


def _embedded_api_libraries(server_jar: Path, destination: Path) -> list[Path]:
    """Extract only the Paper API's compile-time libraries from Paperclip."""
    prefixes = (
        "META-INF/libraries/io/papermc/paper/paper-api/",
        "META-INF/libraries/net/kyori/",
        "META-INF/libraries/org/jetbrains/",
    )
    extracted = []
    try:
        with zipfile.ZipFile(server_jar) as archive:
            for name in archive.namelist():
                if not name.endswith(".jar") or not name.startswith(prefixes):
                    continue
                relative_name = name.removeprefix("META-INF/libraries/")
                relative = PurePosixPath(relative_name)
                if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                    continue
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.append(target)
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    return extracted


def _api_classpath(
    server_root: Path,
    server_jar: Path,
    embedded_directory: Path | None = None,
) -> list[Path]:
    """Find Paper/Spigot API jars without assuming a particular Paper version."""
    candidates = [server_jar]
    libraries = server_root / "libraries"
    if libraries.is_dir():
        for pattern in (
            "**/paper-api-*.jar",
            "**/spigot-api-*.jar",
            "**/bukkit-*.jar",
        ):
            candidates.extend(path for path in libraries.glob(pattern) if path.is_file())
    api_jars = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        try:
            with zipfile.ZipFile(resolved) as archive:
                if (
                    "org/bukkit/Server.class" not in archive.namelist()
                    or "org/bukkit/plugin/java/JavaPlugin.class" not in archive.namelist()
                ):
                    continue
        except (OSError, zipfile.BadZipFile):
            continue
        seen.add(resolved)
        api_jars.append(resolved)
    embedded_jars = []
    if not api_jars and embedded_directory is not None:
        embedded_jars = _embedded_api_libraries(server_jar, embedded_directory)
        for candidate in embedded_jars:
            try:
                with zipfile.ZipFile(candidate) as archive:
                    if (
                        "org/bukkit/Server.class" in archive.namelist()
                        and "org/bukkit/plugin/java/JavaPlugin.class" in archive.namelist()
                    ):
                        api_jars.append(candidate.resolve())
            except (OSError, zipfile.BadZipFile):
                continue
    if not api_jars:
        return []

    # Paper's API signatures reference Adventure and annotation types. Include
    # the server's resolved libraries so javac can resolve those signatures
    # without hard-coding a Paper version's dependency list.
    result = list(api_jars)
    seen = set(api_jars)
    if libraries.is_dir():
        for dependency in libraries.rglob("*.jar"):
            try:
                resolved = dependency.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    for dependency in embedded_jars:
        resolved = dependency.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _installed_bridge_version(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            content = archive.read("plugin.yml").decode("utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    match = _VERSION_PATTERN.search(content)
    return match.group(1) if match else None


def ensure_metrics_bridge(
    directory: str | Path,
    server_jar: str | Path,
    java_path: str = "java",
) -> bool:
    """Install or update the local Paper bridge before a server starts.

    A failed bridge build is deliberately non-fatal: the Minecraft server can
    still start and Craftarr will show TPS/MSPT as unavailable.
    """
    root = Path(directory).expanduser().resolve()
    plugin_directory = root / "plugins"
    target = plugin_directory / "craftarr-metrics.jar"
    disabled = plugin_directory / "craftarr-metrics.jar.disabled"
    if disabled.is_file():
        logger.info("Craftarr metrics bridge is disabled for %s", root)
        return False
    if target.is_file() and _installed_bridge_version(target) == BRIDGE_PLUGIN_VERSION:
        return True

    source_directory = Path(__file__).resolve().parent / "metrics_bridge_source"
    source = source_directory / "CraftarrMetricsPlugin.java"
    descriptor = source_directory / "plugin.yml"
    if not source.is_file() or not descriptor.is_file():
        logger.warning("Craftarr metrics bridge source is missing")
        return False

    javac = _java_tool(java_path, "javac")
    jar_tool = _java_tool(java_path, "jar")
    if not javac or not jar_tool:
        logger.info("A JDK is unavailable; TPS/MSPT metrics will be unavailable for %s", root)
        return False

    try:
        plugin_directory.mkdir(parents=True, exist_ok=True)
        temporary_directory = Path(tempfile.mkdtemp(prefix=".craftarr-metrics-", dir=str(plugin_directory)))
    except OSError as error:
        logger.warning("Unable to prepare the Paper metrics bridge directory: %s", error)
        return False
    classes_directory = temporary_directory / "classes"
    temporary_jar = temporary_directory / "craftarr-metrics.jar"
    classes_directory.mkdir()
    try:
        classpath = _api_classpath(
            root,
            Path(server_jar).expanduser(),
            temporary_directory / "embedded-libraries",
        )
        if not classpath:
            logger.info("Paper API is unavailable; TPS/MSPT metrics will be unavailable for %s", root)
            return False
        shutil.copyfile(descriptor, classes_directory / "plugin.yml")
        compile_result = subprocess.run(
            [
                javac,
                "-source", "8",
                "-target", "8",
                "-encoding", "UTF-8",
                "-cp", os.pathsep.join(str(path) for path in classpath),
                "-d", str(classes_directory),
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if compile_result.returncode != 0:
            detail = (compile_result.stderr or compile_result.stdout or "javac failed").strip()
            logger.warning("Unable to build the Paper metrics bridge: %s", detail[-1000:])
            return False

        package_result = subprocess.run(
            [jar_tool, "cf", str(temporary_jar), "-C", str(classes_directory), "."],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if package_result.returncode != 0:
            detail = (package_result.stderr or package_result.stdout or "jar failed").strip()
            logger.warning("Unable to package the Paper metrics bridge: %s", detail[-1000:])
            return False
        os.replace(temporary_jar, target)
        return True
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Unable to install the Paper metrics bridge: %s", error)
        return False
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)
