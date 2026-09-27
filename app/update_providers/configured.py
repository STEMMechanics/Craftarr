"""User-configured JSON/HTML/Jenkins extraction, with bounded regex execution."""
import hashlib
import html
import json
import re
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

    def __init__(self, project, version_pattern, link_pattern=''):
        super().__init__(validate_url(project))
        compile_pattern(version_pattern, required=True)
        compile_pattern(link_pattern)
        self.version_pattern, self.link_pattern = version_pattern, link_pattern

    def fetch(self):
        document = fetch_document(self.project)
        version = extract(self.version_pattern, document)
        link = validate_url(urljoin(self.project, extract(self.link_pattern, document, 'url'))) if self.link_pattern else None
        return [Release(version, self.project, download_url=link)]


class Jenkins(DocumentSource):
    name = 'Jenkins'

    def __init__(self, project, version_pattern='', link_pattern=''):
        Provider.__init__(self, validate_url(project))
        compile_pattern(version_pattern)
        compile_pattern(link_pattern)
        self.version_pattern, self.link_pattern = version_pattern, link_pattern
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
        try:
            data = json.loads(document)
            number = int(data['number'])
            if number < 0:
                raise ValueError()
            date = datetime.fromtimestamp(data['timestamp'] / 1000, timezone.utc).isoformat() if data.get('timestamp') is not None else None
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            raise SourceError('Jenkins did not return valid successful-build metadata') from None
        version = extract(self.version_pattern, document) if self.version_pattern else str(number)
        release_url = f'{self.project}/{number}/'
        link = None
        if self.link_pattern:
            link = validate_url(urljoin(release_url, extract(self.link_pattern, document, 'url')))
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
    def __init__(self, source, version_pattern='', link_pattern='', installed_pattern=''):
        self.source, self.name, self.project = source, source.name, source.project
        self.version_pattern, self.link_pattern = version_pattern, link_pattern
        self.installed_pattern = installed_pattern
        compile_pattern(version_pattern)
        compile_pattern(link_pattern)
        compile_pattern(installed_pattern)

    @property
    def key(self):
        if not self.version_pattern and not self.link_pattern:
            return self.source.key
        identity = [self.source.key, self.version_pattern, self.link_pattern]
        return f'configured:{hashlib.sha256(json.dumps(identity).encode()).hexdigest()}'

    def fetch(self):
        releases = self.source.fetch()
        for release in releases:
            if self.version_pattern and not isinstance(self.source, DocumentSource):
                release.version = extract(self.version_pattern, release.version)
            if parse_version(release.version) is None:
                raise SourceError('Detected version cannot be compared; adjust the version expression')
        return releases

    def installed_details(self, installed, filename=None):
        if self.installed_pattern:
            return extract(self.installed_pattern, str(installed or '')), 'JAR metadata'
        if isinstance(self.source, Jenkins) and not self.version_pattern:
            # Deliberately require a build marker; never mistake 2.0.43 for build 43.
            pattern = r'(?i)(?:\(build\s+|[- ]build[ .-]*|-b|-SNAPSHOT-)(\d+)(?:\)?(?:\+[a-z0-9]+)?)(?:$|\.jar(?:\.disabled)?$)'
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

    def installed_value(self, installed, filename=None):
        return self.installed_details(installed, filename)[0]

    def compare(self, installed, release, filename=None):
        value = self.installed_value(installed, filename)
        if value.startswith('commit:'):
            build = (release.checksums or {}).get(value.removeprefix('commit:'))
            if not build:
                return None
            value = build
        return compare_versions(value, release.version)

    def display_installed(self, installed, release=None, filename=None):
        value = self.installed_value(installed, filename)
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
