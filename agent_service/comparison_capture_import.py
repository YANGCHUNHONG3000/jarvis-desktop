"""Import retained navigation bytes without fetching pages or calling models."""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re

from .computer import ComputerError, digest, safe_root, write_new

MAX_BYTES = 2 * 1024 * 1024
_CACHE = OrderedDict()


def _fail(code='comparison_capture_import_invalid'):
    raise ComputerError(code)


def validate_manifest(value, seed):
    """Pure shape check. Eligibility is established only by load_bundle."""
    from .comparison_seed import _hash_value, _plain
    if (type(value) is not dict or set(value) != {
            'version', 'path', 'sha256', 'seed_sha256', 'source_ids'}
            or type(value['version']) is not int or value['version'] != 1
            or type(seed) is not dict or seed.get('version') != 5
            or not _plain(value['path'], 2000) or not Path(value['path']).is_absolute()
            or not _hash_value(value['sha256']) or value['seed_sha256'] != digest(seed)
            or type(value['source_ids']) is not list or not 1 <= len(value['source_ids']) <= 2
            or any(type(sid) is not str or re.fullmatch(r'[0-9a-f]{24}', sid) is None
                   for sid in value['source_ids'])
            or len(set(value['source_ids'])) != len(value['source_ids'])):
        _fail()
    return deepcopy(value)


def _raw(value, seed):
    value = validate_manifest(value, seed)
    try:
        path = safe_root(value['path'])
        if str(path) != value['path'] or path.stat().st_size > MAX_BYTES:
            _fail()
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or sha256(raw).hexdigest() != value['sha256']:
            _fail('comparison_capture_import_changed')
        return raw
    except OSError:
        _fail('comparison_capture_import_changed')


def fingerprint(value, seed):
    raw = _raw(value, seed)
    return {'path': value['path'], 'sha256': sha256(raw).hexdigest(), 'size': len(raw)}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail()
        value[key] = item
    return value


def _derive(value, seed, raw, base):
    from .comparison_compact_seed import load_navigation_observations
    from .comparison_evidence_discovery import validate
    from .comparison_cached_reader import CachedNavigationReader, validate_record
    from .computer_agent_tools import AgentEnvironment, _selection

    payload = json.loads(raw, object_pairs_hook=_unique,
                         parse_constant=lambda _: _fail())
    if (type(payload) is not dict or set(payload) != {'version', 'seed_sha256', 'stages', 'selected_urls'}
            or type(payload['version']) is not int or payload['version'] != 1
            or payload['seed_sha256'] != digest(seed)
            or type(payload['stages']) is not list or not 1 <= len(payload['stages']) <= 2
            or type(payload['selected_urls']) is not list
            or not 1 <= len(payload['selected_urls']) <= 2):
        _fail()
    captures, history = deepcopy(base)
    prior = [v for v in history.get('discoveries', []) if v.get('proof', {}).get('version') != 12]
    added, topics, last_request = [], {}, None
    original_request = None
    for stage in payload['stages']:
        if type(stage) is not dict or set(stage) != {'input', 'result'}:
            _fail()
        inp = stage['input']
        if type(inp) is not dict or set(inp) != {'request', 'prior_discoveries', 'source_observations'}:
            _fail()
        r = inp['request']
        if (type(r) is not dict or r.get('topic') not in seed['queries']
                or seed['queries'].count(r['topic']) != 1 or inp['prior_discoveries'] != prior
                or r.get('routing_version') != 5
                or r.get('navigation_policy') not in {'formula_search_v3', 'formula_search_index_v4'}):
            _fail()
        query_index = seed['queries'].index(r['topic'])
        if not any(item['url'] == r.get('source_url') and item['query_index'] == query_index
                   for item, _ in captures):
            _fail()
        identity = {k: r.get(k) for k in ('source_url', 'query', 'topic', 'excluded_urls')}
        if original_request is not None and identity != original_request:
            _fail()
        original_request = identity
        if r['topic'] not in topics:
            topics[r['topic']] = load_navigation_observations(seed, r['topic'])
        if inp['source_observations'] != topics[r['topic']]:
            _fail()
        # Validate the exact serialized request too, including hashes and keys.
        from .comparison_evidence_discovery import _request
        expected = _request(r['source_url'], r['query'], r['excluded_urls'], prior,
                            source_observations=topics[r['topic']], routing_version=5,
                            topic=r['topic'], navigation_policy=r['navigation_policy'])
        if expected != r:
            _fail()
        checked = validate(stage['result'], r['source_url'], r['query'], r['excluded_urls'],
                           prior_discoveries=prior, source_observations=topics[r['topic']],
                           routing_version=5, topic=r['topic'], navigation_policy=r['navigation_policy'])
        if any(digest(checked) == digest(v) for v in prior):
            _fail()
        prior.append(checked)
        added.append(checked)
        last_request = r
    selected = payload['selected_urls']
    available = (added[-1].get('discovery') or {}).get('urls', [])
    if (any(type(url) is not str or url not in available for url in selected)
            or len(set(selected)) != len(selected)
            or [digest(url)[:24] for url in selected] != value['source_ids']):
        _fail()
    known_ids = {item['id'] for item, _ in captures}
    known_urls = {item['url'] for item, _ in captures} | set(history['read_urls'])
    for url, sid in zip(selected, value['source_ids']):
        if sid in known_ids or url in known_urls:
            _fail('comparison_capture_import_duplicate')
        for pending in history['candidates']:
            if (pending['id'] == sid or pending['url'] == url) and (
                    pending['id'] != sid or pending['url'] != url
                    or pending['query_index'] != query_index):
                _fail('comparison_capture_import_duplicate')
        # No fallback exists: this reader can only use the verified stored bytes.
        reader = CachedNavigationReader(None, last_request['query'], prior)
        observation = reader.read(url).to_dict()
        record = {'observation': observation, 'document_proof': reader.last_document_proof,
                  'read_purpose': {'purpose': 'initial'},
                  'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'},
                  'capture_import': {'sha256': value['sha256'], 'source_id': sid,
                                     'query': last_request['query']}}
        AgentEnvironment._validate_page(observation, url)
        validate_record(record, url, last_request['query'], prior)
        excerpt = _selection(observation['text'], seed['queries'][query_index], [])
        if excerpt is None:
            _fail()
        record['excerpt'] = excerpt
        captures.append(({'id': sid, 'url': url, 'query_index': query_index}, record))
        history['read_urls'].append(url)
    if len(captures) > 6:
        _fail('comparison_active_limit')
    history['read_urls'] = sorted(set(history['read_urls']))
    history['candidates'] = [v for v in history['candidates'] if v['url'] not in selected]
    history['discoveries'] = [*history.get('discoveries', []), *added]
    if len(set(history['read_urls']) | {v['url'] for v in history['candidates']}) > 16:
        _fail('comparison_seed_candidate_limit')
    return captures, history


def load_bundle(value, seed, base=None):
    """Return only fully checked captures; never a semantic review verdict."""
    from .comparison_failed_seed import load_bundle as load_base
    before = _raw(value, seed)
    base = load_base(seed) if base is None else base
    key = digest({'descriptor': value, 'base': base})
    try:
        if key in _CACHE:
            result = deepcopy(_CACHE[key])
            _CACHE.move_to_end(key)
        else:
            result = _derive(value, seed, before, base)
        if _raw(value, seed) != before:
            _fail('comparison_capture_import_changed')
        _CACHE[key] = deepcopy(result)
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
        return result
    except ComputerError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        _fail()


def create_manifest(seed, stages, selected_urls, destination):
    """Save a new bounded bundle then verify it before an approved plan can use it."""
    destination = safe_root(destination)
    payload = {'version': 1, 'seed_sha256': digest(seed),
               'stages': deepcopy(stages), 'selected_urls': list(selected_urls)}
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_BYTES:
        _fail()
    value = {'version': 1, 'path': str(destination), 'sha256': sha256(raw).hexdigest(),
             'seed_sha256': digest(seed), 'source_ids': [digest(url)[:24] for url in selected_urls]}
    validate_manifest(value, seed)
    write_new(destination, payload)
    load_bundle(value, seed)
    return value


def prepare_imported(session, instruction, goal, manifest):
    """Prepare a fresh review of retained captures; no model or browser call."""
    from .computer_agent import prepare, validate_goal
    goal = deepcopy(goal)
    goal['comparison']['capture_import'] = deepcopy(manifest)
    validate_goal(goal)
    load_bundle(manifest, goal['comparison']['seed'])
    return prepare(session, instruction, goal)
