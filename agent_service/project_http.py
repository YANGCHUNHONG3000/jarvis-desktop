"""Bounded public-project GET transport, selected only by the new caller profile.

Direct requests validate every DNS answer and pin the selected numeric TCP peer.
The optional Hugging Face profile pins a numeric loopback HTTP proxy, hardcodes
CONNECT to huggingface.co:443 and verifies that name with default TLS. Remote
DNS is resolved by that proxy: fixed domain plus verified TLS replaces remote
numeric IP pinning in this profile. Project/path authorization belongs to the
caller; this module only enforces the exact public host and transport boundary.
No ambient proxy discovery, redirects, cookies, authentication or retries occur.
"""
import http.client
import ipaddress
import os
import socket
import ssl
import unicodedata
from urllib.parse import urlsplit

from .browser import SourceError
from .contracts import PolicyError


MAX_SOURCE_BYTES = 512 * 1024
TIMEOUT = 8
_HOSTS = frozenset({"gitlab.com", "huggingface.co", "codeberg.org", "github.com"})
_MIMES = frozenset({"text/html", "text/plain", "application/json"})
_HF_HOST = "huggingface.co"


def _has_controls(value):
    return any(unicodedata.category(char) == "Cc" or char.isspace() for char in value)


def _source_parts(url):
    if not isinstance(url, str) or _has_controls(url) or "#" in url or "\\" in url:
        raise PolicyError("Invalid public project URL")
    try:
        parsed = urlsplit(url)
        # Exact netloc also rules out userinfo, explicit/empty ports and aliases.
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS
                or parsed.netloc != parsed.hostname or parsed.fragment):
            raise ValueError
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        path.encode("ascii")
    except (ValueError, UnicodeError):
        raise PolicyError("Invalid public project URL") from None
    return parsed.hostname, path


def _hf_proxy():
    """Read only explicit HTTPS_PROXY spellings; never expose their contents."""
    values = [os.environ[name] for name in ("HTTPS_PROXY", "https_proxy") if name in os.environ]
    if not values:
        return None
    if any(value != values[0] for value in values):
        raise PolicyError("Conflicting Hugging Face proxy configuration")
    value = values[0]
    try:
        if not value or _has_controls(value) or "#" in value or "?" in value or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or not parsed.hostname
                or "%" in parsed.hostname or parsed.port is None):
            raise ValueError
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_loopback or not 1 <= parsed.port <= 65535:
            raise ValueError
        # Reject parser ambiguities, dangling bracket text and nonnumeric ports.
        host_text = f"[{parsed.hostname}]" if address.version == 6 else parsed.hostname
        port_text = parsed.netloc[len(host_text) + 1:]
        if (not parsed.netloc.startswith(host_text + ":") or not port_text.isascii()
                or not port_text.isdigit()):
            raise ValueError
        return str(address), parsed.port
    except (ValueError, UnicodeError):
        raise PolicyError("Invalid Hugging Face loopback proxy configuration") from None


class _DirectConnection(http.client.HTTPConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=TIMEOUT)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise


class _HFProxyConnection(http.client.HTTPConnection):
    def __init__(self, address, port):
        super().__init__(_HF_HOST, 443, timeout=TIMEOUT)
        self.proxy_address = address
        self.proxy_port = port
        self.set_tunnel(_HF_HOST, 443)

    def connect(self):
        # Do not let HTTPConnection resolve the original hostname for this TCP hop.
        self.sock = socket.create_connection((self.proxy_address, self.proxy_port), timeout=self.timeout)
        try:
            self._tunnel()
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=_HF_HOST)
        except BaseException:
            self.close()
            raise


def fetch(url):
    """Return (content type, bounded bytes, transport profile) for one exact GET."""
    host, path = _source_parts(url)
    proxy = _hf_proxy() if host == _HF_HOST else None
    if proxy is not None:
        connection = _HFProxyConnection(*proxy)
        transport = "hf-loopback-connect-v1"
    else:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise PolicyError("Project DNS resolved to a private/reserved address")
        connection = _DirectConnection(host, addresses[0][4][0])
        transport = "direct-pinned-v1"
    try:
        connection.request("GET", path, headers={
            "Host": host,
            "User-Agent": "Jarvis-ProjectReader/1",
            "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise SourceError(f"HTTP status {response.status}")
        mime = response.getheader("Content-Type", "").strip().lower()
        if mime.partition(";")[0].strip() not in _MIMES:
            raise SourceError("Unsupported project content type")
        if response.getheader("Content-Encoding", "identity").strip().lower() != "identity":
            raise SourceError("Compressed project sources are not supported")
        length = response.getheader("Content-Length")
        if length is not None:
            if not length.isascii() or not length.isdigit() or int(length) > MAX_SOURCE_BYTES:
                raise SourceError("Invalid or oversized project content length")
        content = response.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceError("Project source exceeds byte limit")
        if length is not None and len(content) != int(length):
            raise SourceError("Incomplete project source")
        return mime, content, transport
    finally:
        connection.close()
