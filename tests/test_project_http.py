"""Public project transport tests: all sockets, TLS and environment are synthetic."""
import io
import socket
import ssl
from types import SimpleNamespace

import pytest

from agent_service import project_http as transport
from agent_service.browser import SourceError
from agent_service.contracts import PolicyError


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]


@pytest.fixture(autouse=True)
def no_real_network_or_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **k: pytest.fail("Unexpected DNS"))
    monkeypatch.setattr(transport.socket, "create_connection", lambda *a, **k: pytest.fail("Unexpected TCP"))


class FakeSocket:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []
        self.closed = False

    def sendall(self, value):
        self.sent.append(value)

    def makefile(self, mode):
        assert mode == "rb"
        return io.BytesIO(next(self.responses))

    def close(self):
        self.closed = True


def response(body=b"content", status="200 OK", mime="text/html", encoding=None, length=None):
    headers = [f"HTTP/1.1 {status}", f"Content-Type: {mime}"]
    if encoding is not None:
        headers.append(f"Content-Encoding: {encoding}")
    if length is not None:
        headers.append(f"Content-Length: {length}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def install_socket(monkeypatch, responses):
    sock = FakeSocket(responses)
    calls = []
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **k: calls.append(("dns", a, k)) or PUBLIC)
    monkeypatch.setattr(transport.socket, "create_connection", lambda peer, timeout: calls.append(("tcp", peer, timeout)) or sock)
    def context():
        calls.append(("default-context",))
        return SimpleNamespace(wrap_socket=lambda raw, server_hostname: calls.append(("tls", raw, server_hostname)) or raw)
    monkeypatch.setattr(transport.ssl, "create_default_context", context)
    return sock, calls


@pytest.mark.parametrize("host", ["gitlab.com", "huggingface.co", "codeberg.org", "github.com"])
@pytest.mark.parametrize("mime", ["text/html", "text/plain; charset=utf-8", "application/json"])
def test_direct_pins_numeric_tcp_and_verifies_original_tls_host(monkeypatch, host, mime):
    sock, calls = install_socket(monkeypatch, [response(mime=mime)])
    assert transport.fetch(f"https://{host}/owner/repo") == (mime, b"content", "direct-pinned-v1")
    assert calls == [("dns", (host, 443), {"type": socket.SOCK_STREAM}),
                     ("tcp", ("93.184.215.14", 443), 8), ("default-context",), ("tls", sock, host)]
    sent = b"".join(sock.sent)
    assert sent.startswith(b"GET /owner/repo HTTP/1.1\r\n")
    assert f"Host: {host}\r\n".encode() in sent
    assert b"Accept-Encoding: identity\r\n" in sent
    assert b"Cookie:" not in sent and b"Authorization:" not in sent
    assert sock.closed


@pytest.mark.parametrize("proxy,peer", [("http://127.0.0.1:8123", ("127.0.0.1", 8123)),
                                        ("http://127.8.9.10:8123", ("127.8.9.10", 8123)),
                                        ("http://[::1]:8123", ("::1", 8123))])
@pytest.mark.parametrize("name", ["HTTPS_PROXY", "https_proxy"])
def test_hf_connect_has_fixed_destination_tls_sni_and_no_auth(monkeypatch, proxy, peer, name):
    monkeypatch.setenv(name, proxy)
    sock, calls = install_socket(monkeypatch, [b"HTTP/1.1 200 Connection established\r\n\r\n", response(mime="application/json")])
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **k: pytest.fail("Proxy profile must not resolve remote DNS"))
    assert transport.fetch("https://huggingface.co/owner/repo/tree/main") == (
        "application/json", b"content", "hf-loopback-connect-v1")
    assert calls == [("tcp", peer, 8), ("default-context",), ("tls", sock, "huggingface.co")]
    sent = b"".join(sock.sent)
    assert sent.startswith(b"CONNECT huggingface.co:443 HTTP/1.")
    assert b"GET /owner/repo/tree/main HTTP/1.1\r\n" in sent
    assert b"Host: huggingface.co\r\n" in sent
    assert b"Authorization:" not in sent and b"Cookie:" not in sent
    assert b"127." not in sent and b"[::1]" not in sent
    assert sock.closed


@pytest.mark.parametrize("proxy", ["", "http://localhost:8123", "https://127.0.0.1:8123",
    "socks5://127.0.0.1:8123", "http://user:password@127.0.0.1:8123", "http://@127.0.0.1:8123",
    "http://192.168.1.1:8123", "http://93.184.215.14:8123", "http://[::ffff:10.0.0.1]:8123",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:",
    "http://127.0.0.1:8123/", "http://127.0.0.1:8123/path", "http://127.0.0.1:8123?",
    "http://127.0.0.1:8123#", " http://127.0.0.1:8123", "http://127.0.0.1:8123\n",
    "http://[::1%zone]:8123", "http://[::1]junk:8123", "http://127.1:8123"])
def test_invalid_hf_proxy_fails_closed_without_exposing_value(monkeypatch, proxy):
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    with pytest.raises(PolicyError, match="Invalid Hugging Face loopback proxy configuration") as captured:
        transport.fetch("https://huggingface.co/owner/repo")
    assert "password" not in str(captured.value)


def test_conflicting_proxy_spellings_fail_closed(monkeypatch):
    # Windows environment keys are case insensitive; model a case-sensitive OS.
    monkeypatch.setattr(transport, "os", SimpleNamespace(environ={
        "HTTPS_PROXY": "http://127.0.0.1:8123", "https_proxy": "http://127.0.0.1:8124"}))
    with pytest.raises(PolicyError, match="Conflicting"):
        transport.fetch("https://huggingface.co/owner/repo")


@pytest.mark.parametrize("host", ["gitlab.com", "codeberg.org", "github.com"])
def test_other_hosts_ignore_all_proxy_environment(monkeypatch, host):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@invalid:8123")
    monkeypatch.setenv("https_proxy", "http://different:8124")
    monkeypatch.setenv("ALL_PROXY", "http://invalid:8125")
    sock, calls = install_socket(monkeypatch, [response()])
    assert transport.fetch(f"https://{host}/owner/repo")[2] == "direct-pinned-v1"
    assert calls[0][0] == "dns"
    assert b"CONNECT" not in b"".join(sock.sent)


def test_hf_does_not_use_no_proxy_or_other_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("ALL_PROXY", "http://invalid:8125")
    install_socket(monkeypatch, [b"HTTP/1.1 200 Connected\r\n\r\n", response()])
    assert transport.fetch("https://huggingface.co/owner/repo")[2] == "hf-loopback-connect-v1"


def test_hf_without_https_proxy_ignores_other_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8123")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8123")
    install_socket(monkeypatch, [response()])
    assert transport.fetch("https://huggingface.co/owner/repo")[2] == "direct-pinned-v1"


@pytest.mark.parametrize("url", ["http://gitlab.com/a/b", "https://example.org/a/b",
    "https://127.0.0.1/a/b", "https://localhost/a/b", "https://gitlab.com.evil/a/b",
    "https://user:password@gitlab.com/a/b", "https://@gitlab.com/a/b", "https://gitlab.com:443/a/b",
    "https://gitlab.com:/a/b", "https://gitlab.com/a/b#x", "https://gitlab.com/a/b#",
    "\nhttps://gitlab.com/a/b", "https://gitlab.com/a/\tb", "https://gitlab.com/a/\x7fb",
    "https://gitlab.com/a/\x85b", "https://gitlab.com\\evil/a/b", "https://[::1/a/b"])
def test_source_url_rejected_before_any_network_or_proxy_access(url):
    with pytest.raises(PolicyError):
        transport.fetch(url)


def test_gitlab_approved_dynamic_get_query_preserved_exactly(monkeypatch):
    sock, _ = install_socket(monkeypatch, [response(mime="application/json")])
    transport.fetch("https://gitlab.com/group/repo/-/refs/main/logs_tree/?format=json&offset=0")
    assert b"GET /group/repo/-/refs/main/logs_tree/?format=json&offset=0 HTTP/1.1\r\n" in b"".join(sock.sent)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "0.0.0.0", "224.0.0.1"])
def test_all_dns_answers_must_be_global(monkeypatch, address):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **k: PUBLIC + [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(PolicyError):
        transport.fetch("https://github.com/owner/repo")


def test_empty_dns_rejected(monkeypatch):
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(PolicyError):
        transport.fetch("https://github.com/owner/repo")


@pytest.mark.parametrize("status", ["301 Moved", "302 Found", "307 Redirect", "401 Unauthorized", "404 Missing", "500 Failure"])
@pytest.mark.parametrize("proxied", [False, True])
def test_unsuccessful_status_never_redirects_or_retries(monkeypatch, status, proxied):
    responses = [response(status=status)]
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
        responses.insert(0, b"HTTP/1.1 200 Connected\r\n\r\n")
    sock, calls = install_socket(monkeypatch, responses)
    with pytest.raises(SourceError, match="HTTP status"):
        transport.fetch("https://huggingface.co/owner/repo")
    assert b"".join(sock.sent).count(b"GET ") == 1
    assert sum(call[0] == "tcp" for call in calls) == 1
    assert sock.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", ""])
def test_compressed_or_ambiguous_encoding_rejected(monkeypatch, encoding):
    sock, _ = install_socket(monkeypatch, [response(encoding=encoding)])
    with pytest.raises(SourceError, match="Compressed"):
        transport.fetch("https://github.com/owner/repo")
    assert sock.closed


@pytest.mark.parametrize("mime", ["application/octet-stream", "text/html-evil", "text/plainx", "application/jsonx", ""])
def test_exact_mime_allowlist(monkeypatch, mime):
    install_socket(monkeypatch, [response(mime=mime)])
    with pytest.raises(SourceError, match="content type"):
        transport.fetch("https://gitlab.com/owner/repo")


def test_size_limit_accepts_exact_boundary(monkeypatch):
    body = b"x" * transport.MAX_SOURCE_BYTES
    install_socket(monkeypatch, [response(body, length=str(len(body)))])
    assert transport.fetch("https://gitlab.com/owner/repo")[1] == body


@pytest.mark.parametrize("length", [None, str(transport.MAX_SOURCE_BYTES + 1)])
def test_oversized_response_rejected_with_or_without_declared_length(monkeypatch, length):
    sock, _ = install_socket(monkeypatch, [response(b"x" * (transport.MAX_SOURCE_BYTES + 1), length=length)])
    with pytest.raises(SourceError, match="limit|oversized"):
        transport.fetch("https://gitlab.com/owner/repo")
    assert sock.closed


@pytest.mark.parametrize("length", ["invalid", "-1", "1, 1"])
def test_invalid_content_length_rejected(monkeypatch, length):
    install_socket(monkeypatch, [response(length=length)])
    with pytest.raises(SourceError, match="content length"):
        transport.fetch("https://gitlab.com/owner/repo")


def test_incomplete_declared_body_rejected(monkeypatch):
    install_socket(monkeypatch, [response(b"short", length="10")])
    with pytest.raises(SourceError, match="Incomplete"):
        transport.fetch("https://gitlab.com/owner/repo")


@pytest.mark.parametrize("status", ["302 Redirect", "407 Proxy Authentication Required", "500 Failure"])
def test_connect_refusal_closes_without_tls_get_or_fallback(monkeypatch, status):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    sock, calls = install_socket(monkeypatch, [f"HTTP/1.1 {status}\r\n\r\n".encode()])
    with pytest.raises(OSError):
        transport.fetch("https://huggingface.co/owner/repo")
    assert not any(call[0] in ("dns", "tls", "default-context") for call in calls)
    assert b"GET " not in b"".join(sock.sent)
    assert sock.closed


@pytest.mark.parametrize("proxied", [False, True])
def test_tls_verification_failure_closes_socket_without_get_or_retry(monkeypatch, proxied):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8123")
    sock, calls = install_socket(monkeypatch, [b"HTTP/1.1 200 Connected\r\n\r\n"] if proxied else [])
    def reject(*args, **kwargs):
        raise ssl.SSLCertVerificationError("synthetic untrusted certificate")
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=reject))
    with pytest.raises(ssl.SSLCertVerificationError):
        transport.fetch("https://huggingface.co/owner/repo")
    assert b"GET " not in b"".join(sock.sent)
    assert sum(call[0] == "tcp" for call in calls) == 1
    assert sock.closed
