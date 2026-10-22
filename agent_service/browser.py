"""Read-only installed Edge adapter; imports Playwright only when executed."""
import ipaddress
import http.client
import json
import re
import socket
import ssl
import sys
import unicodedata
from urllib.parse import urlsplit

from .contracts import BrowserReadError, Observation, PolicyError, utc_now
from .policy import source_url


MAX_SOURCE_BYTES = 512 * 1024
MAX_EXCERPT_CHARS = 12000
_OPENING_CHARS = 900
_WINDOW_CHARS = 1700
_MAX_WINDOWS = 6


class SourceError(ValueError):
    pass


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure):
        super().__init__(host, port, timeout=8)
        self.address = address
        self.secure = secure

    def connect(self):
        # The validated numeric IP is used for TCP, original host for TLS SNI.
        self.sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        if self.secure:
            try:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
            except BaseException:
                self.sock.close()
                raise


def fetch_source(url):
    parsed = urlsplit(source_url(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise PolicyError("Source DNS resolved to a private/reserved address")
    connection = PinnedConnection(parsed.hostname, port, addresses[0][4][0], parsed.scheme == "https")
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"User-Agent": "Jarvis-SourceReader/1", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            # No redirect follow-up, authentication or automatic retry.
            raise SourceError(f"HTTP status {response.status}")
        mime = response.getheader("Content-Type", "").lower()
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise SourceError("Compressed sources are not supported")
        if not (mime.startswith("text/html") or mime.startswith("text/plain")):
            raise SourceError("Unsupported content type")
        content = response.read(MAX_SOURCE_BYTES + 1)
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceError("Source exceeds byte limit")
        return mime, content
    finally:
        connection.close()


def _query_terms(query):
    """Small, language-neutral search vocabulary for selecting source passages."""
    if not query:
        return []
    words = re.findall(r"[a-z0-9][a-z0-9.+-]*|[\u3400-\u9fff]+",
                       unicodedata.normalize("NFKC", query).casefold()[:500])
    stop = {"about", "and", "are", "for", "from", "how", "the", "this", "what",
            "when", "where", "which", "with", "为什么", "是什么", "有哪些", "怎么"}
    terms = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 3:
            parts = [word[i:i + 3] for i in range(len(word) - 2)]
        else:
            parts = [word]
        terms.extend(part for part in parts if len(part) >= 2 and part not in stop)
    return list(dict.fromkeys(terms))[:16]


def _excerpt(source_text, query=None):
    """Keep an opening and query-relevant contiguous passages from a long page."""
    text = source_text.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    terms = _query_terms(query)
    if not terms:
        return text[:MAX_EXCERPT_CHARS - 1].rstrip() + "…"

    folded = unicodedata.normalize("NFKC", text).casefold()
    # NFKC can change offsets for unusual characters. Search the casefolded
    # original too when normalization changes length, preserving slice offsets.
    if len(folded) != len(text):
        folded = text.casefold()
    candidates = set()
    for term in terms:
        positions = [match.start() for match in re.finditer(re.escape(term), folded)]
        if len(positions) > 40:
            step = (len(positions) - 1) / 39
            positions = [positions[round(i * step)] for i in range(40)]
        for position in positions:
            if position < _OPENING_CHARS:
                continue
            start = max(_OPENING_CHARS, position - 350)
            # Prefer a nearby paragraph boundary without losing the match.
            boundary = text.rfind("\n", max(_OPENING_CHARS, start - 100), start)
            if boundary >= _OPENING_CHARS:
                start = boundary + 1
            candidates.add(start)
    if not candidates:
        return text[:MAX_EXCERPT_CHARS - 1].rstrip() + "…"

    ranked = []
    for start in candidates:
        end = min(len(text), start + _WINDOW_CHARS)
        passage = folded[start:end]
        hits = [term for term in terms if term in passage]
        score = sum(2 + min(len(term), 8) for term in hits)
        score += sum(min(passage.count(term), 3) for term in hits)
        # Contents lists tend to have many short lines; favor prose on ties.
        score += min(max((len(line) for line in text[start:end].splitlines()), default=0), 200) / 200
        ranked.append((score, start, end))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = [(0, _OPENING_CHARS)]
    for _, start, end in ranked:
        if any(start < chosen_end and end > chosen_start for chosen_start, chosen_end in selected):
            continue
        selected.append((start, end))
        if len(selected) > _MAX_WINDOWS:
            break
    selected.sort()
    pieces = []
    previous_end = 0
    for start, end in selected:
        if start > previous_end:
            pieces.append("\n…\n")
        pieces.append(text[start:end].strip())
        previous_end = end
    if previous_end < len(text):
        pieces.append("\n…")
    return "".join(pieces)[:MAX_EXCERPT_CHARS]


def _readable_text(page, *, include_attribution=False):
    body = page.locator("body").inner_text(timeout=5000).strip()
    if not body or include_attribution:
        return body
    main = page.locator("main, article, [role='main']")
    candidates = []
    for index in range(min(main.count(), 8)):
        try:
            candidate = main.nth(index).inner_text(timeout=5000).strip()
        except Exception:
            # Hidden or detached containers cannot prevent the body fallback.
            continue
        if len(candidate) >= 80:
            candidates.append(candidate)
    return max(candidates, key=len) if candidates else body


class EdgeBrowser:
    def read(self, url, query=None, *, include_attribution=False):
        if type(include_attribution) is not bool:
            raise ValueError("include_attribution must be a boolean")
        url = source_url(url)
        mime, content = fetch_source(url)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, timeout=15000,
                                         chromium_sandbox=True)
            try:
                context = browser.new_context(java_script_enabled=False, accept_downloads=False,
                                              service_workers="block")
                page = context.new_page()
                blocked_navigation = []

                def route_request(route):
                    req = route.request
                    permitted = (req.method == "GET" and req.is_navigation_request()
                                 and req.frame == page.main_frame and req.url == url)
                    if permitted:
                        # Render a bounded snapshot in the browser. Never let it
                        # resolve again, follow an HTTP redirect or fetch resources.
                        route.fulfill(status=200, body=content, headers={
                            "content-type": mime,
                            "content-security-policy": "default-src 'none'; frame-src 'none'; form-action 'none'",
                        })
                    else:
                        if req.is_navigation_request():
                            blocked_navigation.append(req.url)
                        route.abort()

                context.route("**/*", route_request)
                response = page.goto(url, wait_until="domcontentloaded", timeout=10000)
                if blocked_navigation or page.url != url:
                    raise PolicyError("Source redirected outside the exact authorized URL")
                if response is None or not response.ok:
                    raise ValueError("Source returned an unsuccessful HTTP response")
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "text/plain" not in content_type:
                    raise ValueError("Only HTML and plain text sources are supported")
                text = _readable_text(page, include_attribution=include_attribution)
                if not text:
                    raise ValueError("Source contains no readable text")
                return Observation(url, page.url, page.title()[:300], _excerpt(text, query), utc_now())
            finally:
                browser.close()


class BrowserProcess:
    """Fixed browser worker process, bounded across DNS, browser IO and shutdown."""
    def __init__(self, query=None, *, include_attribution=False):
        if type(include_attribution) is not bool:
            raise ValueError("include_attribution must be a boolean")
        self.query = query
        self.include_attribution = include_attribution

    def read(self, url, should_stop=lambda: False):
        from .process import run_child
        command = [sys.executable, "-m", "agent_service.browser", source_url(url)]
        if self.query is not None or self.include_attribution:
            command.append(str(self.query)[:500] if self.query is not None else "")
        if self.include_attribution:
            command.append("--include-attribution")
        result = run_child(command,
                           should_stop=should_stop, timeout=40)
        data = json.loads(result)
        if "error_code" in data:
            raise BrowserReadError(data["error_code"])
        return Observation(**data)


def _worker_read(argv):
    """Accept only the fixed optional flag after the existing URL/query slots."""
    if len(argv) not in (2, 3, 4) or len(argv) == 4 and argv[3] != "--include-attribution":
        raise ValueError("Invalid browser worker arguments")
    return EdgeBrowser().read(argv[1], argv[2] if len(argv) > 2 else None,
                              include_attribution=len(argv) == 4)


if __name__ == "__main__":
    # Parent assigns process-tree ownership BEFORE releasing this launch gate.
    if sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(2)
    try:
        from dataclasses import asdict
        observation = _worker_read(sys.argv)
        sys.stdout.buffer.write(json.dumps(asdict(observation), ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        # Do not return raw browser errors, URL secrets or local environment data.
        if isinstance(exc, socket.gaierror):
            code = "dns_failed"
        elif isinstance(exc, ssl.SSLError):
            code = "tls_failed"
        elif isinstance(exc, TimeoutError):
            code = "network_timeout"
        elif isinstance(exc, SourceError):
            code = "source_rejected"
        elif isinstance(exc, PolicyError):
            code = "policy_rejected"
        elif isinstance(exc, ImportError):
            code = "missing_dependency"
        elif isinstance(exc, OSError):
            code = "network_failed"
        else:
            code = "browser_failed"
        sys.stdout.buffer.write(json.dumps({"error_code": code}).encode("utf-8"))
