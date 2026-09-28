# Craftarr installation and deployment

Craftarr's production installer supports systemd-based Linux hosts:

| Distribution | Supported versions | Package manager |
| --- | --- | --- |
| Ubuntu | 22.04 and newer | `apt` |
| Oracle Linux | 8 and newer | `dnf` |

The installer creates a locked-down service account, a Python virtual environment,
systemd units, a persistent application secret, and data directories.

## Install

The current one-line installer entry point is:

```bash
curl -fsSL https://raw.githubusercontent.com/STEMMechanics/Craftarr/main/scripts/install.sh | sudo bash
```

The script downloads and installs the latest published release, not unreleased
commits on `main`. Review [the installer](../scripts/install.sh) before piping
it to a shell. For a reviewable installation, clone a trusted release checkout and run:

```bash
sudo ./scripts/install.sh
```

The installer installs Python, polkit and supporting system packages. It detects
and preserves Java installations already on the host. Interactive installs ask
which Java versions to add; a blank response installs none. Automated installs
add no Java by default. Select runtimes with repeated `--java-version` options,
for example `--java-version 21 --java-version 25`. Paper 26.1 and newer require
Java 25; older servers can keep their compatible runtime. Choose a server's Java
when creating it or from its Properties page. The System page lists detected
runtimes. On first startup after an upgrade, older servers without an explicit
Java choice are assigned the installed runtime recommended for their recorded
Minecraft version; explicit choices are preserved.

Use `--skip-packages` only when the dependencies are already installed. The
installer does not configure a firewall or reverse proxy.

## Network and first login

A fresh interactive install asks for the web host and port. Press Enter to use
`0.0.0.0:8000`, which listens on all interfaces. For an unattended install,
provide explicit values or accept the defaults:

```bash
sudo ./scripts/install.sh --host 0.0.0.0 --port 8000 --non-interactive
```

Use `--host 127.0.0.1` when the panel should only be reachable through a local
reverse proxy. Repair installs retain the saved host in
`/etc/craftarr/console.env`; pass `--host 0.0.0.0` to change an
existing loopback-only install.

On a fresh install, the installer prints a generated password for the initial
`admin` account once. Only its one-way hash is stored. Change the temporary
password after signing in.

The initial configuration permits the session cookie over HTTP until HTTPS is
configured. After enabling HTTPS on the reverse proxy, set this value in
`/etc/craftarr/console.env` and restart the service:

```text
CRAFTARR_CONSOLE_COOKIE_SECURE=true
```

Configure HTTPS before exposing the panel to an untrusted network.

## Repair and service commands

If the first service start fails, rerun the installer. It preserves the database,
configuration, Minecraft servers and login details while repairing application
and systemd files:

```bash
curl -fsSL https://raw.githubusercontent.com/STEMMechanics/Craftarr/main/scripts/install.sh | sudo bash
```

For startup diagnostics:

```bash
sudo journalctl -u craftarr-console.service --no-pager -n 200
```

Craftarr stores its application, configuration and database under these paths.
Minecraft servers and their backups remain under `/srv/minecraft`:

| Path | Purpose |
| --- | --- |
| `/opt/craftarr` | Application and Python virtual environment |
| `/etc/craftarr/console.env` | Service configuration and session secret |
| `/var/lib/craftarr` | Database and upgrade snapshots |
| `/srv/minecraft` | Managed Minecraft instances and backups |

The installed `craftarr-console` helper provides service and administration commands:

```bash
craftarr-console status
craftarr-console restart
craftarr-console logs
craftarr-console reset-password admin
craftarr-console server survival restart
craftarr-console server survival logs
```

Privileged actions rerun through `sudo` using the helper's resolved absolute
path. The helper is installed at `/usr/bin/craftarr-console` so it works with a
restricted `sudo` `secure_path`, including the Oracle Linux 8 default. Repair
installs also place the helper at `/usr/local/sbin/craftarr-console`.

Use the release installer to repair root-owned command locations if needed:

```bash
sudo /usr/local/sbin/craftarr-console restart
curl -fsSL https://raw.githubusercontent.com/STEMMechanics/Craftarr/main/scripts/install.sh | sudo bash
```

Configuration uses the `CRAFTARR_*` environment variables documented in
`.env.example`.

## Import an existing server

Use **Import Server** to discover unmanaged Minecraft directories under the
configured server root or inspect an absolute path such as `/opt/minecraft`.
Before enabling import, Craftarr checks `server.properties`, the selected JAR,
port and EULA state; effective file access; a temporary write; systemd units
associated with the directory; and whether the port is occupied.

Stop and disable an active or enabled external service before Craftarr adopts its
directory. This prevents two services from writing the same world or binding the
same port. Production imports cannot use home directories because the service
keeps `ProtectHome=true`.

Importing a path outside `/srv/minecraft` requires rerunning the installer from
the release that added external-path imports. This refreshes the systemd sandbox
policy; normal Unix ownership and permissions still apply.

## Upgrade a v0.3.3 StemCraft installation to v0.4.0

Version 0.4.0 uses `CRAFTARR_*` configuration names. An in-panel update replaces
application files, but keeps the existing systemd console service and its
`EnvironmentFile`. Before updating, find the environment file loaded by the
currently installed service:

```bash
sudo systemctl cat stemcraft-console.service
```

Back up the `EnvironmentFile` shown in that output, then edit it. For the
standard StemCraft path:

```bash
sudo cp -a /etc/stemcraft-console/console.env /etc/stemcraft-console/console.env.pre-0.4.0
sudoedit /etc/stemcraft-console/console.env
```

Substitute the path from `systemctl cat` if it differs. Add or update one
`CRAFTARR_` entry for each existing `STEMCRAFT_` setting by replacing only the
prefix and keeping the current value. Keep the old entries until the update
succeeds. These are the core settings that must keep their old values:

| v0.3.3 setting | v0.4.0 setting |
| --- | --- |
| `STEMCRAFT_CONSOLE_SECRET` | `CRAFTARR_CONSOLE_SECRET` |
| `STEMCRAFT_CONSOLE_DATABASE` | `CRAFTARR_CONSOLE_DATABASE` |
| `STEMCRAFT_CONSOLE_SERVER_ROOT` | `CRAFTARR_CONSOLE_SERVER_ROOT` |
| `STEMCRAFT_CONSOLE_HOST` | `CRAFTARR_CONSOLE_HOST` |
| `STEMCRAFT_CONSOLE_PORT` | `CRAFTARR_CONSOLE_PORT` |
| `STEMCRAFT_CONSOLE_COOKIE_SECURE` | `CRAFTARR_CONSOLE_COOKIE_SECURE` |

Also carry over any optional settings such as timezone, rclone, upload limits,
or automation intervals by applying the same prefix change. Changing the
database path makes Craftarr open a different database, which can look like an
empty installation.

The old StemCraft installation also uses different defaults for managed
Minecraft systemd units and their control sockets. Add these entries when the
existing server units are named `stemcraft-server@NAME.service` and use
`/run/stemcraft-console`:

```text
CRAFTARR_SYSTEMD_UNIT_PREFIX=stemcraft-server@
CRAFTARR_SYSTEMD_SOCKET_DIR=/run/stemcraft-console
```

Use the values from the existing configuration; do not copy `.env.example`
placeholder values over production settings. Then run the update from System
Settings. The existing `stemcraft-console.service` continues to launch the
updated app; its unit name does not need to change for an in-panel update.

If the panel is already unavailable after an update, add the `CRAFTARR_*`
entries to the same environment file and restart the existing service. Substitute
the unit name and environment-file path reported by `systemctl cat` if they
differ from these examples:

```bash
sudo systemctl restart stemcraft-console.service
sudo journalctl -u stemcraft-console.service --no-pager -n 100
```

Confirm the logs show a successful startup and that the panel opens at its usual
address. Keep the backup until login and managed server controls are confirmed.

## Upgrade and rollback

The command-line upgrade script below targets Craftarr installations rooted at
`/opt/craftarr`. For a v0.3.3 StemCraft installation with a legacy path such as
`/opt/stemcraft-console`, use the in-panel updater after completing the migration
steps above; the script does not migrate the old system service or paths.

From a trusted release checkout, upgrade with:

```bash
sudo ./scripts/upgrade.sh
```

Each upgrade saves the previous application and database under
`/var/lib/craftarr/upgrades/TIMESTAMP`. Pass that exact directory to
roll back:

```bash
sudo ./scripts/rollback.sh /var/lib/craftarr/upgrades/TIMESTAMP
```

The in-panel updater can check for releases, install or roll back an update, and
restart Craftarr from System Settings. A maintenance lock prevents concurrent
changes during the operation.

## Uninstall

Remove Craftarr while preserving its database, configuration, snapshots,
backups and Minecraft servers:

```bash
sudo ./scripts/uninstall.sh --confirm
```

For a complete removal, including every managed world under `/srv/minecraft`:

```bash
sudo ./scripts/uninstall.sh --purge-all --confirm
```

> **Warning:** `--purge-all` permanently deletes managed Minecraft worlds and
> server files under `/srv/minecraft`. Keep a separate backup if you need them.

## Release publishing

Set `APP_VERSION` in `app/version.py`, merge the release-preparation pull
request, and tag that exact commit. For version `0.3.3`:

```bash
git switch main
git pull --ff-only
git tag 0.3.3
git push origin 0.3.3
```

The release workflow checks that the tag matches `APP_VERSION`, creates a GitHub
release, and attaches a versioned application archive and SHA-256 checksum. Tags
with or without a `v` prefix are supported; the current preferred style has no
prefix. The one-line installer follows the latest published release.
