import os
import pwd
import re
import socket
import subprocess
import tempfile
import zipfile
from pathlib import Path


IGNORED_SCAN_DIRECTORIES = {
    "backups", "cache", "libraries", "logs", "plugins", "versions",
    "world", "world_nether", "world_the_end",
}
PROTECTED_IMPORT_PATHS = {
    Path("/"), Path("/bin"), Path("/boot"), Path("/dev"), Path("/etc"),
    Path("/proc"), Path("/run"), Path("/sbin"), Path("/sys"), Path("/usr"),
    Path("/opt/craftarr"), Path("/var/lib/craftarr"),
}


def _properties(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def _jar_for(directory: Path) -> Path | None:
    jars = sorted(directory.glob("*.jar"))
    preferred = directory / "paper.jar"
    if preferred.is_file():
        return preferred
    paper = next((jar for jar in jars if "paper" in jar.name.lower()), None)
    return paper or (jars[0] if jars else None)


def _systemd_units() -> list[str]:
    if not Path("/run/systemd/system").exists():
        return []
    try:
        result = subprocess.run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.split()[0]
        for line in result.stdout.splitlines()
        if line.strip().endswith(("enabled", "disabled", "static", "indirect", "generated", "masked"))
        and line.split()[0].endswith(".service")
    ][:500]


def systemd_service_records() -> list[dict]:
    units = _systemd_units()
    if not units:
        return []
    try:
        result = subprocess.run(
            [
                "systemctl", "show", "--no-pager", "--property=Id",
                "--property=WorkingDirectory", "--property=ExecStart",
                "--property=ActiveState", "--property=UnitFileState", *units,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    records = []
    for block in result.stdout.strip().split("\n\n"):
        values = dict(
            line.partition("=")[::2]
            for line in block.splitlines()
            if "=" in line
        )
        if values.get("Id"):
            records.append(values)
    return records


def find_systemd_service(directory: Path, records: list[dict] | None = None) -> dict | None:
    directory_text = str(directory.resolve())
    for values in records if records is not None else systemd_service_records():
        working_directory = values.get("WorkingDirectory", "")
        exec_start = values.get("ExecStart", "")
        if working_directory == directory_text or directory_text in exec_start:
            return {
                "unit": values["Id"],
                "active": values.get("ActiveState") in {"active", "activating"},
                "enabled": values.get("UnitFileState") in {"enabled", "enabled-runtime"},
            }
    return None


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def inspect_server_directory(
    value: str | Path,
    *,
    process_backend: str = "systemd",
    verify_write: bool = False,
    systemd_services: list[dict] | None = None,
) -> dict:
    requested = Path(value).expanduser()
    errors = []
    warnings = []

    if not requested.is_absolute():
        return {"directory": str(requested), "ready": False, "errors": ["Use an absolute server path"], "warnings": []}
    try:
        directory = requested.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"directory": str(requested), "ready": False, "errors": ["Server directory does not exist"], "warnings": []}

    if not directory.is_dir():
        errors.append("Path is not a directory")
    if requested.is_symlink():
        errors.append("Server directory cannot be a symbolic link")
    if directory == Path("/") or any(
        protected != Path("/")
        and (directory == protected or protected in directory.parents)
        for protected in PROTECTED_IMPORT_PATHS
    ):
        errors.append("This system path cannot be used as a Minecraft server directory")
    if (process_backend == "systemd" or os.getenv("INVOCATION_ID")) and (
        directory == Path("/home") or Path("/home") in directory.parents
        or directory == Path("/root") or Path("/root") in directory.parents
        or directory == Path("/Users") or Path("/Users") in directory.parents
    ):
        errors.append("Production services cannot access home directories; move the server to /srv or /opt")

    properties_path = directory / "server.properties"
    jar = _jar_for(directory) if directory.is_dir() else None
    if not properties_path.is_file():
        errors.append("server.properties was not found")
    if not jar:
        errors.append("No server JAR was found in the directory")

    properties = {}
    if properties_path.is_file():
        try:
            properties = _properties(properties_path)
        except OSError as error:
            errors.append(f"Unable to read server.properties: {error}")

    port = None
    if properties_path.is_file():
        try:
            port = int(properties.get("server-port", "25565"))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            port = None
            errors.append("server.properties contains an invalid server-port")

    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        errors.append("The panel service account needs read, write, and traverse access to the directory")
    if jar and not os.access(jar, os.R_OK):
        errors.append(f"The panel service account cannot read {jar.name}")
    elif jar and not zipfile.is_zipfile(jar):
        errors.append(f"{jar.name} is not a valid JAR file")
    if properties_path.is_file() and not os.access(properties_path, os.R_OK | os.W_OK):
        errors.append("The panel service account needs read and write access to server.properties")

    if verify_write and not errors:
        try:
            with tempfile.NamedTemporaryFile(prefix=".craftarr-import-", dir=directory, delete=True):
                pass
        except OSError as error:
            errors.append(f"A write test in the server directory failed: {error}")

    eula_path = directory / "eula.txt"
    try:
        eula_text = eula_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        eula_text = ""
    if not re.search(r"(?mi)^\s*eula\s*=\s*true\s*$", eula_text):
        warnings.append("Minecraft EULA acceptance was not detected (eula=true)")

    service = find_systemd_service(directory, systemd_services)
    if service and (service["active"] or service["enabled"]):
        state = "active" if service["active"] else "enabled"
        errors.append(
            f"Existing service {service['unit']} is {state}; stop and disable it before Craftarr takes ownership"
        )
    elif service:
        warnings.append(
            f"Disabled external service {service['unit']} was detected; Craftarr will use its own service"
        )

    if port is not None and not _port_available(port) and not (service and service["active"]):
        errors.append(f"Port {port} is already in use")

    try:
        stat = directory.stat()
        owner = pwd.getpwuid(stat.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(directory.stat().st_uid) if directory.exists() else "unknown"

    try:
        checked_as = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        checked_as = str(os.geteuid())

    return {
        "name": directory.name,
        "directory": str(directory),
        "jar_name": jar.name if jar else None,
        "port": port,
        "owner": owner,
        "checked_as": checked_as,
        "service": service,
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def detect_server_directories(server_root: Path, managed_directories: set[str]) -> list[dict]:
    root = server_root.resolve()
    if not root.is_dir():
        return []
    results = []
    services = systemd_service_records()
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and name not in IGNORED_SCAN_DIRECTORIES and depth < 2
        ]
        if "server.properties" not in files or str(current_path.resolve()) in managed_directories:
            continue
        result = inspect_server_directory(current_path, systemd_services=services)
        if result.get("jar_name") and "No server JAR was found in the directory" not in result["errors"]:
            results.append(result)
        directories[:] = []
    return sorted(results, key=lambda item: item["name"].lower())
