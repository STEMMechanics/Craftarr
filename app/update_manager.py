import json
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
import re
from datetime import datetime
from pathlib import Path

from .version import APP_VERSION


GITHUB_REPO = "STEMMechanics/Craftarr"

GITHUB_API = (
    "https://api.github.com/repos/"
    f"{GITHUB_REPO}"
)


def github_request(
    url: str,
):
    request = urllib.request.Request(
        url,
        headers={
            "Accept":
                "application/vnd.github+json",

            "User-Agent":
                f"Craftarr-Console/{APP_VERSION}",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=10,
    ) as response:

        return json.load(
            response
        )


def normalize_version(
    version: str,
) -> tuple[int, ...]:

    version = (
        version
        .strip()
        .lower()
        .removeprefix("v")
    )

    try:

        return tuple(
            int(part)
            for part in version.split(".")
        )

    except ValueError:

        return (0,)


def get_latest_release():

    try:

        data = github_request(
            f"{GITHUB_API}/releases/latest"
        )

    except urllib.error.HTTPError as error:

        if error.code == 404:

            return {
                "current_version":
                    APP_VERSION,

                "latest_version":
                    APP_VERSION,

                "tag":
                    None,

                "name":
                    None,

                "url":
                    None,

                "published_at":
                    None,

                "update_available":
                    False,

                "release_available":
                    False,
            }

        raise


    tag = data.get(
        "tag_name",
        ""
    )

    return {
        "current_version":
            APP_VERSION,

        "latest_version":
            tag.removeprefix("v"),

        "tag":
            tag,

        "name":
            data.get("name")
            or tag,

        "url":
            data.get("html_url"),

        "published_at":
            data.get("published_at"),

        "update_available":
            (
                normalize_version(tag)
                >
                normalize_version(
                    APP_VERSION
                )
            ),

        "release_available":
            True,
    }


MONITORING_DEFAULTS_FILE = "plugin-monitoring.yml"
UPDATE_ITEMS = ("app", "migrations", "alembic.ini", "requirements.txt", MONITORING_DEFAULTS_FILE)
RELEASE_TAG_PATTERN = re.compile(r"^v?[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
ROLLBACK_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _restore_items(root: Path, backup: Path) -> None:
    for name in UPDATE_ITEMS:
        saved = backup / name
        if not saved.exists() and name != MONITORING_DEFAULTS_FILE:
            raise ValueError(f"Rollback is incomplete: missing {name}")
    for name in UPDATE_ITEMS:
        saved = backup / name
        target = root / name
        if name == MONITORING_DEFAULTS_FILE and not saved.exists():
            continue  # Snapshots before monitoring defaults existed remain valid.
        if target.exists() and target.is_dir():
            shutil.rmtree(target)
        if saved.is_dir():
            shutil.copytree(saved, target)
        else:
            shutil.copy2(saved, target)


def rollback_release(rollback_id: str, project_root: Path | None = None) -> dict:
    if not ROLLBACK_ID_PATTERN.fullmatch(rollback_id or ""):
        raise ValueError("Invalid rollback identifier")
    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    backup = root / ".updates" / rollback_id
    if not backup.is_dir():
        raise ValueError("Rollback snapshot not found")
    _restore_items(root, backup)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--requirement", str(root / "requirements.txt")],
        check=True, timeout=600,
    )
    return {"rolled_back": rollback_id, "restart_required": True}


def _download(url: str, limit: int = 256 * 1024 * 1024) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com", "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }:
        raise ValueError("Untrusted release download URL")
    request = urllib.request.Request(url, headers={"User-Agent": f"Craftarr-Console/{APP_VERSION}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Release asset is too large")
    return data


def _safe_extract(archive_data: bytes, destination: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as error:
                raise ValueError("Release archive contains an unsafe path") from error
            if member.issym() or member.islnk():
                raise ValueError("Release archive may not contain links")
        archive.extractall(destination, filter="data")
    candidates = [destination, *[item for item in destination.iterdir() if item.is_dir()]]
    for candidate in candidates:
        if (candidate / "app").is_dir() and (candidate / "alembic.ini").is_file():
            return candidate
    raise ValueError("Release archive does not contain a Craftarr application")


def install_release(tag: str, project_root: Path | None = None) -> dict:
    if not tag or len(tag) > 64 or not RELEASE_TAG_PATTERN.fullmatch(tag):
        raise ValueError("Invalid release tag")
    release = github_request(f"{GITHUB_API}/releases/tags/{urllib.parse.quote(tag, safe='')}")
    if release.get("tag_name") != tag:
        raise ValueError("Release tag does not match the requested version")
    assets = release.get("assets", [])
    archives = [asset for asset in assets if str(asset.get("name", "")).endswith(".tar.gz")]
    if len(archives) != 1:
        raise ValueError("Release must contain exactly one .tar.gz application asset")
    archive_asset = archives[0]
    checksum_name = archive_asset["name"] + ".sha256"
    checksum_asset = next((asset for asset in assets if asset.get("name") == checksum_name), None)
    if not checksum_asset:
        raise ValueError(f"Release is missing {checksum_name}")
    archive_data = _download(archive_asset["browser_download_url"])
    checksum_data = _download(checksum_asset["browser_download_url"], 4096).decode("ascii", "strict")
    expected = checksum_data.strip().split()[0].lower()
    actual = hashlib.sha256(archive_data).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        raise ValueError("Release checksum verification failed")

    root = (project_root or Path(__file__).resolve().parent.parent).resolve()
    backup = root / ".updates" / datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="craftarr-update-") as temp_name:
        source = _safe_extract(archive_data, Path(temp_name))
        try:
            for name in UPDATE_ITEMS:
                current = root / name
                if current.exists():
                    if current.is_dir():
                        shutil.copytree(current, backup / name)
                    else:
                        shutil.copy2(current, backup / name)
            for name in UPDATE_ITEMS:
                incoming = source / name
                if name == MONITORING_DEFAULTS_FILE and ((root / name).exists() or not incoming.exists()):
                    continue  # Preserve administrator edits and support older releases.
                if not incoming.exists():
                    raise ValueError(f"Release is missing {name}")
                target = root / name
                if incoming.is_dir():
                    staging = root / f".{name}.update"
                    if staging.exists():
                        shutil.rmtree(staging)
                    shutil.copytree(incoming, staging)
                    old = root / f".{name}.previous"
                    if old.exists():
                        shutil.rmtree(old)
                    os.replace(target, old)
                    os.replace(staging, target)
                    shutil.rmtree(old)
                else:
                    staging = root / f".{name}.update"
                    shutil.copy2(incoming, staging)
                    os.replace(staging, target)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--requirement", str(root / "requirements.txt")],
                check=True, timeout=600,
            )
        except Exception:
            _restore_items(root, backup)
            raise
    return {"installed": tag, "rollback_id": backup.name, "restart_required": True}
