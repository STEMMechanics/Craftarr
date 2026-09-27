# Craftarr administration

Craftarr brings common Minecraft server operations into one web interface. Each
server has its own directory, game version, Paper build, Java memory and launch
options, port, plugins, configuration, files, backups and user access.

## Running Minecraft servers

During development, Minecraft servers can run as child processes. Production
installs should use independent systemd services so Minecraft keeps running when
the Craftarr web panel stops, restarts or upgrades. Choose the systemd process
backend when creating a server.

The installer permits the panel account to manage `craftarr-server@*.service`
units. Server names, JAR names,
memory values and JVM arguments are validated before use. **Start**, **Stop**
and **Restart** control the current runtime without changing its boot policy;
use **Start automatically at boot** in Properties to change that policy.

The installed units are:

- `craftarr-console.service` for the web panel and scheduled work.
- `craftarr-server@.service` for Minecraft instances managed independently.

Use `journalctl -u craftarr-console.service` for panel logs or
`journalctl -u craftarr-server@NAME.service` for a Minecraft instance.

See the [installation guide](installation.md) for importing existing server
directories, file permissions, and systemd sandbox details.

## Plugin and Paper updates

Craftarr checks installed plugins and Paper for updates every 24 hours. The
Plugins page shows versions, compatibility, status and the last check. Paper
status appears on the server overview. Checks only report updates; downloads and
installs remain administrator-controlled.

Plugin sources can be shared across servers or configured per server. The shared
YAML repository is editable and can be downloaded or uploaded from Settings.
Sources include GitHub Releases, Modrinth, Jenkins and custom metadata URLs.
Administrators can preview version extraction and promote verified server
settings to the shared repository. Email notifications use the configured SMTP
settings and avoid repeating the same version every day.

Read [update monitoring](update-monitoring.md) for provider mappings, custom
expressions, version matching, cache behaviour, permissions and troubleshooting.

## Backups

Scheduled backups can copy completed local ZIP files to configured
[rclone](https://rclone.org/) remotes such as Backblaze B2, Storj, S3 and SFTP.
Install rclone on the Craftarr host, then add and test destinations in
**Settings → Off-site Backups**. Credentials are kept in a private,
service-owned configuration file and are not returned to the browser.

Set `CRAFTARR_RCLONE_CONFIG` to choose another configuration location; for
example:

```env
CRAFTARR_RCLONE_CONFIG=/etc/craftarr/rclone.conf
```

Schedules maintain separate local and off-site retention counts. A successful
local backup remains available if an upload fails; Craftarr records a warning
so the upload can be retried or investigated.

## Database and application updates

Craftarr stores application data in SQLite and uses Alembic for schema
migrations. Pending migrations run at application startup. Administrators can
also run them before restarting:

```bash
alembic upgrade head
```

The System Settings page can check for an application release, install or roll
back an update, and restart Craftarr. A maintenance lock prevents connected
users from making changes during these operations. See the
[installation guide](installation.md) for command-line upgrade and rollback
procedures.

## Roles and permissions

Each user is assigned one role; roles do not inherit permissions from other
roles. The built-in Administrator role has full access and cannot be changed.
Other roles can be granted permissions for server controls, console, players,
plugins, files, backups, automation, users and roles, system controls, and global
settings. Roles without global server access can receive access to specific
servers. Keep console and file permissions limited to people who need them:
they can issue Minecraft commands and change server data.

## Accounts and security

Craftarr requires authenticated accounts and supports role assignments, fine-
grained permissions, per-server access, password hashing, forced password
changes, TOTP two-factor authentication, recovery codes, email password recovery
and SMTP configuration. The interface can issue Minecraft commands and manage
server files, so treat it as an administrative service. Use HTTPS in production
and restrict network access appropriately.

The authenticated interface adapts to desktop, tablet and mobile viewport sizes.

For vulnerability reports, use the private process in the
[security policy](../SECURITY.md), not a public issue.
