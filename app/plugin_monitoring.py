"""Per-server overrides of editable application-root monitoring defaults."""
import re
from urllib.parse import urlsplit

from .models import PluginMonitoringSetting
from . import update_providers as providers
from .update_providers.configured import ConfiguredProvider, DocumentSource, Jenkins
from .update_providers.http_source import validate_url


def custom_provider(kind, project, version_pattern='', link_pattern='', installed_pattern=''):
    if not isinstance(kind, str) or not isinstance(project, str):
        raise ValueError('Select a provider and enter its project or URL')
    project = project.strip()
    if kind == 'github':
        if project.startswith('https://'):
            parts = urlsplit(validate_url(project))
            if parts.hostname != 'github.com' or parts.query:
                raise ValueError('Use a github.com repository URL')
            project = parts.path.strip('/')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', project):
            raise ValueError('Enter owner/repository or a GitHub repository URL')
        source = providers.GitHub(project.lower())
    elif kind == 'modrinth':
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
        source = Jenkins(project, version_pattern, link_pattern)
    elif kind == 'custom':
        source = DocumentSource(project, version_pattern, link_pattern)
    else:
        raise ValueError('Select GitHub, Modrinth, Jenkins or Custom URL')
    if link_pattern and kind in {'github', 'modrinth'}:
        raise ValueError('Link expressions apply to Jenkins and Custom URL sources')
    return ConfiguredProvider(source, version_pattern, link_pattern, installed_pattern)


def monitoring_config(db, server_id, name):
    from .monitoring_defaults import default_for, FIELDS
    setting = db.get(PluginMonitoringSetting, (server_id, providers.normalize_name(name)))
    defaults, file_error = default_for(name)
    global_entry = defaults if defaults and not defaults.get('error') else None
    global_settings = {field: global_entry.get(field, '') for field in FIELDS} if global_entry else {}
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
        fields = {field: '' for field in FIELDS}
    config = dict(fields)
    config.update(mode=mode, source=None, configured=bool(setting or defaults or file_error),
                  selection=selection,
                  notes=(global_entry or {}).get('notes', '') if selection == 'global' else '',
                  global_available=bool(global_entry), global_settings=global_settings,
                  global_notes=(global_entry or {}).get('notes', ''), error=None)
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


def save_monitoring_config(db, server_id, name, mode, kind='', project='', version_pattern='', link_pattern='', installed_pattern=''):
    if mode not in ('disabled', 'global', 'custom'):
        raise ValueError('Select disabled, shared or manual monitoring')
    if mode == 'custom':
        provider = custom_provider(kind, project, version_pattern, link_pattern, installed_pattern)
        project = provider.project
    elif mode == 'global':
        from .monitoring_defaults import default_for
        defaults, error = default_for(name)
        if not defaults or defaults.get('error') or error:
            raise ValueError('No valid shared update settings are available for this plugin')
        kind = project = version_pattern = link_pattern = installed_pattern = ''
    else:
        kind, project, version_pattern, link_pattern, installed_pattern = '', '', '', '', ''
    key = providers.normalize_name(name)
    if not key:
        raise ValueError('Plugin has no usable name')
    setting = db.get(PluginMonitoringSetting, (server_id, key))
    if not setting:
        setting = PluginMonitoringSetting(server_id=server_id, plugin_name=key)
        db.add(setting)
    setting.mode, setting.provider, setting.project = mode, kind, project
    setting.version_pattern, setting.link_pattern, setting.installed_pattern = version_pattern, link_pattern, installed_pattern
    db.commit()
