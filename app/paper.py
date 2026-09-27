import json
import hashlib
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

from pathlib import Path

from .version import APP_VERSION


PAPER_API = (
    "https://fill.papermc.io/v3"
)


USER_AGENT = (
    f"Craftarr-Console/{APP_VERSION} "
    "(https://github.com/STEMMechanics/Craftarr)"
)


def inspect_paper_jar(path: str | Path) -> dict:
    """Read a Paperclip JAR's embedded Minecraft version and checksum."""
    jar_path = Path(path)
    try:
        with zipfile.ZipFile(jar_path) as archive:
            if archive.getinfo("version.json").file_size > 65536:
                raise ValueError("Paper version metadata exceeds size limit")
            version_data = json.loads(archive.read("version.json"))
            if not isinstance(version_data, dict):
                raise ValueError("Invalid Paper version metadata")
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise ValueError("Unable to identify the installed Paper JAR") from error

    version = str(version_data.get("id") or version_data.get("name") or "").strip()
    if not version or not re.fullmatch(r"[0-9A-Za-z._+-]+", version):
        raise ValueError("Unable to identify the installed Paper version")

    digest = hashlib.sha256()
    try:
        with jar_path.open("rb") as jar_file:
            for chunk in iter(lambda: jar_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ValueError("Unable to read the installed Paper JAR") from error

    checksum = digest.hexdigest().lower()
    return {"version": version, "sha256": digest.hexdigest().lower()}


def match_paper_build(checksum: str, builds: list[dict]) -> str | None:
    """Return the Paper build whose published checksum matches a JAR."""
    checksum = checksum.lower()
    for build in builds:
        downloads = build.get("downloads", {})
        for download in downloads.values() if isinstance(downloads, dict) else []:
            checksums = download.get("checksums", {}) if isinstance(download, dict) else {}
            if str(checksums.get("sha256", "")).lower() == checksum:
                return str(build["id"])
    return None


def paper_request(
    url: str,
):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                USER_AGENT,
        },
    )

    return request


def get_versions() -> list[str]:

    url = (
        f"{PAPER_API}/projects/paper"
    )

    request = paper_request(
        url
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
    ) as response:

        data = json.load(
            response
        )


    grouped_versions = data.get(
        "versions",
        {},
    )

    versions = []


    if isinstance(
        grouped_versions,
        dict,
    ):

        for group in (
            grouped_versions.values()
        ):

            if isinstance(
                group,
                list,
            ):

                versions.extend(
                    group
                )


    elif isinstance(
        grouped_versions,
        list,
    ):

        versions.extend(
            grouped_versions
        )


    # API group ordering is not a version guarantee. Sort numerically so, for
    # example, 1.21.10 remains newer than 1.7.10 regardless of response order.
    return sorted(
        set(versions),
        key=lambda version: tuple(int(part) for part in re.findall(r"\d+", version)),
        reverse=True,
    )


def get_builds(version: str) -> list[dict]:

    if not re.fullmatch(r"[0-9A-Za-z._+-]+", version):
        raise ValueError("Invalid Paper version")

    url = (
        f"{PAPER_API}/projects/paper/"
        f"versions/{version}/builds"
    )

    request = paper_request(
        url
    )

    with urllib.request.urlopen(
        request,
        timeout=20,
    ) as response:

        builds = json.load(
            response
        )


    if not builds:

        raise ValueError(
            "No Paper builds available "
            f"for {version}"
        )


    return sorted(builds, key=lambda build: int(build["id"]), reverse=True)


def get_latest_build(
    version: str,
) -> dict:
    builds = get_builds(version)

    stable_builds = [
        build
        for build in builds
        if (
            build.get("channel")
            == "STABLE"
        )
    ]


    if stable_builds:

        return stable_builds[0]


    return builds[0]


def download_paper(
    version: str,
    directory: str,
    jar_name: str = "paper.jar",
    build_id: int | None = None,
) -> dict:

    server_dir = Path(
        directory
    )

    server_dir.mkdir(
        parents=True,
        exist_ok=True,
    )


    if not re.fullmatch(r"[0-9A-Za-z._+-]+", version):
        raise ValueError("Invalid Paper version")
    jar_path = Path(jar_name)
    if jar_path.name != jar_name or jar_path.suffix.lower() != ".jar":
        raise ValueError("Invalid Paper JAR filename")

    if build_id is None:
        build = get_latest_build(version)
    else:
        build = next(
            (candidate for candidate in get_builds(version) if int(candidate["id"]) == build_id),
            None,
        )
        if not build:
            raise ValueError(f"Paper build {build_id} is not available for {version}")

    build_number = build[
        "id"
    ]

    download = build[
        "downloads"
    ][
        "server:default"
    ]

    download_url = download[
        "url"
    ]

    destination = (
        server_dir
        / jar_name
    )
    temporary = server_dir / f".{jar_name}.download"


    request = paper_request(
        download_url
    )


    digest = hashlib.sha256()
    expected_hash = download.get("checksums", {}).get("sha256")

    try:
        with urllib.request.urlopen(request, timeout=120) as source:
            with temporary.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)

        if expected_hash and digest.hexdigest().lower() != expected_hash.lower():
            raise ValueError("Downloaded Paper JAR failed SHA-256 verification")

        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


    return {
        "version":
            version,

        "build":
            str(
                build_number
            ),

        "path":
            str(
                destination
            ),
    }


def create_eula(
    directory: str,
):

    path = (
        Path(directory)
        / "eula.txt"
    )

    path.write_text(
        "eula=true\n",
        encoding="utf-8",
    )


def property_bool(
    value: bool,
) -> str:

    return (
        "true"
        if value
        else "false"
    )


def clean_property_value(
    value,
) -> str:

    return (
        str(value)
        .replace("\r", "")
        .replace("\n", " ")
    )


def create_server_properties(
    directory: str,
    port: int,

    max_players: int = 20,

    difficulty: str = "normal",

    gamemode: str = "survival",

    view_distance: int = 10,

    simulation_distance: int = 10,

    world_name: str = "world",

    seed: str = "",

    world_type: str = "minecraft:normal",

    generate_structures: bool = True,

    spawn_animals: bool = True,

    spawn_monsters: bool = True,

    spawn_npcs: bool = True,

    online_mode: bool = True,

    whitelist: bool = False,

    pvp: bool = True,

    enable_command_blocks: bool = False,

    motd: str = "A Minecraft Server",
):

    path = (
        Path(directory)
        / "server.properties"
    )


    if path.exists():
        return


    properties = {
        "server-port":
            port,

        "enable-rcon":
            False,

        "max-players":
            max_players,

        "difficulty":
            difficulty,

        "gamemode":
            gamemode,

        "view-distance":
            view_distance,

        "simulation-distance":
            simulation_distance,

        "level-name":
            world_name,

        "level-seed":
            seed,

        "level-type":
            world_type,

        "generate-structures":
            generate_structures,

        "spawn-animals":
            spawn_animals,

        "spawn-monsters":
            spawn_monsters,

        "spawn-npcs":
            spawn_npcs,

        "online-mode":
            online_mode,

        "enforce-secure-profile":
            False,

        "white-list":
            whitelist,

        "pvp":
            pvp,

        "enable-command-block":
            enable_command_blocks,

        "motd":
            motd,
    }


    lines = []


    for key, value in (
        properties.items()
    ):

        if isinstance(
            value,
            bool,
        ):

            value = property_bool(
                value
            )

        else:

            value = clean_property_value(
                value
            )


        lines.append(
            f"{key}={value}"
        )


    path.write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )


def java_available() -> bool:

    return (
        shutil.which(
            "java"
        )
        is not None
    )
