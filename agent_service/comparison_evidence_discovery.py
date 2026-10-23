"""Proof10 navigation over upstream-verified captures; never supplies review answers.

Old page depths/references remain intact. ``extension_depth`` bounds only the
new edges in this request. Callers must verify the owning source/navigation runs
before providing their observations/proofs; this module binds and replays bytes.
"""
import base64
from hashlib import sha256
import json
import math
import re
import sys
from urllib.parse import urljoin, urlsplit
import zlib

from . import comparison_dimension_discovery as old
from .computer import ComputerError, digest, safe_root, write_new
from .computer_search import validate_query, validate_result
from .contracts import utc_now
from .policy import source_url

MAX_INPUT_BYTES = old.MAX_INPUT_BYTES
MAX_HTML_BYTES = old.MAX_HTML_BYTES
MAX_TOTAL_HTML_BYTES = old.MAX_TOTAL_HTML_BYTES
MAX_RESULT_CHARS = old.MAX_RESULT_CHARS
MAX_PAGES = 4
MAX_DEPTH = 3
MAX_INDEX_BYTES = 1024 * 1024
TIMEOUT = old.TIMEOUT
_GATEWAYS = old._GATEWAYS | {'usage', 'examples', 'using'}
_LITERAL = re.compile(r'https?://[^\s<>\[\]"\x00-\x20]+')
_VERB = re.compile(r"\b(?:evaluat(?:e|es)|calculat(?:e|es)|recalculat(?:e|es))\b|"
                   r'\b(?:are|is|be)\s+(?:not\s+)?(?:evaluated|calculated|recalculated)\b', re.I)


def _fail():
    raise ComputerError('supplement_dimension_proof_invalid')


def _json(value):
    return old._json(value)


def _request(url, query, excluded_urls, prior_discoveries, *, source_observations=(),
             routing_version=5, topic=None, navigation_policy=None):
    try:
        validate_query(query, 2, provider='bing', comparison=True)
        if (type(routing_version) is not int or routing_version != 5
                or source_url(url) != url or type(topic) is not str
                or not topic.strip() or len(topic) > 500 or any(ord(c) < 32 for c in topic)
                or not re.sub(r'\bsite:\S+', '', query, flags=re.I).strip()
                or type(excluded_urls) not in (tuple, list) or len(excluded_urls) > 16
                or any(type(v) is not str or source_url(v) != v for v in excluded_urls)
                or type(prior_discoveries) is not list
                or len(_json(prior_discoveries)) > MAX_INPUT_BYTES
                or type(source_observations) not in (tuple, list) or len(source_observations) > 6):
            _fail()
        if navigation_policy is not None and navigation_policy not in (
                'formula_direct_v2', 'formula_search_v3', 'formula_search_index_v4'):
            _fail()
        for observation in source_observations:
            if (type(observation) is not dict
                    or set(observation) != {'requested_url', 'final_url', 'title', 'text', 'captured_at', 'sha256'}
                    or any(type(v) is not str for v in observation.values())
                    or source_url(observation['requested_url']) != observation['requested_url']
                    or source_url(observation['final_url']) != observation['final_url']
                    or len(observation['title']) > 300 or not observation['text']
                    or len(observation['text']) > 12000
                    or sha256(observation['text'].encode()).hexdigest() != observation['sha256']):
                _fail()
            validate_result({'urls': [observation['final_url']], 'searched_at': observation['captured_at']}, 2)
        request = {'source_url': url, 'query': query, 'topic': topic, 'routing_version': 5,
                   'excluded_urls': sorted(set(excluded_urls)),
                   'prior_sha256': digest(prior_discoveries),
                   'observations_sha256': digest(list(source_observations))}
        if navigation_policy is not None:
            request['navigation_policy'] = navigation_policy
        request['request_sha256'] = digest(request)
        return request
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        _fail()


def _words(text):
    return set(re.findall(r'[a-z][a-z0-9]*', text.casefold()))


def _product_words(request):
    return _words(re.sub(r'\bsite:\S+', '', request['topic'], flags=re.I)) - old._TOPIC_STOP - _GATEWAYS


def _matches(request, url):
    """A routing identity, deliberately not a publisher/ownership assertion."""
    if not old._allowed(request['source_url'], request['source_url'], url):
        return False
    product = _product_words(request)
    if product:
        return bool(product & _words(urlsplit(url).hostname or '')) or url == request['source_url']
    return urlsplit(url).hostname == urlsplit(request['source_url']).hostname


def _unpack_index(value):
    try:
        if type(value) is not str or len(value) > MAX_INPUT_BYTES:
            _fail()
        decoder = zlib.decompressobj()
        data = decoder.decompress(base64.b64decode(value, validate=True), MAX_INDEX_BYTES + 1)
        if (not data or len(data) > MAX_INDEX_BYTES or not decoder.eof
                or decoder.unconsumed_tail or decoder.unused_data):
            _fail()
        return data
    except (ValueError, TypeError, zlib.error):
        _fail()


def _content_limit(page, mime, large_index):
    return (MAX_INDEX_BYTES if large_index and page.get('resource_kind') == 'sphinx_search_index_v1'
            and type(mime) is str and mime.startswith(('text/plain', 'application/javascript',
                'text/javascript', 'application/x-javascript')) else MAX_HTML_BYTES)


def _result_limit(policy):
    # Every complete proof must fit the existing 300000-byte receipt reader
    # after its ordinary action/envelope fields are added.
    return MAX_RESULT_CHARS


def _decoded(page, ref, captured_at, *, allow_resource=False, large_index=False):
    if (type(page) is not dict or source_url(page['url']) != page['url']
            or type(page.get('depth')) is not int or not 0 <= page['depth'] <= 64
            or page.get('status') not in {'ok', 'unavailable'}):
        _fail()
    if page['status'] == 'unavailable':
        return None
    resource = page.get('resource_kind')
    resource_ok = allow_resource and resource == 'sphinx_search_index_v1'
    content = (_unpack_index(page['html_zlib_base64'])
               if _content_limit(page, page.get('mime'), large_index) == MAX_INDEX_BYTES
               else old._unpack(page['html_zlib_base64']))
    if (resource is not None and not resource_ok
            or sha256(content).hexdigest() != page.get('source_sha256')
            or type(page.get('mime')) is not str or len(page['mime']) > 300
            or not page['mime'].startswith(('text/html', 'text/plain') + (
                ('application/javascript', 'text/javascript', 'application/x-javascript') if resource_ok else ()))):
        _fail()
    validate_result({'urls': [page['url']], 'searched_at': captured_at}, 2)
    result = {'url': page['url'], 'depth': page['depth'], 'extension_depth': 0,
            'ref': ref, 'mime': page['mime'], 'content': content,
            'source_sha256': page['source_sha256'], 'captured_at': captured_at}
    if resource_ok:
        if (source_url(page.get('document_base')) != page['document_base']
                or not _same_host(page['url'], page['document_base'])):
            _fail()
        result['resource_kind'] = resource
        result['document_base'] = page['document_base']
    return result


def _inventory(prior):
    pages, visited, identities = [], set(), set()
    for i, value in enumerate(prior):
        if type(value) is not dict or type(value.get('proof')) is not dict:
            _fail()
        proof = value['proof']
        version = proof.get('version')
        if type(version) is not int or version not in (*range(1, 12), 13, 14):
            _fail()
        if type(proof.get('pages')) is not list:
            if version >= 6:
                _fail()
            continue
        if len(proof['pages']) > MAX_PAGES:
            _fail()
        for j, page in enumerate(proof['pages']):
            item = _decoded(page, {'prior': i, 'page': j, 'sha256': digest(page)}, proof.get('searched_at'),
                            allow_resource=version in (13, 14), large_index=version == 14)
            visited.add(page['url'])
            if item and (item['url'], item['source_sha256'], item['mime']) not in identities:
                pages.append(item)
                identities.add((item['url'], item['source_sha256'], item['mime']))
    return pages, visited


def cached_page(url, prior_discoveries, currentproof=None):
    """Decode a retained page after upstream verification/``validate``.

    Returns original capture time and reference. This helper performs no GET and
    does not replace validation of the owning proof and its request inputs.
    """
    try:
        if source_url(url) != url or type(prior_discoveries) is not list:
            _fail()
        pages, _ = _inventory(prior_discoveries)
        if currentproof is not None:
            proof = currentproof.get('proof', currentproof)
            if type(proof) is not dict or proof.get('version') not in (10, 11, 13, 14):
                _fail()
            for i, page in enumerate(proof['pages']):
                item = _decoded(page, {'new': i}, proof['searched_at'],
                                allow_resource=proof['version'] in (13, 14), large_index=proof['version'] == 14)
                if item:
                    pages.append(item)
        matches = [page for page in pages if page['url'] == url]
        if not matches:
            return None
        # Ambiguous historic bytes must not silently choose a newer capture.
        if len({(p['source_sha256'], p['mime']) for p in matches}) != 1:
            _fail()
        return dict(matches[-1])
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        _fail()


def _soup(page):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page['content'], 'html.parser')
    for tag in soup(['script', 'style', 'template', 'noscript']):
        tag.decompose()
    return soup


def _passages(page):
    if page.get('resource_kind'):
        return False, False
    text = _soup(page).get_text('\n', strip=True) if page['mime'].startswith('text/html') else page['content'].decode('utf-8', errors='replace')
    # Sentence/line boundaries keep a distant mention from granting a match.
    direct = any(re.search(r'\bformula(?:s|e)?\b', s, re.I) and _VERB.search(s)
                 for s in re.split(r'(?<=[.!?])\s+|\n+', text) if len(s) <= 600)
    author = re.search(r'\bauthors?\s*:\s*([A-Za-z][^\n]{2,150})', text, re.I)
    copyright_line = re.search(r'(?:©[ \t]*|\bcopyright[ \t]+)(?:Copyright[ \t]+)?[\d \t,–—-]*([A-Za-z][^\n]{2,150})', text, re.I)
    attribution = bool(author or copyright_line and not re.match(
        r'(?:See\s+AUTHORS\b|All\s+rights\s+reserved\b|Created\s+using\b|Built\s+with\b)',
        copyright_line[1], re.I))
    return direct, attribution


def _edge(request, origin, target, label, parent, depth, extension_depth, **basis):
    try:
        target = source_url(target)
    except (ValueError, TypeError):
        return None
    path = urlsplit(target).path.casefold()
    if (old._BINARY.search(path) or '_sources' in path.split('/') or path.endswith('.rst.txt')
            or not old._allowed(request['source_url'], origin, target)
            or not _matches(request, target)):
        return None
    words = _words(path + ' ' + label)
    core = len(words & {'formula', 'formulas', 'formulae'})
    hits = core * 4 + len(words & set(old._FORMULA)) if core else 0
    calculation_terms = len(words & (set(old._FORMULA) - {'formula', 'formulas', 'formulae'}))
    gateway = len(words & _GATEWAYS)
    # A literal product URL may be a bare documentation hostname.
    if 'literal' in basis and _product_words(request) & _words(urlsplit(target).hostname or ''):
        gateway = max(gateway, 1)
    if not hits and not gateway:
        return None
    priority = 2 if words & {'usage', 'using'} else 1 if 'examples' in words else 0
    return {'url': target, 'parent': parent, 'depth': depth, 'extension_depth': extension_depth,
            'hits': hits, 'calculation_terms': calculation_terms,
            'gateway': gateway, 'gateway_priority': priority, **basis}


def _anchors(page, request):
    if not page['mime'].startswith('text/html') or page['extension_depth'] >= MAX_DEPTH:
        return []
    soup = _soup(page)
    base = soup.find('base', href=True)
    base_url = urljoin(page['url'], base['href']) if base else page['url']
    edges = []
    for i, anchor in enumerate(soup.find_all('a', href=True)):
        href = anchor['href']
        if not href.strip() or href.strip().startswith('#'):
            continue
        edge = _edge(request, page['url'], urljoin(base_url, href.strip()), anchor.get_text(' ', strip=True)[:1000],
                     page['ref'], page['depth'] + 1, page['extension_depth'] + 1, anchor=i)
        if edge:
            edges.append(edge)
    return edges


def _literals(observations, request):
    edges = []
    for i, observation in enumerate(observations):
        origin = observation['final_url']
        # Source path can identify an approved GitHub product even on github.com.
        product = _product_words(request)
        if product and not product & _words(urlsplit(origin).hostname + ' ' + urlsplit(origin).path):
            continue
        for match in _LITERAL.finditer(observation['text']):
            literal = match[0].rstrip('.,;:!?)')
            edge = _edge(request, origin, literal, '',
                         {'source': i, 'sha256': digest(observation)}, 1, 1,
                         literal={'start': match.start(), 'end': match.start() + len(literal), 'text': literal})
            if edge:
                edges.append(edge)
    return edges


def _same_host(left, right):
    return urlsplit(left).hostname == urlsplit(right).hostname


def _search_edges(page, request):
    """Only observed search anchors and observed same-host index script URLs."""
    if page['extension_depth'] >= MAX_DEPTH or not page['mime'].startswith('text/html'):
        return []
    from bs4 import BeautifulSoup
    raw = BeautifulSoup(page['content'], 'html.parser')
    base = raw.find('base', href=True)
    base_url = urljoin(page['url'], base['href']) if base else page['url']
    edges = []
    soup = _soup(page)
    for i, anchor in enumerate(soup.find_all('a', href=True)):
        href = anchor['href'].strip()
        if not href or href.startswith('#'):
            continue
        try:
            target = source_url(urljoin(base_url, href))
        except (ValueError, TypeError):
            continue
        words = _words(urlsplit(target).path + ' ' + anchor.get_text(' ', strip=True))
        if ('search' not in words or not _same_host(page['url'], target)
                or not _matches(request, target) or not old._allowed(request['source_url'], page['url'], target)
                or not urlsplit(target).path.lower().endswith(('.html', '/search'))):
            continue
        edges.append({'url': target, 'parent': page['ref'], 'anchor': i,
                      'depth': page['depth'] + 1, 'extension_depth': page['extension_depth'] + 1,
                      'hits': 0, 'calculation_terms': 0, 'gateway': 1,
                      'gateway_priority': 0, 'search_gateway': True})
    if 'search' not in _words(urlsplit(page['url']).path):
        return edges
    for i, script in enumerate(raw.find_all('script', src=True)):
        try:
            target = source_url(urljoin(base_url, script['src'].strip()))
        except (ValueError, TypeError):
            continue
        if (not _same_host(page['url'], target) or not _matches(request, target)
                or not old._allowed(request['source_url'], page['url'], target)
                or not re.fullmatch(r'searchindex(?:[.-][a-z0-9_-]+)?\.js',
                                    urlsplit(target).path.rsplit('/', 1)[-1], re.I)):
            continue
        edges.append({'url': target, 'parent': page['ref'], 'script': i,
                      'depth': page['depth'] + 1, 'extension_depth': page['extension_depth'] + 1,
                      'hits': 0, 'calculation_terms': 0, 'gateway': 1,
                      'gateway_priority': 0, 'resource_kind': 'sphinx_search_index_v1',
                      'document_base': source_url(urljoin(page['url'], '.'))})
    return edges


def _index_edges(page, request):
    """Parse a static Sphinx index, never JavaScript expressions or executable code."""
    if page.get('resource_kind') != 'sphinx_search_index_v1' or page['extension_depth'] >= MAX_DEPTH:
        return []
    try:
        text = page['content'].decode('utf-8')
        prefix = re.match(r'\s*Search\.setIndex\s*\(\s*', text)
        if prefix is None:
            return []
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('duplicate JSON key')
                value[key] = item
            return value
        def nonfinite(value):
            raise ValueError('non-JSON number')
        decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=nonfinite)
        data, end = decoder.raw_decode(text, prefix.end())
        if not re.fullmatch(r'\s*\)\s*;?\s*', text[end:]) or type(data) is not dict:
            return []
        names, titles = data.get('docnames'), data.get('titles')
        if (type(names) is not list or type(titles) is not list or not 1 <= len(names) <= 5000
                or len(names) != len(titles) or any(type(v) is not str for v in names + titles)
                or len(set(names)) != len(names) or any(len(v) > 1000 for v in titles)):
            return []
        # Retain exact top-level array locations and exact JSON string tokens.
        fields, cursor = {}, prefix.end() + 1
        while True:
            cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
            if text[cursor] == '}':
                break
            key, cursor = decoder.raw_decode(text, cursor)
            cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
            if text[cursor] != ':':
                return []
            cursor += 1
            cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
            start = cursor
            _, cursor = decoder.raw_decode(text, cursor)
            fields[key] = (start, cursor)
            cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
            if text[cursor] == '}':
                break
            if text[cursor] != ',':
                return []
            cursor += 1
        def tokens(field):
            cursor = fields[field][0] + 1
            result = []
            for _ in names:
                cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
                start = cursor
                _, cursor = decoder.raw_decode(text, cursor)
                result.append({'start': start, 'end': cursor, 'text': text[start:cursor]})
                cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
                if text[cursor] == ',':
                    cursor += 1
            return result
        name_tokens, title_tokens = tokens('docnames'), tokens('titles')
        edges = []
        for i, (name, title) in enumerate(zip(names, titles)):
            # This is the versioned Sphinx docname -> .html contract, not a
            # guessed project path. Absolute paths, queries and escapes reject.
            if (not re.fullmatch(r'[A-Za-z0-9_./-]{1,250}', name)
                    or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/'))
                    or name.endswith(('.html', '.js'))):
                return []
            matched = sorted(_words(name + ' ' + title) & set(old._FORMULA))
            if not set(matched) & {'formula', 'formulas', 'formulae'}:
                continue
            target = urljoin(page['document_base'], name + '.html')
            if not _same_host(page['url'], target):
                continue
            edge = _edge(request, page['url'], target, title, page['ref'],
                         page['depth'] + 1, page['extension_depth'] + 1,
                         index_entry={'document': i, 'docname': name, 'title': title,
                             'docname_token': name_tokens[i], 'title_token': title_tokens[i],
                             'json_start': prefix.end(), 'json_end': end,
                             'json_sha256': sha256(text[prefix.end():end].encode()).hexdigest(),
                             'matching_terms': matched})
            if edge:
                edges.append(edge)
        return edges
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        return []


def _index_retry(prior):
    successes, attempted14 = set(), set()
    for value in prior:
        proof = value['proof']
        for page in proof.get('pages', []):
            if page.get('status') == 'ok':
                successes.add(page['url'])
            if proof.get('version') == 14:
                attempted14.add(page['url'])
    for i, value in enumerate(prior):
        if value['proof'].get('version') != 13:
            continue
        for j, page in enumerate(value['proof'].get('pages', [])):
            if (page.get('status') == 'unavailable'
                    and page.get('resource_kind') == 'sphinx_search_index_v1'
                    and page['url'] not in successes | attempted14):
                return {page['url']: {'prior': i, 'page': j, 'sha256': digest(page)}}
    return {}


def _discover_search(request, prior_discoveries, fetch, searched_at, *, source_observations=()):
    validate_result({'urls': [request['source_url']], 'searched_at': searched_at}, 2)
    cached, visited = _inventory(prior_discoveries)
    cached = [p for p in cached if _matches(request, p['url'])]
    large_index = request['navigation_policy'] == 'formula_search_index_v4'
    retry = _index_retry(prior_discoveries) if large_index else {}
    visited.difference_update(retry)
    visited.update(o[k] for o in source_observations for k in ('requested_url', 'final_url'))
    excluded = set(request['excluded_urls'])
    literal_edges = _literals(source_observations, request)
    frontier, pages, chosen, evidence_pages = list(literal_edges), [], [], list(cached)
    def expand(page):
        edges = _anchors(page, request) + _search_edges(page, request) + _index_edges(page, request)
        for edge in edges:
            if edge.get('resource_kind') == 'sphinx_search_index_v1' and edge['url'] in retry:
                edge['retry_from'] = retry[edge['url']]
        return edges
    for page in cached:
        frontier.extend(expand(page))
    total = 0
    root_pending = request['source_url'] not in visited
    while True:
        direct = next((p for p in evidence_pages if p['url'] not in excluded and _passages(p)[0]), None)
        if direct:
            chosen = [{'url': direct['url'], 'page': direct['ref'], 'reason': 'formula_passage'}]
            host = urlsplit(direct['url']).hostname
            author = next((p for p in evidence_pages if p['url'] not in excluded
                           and p['url'] != direct['url'] and urlsplit(p['url']).hostname == host
                           and _passages(p)[1]), None)
            if author and not _passages(direct)[1]:
                chosen.append({'url': author['url'], 'page': author['ref'], 'reason': 'attribution_passage'})
            break
        frontier = [e for e in frontier if e['url'] not in visited and e['url'] not in excluded
                    and (e.get('resource_kind') or not urlsplit(e['url']).path.casefold().endswith('.js'))]
        def priority(edge):
            if 'index_entry' in edge:
                return 6
            if edge.get('resource_kind'):
                return 5
            path = urlsplit(edge['url']).path.casefold()
            if edge['hits'] and 'api' not in path.split('/') and not _words(path) & {'example', 'examples'}:
                return 4
            if edge.get('search_gateway'):
                return 3
            return edge['gateway_priority']
        frontier.sort(key=lambda e: (-priority(e), -e['hits'], -e['gateway'],
                                     e['extension_depth'], _json(e)))
        if len(pages) >= MAX_PAGES:
            break
        if frontier:
            edge = frontier.pop(0)
            pending = {k: edge[k] for k in ('url', 'parent', 'depth', 'extension_depth')}
            pending.update({k: edge[k] for k in ('anchor', 'literal', 'script', 'resource_kind', 'document_base', 'index_entry', 'retry_from',
                                                'search_gateway') if k in edge})
        elif root_pending:
            pending = {'url': request['source_url'], 'parent': None, 'depth': 0, 'extension_depth': 0}
            root_pending = False
        else:
            break
        visited.add(pending['url'])
        page = dict(pending)
        try:
            mime, content = fetch(page['url'])
        except Exception:
            page['status'] = 'unavailable'
        else:
            allowed = ('text/html', 'text/plain') + (('application/javascript', 'text/javascript', 'application/x-javascript')
                         if page.get('resource_kind') == 'sphinx_search_index_v1' else ())
            if (type(content) is not bytes or not content or len(content) > _content_limit(page, mime, large_index)
                    or type(mime) is not str or len(mime) > 300 or not mime.startswith(allowed)):
                page['status'] = 'unavailable'
            else:
                total += len(content)
                if total > MAX_TOTAL_HTML_BYTES:
                    _fail()
                page.update(status='ok', mime=mime, source_sha256=sha256(content).hexdigest(),
                            html_zlib_base64=base64.b64encode(zlib.compress(content, 9)).decode('ascii'))
                retained = _decoded(page, {'new': len(pages)}, searched_at,
                                    allow_resource=True, large_index=large_index)
                retained['extension_depth'] = page['extension_depth']
                evidence_pages.append(retained)
                frontier.extend(expand(retained))
        pages.append(page)
    urls = [c['url'] for c in chosen]
    value = {'discovery': {'urls': urls, 'searched_at': searched_at} if urls else None,
             'proof': {'version': 14 if large_index else 13, **request, 'cached_refs': [p['ref'] for p in cached],
                       'literal_edges': literal_edges, 'pages': pages, 'chosen': chosen,
                       'chosen_urls': urls, 'searched_at': searched_at}}
    if len(_json(value)) > _result_limit(request['navigation_policy']):
        raise ComputerError('supplement_dimension_unavailable')
    return value


def _discover(request, prior_discoveries, fetch, searched_at, *, source_observations=()):
    if request.get('navigation_policy') in ('formula_search_v3', 'formula_search_index_v4'):
        return _discover_search(request, prior_discoveries, fetch, searched_at,
                                source_observations=source_observations)
    validate_result({'urls': [request['source_url']], 'searched_at': searched_at}, 2)
    cached, visited = _inventory(prior_discoveries)
    cached = [page for page in cached if _matches(request, page['url'])]
    visited.update(o[k] for o in source_observations for k in ('requested_url', 'final_url'))
    excluded = set(request['excluded_urls'])
    frontier, pages, chosen = [], [], []
    literal_edges = _literals(source_observations, request)
    frontier.extend(literal_edges)
    for page in cached:
        frontier.extend(_anchors(page, request))
    evidence_pages = list(cached)
    total = 0
    root_pending = request['source_url'] not in visited
    while True:
        direct = [p for p in evidence_pages if p['url'] not in excluded and _passages(p)[0]]
        if direct:
            page = direct[0]
            chosen = [{'url': page['url'], 'page': page['ref'], 'reason': 'formula_passage'}]
        frontier = [e for e in frontier if e['url'] not in visited and e['url'] not in excluded]
        if request.get('navigation_policy') == 'formula_direct_v2':
            # Formula noun links are still fetched, not accepted as calculation
            # evidence. Ordinary API symbol pages follow usage/examples routes.
            def priority(edge):
                path = urlsplit(edge['url']).path.casefold()
                formula_document = (edge['hits'] and 'api' not in path.split('/')
                                    and not _words(path) & {'example', 'examples'})
                return 3 if formula_document else edge['gateway_priority']
            frontier.sort(key=lambda e: (-bool(e['hits'] and e['calculation_terms']), -priority(e),
                                         -e['hits'], -e['gateway'], e['extension_depth'], _json(e)))
        else:
            frontier.sort(key=lambda e: (-bool(e['hits'] and e['calculation_terms']), -e['gateway_priority'],
                                         -e['hits'], -e['gateway'], e['extension_depth'], _json(e)))
        # A formula noun also labels parsing/API pages. Traverse that page within
        # the GET bound; only an additional calculation/evaluation term permits
        # selecting an unread target directly for the normal source reader.
        if not chosen and frontier and frontier[0]['hits'] and frontier[0]['calculation_terms']:
            chosen = [dict(frontier[0], reason='formula_link')]
        if chosen:
            host = urlsplit(chosen[0]['url']).hostname
            attribution = next((p for p in evidence_pages if p['url'] not in excluded
                                and p['url'] != chosen[0]['url'] and urlsplit(p['url']).hostname == host
                                and _passages(p)[1]), None)
            if attribution and not any(p['url'] == chosen[0]['url'] and _passages(p)[1] for p in evidence_pages):
                chosen.append({'url': attribution['url'], 'page': attribution['ref'], 'reason': 'attribution_passage'})
            break
        if len(pages) >= MAX_PAGES:
            break
        if frontier:
            edge = frontier.pop(0)
            pending = {k: edge[k] for k in ('url', 'parent', 'depth', 'extension_depth')}
            pending.update({k: edge[k] for k in ('anchor', 'literal') if k in edge})
        elif root_pending:
            pending = {'url': request['source_url'], 'parent': None, 'depth': 0, 'extension_depth': 0}
            root_pending = False
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
                page.update(status='ok', mime=mime, source_sha256=sha256(content).hexdigest(),
                            html_zlib_base64=base64.b64encode(zlib.compress(content, 9)).decode('ascii'))
                retained = _decoded(page, {'new': len(pages)}, searched_at)
                retained['extension_depth'] = page['extension_depth']
                evidence_pages.append(retained)
                frontier.extend(_anchors(retained, request))
        pages.append(page)
    urls = [c['url'] for c in chosen]
    value = {'discovery': {'urls': urls, 'searched_at': searched_at} if urls else None,
             'proof': {'version': 11 if 'navigation_policy' in request else 10,
                       **request, 'cached_refs': [p['ref'] for p in cached],
                       'literal_edges': literal_edges, 'pages': pages, 'chosen': chosen,
                       'chosen_urls': urls, 'searched_at': searched_at}}
    if len(_json(value)) > MAX_RESULT_CHARS:
        raise ComputerError('supplement_dimension_unavailable')
    return value


def validate(value, url, query, excluded_urls=(), *, prior_discoveries,
             source_observations=(), routing_version=5, topic=None, navigation_policy=None):
    # Legacy receipts have no policy in either request or proof. Infer only the
    # versioned policy present in proof11, never upgrade a proof10 on replay.
    if type(value) is dict and type(value.get('proof')) is dict and value['proof'].get('version') in (11, 13, 14):
        supplied = value['proof'].get('navigation_policy')
        policy = {11: 'formula_direct_v2', 13: 'formula_search_v3', 14: 'formula_search_index_v4'}[value['proof']['version']]
        if supplied != policy or navigation_policy not in (None, supplied):
            _fail()
        navigation_policy = supplied
    request = _request(url, query, excluded_urls, prior_discoveries,
                       source_observations=source_observations, routing_version=routing_version,
                       topic=topic, navigation_policy=navigation_policy)
    try:
        if (type(value) is not dict or set(value) != {'discovery', 'proof'}
                or len(_json(value)) > _result_limit(navigation_policy) or type(value['proof']) is not dict
                or type(value['proof'].get('version')) is not int
                or value['proof']['version'] != {None: 10, 'formula_direct_v2': 11,
                                                'formula_search_v3': 13, 'formula_search_index_v4': 14}[navigation_policy]
                or type(value['proof'].get('pages')) is not list or len(value['proof']['pages']) > MAX_PAGES):
            _fail()
        retained = []
        for i, page in enumerate(value['proof']['pages']):
            item = _decoded(page, {'new': i}, value['proof']['searched_at'],
                            allow_resource=value['proof']['version'] in (13, 14), large_index=value['proof']['version'] == 14)
            retained.append((page['url'], item))
        cursor = 0
        def replay(target):
            nonlocal cursor
            if cursor >= len(retained) or target != retained[cursor][0]:
                _fail()
            item = retained[cursor][1]
            cursor += 1
            if item is None:
                raise OSError('retained failed GET')
            return item['mime'], item['content']
        expected = _discover(request, prior_discoveries, replay, value['proof']['searched_at'],
                             source_observations=source_observations)
        if cursor != len(retained) or _json(value) != _json(expected):
            _fail()
        return expected
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _fail()


def _input(path, expected_hash):
    path = safe_root(path)
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        _fail()
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES or sha256(raw).hexdigest() != expected_hash:
        _fail()
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'request', 'prior_discoveries', 'source_observations'}:
        _fail()
    r = value['request']
    if r != _request(r['source_url'], r['query'], r['excluded_urls'], value['prior_discoveries'],
                     source_observations=value['source_observations'], routing_version=r['routing_version'],
                     topic=r['topic'], navigation_policy=r.get('navigation_policy')):
        _fail()
    return value


class EvidenceDiscoveryProcess:
    def discover(self, url, query, excluded_urls=(), *, prior_discoveries, storage_dir,
                 source_observations=(), should_stop=lambda: False, timeout=TIMEOUT,
                 routing_version=5, topic=None, navigation_policy='formula_direct_v2'):
        from .process import run_child
        request = _request(url, query, excluded_urls, prior_discoveries,
                           source_observations=source_observations, routing_version=routing_version,
                           topic=topic, navigation_policy=navigation_policy)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= TIMEOUT:
            raise ComputerError('supplement_dimension_timeout')
        if should_stop():
            raise ComputerError('supplement_dimension_cancelled')
        root = safe_root(storage_dir)
        if not root.is_dir():
            _fail()
        payload = {'request': request, 'prior_discoveries': prior_discoveries,
                   'source_observations': list(source_observations)}
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(raw) > MAX_INPUT_BYTES:
            _fail()
        path = root / ('evidence-input-' + request['request_sha256'] + '.json')
        if safe_root(path) != path:
            _fail()
        if path.exists():
            if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES or path.read_bytes() != raw:
                _fail()
        else:
            write_new(path, payload)
        input_hash = sha256(raw).hexdigest()
        command = [sys.executable, '-B', '-m', 'agent_service.comparison_evidence_discovery', str(path), input_hash]
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
        if type(result) is not str or len(result.encode('utf-8')) > _result_limit(navigation_policy):
            _fail()
        try:
            value = json.loads(result)
        except (ValueError, TypeError, RecursionError):
            _fail()
        if value == {'error': 'supplement_dimension_unavailable'}:
            raise ComputerError('supplement_dimension_unavailable')
        return validate(value, url, query, excluded_urls, prior_discoveries=prior_discoveries,
                        source_observations=source_observations, routing_version=routing_version, topic=topic)


def _fetch_search_resource(url, *, large_index=False):
    """One pinned, bounded GET; accepts a static JS index without retrying HTTP."""
    import ipaddress
    import socket
    from .browser import PinnedConnection, SourceError
    from .contracts import PolicyError
    parsed = urlsplit(source_url(url))
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise PolicyError('Source DNS resolved to a private/reserved address')
    connection = PinnedConnection(parsed.hostname, port, addresses[0][4][0], parsed.scheme == 'https')
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request('GET', path, headers={'User-Agent': 'Jarvis-SourceReader/1', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise SourceError('Unexpected HTTP status')
        mime = response.getheader('Content-Type', '').lower()
        if (response.getheader('Content-Encoding', 'identity').lower() != 'identity'
                or not mime.startswith(('text/html', 'text/plain', 'application/javascript',
                                         'text/javascript', 'application/x-javascript'))):
            raise SourceError('Unsupported resource response')
        resource = (re.fullmatch(r'searchindex(?:[.-][a-z0-9_-]+)?\.js',
                                 parsed.path.rsplit('/', 1)[-1], re.I) is not None)
        limit = _content_limit({'resource_kind': 'sphinx_search_index_v1'} if resource else {}, mime, large_index)
        content = response.read(limit + 1)
        if not content or len(content) > limit:
            raise SourceError('Source exceeds byte limit')
        return mime, content
    finally:
        connection.close()


def main():
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    try:
        if len(sys.argv) != 3:
            _fail()
        payload = _input(sys.argv[1], sys.argv[2])
        from .browser import fetch_source
        policy = payload['request'].get('navigation_policy')
        if policy == 'formula_search_index_v4':
            fetch = lambda url: _fetch_search_resource(url, large_index=True)
        else:
            fetch = _fetch_search_resource if policy == 'formula_search_v3' else fetch_source
        value = _discover(payload['request'], payload['prior_discoveries'], fetch, utc_now(),
                          source_observations=payload['source_observations'])
    except Exception:
        value = {'error': 'supplement_dimension_unavailable'}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
