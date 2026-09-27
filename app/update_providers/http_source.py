"""Bounded public-HTTPS reads for administrator-configured monitoring sources."""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl

from ..paper import USER_AGENT

MAX_BODY = 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SourceError(ValueError):
    """Safe, actionable message that never contains response bodies or secrets."""


class SourceHTTPError(SourceError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Source returned HTTP {status}")


def validate_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise SourceError('Enter a valid public HTTPS URL')
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment or '\\' in value):
            raise ValueError()
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not is_public(address):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise SourceError('Use a public HTTPS URL without credentials, fragments or a custom port') from None
    if any(key.casefold() in {'token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'password', 'authorization', 'signature', 'credential'}
           for key, _ in parse_qsl(parts.query)):
        raise SourceError('Use a public URL without access tokens or credentials')
    authority = f'[{host}]' if ':' in host else host
    return urlunsplit(('https', authority, parts.path or '/', parts.query, ''))


def is_public(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    for embedded in (getattr(address, 'ipv4_mapped', None), getattr(address, 'sixtofour', None)):
        if embedded and not is_public(embedded):
            return False
    teredo = getattr(address, 'teredo', None)
    return not teredo or all(is_public(part) for part in teredo)


def public_addresses(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(entry[4][0] for entry in addresses))
        if not ips or any(not is_public(ipaddress.ip_address(ip)) for ip in ips):
            raise SourceError('Source must resolve only to public IP addresses')
        return ips
    except socket.gaierror:
        raise SourceError('Source hostname could not be resolved') from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the already-validated IP without a second DNS resolution.
        # TLS SNI and certificate verification still use the original hostname.
        raw_socket = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def fetch_document(url):
    current_url = validate_url(url)
    deadline = time.monotonic() + 30
    for redirect_count in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current_url)
        if parts.path.lower().endswith(('.jar', '.zip', '.exe', '.gz')):
            raise SourceError('Configure a metadata page or API, not an artifact URL')
        addresses = public_addresses(parts.hostname)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError('Source request exceeded the time limit')
        connection = PinnedHTTPSConnection(parts.hostname, addresses[0])
        connection.timeout = min(connection.timeout, remaining)
        try:
            target = urlunsplit(('', '', parts.path, parts.query, ''))
            connection.request('GET', target, headers={
                'User-Agent': USER_AGENT, 'Accept': 'application/json, text/html, text/plain',
                'Accept-Encoding': 'identity',
            })
            response = connection.getresponse()
            if response.status in REDIRECT_STATUSES:
                if redirect_count >= MAX_REDIRECTS:
                    raise SourceError('Source redirected too many times')
                location = response.getheader('Location')
                if not location:
                    raise SourceError('Source returned a redirect without a destination')
                current_url = validate_url(urljoin(current_url, location))
                continue
            if 300 <= response.status < 400:
                raise SourceHTTPError(response.status)
            if response.status != 200:
                raise SourceHTTPError(response.status)
            content_type = response.getheader('Content-Type', '').split(';')[0].strip().lower()
            if not (content_type.startswith('text/') or content_type in {'application/json', 'application/xml'}
                    or content_type.endswith(('+json', '+xml'))):
                raise SourceError('Source must return text, HTML, XML or JSON metadata, not an artifact')
            size = response.getheader('Content-Length')
            if size and int(size) > MAX_BODY:
                raise SourceError('Source response exceeds the 1 MiB limit')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise SourceError('Compressed source responses are not supported')
            body = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise SourceError('Source request exceeded the time limit')
                chunk = response.read1(min(65536, MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                if not body and chunk.startswith(b'PK\x03\x04'):
                    raise SourceError('Source returned an archive rather than metadata')
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise SourceError('Source response exceeds the 1 MiB limit')
            return body.decode('utf-8', errors='replace')
        except SourceError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise SourceError('Unable to read source metadata; check HTTPS access and response format') from None
        finally:
            connection.close()
