"""Cache-only author repair preserves old formula reads and replay prefixes."""
from copy import deepcopy

import pytest

from agent_service.comparison_completion import CompletionEnvironment
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.computer import ComputerError, digest
from agent_service.computer_agent_tools import _selection
from test_comparison_evidence_discovery import history, page, observation, AT

FORMULA = 'https://xlsxwriter.readthedocs.io/working_with_formulas.html'
AUTHOR = 'https://xlsxwriter.readthedocs.io/index.html'
BETA = 'https://openpyxl.readthedocs.io/usage.html'


def setup(monkeypatch, *, original_pending=False, author=AUTHOR, route=6):
    prior = history([page(author, '<title>XlsxWriter</title><p>© Copyright 2026, John McNamara.</p>', 1)])
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['XlsxWriter formula calculation', 'openpyxl formula calculation'],
        'outputs': ['spreadsheet', 'document'], 'required_terms': [],
        'comparison': {'completion_policy': 'dimension_first_v4', 'dimension_routing': route}}
    env.record_origins = {}
    monkeypatch.setattr(env, '_prior_discoveries', lambda: deepcopy(prior))
    monkeypatch.setattr(env, '_repair_input', lambda: ([], {'read_urls': [], 'discoveries': prior}))
    facts = CompletionEnvironment._empty(None)
    facts['candidates'] = [{'id': 'formula', 'url': FORMULA, 'query_index': 0},
        {'id': 'beta', 'url': BETA, 'query_index': 1, 'need_id': 'original-beta'}]
    facts['active_ids'] = ['formula', 'beta']
    facts['queries'] = [0, 1]
    facts['reads'] = {
        'formula': {'action': {'kind': 'read', 'candidate_id': 'formula'},
            'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'},
            'observation': observation(FORMULA, 'XlsxWriter does not calculate formulas.')},
        'beta': {'result': {'ok': True}, 'observation': observation(BETA, 'openpyxl sources')}}
    facts['comparisons'] = [{'active_ids': ['formula', 'beta'], 'comparison': {
        'assessment': {'group_details': [{'ids': ['formula'], 'complete': False},
                                       {'ids': ['beta'], 'complete': True}]}}}]
    facts['needs'] = [{'purpose': 'dimension_evidence_v6', 'need_id': 'original-beta',
                      'host': 'openpyxl.readthedocs.io'}]
    facts['repair_history'] = {'queries': [0, 1], 'read_urls': [],
        'candidates': [], 'supplement_keys': []}
    facts['records'] = [({'action': {'kind': 'compare'}}, 'old-comparison')]
    if original_pending:
        facts['candidates'][1].pop('need_id')
    monkeypatch.setattr(env, '_navigation_records', lambda: [])
    return env, facts, prior


def action_for(env, facts):
    env._extend_cached_attribution(facts)
    return {'kind': 'supplement', **facts['needs'][-1]}


def test_existing_original_need_and_formula_capture_stay_exact(monkeypatch):
    env, facts, _ = setup(monkeypatch, original_pending=True)
    before = deepcopy(facts)
    assert env._frontier(facts) == [{'kind': 'supplement', **before['needs'][0]}]
    assert facts['needs'][:1] == before['needs'] and facts['reads'] == before['reads']
    assert facts['records'] == before['records']
    assert facts['needs'][-1]['purpose'] == 'cached_attribution_v1'
    env._frontier(facts)
    assert len(facts['needs']) == 2


def test_no_future_navigation_can_create_need_for_old_prefix(monkeypatch):
    env, facts, prior = setup(monkeypatch)
    monkeypatch.setattr(env, '_prior_discoveries', lambda: [])
    monkeypatch.setattr(env, '_navigation_records', lambda: [({'action': {'kind': 'supplement'},
        'link_result': prior[0]}, 'future')])
    env._extend_cached_attribution(facts)
    assert len(facts['needs']) == 1
    facts['records'].append(({'action': {'kind': 'supplement'}, 'link_result': prior[0]}, 'now-prefix'))
    env._extend_cached_attribution(facts)
    assert len(facts['needs']) == 2


@pytest.mark.parametrize('case', ['old_route', 'attributed', 'not_direct', 'inactive', 'candidate', 'consumed', 'cross_host'])
def test_optional_need_requires_current_direct_missing_identity_and_cached_scope(monkeypatch, case):
    env, facts, _ = setup(monkeypatch, author='https://unrelated.example/authors' if case == 'cross_host' else AUTHOR,
        route=5 if case == 'old_route' else 6)
    if case == 'attributed':
        facts['reads']['formula']['observation']['text'] += '\nCopyright 2026, John McNamara.'
    elif case == 'not_direct':
        facts['reads']['formula']['observation']['text'] = 'Formula parsing API reference.'
    elif case == 'inactive':
        facts['active_ids'].remove('formula')
    elif case == 'candidate':
        facts['candidates'].append({'id': 'author', 'url': AUTHOR, 'query_index': 0})
    elif case == 'consumed':
        facts['repair_history']['read_urls'].append(AUTHOR)
    env._extend_cached_attribution(facts)
    assert len(facts['needs']) == 1


def test_cache_proof_zero_network_exact_binding_and_no_search_fallback(monkeypatch):
    env, facts, prior = setup(monkeypatch)
    action = action_for(env, facts)
    from agent_service import comparison_evidence_discovery as evidence
    monkeypatch.setattr(evidence, 'EvidenceDiscoveryProcess', lambda: pytest.fail('network discovery'))
    linked = env._discover_links(FORMULA, action, [FORMULA])
    assert linked['proof'] == {'version': 12, 'kind': 'cached_attribution_v1',
        'action_sha256': digest(action), 'prior_sha256': digest(prior),
        'ref': action['target_ref'], 'source_sha256': action['source_sha256']}
    assert linked['discovery'] == {'urls': [AUTHOR], 'searched_at': AT}
    assert env._validate_links(linked, FORMULA, action, [FORMULA]) == linked
    assert env._supplement_allowed({'link_result': linked}, action, [AUTHOR])
    assert not env._supplement_allowed({'link_result': linked}, action, [BETA])
    with pytest.raises(ComputerError, match='search_unavailable'):
        env._search_candidates(action, action['query'])


@pytest.mark.parametrize('field', ['prior_sha256', 'ref', 'source_sha256', 'action_sha256'])
def test_modified_cache_proof_rejected(monkeypatch, field):
    env, facts, _ = setup(monkeypatch)
    action = action_for(env, facts)
    linked = env._discover_links(FORMULA, action, [FORMULA])
    linked['proof'][field] = 'changed'
    with pytest.raises(ComputerError):
        env._validate_links(linked, FORMULA, action, [FORMULA])


@pytest.mark.parametrize('records,candidates,expected', [(7, 7, True), (8, 7, False), (7, 8, False)])
def test_author_need_reserves_only_one_candidate_and_two_actions(monkeypatch, records, candidates, expected):
    env, facts, _ = setup(monkeypatch)
    facts['records'] *= records
    for i in range(candidates - 2):
        sid = f'extra-{i}'
        facts['candidates'].append({'id': sid, 'url': f'https://extra.example/{i}', 'query_index': 1})
        facts['reads'][sid] = {'result': {'ok': False}}
    actions = env._frontier(facts)
    assert bool(actions and actions[0].get('purpose') == 'cached_attribution_v1') is expected


def test_real_replay_supplement_pause_read_keeps_original_capture_and_final_review(monkeypatch):
    from agent_service.comparison_cached_reader import _capture
    env, initial, prior = setup(monkeypatch)
    original = deepcopy(initial['reads']['formula'])
    action = action_for(env, deepcopy(initial))
    linked = env._discover_links(FORMULA, action, [FORMULA, BETA])
    supplement = {'action': action, 'at': AT, 'intent_sha256': 'supplement-intent',
        'link_result': linked, 'discovery': linked['discovery'],
        'result': {'ok': True, 'state': 'verified', 'code': 'search_discovered'}}
    monkeypatch.setattr(env, '_empty', lambda: deepcopy(initial))
    monkeypatch.delattr(env, '_navigation_records')
    paused = env._replay([(supplement, 'supplement-record')])
    sid = digest(AUTHOR)[:24]
    assert env._frontier(paused) == [{'kind': 'read', 'candidate_id': sid}]
    observation, proof = _capture(AUTHOR, action['query'], prior)
    read = {'action': {'kind': 'read', 'candidate_id': sid}, 'at': AT,
        'intent_sha256': 'read-intent', 'observation': observation.to_dict(), 'document_proof': proof,
        'excerpt': _selection(observation.text, action['query'], []),
        'read_purpose': {'purpose': 'supplement', 'need_id': action['need_id']},
        'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'}}
    final = env._replay([(supplement, 'supplement-record'), (read, 'read-record')])
    assert final['reads']['formula'] == original
    assert env._frontier(final) == [{'kind': 'compare'}]
    assert final['comparison'] is None and not final['office_started']
    assert len([need for need in final['needs'] if need.get('purpose') == 'cached_attribution_v1']) == 1
