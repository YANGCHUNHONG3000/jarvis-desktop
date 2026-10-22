"""Network boundary tests use fake transports, never real external requests."""
import json
import socket
from types import SimpleNamespace

import pytest

from agent_service import browser
from agent_service.contracts import PolicyError


URL = "https://example.org/source"
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]


def test_pinned_tcp_and_tls_hostname_are_distinct(monkeypatch):
    calls = []
    sock = object()
    monkeypatch.setattr(browser.socket, "create_connection", lambda address, timeout: calls.append(address) or sock)
    monkeypatch.setattr(browser.ssl, "create_default_context", lambda: SimpleNamespace(
        wrap_socket=lambda given, server_hostname: calls.append((given, server_hostname)) or given))
    connection = browser.PinnedConnection("example.org", 443, "93.184.215.14", True)
    connection.connect()
    assert calls == [("93.184.215.14", 443), (sock, "example.org")]


def fake_connection(monkeypatch, status=200, content=b"<body>evidence</body>", mime="text/html", encoding="identity"):
    calls = []
    monkeypatch.setattr(browser.socket, "getaddrinfo", lambda *args, **kwargs: PUBLIC)

    class Connection:
        def __init__(self, host, port, address, secure):
            calls.append((host, port, address, secure))

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return SimpleNamespace(status=status, getheader=lambda name, default="": mime if name == "Content-Type" else encoding,
                                   read=lambda size: content[:size])

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(browser, "PinnedConnection", Connection)
    return calls


def test_fetch_pins_validated_ip_no_proxy_or_cookie(monkeypatch):
    calls = fake_connection(monkeypatch)
    assert browser.fetch_source(URL) == ("text/html", b"<body>evidence</body>")
    assert calls[0] == ("example.org", 443, "93.184.215.14", True)
    assert calls[1][0] == ("GET", "/source")
    assert "Cookie" not in calls[1][1]["headers"]
    assert calls[-1] == "closed"


@pytest.mark.parametrize("status", [301, 302, 307, 401, 404, 500])
def test_redirect_or_error_never_issues_second_request(monkeypatch, status):
    calls = fake_connection(monkeypatch, status=status)
    with pytest.raises(browser.SourceError):
        browser.fetch_source(URL)
    assert len(calls) == 3  # construction, one request, close


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1"])
def test_dns_private_destination_never_creates_connection(monkeypatch, address):
    calls = fake_connection(monkeypatch)
    monkeypatch.setattr(browser.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(PolicyError):
        browser.fetch_source(URL)
    assert not calls


def test_oversized_body_rejected(monkeypatch):
    fake_connection(monkeypatch, content=b"x" * (browser.MAX_SOURCE_BYTES + 1))
    with pytest.raises(browser.SourceError):
        browser.fetch_source(URL)


def test_unsupported_mime_rejected(monkeypatch):
    fake_connection(monkeypatch, mime="application/octet-stream")
    with pytest.raises(browser.SourceError):
        browser.fetch_source(URL)


def test_compressed_response_rejected(monkeypatch):
    fake_connection(monkeypatch, encoding="gzip")
    with pytest.raises(browser.SourceError):
        browser.fetch_source(URL)


def test_url_canonicalization():
    from agent_service.policy import source_url
    assert source_url("https://EXAMPLE.org:443/中文?q=中文#x") == "https://example.org/%E4%B8%AD%E6%96%87?q=%E4%B8%AD%E6%96%87"
    with pytest.raises(PolicyError):
        source_url("https://example.org/a/%2e%2e/b")


def test_long_page_selects_relevant_passage_after_contents():
    contents = "Contents\n" + "Installation and migration\n" * 650
    detail = "The pathlib Path.walk method yields directory paths and their entries. " * 12
    source = "Official guide opening.\n" + contents + "\n" + detail + "\n" + "Appendix. " * 400
    excerpt = browser._excerpt(source, "pathlib Path.walk directory entries")
    assert len(source) > browser.MAX_EXCERPT_CHARS
    assert len(excerpt) <= browser.MAX_EXCERPT_CHARS
    assert excerpt.startswith("Official guide opening.")
    assert "Path.walk method yields directory paths" in excerpt
    assert "\n…\n" in excerpt
    assert excerpt.endswith("…")
    assert excerpt.index("Official guide") < excerpt.index("Path.walk method")


def test_short_page_preserved_and_no_match_falls_back_to_bounded_opening():
    assert browser._excerpt("a short source", "irrelevant") == "a short source"
    long_source = "opening " + "ordinary text " * 1200
    excerpt = browser._excerpt(long_source, "no matching topic")
    assert len(excerpt) <= browser.MAX_EXCERPT_CHARS
    assert excerpt.startswith("opening ")
    assert excerpt.endswith("…")


def test_chinese_query_finds_detail_beyond_first_twelve_thousand_characters():
    source = "页面开头。\n" + "目录条目\n" * 3000 + "真实正文介绍量子电池寿命与温度关系。\n"
    excerpt = browser._excerpt(source, "量子电池寿命")
    assert "量子电池寿命与温度关系" in excerpt
    assert len(excerpt) <= browser.MAX_EXCERPT_CHARS


def test_main_content_preferred_and_body_fallback():
    class Locator:
        def __init__(self, values):
            self.values = values

        def inner_text(self, timeout):
            return self.values[0]

        def count(self):
            return len(self.values)

        def nth(self, index):
            return Locator([self.values[index]])

    class Page:
        def __init__(self, body, main):
            self.body, self.main = body, main

        def locator(self, selector):
            return Locator([self.body] if selector == "body" else self.main)

    article = "A substantial article paragraph. " * 8
    body = "Navigation " * 100 + article
    assert browser._readable_text(Page(body, [article])) == article.strip()
    assert browser._readable_text(Page(body, ["tiny"])) == body.strip()


def test_browser_process_forwards_optional_query_and_preserves_deadline(monkeypatch):
    calls = []
    observation = {"requested_url": URL, "final_url": URL, "title": "Source",
                   "text": "Evidence", "captured_at": "2026-10-17T00:00:00Z"}

    def run_child(command, should_stop, timeout):
        calls.append((command, timeout, should_stop()))
        return json.dumps(observation)

    monkeypatch.setattr("agent_service.process.run_child", run_child)
    assert browser.BrowserProcess(query="pathlib Path.walk").read(URL).text == "Evidence"
    assert calls[0][0][-2:] == [URL, "pathlib Path.walk"]
    assert calls[0][1:] == (40, False)
    browser.BrowserProcess().read(URL)
    assert calls[1][0][-1] == URL
