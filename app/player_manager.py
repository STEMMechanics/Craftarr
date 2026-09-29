import gzip
import io
import ipaddress
import json
import re
import struct
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from .processes import (
    get_console,
    get_runtime_online_players,
    send_command,
    server_status,
)


JOIN_PATTERN = re.compile(
    r": ([A-Za-z0-9_]{1,16}) joined the game"
)

LOGIN_PATTERN = re.compile(
    r"\]:\s+([A-Za-z0-9_]{1,16})(?:\[/[^\]]+\])? logged in with entity id"
)

LEAVE_PATTERN = re.compile(
    r": ([A-Za-z0-9_]{1,16}) left the game"
)

DISCONNECT_PATTERN = re.compile(
    r"\]:\s+([A-Za-z0-9_]{1,16})(?:\s+\([^)]*\))? lost connection:"
)

MAX_PLAYER_NBT_BYTES = 32 * 1024 * 1024
PLAYER_DATA_TIMESTAMP_TOLERANCE_SECONDS = 60


def read_json_file(
    path: Path,
    default,
):
    if not path.exists():
        return default

    try:
        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ):
        return default


def read_properties(
    directory: str,
) -> dict:

    path = (
        Path(directory)
        / "server.properties"
    )

    properties = {}

    if not path.exists():
        return properties

    for line in path.read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines():

        line = line.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, value = line.split(
            "=",
            1,
        )

        properties[key.strip()] = (
            value.strip()
        )

    return properties


def _read_nbt_last_played(data: bytes) -> int | None:
    """Read the LastPlayed long from a compressed or raw player NBT file."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            payload = compressed.read(MAX_PLAYER_NBT_BYTES + 1)
        if len(payload) > MAX_PLAYER_NBT_BYTES:
            return None
    except (OSError, EOFError, zlib.error):
        try:
            decompressor = zlib.decompressobj()
            payload = decompressor.decompress(data, MAX_PLAYER_NBT_BYTES + 1)
            has_unconsumed_data = bool(decompressor.unconsumed_tail)
        except zlib.error:
            payload = data
            has_unconsumed_data = False
        if len(payload) > MAX_PLAYER_NBT_BYTES or has_unconsumed_data:
            return None

    offset = 0
    last_played = None

    def read(size: int) -> bytes:
        nonlocal offset
        if size < 0 or offset + size > len(payload):
            raise ValueError("Invalid player NBT data")
        value = payload[offset:offset + size]
        offset += size
        return value

    def read_unsigned_byte() -> int:
        return read(1)[0]

    def read_int() -> int:
        return struct.unpack(">i", read(4))[0]

    def read_string() -> str:
        length = struct.unpack(">H", read(2))[0]
        return read(length).decode("utf-8", errors="replace")

    def read_payload(tag_type: int, name: str | None = None, depth: int = 0):
        nonlocal last_played
        if depth > 64:
            raise ValueError("Player NBT nesting is too deep")

        if tag_type == 1:
            read(1)
        elif tag_type == 2:
            read(2)
        elif tag_type == 3:
            read(4)
        elif tag_type == 4:
            value = struct.unpack(">q", read(8))[0]
            if name == "LastPlayed":
                last_played = value
        elif tag_type == 5:
            read(4)
        elif tag_type == 6:
            read(8)
        elif tag_type == 7:
            length = read_int()
            read(length)
        elif tag_type == 8:
            read_string()
        elif tag_type == 9:
            element_type = read_unsigned_byte()
            length = read_int()
            if length < 0 or (length and element_type == 0):
                raise ValueError("Invalid player NBT list")
            for _ in range(length):
                read_payload(element_type, depth=depth + 1)
        elif tag_type == 10:
            while True:
                child_type = read_unsigned_byte()
                if child_type == 0:
                    break
                child_name = read_string()
                read_payload(child_type, child_name, depth + 1)
        elif tag_type == 11:
            length = read_int()
            read(length * 4)
        elif tag_type == 12:
            length = read_int()
            read(length * 8)
        else:
            raise ValueError("Unknown player NBT tag")

    try:
        root_type = read_unsigned_byte()
        if root_type != 10:
            return None
        read_string()  # Root compound name.
        read_payload(root_type)
    except (ValueError, IndexError, struct.error):
        return None

    return last_played


@lru_cache(maxsize=8192)
def _cached_nbt_last_played(path: str, modified_ns: int, size: int) -> int | None:
    if size > MAX_PLAYER_NBT_BYTES:
        return None
    try:
        return _read_nbt_last_played(Path(path).read_bytes())
    except OSError:
        return None


def player_data_last_online(player_data_directory: Path | None, player_uuid):
    if player_data_directory is None or not player_uuid:
        return None, False
    try:
        normalized_uuid = str(UUID(str(player_uuid)))
    except (TypeError, ValueError, AttributeError):
        return None, False

    player_ids = tuple(
        dict.fromkeys(
            (normalized_uuid.replace("-", ""), normalized_uuid)
        )
    )
    exact_timestamps = []
    estimated_timestamps = []
    for player_id in player_ids:
        for suffix in (".dat", ".dat_old"):
            path = player_data_directory / f"{player_id}{suffix}"
            try:
                resolved = path.resolve()
                if resolved.is_relative_to(player_data_directory) and resolved.is_file():
                    stat = resolved.stat()
                    last_played = _cached_nbt_last_played(
                        str(resolved),
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
                    if last_played is not None and last_played > 0:
                        exact_timestamp = last_played / 1000
                        exact_timestamps.append(exact_timestamp)
                        if (
                            stat.st_mtime
                            > exact_timestamp + PLAYER_DATA_TIMESTAMP_TOLERANCE_SECONDS
                        ):
                            estimated_timestamps.append(stat.st_mtime)
                    else:
                        estimated_timestamps.append(stat.st_mtime)
            except (OSError, RuntimeError):
                continue
    if not exact_timestamps and not estimated_timestamps:
        return None, False
    exact_timestamp = max(exact_timestamps, default=None)
    estimated_timestamp = max(estimated_timestamps, default=None)
    if estimated_timestamp is not None and (
        exact_timestamp is None
        or estimated_timestamp
        > exact_timestamp + PLAYER_DATA_TIMESTAMP_TOLERANCE_SECONDS
    ):
        timestamp = estimated_timestamp
        estimated = True
    else:
        timestamp = exact_timestamp
        estimated = False
    try:
        value = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None, False
    return value, estimated


def set_property(
    directory: str,
    key: str,
    value: str,
):
    path = (
        Path(directory)
        / "server.properties"
    )

    lines = []

    found = False

    if path.exists():

        lines = path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()

    output = []

    for line in lines:

        if line.startswith(
            key + "="
        ):

            output.append(
                f"{key}={value}"
            )

            found = True

        else:
            output.append(line)

    if not found:
        output.append(
            f"{key}={value}"
        )

    path.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )


def get_online_players(
    server_id: int,
) -> set[str]:
    """
    Reconstruct online state from this
    panel session's console buffer.

    Later on Ubuntu we can replace this
    with a stronger runtime query.
    """

    status = server_status(
        server_id
    )

    if not status.get(
        "running",
        False,
    ):
        return set()

    # The supervisor is the best starting point, but its in-memory state can be
    # empty after an upgrade/restart while Minecraft is already running. Replay
    # the recent console events over that snapshot so a visible login is never
    # discarded merely because the socket returned a valid empty list.
    runtime_players = get_runtime_online_players(server_id)
    online = set(runtime_players or ())

    for line in get_console(
        server_id
    ):

        joined = JOIN_PATTERN.search(line) or LOGIN_PATTERN.search(line)

        if joined:

            online.add(
                joined.group(1)
            )

            continue

        left = LEAVE_PATTERN.search(line) or DISCONNECT_PATTERN.search(line)

        if left:

            online.discard(
                left.group(1)
            )

    return online


def get_player_data(
    server,
) -> dict:

    root = Path(
        server.directory
    )

    properties = read_properties(
        server.directory
    )

    player_data_directory = None
    try:
        resolved_root = root.resolve()
        world_directory = (resolved_root / properties.get("level-name", "world")).resolve()
        if world_directory.is_relative_to(resolved_root):
            # Minecraft 26.1 moved player files from <world>/playerdata to
            # <world>/players/data. Prefer the new location, while retaining
            # compatibility with older server layouts.
            candidates = (
                world_directory / "players" / "data",
                world_directory / "playerdata",
            )
            for candidate in candidates:
                resolved_candidate = candidate.resolve()
                if (
                    resolved_candidate.is_relative_to(resolved_root)
                    and resolved_candidate.is_dir()
                ):
                    player_data_directory = resolved_candidate
                    break
    except (OSError, RuntimeError, ValueError):
        pass

    whitelist_data = read_json_file(
        root / "whitelist.json",
        [],
    )

    ops_data = read_json_file(
        root / "ops.json",
        [],
    )

    banned_data = read_json_file(
        root / "banned-players.json",
        [],
    )

    ip_bans = read_json_file(
        root / "banned-ips.json",
        [],
    )

    user_cache = read_json_file(
        root / "usercache.json",
        [],
    )


    whitelist = {
        item.get("name", "").casefold():
            item
        for item in whitelist_data
        if item.get("name")
    }

    operators = {
        item.get("name", "").casefold():
            item
        for item in ops_data
        if item.get("name")
    }

    banned = {
        item.get("name", "").casefold():
            item
        for item in banned_data
        if item.get("name")
    }

    cache = {
        item.get("name", "").casefold():
            item
        for item in user_cache
        if item.get("name")
    }


    online_names = get_online_players(
        server.id
    )

    online = {
        name.casefold(): name
        for name in online_names
    }


    names = set()

    names.update(cache)
    names.update(whitelist)
    names.update(operators)
    names.update(banned)
    names.update(online)


    players = []

    for key in names:

        cached = cache.get(
            key,
            {}
        )

        whitelist_entry = (
            whitelist.get(
                key,
                {}
            )
        )

        op_entry = (
            operators.get(
                key,
                {}
            )
        )

        banned_entry = (
            banned.get(
                key,
                {}
            )
        )

        name = (
            online.get(key)
            or cached.get("name")
            or whitelist_entry.get("name")
            or op_entry.get("name")
            or banned_entry.get("name")
            or key
        )

        uuid = (
            cached.get("uuid")
            or whitelist_entry.get("uuid")
            or op_entry.get("uuid")
            or banned_entry.get("uuid")
        )

        last_online, last_online_estimated = player_data_last_online(
            player_data_directory,
            uuid,
        )

        players.append({
            "name": name,
            "uuid": uuid,
            "last_online": last_online,
            "last_online_estimated": last_online_estimated,

            "online":
                key in online,

            "whitelisted":
                key in whitelist,

            "operator":
                key in operators,

            "op_level":
                op_entry.get(
                    "level",
                    None,
                ),

            "banned":
                key in banned,

            "ban_reason":
                banned_entry.get(
                    "reason"
                ),
        })


    players.sort(
        key=lambda item: (
            not item["online"],
            item["name"].lower(),
        )
    )


    max_players = 20

    try:
        max_players = int(
            properties.get(
                "max-players",
                "20",
            )
        )

    except ValueError:
        pass


    whitelist_enabled = (
        properties.get(
            "white-list",
            "false",
        ).lower()
        == "true"
    )


    return {
        "running":
            server_status(
                server.id
            ).get(
                "running",
                False,
            ),

        "online_count":
            len(online_names),

        "max_players":
            max_players,

        "whitelist_enabled":
            whitelist_enabled,

        "whitelisted_count":
            len(whitelist),

        "operator_count":
            len(operators),

        "banned_count":
            len(banned),

        "ip_banned_count":
            len(ip_bans),

        "ip_bans": [
            {
                "ip": str(item.get("ip", "")),
                "reason": item.get("reason"),
                "source": item.get("source"),
                "created": item.get("created"),
                "expires": item.get("expires"),
            }
            for item in ip_bans
            if item.get("ip")
        ],

        "players":
            players,
    }


def set_whitelist_enabled(
    server,
    enabled: bool,
):
    set_property(
        server.directory,
        "white-list",
        "true"
        if enabled
        else "false",
    )

    if server_status(
        server.id
    ).get("running"):

        send_command(
            server.id,
            (
                "whitelist on"
                if enabled
                else "whitelist off"
            ),
        )


def require_running(
    server,
):
    if not server_status(
        server.id
    ).get(
        "running",
        False,
    ):
        raise RuntimeError(
            "Server must be running "
            "for this action."
        )


def whitelist_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"whitelist add {player}",
    )


def remove_whitelist(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"whitelist remove {player}",
    )


def op_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"op {player}",
    )


def deop_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"deop {player}",
    )


def kick_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"kick {player}",
    )


def ban_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"ban {player}",
    )


def pardon_player(
    server,
    player: str,
):
    require_running(server)

    send_command(
        server.id,
        f"pardon {player}",
    )


def ban_ip(server, address: str):
    require_running(server)
    try:
        normalized = str(ipaddress.ip_address(address.strip()))
    except ValueError as error:
        raise RuntimeError("A valid IPv4 or IPv6 address is required") from error
    send_command(server.id, f"ban-ip {normalized}")


def pardon_ip(server, address: str):
    require_running(server)
    try:
        normalized = str(ipaddress.ip_address(address.strip()))
    except ValueError as error:
        raise RuntimeError("A valid IPv4 or IPv6 address is required") from error
    send_command(server.id, f"pardon-ip {normalized}")
