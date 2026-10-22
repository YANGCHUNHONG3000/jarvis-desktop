"""Bounded public anchor discovery with self-contained, replayable source proof."""
import base64
from hashlib import sha256
import json
import math
import re
import sys
from urllib.parse import urljoin, urlsplit

from .computer import ComputerError
from .computer_search import validate_query, validate_result
from .contracts import utc_now
from .policy import source_url


MAX_HTML_BYTES = 180_000
MAX_RESULT_CHARS = 280_000
LINK_TIMEOUT = 20
_ERRORS = frozenset({'supplement_links_unavailable', 'supplement_links_source_too_large'})
_HINTS = ('about', 'company', 'copyright', 'legal', 'format', 'compatib', 'feature')
_GAPS = (
    ('about', 'company', 'publisher', 'copyright', 'legal', 'imprint', 'authorship'),
    ('format', 'opendocument', 'odf', 'compatib', 'filetype', 'file-type', 'extension'),
    ('feature',),
)


def _fail():
    raise ComputerError('supplement_links_proof_invalid')


def _request(url, query, excluded_urls):
    try:
        canonical = source_url(url)
        validate_query(query, 2, provider='bing', comparison=True)
        if (canonical != url or type(excluded_urls) not in (tuple, list)
                or len(excluded_urls) > 8 or any(type(v) is not str for v in excluded_urls)
                or not re.sub(r'\bsite:\S+', '', query, flags=re.IGNORECASE).strip()):
            _fail()
        excluded = sorted({source_url(value) for value in excluded_urls})
        return {'source_url': canonical, 'query': query, 'excluded_urls': excluded}
    except (ValueError, TypeError, AttributeError):
        _fail()


def _choose(content, mime, request):
    """Resolve actual anchors using document base and rank without network access."""
    if not mime.startswith('text/html'):
        return []
    from bs4 import BeautifulSoup
    from .browser import _query_terms
    soup = BeautifulSoup(content, 'html.parser')
    url = request['source_url']
    host = urlsplit(url).hostname
    base = soup.find('base', href=True)
    base_url = urljoin(url, base['href']) if base is not None else url
    query = re.sub(r'\bsite:\S+', '', request['query'], flags=re.IGNORECASE)
    terms = _query_terms(query)
    terms += [hint for hint in _HINTS if hint in query.casefold() and hint not in terms]
    gap_groups = [group for group in _GAPS if any(term in query.casefold() for term in group)]
    excluded = {*request['excluded_urls'], url}
    ranked = {}
    for index, anchor in enumerate(soup.find_all('a', href=True)):
        href = anchor.get('href')
        if type(href) is not str or not href.strip() or href.strip().startswith('#'):
            continue
        try:
            target = source_url(urljoin(base_url, href))
        except (ValueError, TypeError):
            continue
        if urlsplit(target).hostname != host or target in excluded:
            continue
        text = (urlsplit(target).path + ' ' + anchor.get_text(' ', strip=True)[:1000]).casefold()
        query_hits = sum(term in text for term in terms)
        hint_hits = sum(hint in text for hint in _HINTS)
        gap_hits = sum(sum(term in text for term in group) for group in gap_groups)
        # A requested evidence gap cannot be satisfied by unrelated navigation
        # bearing only the approved product/topic words.
        if gap_groups and not gap_hits:
            continue
        if not query_hits and not hint_hits:
            if not gap_hits:
                continue
        score = query_hits * 8 + hint_hits * 2
        rank = (-gap_hits, -score, index, target)
        if target not in ranked or rank < ranked[target]:
            ranked[target] = rank
    return sorted(ranked, key=ranked.get)[:2]


def make_link_result(url, query, excluded_urls, mime, content, searched_at):
    """Construct local proof; only the worker fetches bytes from the network."""
    request = _request(url, query, excluded_urls)
    if (type(content) is not bytes or not content or len(content) > MAX_HTML_BYTES
            or type(mime) is not str or len(mime) > 300
            or not (mime.startswith('text/html') or mime.startswith('text/plain'))):
        _fail()
    # Reuse the existing timestamp validation even when no links were found.
    validate_result({'urls': [url], 'searched_at': searched_at}, 2)
    urls = _choose(content, mime, request)
    proof = {'version': 1, **request, 'mime': mime,
             'html_base64': base64.b64encode(content).decode('ascii'),
             'source_sha256': sha256(content).hexdigest(), 'chosen_urls': urls,
             'searched_at': searched_at}
    result = {'discovery': {'urls': urls, 'searched_at': searched_at} if urls else None,
              'proof': proof}
    if len(json.dumps(result, ensure_ascii=False)) > MAX_RESULT_CHARS:
        _fail()
    return result


def validate_link_result(value, url, query, excluded_urls=()):
    """Recompute every chosen URL from the exact retained original bytes."""
    request = _request(url, query, excluded_urls)
    try:
        if type(value) is not dict or set(value) != {'discovery', 'proof'}:
            _fail()
        proof = value['proof']
        if (type(proof) is not dict or set(proof) != {
                'version', 'source_url', 'query', 'excluded_urls', 'mime', 'html_base64',
                'source_sha256', 'chosen_urls', 'searched_at'} or type(proof['version']) is not int
                or proof['version'] != 1 or any(proof.get(k) != v for k, v in request.items())
                or type(proof['html_base64']) is not str
                or len(proof['html_base64']) > 4 * ((MAX_HTML_BYTES + 2) // 3)):
            _fail()
        content = base64.b64decode(proof['html_base64'], validate=True)
        expected = make_link_result(url, query, request['excluded_urls'], proof['mime'],
                                    content, proof['searched_at'])
        if value != expected:
            _fail()
        return expected
    except (ValueError, TypeError, KeyError, AttributeError):
        _fail()


class LinkDiscoveryProcess:
    def discover(self, url, query, excluded_urls=(), *, should_stop=lambda: False, timeout=LINK_TIMEOUT):
        from .process import run_child
        request = _request(url, query, excluded_urls)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= LINK_TIMEOUT):
            raise ComputerError('supplement_links_timeout')
        if should_stop():
            raise ComputerError('supplement_links_cancelled')
        command = [sys.executable, '-B', '-m', 'agent_service.comparison_link_discovery',
                   json.dumps(request, ensure_ascii=False)]
        try:
            raw = run_child(command, should_stop=should_stop, timeout=timeout)
        except InterruptedError:
            raise ComputerError('supplement_links_cancelled') from None
        except TimeoutError:
            raise ComputerError('supplement_links_timeout') from None
        except (OSError, RuntimeError, UnicodeError):
            raise ComputerError('supplement_links_unavailable') from None
        if should_stop():
            raise ComputerError('supplement_links_cancelled')
        try:
            if type(raw) is not str or len(raw) > MAX_RESULT_CHARS:
                _fail()
            value = json.loads(raw)
        except (ValueError, TypeError):
            _fail()
        if (type(value) is dict and set(value) == {'error'}
                and type(value['error']) is str and value['error'] in _ERRORS):
            raise ComputerError('supplement_links_unavailable')
        return validate_link_result(value, url, query, request['excluded_urls'])


def main():
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        value = json.loads(sys.argv[1])
        if type(value) is not dict or set(value) != {'source_url', 'query', 'excluded_urls'}:
            raise ValueError()
        request = _request(value['source_url'], value['query'], value['excluded_urls'])
        from .browser import fetch_source
        mime, content = fetch_source(request['source_url'])
        if len(content) > MAX_HTML_BYTES:
            value = {'error': 'supplement_links_source_too_large'}
        else:
            value = make_link_result(request['source_url'], request['query'], request['excluded_urls'],
                                     mime, content, utc_now())
    except Exception:
        value = {'error': 'supplement_links_unavailable'}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
