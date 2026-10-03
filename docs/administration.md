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
From a hub, choose the owning Node in **Configure for Node** to manage
that host's destinations without signing in to its local web interface. rclone
must still be installed on the host that will create and upload the backup.

When a server is on a linked Node, its backup list, restore/create actions,
schedules, job progress and results are managed through the hub and executed on
the Node that owns that server. The archive stays on that Node (and its
configured off-site destination); linking Nodes does not copy large backup
files between them. Backup completion and failure events appear in the hub's
notification center for users who can view that server's backups.

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

The System Settings page can check for an application release. In-panel install,
rollback and restart are available when Craftarr runs as a systemd service. A
maintenance lock prevents connected users from making changes during these
operations. See the [installation guide](installation.md) for command-line
upgrade and rollback procedures.

Docker deployments must be updated through their container manager. In TrueNAS,
pull the new Craftarr image and redeploy or restart the app. The application
files are managed by the image, so in-container updates cannot replace them
reliably or restart the container safely.

## Roles and permissions

Each user is assigned one role; roles do not inherit permissions from other
roles. The built-in Administrator role has full access and cannot be changed.
Other roles can be granted permissions for server controls, console, players,
plugins, files, backups, automation, users and roles, system controls, and global
settings. Roles without global server access can receive access to specific
servers. Keep console and file permissions limited to people who need them:
they can issue Minecraft commands and change server data.

Each Craftarr installation has a persistent UUID node ID. Authenticated API
clients can retrieve it from `GET /api/instance`; server records returned by
`/api/servers` also include `node_id` and a `server_ref` in the form
`<node-id>:<local-server-id>`. The existing integer server ID remains local to
that Craftarr installation. The node ID is an identifier, not a credential.

## Linking Craftarr Nodes

Run the same Craftarr release on each Node. HTTPS is recommended between
Nodes. HTTP links are also supported when you explicitly acknowledge the
warning; use them only across a trusted private network such as Tailscale or a
VPN. HTTP does not encrypt the full-access token or server traffic, so anyone
able to monitor that network path could capture them. On the managed Node, open **Settings →
Node access token**. The active token remains visible there and can
be copied whenever a hub needs to be linked or reconnected. On the hub, open
**Settings → Linked Nodes → Link Node**, enter the managed
Node's URL and token, and connect. The hub checks the token and imports the
server list before saving the link. Use the hub's **Users** settings to assign
each user the specific local and linked Node servers they can open.

Opening a linked server console requires the user's **View assigned servers**
and **View server consoles** permissions, plus access to that server. Sending
Minecraft commands also requires **Send console commands** permission.

The token is a full server-management credential for a trusted hub. The hub
stores it encrypted and uses its own signed-in user's role and server
assignments to authorize each proxied request. User accounts on the managed
Node continue to govern direct sign-ins to that Node. Keep the token
private, and give it only to a hub you administer. Use a stable
`CRAFTARR_CONSOLE_SECRET` on each Node so saved node tokens remain
decryptable and visible after restarts.

To rotate a token, generate a replacement on the managed Node, then edit
the saved link on every hub and paste the new value. Rotation invalidates the
previous token immediately. The hub keeps the remote Node ID, cached server
IDs, and user assignments when you update the saved token, so access resumes
with the same server assignments after the new token is saved. Use **Refresh**
on the linked Node row to import server additions, changes, and removals.

Tokens created before persistent token display was added are stored only as
hashes and cannot be recovered. Regenerate such a token once and update every
hub using it; the replacement remains visible for future use.

The managed Node accepts the token for its node inventory API and
server-scoped management requests. The hub refreshes the remote server list,
shared plugin update settings and remote update/backup notifications
automatically. Shared plugin settings use the newest saved file revision;
changes made directly on either Node converge within about a minute. In-app
notifications and Node outage notices are shown on the hub and filtered by
the hub user's server assignments and content permissions. Outage and recovery
emails go to hub administrators when SMTP is configured on the hub. Node
outage emails wait five minutes by default; you can change the delay in
**Settings → System Alert Emails**. System
resource alert thresholds configured on the hub are applied to linked hosts;
those alerts appear in the hub and use the hub's SMTP settings. If a remote
Node cannot reach its hub for three minutes, it resumes its own configured
plugin update and system alert email delivery. When hub SMTP is not configured,
the remote Node keeps using its own configured email delivery. Notification
read state is stored per hub account, so it follows that user between browsers
without sharing read status with other users.

The hub owns user roles and server assignments. Each server's files, backup
archives, schedules and runtime state remain on the Node that runs it, but
are managed through the hub. Off-site credentials stay stored on their owning
Node even when you edit them from the hub. The hub's SMTP settings handle
linked Node alerts when configured. A remote Node uses its own SMTP when
the hub has no email service configured or has not checked in for three
minutes. Nodes can connect across Craftarr versions, though some functions may
be unavailable on an older Node. Keep Nodes updated to use all available
functions.

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
