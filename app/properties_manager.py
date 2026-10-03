from pathlib import Path


BOOLEAN_TRUE = {"true", "1", "yes", "on"}


def properties_path(server) -> Path:
    return (
        Path(server.directory)
        / "server.properties"
    )


def read_properties(server) -> dict:
    path = properties_path(server)

    if not path.exists():
        return {}

    result = {}

    for line in path.read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines():

        stripped = line.strip()

        if (
            not stripped
            or stripped.startswith("#")
            or "=" not in stripped
        ):
            continue

        key, value = stripped.split(
            "=",
            1,
        )

        result[key.strip()] = value.strip()

    return result


def bool_value(
    properties: dict,
    key: str,
    default: bool = False,
) -> bool:

    value = properties.get(
        key,
        str(default).lower(),
    )

    return (
        str(value).lower()
        in BOOLEAN_TRUE
    )


def int_value(
    properties: dict,
    key: str,
    default: int,
) -> int:

    try:
        return int(
            properties.get(
                key,
                default,
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def write_properties(
    server,
    updates: dict,
):
    path = properties_path(server)

    existing_lines = []

    if path.exists():
        existing_lines = path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()

    updated_keys = set()
    output = []

    for line in existing_lines:

        stripped = line.strip()

        if (
            not stripped
            or stripped.startswith("#")
            or "=" not in line
        ):
            output.append(line)
            continue

        key, _ = line.split(
            "=",
            1,
        )

        key = key.strip()

        if key in updates:
            output.append(
                f"{key}={updates[key]}"
            )

            updated_keys.add(key)

        else:
            output.append(line)

    for key, value in updates.items():

        if key not in updated_keys:
            output.append(
                f"{key}={value}"
            )

    path.write_text(
        "\n".join(output) + "\n",
        encoding="utf-8",
    )


def get_properties_view(server) -> dict:
    props = read_properties(server)

    return {
        "motd":
            props.get(
                "motd",
                "A Minecraft Server",
            ),

        "server_port":
            int_value(
                props,
                "server-port",
                25565,
            ),

        "max_players":
            int_value(
                props,
                "max-players",
                20,
            ),

        "difficulty":
            props.get(
                "difficulty",
                "easy",
            ),

        "gamemode":
            props.get(
                "gamemode",
                "survival",
            ),

        "online_mode":
            bool_value(
                props,
                "online-mode",
                True,
            ),

        "enforce_secure_profile":
            bool_value(
                props,
                "enforce-secure-profile",
                False,
            ),

        "level_name":
            props.get(
                "level-name",
                "world",
            ),

        "level_seed":
            props.get(
                "level-seed",
                "",
            ),

        "view_distance":
            int_value(
                props,
                "view-distance",
                10,
            ),

        "simulation_distance":
            int_value(
                props,
                "simulation-distance",
                10,
            ),

        "pause_when_empty_seconds":
            int_value(
                props,
                "pause-when-empty-seconds",
                -1,
            ),

        "spawn_protection":
            int_value(
                props,
                "spawn-protection",
                16,
            ),

        "allow_nether":
            bool_value(
                props,
                "allow-nether",
                True,
            ),

        "pvp":
            bool_value(
                props,
                "pvp",
                True,
            ),

        "hardcore":
            bool_value(
                props,
                "hardcore",
                False,
            ),

        "enable_command_block":
            bool_value(
                props,
                "enable-command-block",
                False,
            ),

        "allow_flight":
            bool_value(
                props,
                "allow-flight",
                False,
            ),

        "white_list":
            bool_value(
                props,
                "white-list",
                False,
            ),

        "enable_query":
            bool_value(
                props,
                "enable-query",
                False,
            ),

        "enable_rcon":
            bool_value(
                props,
                "enable-rcon",
                False,
            ),

        "resource_pack":
            props.get(
                "resource-pack",
                "",
            ),
    }


def save_properties(
    server,
    data: dict,
):

    updates = {
        "motd":
            str(
                data.get(
                    "motd",
                    "",
                )
            ),

        "server-port":
            str(
                int(
                    data.get(
                        "server_port",
                        25565,
                    )
                )
            ),

        "max-players":
            str(
                int(
                    data.get(
                        "max_players",
                        20,
                    )
                )
            ),

        "difficulty":
            str(
                data.get(
                    "difficulty",
                    "easy",
                )
            ),

        "gamemode":
            str(
                data.get(
                    "gamemode",
                    "survival",
                )
            ),

        "online-mode":
            str(
                bool(
                    data.get(
                        "online_mode"
                    )
                )
            ).lower(),

        "enforce-secure-profile":
            str(
                bool(
                    data.get(
                        "enforce_secure_profile",
                        False,
                    )
                )
            ).lower(),

        "level-name":
            str(
                data.get(
                    "level_name",
                    "world",
                )
            ),

        "level-seed":
            str(
                data.get(
                    "level_seed",
                    "",
                )
            ),

        "view-distance":
            str(
                int(
                    data.get(
                        "view_distance",
                        10,
                    )
                )
            ),

        "simulation-distance":
            str(
                int(
                    data.get(
                        "simulation_distance",
                        10,
                    )
                )
            ),

        "spawn-protection":
            str(
                int(
                    data.get(
                        "spawn_protection",
                        16,
                    )
                )
            ),

        "allow-nether":
            str(
                bool(
                    data.get(
                        "allow_nether"
                    )
                )
            ).lower(),

        "pvp":
            str(
                bool(
                    data.get(
                        "pvp"
                    )
                )
            ).lower(),

        "hardcore":
            str(
                bool(
                    data.get(
                        "hardcore"
                    )
                )
            ).lower(),

        "enable-command-block":
            str(
                bool(
                    data.get(
                        "enable_command_block"
                    )
                )
            ).lower(),

        "allow-flight":
            str(
                bool(
                    data.get(
                        "allow_flight"
                    )
                )
            ).lower(),

        "white-list":
            str(
                bool(
                    data.get(
                        "white_list"
                    )
                )
            ).lower(),

        "enable-query":
            str(
                bool(
                    data.get(
                        "enable_query"
                    )
                )
            ).lower(),

        "enable-rcon":
            str(
                bool(
                    data.get(
                        "enable_rcon"
                    )
                )
            ).lower(),

        "resource-pack":
            str(
                data.get(
                    "resource_pack",
                    "",
                )
            ),
    }

    if "pause_when_empty_seconds" in data:
        updates["pause-when-empty-seconds"] = str(int(data["pause_when_empty_seconds"]))

    write_properties(
        server,
        updates,
    )
