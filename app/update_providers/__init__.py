"""Structured metadata providers. Plugin projects are always administrator-configured."""
from dataclasses import asdict, dataclass
import json
import re
from html import unescape
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..paper import PAPER_API, USER_AGENT
from .http_source import SourceError
from .versions import parse_version, compare_versions


@dataclass
class Release:
    version: str
    url: str
    date: str | None = None
    minecraft_versions: list[str] | None = None
    build: int | None = None
    checksums: dict | None = None
    download_url: str | None = None

    def to_dict(self):
        return asdict(self)


def text(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid release metadata')
    return value


def get_json(url):
    # All callers construct URLs from fixed mappings; never follow upstream links.
    with httpx.Client(timeout=15, follow_redirects=False, headers={'User-Agent': USER_AGENT}) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError('Release metadata exceeds size limit')
    return json.loads(data)


class Provider:
    name = ''
    def __init__(self, project):
        self.project = project

    @property
    def key(self):
        return f'{self.name}:{self.project}'

    def fetch(self):
        raise NotImplementedError

    def compare(self, installed, release, filename=None):
        return compare_versions(installed, release.version)

    def display_version(self, release):
        return release.version


class GitHub(Provider):
    name = 'GitHub Releases'

    @property
    def key(self):
        # GitHub repository names are case-insensitive; share caches between configurations.
        return f'{self.name}:release-page-fallback-v3:{self.project.lower()}'

    def fetch_from_release_page(self):
        page_url = f'https://github.com/{self.project}/releases/latest'
        assets_url = None
        assets_page = ''
        try:
            with httpx.Client(timeout=15, follow_redirects=False, headers={'User-Agent': USER_AGENT}) as client:
                for _ in range(5):
                    with client.stream('GET', page_url) as response:
                        if 300 <= response.status_code < 400:
                            location = response.headers.get('location')
                            if not location:
                                raise SourceError('GitHub did not return a latest release page')
                            next_url = urljoin(page_url, location)
                            next_parts = urlsplit(next_url)
                            if next_parts.scheme != 'https' or next_parts.hostname != 'github.com' or next_parts.port not in (None, 443):
                                raise SourceError('GitHub redirected outside its release pages')
                            page_url = next_url
                            continue
                        response.raise_for_status()
                        final_url = str(response.url)
                        parts = urlsplit(final_url)
                        expected_path = f'/{self.project}/releases/tag/'.casefold()
                        if parts.hostname != 'github.com' or not parts.path.casefold().startswith(expected_path):
                            raise SourceError('GitHub did not return a latest release page')
                        tag = parts.path.rsplit('/', 1)[-1]
                        if not response.headers.get('content-type', '').lower().startswith('text/html'):
                            raise SourceError('GitHub release page did not return HTML metadata')
                        page = bytearray()
                        for chunk in response.iter_bytes():
                            page.extend(chunk)
                            if len(page) > 8 * 1024 * 1024:
                                raise SourceError('GitHub release page exceeds the size limit')
                        assets_url = f'https://github.com/{self.project}/releases/expanded_assets/{quote(tag, safe="")}'
                        break
                else:
                    raise SourceError('GitHub returned too many redirects while loading the release page')
                if assets_url:
                    try:
                        with client.stream('GET', assets_url) as response:
                            if response.status_code == 200:
                                for chunk in response.iter_bytes():
                                    assets_page += chunk.decode('utf-8', errors='replace')
                                    if len(assets_page) > 1024 * 1024:
                                        assets_page = ''
                                        break
                    except httpx.HTTPError:
                        pass
        except SourceError:
            raise
        except httpx.HTTPError:
            raise SourceError('GitHub release metadata is unavailable; its API and public release page could not be read') from None

        title_match = re.search(r'<title[^>]*>(.*?)</title\s*>', page.decode('utf-8', errors='replace'), re.I | re.S)
        if not title_match:
            raise SourceError('GitHub release page did not include release metadata')
        title = re.sub(r'\s+', ' ', unescape(title_match.group(1))).strip()
        suffix = f' · {self.project} · GitHub'
        if not title.casefold().endswith(suffix.casefold()):
            raise SourceError('GitHub release page did not match the selected repository')
        # Release titles often contain descriptive text (for example,
        # "Release 1.2.3 | bug fixes"). The tag in the final URL is the
        # canonical version when it is comparable.
        version = tag if parse_version(tag) is not None else title[:-len(suffix)].strip()
        version = re.sub(r'^release\s+', '', version, flags=re.I).strip()
        downloads = []
        for href in re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', assets_page, re.I):
            candidate = urljoin(assets_url, unescape(href))
            candidate_parts = urlsplit(candidate)
            download_prefix = f'/{self.project}/releases/download/'.casefold()
            if (candidate_parts.scheme == 'https' and candidate_parts.hostname == 'github.com'
                    and candidate_parts.path.casefold().startswith(download_prefix)
                    and candidate_parts.path.lower().endswith('.jar')):
                downloads.append(candidate)
        if not version or not tag:
            raise SourceError('GitHub release page did not include a version and tag')
        download_url = downloads[0] if len(downloads) == 1 else None
        return [Release(text(version), final_url, download_url=download_url)]

    def fetch(self):
        try:
            data = get_json(f'https://api.github.com/repos/{self.project}/releases/latest')
        except httpx.HTTPError:
            return self.fetch_from_release_page()
        if data.get('draft') or data.get('prerelease'):
            raise ValueError('No stable release')
        tag = text(data['tag_name'])
        version = tag
        release_name = data.get('name')
        if parse_version(tag) is None and isinstance(release_name, str) and parse_version(release_name) is not None:
            version = text(release_name)
        assets = [a for a in data.get('assets', []) if str(a.get('name', '')).endswith('.jar')]
        download = (f'https://github.com/{self.project}/releases/download/{quote(tag, safe="")}/{quote(text(assets[0]["name"]), safe="")}'
                    if len(assets) == 1 else None)
        return [Release(version, f'https://github.com/{self.project}/releases/tag/{quote(tag, safe="")}',
                        data.get('published_at'), download_url=download)]


class Modrinth(Provider):
    name = 'Modrinth'

    def release_version(self, value):
        return text(value)

    def fetch(self):
        data = get_json(f'https://api.modrinth.com/v2/project/{self.project}/version')
        releases = []
        beta_releases = []
        if not isinstance(data, list):
            raise ValueError('Invalid release list')
        for item in data:
            if not isinstance(item.get('loaders'), list) or not isinstance(item.get('game_versions'), list):
                raise ValueError('Invalid compatibility metadata')
            version_type = item.get('version_type')
            if version_type not in {'release', 'beta'} or not set(item.get('loaders', [])) & {'paper', 'spigot', 'bukkit', 'purpur'}:
                continue
            versions = [text(v) for v in item['game_versions']]
            files = [f for f in item.get('files', []) if f.get('primary') and str(f.get('filename', '')).endswith('.jar')]
            download = None
            if len(files) == 1:
                from .http_source import validate_url
                from urllib.parse import urlsplit
                candidate = validate_url(files[0]['url'])
                if urlsplit(candidate).hostname == 'cdn.modrinth.com':
                    download = candidate
            release = Release(self.release_version(item['version_number']),
                f'https://modrinth.com/plugin/{self.project}/version/{quote(text(item["id"]), safe="")}',
                item.get('date_published'), versions or None, download_url=download)
            (releases if version_type == 'release' else beta_releases).append(release)
        # Some projects, including Pl3xMap, publish Bukkit/Paper builds only as
        # beta on Modrinth. Prefer stable releases whenever they exist, and use
        # beta releases only when the project has no stable compatible artifacts.
        return releases or beta_releases


class Paper(Provider):
    name = 'PaperMC'

    def fetch(self):
        if not re.fullmatch(r'[0-9A-Za-z._+-]+', self.project):
            raise ValueError('Invalid Minecraft version')
        builds = get_json(f'{PAPER_API}/projects/paper/versions/{self.project}/builds')
        checksums = {}
        for item in builds:
            for download in item.get('downloads', {}).values():
                sha = download.get('checksums', {}).get('sha256')
                if sha:
                    checksums[str(sha).lower()] = str(int(item['id']))
        releases = [Release(str(int(item['id'])), 'https://papermc.io/downloads/paper',
                            item.get('time'), [self.project])
                    for item in builds if str(item.get('channel', '')).upper() in {'STABLE', 'RECOMMENDED'}]
        if releases:
            # Share the checksum index once, rather than duplicating it for every build.
            releases[0].checksums = checksums
        return releases


def normalize_name(name):
    return re.sub(r'[^a-z0-9]', '', str(name).casefold())


def select_release(releases, minecraft_version):
    candidates = [r for r in releases if not r.minecraft_versions or not minecraft_version or minecraft_version in r.minecraft_versions]
    pool = candidates or releases
    if not pool:
        raise ValueError('No published releases for this platform')
    # Providers return stable releases except documented continuous CI providers.
    return max(pool, key=lambda r: parse_version(r.version) or parse_version('0'))
