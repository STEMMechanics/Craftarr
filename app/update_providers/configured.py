"""User-configured JSON/HTML/Jenkins extraction, with bounded regex execution."""
import hashlib
import html
import json
import re
from string import Formatter
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit

import regex

from . import Provider, Release, text
from .http_source import SourceHTTPError, SourceError, fetch_document, validate_url
from .versions import compare_versions, parse_version


def compile_pattern(pattern, required=False):
    if not isinstance(pattern, str) or len(pattern) > 1024:
        raise SourceError('Extraction expressions must be text of at most 1024 characters')
    if not pattern:
        if required:
            raise SourceError('A version expression with a capture group is required')
        return None
    try:
        compiled = regex.compile(pattern)
    except (regex.error, RecursionError):
        raise SourceError('Invalid extraction expression') from None
    if compiled.groups < 1:
        raise SourceError('An extraction expression must contain a capture group, for example ([0-9.]+)')
    return compiled


def is_fixed_download_url(value):
    """Distinguish a literal URL from an expression that extracts one."""
    if not isinstance(value, str) or not value.startswith('https://'):
        return False
    try:
        return regex.compile(value).groups == 0
    except (regex.error, RecursionError):
        return True


def compile_download_url(value):
    """Accept either an extracted URL expression or a fixed HTTPS URL."""
    if not isinstance(value, str) or len(value) > 1024:
        raise SourceError('Download link settings must be text of at most 1024 characters')
    if not value:
        return None
    if is_fixed_download_url(value):
        return validate_url(value)
    return compile_pattern(value)


def compile_download_rename(template):
    if not isinstance(template, str) or len(template) > 255:
        raise SourceError('Download rename must be text of at most 255 characters')
    if not template:
        return
    try:
        for _, field, format_spec, conversion in Formatter().parse(template):
            if field is not None and (field not in {'filename', 'version', 'extension'} or format_spec or conversion):
                raise ValueError
    except (ValueError, TypeError):
        raise SourceError('Download rename supports only {filename}, {version} and {extension}') from None
    if '/' in template or '\\' in template or '\n' in template or '\r' in template:
        raise SourceError('Download rename must produce a filename, not a path')


# Transitional alias for callers using the former setting name.
compile_link_pattern = compile_download_url


def is_direct_download_url(pattern):
    return is_fixed_download_url(pattern)


def extract(pattern, document, label='version'):
    compiled = compile_pattern(pattern, required=True)
    try:
        match = compiled.search(document, timeout=0.1)
    except TimeoutError:
        raise SourceError('Extraction expression exceeded the time limit; simplify it') from None
    if not match:
        raise SourceError(f'The {label} expression did not match')
    value = match.group(label if label in compiled.groupindex else 1)
    if not value:
        raise SourceError(f'The {label} expression captured an empty value')
    value = html.unescape(value.replace('\\/', '/')).strip()
    if len(value) > (2048 if label == 'url' else 200) or any(ord(c) < 32 for c in value):
        raise SourceError(f'The captured {label} is invalid')
    return value


class DocumentSource(Provider):
    name = 'Custom URL'

    def __init__(self, project, version_pattern, download_url='', *, link_pattern=None):
        if link_pattern is not None:
            download_url = link_pattern
        super().__init__(validate_url(project))
        compile_pattern(version_pattern, required=True)
        self.direct_download_url = validate_url(download_url) if is_fixed_download_url(download_url) else None
        compile_download_url(download_url)
        self.version_pattern, self.download_url = version_pattern, download_url

    def fetch(self):
        document = fetch_document(self.project)
        self.preview_input = document
        self.preview_default_version = None
        version = extract(self.version_pattern, document)
        link = self.direct_download_url
        if not link and self.download_url:
            link = validate_url(urljoin(self.project, extract(self.download_url, document, 'url')))
        return [Release(version, self.project, download_url=link)]


class Jenkins(DocumentSource):
    name = 'Jenkins'

    def __init__(self, project, version_pattern='', download_url='', *, link_pattern=None):
        if link_pattern is not None:
            download_url = link_pattern
        Provider.__init__(self, validate_url(project))
        compile_pattern(version_pattern)
        self.direct_download_url = validate_url(download_url) if is_fixed_download_url(download_url) else None
        compile_download_url(download_url)
        self.version_pattern, self.download_url = version_pattern, download_url
        if urlsplit(self.project).query:
            raise SourceError('Enter a Jenkins job URL without query parameters')
        self.project = self.project.rstrip('/')

    def fetch(self):
        build_index = {}
        try:
            builds_data = json.loads(fetch_document(
                self.project + '/api/json?tree=builds%5Bnumber%2Cresult%2CchangeSet%5Bitems%5Bid%5D%5D%5D'
            ))
            for build in builds_data.get('builds', []):
                if build.get('result') not in (None, 'SUCCESS'):
                    continue
                for change in (build.get('changeSet') or {}).get('items', []):
                    commit = str(change.get('id', '')).casefold()
                    number = build.get('number')
                    if commit and isinstance(number, int) and number >= 0:
                        build_index[commit] = str(number)
                        build_index[commit[:7]] = str(number)
        except (SourceError, ValueError, TypeError, KeyError):
            # The build index is helpful for snapshot JARs, but the normal
            # Jenkins endpoint remains sufficient for release checks.
            pass
        try:
            document = fetch_document(self.project + '/lastSuccessfulBuild/api/json?tree=number,timestamp,artifacts[fileName,relativePath]')
        except SourceHTTPError as error:
            if error.status not in (403, 404):
                raise
            # Fixed page on the configured job, never a URL read from upstream.
            document = self.page_metadata(fetch_document(self.project + '/lastSuccessfulBuild/'))
        self.preview_input = document
        try:
            data = json.loads(document)
            number = int(data['number'])
            if number < 0:
                raise ValueError()
            date = datetime.fromtimestamp(data['timestamp'] / 1000, timezone.utc).isoformat() if data.get('timestamp') is not None else None
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            raise SourceError('Jenkins did not return valid successful-build metadata') from None
        self.preview_default_version = str(number)
        version = extract(self.version_pattern, document) if self.version_pattern else str(number)
        release_url = f'{self.project}/{number}/'
        link = None
        if self.direct_download_url:
            link = self.direct_download_url
        elif self.download_url:
            link = validate_url(urljoin(release_url, extract(self.download_url, document, 'url')))
        else:
            artifacts = [a for a in data.get('artifacts', []) if str(a.get('fileName', '')).endswith('.jar')]
            paper_artifacts = [a for a in artifacts if re.search(r'(^|[-_.])paper([-_.]|$)', str(a.get('fileName', '')), re.IGNORECASE)]
            selected = paper_artifacts[0] if len(paper_artifacts) == 1 else artifacts[0] if len(artifacts) == 1 else None
            if selected:
                path = selected.get('relativePath', '')
                if isinstance(path, str) and path and not path.startswith('/') and all(p not in {'.', '..', ''} for p in path.split('/')):
                    link = validate_url(release_url + 'artifact/' + quote(path, safe='/'))
        return [Release(version, release_url, date, checksums=build_index or None, download_url=link)]


    def page_metadata(self, document):
        class BuildPage(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_title, self.title, self.links = False, '', []
            def handle_starttag(self, tag, attrs):
                if tag == 'title':
                    self.in_title = True
                if tag == 'a':
                    self.links.append(dict(attrs).get('href', ''))
            def handle_endtag(self, tag):
                if tag == 'title':
                    self.in_title = False
            def handle_data(self, data):
                if self.in_title:
                    self.title += data
        page = BuildPage()
        page.feed(document)
        match = re.search(r'#(\d{1,12})\s*-\s*Jenkins\s*$', page.title)
        if not match:
            raise SourceError('Jenkins API is unavailable and its successful-build page could not be read')
        number = int(match[1])
        artifacts = []
        for href in page.links:
            url = urljoin(self.project + '/lastSuccessfulBuild/', href)
            for prefix in (f'{self.project}/lastSuccessfulBuild/artifact/', f'{self.project}/{number}/artifact/'):
                if url.startswith(prefix) and url.endswith('.jar'):
                    path = url[len(prefix):]
                    artifact = {'fileName': path.rsplit('/', 1)[-1], 'relativePath': path}
                    if artifact not in artifacts:
                        artifacts.append(artifact)
        return json.dumps({'number': number, 'artifacts': artifacts})


class ConfiguredProvider(Provider):
    """Include upstream extraction rules in cache identity, not installed-version rules."""
    INSTALLED_DETECTIONS = {'auto', 'filename', 'plugin.yml', 'paper-plugin.yml'}

    def __init__(self, source, version_pattern='', download_url='', installed_pattern='', asset_pattern='', installed_detection='auto', download_rename='', *, link_pattern=None):
        if link_pattern is not None:
            download_url = link_pattern
        self.source, self.name, self.project = source, source.name, source.project
        self.version_pattern, self.download_url = version_pattern, download_url
        self.link_pattern = download_url
        self.installed_pattern = installed_pattern
        self.asset_pattern = asset_pattern
        self.installed_detection = installed_detection or 'auto'
        self.download_rename = download_rename or ''
        if self.installed_detection not in self.INSTALLED_DETECTIONS:
            raise SourceError('Installed detection must be auto, filename, plugin.yml or paper-plugin.yml')
        if self.installed_detection == 'filename' and not installed_pattern:
            raise SourceError('A filename expression is required when installed detection is filename')
        self.preview_input = None
        self.preview_default_version = None
        self.preview_diagnostics = None
        compile_pattern(version_pattern)
        compile_download_url(download_url)
        compile_download_rename(self.download_rename)
        compile_pattern(installed_pattern)

    @property
    def key(self):
        if not self.version_pattern and not self.download_url and not self.asset_pattern:
            return self.source.key
        identity = [self.source.key, self.version_pattern, self.download_url]
        if self.asset_pattern:
            identity.append(self.asset_pattern)
        return f'configured:{hashlib.sha256(json.dumps(identity).encode()).hexdigest()}'

    def fetch(self):
        self.preview_input = None
        self.preview_default_version = None
        self.preview_diagnostics = None
        try:
            releases = self.source.fetch()
        except SourceError as error:
            self.preview_input = getattr(self.source, 'preview_input', None)
            self.preview_default_version = getattr(self.source, 'preview_default_version', None)
            if self.preview_input is not None:
                message = str(error)
                state = 'no_match' if 'did not match' in message else 'invalid' if 'Invalid extraction expression' in message else 'error'
                self.preview_diagnostics = {
                    'source_label': 'Source metadata',
                    'source_value': None,
                    'captured_value': None,
                    'pattern_status': state,
                    'comparable': False,
                    'error': message,
                }
            raise
        self.preview_input = getattr(self.source, 'preview_input', None)
        self.preview_default_version = getattr(self.source, 'preview_default_version', None)
        for release in releases:
            source_value = release.version
            if self.preview_input is None:
                self.preview_input = source_value
            if self.preview_default_version is None and not isinstance(self.source, DocumentSource):
                self.preview_default_version = source_value
            document_source = isinstance(self.source, DocumentSource)
            diagnostic = {
                'source_label': 'Source metadata' if document_source else 'Release tag/version',
                'source_value': None if document_source else source_value,
                'captured_value': None if self.version_pattern else source_value,
                'pattern_status': 'matched' if self.version_pattern else 'not_set',
                'comparable': parse_version(source_value) is not None,
                'error': None,
            }
            if self.version_pattern and not document_source:
                try:
                    release.version = extract(self.version_pattern, source_value)
                    diagnostic['captured_value'] = release.version
                    diagnostic['pattern_status'] = 'matched'
                    diagnostic['comparable'] = parse_version(release.version) is not None
                except SourceError as error:
                    state = 'no_match' if 'did not match' in str(error) else 'invalid' if 'Invalid extraction expression' in str(error) else 'error'
                    diagnostic.update(pattern_status=state, captured_value=None, comparable=False, error=str(error))
                    self.preview_diagnostics = diagnostic
                    raise
            if parse_version(release.version) is None:
                message = f'Detected version "{release.version}" cannot be compared; adjust the version expression'
                diagnostic.update(pattern_status='uncomparable', comparable=False, error=message)
                self.preview_diagnostics = diagnostic
                raise SourceError(message)
            self.preview_diagnostics = diagnostic
        return releases

    def installed_details(self, installed, filename=None, version_sources=None):
        if self.installed_detection == 'filename':
            source_value, source_label = filename, 'JAR filename'
            if not source_value:
                raise SourceError('The installed JAR filename is unavailable')
            return extract(self.installed_pattern, str(source_value), 'version'), source_label
        if self.installed_detection in {'plugin.yml', 'paper-plugin.yml'}:
            source_label = f'{self.installed_detection} metadata'
            source_value = (version_sources or {}).get(self.installed_detection)
            if not source_value:
                raise SourceError(f'No version was found in {self.installed_detection}')
            if self.installed_pattern:
                return extract(self.installed_pattern, str(source_value)), source_label
            return str(source_value), source_label
        if self.installed_pattern:
            return extract(self.installed_pattern, str(installed or '')), 'JAR metadata'
        if isinstance(self.source, Jenkins) and not self.version_pattern:
            # Deliberately require a build marker; never mistake 2.0.43 for build 43.
            pattern = r'(?i)(?:\(build\s+|[- ]build[ .-]*|[-+]b|-SNAPSHOT[-+]b?)(\d+)(?:\)?(?:\+[a-z0-9]+)?)(?:$|\.jar(?:\.disabled)?$)'
            for value, origin in ((installed, 'JAR metadata'), (filename, 'JAR filename')):
                match = re.search(pattern, str(value or ''))
                if match:
                    return match[1], origin
            # Snapshot JARs often carry the source commit in plugin.yml while
            # their filenames are identical across Jenkins builds.
            commit = re.search(r'(?i)\+([0-9a-f]{7,40})(?:$|[+ ])', str(installed or ''))
            if commit:
                return f'commit:{commit[1].casefold()}', 'JAR metadata'
            raise SourceError('No installed Jenkins build number found in JAR metadata or filename; use an installed version expression or a version-based source')
        return installed, 'JAR metadata'

    def installed_value(self, installed, filename=None, version_sources=None):
        return self.installed_details(installed, filename, version_sources)[0]

    def compare(self, installed, release, filename=None, version_sources=None):
        value = self.installed_value(installed, filename, version_sources)
        if value.startswith('commit:'):
            build = (release.checksums or {}).get(value.removeprefix('commit:'))
            if not build:
                return None
            value = build
        return compare_versions(value, release.version)

    def display_installed(self, installed, release=None, filename=None, version_sources=None):
        value = self.installed_value(installed, filename, version_sources)
        if isinstance(value, str) and value.startswith('commit:'):
            if release:
                build = (release.checksums or {}).get(value.removeprefix('commit:'))
                if build:
                    return f'build {build}'
            return installed or value
        if isinstance(self.source, Jenkins) and not self.version_pattern and value:
            return f'build {value}'
        return value if value is not None else installed

    def display_version(self, release):
        return f'build {release.version}' if isinstance(self.source, Jenkins) and not self.version_pattern else release.version
