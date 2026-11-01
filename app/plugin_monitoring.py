"""Per-server overrides of editable application-root monitoring defaults."""
import re
from urllib.parse import urlsplit

from .models import DeveloperPluginUpdate, PluginMonitoringSetting, Server
from . import update_providers as providers
from .update_providers.configured import (
    ConfiguredProvider, DocumentSource, Jenkins, compile_download_rename,
)
from .update_providers.http_source import validate_url


def custom_provider(kind, project, version_pattern='', download_url='', installed_pattern='', asset_pattern='', installed_detection='auto', download_rename='', *, link_pattern=None):
    if link_pattern is not None:
        if download_url and download_url != link_pattern:
            raise ValueError('Set either download_url or the legacy link_pattern, not both')
        download_url = link_pattern
    if not isinstance(kind, str) or not isinstance(project, str):
        raise ValueError('Select a provider and enter its project or URL')
    if not isinstance(asset_pattern, str):
        raise ValueError('The GitHub JAR filename expression must be text')
    if not isinstance(download_rename, str):
        raise ValueError('Download rename must be text')
    if not isinstance(installed_detection, str):
        raise ValueError('Installed detection must be auto, filename, plugin.yml or paper-plugin.yml')
    project = project.strip()
    compile_download_rename(download_rename)
    if kind == 'github':
        if project.startswith('https://'):
            parts = urlsplit(validate_url(project))
            if parts.hostname != 'github.com' or parts.query:
                raise ValueError('Use a github.com repository URL')
            project = parts.path.strip('/')
        if project.casefold().endswith('.git'):
            project = project[:-4]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', project):
            raise ValueError('Enter owner/repository or a GitHub repository URL')
        source = providers.GitHub(project.lower(), asset_pattern)
    elif kind == 'modrinth':
        if asset_pattern:
            raise ValueError('JAR filename expressions apply only to GitHub Releases')
        if project.startswith('https://'):
            parts = urlsplit(validate_url(project))
            segments = parts.path.strip('/').split('/')
            if parts.hostname != 'modrinth.com' or parts.query or len(segments) != 2 or segments[0] not in {'plugin', 'mod', 'project'}:
                raise ValueError('Use a Modrinth project URL')
            project = segments[1]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', project):
            raise ValueError('Enter a Modrinth project slug, ID or project URL')
        source = providers.Modrinth(project)
    elif kind == 'jenkins':
        if asset_pattern:
            raise ValueError('JAR filename expressions apply only to GitHub Releases')
        source = Jenkins(project, version_pattern, download_url)
    elif kind == 'custom':
        if asset_pattern:
            raise ValueError('JAR filename expressions apply only to GitHub Releases')
        source = DocumentSource(project, version_pattern, download_url)
    else:
        raise ValueError('Select GitHub, Modrinth, Jenkins or Custom URL')
    if download_url and kind in {'github', 'modrinth'}:
        raise ValueError('Download URLs apply to Jenkins and Custom URL sources')
    return ConfiguredProvider(source, version_pattern, download_url, installed_pattern, asset_pattern, installed_detection, download_rename)


def validate_development_branch(branch):
    """Accept a single Git branch ref without option-like or ambiguous paths."""
    if not isinstance(branch, str):
        raise ValueError('GitHub branch must be text')
    branch = branch.strip()
    if not branch:
        return ''
    if (len(branch) > 255 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch)
            or '..' in branch or '//' in branch or '@{' in branch
            or branch.endswith(('.', '/'))):
        raise ValueError('Enter a valid Git branch name')
    parts = branch.split('/')
    if any(not part or part.startswith('.') or part.endswith('.') or part.casefold().endswith('.lock') for part in parts):
        raise ValueError('Enter a valid Git branch name')
    return branch


def monitoring_config(db, server_id, name):
    from .monitoring_defaults import default_for, FIELDS
    setting = db.get(PluginMonitoringSetting, (server_id, providers.normalize_name(name)))
    defaults, file_error = default_for(name)
    global_entry = defaults if defaults and not defaults.get('error') else None
    global_settings = {field: global_entry.get(field, 'auto' if field == 'installed_detection' else '') for field in FIELDS} if global_entry else {}
    if global_entry:
        global_settings['notes'] = global_entry.get('notes', '')
    global_error = (defaults or {}).get('error') or file_error
    has_global_source = bool(global_entry or global_error)
    # `mode` remains the provider/legacy field; `selection` is the UI scope.
    mode = setting.mode if setting else 'custom' if has_global_source else 'disabled'
    selection = setting.mode if setting else 'global' if has_global_source else 'disabled'
    if setting and mode == 'custom':
        fields = {field: getattr(setting, field) for field in FIELDS}
    elif selection == 'global':
        fields = dict(global_settings)
    else:
        fields = {field: ('auto' if field == 'installed_detection' else '') for field in FIELDS}
    config = dict(fields)
    config.update(mode=mode, source=None, configured=bool(setting or defaults or file_error),
                  selection=selection,
                  notes=(global_entry or {}).get('notes', '') if selection == 'global' else '',
                  global_available=bool(global_entry), global_settings=global_settings,
                  global_notes=(global_entry or {}).get('notes', ''), error=None)
    config['development_branch'] = setting.development_branch if setting else ''
    config['developer_updates_enabled'] = bool(
        (server := db.get(Server, server_id)) and server.developer_updates_enabled
    )
    state = db.get(DeveloperPluginUpdate, (server_id, providers.normalize_name(name)))
    source_key = f"{setting.project.lower()}#{setting.development_branch}" if setting and setting.development_branch else None
    config['development_status'] = ({
        'status': state.status,
        'latest_commit': state.latest_commit,
        'installed_commit': state.installed_commit,
        'error': state.error,
        'build_log': state.build_log,
        'build_report_available': bool(state.build_report_html),
        'build_report_path': state.build_report_path,
        'checked_at': state.checked_at.isoformat() + 'Z' if state.checked_at else None,
        'updated_at': state.updated_at.isoformat() + 'Z' if state.updated_at else None,
    } if state and source_key == state.source_key else None)
    provider = None
    if selection == 'custom':
        provider = custom_provider(*(config[field] for field in FIELDS))
    elif selection == 'global':
        if global_entry:
            provider = custom_provider(*(global_settings[field] for field in FIELDS))
        else:
            config['error'] = global_error or 'No shared update settings are available for this plugin.'
    if provider:
        config['source'] = f'{provider.name}: {provider.project}'
    return provider, config


def save_monitoring_config(db, server_id, name, mode, kind='', project='', version_pattern='', download_url='', installed_pattern='', asset_pattern='', installed_detection='auto', download_rename='', *, link_pattern=None, development_branch=''):
    if link_pattern is not None:
        if download_url and download_url != link_pattern:
            raise ValueError('Set either download_url or the legacy link_pattern, not both')
        download_url = link_pattern
    if mode not in ('disabled', 'global', 'custom'):
        raise ValueError('Select disabled, shared or manual monitoring')
    development_branch = validate_development_branch(development_branch)
    if mode == 'custom':
        provider = custom_provider(kind, project, version_pattern, download_url, installed_pattern, asset_pattern, installed_detection, download_rename)
        project = provider.project
        if development_branch:
            server = db.get(Server, server_id)
            if not server or not server.developer_updates_enabled:
                raise ValueError('Enable Developer options for this server before setting a branch')
            if kind != 'github':
                raise ValueError('Branch builds are available only for GitHub projects')
    elif mode == 'global':
        from .monitoring_defaults import default_for
        defaults, error = default_for(name)
        if not defaults or defaults.get('error') or error:
            raise ValueError('No valid shared update settings are available for this plugin')
        kind = project = version_pattern = download_url = installed_pattern = asset_pattern = download_rename = ''
        installed_detection = 'auto'
        development_branch = ''
    else:
        kind, project, version_pattern, download_url, installed_pattern, asset_pattern, download_rename = '', '', '', '', '', '', ''
        installed_detection = 'auto'
        development_branch = ''
    key = providers.normalize_name(name)
    if not key:
        raise ValueError('Plugin has no usable name')
    setting = db.get(PluginMonitoringSetting, (server_id, key))
    if not setting:
        setting = PluginMonitoringSetting(server_id=server_id, plugin_name=key)
        db.add(setting)
    setting.mode, setting.provider, setting.project = mode, kind, project
    setting.version_pattern, setting.download_url, setting.installed_pattern = version_pattern, download_url, installed_pattern
    setting.asset_pattern = asset_pattern
    setting.installed_detection = installed_detection
    setting.download_rename = download_rename
    setting.development_branch = development_branch
    db.commit()
