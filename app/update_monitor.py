"""Shared metadata cache, server comparison and deduplicated SMTP notifications."""
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from . import update_providers as providers
from .models import (Server, UpstreamUpdateCache, ServerUpdateCheck,
                     UpdateNotification, UpdateMonitorLease, User)
from .plugin_manager import filename_for_plugin_build, list_plugins
from .plugin_monitoring import monitoring_config
from .update_providers.http_source import SourceError
from .paper import inspect_paper_jar
from .emailer import send_email
from .system_alerts import _admin_addresses, hub_manages_alerts

logger = logging.getLogger(__name__)
CACHE_TTL = timedelta(hours=6)
FAILURE_TTL = timedelta(minutes=15)
FRESH_COOLDOWN = timedelta(minutes=1)
CHECK_INTERVAL = timedelta(hours=24)


class CheckInProgress(RuntimeError):
    pass


def acquire_lease(db, now):
    if db.get(UpdateMonitorLease, 1) is None:
        db.add(UpdateMonitorLease(id=1, expires_at=datetime.min))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    claimed = db.query(UpdateMonitorLease).filter(
        UpdateMonitorLease.id == 1, UpdateMonitorLease.expires_at <= now,
    ).update({'expires_at': now + timedelta(minutes=30)}, synchronize_session=False)
    db.commit()
    if not claimed:
        raise CheckInProgress('An update check is already running. Try again shortly.')


def cached_releases(db, provider, now, force=False, fetch=True, seen=None):
    row = db.get(UpstreamUpdateCache, provider.key)
    ttl = FAILURE_TTL if row and row.error else FRESH_COOLDOWN if force else CACHE_TTL
    if fetch and (not row or now - row.checked_at >= ttl) and (seen is None or provider.key not in seen):
        try:
            releases = provider.fetch()
            if not releases:
                raise ValueError('No releases')
            payload, error = json.dumps([r.to_dict() for r in releases]), None
        except Exception as exc:
            # Never publish HTTP bodies, credentials, request URLs or SMTP exceptions.
            error = str(exc) if isinstance(exc, SourceError) else 'Upstream request failed or returned invalid release metadata'
            payload = '[]'
            logger.warning('Update provider %s failed (%s)', provider.key, type(exc).__name__)
        if not row:
            row = UpstreamUpdateCache(key=provider.key)
            db.add(row)
        row.payload, row.error, row.checked_at = payload, error, now
        db.commit()
    if seen is not None:
        seen.add(provider.key)
    return row


def base_result(name, installed, provider=None):
    return dict(plugin=name, installed_version=installed, latest_version=None,
        update_available=False, release_url=None, download_url=None, release_date=None,
        provider=provider.name if provider else None, checked_at=None, error=None,
        status='Not checked' if provider else 'Unsupported/unmonitored')


def compare_release(name, installed, provider, cache, minecraft_version, filename=None):
    result = base_result(name, installed, provider)
    if not provider or not cache:
        return result
    result['checked_at'] = cache.checked_at.isoformat() + 'Z'
    if cache.error:
        result.update(status='Check failed', error=cache.error)
        return result
    try:
        release = providers.select_release([providers.Release(**r) for r in json.loads(cache.payload)], minecraft_version)
        compatible = ('Unknown' if not release.minecraft_versions or not minecraft_version else
                      'Compatible' if minecraft_version in release.minecraft_versions else 'Incompatible')
        result.update(latest_version=release.version, release_url=release.url, download_url=release.download_url, release_date=release.date)
        comparison_details = None
        if hasattr(provider, 'installed_details'):
            comparison_details = {
                'installed_value': str(installed or '')[:300],
                'installed_filename': str(filename or '')[:300],
                'release_value': str(release.version or '')[:200],
                'release_pattern': str(getattr(provider, 'version_pattern', '') or '')[:1024],
                'installed_pattern': str(getattr(provider, 'installed_pattern', '') or '')[:1024],
                'installed_source': 'JAR metadata',
            }
            try:
                matched, source = provider.installed_details(installed, filename)
                comparison_details.update(installed_comparison=str(matched)[:300], installed_source=source)
            except SourceError as exc:
                comparison_details['match_error'] = str(exc)
            result['comparison_details'] = comparison_details
        comparison = provider.compare(installed, release, filename=filename)
        if hasattr(provider, 'display_installed'):
            result['installed_version'] = provider.display_installed(installed, release, filename=filename)
        result['latest_version'] = provider.display_version(release)
        if comparison is None:
            result.update(status='Check failed', error='Installed version/build cannot be reliably compared')
        elif compatible == 'Incompatible':
            result.pop('comparison_details', None)
            result['status'] = 'Update not available'
        elif comparison > 0:
            result.pop('comparison_details', None)
            result.update(update_available=True, status='Update available')
        else:
            if comparison_details:
                suggested_filename = filename_for_plugin_build(
                    filename or '', comparison_details.get('installed_comparison', ''),
                )
                if suggested_filename:
                    result['suggested_filename'] = suggested_filename
            result.pop('comparison_details', None)
            result['status'] = 'Current'
    except SourceError as exc:
        details = result.get('comparison_details') or {}
        error = f'Installed version expression failed: {exc}' if details.get('installed_pattern') else str(exc)
        result.update(status='Check failed', error=error)
    except (TypeError, ValueError, KeyError):
        result.update(status='Check failed', error='Invalid release metadata')
    return result


def plugin_results(db, server, plugins=None, *, fetch=False, force=False, now=None, seen=None, on_progress=None):
    now = now or datetime.utcnow()
    results = []
    plugins = [
        plugin
        for plugin in (plugins if plugins is not None else list_plugins(server))
        if plugin.get('enabled') is True
    ]
    total = len(plugins)
    for index, plugin in enumerate(plugins, start=1):
        if on_progress:
            on_progress(index - 1, total, plugin['name'])
        if plugin.get('previous_version'):
            result = base_result(plugin['name'], plugin.get('version'))
            result.update(status='Previous version', component=plugin['filename'], notification_key=plugin['name'])
            results.append(result)
            if on_progress:
                on_progress(index, total, None)
            continue
        provider, config = monitoring_config(db, server.id, plugin['name'])
        cache = cached_releases(db, provider, now, force, fetch, seen) if provider else None
        result = compare_release(plugin['name'], plugin.get('version'), provider, cache, server.minecraft_version, filename=plugin['filename'])
        result['monitoring'] = config
        if config.get('error'):
            result.update(status='Check failed', error=config['error'])
        if config['mode'] == 'disabled' and config['configured']:
            result['status'] = 'Monitoring disabled'
        result['component'] = plugin['filename']
        result['notification_key'] = provider.key if provider else plugin['name']
        results.append(result)
        if on_progress:
            on_progress(index, total, None)
    return results


def paper_fingerprint(server):
    path = Path(server.directory) / server.jar_name
    try:
        stat = path.stat()
        return [server.jar_name, stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


def paper_result(db, server, *, fetch=False, force=False, now=None, seen=None):
    now = now or datetime.utcnow()
    if not fetch:
        row = db.get(ServerUpdateCheck, (server.id, '@paper'))
        result = json.loads(row.payload) if row else {}
        if result and result.get('fingerprint') == paper_fingerprint(server):
            return result
        return base_result('Paper', None, providers.Paper('unknown'))
    provider = providers.Paper(server.minecraft_version or 'unknown')
    result = base_result('Paper', None, provider)
    try:
        detected = inspect_paper_jar(Path(server.directory) / server.jar_name)
        provider = providers.Paper(detected['version'])
        cache = cached_releases(db, provider, now, force, True, seen)
        build = None
        if cache and not cache.error:
            releases = json.loads(cache.payload)
            build = releases[0].get('checksums', {}).get(detected['sha256'])
        result = compare_release('Paper', build, provider, cache, detected['version'])
        result['installed_version'] = f'{detected["version"]} build {build or "unknown"}'
        if result['latest_version']:
            result['latest_version'] = f'{detected["version"]} build {result["latest_version"]}'
        # Existing metadata stays in sync with the JAR, never trusting stale DB build numbers.
        if cache and not cache.error:
            server.paper_build = build
        server.minecraft_version = detected['version']
    except Exception as exc:
        logger.warning('Paper inspection failed for server %s (%s)', server.id, type(exc).__name__)
        result.update(status='Check failed', error='Unable to identify the installed Paper JAR', checked_at=now.isoformat() + 'Z')
    result.update(component='@paper', notification_key=provider.key, fingerprint=paper_fingerprint(server))
    return result


def notify_updates(db, grouped, now):
    if hub_manages_alerts(db, now):
        return
    # Use the same recipient policy as system alerts, further scoped to the
    # content each administrator is allowed to see.
    addresses = set(_admin_addresses(db))
    for user in db.query(User).filter(User.enabled.is_(True)).all():
        if not user.email or user.email.strip() not in addresses:
            continue
        address = user.email.strip()
        pending = {}
        for server, results in grouped:
            if not user.can('servers.view') or not (user.can('servers.view_all') or server in user.servers):
                continue
            for result in results:
                if not result['update_available'] or (result['component'] != '@paper' and not user.can('plugins.view')):
                    continue
                key = (server.id, result['notification_key'], address, result['latest_version'])
                if db.get(UpdateNotification, key) is None:
                    pending[key] = (server, result)
        if not pending:
            continue
        email_updates = []
        for server, result in pending.values():
            is_paper = result.get('component') == '@paper'
            email_updates.append({
                'server': server.name,
                'name': 'Paper' if is_paper else result['plugin'],
                'installed': result.get('installed_version'),
                'available': result.get('latest_version'),
                'path': f'/servers/{server.id}' if is_paper else f'/servers/{server.id}/plugins',
            })
        try:
            from .update_email import render_update_email
            subject, body, html_body = render_update_email(db, email_updates)
            send_email(db, address, subject, body, html_body=html_body)
        except Exception as exc:
            logger.warning('Update notification delivery failed (%s)', type(exc).__name__)
            continue
        for key in pending:
            db.add(UpdateNotification(server_id=key[0], component=key[1], recipient=key[2], version=key[3], sent_at=now))
        db.commit()


def check_updates(db, servers=None, *, force=False, notify=False, scheduled=False, now=None, scope="all", on_progress=None):
    now = now or datetime.utcnow()
    acquire_lease(db, now)
    try:
        lease = db.get(UpdateMonitorLease, 1)
        db.refresh(lease)
        if scheduled and lease.last_scheduled_at and now - lease.last_scheduled_at < CHECK_INTERVAL:
            return []
        grouped, seen = [], set()
        for server in servers if servers is not None else db.query(Server).all():
            if scope == 'plugins':
                try:
                    server.minecraft_version = inspect_paper_jar(Path(server.directory) / server.jar_name)['version']
                except (OSError, ValueError, RuntimeError):
                    pass
            paper = paper_result(db, server, fetch=True, force=force, now=now, seen=seen) if scope != 'plugins' else None
            try:
                results = plugin_results(
                    db, server, fetch=True, force=force, now=now, seen=seen,
                    on_progress=on_progress,
                ) if scope != 'paper' else []
            except OSError as exc:
                logger.warning('Plugin discovery failed for server %s (%s)', server.id, type(exc).__name__)
                results = []
            if paper:
                results.append(paper)
            for result in results:
                row = db.get(ServerUpdateCheck, (server.id, result['component']))
                if row is None:
                    row = ServerUpdateCheck(server_id=server.id, component=result['component'])
                    db.add(row)
                row.payload = json.dumps(result)
            db.commit()
            grouped.append((server, results))
        if notify:
            notify_updates(db, grouped, now)
        if scheduled:
            lease.last_scheduled_at = now
            db.commit()
        return grouped
    finally:
        db.rollback()
        db.query(UpdateMonitorLease).filter_by(id=1).update({'expires_at': datetime.min})
        db.commit()


def run_scheduled_check():
    from .database import SessionLocal
    with SessionLocal() as db:
        # Avoid taking the lease or writing on every automation poll.
        lease = db.get(UpdateMonitorLease, 1)
        if lease and lease.last_scheduled_at and datetime.utcnow() - lease.last_scheduled_at < CHECK_INTERVAL:
            return
        try:
            check_updates(db, scheduled=True, notify=True)
        except CheckInProgress:
            pass
