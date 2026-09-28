"""Read and manage the shared plugin update settings repository."""
from functools import lru_cache
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from .env import getenv

import yaml

from .update_providers import normalize_name

logger = logging.getLogger(__name__)
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / 'plugin-monitoring.yml'
FIELDS = ('provider', 'project', 'version_pattern', 'link_pattern', 'installed_pattern', 'asset_pattern')
MAX_REPOSITORY_BYTES = 262144
_repository_lock = threading.RLock()


def repository_path():
    configured = getenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', '').strip()
    if configured:
        return Path(configured).expanduser()
    database = Path(getenv('CRAFTARR_CONSOLE_DATABASE', 'craftarr.db')).expanduser().resolve()
    return database.parent / 'plugin-monitoring.yml'


def _source_path():
    path = repository_path()
    if path.exists() or getenv('CRAFTARR_PLUGIN_MONITORING_DEFAULTS', '').strip():
        return path
    # Keep bundled mappings available until an app-managed copy is saved. The
    # managed copy lives beside the database so it survives image replacement.
    return DEFAULTS_PATH


def default_for(name):
    path = _source_path()
    try:
        stat = path.stat()
        entries, error = _load(str(path.resolve()), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, 'Unable to read plugin monitoring defaults'
    entry = entries.get(normalize_name(name))
    return (dict(entry) if entry else None), error


def read_repository_text():
    path = _source_path()
    try:
        with path.open('rb') as source:
            raw = source.read(MAX_REPOSITORY_BYTES + 1)
    except FileNotFoundError:
        return 'version: 1\nplugins: {}\n'
    except OSError:
        raise ValueError('Unable to read the shared plugin settings file') from None
    if len(raw) > MAX_REPOSITORY_BYTES:
        raise ValueError('The shared plugin settings file exceeds 256 KiB')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('The shared plugin settings file must be UTF-8 text') from None


def validate_repository_text(content):
    if not isinstance(content, str):
        raise ValueError('Plugin settings must be text')
    try:
        raw = content.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Plugin settings must be valid UTF-8 text') from None
    if len(raw) > MAX_REPOSITORY_BYTES:
        raise ValueError('The shared plugin settings file must be 256 KiB or smaller')
    try:
        data = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError):
        raise ValueError('The YAML file could not be parsed') from None
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1
            or not isinstance(data.get('plugins'), dict)):
        raise ValueError('Use YAML with version: 1 and a plugins mapping')
    if set(data) - {'version', 'plugins'}:
        raise ValueError('Only the version and plugins sections are supported')

    from .plugin_monitoring import custom_provider
    names = {}
    for name, definition in data['plugins'].items():
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or not normalize_name(name):
            raise ValueError('Every plugin needs a valid name of 200 characters or fewer')
        if not isinstance(definition, dict):
            raise ValueError(f'{name}: plugin settings must be a YAML mapping')
        aliases = definition.get('aliases', [])
        if (not isinstance(aliases, list) or len(aliases) > 50
                or not all(isinstance(alias, str) and alias.strip() and len(alias) <= 200 and normalize_name(alias) for alias in aliases)):
            raise ValueError(f'{name}: aliases must be a list of plugin names')
        for alias in [name, *aliases]:
            key = normalize_name(alias)
            if key in names:
                raise ValueError(f'{alias}: duplicate plugin name or alias')
            names[key] = name
        fields = {field: definition.get(field, '') for field in FIELDS}
        if not all(isinstance(value, str) for value in fields.values()):
            raise ValueError(f'{name}: provider, project and expressions must be text')
        notes = definition.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 1000:
            raise ValueError(f'{name}: notes must be text of at most 1000 characters')
        try:
            custom_provider(fields['provider'], fields['project'], fields['version_pattern'],
                            fields['link_pattern'], fields['installed_pattern'], fields['asset_pattern'])
        except (ValueError, TypeError) as error:
            raise ValueError(f'{name}: {error}') from None
    return data


def _write_repository_text(content):
    validate_repository_text(content)
    path = repository_path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _repository_lock:
            descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                target.write(content if content.endswith('\n') else content + '\n')
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            temporary = None
            _load.cache_clear()
    except OSError:
        raise ValueError('Unable to save the shared plugin settings file; check the application data directory permissions') from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def save_repository_text(content):
    data = validate_repository_text(content)
    _write_repository_text(content)
    return {'success': True, 'plugins': len(data['plugins'])}


def add_repository_entry(name, settings):
    with _repository_lock:
        return _add_repository_entry(name, settings)


def upsert_repository_entry(name, settings):
    """Create or replace a plugin's shared settings while retaining its aliases and notes."""
    with _repository_lock:
        content = read_repository_text()
        data = validate_repository_text(content)
        key = normalize_name(name)
        fields = {field: settings[field] for field in FIELDS}
        for existing, definition in data['plugins'].items():
            aliases = definition.get('aliases', [])
            if key == normalize_name(existing) or key in {normalize_name(alias) for alias in aliases}:
                replacement = dict(definition)
                replacement.update(fields)
                data['plugins'][existing] = replacement
                _write_repository_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
                return {'success': True, 'plugins': len(data['plugins']), 'replaced': True}

        result = _add_repository_entry(name, settings)
        result['replaced'] = False
        return result


def _add_repository_entry(name, settings):
    content = read_repository_text()
    data = validate_repository_text(content)
    key = normalize_name(name)
    for existing, definition in data['plugins'].items():
        if key == normalize_name(existing) or key in {normalize_name(alias) for alias in definition.get('aliases', [])}:
            raise ValueError('Shared settings already exist for this plugin')
    fields = {field: settings[field] for field in FIELDS}
    definition = dict(fields)
    # Append a new mapping to the normal block-style YAML while preserving the
    # shipped repository's comments and formatting.
    entry_text = yaml.safe_dump({'plugins': {name: definition}}, sort_keys=False, allow_unicode=True)
    child_lines = entry_text.splitlines()[1:]
    if not child_lines:
        raise ValueError('Unable to prepare shared plugin settings')
    empty_plugins = re.compile(r'(?m)^plugins:\s*\{\s*\}\s*(?:#.*)?$')
    if empty_plugins.search(content):
        updated = empty_plugins.sub('plugins:', content, count=1).rstrip() + '\n'
    elif re.search(r'(?m)^plugins:\s*(?:#.*)?$', content):
        updated = content.rstrip() + '\n'
    else:
        data['plugins'][name] = definition
        updated = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        _write_repository_text(updated)
        return {'success': True, 'plugins': len(data['plugins'])}
    updated += '\n'.join(child_lines) + '\n'
    _write_repository_text(updated)
    return {'success': True, 'plugins': len(data['plugins']) + 1}


@lru_cache(maxsize=8)
def _load(path, mtime, size, inode):
    # Stat signatures refresh edited/replaced files without restarting the Console.
    try:
        with open(path, 'rb') as source:
            raw = source.read(262145)
        if len(raw) > MAX_REPOSITORY_BYTES:
            raise ValueError('Defaults file exceeds 256 KiB')
        data = yaml.safe_load(raw)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('plugins'), dict):
            raise ValueError('Expected version 1 and a plugins mapping')
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        logger.warning('Unable to load plugin monitoring defaults; check the file format and access')
        return {}, 'Plugin monitoring defaults could not be loaded'
    from .plugin_monitoring import custom_provider
    entries = {}
    for name, definition in data['plugins'].items():
        if not isinstance(name, str) or not normalize_name(name):
            logger.warning('Ignoring a default monitoring entry with an invalid plugin name')
            continue
        names = [name]
        entry = {}
        try:
            if not isinstance(definition, dict):
                raise ValueError()
            aliases = definition.get('aliases', [])
            if not isinstance(aliases, list) or not all(isinstance(alias, str) and normalize_name(alias) for alias in aliases):
                raise ValueError()
            names += aliases
            entry = {field: definition.get(field, '') for field in FIELDS}
            provider = custom_provider(entry['provider'], entry['project'], entry['version_pattern'], entry['link_pattern'], entry['installed_pattern'], entry['asset_pattern'])
            entry['project'] = provider.project
            notes = definition.get('notes', '')
            if not isinstance(notes, str) or len(notes) > 1000:
                raise ValueError()
            entry['notes'] = notes
        except (ValueError, TypeError):
            entry = {'error': 'Invalid default monitoring source; check plugin-monitoring.yml'}
            logger.warning('Invalid plugin monitoring default entry (source details omitted)')
        for alias in names:
            key = normalize_name(alias)
            if key in entries:
                entries[key] = {'error': 'Ambiguous plugin name/alias in monitoring defaults'}
                logger.warning('Duplicate plugin name/alias in monitoring defaults')
            else:
                entries[key] = entry
    return entries, None
