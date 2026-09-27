# Craftarr UI redesign audit

This audit records the current frontend and backend boundaries before template changes. The redesign reference supplied in chat contains a desktop dashboard, server detail screen, mobile navigation, login screen, component palette, and a separate Minecraft landscape image. The intended visual direction is a Poppins-based, light-blue STEMMechanics interface with a desktop sidebar, clear server controls, compact metrics, selective voxel art, and a bottom bar on phones.

## 1. Pages and routes

The web UI is served by FastAPI and Jinja templates. `/dashboard` redirects to the active server page, or to `/system` when there is no active server. Existing pages are:

- Account: `/login`, `/login/tfa`, `/forgot-password`, `/reset-password`, `/change-password`, `/profile`, `/users`, and `/users/{user_id}`.
- Server list and setup: `/servers`, `/servers/new`, and `/servers/import`.
- Server pages: `/servers/{server_id}`, `/console`, `/players`, `/plugins`, `/files`, `/files/edit`, `/logs`, `/backups`, `/scheduling` (with the old `/automation` route), `/properties`, and `/advanced-properties`.
- Global administration: `/system` and `/settings`.

Each major server page has a full-page template and a matching `partials/` template. `web_render.render_page()` returns the partial for an HTMX request and the full page otherwise. Navigation and redesign work must preserve this split, the `#page-content` target, route URLs, and permission checks.

## 2. Existing reusable components

`base.html` supplies the application shell, `site_header.html` supplies the current header/navigation, and `topbar.html` supplies server status and controls. The base also owns shared operation/progress overlays and the toast region. Page templates reuse CSS patterns such as `overview-card`, `file-row`, `plugin-row`, `backup-row`, and modal classes, but there is no consistent component/template library beyond a navigation macro. The previous sidebar partial is deleted; old sidebar selectors remain in the stylesheet even though the current shell uses a sticky two-row header on desktop and bottom navigation below 900px.

## 3. JavaScript behavior

Most interactions live in `app/static/app.js` (about 7,180 lines). It handles HTMX navigation state, polling, dialogs, forms, server controls, metrics, files, plugins, backups, settings, and user/role management. DOM IDs and `data-*` attributes connect the templates to these behaviors, so those contracts need to be preserved or deliberately migrated together. `editor.js` initializes the bundled CodeMirror 6 editor in `editor-source.js`; it supports YAML, JSON, HTML, XML, CSS, JavaScript, Markdown, and SQL, remembers cursor/scroll position, and reports YAML warnings. Uploads have a determinate progress dialog, ZIP creation has an indeterminate progress dialog, and backup jobs expose progress and cancellation.

## 4. Forms and APIs

Page forms use Jinja/HTML form posts for login, server creation/import, file upload/edit, profile, users, and settings. Dynamic screens use JSON `fetch()` calls. Important API groups include server status/control/console/process stats; players, whitelist and IP bans; plugin listing/upload/link/actions/update monitoring; file folder/ZIP/extract/move/rename/delete/YAML checks; backup list/create/jobs/cancel/restore/delete; schedules and historical metrics; Paper versions/builds/install/update checks; and global settings, roles, users, alerts, and off-site backups. Existing server permissions gate both navigation and these actions.

## 5. Server operations

The existing server action API supports start, stop, restart, and console commands. Status is polled for the active server and server selector. Process stats provide current per-server CPU, memory, and uptime. Paper version/build installation is separate from update checks. No kill-server action was found in the current web UI/API, so the redesign should not imply that it exists.

## 6. File management and editor

The file page supports directory browsing, file/folder download (folders are returned as ZIPs), file and folder uploads, folder creation, file editing, rename, delete, drag-and-drop move, ZIP creation, and ZIP extraction with conflict handling. YAML validation runs before saving supported files. Destructive file operations already use a confirmation modal. File icons currently distinguish folders from generic files; there is no direct create-file route or action. The brief’s requested create-file feature needs a small, explicit implementation using the existing safe-path/file-writing boundary. Mobile layout rules already adapt the file rows, but the redesign should verify this after the shared shell changes.

## 7. Plugins

The plugin page supports upload or HTTPS-link installation, replacement prompts, duplicate detection/resolution, enable/disable, removal, configuration-file editing, update checks, and per-plugin monitoring setup/preview. Actions and data are rendered dynamically by `app.js`. Removal already requires confirmation and non-primary actions are exposed through row menus. Plugin update checks are separate from server software checks.

## 8. Backups and updates

Backups support create, list, download, restore, delete, job progress, and cancellation. Restore/delete have confirmation dialogs; restore is blocked while the server is running. The current list exposes filename, date, and size, but backup type is not stored as a separate field. Paper and plugin update status/check APIs exist, and Paper installation is available from server properties. There is no combined “Backup & Update” transaction today; a combined UI action would need to sequence the existing backup and update operations rather than bypass them.

## 9. Performance data sources

Historical per-server metrics store running state, CPU percentage, memory bytes, player count, and uptime. `/api/web/servers/{server_id}/metrics` returns those values for charting, and the current dashboard draws CPU, memory, players, and uptime with canvas. There is no TPS/MSPT metric or per-server disk usage in the current data model. `/api/system/stats` exposes host-level CPU, memory, and disk/storage; that storage value describes the machine, not an individual Minecraft server. The dashboard should show only supported values and label host-level storage clearly if it is included. Current charts have no hover tooltips and use a shared line color.

## 10. Mobile, accessibility, assets, and implementation implications

The stylesheet is one 151 KB, 8,350-line file with four `:root` blocks and 31 media queries, including overlapping 1200, 1100, 1000, 900, 800, 700, and 600px rules. It contains obsolete sidebar rules and several generations of appended theme overrides. Current mobile navigation replaces the desktop header navigation at 900px; focus styling and a reduced-motion rule exist. The static assets currently include the STEMMechanics SVG logo and a bundled editor, but no landscape or illustration assets. The supplied landscape is a 1774×887 PNG of about 1.85 MB and is suitable for an optimized WebP crop/hero treatment rather than a full-page background. The current STEMMechanics website loads Poppins from Google Fonts, which matches the concept’s typography direction; this can be self-hosted to avoid adding a runtime font CDN dependency.

The redesign can therefore reuse existing APIs, editor, confirmations, and progress UI while changing the shared shell and page presentation. The only explicit feature gaps identified against the brief are a dedicated Performance page, direct file creation, a separately stored backup type, and TPS/MSPT data. Of these, only the first two can be added cleanly without inventing new server telemetry; the other two require backend data/design decisions.

## Worktree note

The repository was already on `feature/docker-image` with a large staged change set before this audit request. No templates or UI source files were changed during the audit; this report is the first new artifact. Keep the work staged and uncommitted as previously requested.

## Redesign implementation notes

After completing the audit, the shared shell was rebuilt with a desktop sidebar, an explicit mobile bottom bar, a server selector, and permission-filtered links. The desktop dashboard and mobile layout now use the supplied landscape, self-hosted Poppins fonts, and the official STEMMechanics wordmark. Existing routes, form submissions, JavaScript IDs, progress dialogs, and server controls remain in place. Navigation uses partial swaps within the current server and full-page navigation when the server context changes.

The new `/servers/{server_id}/performance` page reads the existing historical metrics endpoint and shows CPU, memory, players, and uptime. TPS/MSPT remain absent because the application does not collect them. A safe empty-file creation action was added to the file manager using the existing server-root path boundary and exclusive file creation; an existing file cannot be replaced. Host storage remains on the machine page and is not shown as server storage. Backup type is still not shown because it is not stored as a distinct field, and “Backup & Update” remains separate actions rather than implying a combined transaction.

The shared header now holds notifications and the account menu; the server controls sit in the server home card and disable according to the polled online state. The supplied overworld image fills the viewport as a fixed background behind the rounded, inset app frame.
