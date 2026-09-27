import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import admin_cli, update_monitor as monitor, update_providers as providers, web_plugins
from app.database import Base, get_db
from app.models import Server, User, UpstreamUpdateCache, UpdateNotification, UpdateMonitorLease, ServerUpdateCheck
from app.plugin_manager import list_plugins, read_plugin_yml
from app.update_providers.versions import compare_versions

NOW = datetime(2026, 9, 18, 12)
REAL_GET_JSON = providers.get_json


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAFTARR_PLUGIN_MONITORING_DEFAULTS", str(tmp_path / "no-defaults.yml"))
    def blocked(*args, **kwargs):
        raise AssertionError('Unexpected network request')
    monkeypatch.setattr(providers, 'get_json', blocked)


@pytest.fixture
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def jar(path, name, version, metadata='plugin.yml'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(metadata, f'name: "{name}"\nversion: "{version}" # version comment\ncommands:\n  name: Wrong\n')


def server(db, tmp_path, name='Survival', version='1.21.8', plugin_version='5.5.0'):
    root = tmp_path / name
    jar(root / 'plugins' / 'arbitrary-name.jar', 'ViaVersion', plugin_version)
    with zipfile.ZipFile(root / 'paper.jar', 'w') as archive:
        archive.writestr('version.json', json.dumps({'id': version}))
    row = Server(name=name, directory=str(root), service_name=name, minecraft_version=version, paper_build='999')
    db.add(row)
    db.commit()
    from app.plugin_monitoring import save_monitoring_config
    save_monitoring_config(db, row.id, 'ViaVersion', 'custom', 'github', 'ViaVersion/ViaVersion')
    return row


def upstream(monkeypatch, servers, latest='5.6.0', build=61):
    calls = []
    def get(url):
        calls.append(url)
        if 'fill.papermc.io' in url:
            return [{'id': 42, 'channel': 'STABLE', 'downloads': {
                str(s.id): {'checksums': {'sha256': hashlib.sha256((Path(s.directory) / 'paper.jar').read_bytes()).hexdigest()}}
                for s in servers}}, {'id': build, 'channel': 'STABLE'}]
        if 'viaversion' in url.lower():
            return {'tag_name': latest, 'published_at': '2026-09-18T00:00:00Z'}
        raise httpx.ReadTimeout('sensitive upstream message')
    monkeypatch.setattr(providers, 'get_json', get)
    return calls


@pytest.mark.parametrize('metadata', ['plugin.yml', 'paper-plugin.yml'])
def test_installed_metadata_and_normalization(tmp_path, metadata):
    jar(tmp_path / 'plugins' / 'renamed.jar.disabled', 'FastAsyncWorldEdit', '2.15.4', metadata)
    result = list_plugins(SimpleNamespace(directory=tmp_path))[0]
    assert result['name'] == 'FastAsyncWorldEdit'
    assert result['version'] == '2.15.4'
    assert result['enabled'] is False
    assert providers.normalize_name(result['name']) == 'fastasyncworldedit'
    assert providers.normalize_name('Geyser-Spigot') == 'geyserspigot'


def test_bad_and_oversized_jar_metadata(tmp_path):
    path = tmp_path / 'bad.jar'
    path.write_text('not a jar')
    assert read_plugin_yml(path) == {}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('plugin.yml', 'name: ' + 'a' * 70000)
    assert read_plugin_yml(path) == {}


@pytest.mark.parametrize('installed,latest,expected', [
    ('5.9', '5.10', 1), ('v1.2.0', '1.2', 0), ('1.3', '1.2', -1),
    ('1.2-rc.1', '1.2', 1), ('1.2-beta.2', '1.2-rc.1', 1),
    ('1.2', '1.2-rc.1', -1), ('1.2-SNAPSHOT', '1.2', 1),
    ('1.2+abc', '1.2+def', 0), ('1.7.3-b131', '1.7.3', -1),
    ('42', '61', 1), ('release-unknown', '1.2', None), (None, '1.2', None),
    ('1.2-b9', '1.2-b10', 1), ('1.2.0', '1.2.0.1', 1),
])
def test_version_ordering(installed, latest, expected):
    assert compare_versions(installed, latest) == expected


def test_github_parsing_and_untrusted_links(monkeypatch):
    monkeypatch.setattr(providers, 'get_json', lambda url: {'tag_name': 'v1.2', 'html_url': 'javascript:bad', 'published_at': '2026-09-18'})
    release = providers.GitHub('MilkBowl/Vault').fetch()[0]
    assert release.version == 'v1.2'
    assert release.url == 'https://github.com/MilkBowl/Vault/releases/tag/v1.2'
    monkeypatch.setattr(providers, 'get_json', lambda url: {'tag_name': 'v1.3-rc1', 'prerelease': True})
    with pytest.raises(ValueError):
        providers.GitHub('MilkBowl/Vault').fetch()


def test_modrinth_platform_stability_and_compatibility(monkeypatch):
    def release(version, mc, loader='paper', channel='release'):
        return dict(version_number=version, game_versions=[mc], loaders=[loader], version_type=channel, id='safe', date_published='2026-09-18')
    monkeypatch.setattr(providers, 'get_json', lambda url: [release('1.4.40', '1.21.8'), release('1.5', '26.1'), release('9', '1.21.8', 'fabric'), release('2', '1.21.8', channel='beta')])
    releases = providers.Modrinth('chunky').fetch()
    assert len(releases) == 2
    assert providers.select_release(releases, '1.21.8').version == '1.4.40'
    assert providers.select_release(releases, '26.1').version == '1.5'






@pytest.mark.parametrize('mc,versions,installed,status,available', [
    ('1.21.8', ['1.21.8'], '1', 'Update available', True),
    ('1.21.8', ['1.21.7'], '1', 'Incompatible', False),
    ('1.21.8', None, '1', 'Compatibility unknown', True),
    (None, ['1.21.8'], '1', 'Compatibility unknown', True),
    ('1.21.8', ['1.21.8'], '2', 'Current', False),
    ('1.21.8', None, '2', 'Current', False),
    ('1.21.8', None, 'unknown', 'Check failed', False),
])
def test_comparison_results(mc, versions, installed, status, available):
    release = providers.Release('2', 'https://example.org', minecraft_versions=versions)
    row = SimpleNamespace(payload=json.dumps([release.to_dict()]), checked_at=NOW, error=None)
    result = monitor.compare_release('Test', installed, providers.Modrinth('test'), row, mc)
    assert result['status'] == status
    assert result['update_available'] is available


def test_failure_isolation_cache_and_multiple_servers(db, tmp_path, monkeypatch):
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative', plugin_version='5.6.0')
    jar(tmp_path / 'Survival/plugins/other.jar', 'Vault', '1.7.2')
    from app.plugin_monitoring import save_monitoring_config
    save_monitoring_config(db, first.id, 'Vault', 'custom', 'github', 'MilkBowl/Vault')
    jar(tmp_path / 'Survival/plugins/unknown.jar', 'CustomPlugin', '9')
    calls = upstream(monkeypatch, [first, second])
    grouped = monitor.check_updates(db, now=NOW, force=True)
    assert len(calls) == 3  # one shared Paper request, one ViaVersion, one failed Vault
    results = {r['plugin']: r for r in grouped[0][1]}
    assert results['ViaVersion']['update_available']
    assert results['Vault']['status'] == 'Check failed'
    assert 'sensitive' not in str(results)
    assert results['CustomPlugin']['status'] == 'Unsupported/unmonitored'
    assert next(r for r in grouped[1][1] if r['plugin'] == 'ViaVersion')['status'] == 'Current'
    monitor.check_updates(db, now=NOW + timedelta(minutes=2))
    assert len(calls) == 3
    # Failure cooldown is retained even on explicit fresh requests.
    monitor.check_updates(db, force=True, now=NOW + timedelta(minutes=3))
    assert len(calls) == 5
    monitor.check_updates(db, now=NOW + timedelta(hours=7))
    assert len(calls) == 8


def test_paper_uses_actual_jar_checksum_not_recorded_build(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    upstream(monkeypatch, [row])
    results = monitor.check_updates(db, now=NOW)[0][1]
    paper = results[-1]
    assert paper['installed_version'] == '1.21.8 build 42'
    assert paper['latest_version'] == '1.21.8 build 61'
    assert paper['update_available'] is True
    assert row.paper_build == '42'
    assert paper['compatibility'] == 'Compatible'
    assert monitor.paper_result(db, row) == paper
    (tmp_path / 'Survival/paper.jar').write_bytes(b'replaced')
    assert monitor.paper_result(db, row)['status'] == 'Not checked'


def test_paper_unknown_checksum_and_unstable_builds(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    monkeypatch.setattr(providers, 'get_json', lambda url: [{'id': 42, 'channel': 'STABLE'}, {'id': 99, 'channel': 'ALPHA'}])
    result = monitor.paper_result(db, row, fetch=True, now=NOW)
    assert result['latest_version'] == '1.21.8 build 42'
    assert result['status'] == 'Check failed'
    assert result['update_available'] is False


def admin(db, name='admin', enabled=True):
    user = User(username=name, password_hash='hash', role='admin', enabled=enabled, email=f'{name}@example.org')
    db.add(user)
    db.commit()
    return user


def test_grouped_notifications_deduplicate_and_report_newer(db, tmp_path, monkeypatch):
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    admin(db)
    admin(db, 'disabled', enabled=False)
    calls = upstream(monkeypatch, [first, second])
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args[1:]))
    monitor.check_updates(db, now=NOW, notify=True)
    assert len(sent) == 1
    assert sent[0][1] == 'Craftarr: Plugin updates available'
    assert all(text in sent[0][2] for text in ['Survival', 'Creative', 'ViaVersion', 'Paper', '5.6.0', 'build 61', 'https://github.com'])
    assert db.query(UpdateNotification).count() == 4
    monitor.check_updates(db, now=NOW + timedelta(days=1), notify=True)
    assert len(sent) == 1
    upstream(monkeypatch, [first, second], latest='5.7.0')
    monitor.check_updates(db, now=NOW + timedelta(days=2), notify=True)
    assert len(sent) == 2
    assert '5.7.0' in sent[-1][2]
    assert ': Paper' not in sent[-1][2]
    jar(tmp_path / 'Survival/plugins/arbitrary-name.jar', 'ViaVersion', '5.7.0')
    # Cached UI is compared against the current JAR, not an old check snapshot.
    assert monitor.plugin_results(db, first)[0]['status'] == 'Current'


def test_manual_check_does_not_consume_notification(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    admin(db)
    upstream(monkeypatch, [row])
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args))
    monitor.check_updates(db, now=NOW)
    assert not sent
    assert db.query(UpdateNotification).count() == 0
    monitor.check_updates(db, now=NOW, notify=True)
    assert len(sent) == 1


def test_partial_smtp_failure_retries_only_failed_recipient(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    admin(db)
    admin(db, 'second')
    upstream(monkeypatch, [row])
    sent = []
    def send(db, recipient, *args):
        if recipient.startswith('second'):
            raise RuntimeError('password=secret')
        sent.append(recipient)
    monkeypatch.setattr(monitor, 'send_email', send)
    monitor.check_updates(db, now=NOW, notify=True)
    monkeypatch.setattr(monitor, 'send_email', lambda db, recipient, *args: sent.append(recipient))
    monitor.check_updates(db, now=NOW, notify=True)
    assert sent == ['admin@example.org', 'second@example.org']


def test_schedule_persists_across_runs_and_lease_excludes_concurrent_checks(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    calls = upstream(monkeypatch, [row])
    monitor.check_updates(db, scheduled=True, now=NOW)
    assert monitor.check_updates(db, scheduled=True, now=NOW + timedelta(hours=23)) == []
    assert len(calls) == 2
    assert monitor.check_updates(db, scheduled=True, now=NOW + timedelta(hours=24))
    assert len(calls) == 4
    monitor.acquire_lease(db, NOW + timedelta(days=2))
    with pytest.raises(monitor.CheckInProgress):
        monitor.check_updates(db, now=NOW + timedelta(days=2))


def test_cli_one_server_all_and_explicit_notifications(db, tmp_path, monkeypatch, capsys):
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    admin(db)
    upstream(monkeypatch, [first, second])
    monkeypatch.setattr(admin_cli, 'SessionLocal', lambda: db)
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args))
    monkeypatch.setattr(sys, 'argv', ['console', 'check-updates', '--server', 'Survival'])
    assert admin_cli.main() == 0
    output = capsys.readouterr().out
    assert 'Survival' in output and 'Creative' not in output and 'Paper' in output and '5.6.0' in output
    assert not sent
    monkeypatch.setattr(sys, 'argv', ['console', 'check-updates', '--notify'])
    assert admin_cli.main() == 0
    assert 'Creative' in capsys.readouterr().out
    assert len(sent) == 1


@pytest.mark.parametrize('path,method,permission', [
    ('plugins/check-updates', 'post', 'plugins.manage'),
    ('paper/check-updates', 'post', 'servers.properties'),
    ('paper/update-status', 'get', 'servers.view'),
    ('plugins', 'get', 'plugins.view'),
])
def test_web_permissions(db, tmp_path, monkeypatch, path, method, permission):
    row = server(db, tmp_path)
    upstream(monkeypatch, [row])
    app = FastAPI()
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: db
    selected = [None, row]
    monkeypatch.setattr(web_plugins, 'get_accessible_server', lambda *args: selected)
    monkeypatch.setattr(web_plugins, 'server_status', lambda *args: {})
    client = TestClient(app)
    request = getattr(client, method)
    url = f'/api/web/servers/{row.id}/{path}'
    assert request(url).status_code == 401
    selected[0] = SimpleNamespace(enabled=True, role='user', access_role=None, can=lambda p: False)
    assert request(url).status_code == 403
    selected[0].can = lambda p: p == permission
    selected[1] = None
    assert request(url).status_code == 403  # assignment/server access is required too
    selected[1] = row
    assert request(url).status_code == 200


def test_http_timeout_redirects_and_bounded_body(monkeypatch):
    # Exercise the real transport helper with httpx MockTransport, never live HTTP.
    client = httpx.Client
    seen = []
    def request(req):
        seen.append(req)
        return httpx.Response(302, headers={'Location': 'http://127.0.0.1/secret'})
    monkeypatch.setattr(providers.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    with pytest.raises(httpx.HTTPStatusError):
        REAL_GET_JSON('https://api.github.com/repos/MilkBowl/Vault/releases/latest')
    assert len(seen) == 1
    assert seen[0].extensions['timeout']['read'] == 15


def test_transport_parses_json_times_out_and_limits_body(monkeypatch):
    client = httpx.Client
    response = [httpx.Response(200, json={'version': '1.2.3'})]
    def handler(request):
        if isinstance(response[0], Exception):
            raise response[0]
        return response[0]
    monkeypatch.setattr(providers.httpx, 'Client', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    assert REAL_GET_JSON('https://api.modrinth.com/v2/project/chunky/version') == {'version': '1.2.3'}
    response[0] = httpx.ReadTimeout('private error body')
    with pytest.raises(httpx.ReadTimeout):
        REAL_GET_JSON('https://api.modrinth.com/v2/project/chunky/version')
    response[0] = httpx.Response(200, content=b'x' * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='size limit'):
        REAL_GET_JSON('https://api.modrinth.com/v2/project/chunky/version')


def test_provider_malformed_data_fails_safely_and_recovers(db, tmp_path, monkeypatch, caplog):
    row = server(db, tmp_path)
    provider = providers.GitHub('ViaVersion/ViaVersion')
    monkeypatch.setattr(providers, 'get_json', lambda url: {'tag_name': 'secret\nmalformed'})
    cache = monitor.cached_releases(db, provider, NOW)
    assert cache.error
    assert 'secret' not in caplog.text
    assert 'GitHub Releases:viaversion/viaversion' in caplog.text
    calls = upstream(monkeypatch, [row])
    cache = monitor.cached_releases(db, provider, NOW + timedelta(minutes=16))
    assert cache.error is None
    assert len(calls) == 1


def test_current_paper_and_plugin_do_not_notify(db, tmp_path, monkeypatch):
    row = server(db, tmp_path, plugin_version='5.6.0')
    upstream(monkeypatch, [row], build=42)
    admin(db)
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args))
    results = monitor.check_updates(db, now=NOW, notify=True)[0][1]
    assert all(result['status'] == 'Current' for result in results)
    assert not sent


def test_unsupported_and_failed_sources_do_not_notify(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    jar(tmp_path / 'Survival/plugins/custom.jar', 'Custom', '1')
    admin(db)
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args))
    def timeout(url):
        raise httpx.ReadTimeout('token-secret')
    monkeypatch.setattr(providers, 'get_json', timeout)
    monitor.check_updates(db, now=NOW, notify=True)
    assert not sent
    assert db.query(UpdateNotification).count() == 0


def test_notifications_respect_admin_content_and_server_access(db, tmp_path, monkeypatch):
    from app.models import AccessRole, Permission
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    permissions = [Permission(key=key, label=key) for key in ['settings.manage', 'servers.view']]
    role = AccessRole(name='Scoped administrator', permissions=permissions)
    user = User(username='scoped', password_hash='hash', enabled=True, email='scoped@example.org', access_role=role, servers=[first])
    db.add(user)
    db.commit()
    upstream(monkeypatch, [first, second])
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args[3]))
    monitor.check_updates(db, now=NOW, notify=True)
    assert len(sent) == 1
    assert 'Survival: Paper' in sent[0]
    assert 'Creative' not in sent[0]
    assert 'ViaVersion' not in sent[0]


def test_read_only_views_never_fetch_upstream(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    assert monitor.plugin_results(db, row)[0]['status'] == 'Not checked'
    upstream(monkeypatch, [row])
    monitor.check_updates(db, now=NOW)
    def forbidden(*args):
        pytest.fail('A read-only view must not request upstream metadata')
    monkeypatch.setattr(providers, 'get_json', forbidden)
    assert monitor.plugin_results(db, row, now=NOW + timedelta(days=10))[0]['update_available']
    assert monitor.paper_result(db, row)['update_available']


def test_scheduler_entrypoint_reuses_persisted_daily_state(db, tmp_path, monkeypatch):
    from app import database
    row = server(db, tmp_path)
    calls = upstream(monkeypatch, [row])
    monkeypatch.setattr(database, 'SessionLocal', lambda: db)
    monitor.run_scheduled_check()
    monitor.run_scheduled_check()
    assert len(calls) == 2
    assert db.get(UpdateMonitorLease, 1).last_scheduled_at is not None


def test_fresh_cooldown_and_persisted_cache_after_new_session(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    calls = upstream(monkeypatch, [row])
    monitor.check_updates(db, now=NOW, force=True)
    with sessionmaker(bind=db.bind)() as other:
        monitor.check_updates(other, now=NOW + timedelta(seconds=30), force=True)
        assert len(calls) == 2
        monitor.check_updates(other, now=NOW + timedelta(seconds=61), force=True)
        assert len(calls) == 4






def test_bad_paper_jar_does_not_block_plugin_results(db, tmp_path, monkeypatch):
    row = server(db, tmp_path)
    upstream(monkeypatch, [row])
    (tmp_path / 'Survival/paper.jar').unlink()
    results = monitor.check_updates(db, now=NOW)[0][1]
    assert results[0]['update_available']
    assert results[-1]['status'] == 'Check failed'
    assert monitor.paper_result(db, row)['status'] == 'Check failed'


def test_migration_upgrades_previous_head_and_preserves_servers(monkeypatch, tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from app import migrations
    path = tmp_path / 'migration.db'
    monkeypatch.setenv('CRAFTARR_CONSOLE_DATABASE', str(path))
    config = Config(str(migrations.PROJECT_ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(migrations.PROJECT_ROOT / 'migrations'))
    command.upgrade(config, '5bb3c8a91120')
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO servers(name,directory,service_name,port,enabled,memory,plugins_dirty) VALUES ('Survival','/srv/survival','survival',25565,1,'2G',0)")
    command.upgrade(config, 'head')
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT name FROM servers').fetchone()[0] == 'Survival'
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'upstream_update_cache', 'server_update_checks', 'update_notifications', 'update_monitor_lease', 'plugin_monitoring_settings'} <= tables
    command.downgrade(config, '5bb3c8a91120')
    command.upgrade(config, 'head')


def test_luckperms_official_platform_version_suffix(monkeypatch):
    monkeypatch.setattr(providers, 'get_json', lambda url: [
        {'version_number': 'v5.5.71-bukkit', 'version_type': 'release', 'loaders': ['bukkit', 'paper'],
         'game_versions': ['1.21.8'], 'id': 'release71'},
        {'version_number': 'v5.5.53-bukkit', 'version_type': 'release', 'loaders': ['bukkit', 'paper'],
         'game_versions': ['1.21.8'], 'id': 'release53'},
    ])
    from app.plugin_monitoring import custom_provider
    provider = custom_provider('modrinth', 'luckperms', r'^v?([0-9.]+)')
    selected = providers.select_release(provider.fetch(), '1.21.8')
    assert selected.version == '5.5.71'
    assert provider.compare('5.5.53', selected) == 1
    assert provider.compare('5.5.71', selected) == 0
    assert selected.url.endswith('/release71')


@pytest.mark.parametrize('kind,project', [
    ('github', 'https://evil.test/repo'), ('github', '../repo'),
    ('github', 'owner/../../secret'), ('github', 'owner/repo?url=x'),
    ('modrinth', 'https://example.org/plugin'), ('modrinth', '../project'),
    ('modrinth', 'project%2fsecret'), ('modrinth', 'project#fragment'),
    ('geyser', 'other-project'), ('citizens', 'OtherJob'), ('unknown', 'project'),
    ('github', None), ([], 'project'),
])
def test_custom_monitoring_rejects_unsafe_projects(kind, project):
    from app.plugin_monitoring import custom_provider
    with pytest.raises(ValueError):
        custom_provider(kind, project)


def test_monitoring_settings_disable_override_reset_and_server_scope(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import save_monitoring_config
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    admin(db)
    calls = upstream(monkeypatch, [first, second])
    sent = []
    monkeypatch.setattr(monitor, 'send_email', lambda *args: sent.append(args[3]))
    save_monitoring_config(db, first.id, 'ViaVersion', 'disabled')
    grouped = monitor.check_updates(db, now=NOW, notify=True, scope='plugins')
    assert grouped[0][1][0]['status'] == 'Monitoring disabled'
    assert grouped[0][1][0]['update_available'] is False
    assert grouped[1][1][0]['update_available'] is True
    assert len(calls) == 1
    assert 'Survival' not in sent[0] and 'Creative' in sent[0]
    # Configuration belongs to the plugin metadata name, not the filename/version.
    (tmp_path / 'Survival/plugins/arbitrary-name.jar').unlink()
    jar(tmp_path / 'Survival/plugins/replacement.jar', 'ViaVersion', '5.7.0')
    assert monitor.plugin_results(db, first)[0]['status'] == 'Monitoring disabled'
    save_monitoring_config(db, first.id, 'ViaVersion', 'custom', 'github', 'Example/Replacement')
    calls.clear()
    def custom_source(url):
        calls.append(url)
        return {'tag_name': '5.8.0'}
    monkeypatch.setattr(providers, 'get_json', custom_source)
    result = monitor.check_updates(db, [first], scope='plugins', now=NOW)[0][1][0]
    assert calls == ['https://api.github.com/repos/example/replacement/releases/latest']
    assert result['latest_version'] == '5.8.0'
    assert result['monitoring']['mode'] == 'custom'
    save_monitoring_config(db, first.id, 'ViaVersion', 'custom', 'github', 'ViaVersion/ViaVersion')
    result = monitor.plugin_results(db, first)[0]
    assert result['monitoring']['mode'] == 'custom'
    assert result['monitoring']['source'] == 'GitHub Releases: viaversion/viaversion'
    assert result['latest_version'] == '5.6.0'


def test_custom_source_supports_unknown_plugin_and_shares_cache(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import save_monitoring_config
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    for row in [first, second]:
        save_monitoring_config(db, row.id, 'ViaVersion', 'disabled')
        jar(Path(row.directory) / 'plugins/custom.jar', 'NewPlugin', '1.0')
        save_monitoring_config(db, row.id, 'NewPlugin', 'custom', 'modrinth', 'new-plugin')
    calls = []
    def source(url):
        calls.append(url)
        return [{'version_number': '1.1', 'version_type': 'release', 'loaders': ['paper'],
                 'game_versions': ['1.21.8'], 'id': 'new-release'}]
    monkeypatch.setattr(providers, 'get_json', source)
    results = monitor.check_updates(db, scope='plugins', now=NOW)
    assert len(calls) == 1
    assert all(next(item for item in items if item['plugin'] == 'NewPlugin')['update_available'] for _, items in results)


def test_monitoring_setting_api_permissions_validation_and_persistence(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import monitoring_config
    row = server(db, tmp_path)
    app = FastAPI()
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: db
    user = SimpleNamespace(enabled=True, role='user', access_role=None, can=lambda p: p == 'plugins.view')
    selected = [None, row]
    monkeypatch.setattr(web_plugins, 'get_accessible_server', lambda *args: selected)
    client = TestClient(app)
    url = f'/api/web/servers/{row.id}/plugins/monitoring'
    body = {'filename': 'arbitrary-name.jar', 'mode': 'disabled'}
    assert client.post(url, json=body).status_code == 401
    selected[0] = user
    assert client.post(url, json=body).status_code == 403
    user.can = lambda p: p == 'plugins.manage'
    selected[1] = None
    assert client.post(url, json=body).status_code == 403
    selected[1] = row
    assert client.post(url, json=[]).status_code == 400
    assert client.post(url, json={**body, 'filename': 'not-installed.jar'}).status_code == 404
    assert client.post(url, json={**body, 'mode': 'invalid'}).status_code == 400
    assert client.post(url, json={**body, 'mode': 'custom', 'provider': 'github', 'project': 'https://localhost/secret'}).status_code == 400
    assert monitoring_config(db, row.id, 'ViaVersion')[1]['mode'] == 'custom'
    assert client.post(url, json=body).status_code == 200
    with sessionmaker(bind=db.bind)() as other:
        assert monitoring_config(other, row.id, 'ViaVersion')[1]['mode'] == 'disabled'
    monitor.acquire_lease(db, datetime.utcnow())
    assert client.post(url, json=body).status_code == 409




def test_configured_github_repositories_share_cache(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import save_monitoring_config
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    save_monitoring_config(db, second.id, 'ViaVersion', 'custom', 'github', 'viaversion/viaversion')
    calls = upstream(monkeypatch, [first, second])
    results = monitor.check_updates(db, scope='plugins', now=NOW, force=True)
    assert len(calls) == 1
    assert all(items[0]['update_available'] for _, items in results)


def test_plugins_without_defaults_file_have_no_implicit_sources(db, tmp_path):
    from app.plugin_monitoring import monitoring_config, save_monitoring_config
    row = server(db, tmp_path)
    for name in ['Geyser', 'LuckPerms', 'Citizens', 'FAWE', 'AntiPopup', 'NewPlugin']:
        provider, config = monitoring_config(db, row.id, name)
        assert provider is None
        assert config['mode'] == 'disabled'
    with pytest.raises(ValueError):
        save_monitoring_config(db, row.id, 'ViaVersion', 'automatic')
    assert not hasattr(providers, 'MAPPINGS')
    assert not hasattr(providers, 'provider_for')


@pytest.mark.parametrize('kind,project,expected', [
    ('github', 'https://github.com/Example/Plugin/', 'example/plugin'),
    ('modrinth', 'https://modrinth.com/plugin/example', 'example'),
    ('modrinth', 'example', 'example'),
])
def test_provider_url_configuration(kind, project, expected):
    from app.plugin_monitoring import custom_provider
    assert custom_provider(kind, project).project == expected


def test_custom_extraction_preview_and_normalized_installed(monkeypatch):
    from app.plugin_monitoring import custom_provider
    from app.update_providers import configured
    calls = []
    def fetch(url):
        calls.append(url)
        return '{"version":"2.8.3", "link":"/releases/plugin.jar?one=1&amp;two=2"}'
    monkeypatch.setattr(configured, 'fetch_document', fetch)
    source = custom_provider('custom', 'https://releases.example.org/latest',
                             r'"version":"(?P<version>[^\"]+)"', r'"link":"(?P<url>[^\"]+)"', r'^([0-9.]+)')
    release = source.fetch()[0]
    assert release.version == '2.8.3'
    assert release.download_url == 'https://releases.example.org/releases/plugin.jar?one=1&two=2'
    assert source.compare('2.8.2-SNAPSHOT', release) == 1
    assert source.installed_value('2.8.2-SNAPSHOT') == '2.8.2'
    assert calls == ['https://releases.example.org/latest']  # no download link fetched


def test_jenkins_generic_job_metadata(monkeypatch):
    from app.plugin_monitoring import custom_provider
    from app.update_providers import configured
    calls = []
    def fetch(url):
        calls.append(url)
        if '/api/json?tree=builds' in url:
            return json.dumps({'builds': []})
        return json.dumps({'number': 41, 'timestamp': 1700000000000,
                           'artifacts': [{'fileName': 'MyPlugin-1.4.0.jar', 'relativePath': 'target/MyPlugin-1.4.0.jar'}]})
    monkeypatch.setattr(configured, 'fetch_document', fetch)
    source = custom_provider('jenkins', 'https://ci.example.org/job/Folder/job/Plugin/',
                             r'MyPlugin-([0-9.]+)\.jar')
    release = source.fetch()[0]
    assert release.version == '1.4.0'
    assert release.url == 'https://ci.example.org/job/Folder/job/Plugin/41/'
    assert release.download_url == 'https://ci.example.org/job/Folder/job/Plugin/41/artifact/target/MyPlugin-1.4.0.jar'
    assert len(calls) == 2
    assert '/lastSuccessfulBuild/api/json?' in calls[1]


def test_jenkins_selects_paper_artifact_and_maps_snapshot_commit_to_build(monkeypatch):
    from app.plugin_monitoring import custom_provider
    from app.update_providers import configured

    def fetch(url):
        if '/api/json?tree=builds' in url:
            return json.dumps({'builds': [
                {'number': 1389, 'result': 'SUCCESS', 'changeSet': {'items': [{'id': '944c416b0a7cc735a08c11201d348ea61b80c368'}]}},
                {'number': 1387, 'result': 'SUCCESS', 'changeSet': {'items': [{'id': 'cb030faa985ea8add7f254f23ca9634dbfc12a11'}]}},
            ]})
        return json.dumps({'number': 1389, 'timestamp': 1700000000000, 'artifacts': [
            {'fileName': 'FastAsyncWorldEdit-Bukkit-2.15.5-SNAPSHOT.jar', 'relativePath': 'artifacts/FastAsyncWorldEdit-Bukkit-2.15.5-SNAPSHOT.jar'},
            {'fileName': 'FastAsyncWorldEdit-CLI-2.15.5-SNAPSHOT.jar', 'relativePath': 'artifacts/FastAsyncWorldEdit-CLI-2.15.5-SNAPSHOT.jar'},
            {'fileName': 'FastAsyncWorldEdit-Paper-2.15.5-SNAPSHOT.jar', 'relativePath': 'artifacts/FastAsyncWorldEdit-Paper-2.15.5-SNAPSHOT.jar'},
        ]})

    monkeypatch.setattr(configured, 'fetch_document', fetch)
    source = custom_provider('jenkins', 'https://ci.athion.net/job/FastAsyncWorldEdit/')
    release = source.fetch()[0]
    assert release.version == '1389'
    assert release.download_url.endswith('/artifact/artifacts/FastAsyncWorldEdit-Paper-2.15.5-SNAPSHOT.jar')
    assert source.compare('2.15.5-SNAPSHOT+cb030faa985ea8add7f254f23ca9634dbfc12a11', release) == 1
    assert source.compare('2.15.5-SNAPSHOT+944c416b0a7cc735a08c11201d348ea61b80c368', release) == 0


@pytest.mark.parametrize('pattern,document,error', [
    ('version', 'version', 'capture group'), ('(', '', 'Invalid extraction'),
    ('(not-here)', '1.2', 'did not match'), ('()', '1.2', 'empty value'),
])
def test_extraction_validation(pattern, document, error):
    from app.update_providers.configured import extract
    with pytest.raises(ValueError, match=error):
        extract(pattern, document)


def test_regex_execution_has_timeout():
    from app.update_providers.configured import extract
    with pytest.raises(ValueError, match='time limit'):
        extract('(a+)+$', 'a' * 100000 + '!')


@pytest.mark.parametrize('url', [
    'http://example.org', 'file:///etc/passwd', 'https://localhost/',
    'https://127.0.0.1/', 'https://[::1]/', 'https://169.254.169.254/',
    'https://10.0.0.1/', 'https://192.168.1.1/', 'https://224.0.0.1/',
    'https://example.org:444/', 'https://user:password@example.org/',
    'https://example.org/\r\nInjected: yes', 'https://example.org/#fragment',
    'https://example.org/?access_token=secret', 'https://example.org/?api_key=secret',
])
def test_custom_source_url_validation(url):
    from app.update_providers.http_source import validate_url
    with pytest.raises(ValueError):
        validate_url(url)


def test_private_dns_and_mixed_dns_are_blocked(monkeypatch):
    from app.update_providers import http_source
    for ips in [['127.0.0.1'], ['93.184.216.34', '10.0.0.1']]:
        monkeypatch.setattr(http_source.socket, 'getaddrinfo', lambda *args, **kwargs: [(2, 1, 6, '', (ip, 443)) for ip in ips])
        with pytest.raises(ValueError, match='public IP'):
            http_source.public_addresses('evil.example.org')


def test_pinned_tls_connect_preserves_certificate_hostname(monkeypatch):
    from app.update_providers import http_source
    calls = []
    raw = SimpleNamespace(close=lambda: None)
    context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: calls.append(('tls', sock, server_hostname)))
    monkeypatch.setattr(http_source.ssl, 'create_default_context', lambda: context)
    monkeypatch.setattr(http_source.socket, 'create_connection', lambda address, timeout: calls.append(('connect', address, timeout)) or raw)
    # HTTPSConnection checks context ALPN only when it creates its own context.
    connection = http_source.PinnedHTTPSConnection('releases.example.org', '93.184.216.34')
    connection.connect()
    assert calls == [('connect', ('93.184.216.34', 443), 10), ('tls', raw, 'releases.example.org')]


@pytest.mark.parametrize('status,headers,body,error', [
    (302, {'Location': 'http://127.0.0.1/'}, b'', 'redirects'),
    (200, {'Content-Type': 'application/java-archive'}, b'jar', 'not an artifact'),
    (200, {'Content-Type': 'text/plain', 'Content-Length': str(2 * 1024 * 1024)}, b'', '1 MiB'),
    (200, {'Content-Type': 'text/plain'}, b'x' * (1024 * 1024 + 1), '1 MiB'),
    (200, {'Content-Type': 'text/plain'}, b'PK\x03\x04jar', 'archive'),
    (200, {'Content-Type': 'text/plain', 'Content-Encoding': 'gzip'}, b'', 'Compressed'),
])
def test_public_metadata_transport_limits(monkeypatch, status, headers, body, error):
    import io
    from app.update_providers import http_source
    stream = io.BytesIO(body)
    response = SimpleNamespace(status=status, getheader=lambda name, default=None: headers.get(name, default), read1=stream.read)
    calls = []
    connection = SimpleNamespace(request=lambda *args, **kwargs: calls.append(args), getresponse=lambda: response, close=lambda: calls.append('closed'))
    monkeypatch.setattr(http_source, 'public_addresses', lambda host: ['93.184.216.34'])
    monkeypatch.setattr(http_source, 'PinnedHTTPSConnection', lambda host, address: connection)
    with pytest.raises(ValueError, match=error):
        http_source.fetch_document('https://example.org/releases')
    assert calls[-1] == 'closed'
    assert len(calls) == 2  # exactly one request; no redirect request


def test_preview_permissions_no_persistence_and_matches_saved_check(db, tmp_path, monkeypatch):
    from app.models import PluginMonitoringSetting
    from app.update_providers import configured
    row = server(db, tmp_path)
    calls = []
    monkeypatch.setattr(configured, 'fetch_document', lambda url: calls.append(url) or '{"version":"5.7.0","url":"https://example.org/plugin.jar"}')
    app = FastAPI()
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: db
    user = SimpleNamespace(enabled=True, role='user', access_role=None, can=lambda p: p == 'plugins.view')
    access = [None, row]
    monkeypatch.setattr(web_plugins, 'get_accessible_server', lambda *args: access)
    client = TestClient(app)
    url = f'/api/web/servers/{row.id}/plugins/monitoring'
    body = dict(filename='arbitrary-name.jar', mode='custom', provider='custom', project='https://example.org/releases',
                version_pattern=r'"version":"([^\"]+)"', link_pattern=r'"url":"([^\"]+)"')
    assert client.post(url + '/preview', json=body).status_code == 401
    access[0] = user
    assert client.post(url + '/preview', json=body).status_code == 403
    user.can = lambda p: p == 'plugins.manage'
    access[1] = None
    assert client.post(url + '/preview', json=body).status_code == 403
    access[1] = row
    assert not calls
    response = client.post(url + '/preview', json=body)
    assert response.status_code == 200
    result = response.json()
    assert result['latest_version'] == '5.7.0'
    assert result['download_url'] == 'https://example.org/plugin.jar'
    assert result['compatibility'] == 'Unknown'
    assert result['update_available']
    assert db.get(PluginMonitoringSetting, (row.id, 'viaversion')).provider == 'github'
    assert db.query(UpstreamUpdateCache).count() == 0
    assert db.query(ServerUpdateCheck).count() == 0
    assert db.query(UpdateNotification).count() == 0
    assert client.post(url, json=body).status_code == 200
    saved = monitor.check_updates(db, [row], scope='plugins', now=NOW)[0][1][0]
    assert saved['latest_version'] == result['latest_version']
    assert saved['download_url'] == result['download_url']
    assert saved['update_available'] == result['update_available']
    bad = client.post(url + '/preview', json={**body, 'version_pattern': '(no-match)'})
    assert bad.status_code == 400 and 'did not match' in bad.json()['error']


def test_rule_changes_invalidate_cache_but_installed_expression_does_not():
    from app.plugin_monitoring import custom_provider
    first = custom_provider('custom', 'https://example.org/', '(1.2)')
    second = custom_provider('custom', 'https://example.org/', '(1.3)')
    third = custom_provider('custom', 'https://example.org/', '(1.2)', installed_pattern='(1.0)')
    assert first.key != second.key
    assert first.key == third.key


def test_jenkins_filename_build_is_displayed_when_plugin_metadata_has_no_version():
    from app.plugin_monitoring import custom_provider
    source = custom_provider('jenkins', 'https://ci.example.org/job/Citizens')
    release = providers.Release('4255', 'https://ci.example.org/job/Citizens/4255/')
    filename = 'Citizens-2.0.43-b4241.jar'

    assert source.compare(None, release, filename=filename) == 1
    assert source.display_installed(None, release, filename=filename) == 'build 4241'


def test_explicit_settings_migrate_without_plugin_defaults(monkeypatch, tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from app import migrations
    path = tmp_path / 'source-migration.db'
    monkeypatch.setenv('CRAFTARR_CONSOLE_DATABASE', str(path))
    config = Config(str(migrations.PROJECT_ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(migrations.PROJECT_ROOT / 'migrations'))
    command.upgrade(config, '38c21d9e40b5')
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO servers(name,directory,service_name,port,enabled,memory,plugins_dirty) VALUES ('Survival','/srv/survival','survival',25565,1,'2G',0)")
        connection.executemany('INSERT INTO plugin_monitoring_settings VALUES (1, ?, ?, ?, ?)', [
            ('viaversion', 'custom', 'github', 'viaversion/viaversion'),
            ('geyser', 'custom', 'geyser', 'geyser'), ('legacy', 'automatic', '', ''),
        ])
    command.upgrade(config, 'head')
    with sqlite3.connect(path) as connection:
        rows = {r[0]: r[1:] for r in connection.execute('SELECT plugin_name, mode, provider, project, version_pattern FROM plugin_monitoring_settings')}
    assert rows['viaversion'] == ('custom', 'github', 'viaversion/viaversion', '')
    assert rows['geyser'] == ('disabled', '', '', '')
    assert rows['legacy'] == ('disabled', '', '', '')


def write_defaults(path, plugins):
    import yaml
    path.write_text(yaml.safe_dump({'version': 1, 'plugins': plugins}))


def test_shipped_default_sources_and_aliases(db, tmp_path, monkeypatch):
    from app.monitoring_defaults import DEFAULTS_PATH, default_for
    from app.plugin_monitoring import monitoring_config
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(DEFAULTS_PATH))
    row = server(db, tmp_path)
    for name in ['AntiPopup', 'Chunky', 'Citizens', 'FAWE', 'Floodgate', 'Geyser-Spigot',
                 'LuckPerms', 'PlaceholderAPI', 'PlotSquared Premium', 'Vault', 'ViaVersion']:
        entry, error = default_for(name)
        assert entry and not error and not entry.get('error')
        provider, config = monitoring_config(db, row.id, name)
        assert provider is not None
    assert default_for('FAWE') == default_for('Fast Async World Edit')
    assert default_for('Geyser-Spigot') == default_for('Geyser')
    assert default_for('SomeUnknownPlugin') == (None, None)
    # There is deliberately no plugin entry for the separate Paper integration.
    assert default_for('Paper') == (None, None)


def test_defaults_prefill_and_saved_fields_survive_file_changes(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import monitoring_config, save_monitoring_config
    path = tmp_path / 'defaults.yml'
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(path))
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    write_defaults(path, {'ViaVersion': {'provider': 'github', 'project': 'Example/First'}})
    clear_saved_monitoring(db, first.id)
    source, config = monitoring_config(db, first.id, 'ViaVersion')
    assert source.project == 'example/first'
    assert config['mode'] == 'custom'
    assert config['provider'] == 'github' and config['project'] == 'example/first'
    # Existing per-server settings are not overridden by the file.
    assert monitoring_config(db, second.id, 'ViaVersion')[0].project == 'viaversion/viaversion'
    write_defaults(path, {'ViaVersion': {'provider': 'github', 'project': 'Example/SecondProject'}})
    assert monitoring_config(db, first.id, 'ViaVersion')[0].project == 'example/secondproject'
    _, fields = monitoring_config(db, first.id, 'ViaVersion')
    save_monitoring_config(db, first.id, 'ViaVersion', fields['mode'], fields['provider'], fields['project'])
    write_defaults(path, {'ViaVersion': {'provider': 'modrinth', 'project': 'new-release'}})
    assert monitoring_config(db, first.id, 'ViaVersion')[0].project == 'example/secondproject'
    save_monitoring_config(db, first.id, 'ViaVersion', 'disabled')
    assert monitoring_config(db, first.id, 'ViaVersion')[0] is None
    path.unlink()
    assert monitoring_config(db, first.id, 'ViaVersion')[0] is None
    with pytest.raises(ValueError):
        save_monitoring_config(db, first.id, 'ViaVersion', 'default')


def test_bad_default_entry_and_malformed_file_fail_safely(db, tmp_path, monkeypatch):
    from app.monitoring_defaults import default_for
    path = tmp_path / 'defaults.yml'
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(path))
    write_defaults(path, {
        'Good': {'provider': 'github', 'project': 'Example/Good'},
        'Bad': {'provider': 'custom', 'project': 'https://localhost/', 'version_pattern': '(1)'},
    })
    assert default_for('Good')[0]['project'] == 'example/good'
    assert default_for('Bad')[0]['error']
    row = server(db, tmp_path)
    jar(Path(row.directory) / 'plugins/bad.jar', 'Bad', '1')
    assert next(r for r in monitor.plugin_results(db, row) if r['plugin'] == 'Bad')['status'] == 'Check failed'
    path.write_text('plugins: [bad YAML')
    assert default_for('Good')[1] == 'Plugin monitoring defaults could not be loaded'
    # An explicit source continues to work even with a broken default file.
    from app.plugin_monitoring import monitoring_config
    assert monitoring_config(db, row.id, 'ViaVersion')[0] is not None


def test_default_rules_parse_expected_release_formats(monkeypatch):
    from app.monitoring_defaults import DEFAULTS_PATH, default_for
    from app.plugin_monitoring import custom_provider
    from app.update_providers import configured
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(DEFAULTS_PATH))
    for name, document, installed, expected in [
        ('Geyser', '{"version":"2.8.3","build":123}', '2.8.2-SNAPSHOT', '2.8.3'),
        ('Floodgate', '{"version":"2.2.4","build":56}', '2.2.3-SNAPSHOT', '2.2.4'),
        ('Citizens', json.dumps({'number': 4100, 'timestamp': 1700000000000, 'artifacts': []}), '2.0.40-SNAPSHOT (build 4099)', '4100'),
    ]:
        entry, _ = default_for(name)
        source = custom_provider(entry['provider'], entry['project'], entry['version_pattern'], entry['link_pattern'], entry['installed_pattern'])
        monkeypatch.setattr(configured, 'fetch_document', lambda url: document)
        release = source.fetch()[0]
        assert release.version == expected
        assert source.compare(installed, release) == 1
    fawe, _ = default_for('FAWE')
    source = custom_provider(fawe['provider'], fawe['project'], fawe['version_pattern'], fawe['link_pattern'], fawe['installed_pattern'])
    assert source.compare('2.15.4-SNAPSHOT-1357+995c825', providers.Release('1389', 'https://ci.athion.net')) == 1
    lp, _ = default_for('LuckPerms')
    from app.update_providers.configured import extract
    assert extract(lp['version_pattern'], 'v5.5.71-bukkit') == '5.5.71'


def test_prefilled_preview_checks_edited_fields_without_saving(db, tmp_path, monkeypatch):
    from app.models import PluginMonitoringSetting
    from app.plugin_monitoring import save_monitoring_config
    path = tmp_path / 'defaults.yml'
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(path))
    write_defaults(path, {'ViaVersion': {'provider': 'github', 'project': 'Example/Original'}})
    row = server(db, tmp_path)
    clear_saved_monitoring(db, row.id)
    calls = upstream(monkeypatch, [row])
    app = FastAPI()
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(web_plugins, 'get_accessible_server', lambda *args: (admin_user, row))
    admin_user = SimpleNamespace(role='admin', access_role=None, enabled=True)
    with TestClient(app) as client:
        response = client.post(f'/api/web/servers/{row.id}/plugins/monitoring/preview', json={
            'filename': 'arbitrary-name.jar', 'mode': 'custom', 'provider': 'github', 'project': 'ViaVersion/ViaVersion',
        })
    assert response.status_code == 200
    assert response.json()['latest_version'] == '5.6.0'
    assert len(calls) == 1
    assert db.query(PluginMonitoringSetting).count() == 0


def test_default_sources_share_cache_across_servers(db, tmp_path, monkeypatch):
    from app.plugin_monitoring import save_monitoring_config
    path = tmp_path / 'defaults.yml'
    monkeypatch.setenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', str(path))
    write_defaults(path, {'ViaVersion': {'provider': 'github', 'project': 'ViaVersion/ViaVersion'}})
    first = server(db, tmp_path)
    second = server(db, tmp_path, 'Creative')
    for row in [first, second]:
        clear_saved_monitoring(db, row.id)
    calls = upstream(monkeypatch, [first, second])
    results = monitor.check_updates(db, scope='plugins', now=NOW)
    assert len(calls) == 1
    assert all(items[0]['update_available'] for _, items in results)


def clear_saved_monitoring(db, server_id):
    from app.models import PluginMonitoringSetting
    db.query(PluginMonitoringSetting).filter_by(server_id=server_id).delete()
    db.commit()
