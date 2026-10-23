"""Read-only import replays real navigation/extraction over offline HTTP bytes.

Only the already-verified compact graph loader boundary is replaced. Navigation
proof creation, byte replay, cache extraction and observation validation are real.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from agent_service import comparison_capture_import as imported
from agent_service import comparison_evidence_discovery as navigation
from agent_service.comparison_cached_reader import CachedNavigationReader
from agent_service.computer import ComputerError, digest
from agent_service.project_documents import extract
from test_comparison_evidence_discovery import (ROOT, INDEX, TOPIC, QUERY, AT, SEARCH,
    SEARCH_INDEX, FORMULA_DOC, search_history, large_index_text)


@pytest.fixture(autouse=True)
def isolate_cache_and_forbid_effects(monkeypatch):
    from agent_service import browser, comparison_evidence_discovery, computer_agent
    imported._CACHE.clear()
    def forbidden(*args, **kwargs):
        pytest.fail('capture import must never dispatch GET, search or a paid model call')
    monkeypatch.setattr(browser, 'fetch_source', forbidden)
    monkeypatch.setattr(comparison_evidence_discovery, '_fetch_search_resource', forbidden)
    monkeypatch.setattr(computer_agent, 'model_json', forbidden)
    yield
    imported._CACHE.clear()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    from agent_service import comparison_failed_seed, comparison_compact_seed
    root_observation = extract(ROOT, 'text/html',
        b'<title>openpyxl</title><p>openpyxl Python Excel documentation.</p>', QUERY, AT).to_dict()
    root_item = {'id': digest(ROOT)[:24], 'url': ROOT, 'query_index': 0}
    root_record = {'observation': root_observation, 'read_purpose': {'purpose': 'initial'},
                   'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'}}
    base = ([(root_item, root_record)], {'read_urls': [ROOT], 'discoveries': search_history(),
        'candidates': [], 'queries': [0], 'supplement_keys': []})
    seed = {'version': 5, 'queries': [TOPIC], 'test_verified_seed': 'offline boundary'}
    observations = [root_observation]
    state = {'base': base, 'base_calls': 0, 'observations': observations, 'observation_calls': []}
    def load_base(value):
        assert value == seed
        state['base_calls'] += 1
        return deepcopy(state['base'])
    def load_observations(value, topic):
        assert value == seed and topic == TOPIC
        state['observation_calls'].append(topic)
        return deepcopy(state['observations'])
    monkeypatch.setattr(comparison_failed_seed, 'load_bundle', load_base)
    monkeypatch.setattr(comparison_compact_seed, 'load_navigation_observations', load_observations)
    prior = deepcopy(base[1]['discoveries'])
    request13 = navigation._request(ROOT, QUERY, [ROOT], prior, source_observations=observations,
                                    topic=TOPIC, navigation_policy='formula_search_v3')
    calls = []
    responses = {SEARCH: ('text/html', b'<script src="searchindex.js"></script>'),
                 SEARCH_INDEX: ('text/javascript', large_index_text(937369).encode()),
                 FORMULA_DOC: ('text/html', b'<p>openpyxl never evaluates formulae.</p>')}
    def capture(url):
        calls.append(url)
        return responses[url]
    result13 = navigation._discover(request13, prior, capture, AT, source_observations=observations)
    assert calls == [SEARCH, SEARCH_INDEX] and result13['discovery'] is None
    input13 = {'request': request13, 'prior_discoveries': deepcopy(prior), 'source_observations': deepcopy(observations)}
    prior.append(result13)
    request14 = navigation._request(ROOT, QUERY, [ROOT], prior, source_observations=observations,
                                    topic=TOPIC, navigation_policy='formula_search_index_v4')
    calls.clear()
    result14 = navigation._discover(request14, prior, capture, AT, source_observations=observations)
    assert calls == [SEARCH_INDEX, FORMULA_DOC]
    input14 = {'request': request14, 'prior_discoveries': deepcopy(prior), 'source_observations': deepcopy(observations)}
    stages = [{'input': input13, 'result': result13}, {'input': input14, 'result': result14}]
    assert result14['discovery']['urls'] == [FORMULA_DOC, INDEX]
    path = tmp_path/'approved-retained-captures.json'
    payload = {'version': 1, 'seed_sha256': digest(seed), 'stages': stages,
               'selected_urls': [FORMULA_DOC, INDEX]}
    state.update(seed=seed, stages=stages, path=path, payload=payload)
    state['manifest'] = save_payload(path, payload, seed)
    return state


def save_payload(path, payload, seed):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return {'version': 1, 'path': str(path.absolute()), 'sha256': sha256(raw).hexdigest(),
            'seed_sha256': digest(seed), 'source_ids': [digest(url)[:24] for url in payload['selected_urls']]}


def test_retained13_to14_capture_bytes_are_derived_before_import_without_effects(bundle):
    b = bundle
    captures, history = imported.load_bundle(b['manifest'], b['seed'])
    assert b['base_calls'] == 1 and b['observation_calls'] == [TOPIC]
    assert len(captures) == 3 and len(history['discoveries']) == 3
    assert captures[0] == b['base'][0][0]
    for item, record in captures[1:]:
        reader = CachedNavigationReader(None, QUERY, history['discoveries'])
        expected = reader.read(item['url']).to_dict()
        assert record['observation'] == expected
        assert record['document_proof'] == reader.last_document_proof
        assert record['capture_import'] == {'sha256': b['manifest']['sha256'], 'source_id': item['id'], 'query': QUERY}
        assert record['read_purpose'] == {'purpose': 'initial'}
        assert record['observation']['captured_at'] == AT
    assert 'openpyxl never evaluates formulae.' in captures[1][1]['observation']['text']
    assert 'Eric Gazoni' in captures[2][1]['observation']['text']
    assert set(history['read_urls']) == {ROOT, FORMULA_DOC, INDEX}
    assert 'completed' not in history and 'comparison' not in history


def test_cache_returns_copies_and_rechecks_file_sha_even_with_same_mtime(bundle):
    b = bundle
    first = imported.load_bundle(b['manifest'], b['seed'])
    first[0][1][1]['observation']['text'] = 'caller mutation'
    first[1]['read_urls'].clear()
    again = imported.load_bundle(b['manifest'], b['seed'])
    assert 'caller mutation' not in again[0][1][1]['observation']['text']
    assert FORMULA_DOC in again[1]['read_urls'] and b['base_calls'] == 2
    before = b['path'].stat()
    raw = b['path'].read_bytes().replace(b'openpyxl formula calculation', b'openpyxl formula calculatioN', 1)
    assert len(raw) == before.st_size
    b['path'].write_bytes(raw)
    os.utime(b['path'], ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ComputerError, match='comparison_capture_import_changed'):
        imported.load_bundle(b['manifest'], b['seed'])
    assert b['base_calls'] == 2  # File SHA checked before even reloading the base.


@pytest.mark.parametrize('case', ['seed_hash', 'source_url', 'topic', 'query', 'first_prior',
    'source_observation', 'request_hash', 'extra_request_field', 'second_prefix', 'stage_identity',
    'wrong_retry_hash', 'wrong_html_hash', 'supplied_observation', 'stage_order', 'selected_not_returned',
    'duplicate_selected_url'])
def test_seed_source_topic_query_prefix_and_capture_binding_fail_closed(bundle, case):
    b = bundle
    payload = deepcopy(b['payload'])
    first, second = payload['stages']
    if case == 'seed_hash':
        payload['seed_sha256'] = '0'*64
    elif case == 'source_url':
        first['input']['request']['source_url'] = 'https://github.com/other/openpyxl'
    elif case == 'topic':
        first['input']['request']['topic'] = 'XlsxWriter Python Excel documentation'
    elif case == 'query':
        first['input']['request']['query'] += ' changed'
    elif case == 'first_prior':
        first['input']['prior_discoveries'] = []
    elif case == 'source_observation':
        first['input']['source_observations'][0]['text'] += ' forged observation'
    elif case == 'request_hash':
        first['input']['request']['request_sha256'] = '0'*64
    elif case == 'extra_request_field':
        first['input']['request']['approved'] = True
    elif case == 'second_prefix':
        second['input']['prior_discoveries'].pop()
    elif case == 'stage_identity':
        second['input']['request']['excluded_urls'] = []
    elif case == 'wrong_retry_hash':
        second['result']['proof']['pages'][0]['retry_from']['sha256'] = '0'*64
    elif case == 'wrong_html_hash':
        second['result']['proof']['pages'][1]['source_sha256'] = '0'*64
    elif case == 'supplied_observation':
        second['observation'] = {'text': 'hand supplied answer'}
    elif case == 'stage_order':
        payload['stages'].reverse()
    elif case == 'selected_not_returned':
        payload['selected_urls'] = [SEARCH]
    else:
        payload['selected_urls'] = [FORMULA_DOC, FORMULA_DOC]
    manifest = save_payload(b['path'], payload, b['seed'])
    with pytest.raises(ComputerError):
        imported.load_bundle(manifest, b['seed'])


@pytest.mark.parametrize('case', ['known_url', 'known_id', 'historical_read', 'active_limit', 'history_limit'])
def test_duplicates_and_unchanged_capture_limits_are_enforced(bundle, case):
    b = bundle
    if case == 'known_url':
        b['base'][0].append(({'id': 'a'*24, 'url': FORMULA_DOC, 'query_index': 0}, {}))
    elif case == 'known_id':
        b['base'][0].append(({'id': digest(FORMULA_DOC)[:24], 'url': 'https://old.example/doc', 'query_index': 0}, {}))
    elif case == 'historical_read':
        b['base'][1]['read_urls'].append(FORMULA_DOC)
    elif case == 'active_limit':
        b['base'][0].extend(({'id': str(i)*24, 'url': 'https://old.example/'+str(i), 'query_index': 0}, {}) for i in range(4))
    else:
        b['base'][1]['read_urls'].extend('https://old.example/'+str(i) for i in range(14))
    with pytest.raises(ComputerError):
        imported.load_bundle(b['manifest'], b['seed'])


def test_pending_candidates_read_url_replaced_without_touching_original_base(bundle):
    b = bundle
    candidate = {'id': digest(FORMULA_DOC)[:24], 'url': FORMULA_DOC, 'query_index': 0}
    b['base'][1]['candidates'].append(candidate)
    result = imported.load_bundle(b['manifest'], b['seed'])
    assert result[1]['candidates'] == []
    assert b['base'][1]['candidates'] == [candidate]


@pytest.mark.parametrize('field', ['id', 'url', 'query_index'])
def test_pending_candidate_collision_cannot_remap_identity_or_query(bundle, field):
    b = bundle
    candidate = {'id': digest(FORMULA_DOC)[:24], 'url': FORMULA_DOC, 'query_index': 0}
    candidate[field] = {'id': '0'*24, 'url': 'https://old.example/another-document', 'query_index': 1}[field]
    b['base'][1]['candidates'].append(candidate)
    before = deepcopy(b['base'])
    with pytest.raises(ComputerError, match='comparison_capture_import_duplicate'):
        imported.load_bundle(b['manifest'], b['seed'])
    assert b['base'] == before


@pytest.mark.parametrize('case', ['wrong_hash', 'missing_file', 'file_over_limit', 'manifest_ids', 'manifest_seed'])
def test_descriptor_and_raw_bytes_fail_before_graph_or_effect_calls(bundle, case):
    b = bundle
    manifest = deepcopy(b['manifest'])
    if case == 'wrong_hash':
        manifest['sha256'] = '0'*64
    elif case == 'missing_file':
        manifest['path'] = str(b['path'].with_name('absent.json'))
    elif case == 'file_over_limit':
        b['path'].write_bytes(b'x'*(imported.MAX_BYTES+1))
    elif case == 'manifest_ids':
        manifest['source_ids'][0] = '0'*24
    else:
        manifest['seed_sha256'] = '0'*64
    with pytest.raises(ComputerError):
        imported.load_bundle(manifest, b['seed'])
    if case != 'manifest_ids':
        assert b['base_calls'] == 0


def test_duplicate_json_keys_and_nan_rejected_even_with_recomputed_bundle_hash(bundle):
    b = bundle
    for raw in (b'{"version":1,"version":1}', b'{"version":NaN}'):
        b['path'].write_bytes(raw)
        manifest = {**b['manifest'], 'sha256': sha256(raw).hexdigest()}
        with pytest.raises(ComputerError):
            imported.load_bundle(manifest, b['seed'])


def test_post_derivation_file_change_is_detected_and_never_cached(bundle, monkeypatch):
    b = bundle
    derive = imported._derive
    def mutate_after(*args):
        result = derive(*args)
        b['path'].write_bytes(b['path'].read_bytes()+b' ')
        return result
    monkeypatch.setattr(imported, '_derive', mutate_after)
    with pytest.raises(ComputerError, match='comparison_capture_import_changed'):
        imported.load_bundle(b['manifest'], b['seed'])
    assert not imported._CACHE


def test_create_manifest_uses_original_bound_stages_then_full_loader(bundle, tmp_path):
    b = bundle
    destination = tmp_path/'new-approved-import.json'
    descriptor = imported.create_manifest(b['seed'], b['stages'], [FORMULA_DOC], destination)
    assert descriptor['source_ids'] == [digest(FORMULA_DOC)[:24]]
    assert imported.fingerprint(descriptor, b['seed'])['sha256'] == descriptor['sha256']
    captures, _ = imported.load_bundle(descriptor, b['seed'])
    assert len(captures) == 2 and captures[-1][0]['url'] == FORMULA_DOC


def imported_goal(bundle):
    return {'queries': deepcopy(bundle['seed']['queries']), 'min_sources': 2,
        'outputs': ['spreadsheet', 'document'], 'comparison': {
            'dimensions': ['Whether the library calculates formula results'], 'min_domains': 2,
            'evidence_version': 4, 'completion_policy': 'dimension_first_v4', 'dimension_routing': 6,
            'seed': deepcopy(bundle['seed']), 'capture_import': deepcopy(bundle['manifest'])}}


def test_crosssite_empty_and_frontier_review_imported_bytes_without_research_actions(bundle, monkeypatch):
    from agent_service import comparison_seed
    from agent_service.comparison_crosssite import CrossSiteEnvironment
    # The compact loader boundary above establishes graph verification; goal
    # shape validation must not open a separate real dependency graph in tests.
    monkeypatch.setattr(comparison_seed, 'validate_manifest', lambda value: deepcopy(value))
    env = object.__new__(CrossSiteEnvironment)
    env.goal = imported_goal(bundle)
    facts = env._empty()
    assert {item['url'] for item in facts['candidates']} == {ROOT, FORMULA_DOC, INDEX}
    assert set(facts['reads']) == set(facts['active_ids'])
    assert all(facts['reads'][sid]['result']['ok'] for sid in facts['active_ids'])
    assert facts['records'] == [] and facts['comparisons'] == [] and not facts['done']
    assert env._frontier(facts) == [{'kind': 'compare'}]
    assert facts['records'] == []  # Importing captures invents no completed steps.


@pytest.mark.parametrize('review', [False, True])
def test_transport_strips_capture_import_path_but_keeps_canonical_binding(bundle, monkeypatch, review):
    from itertools import combinations
    from agent_service import comparison_seed
    from agent_service.source_comparison import build_context
    from agent_service.comparison_model_transport import call_model
    from agent_service.comparison_review import build_review_context
    monkeypatch.setattr(comparison_seed, 'validate_manifest', lambda value: deepcopy(value))
    captures, _ = imported.load_bundle(bundle['manifest'], bundle['seed'])
    context = build_context(imported_goal(bundle), captures)
    if review:
        selected = {'sources': [{'id': item['id'], 'spans': [None]} for item, _ in captures]}
        context = build_review_context(context, selected)
    canonical = deepcopy(context)
    seen = []
    def offline_transport(wire):
        seen.append(deepcopy(wire))
        assert 'capture_import' not in wire['goal']['comparison']
        assert 'seed' not in wire['goal']['comparison']
        assert str(bundle['path']) not in json.dumps(wire, ensure_ascii=False)
        assert [row['id'] for row in wire['sources']] == ['s0', 's1', 's2']
        assert any('openpyxl never evaluates formulae.' in source['text'] for source in wire['sources'])
        if not review:
            return {'sources': [{'id': row['id'], 'spans': [None]} for row in wire['sources']]}
        return {'sources': [{'id': row['id'], 'publisher': None,
            'cells': [{'status': 'missing', 'reason': 'Offline transport grammar only'}]}
            for row in wire['sources']], 'pairs': [{'left': left['id'], 'right': right['id'],
                'relation': 'independent', 'reason': 'Offline transport grammar only'}
                for left, right in combinations(wire['sources'], 2)]}
    response = call_model(context, offline_transport)
    assert len(seen) == 1
    assert [row['id'] for row in response['sources']] == [item['id'] for item, _ in captures]
    assert context == canonical
    assert context['goal']['comparison']['capture_import'] == bundle['manifest']


def test_prepare_imported_validates_real_import_before_calling_plan_preparer(bundle, monkeypatch):
    from agent_service import computer_agent
    calls = []
    goal = imported_goal(bundle)
    goal['comparison'].pop('capture_import')
    before = deepcopy(goal)
    monkeypatch.setattr(computer_agent, 'validate_goal', lambda value: deepcopy(value))
    def prepare(session, instruction, approved):
        calls.append((session, instruction, deepcopy(approved)))
        assert approved['comparison']['capture_import'] == bundle['manifest']
        return {'plan': {'goal': approved}}
    monkeypatch.setattr(computer_agent, 'prepare', prepare)
    result = imported.prepare_imported('offline-session', 'Review retained formula evidence', goal, bundle['manifest'])
    assert len(calls) == 1 and result['plan']['goal']['comparison']['capture_import'] == bundle['manifest']
    assert goal == before and bundle['base_calls'] == 1
    bad = {**bundle['manifest'], 'sha256': '0'*64}
    with pytest.raises(ComputerError):
        imported.prepare_imported('offline-session', 'Review retained formula evidence', goal, bad)
    assert len(calls) == 1
