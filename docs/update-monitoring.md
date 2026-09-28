# Craftarr plugin and Paper update monitoring

**Monitoring never installs or downloads plugin/Paper JARs.** It reads release
metadata and links. Existing administrator-controlled installation features are
separate.

A plugin's name/version is read from `plugin.yml` or `paper-plugin.yml`. The shared
`plugin-monitoring.yml` repository maps normalized metadata names and aliases to
update sources. Renaming a JAR does not affect recognition or its settings.
Enabled and disabled JARs are included; monitoring can be switched off independently.
Plugin descriptors are limited to 256 KiB; this accommodates large permission lists
such as Citizens while keeping metadata parsing bounded.

The existing Craftarr automation worker checks all managed servers every 24 hours.
The last daily run is persisted across restarts. Downtime delays checks until the
Craftarr runs again. No additional service or cron entry is needed.

## Configuring a source

On the Plugins page, click **Update settings** beside a plugin. Choose not to
monitor it, use a shared source when one exists, or configure a source for that
server. Then select a provider:

| Provider | Input | Behaviour |
| --- | --- | --- |
| GitHub Releases | `owner/repository` or `https://github.com/owner/repository` | Uses the latest stable GitHub release; ignores drafts/prereleases |
| Modrinth | Project slug, ID or project URL | Prefers stable Bukkit/Paper-compatible artifacts, falls back to beta if no stable artifacts exist, and filters by published Minecraft versions |
| Jenkins | Public HTTPS job URL, including folder/job path if needed | Reads `/lastSuccessfulBuild/api/json` for number, timestamp and artifact metadata |
| Custom URL | Public HTTPS metadata URL returning JSON, HTML, XML or text | Extracts the version and optional link from that response |

Use the developer's official source. New plugins and changed project locations
can be configured entirely through this form, without modifying application code.
GeyserMC, LuckPerms and other projects do not have special hidden mappings or
adapters; use whichever of these source types suits their published metadata.

Manual and disabled choices are stored per server and normalized plugin metadata
name. Shared choices refer to the repository and update when it changes. Settings
survive JAR replacement, filename changes, disable/enable and reinstalling the same
plugin name. Duplicate JARs with the same metadata name share settings. Server
deletion removes its per-server preferences.

Changing settings requires existing `plugins.manage` permission and access to the
server. Viewing results requires `plugins.view`. Settings cannot be saved during
an active check or preview; retry after it finishes.

## Shared settings repository

Administrators with `settings.manage` can edit the YAML, download it for sharing,
and upload a replacement from **Settings → Shared plugin update settings**. The
file is validated before it is saved. By default, the editable copy lives beside
the Craftarr database so it persists across container image updates. The bundled
`plugin-monitoring.yml` seeds the repository until an editable copy is created.
`CRAFTARR_PLUGIN_MONITORING_DEFAULTS` can set a different persistent path.

On the Plugins page, **Use shared settings** follows the current repository entry
for that plugin. **Configure settings for this server** stores an independent source,
and **Do not check for updates** overrides either source. Administrators can use
**Make global** to add a verified manual source to the shared repository. Changes to
the repository are picked up without restarting Craftarr.

The shipped configuration covers:

| Source | Plugins |
| --- | --- |
| Modrinth | AntiPopup, Chunky, LuckPerms, Pl3xMap |
| GitHub Releases | PlaceholderAPI, PlotSquared Premium (public PlotSquared releases), Vault, ViaVersion |
| Jenkins | Citizens, FastAsyncWorldEdit (FAWE) |
| Custom URL | Geyser, Floodgate (GeyserMC public download metadata API) |

Citizens compares Jenkins build numbers and needs a build number in its installed
JAR metadata. Geyser/Floodgate defaults compare base versions, not individual CI
builds. Their compatibility remains unknown. PlotSquared monitors public release
metadata without authenticated premium downloads. Use Preview to verify the
source still matches the upstream format and your installed version.

To share a source with other Craftarr users, add an entry to the repository:

```yaml
version: 1
plugins:
  ExamplePlugin:
    aliases: [Example]
    provider: github
    project: https://github.com/example/plugin
    version_pattern: ''
    link_pattern: ''
    installed_pattern: ''
```

Supported provider values are `github`, `modrinth`, `jenkins` and `custom`.
Aliases and plugin names ignore punctuation, spaces and case. Expressions follow
the same rules as the form. Optional `notes` explain source limitations. Unknown
plugins have empty fields and remain unsupported/unmonitored until configured.
A missing repository starts from the bundled mappings. Invalid entries report check
failures without stopping other sources. Existing manual and disabled settings work
independently of the file.

Install/upgrade scripts preserve the bundled root file. App-managed settings are
stored separately from the installed code and can be downloaded before sharing or
backing them up. No database migration is required for the shared mode.

## Expressions and preview

**Version expression** is required for Jenkins/Custom URL. It searches the metadata
response and returns the first capture group, or the named `version` group.
For GitHub/Modrinth it is optional and runs on the release tag/version field instead
of the whole API response. Use it to remove upstream distribution suffixes.

Examples for the response formats shown (verify the current upstream format with
Preview; these are examples, not automatic settings):

| Input text | Expression | Extracted value |
| --- | --- | --- |
| `{"version":"2.8.3"}` | `"version"\s*:\s*"([^\"]+)"` | `2.8.3` |
| `v5.5.71-bukkit` | `^v?([0-9.]+)` | `5.5.71` |
| Jenkins JSON containing `"number": 4100` | `"number"\s*:\s*(\d+)` | `4100` |
| Jenkins artifact `Example-1.4.0.jar` | `Example-([0-9.]+)\.jar` | `1.4.0` |

**Installed version expression** optionally extracts the comparable portion of the
version read from the local JAR. For `2.8.2-SNAPSHOT`, `^([0-9.]+)` extracts `2.8.2`.
For `2.0.40-SNAPSHOT (build 4099)`, `\(build\s+(\d+)\)` extracts `4099`.
If comparing upstream build numbers, extract an installed build number too;
comparing an upstream build counter with an installed semantic version is not
meaningful. When a JAR doesn't expose a build number, same-version CI builds cannot
reliably be distinguished. Comparing only base versions monitors base-version
changes, not every build.

**Download link expression** is optional for Jenkins/Custom URL. It returns the
first capture group or the named `url` group. For example,
`"url"\s*:\s*"([^\"]+)"` extracts a URL from JSON. Relative URLs resolve against the
configured source URL (Jenkins uses its successful build page). HTML entities and
JSON-escaped slashes are decoded. The detected link is displayed but never fetched.
Without this expression, Jenkins constructs an artifact link only if the response
identifies exactly one JAR; otherwise it links to the build page. GitHub similarly
shows a download link for a single JAR asset; Modrinth uses a primary JAR when present.

Click **Test these settings** to fetch metadata and see:

- the actual installed version and value used for comparison;
- the source version or tag, the value captured by the expression, and whether that value can be compared;
- the detected latest version, release/source page and download link;
- update status, compatibility and any extraction/request error.

Changing the version expression after a test re-runs the match against the same
fetched source data; it does not request the source again. Shared settings can
also be tested, and their result is shown in the modal.
Changing the provider, project, download-link expression or installed-version
expression clears the preview, so test again after those changes.

Testing does not save settings, cache release results, send email, or alter
check history/notification state. Responses from an earlier configuration are
discarded. Save when the results are correct. Saving does not fetch the source;
use **Check for updates** to populate normal results or wait for the daily run.
Matching cached results may appear immediately.

Expressions use the `regex` package, with a 1024-character expression limit and a
100 ms execution timeout. Matching uses the first match, not necessarily the
newest version on a page; target a latest-release field or a well-defined page
section. Invalid expressions, missing matches, empty values and uncomparable
versions produce a failure rather than a guessed result. Regex flags such as
`(?s)` may be used for multiline source formats.

## Results, compatibility and Paper

Plugin rows show Installed, Latest, Update Status, Compatibility, Last Checked,
source and any detected download link. Official/source links open in a new tab.

- **Current**: comparable installed version is equal to or newer than the selected release.
- **Update available**: a newer release explicitly supports the server's Minecraft version.
- **Compatibility unknown**: a newer release exists without explicit compatibility data.
- **Incompatible**: published compatibility excludes this Minecraft version; no notification is sent.
- **Unsupported/unmonitored**: no source is configured.
- **Monitoring disabled**: monitoring was explicitly switched off.
- **Check failed**: request, parsing or comparison failed. Other sources still run.
- **Not checked**: no result exists yet, or the Paper JAR changed since its last check.

Modrinth selects the newest eligible release for the installed Minecraft version,
including an older compatible release when newer releases target other versions.
It prefers stable versions and falls back to beta only when a project has no
stable Bukkit/Paper-compatible versions; it ignores alpha versions.
GitHub prose, Jenkins metadata and Custom URL text are not treated as explicit
Minecraft compatibility guarantees. They report Unknown. Custom/Jenkins patterns
may select prereleases or development builds; preview the extracted value before
saving. Numeric versions, `v` prefixes, common CI suffixes and prereleases are
compared with the existing version parser; opaque formats are not guessed.

Paper remains a separate server integration, not a plugin mapping. The Server
Version card reads its installed JAR's Minecraft metadata and compares SHA-256
against the PaperMC Fill v3 build index. It monitors stable/recommended builds
**within the installed Minecraft version**. A custom/unmatched JAR has an unknown
build; stale database build numbers are not trusted. Viewing requires `servers.view`;
manually checking Paper requires `servers.properties` and server access.

## Caching, safety and failures

Upstream results are cached persistently for six hours and shared across servers
with the same source and extraction rules. Installed-version expressions do not
fragment the shared release cache. A manual fresh check bypasses the normal TTL,
subject to a one-minute refresh minimum. Failures are cached for 15 minutes,
including manual retries. Each provider key is fetched at most once per batch.
Changing source/extraction rules changes cache identity. Preview fetches directly
and does not populate this cache.

GitHub/Modrinth/Paper API destinations are fixed service hosts. Administrator-defined
Jenkins/Custom URLs must use public HTTPS on port 443, without embedded credentials,
fragments or known token-bearing query parameters. Authentication is not supported;
use public metadata endpoints. DNS results are checked for non-public addresses,
and the connection is pinned to a checked address while retaining TLS hostname
verification. This prevents DNS rebinding between validation and connection.
Environment proxy settings are not used for these custom requests.

Custom requests follow at most five HTTPS redirects. Every destination is validated
again, must resolve only to public IP addresses, and is connected through a pinned
address with TLS hostname verification. Requests have a ten-second socket timeout,
an overall read deadline, a 1 MiB response limit and text/JSON/XML content checks.
Known artifact URL suffixes, archive responses and compressed responses are
rejected. Download links are validated as HTTPS browser links and never requested by
the monitor. API bodies, regex input text and exception details containing
credentials are not logged.
The structured service clients retain their 15-second timeout and 8 MiB limit.

A database lease serializes scheduled/CLI/web checks, settings writes and previews
across processes. A crashed process's lease expires after 30 minutes. Failures are
isolated per source, and request/extraction errors appear in the row or preview.

## Email and manual checks

Scheduled checks group newly discovered updates across servers into one plain-text
email per eligible recipient with subject `Craftarr: Plugin updates available`.
The existing SMTP settings are used; Mailgun works through SMTP. Recipients follow
the existing system-alert policy: enabled users with an email address and
`settings.manage`, further limited to servers/content they can view. The system
resource-alert enable switch is separate from update monitoring.

Notification state persists per server, source, recipient and available version.
The same reported version is not mailed again after restart; a newer version
triggers another notification. Installing it returns the plugin to Current on the
next view/check. Failed email delivery does not consume notification state.
Disabling monitoring retains this history, preventing repeat emails when the same
source is re-enabled. Changing sources/version extraction can represent a new
release identity. As with ordinary SMTP delivery, a crash between SMTP acceptance
and the database commit may cause a repeat message.

Web checks and previews never send email. From the application environment:

```bash
python -m app.admin_cli check-updates
python -m app.admin_cli check-updates --server Survival
python -m app.admin_cli check-updates --server 1
python -m app.admin_cli check-updates --notify
python -m app.admin_cli check-updates --cached
```

Omit `--server` for all servers, including Paper. `--notify` explicitly enables
grouped/deduplicated email; `--cached` uses the normal TTL. Disabled or unconfigured
plugins are reported without upstream requests. CLI uses the same saved settings
as the page and scheduler. Per-provider failures appear in the table while other
checks continue. An invalid server or an active check exits with an error.

Standard production example:

```bash
cd /opt/craftarr
sudo -u craftarr env CRAFTARR_CONSOLE_ENV=/etc/craftarr/console.env \
  /opt/craftarr/.venv/bin/python -m app.admin_cli check-updates
```

## Deployment, migration and extension

Install `requirements.txt` (includes `packaging` and `regex`), run
`alembic upgrade head`, and restart the existing Craftarr service. Startup also
applies migrations automatically. No new service, permission or secret is needed.

- `27b10c8d39a4`: upstream cache, server check snapshots, notification history and lease.
- `38c21d9e40b5`: per-server plugin monitoring preferences.
- `49d32eaf51c6`: source URLs and extraction expressions; removes automatic selection.

The latest migration preserves explicitly configured GitHub/Modrinth sources.
Former automatic/GeyserMC/Citizens-specific configurations become disabled and need
an explicit generic source. Saved disabled settings take precedence over YAML
starting settings.
Old plugin check snapshots are cleared; Paper history and notification history are
retained. Existing explicit sources that relied on project-specific suffix handling
must configure a suitable version expression and preview it.

Adding a plugin or changing its release location requires only UI settings, or a
shared YAML entry that all servers can use.
Adding an entirely new structured service parser still requires a `Provider`
implementation returning `Release` values and a factory option in
`app/plugin_monitoring.py`, along with the UI option and mocked tests. The core
cache, scheduler, notification and CLI code remain provider-independent. Avoid
adding plugin-name mappings to Python code: keep shared starting settings in the
YAML file and use generic providers and extraction rules.

Run the full suite with `pytest`. Monitoring/source tests in
`tests/test_update_monitoring.py` mock all external HTTP and DNS responses.
