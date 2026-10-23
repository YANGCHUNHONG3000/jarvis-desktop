"""Dimension navigation from verified historical HTTP proofs and bounded public GETs.

Prior proofs are supplied only after their owning run has been fully verified.
Navigation selects actual links; it grants neither semantic nor publisher evidence.
"""
import base64
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit
import zlib

from .computer import ComputerError, digest, safe_root, write_new
from .computer_search import validate_query, validate_result
from .contracts import utc_now
from .policy import source_url

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_TOTAL_HTML_BYTES = 2 * 1024 * 1024
MAX_RESULT_CHARS = 270_000
MAX_PAGES = 4
MAX_DEPTH = 3
TIMEOUT = 40
_FORMULA = ('formula', 'formulas', 'formulae', 'calculate', 'calculation',
            'evaluate', 'evaluation')
_GATEWAYS = {'documentation', 'docs', 'help', 'manual', 'guide', 'userguide',
             'tutorial', 'overview', 'index'}
_STOP = {'site', 'the', 'and', 'for', 'with', 'does', 'can', 'how', 'what',
         'whether', 'result', 'results', 'excel', 'cell', 'cells'}
_TOPIC_STOP = _STOP | _GATEWAYS | set(_FORMULA) | {
    'python', 'common', 'library', 'libraries', 'tool', 'tools', 'support',
    'supported', 'supports', 'file', 'files', 'format', 'formats', 'using',
    'use', 'create', 'write', 'read', 'comparison', 'compare', 'compute',
    'computes', 'calculates', 'capability', 'capabilities'}
_BINARY = re.compile(r'\.(?:exe|msi|dmg|deb|rpm|zip|gz|7z|pdf|png|jpg|svg|mp4)$', re.I)


def _fail():
    raise ComputerError('supplement_dimension_proof_invalid')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _request(url, query, excluded_urls, prior_discoveries, *, routing_version=1, topic=None):
    try:
        canonical = source_url(url)
        validate_query(query, 2, provider='bing', comparison=True)
        if (canonical != url or type(excluded_urls) not in (tuple, list)
                or len(excluded_urls) > (10 if routing_version in (3, 4) else 8)
                or any(type(v) is not str for v in excluded_urls)
                or not re.sub(r'\bsite:\S+', '', query, flags=re.I).strip()
                or type(prior_discoveries) is not list
                or len(_json(prior_discoveries)) > MAX_INPUT_BYTES):
            _fail()
        if (type(routing_version) is not int or routing_version not in (1, 2, 3, 4)
                or routing_version == 1 and topic is not None
                or routing_version in (2, 3, 4) and (type(topic) is not str or not topic.strip()
                    or len(topic) > 500 or any(ord(c) < 32 for c in topic))):
            _fail()
        value = {'source_url': url, 'query': query,
                 'excluded_urls': sorted({source_url(v) for v in excluded_urls}),
                 'prior_sha256': digest(prior_discoveries)}
        if routing_version in (2, 3, 4):
            value.update(routing_version=routing_version, topic=topic)
        value['request_sha256'] = digest(value)
        return value
    except (ValueError, TypeError, AttributeError, RecursionError):
        _fail()


def _unpack(value):
    try:
        if type(value) is not str or len(value) > MAX_INPUT_BYTES:
            _fail()
        decoder = zlib.decompressobj()
        data = decoder.decompress(base64.b64decode(value, validate=True), MAX_HTML_BYTES + 1)
        if (not data or len(data) > MAX_HTML_BYTES or not decoder.eof
                or decoder.unconsumed_tail or decoder.unused_data):
            _fail()
        return data
    except (ValueError, TypeError, zlib.error):
        _fail()


def _cache(request, prior):
    """Keep original page identities/depths; never turn cached descendants into roots."""
    if request.get('routing_version') in (3, 4):
        return _cache_v3(request, prior)
    pages = []
    seen = set()
    for i, value in enumerate(prior):
        if type(value) is not dict or type(value.get('proof')) is not dict:
            _fail()
        proof = value['proof']
        if proof.get('source_url') != request['source_url']:
            continue
        version = proof.get('version')
        if type(version) is not int:
            _fail()
        if version not in (1, 2, 3, 4):
            continue  # Dynamic v5 resources need their own future adapter.
        if type(proof.get('pages')) is not list:
            continue  # The older single-page link proof has a different schema.
        for j, page in enumerate(proof['pages']):
            if (type(page) is not dict or page.get('status') not in {'ok', 'unavailable'}
                    or type(page.get('depth')) is not int or not 0 <= page['depth'] <= MAX_DEPTH
                    or source_url(page['url']) != page['url']):
                _fail()
            ref = {'prior': i, 'page': j, 'sha256': digest(page)}
            seen.add(page['url'])
            if page['status'] == 'ok':
                content = _unpack(page['html_zlib_base64'])
                if (sha256(content).hexdigest() != page.get('source_sha256')
                        or type(page.get('mime')) is not str or len(page['mime']) > 300):
                    _fail()
                pages.append({'url': page['url'], 'depth': page['depth'],
                              'ref': ref, 'mime': page['mime'], 'content': content})
    return pages, seen


def _cache_v3(request, prior):
    """Reuse dimension navigation pages with their original, verified reference graph."""
    pages, seen, retained = [], set(), set()
    allowed = (1, 2, 3, 4, 6, 7, 8) + ((9,) if request.get('routing_version') == 4 else ())
    prefix_hashes = {digest(prior[:i]): i for i in range(len(prior) + 1)}
    for i, value in enumerate(prior):
        if type(value) is not dict or type(value.get('proof')) is not dict:
            _fail()
        proof = value['proof']
        if proof.get('source_url') != request['source_url']:
            continue
        version = proof.get('version')
        if type(version) is not int:
            _fail()
        if version not in allowed:
            continue
        if type(proof.get('pages')) is not list:
            if version >= 6:
                _fail()
            continue
        if len(proof['pages']) > MAX_PAGES:
            _fail()
        if version >= 6:
            prefix = prefix_hashes.get(proof.get('prior_sha256'))
            if prefix is None or prefix > i or type(proof.get('cached_refs')) is not list:
                _fail()
            for ref in proof['cached_refs']:
                if (type(ref) is not dict or set(ref) != {'prior', 'page', 'sha256'}
                        or type(ref['prior']) is not int or not 0 <= ref['prior'] < prefix
                        or type(ref['page']) is not int or ref['page'] < 0):
                    _fail()
                origin = prior[ref['prior']]['proof']
                if (origin.get('source_url') != request['source_url']
                        or origin.get('version') not in allowed
                        or type(origin.get('pages')) is not list
                        or ref['page'] >= len(origin['pages'])
                        or digest(origin['pages'][ref['page']]) != ref['sha256']):
                    _fail()
        for j, page in enumerate(proof['pages']):
            if (type(page) is not dict or page.get('status') not in {'ok', 'unavailable'}
                    or type(page.get('depth')) is not int or not 0 <= page['depth'] <= MAX_DEPTH
                    or source_url(page['url']) != page['url']):
                _fail()
            seen.add(page['url'])
            if page['status'] != 'ok':
                continue
            content = _unpack(page['html_zlib_base64'])
            if (sha256(content).hexdigest() != page.get('source_sha256')
                    or type(page.get('mime')) is not str or len(page['mime']) > 300):
                _fail()
            key = (page['url'], page['source_sha256'], page['mime'])
            if key in retained:
                continue
            retained.add(key)
            pages.append({'url': page['url'], 'depth': page['depth'],
                          'ref': {'prior': i, 'page': j, 'sha256': digest(page)},
                          'mime': page['mime'], 'content': content})
    return pages, seen


def _terms(query):
    query = re.sub(r'\bsite:\S+', '', query, flags=re.I).casefold()
    words = set(re.findall(r'[a-z][a-z0-9]*', query)) - _STOP
    if '公式' in query or any(word in words for word in _FORMULA):
        return set(_FORMULA)
    return words


def _allowed(root, origin, target):
    from .project_scope import project_scope, requires_project
    host = urlsplit(target).hostname
    for parent in (root, origin):
        if host == urlsplit(parent).hostname and requires_project(parent):
            scope = project_scope(parent)
            if scope is None or project_scope(target) != scope:
                return False
    # Cross-host hosted projects also require a matching project identity.
    if requires_project(target) and host != urlsplit(origin).hostname:
        scope = project_scope(root)
        return scope is not None and project_scope(target) == scope
    return True


def _anchors(page, request):
    if not page['mime'].startswith('text/html') or page['depth'] >= MAX_DEPTH:
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page['content'], 'html.parser')
    for tag in soup(['script', 'style', 'template', 'noscript']):
        tag.decompose()
    base = soup.find('base', href=True)
    base_url = urljoin(page['url'], base['href']) if base is not None else page['url']
    terms = _terms(request['query'])
    anchors = []
    for index, anchor in enumerate(soup.find_all('a', href=True)):
        href = anchor.get('href')
        if type(href) is not str or not href.strip() or href.strip().startswith('#'):
            continue
        try:
            target = source_url(urljoin(base_url, href.strip()))
        except (ValueError, TypeError):
            continue
        if (_BINARY.search(urlsplit(target).path)
                or not _allowed(request['source_url'], page['url'], target)):
            continue
        if request.get('routing_version') == 4:
            path = urlsplit(target).path.casefold()
            if '_sources' in path.split('/') or path.endswith('.rst.txt'):
                continue  # Sphinx exports repeat a rendered document, not a new source.
        text = urlsplit(target).path + ' ' + anchor.get_text(' ', strip=True)[:1000]
        words = set(re.findall(r'[a-z][a-z0-9]*', text.casefold()))
        hits = len(words & terms)
        gateway = len(words & _GATEWAYS)
        if request.get('routing_version') in (2, 3, 4):
            if terms == set(_FORMULA):
                core = len(words & {'formula', 'formulas', 'formulae'})
                hits = core * 4 + hits if core else 0
            topic = request['topic'].casefold()
            if request.get('routing_version') in (3, 4):
                topic = re.sub(r'\bsite:\S+', '', topic, flags=re.I)
            topic_words = set(re.findall(r'[a-z][a-z0-9]*', topic)) - _TOPIC_STOP
            target_words = words | set(re.findall(r'[a-z][a-z0-9]*', urlsplit(target).netloc.casefold()))
            if topic_words:
                if not target_words & topic_words:
                    gateway = 0
            elif urlsplit(target).hostname != urlsplit(page['url']).hostname:
                gateway = 0
        if hits or gateway:
            anchors.append({'url': target, 'parent': page['ref'], 'anchor': index,
                            'depth': page['depth'] + 1, 'hits': hits, 'gateway': gateway})
    return anchors


def _discover(request, prior_discoveries, fetch, searched_at):
    """Pure traversal; fetch is injected, and validation supplies retained bytes only."""
    validate_result({'urls': [request['source_url']], 'searched_at': searched_at}, 2)
    cached, visited = _cache(request, prior_discoveries)
    excluded = set(request['excluded_urls'])
    references = [page['ref'] for page in cached]
    frontier = []
    for page in cached:
        frontier.extend(_anchors(page, request))
    pages, chosen = [], []
    total = 0
    root_pending = request['source_url'] not in visited
    while True:
        frontier = [edge for edge in frontier if edge['url'] not in visited
                    and edge['url'] not in excluded]
        frontier.sort(key=lambda e: (-e['hits'], -e['gateway'], e['depth'],
                                    _json(e['parent']), e['anchor'], e['url']))
        if frontier and frontier[0]['hits']:
            chosen = [frontier[0]]
            break  # The normal source reader performs the candidate's sole GET.
        if len(pages) >= MAX_PAGES:
            break
        if root_pending:
            pending = {'url': request['source_url'], 'parent': None, 'anchor': None, 'depth': 0}
            root_pending = False
        elif frontier:
            pending = frontier.pop(0)
            pending = {k: pending[k] for k in ('url', 'parent', 'anchor', 'depth')}
        else:
            break
        visited.add(pending['url'])
        page = dict(pending)
        try:
            mime, content = fetch(page['url'])
        except Exception:
            page['status'] = 'unavailable'
        else:
            if (type(content) is not bytes or not content or len(content) > MAX_HTML_BYTES
                    or type(mime) is not str or len(mime) > 300
                    or not mime.startswith(('text/html', 'text/plain'))):
                page['status'] = 'unavailable'
            else:
                total += len(content)
                if total > MAX_TOTAL_HTML_BYTES:
                    _fail()
                page.update(status='ok', mime=mime,
                            html_zlib_base64=base64.b64encode(zlib.compress(content, 9)).decode('ascii'),
                            source_sha256=sha256(content).hexdigest())
                frontier.extend(_anchors({'url': page['url'], 'depth': page['depth'],
                    'ref': {'new': len(pages)}, 'mime': mime, 'content': content}, request))
        pages.append(page)
    urls = [edge['url'] for edge in chosen]
    value = {'discovery': {'urls': urls, 'searched_at': searched_at} if urls else None,
             'proof': {'version': {2: 7, 3: 8, 4: 9}.get(request.get('routing_version'), 6),
                       **request, 'cached_refs': references, 'pages': pages,
                       'chosen': chosen, 'chosen_urls': urls, 'searched_at': searched_at}}
    if len(_json(value)) > MAX_RESULT_CHARS:
        raise ComputerError('supplement_dimension_unavailable')
    return value


def validate(value, url, query, excluded_urls=(), *, prior_discoveries, routing_version=1, topic=None):
    request = _request(url, query, excluded_urls, prior_discoveries,
                       routing_version=routing_version, topic=topic)
    try:
        if (type(value) is not dict or set(value) != {'discovery', 'proof'}
                or len(_json(value)) > MAX_RESULT_CHARS):
            _fail()
        proof = value['proof']
        if (type(proof) is not dict or type(proof.get('version')) is not int
                or proof['version'] != {1: 6, 2: 7, 3: 8, 4: 9}[routing_version]
                or type(proof.get('pages')) is not list
                or len(proof['pages']) > MAX_PAGES):
            _fail()
        retained, total = [], 0
        for page in proof['pages']:
            if page.get('status') == 'ok':
                content = _unpack(page['html_zlib_base64'])
                total += len(content)
                if total > MAX_TOTAL_HTML_BYTES:
                    _fail()
                retained.append((page['url'], page['mime'], content))
            elif page.get('status') == 'unavailable':
                retained.append((page['url'], None, None))
            else:
                _fail()
        cursor = 0
        def replay(target):
            nonlocal cursor
            if cursor >= len(retained) or retained[cursor][0] != target:
                _fail()
            _, mime, content = retained[cursor]
            cursor += 1
            if mime is None:
                raise OSError('retained failed GET')
            return mime, content
        expected = _discover(request, prior_discoveries, replay, proof['searched_at'])
        if cursor != len(retained) or _json(value) != _json(expected):
            _fail()
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail()


def _input(path, expected_hash):
    path = safe_root(path)
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        _fail()
    with path.open('rb') as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES or sha256(raw).hexdigest() != expected_hash:
        _fail()
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'request', 'prior_discoveries'}:
        _fail()
    request = value['request']
    if request != _request(request['source_url'], request['query'], request['excluded_urls'],
                           value['prior_discoveries'], routing_version=request.get('routing_version', 1),
                           topic=request.get('topic')):
        _fail()
    return value


class DimensionDiscoveryProcess:
    def discover(self, url, query, excluded_urls=(), *, prior_discoveries, storage_dir,
                 should_stop=lambda: False, timeout=TIMEOUT, routing_version=1, topic=None):
        from .process import run_child
        request = _request(url, query, excluded_urls, prior_discoveries,
                           routing_version=routing_version, topic=topic)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= TIMEOUT):
            raise ComputerError('supplement_dimension_timeout')
        if should_stop():
            raise ComputerError('supplement_dimension_cancelled')
        root = safe_root(storage_dir)
        if not root.is_dir():
            _fail()
        payload = {'request': request, 'prior_discoveries': prior_discoveries}
        # Match write_new's exact JSON encoding when comparing existing immutable inputs.
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(raw) > MAX_INPUT_BYTES:
            _fail()
        path = root / ('dimension-input-' + request['request_sha256'] + '.json')
        if safe_root(path) != path:
            _fail()
        if path.exists():
            if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
                _fail()
            with path.open('rb') as handle:
                retained = handle.read(MAX_INPUT_BYTES + 1)
            if retained != raw:
                _fail()
        else:
            write_new(path, payload)
        input_hash = sha256(raw).hexdigest()
        command = [sys.executable, '-B', '-m', 'agent_service.comparison_dimension_discovery',
                   str(path), input_hash]
        try:
            result = run_child(command, should_stop=should_stop, timeout=timeout)
        except InterruptedError:
            raise ComputerError('supplement_dimension_cancelled') from None
        except TimeoutError:
            raise ComputerError('supplement_dimension_timeout') from None
        except (OSError, RuntimeError, UnicodeError):
            raise ComputerError('supplement_dimension_unavailable') from None
        if should_stop():
            raise ComputerError('supplement_dimension_cancelled')
        _input(path, input_hash)
        try:
            if type(result) is not str or len(result.encode('utf-8')) > MAX_RESULT_CHARS:
                _fail()
            value = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            _fail()
        if value == {'error': 'supplement_dimension_unavailable'}:
            raise ComputerError('supplement_dimension_unavailable')
        return validate(value, url, query, excluded_urls, prior_discoveries=prior_discoveries,
                        routing_version=routing_version, topic=topic)


def main():
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    try:
        if len(sys.argv) != 3:
            _fail()
        payload = _input(sys.argv[1], sys.argv[2])
        from .browser import fetch_source
        value = _discover(payload['request'], payload['prior_discoveries'], fetch_source, utc_now())
    except Exception:
        value = {'error': 'supplement_dimension_unavailable'}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
