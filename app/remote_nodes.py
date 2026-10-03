"""Remote Craftarr link validation, inventory, and HTTP client helpers."""

import ipaddress
import hashlib
import json
from datetime import datetime, timedelta, timezone
import logging
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy.orm import Session

from .models import RemoteNode, RemoteServer, User
from .node_security import decrypt_remote_token
from .schemas import NodeIdentityOut, NodeServerOut


logger = logging.getLogger(__name__)
_last_plugin_settings_sync = 0.0
_plugin_settings_sync_interval = 60.0


class RemoteNodeError(ValueError):
    pass


class RemoteNodeUnavailable(RemoteNodeError):
    """The linked Node could not be reached over the network."""


class RemoteFunctionUnavailable(RemoteNodeError):
    """The linked Node does not implement the requested API function."""


def normalize_remote_url(value: str, *, allow_insecure_http: bool = False) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise RemoteNodeError("Enter a valid Craftarr URL") from error
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise RemoteNodeError("Enter a Craftarr URL beginning with https://")
    if parsed.username or parsed.password:
        raise RemoteNodeError("Do not include a username or password in the Craftarr URL")
    if parsed.query or parsed.fragment:
        raise RemoteNodeError("The Craftarr URL cannot include a query string or fragment")

    if parsed.scheme != "https":
        host = parsed.hostname.lower()
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback and not allow_insecure_http:
            raise RemoteNodeError(
                "HTTPS is recommended. HTTP connections are unencrypted; confirm that this link uses a trusted private network."
            )

    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    normalized = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    if len(normalized) > 1000:
        raise RemoteNodeError("Craftarr URL cannot exceed 1000 characters")
    return normalized


def _request(base_url: str, token: str, path: str, *, method="GET", payload=None, timeout=8.0) -> dict | list:
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)), follow_redirects=False) as client:
            response = client.request(
                method,
                f"{base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json=payload,
            )
    except httpx.TimeoutException as error:
        raise RemoteNodeUnavailable("The remote Node did not respond before the timeout") from error
    except httpx.HTTPError as error:
        raise RemoteNodeUnavailable("Could not connect to the remote Node") from error

    if response.status_code == 401:
        raise RemoteNodeError("The remote Node rejected this token")
    if response.status_code != 200:
        detail = None
        payload = None
        try:
            body = response.json()
            if isinstance(body, dict):
                payload = body
                detail = body.get("error") or body.get("detail")
        except ValueError:
            pass
        if response.status_code == 501:
            raise RemoteFunctionUnavailable(str(detail or "This function is not available on the selected Node"))
        if response.status_code == 404 and detail == "Not Found":
            raise RemoteFunctionUnavailable("This function is not available on the selected Node")
        if response.status_code in {502, 503, 504}:
            raise RemoteNodeUnavailable(f"The remote Node returned HTTP {response.status_code}")
        suffix = f": {str(detail)[:300]}" if detail else ""
        raise RemoteNodeError(f"The remote Node returned HTTP {response.status_code}{suffix}")
    try:
        return response.json()
    except ValueError as error:
        raise RemoteNodeError("The remote Node returned an invalid response") from error


def fetch_remote_inventory(base_url: str, token: str) -> tuple[NodeIdentityOut, list[NodeServerOut]]:
    identity_data = _request(base_url, token, "/api/node/identity")
    servers_data = _request(base_url, token, "/api/node/servers")
    if not isinstance(identity_data, dict) or not isinstance(servers_data, list):
        raise RemoteNodeError("The remote Node returned an invalid identity or server list")
    try:
        identity = NodeIdentityOut.model_validate(identity_data)
        servers = [NodeServerOut.model_validate(server) for server in servers_data]
    except Exception as error:
        raise RemoteNodeError("The remote Node returned an unsupported API response") from error
    return identity, servers


def sync_node_servers(
    db: Session,
    node: RemoteNode,
    inventory: list[NodeServerOut] | None = None,
    app_version: str | None = None,
) -> list[RemoteServer]:
    if inventory is None:
        token = decrypt_remote_token(node.token_ciphertext)
        identity, inventory = fetch_remote_inventory(node.base_url, token)
        app_version = identity.app_version

    existing = {server.server_id: server for server in node.servers}
    seen: set[int] = set()
    for remote in inventory:
        if remote.server_id <= 0:
            raise RemoteNodeError("The remote Node returned an invalid server ID")
        if remote.server_id in seen:
            raise RemoteNodeError("The remote Node returned a duplicate server ID")
        seen.add(remote.server_id)
        server = existing.get(remote.server_id)
        if server is None:
            server = RemoteServer(node_id=node.node_id, server_id=remote.server_id)
            db.add(server)
        server.name = remote.name
        server.minecraft_version = remote.minecraft_version
        server.paper_build = remote.paper_build
        server.memory = remote.memory
        server.min_memory = remote.min_memory
        server.port = remote.port
        server.enabled = remote.enabled

    for server_id, server in existing.items():
        if server_id not in seen:
            db.delete(server)

    node.last_connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    node.last_error = None
    node.outage_started_at = None
    node.offline_alert_sent = False
    node.app_version = app_version
    db.flush()
    db.expire(node, ["servers"])
    return list(node.servers)


def refresh_remote_node(db: Session, node: RemoteNode) -> list[RemoteServer]:
    was_offline = node.outage_started_at is not None
    alert_was_sent = bool(node.offline_alert_sent)
    try:
        token = decrypt_remote_token(node.token_ciphertext)
        identity, inventory = fetch_remote_inventory(node.base_url, token)
        if str(identity.node_id) != node.node_id:
            raise RemoteNodeError(
                "The Node at this URL has a different ID. Remove and link it again to avoid mixing server access."
            )
        servers = sync_node_servers(db, node, inventory, identity.app_version)
        db.commit()
        if was_offline and alert_was_sent:
            _email_remote_connection_alert(db, node, "restored", None)
        return servers
    except RemoteNodeUnavailable as error:
        mark_remote_node_offline(db, node, str(error))
        db.commit()
        raise
    except ValueError as error:
        node.last_error = str(error)[:255]
        db.commit()
        if isinstance(error, RemoteNodeError):
            raise
        raise RemoteNodeError(str(error)) from error


def _remote_notification_key(node_id: str) -> str:
    return f"remote_notifications:{node_id}"


def _save_remote_notification_snapshot(db: Session, node: RemoteNode, notifications: list[dict]) -> None:
    from .settings_manager import set_setting

    alerts = [item for item in notifications if isinstance(item, dict) and item.get("kind") == "system-alert"]
    other_items = [item for item in notifications if isinstance(item, dict) and item.get("kind") != "system-alert"]
    safe_items = alerts[:2] + other_items[:98]
    set_setting(db, _remote_notification_key(node.node_id), json.dumps(safe_items, separators=(",", ":")))


def _email_remote_connection_alert(db: Session, node: RemoteNode, state: str, detail: str | None) -> bool:
    from .emailer import send_email
    from .settings_manager import get_smtp_settings
    from .system_alerts import _admin_addresses

    recipients = _admin_addresses(db)
    if not recipients or not get_smtp_settings(db).get("smtp_host", "").strip():
        return False
    if state == "unavailable":
        subject = f"Craftarr alert: Node {node.name} is offline"
        body = (
            f"The hub could not reach Node {node.name}.\n"
            f"Last error: {detail or 'Connection failed'}\n"
            "The hub will retry automatically."
        )
    else:
        subject = f"Craftarr: Node {node.name} is back online"
        body = f"The hub can reach Node {node.name} again."
    try:
        for address in recipients:
            send_email(db, address, subject, body)
    except Exception as error:
        logger.warning("Unable to send Node connection alert (%s)", type(error).__name__)
        return False
    return True


def mark_remote_node_offline(db: Session, node: RemoteNode, detail: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if node.outage_started_at is None:
        node.outage_started_at = now
    node.last_error = detail[:255]

    from .settings_manager import get_system_alert_settings

    delay = get_system_alert_settings(db)["node_offline_delay_minutes"]
    outage_age = now - node.outage_started_at
    if not node.offline_alert_sent and outage_age >= timedelta(minutes=delay):
        node.offline_alert_sent = _email_remote_connection_alert(db, node, "unavailable", node.last_error)


def record_remote_node_offline(node_id: str, detail: str) -> None:
    from .database import SessionLocal

    db = SessionLocal()
    try:
        node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
        if node:
            mark_remote_node_offline(db, node, detail)
            db.commit()
    finally:
        db.close()


def record_remote_node_online(node_id: str) -> None:
    from .database import SessionLocal

    db = SessionLocal()
    try:
        node = db.query(RemoteNode).filter(RemoteNode.node_id == node_id).first()
        if not node:
            return
        mark_remote_node_online(db, node)
        db.commit()
    finally:
        db.close()


def mark_remote_node_online(
    db: Session,
    node: RemoteNode,
    app_version: str | None = None,
    now: datetime | None = None,
) -> None:
    was_offline = node.outage_started_at is not None
    alert_was_sent = bool(node.offline_alert_sent)
    node.last_connected_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
    node.last_error = None
    node.outage_started_at = None
    node.offline_alert_sent = False
    if app_version is not None:
        node.app_version = app_version
    if was_offline and alert_was_sent:
        _email_remote_connection_alert(db, node, "restored", None)


def _email_remote_update_notifications(db: Session, node: RemoteNode, notifications: list[dict]) -> None:
    from .emailer import send_email
    from .permissions import has_permission
    from .settings_manager import get_setting, get_smtp_settings, set_setting
    from .system_alerts import _admin_addresses

    recipients = set(_admin_addresses(db))
    if not recipients or not get_smtp_settings(db).get("smtp_host", "").strip():
        return
    users = db.query(User).filter(User.enabled.is_(True)).all()
    for user in users:
        if not user.email or user.email.strip() not in recipients or not has_permission(user, "servers.view"):
            continue
        address = user.email.strip()
        pending = []
        dedupe_keys = []
        assigned = {server.server_id for server in user.remote_servers if server.node_id == node.node_id}
        for item in notifications:
            if item.get("kind") not in {"plugin-update", "paper-update"}:
                continue
            try:
                server_id = int(item.get("server_id"))
            except (TypeError, ValueError):
                continue
            if not has_permission(user, "servers.view_all") and server_id not in assigned:
                continue
            if item.get("kind") == "plugin-update" and not has_permission(user, "plugins.view"):
                continue
            event_id = str(item.get("id") or "")
            if not event_id:
                continue
            digest = hashlib.sha256(f"{node.node_id}|{event_id}|{address}".encode("utf-8")).hexdigest()
            key = f"remote_update_sent_{digest}"
            if get_setting(db, key):
                continue
            pending.append(item)
            dedupe_keys.append(key)
        if not pending:
            continue
        lines = ["Updates are available. No updates have been installed automatically.", ""]
        for item in pending:
            lines.extend([
                f"{node.name} · {item.get('server', 'Server')}: {item.get('title', 'Update available')}",
                str(item.get("message") or ""),
                "",
            ])
        try:
            send_email(db, address, "Craftarr: Plugin updates available", "\n".join(lines))
        except Exception as error:
            logger.warning("Remote update notification delivery failed (%s)", type(error).__name__)
            continue
        for key in dedupe_keys:
            set_setting(db, key, datetime.now(timezone.utc).isoformat())
        db.commit()


def _merge_remote_system_alerts(db: Session, node: RemoteNode, alerts: list[dict]) -> list[dict]:
    from .settings_manager import get_setting, set_setting

    active = {}
    for item in alerts:
        if not isinstance(item, dict) or item.get("resource") not in {"memory", "storage"}:
            continue
        try:
            percent = float(item.get("percent"))
            threshold = int(item.get("threshold"))
        except (TypeError, ValueError):
            continue
        active[item["resource"]] = (percent, threshold)

    notifications = []
    now = datetime.now(timezone.utc).isoformat()
    for resource in ("memory", "storage"):
        key = f"remote_system_alert_since:{node.node_id}:{resource}"
        since = get_setting(db, key)
        if resource not in active:
            if since:
                set_setting(db, key, "")
            continue
        if not since:
            since = now
            set_setting(db, key, since)
        percent, threshold = active[resource]
        notifications.append({
            "id": f"system-alert:{node.node_id}:{resource}:{since}",
            "kind": "system-alert",
            "resource": resource,
            "title": f"{node.name}: {resource} usage is high",
            "message": f"{resource.title()} usage is {percent:.1f}% (threshold {threshold}%).",
            "checked_at": now,
        })
    return notifications


def _email_remote_system_alerts(db: Session, node: RemoteNode, notifications: list[dict]) -> None:
    from .emailer import send_email
    from .settings_manager import get_setting, get_smtp_settings, set_setting
    from .system_alerts import _admin_addresses

    recipients = _admin_addresses(db)
    if not recipients or not get_smtp_settings(db).get("smtp_host", "").strip():
        return
    for item in notifications:
        if item.get("kind") != "system-alert":
            continue
        event_id = str(item.get("id") or "")
        if not event_id:
            continue
        for address in recipients:
            digest = hashlib.sha256(f"{event_id}|{address}".encode("utf-8")).hexdigest()
            key = f"remote_system_alert_sent_{digest}"
            if get_setting(db, key):
                continue
            try:
                send_email(db, address, f"Craftarr alert: {item.get('title', node.name)}", item.get("message", ""))
            except Exception as error:
                logger.warning("Remote system alert delivery failed (%s)", type(error).__name__)
                continue
            set_setting(db, key, datetime.now(timezone.utc).isoformat())
            db.commit()


def synchronize_plugin_monitoring_repositories(db: Session) -> dict:
    """Converge linked Nodes on the repository with the newest file revision."""
    from .monitoring_defaults import read_repository_snapshot, save_repository_text, validate_repository_text

    local = read_repository_snapshot()
    validate_repository_text(local["content"])
    nodes = db.query(RemoteNode).order_by(RemoteNode.name).all()
    connected = []
    failed = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recoveries = []

    for node in nodes:
        was_offline = node.outage_started_at is not None
        alert_was_sent = bool(node.offline_alert_sent)
        try:
            token = decrypt_remote_token(node.token_ciphertext)
            identity, inventory = fetch_remote_inventory(node.base_url, token)
            if str(identity.node_id) != node.node_id:
                raise RemoteNodeError(
                    "The Node at this URL has a different ID. Remove and link it again to avoid mixing server access."
                )
            sync_node_servers(db, node, inventory, identity.app_version)

            remote_settings = None
            try:
                remote_settings = _request(node.base_url, token, "/api/node/plugin-monitoring-settings")
            except RemoteFunctionUnavailable:
                pass
            if remote_settings is not None:
                if not isinstance(remote_settings, dict):
                    raise RemoteNodeError("The remote Node returned invalid plugin settings")
                content = remote_settings.get("content")
                modified_at_ns = remote_settings.get("modified_at_ns")
                if not isinstance(content, str) or type(modified_at_ns) is not int or modified_at_ns < 0:
                    raise RemoteNodeError("The remote Node returned invalid plugin settings")
                validate_repository_text(content)
                remote_settings = {"content": content, "modified_at_ns": modified_at_ns}
                remote_settings["hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()

            remote_notifications = []
            try:
                alerts = _request(node.base_url, token, "/api/node/notifications")
                if not isinstance(alerts, dict) or not isinstance(alerts.get("notifications"), list):
                    raise RemoteNodeError("The remote Node returned invalid notifications")
                remote_notifications = alerts["notifications"]
            except RemoteFunctionUnavailable:
                pass

            from .settings_manager import get_smtp_settings, get_system_alert_settings
            from .system_alerts import _admin_addresses

            system_alert_settings = get_system_alert_settings(db)
            system_alert_settings["managed_by_hub"] = bool(
                get_smtp_settings(db).get("smtp_host", "").strip() and _admin_addresses(db)
            )
            system_alerts = []
            try:
                system_status = _request(
                    node.base_url,
                    token,
                    "/api/node/system-alerts",
                    method="POST",
                    payload=system_alert_settings,
                )
                if not isinstance(system_status, dict) or not isinstance(system_status.get("alerts"), list):
                    raise RemoteNodeError("The remote Node returned invalid system alert status")
                system_alerts = _merge_remote_system_alerts(db, node, system_status["alerts"])
            except RemoteFunctionUnavailable:
                pass

            notifications = remote_notifications + system_alerts
            connected.append((node, token, remote_settings))
            node.last_connected_at = now
            node.last_error = None
            node.outage_started_at = None
            node.offline_alert_sent = False
            _save_remote_notification_snapshot(db, node, notifications)
            _email_remote_update_notifications(db, node, notifications)
            _email_remote_system_alerts(db, node, system_alerts)
            if was_offline and alert_was_sent:
                recoveries.append(node)
        except RemoteNodeUnavailable as error:
            mark_remote_node_offline(db, node, str(error), now)
            if node.name not in failed:
                failed.append(node.name)
        except RemoteFunctionUnavailable as error:
            node.last_error = str(error)[:255]
            if node.name not in failed:
                failed.append(node.name)
        except (RemoteNodeError, ValueError) as error:
            node.last_error = str(error)[:255]
            if node.name not in failed:
                failed.append(node.name)
        except Exception as error:
            logger.warning("Linked Node sync failed for %s (%s)", node.name, type(error).__name__)
            node.last_error = "Unable to synchronize shared plugin settings"
            if node.name not in failed:
                failed.append(node.name)

    local_hash = hashlib.sha256(local["content"].encode("utf-8")).hexdigest()
    selected = {**local, "hash": local_hash}
    for _node, _token, remote in connected:
        if remote and remote["modified_at_ns"] > selected["modified_at_ns"]:
            selected = remote

    local_changed = selected["hash"] != local_hash or selected["modified_at_ns"] != local["modified_at_ns"]
    if local_changed:
        try:
            save_repository_text(selected["content"], modified_at_ns=selected["modified_at_ns"])
        except (TypeError, ValueError) as error:
            logger.warning("Unable to apply synchronized plugin settings (%s)", type(error).__name__)
            db.commit()
            return {"updated": False, "linked_nodes": len(nodes), "failed_nodes": failed, "error": str(error)}

    for node, token, remote in connected:
        if not remote or (remote["hash"] == selected["hash"] and remote["modified_at_ns"] == selected["modified_at_ns"]):
            continue
        try:
            result = _request(
                node.base_url,
                token,
                "/api/node/plugin-monitoring-settings",
                method="PUT",
                payload={"content": selected["content"], "modified_at_ns": selected["modified_at_ns"]},
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RemoteNodeError("The remote Node did not save the shared plugin settings")
            node.last_connected_at = now
            node.last_error = None
            node.outage_started_at = None
            node.offline_alert_sent = False
        except RemoteNodeUnavailable as error:
            mark_remote_node_offline(db, node, str(error), now)
            if node.name not in failed:
                failed.append(node.name)
        except RemoteFunctionUnavailable as error:
            node.last_error = str(error)[:255]
            if node.name not in failed:
                failed.append(node.name)
        except (RemoteNodeError, ValueError) as error:
            if "HTTP 409" in str(error):
                logger.info("Plugin monitoring settings sync deferred for %s while its monitor is busy", node.name)
                continue
            node.last_error = str(error)[:255]
            if node.name not in failed:
                failed.append(node.name)
        except Exception as error:
            logger.warning("Unable to send shared plugin settings to Node %s (%s)", node.name, type(error).__name__)
            node.last_error = "Unable to synchronize shared plugin settings"
            if node.name not in failed:
                failed.append(node.name)

    for node in recoveries:
        if node.outage_started_at is None:
            _email_remote_connection_alert(db, node, "restored", None)

    db.commit()
    return {
        "updated": local_changed or any(
            remote is not None and (
                remote["hash"] != selected["hash"] or remote["modified_at_ns"] != selected["modified_at_ns"]
            )
            for _node, _token, remote in connected
        ),
        "linked_nodes": len(nodes),
        "failed_nodes": failed,
    }


def run_linked_plugin_settings_sync(force=False) -> dict:
    """Periodically reconcile shared plugin defaults across linked Nodes."""
    global _last_plugin_settings_sync
    now = time.monotonic()
    if not force and now - _last_plugin_settings_sync < _plugin_settings_sync_interval:
        return {"skipped": True}
    _last_plugin_settings_sync = now

    from .database import SessionLocal
    from .update_monitor import CheckInProgress, acquire_lease
    from .models import UpdateMonitorLease

    db = SessionLocal()
    try:
        try:
            acquire_lease(db, datetime.utcnow())
        except CheckInProgress:
            return {"skipped": True}
        try:
            return synchronize_plugin_monitoring_repositories(db)
        except Exception as error:
            logger.exception("Linked plugin settings synchronization failed")
            return {"updated": False, "error": str(error)}
        finally:
            db.rollback()
            db.query(UpdateMonitorLease).filter_by(id=1).update({"expires_at": datetime.min})
            db.commit()
    finally:
        db.close()
