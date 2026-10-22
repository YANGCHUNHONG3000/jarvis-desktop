"""Bounded actual-anchor navigation with compressed, locally replayable proof.

Navigation is a candidate discovery hint; it never establishes publisher identity.
The owned child uses the existing DNS-pinned public GET gate without redirects.
"""
import base64
from hashlib import sha256
import json
import math
import re
import sys
from urllib.parse import urljoin, urlsplit
import zlib

from .computer import ComputerError
from .computer_search import validate_query, validate_result
from .contracts import utc_now
from .policy import source_url


MAX_HTML_BYTES = 512 * 1024
MAX_TOTAL_HTML_BYTES = 4 * MAX_HTML_BYTES
MAX_RESULT_CHARS = 270_000
MAX_PAGES = 4
MAX_DEPTH = 3
CROSSSITE_TIMEOUT = 40
_FORMAT = ('format', 'opendocument', 'odf', 'filetype', 'file-type', 'compatib', 'extension')
_GATEWAY = ('documentation', 'userguide', 'help', 'manual', 'instructions', 'installation',
            'getting-started', 'getting started', 'overview', 'desktop', 'docs', 'wiki')
_BINARY = re.compile(r'\.(?:exe|msi|dmg|deb|rpm|zip|gz|7z|pdf|png|jpg|svg|mp4)$', re.I)


def _fail():
    raise ComputerError('supplement_crosssite_proof_invalid')


def _request(url, query, excluded_urls):
    try:
        canonical = source_url(url)
        validate_query(query, 2, provider='bing', comparison=True)
        if (canonical != url or type(excluded_urls) not in (tuple, list)
                or len(excluded_urls) > 8 or any(type(v) is not str for v in excluded_urls)
                or not re.sub(r'\bsite:\S+', '', query, flags=re.I).strip()):
            _fail()
        request = {'source_url': canonical, 'query': query,
                   'excluded_urls': sorted({source_url(value) for value in excluded_urls})}
        request['request_sha256'] = sha256(json.dumps(request, sort_keys=True,
                                                    ensure_ascii=False).encode('utf-8')).hexdigest()
        return request
    except (ValueError, TypeError, AttributeError):
        _fail()


def _parse(content, mime, url, query):
    """Rank public anchors, retaining their actual document-order identity."""
    if not mime.startswith('text/html'):
        return [], False
    from bs4 import BeautifulSoup
    from .browser import _query_terms
    soup = BeautifulSoup(content, 'html.parser')
    for tag in soup(['script', 'style', 'template', 'noscript']):
        tag.decompose()
    base = soup.find('base', href=True)
    base_url = urljoin(url, base['href']) if base is not None else url
    words = _query_terms(re.sub(r'\bsite:\S+', '', query, flags=re.I))
    anchors = []
    for index, anchor in enumerate(soup.find_all('a', href=True)):
        href = anchor.get('href')
        if type(href) is not str or not href.strip() or href.strip().startswith('#'):
            continue
        try:
            target = source_url(urljoin(base_url, href.strip()))
        except (ValueError, TypeError):
            continue
        parsed = urlsplit(target)
        if _BINARY.search(parsed.path):
            continue
        label = anchor.get_text(' ', strip=True)[:1000]
        text = (parsed.path + ' ' + label).casefold()
        format_hits = sum(cue in text for cue in _FORMAT)
        gateway_hits = sum(cue in text for cue in _GATEWAY)
        # Gap vocabulary leads. Topic repetition cannot outrank format support.
        score = format_hits * 100 + min(3, gateway_hits) * 6
        if 'getting-started' in text or 'getting started' in text:
            score += 60
        if 'instructions' in text:
            score += 35
        if parsed.path.rstrip('/').count('/') == 1 and gateway_hits:
            score += 35
        score += min(3, sum(word in text for word in words))
        if any(cue in text for cue in ('changelog', 'troubleshoot', '/blog', '/news',
                                        '/download', '/connection', '/macros', '/api/')):
            score -= 90
        anchors.append({'url': target, 'anchor': index, 'score': score,
                        'format': bool(format_hits),
                        'home': parsed.path == '/' and not parsed.query
                        and (not label or label.casefold() in ('home', 'homepage', 'main page'))})
    body = soup.get_text(' ', strip=True).casefold()
    types = set(re.findall(r'\b(?:docx|odt|xlsx|ods|pptx|odp|csv|rtf)\b', body))
    # These are routing cues only. Evidence selection/review checks real support.
    format_page = (len(types) >= 2 and any(cue in body for cue in ('format', 'support', 'edit')))
    return anchors, format_page


def _pack(content):
    return base64.b64encode(zlib.compress(content, 9)).decode('ascii')


def _unpack(encoded):
    if type(encoded) is not str or len(encoded) > MAX_RESULT_CHARS:
        _fail()
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        content = decoder.decompress(compressed, MAX_HTML_BYTES + 1)
        if (not content or len(content) > MAX_HTML_BYTES or not decoder.eof
                or decoder.unconsumed_tail or decoder.unused_data):
            _fail()
        return content
    except (ValueError, zlib.error):
        _fail()


def _discover(request, fetch, searched_at):
    """Deterministic traversal used both for the worker and pure proof replay."""
    validate_result({'urls': [request['source_url']], 'searched_at': searched_at}, 2)
    pages, frontier, roots, chosen = [], [], {}, []
    visited = set()
    excluded = set(request['excluded_urls'])
    pending = {'url': request['source_url'], 'parent': None, 'anchor': None, 'depth': 0}
    while pending is not None and len(pages) < MAX_PAGES:
        url = pending['url']
        visited.add(url)
        page = dict(pending)
        try:
            mime, content = fetch(url)
        except Exception:
            page['status'] = 'unavailable'
            pages.append(page)
        else:
            if (type(content) is not bytes or not content or len(content) > MAX_HTML_BYTES
                    or type(mime) is not str or len(mime) > 300
                    or not (mime.startswith('text/html') or mime.startswith('text/plain'))):
                page['status'] = 'unavailable'
                pages.append(page)
            else:
                page.update(status='ok', mime=mime, html_zlib_base64=_pack(content),
                            source_sha256=sha256(content).hexdigest())
                pages.append(page)
                index = len(pages) - 1
                anchors, format_page = _parse(content, mime, url, request['query'])
                for anchor in anchors:
                    if anchor['url'] in excluded or anchor['url'] == request['source_url']:
                        continue
                    edge = {'url': anchor['url'], 'parent': index,
                            'anchor': anchor['anchor'], 'depth': page['depth'] + 1}
                    if edge['depth'] > MAX_DEPTH:
                        continue
                    if anchor['home'] and urlsplit(anchor['url']).hostname == urlsplit(url).hostname:
                        roots.setdefault(urlsplit(url).hostname, edge)
                    if anchor['score'] > 0 and anchor['url'] not in visited:
                        frontier.append((anchor['score'], anchor['format'], edge))
                came_from_format = pending.get('format_hint', False)
                if page['depth'] and url not in excluded and (format_page or came_from_format):
                    selected = {k: page[k] for k in ('url', 'parent', 'anchor', 'depth')}
                    chosen = [{**selected, 'role': 'format'}]
                    attribution = roots.get(urlsplit(url).hostname)
                    if attribution is not None and attribution['url'] != url:
                        chosen.append({**attribution, 'role': 'attribution'})
                    break
        pending = None
        # Follow one useful documentation branch instead of spending the bound
        # on sibling installers. An actual format anchor takes precedence.
        frontier.sort(key=lambda entry: (-int(entry[1]), -entry[2]['parent'],
                                         -entry[0], entry[2]['anchor'], entry[2]['url']))
        while frontier:
            _, hint, edge = frontier.pop(0)
            if edge['url'] not in visited and edge['url'] not in excluded:
                pending = {**edge, 'format_hint': hint}
                break
    urls = [edge['url'] for edge in chosen]
    value = {'discovery': {'urls': urls, 'searched_at': searched_at} if urls else None,
             'proof': {'version': 1, **request, 'pages': pages, 'chosen': chosen,
                       'chosen_urls': urls, 'searched_at': searched_at}}
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_RESULT_CHARS:
        raise ComputerError('supplement_crosssite_unavailable')
    return value


def validate_crosssite_result(value, url, query, excluded_urls=()):
    """Replay all fetched bytes, tree edges, ranking and selections without I/O."""
    request = _request(url, query, excluded_urls)
    try:
        if (type(value) is not dict or set(value) != {'discovery', 'proof'}
                or len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_RESULT_CHARS):
            _fail()
        proof = value['proof']
        if (type(proof) is not dict or set(proof) != {
                'version', *request, 'pages', 'chosen', 'chosen_urls', 'searched_at'}
                or type(proof['version']) is not int or proof['version'] != 1
                or any(proof.get(key) != item for key, item in request.items())
                or type(proof['pages']) is not list or not 1 <= len(proof['pages']) <= MAX_PAGES):
            _fail()
        decoded = []
        total = 0
        for page in proof['pages']:
            if type(page) is not dict or page.get('status') not in ('ok', 'unavailable'):
                _fail()
            if page['status'] == 'ok':
                content = _unpack(page['html_zlib_base64'])
                total += len(content)
                if total > MAX_TOTAL_HTML_BYTES:
                    _fail()
                decoded.append((page['url'], page['mime'], content))
            else:
                decoded.append((page['url'], None, None))
        cursor = 0
        def replay(target):
            nonlocal cursor
            if cursor >= len(decoded) or decoded[cursor][0] != target:
                _fail()
            _, mime, content = decoded[cursor]
            cursor += 1
            if mime is None:
                raise OSError('retained failed GET')
            return mime, content
        expected = _discover(request, replay, proof['searched_at'])
        # JSON comparison also distinguishes booleans from integer edge indices.
        if cursor != len(decoded) or json.dumps(value, sort_keys=True) != json.dumps(expected, sort_keys=True):
            _fail()
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail()


class CrossSiteDiscoveryProcess:
    def discover(self, url, query, excluded_urls=(), *, should_stop=lambda: False,
                 timeout=CROSSSITE_TIMEOUT):
        from .process import run_child
        request = _request(url, query, excluded_urls)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= CROSSSITE_TIMEOUT):
            raise ComputerError('supplement_crosssite_timeout')
        if should_stop():
            raise ComputerError('supplement_crosssite_cancelled')
        command = [sys.executable, '-B', '-m', 'agent_service.comparison_crosssite_discovery',
                   json.dumps(request, ensure_ascii=False)]
        try:
            raw = run_child(command, should_stop=should_stop, timeout=timeout)
        except InterruptedError:
            raise ComputerError('supplement_crosssite_cancelled') from None
        except TimeoutError:
            raise ComputerError('supplement_crosssite_timeout') from None
        except (OSError, RuntimeError, UnicodeError):
            raise ComputerError('supplement_crosssite_unavailable') from None
        if should_stop():
            raise ComputerError('supplement_crosssite_cancelled')
        try:
            if type(raw) is not str or len(raw.encode('utf-8')) > MAX_RESULT_CHARS:
                _fail()
            value = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            _fail()
        if type(value) is dict and value == {'error': 'supplement_crosssite_unavailable'}:
            raise ComputerError('supplement_crosssite_unavailable')
        return validate_crosssite_result(value, url, query, request['excluded_urls'])


def main():
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        raw = json.loads(sys.argv[1])
        if type(raw) is not dict or set(raw) != {'source_url', 'query', 'excluded_urls', 'request_sha256'}:
            raise ValueError()
        request = _request(raw['source_url'], raw['query'], raw['excluded_urls'])
        if raw != request:
            _fail()
        from .browser import fetch_source
        value = _discover(request, fetch_source, utc_now())
    except Exception:
        value = {'error': 'supplement_crosssite_unavailable'}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
