"""Free public web search followed by Jarvis's existing source-document worker.

Search results are untrusted URL candidates. The worker fetches each selected page
itself, writes literal source excerpts, and verifies the new file's bytes.
"""
import argparse
import base64
import binascii
from html import unescape
import json
import os
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit, urlunsplit
from urllib.request import getproxies
import uuid

import requests
from bs4 import BeautifulSoup

from .browser import BrowserProcess
from .contracts import utc_now
from .policy import PolicyError, source_url
from .store import JobStore
from .worker import Worker, verify_artifact


SEARCH_SECONDS = 20
WORKFLOW_SECONDS = 160  # Search (20s) plus at most three 40s source workers and local save.
MAX_HTML_BYTES = 512 * 1024
MAX_SOURCES = 3
SAFE_FAILURES = frozenset({"topic_invalid", "source_count_invalid", "proxy_invalid",
                           "search_timeout", "search_results_invalid", "root_invalid",
                           "workflow_timeout", "job_not_claimed"})
ENGINES = (
    ("duckduckgo", "https://html.duckduckgo.com/html/", "a.result__a"),
    ("bing", "https://www.bing.com/search", "li.b_algo h2 a"),
)


class ResearchError(ValueError):
    """A bounded, user-visible failure without response or page content."""


def _public_search_proxy():
    """Use an explicit system HTTPS proxy without enabling netrc or implicit auth."""
    configured = getproxies()
    raw = configured.get("https") or configured.get("all")
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise ResearchError("proxy_invalid") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ResearchError("proxy_invalid")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return urlunsplit((parsed.scheme, host + (f":{port}" if port else ""), "", "", ""))


def _bing_region_redirect(location):
    """One known HTTPS regional search endpoint; never follow arbitrary Location."""
    if not isinstance(location, str):
        return False
    try:
        parsed = urlsplit(location)
        return (parsed.scheme == "https" and parsed.hostname == "cn.bing.com"
                and parsed.port in {None, 443} and parsed.path == "/search"
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


def _candidate(href):
    if not isinstance(href, str):
        return None
    href = unescape(href)
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.netloc == "www.bing.com" and parsed.path == "/ck/a":
        # Bing HTML results encode their destination in u=a1<base64url>.
        # Decode data locally; never visit the tracking/redirect endpoint.
        if (parsed.scheme != "https" or parsed.fragment or len(href) > 8192
                or re.search(r"%(?![0-9a-fA-F]{2})", parsed.query)):
            return None
        try:
            # Current Bing HTML uses the literal sentinel !&& before its fields.
            # Remove only that exact prefix; remaining fields stay strict.
            query = parsed.query[3:] if parsed.query.startswith("!&&") else parsed.query
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=20)
            if (any(not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key) for key, _ in pairs)
                    or len({key for key, _ in pairs}) != len(pairs)):
                return None
            encoded = dict(pairs).get("u", "")
            if not re.fullmatch(r"a1[A-Za-z0-9_-]{1,2732}={0,2}", encoded):
                return None
            payload = encoded[2:]
            decoded = base64.b64decode(payload + "=" * (-len(payload) % 4),
                                       altchars=b"-_", validate=True)
            canonical = base64.urlsafe_b64encode(decoded).decode("ascii")
            if len(decoded) > 2048 or payload not in {canonical, canonical.rstrip("=")}:
                return None
            href = decoded.decode("utf-8", errors="strict")
        except (ValueError, UnicodeError, binascii.Error):
            return None
    if parsed.path == "/l/" and (parsed.hostname is None or parsed.hostname.endswith(".duckduckgo.com")
                                  or parsed.hostname == "duckduckgo.com"):
        href = parse_qs(parsed.query).get("uddg", [None])[0]
    if not href or not href.startswith("https://"):
        return None
    try:
        canonical = source_url(href)
    except (PolicyError, ValueError):
        return None
    host = urlsplit(canonical).hostname or ""
    if host in {"duckduckgo.com", "html.duckduckgo.com", "www.bing.com", "bing.com"}:
        return None
    return canonical


def _topic_terms(topic):
    """Extract useful words without treating a site filter as page content."""
    topic = re.sub(r"\bsite:\S+", " ", topic, flags=re.IGNORECASE)
    words = re.findall(r"[a-z0-9][a-z0-9._+-]*|[\u3400-\u9fff]+",
                       unicodedata.normalize("NFKC", topic).casefold())
    terms = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 3:
            terms.extend(word[i:i + 3] for i in range(len(word) - 2))
        elif len(word) >= 2:
            terms.append(word)
    return list(dict.fromkeys(terms))[:24]


def _result_score(url, title, terms):
    parsed = urlsplit(url)
    title = unicodedata.normalize("NFKC", title).casefold()
    path = unquote(parsed.path).casefold()
    host = (parsed.hostname or "").casefold()
    score = sum(4 * (term in title) + 3 * (term in path) + (term in host)
                for term in terms)
    # An index path is usually a weaker answer than a matching article.
    if path.endswith("/") or path.rsplit("/", 1)[-1] in {"index.html", "index.htm"}:
        score -= 2
    return score


def search_public(topic, max_sources=2, *, session=None, clock=time.monotonic, provider=None, comparison=False):
    """Read public HTML search pages; no account, key, browser profile or paid API."""
    if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 500:
        raise ResearchError("topic_invalid")
    if type(comparison) is not bool or comparison and provider != 'bing':
        raise ResearchError('search_provider_invalid')
    if type(max_sources) is not int or not 1 <= max_sources <= (4 if comparison else MAX_SOURCES):
        raise ResearchError("source_count_invalid")
    if provider is not None and provider != 'bing':
        raise ResearchError('search_provider_invalid')
    # A comparison's site: scope is a filter, never a browser permission.
    scope = None
    if provider == 'bing':
        scopes = re.findall(r'(?:^|\s)site:([^\s]+)', topic, flags=re.I)
        if scopes:
            if len(scopes) != 1 or not re.fullmatch(r'[A-Za-z0-9.-]+(?:/[A-Za-z0-9_./-]*)?',scopes[0]):
                raise ResearchError('topic_invalid')
            scope = urlsplit('https://' + scopes[0])
            if not scope.hostname or any(p in {'.','..'} for p in scope.path.split('/')):
                raise ResearchError('topic_invalid')
    owned_session = session is None
    proxy = _public_search_proxy() if owned_session else None
    if owned_session:
        session = requests.Session()
        # Netrc and implicit environment behavior remain disabled. A validated
        # system proxy is passed explicitly for this public search only.
        session.trust_env = False
    proxies = {"https": proxy} if proxy else {}
    deadline = clock() + SEARCH_SECONDS
    failures = []
    try:
        for name, endpoint, selector in ENGINES:
            if provider is not None and name != provider:
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                break
            try:
                def request_page(url):
                    left = deadline - clock()
                    if left <= 0:
                        raise ResearchError("search_timeout")
                    return session.get(url, params={"q": topic.strip()}, timeout=(3, min(8, left)),
                                       allow_redirects=False, stream=True, proxies=proxies,
                                       headers={"User-Agent": "Mozilla/5.0 (compatible; JarvisResearch/1.0)",
                                                "Accept": "text/html"})

                response = request_page(endpoint)
                if (name == "bing" and response.status_code == 302
                        and _bing_region_redirect(response.headers.get("Location"))):
                    response.close()
                    if owned_session:
                        session.cookies.clear()
                    response = request_page("https://cn.bing.com/search")
                try:
                    if response.status_code != 200:
                        failures.append(f"{name}:http_{response.status_code}")
                        continue
                    if "text/html" not in response.headers.get("Content-Type", "").lower():
                        failures.append(f"{name}:content_type")
                        continue
                    body = bytearray()
                    for chunk in response.iter_content(16384):
                        if clock() >= deadline:
                            raise ResearchError("search_timeout")
                        body.extend(chunk)
                        if len(body) > MAX_HTML_BYTES:
                            break
                finally:
                    response.close()
                if len(body) > MAX_HTML_BYTES:
                    failures.append(f"{name}:oversize")
                    continue
                soup = BeautifulSoup(bytes(body), "html.parser")
                text = soup.get_text(" ", strip=True).lower()
                if any(marker in text for marker in ("verify you are human", "unusual traffic", "captcha")):
                    failures.append(f"{name}:verification_required")
                    continue
                candidates = []
                seen = set()
                for link in soup.select(selector):
                    url = _candidate(link.get("href"))
                    if url and scope:
                        parsed = urlsplit(url)
                        host = parsed.hostname or ''
                        prefix = scope.path.rstrip('/')
                        if (host != scope.hostname and not host.endswith('.'+scope.hostname)
                                or prefix and parsed.path != prefix and not parsed.path.startswith(prefix+'/')):
                            continue
                    if url and url not in seen:
                        seen.add(url)
                        candidates.append((url, link.get_text(" ", strip=True)))
                    if len(candidates) == 20:
                        break
                if candidates:
                    terms = _topic_terms(topic)
                    candidates.sort(key=lambda item: -_result_score(item[0], item[1], terms))
                    return [url for url, _ in candidates[:max_sources]]
                failures.append(f"{name}:no_results")
            except requests.RequestException:
                failures.append(f"{name}:network_failed")
            finally:
                if owned_session:
                    session.cookies.clear()
    finally:
        if owned_session:
            session.close()
    raise ResearchError("search_unavailable:" + ",".join(failures or ["timeout"]))


def run_research(topic, root, max_sources=2, *, search=search_public, browser=None):
    """Isolate one JobStore per run so an older queued job cannot be claimed."""
    started = time.monotonic()
    urls = search(topic, max_sources)
    if time.monotonic() - started > SEARCH_SECONDS:
        raise ResearchError("search_timeout")
    if not urls or len(urls) > max_sources:
        raise ResearchError("search_results_invalid")
    urls = [_candidate(url) for url in urls]
    if any(url is None for url in urls) or len(set(urls)) != len(urls):
        raise ResearchError("search_results_invalid")
    base = Path(root).resolve()
    if base.exists() and not base.is_dir():
        raise ResearchError("root_invalid")
    base.mkdir(parents=True, exist_ok=True)
    run_root = base / ("research-" + uuid.uuid4().hex)
    store = JobStore(run_root)
    job = store.submit(topic, urls)
    class BoundedBrowser:
        def read(self, url, should_stop=lambda: False):
            # BrowserProcess has its own 40s process-tree deadline per page.
            if time.monotonic() - started > WORKFLOW_SECONDS - 40:
                raise ResearchError("workflow_timeout")
            return (browser or BrowserProcess(query=topic)).read(url, should_stop=should_stop)

    result = Worker(store, BoundedBrowser()).run_once()
    if result is None or result["id"] != job["id"]:
        raise ResearchError("job_not_claimed")
    readback = verify_artifact(store, job["id"]) if result["state"] == "succeeded" else {"ok": False}
    return {"ok": result["state"] == "succeeded" and readback["ok"],
            "job_id": job["id"], "state": result["state"], "reason": result["reason"],
            "sources": urls, "artifact": readback.get("path"),
            "readback_sha256": readback.get("sha256"), "root": str(run_root),
            "content_kind": "source_excerpts_without_model_summary"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search public pages and save verified source excerpts")
    parser.add_argument("topic", help="Research topic, up to 500 characters")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "agent_service")
    parser.add_argument("--max-sources", type=int, choices=(1, 2, 3), default=2)
    args = parser.parse_args(argv)
    try:
        attempts = args.root.resolve() / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        attempt_log = attempts / (uuid.uuid4().hex + ".jsonl")
        log = attempt_log.open("x", encoding="utf-8")
        with log:
            def record(event, **fields):
                log.write(json.dumps({"event": event, "at": utc_now(), **fields},
                                     ensure_ascii=False, separators=(",", ":")) + "\n")
                log.flush()
                os.fsync(log.fileno())

            record("started", topic=args.topic, max_sources=args.max_sources)
            try:
                result = run_research(args.topic, args.root, args.max_sources)
                code = 0 if result.get("ok") is True else 1
            except ResearchError as exc:
                detail = str(exc)
                safe = detail if detail in SAFE_FAILURES else "search_unavailable" if detail.startswith("search_unavailable:") else "research_failed"
                result, code = {"ok": False, "error": safe}, 1
            except OSError:
                result, code = {"ok": False, "error": "filesystem_error"}, 1
            except KeyboardInterrupt:
                result, code = {"ok": False, "error": "cancelled"}, 130
            except Exception as exc:
                result, code = {"ok": False, "error": "unexpected_" + type(exc).__name__}, 1
            record("result", result=result)
    except OSError:
        print(json.dumps({"ok": False, "error": "attempt_log_unavailable"}, ensure_ascii=False))
        return 1
    result["attempt_log"] = str(attempt_log)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
