"""Versioned dimension actions preserve topic identity and captured-source binding.

Replay inputs are isolated in-memory receipts; source/project checks are the
production methods. No browser, model, Office executor, or persisted run is used.
"""
from copy import deepcopy
import time

import pytest

from agent_service.comparison_completion import CompletionEnvironment
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.computer import ComputerError, digest


FIRST = 'https://github.com/first/library/blob/main/README.md'
SECOND = 'https://github.com/second/library/blob/main/README.md'
TARGET = 'https://github.com/second/library/blob/main/formulas.md'
STAMP = '2026-10-23T00:00:00+00:00'


def mock_repair_history(monkeypatch, value):
    from agent_service import comparison_failed_seed
    monkeypatch.setattr(comparison_failed_seed, 'load_repair_history', lambda _: deepcopy(value))
    monkeypatch.setattr(comparison_failed_seed, 'load_bundle', lambda _: ([], deepcopy(value)))


def source_facts():
    facts = CompletionEnvironment._empty(None)
    facts['candidates'] = [
        {'id': 'first', 'url': FIRST, 'query_index': 0},
        {'id': 'second', 'url': SECOND, 'query_index': 1},
    ]
    facts['active_ids'] = ['first', 'second']
    facts['reads'] = {sid: {'result': {'ok': True}, 'observation': {'text': sid}}
                      for sid in facts['active_ids']}
    facts['repair_history'] = {'read_urls': [FIRST], 'supplement_keys': []}
    return facts


def bound_action(purpose='dimension_evidence_v3'):
    return {'kind': 'supplement', 'purpose': purpose, 'host': 'github.com',
            'query': 'second library formula calculation', 'link_query': 'formula calculation',
            'topic': 'second library',
            'need_id': 'second-formula', 'source_id': 'second', 'source_url': SECOND,
            'capture_sha256': digest(source_facts()['reads']['second']['observation'])}


@pytest.mark.parametrize('purpose,expected', [
    ('dimension_evidence_v1', 0),
    ('dimension_evidence_v2', 0),
    ('dimension_evidence_v3', 1),
    ('dimension_evidence_v4', 1),
])
def test_same_host_second_topic_uses_versioned_source_index(monkeypatch, purpose, expected):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {}}
    action = bound_action(purpose)
    facts = source_facts()
    monkeypatch.setattr(env, '_empty', lambda: deepcopy(facts))
    # Isolate the replay candidate-assignment branch after action selection.
    monkeypatch.setattr(env, '_frontier', lambda _: [action])
    monkeypatch.setattr(env, '_supplement_allowed', lambda *_: True)
    record = {'action': action, 'at': STAMP,
              'result': {'ok': True, 'state': 'verified', 'code': 'search_discovered'},
              'discovery': {'urls': [TARGET], 'searched_at': STAMP}}
    replayed = env._replay([(record, 'offline-record-hash')])
    discovered = next(item for item in replayed['candidates'] if item['url'] == TARGET)
    assert discovered['query_index'] == expected
    assert discovered['need_id'] == action['need_id']
    assert facts['candidates'] == source_facts()['candidates']


@pytest.mark.parametrize('purpose', ['dimension_evidence_v3', 'dimension_evidence_v4'])
def test_new_successful_source_binds_without_seed_selection(monkeypatch, purpose):
    from agent_service import comparison_seed
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'completion_policy': 'dimension_first_v4', 'seed': {}}}
    mock_repair_history(monkeypatch, {'read_urls': [FIRST]})
    monkeypatch.setattr(comparison_seed, 'load_seed',
                        lambda _: pytest.fail('fresh action must not require seed selection'))
    source, excluded = env._link_inputs(source_facts(), bound_action(purpose))
    assert source == SECOND and source in excluded
    assert env._supplement_allowed({}, bound_action(purpose), [TARGET])


@pytest.mark.parametrize('changes', [
    {'source_id': 'missing'},
    {'source_url': FIRST},
    {'capture_sha256': '0' * 64},
])
@pytest.mark.parametrize('purpose', ['dimension_evidence_v3', 'dimension_evidence_v4'])
def test_dimension_action_rejects_source_identity_or_capture_changes(changes, purpose):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'completion_policy': 'dimension_first_v4'}}
    with pytest.raises(ComputerError, match='comparison_seed_capture_changed'):
        env._link_inputs(source_facts(), {**bound_action(purpose), **changes})


@pytest.mark.parametrize('target', [
    'https://github.com/first/library/blob/main/formulas.md',
    'https://github.com/second/other/blob/main/formulas.md',
])
@pytest.mark.parametrize('purpose', ['dimension_evidence_v3', 'dimension_evidence_v4'])
def test_fresh_source_cannot_borrow_other_project(monkeypatch, target, purpose):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {}}}
    mock_repair_history(monkeypatch, {'read_urls': []})
    assert not env._supplement_allowed({}, bound_action(purpose), [target])


def search_environment(monkeypatch, urls, consumed=()):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {}}}
    env.stop, env.clock, env.deadline = lambda: False, time.monotonic, float('inf')
    class Search:
        def search(self, query, maximum, **kwargs):
            assert maximum == 2
            return {'urls': urls, 'searched_at': STAMP}
    env.search = Search()
    mock_repair_history(monkeypatch, {'read_urls': list(consumed)})
    return env


@pytest.mark.parametrize('purpose,expected', [
    ('dimension_evidence_v2', 'https://unrelated.readthedocs.io/formula'),
    ('dimension_evidence_v3', 'https://openpyxl.readthedocs.io/formula'),
    ('dimension_evidence_v4', 'https://openpyxl.readthedocs.io/formula'),
])
def test_site_operator_cannot_supply_topic_match_under_new_route(monkeypatch, purpose, expected):
    urls = ['https://unrelated.readthedocs.io/formula',
            'https://openpyxl.readthedocs.io/formula']
    env = search_environment(monkeypatch, urls)
    action = {**bound_action(purpose), 'topic': 'site:readthedocs.io openpyxl'}
    assert env._search_candidates(action, 'openpyxl formula')['urls'] == [expected]


def test_new_route_search_rejects_unrelated_topic(monkeypatch):
    env = search_environment(monkeypatch, ['https://example.org/formula'])
    action = {**bound_action(), 'topic': 'openpyxl formula calculation'}
    with pytest.raises(ComputerError, match='search_unavailable'):
        env._search_candidates(action, 'openpyxl formula')


def test_new_route_search_skips_wrong_project_before_selecting(monkeypatch):
    urls = ['https://github.com/unrelated/library/blob/main/formulas.md', TARGET]
    env = search_environment(monkeypatch, urls)
    # Generic topic words leave project scope as the decisive filter.
    action = {**bound_action(), 'topic': 'python library formula calculation'}
    assert env._search_candidates(action, 'library formula')['urls'] == [TARGET]


def test_new_route_search_keeps_consumed_urls_excluded(monkeypatch):
    env = search_environment(monkeypatch, [TARGET], consumed=[TARGET])
    with pytest.raises(ComputerError, match='search_unavailable'):
        env._search_candidates(bound_action(), 'second library formula')


@pytest.mark.parametrize('route,expected', [(2, False), (3, True)])
def test_short_query_length_is_checked_after_new_route_selection(route, expected):
    env = object.__new__(CrossSiteEnvironment)
    topic = 'openpyxl ' + 'a' * 450
    env.goal = {'queries': [topic], 'comparison': {'completion_policy': 'dimension_first_v4',
                'dimension_routing': route, 'dimensions': ['是否计算Excel单元格公式的结果']}}
    facts = source_facts()
    facts['candidates'] = [{**facts['candidates'][1], 'query_index': 0}]
    facts['active_ids'] = ['second']
    evidence = {'assessment': {'group_details': [
        {'ids': ['second'], 'complete': False, 'covered_dimensions': [False]}]}}
    needs = env._needs(facts, evidence)
    assert bool(needs) == expected
    if needs:
        assert needs[0]['query'] == topic + ' formula results calculation'
        assert len(needs[0]['query']) <= 500
        assert needs[0]['topic'] == topic


@pytest.mark.parametrize('historical,current,expected', [
    (9, 6, 'supplement'),
    (10, 6, None),
    (9, 8, None),
])
def test_route3_frontier_reserves_both_historical_and_current_capacity(historical, current, expected):
    from test_comparison_query_fallback import _frontier_facts
    from agent_service.comparison_repair import candidate_count
    facts = _frontier_facts(candidates=current, pending=False)
    facts['reads']['unused'] = {'result': {'ok': False}}
    facts['needs'] = [bound_action()]
    facts['supplements'] = []
    facts['repair_history'] = {'read_urls': [
        f'https://consumed{i}.example/' for i in range(historical - current)],
        'supplement_keys': []}
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['spreadsheet', 'document'],
                'comparison': {'completion_policy': 'dimension_first_v4', 'dimension_routing': 3}}
    assert candidate_count(facts) == historical
    actions = env._frontier(facts)
    assert (actions[0]['kind'] if actions else None) == expected


@pytest.mark.parametrize('limit,consumed,allowed', [
    (None, 6, True),
    (None, 7, False),
    (10, 8, True),
    (10, 9, False),
])
def test_import_history_keeps_old_eight_and_explicit_ten_bounds(limit, consumed, allowed):
    from agent_service.comparison_repair import candidate_count, import_history
    facts = source_facts()
    value = {'queries': [], 'candidates': [], 'supplement_keys': [],
             'read_urls': [f'https://consumed{i}.example/' for i in range(consumed)]}
    options = {} if limit is None else {'limit': limit}
    if allowed:
        import_history(facts, value, **options)
        assert candidate_count(facts) == consumed + 2
        assert len(facts['candidates']) == 2
    else:
        with pytest.raises(ComputerError, match='comparison_seed_candidate_limit'):
            import_history(facts, value, **options)


def batch_environment(monkeypatch, loader=None, route=3):
    from agent_service import comparison_failed_seed
    env = object.__new__(CrossSiteEnvironment)
    manifest = {'offline': True}
    env.goal = {'comparison': {'dimension_routing': route, 'seed': manifest}}
    bundle = ([{'captured': 'original'}], {'read_urls': [FIRST],
                                        'discoveries': [{'proof': 'original'}]})
    calls = []
    def load(value):
        assert value == manifest
        calls.append(value)
        return deepcopy(bundle) if loader is None else loader(len(calls), deepcopy(bundle))
    monkeypatch.setattr(comparison_failed_seed, 'load_bundle', load)
    return env, bundle, calls


@pytest.mark.parametrize('route', [3, 4])
def test_replay_batch_loads_only_at_boundaries_and_isolates_internal_copies(monkeypatch, route):
    env, bundle, calls = batch_environment(monkeypatch, route=route)
    def replay(self, records, *, verify_office=True):
        assert verify_office
        first = self._repair_input()
        first[0][0]['captured'] = 'changed'
        first[1]['read_urls'].append(SECOND)
        discoveries = self._prior_discoveries()
        discoveries[0]['proof'] = 'changed'
        assert self._repair_input() == bundle
        assert len(calls) == 1
        return {'offline_replayed': records}
    monkeypatch.setattr(CompletionEnvironment, '_replay', replay)
    assert env._replay(['record']) == {'offline_replayed': ['record']}
    assert len(calls) == 2 and not hasattr(env, '_repair_batch')


def test_replay_batch_rejects_import_changes_at_exit(monkeypatch):
    def loader(number, bundle):
        if number == 2:
            raise ComputerError('comparison_seed_changed')
        return bundle
    env, _, calls = batch_environment(monkeypatch, loader)
    monkeypatch.setattr(CompletionEnvironment, '_replay', lambda *_, **__: {'ok': True})
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        env._replay([])
    assert len(calls) == 2 and not hasattr(env, '_repair_batch')


def test_replay_batch_clears_after_replay_exception(monkeypatch):
    env, _, calls = batch_environment(monkeypatch)
    def replay(*args, **kwargs):
        raise ComputerError('agent_evidence_changed')
    monkeypatch.setattr(CompletionEnvironment, '_replay', replay)
    with pytest.raises(ComputerError, match='agent_evidence_changed'):
        env._replay([])
    assert len(calls) == 2 and not hasattr(env, '_repair_batch')


def test_old_routing_does_not_load_replay_bundle(monkeypatch):
    env, _, calls = batch_environment(monkeypatch, route=2)
    def replay(self, records, *, verify_office=True):
        assert not hasattr(self, '_repair_batch')
        return records
    monkeypatch.setattr(CompletionEnvironment, '_replay', replay)
    assert env._replay(['legacy']) == ['legacy']
    assert calls == []


@pytest.mark.parametrize('route', [3, 4])
def test_nested_office_origin_replay_uses_outer_batch_boundaries(monkeypatch, route):
    env, bundle, calls = batch_environment(monkeypatch, route=route)
    def replay(self, records, *, verify_office=True):
        assert self._repair_input() == bundle
        if records == ['outer']:
            assert verify_office
            assert self._origin_facts(['inner']) == {'prefix': ['inner']}
            assert hasattr(self, '_repair_batch')
        else:
            assert not verify_office
        return {'prefix': records}
    monkeypatch.setattr(CompletionEnvironment, '_replay', replay)
    assert env._replay(['outer']) == {'prefix': ['outer']}
    assert len(calls) == 2 and not hasattr(env, '_repair_batch')


def root_choice_inputs(route):
    env = object.__new__(CrossSiteEnvironment)
    dimension = '是否计算Excel单元格公式的结果'
    topic = 'site:readthedocs.io openpyxl python library formula calculation'
    root_url = 'https://openpyxl.readthedocs.io/en/stable/'
    detour_url = 'https://time.is/formula'
    env.goal = {'queries': [topic], 'comparison': {'completion_policy': 'dimension_first_v4',
                'dimension_routing': route, 'publisher_policy': 'explicit_attribution_v1',
                'dimensions': [dimension]}}
    facts = CompletionEnvironment._empty(None)
    facts['candidates'] = [
        {'id': 'original', 'url': root_url, 'query_index': 0},
        {'id': 'detour', 'url': detour_url, 'query_index': 0}]
    facts['active_ids'] = ['original', 'detour']
    facts['reads'] = {sid: {'observation': {'text': sid}}
                      for sid in facts['active_ids']}
    evidence = {'assessment': {'group_details': [
        {'ids': ['original', 'detour'], 'complete': False, 'covered_dimensions': [False]}],
        'publisher_support': {'original': 'original', 'detour': None}}}
    return env, facts, evidence


def test_v3_dimension_need_keeps_exact_unsupported_root_and_query_for_replay():
    env, facts, evidence = root_choice_inputs(3)
    expected = {'host': 'time.is', 'source_id': 'detour',
                'query': 'openpyxl python library formula calculation formula results calculation',
                'link_query': '是否计算Excel单元格公式的结果 formula calculate calculation evaluate evaluation',
                'capture_sha256': digest(facts['reads']['detour']['observation']),
                'review_sha256': digest(evidence), 'purpose': 'dimension_evidence_v3',
                'dimensions': ['是否计算Excel单元格公式的结果'],
                'topic': env.goal['queries'][0], 'source_url': 'https://time.is/formula'}
    expected['need_id'] = digest(expected)[:24]
    assert env._needs(facts, evidence) == [expected]


def test_v4_missing_dimension_uses_original_supported_root_and_product_query():
    env, facts, evidence = root_choice_inputs(4)
    before = deepcopy(facts)
    needs = env._needs(facts, evidence)
    assert len(needs) == 1
    need = needs[0]
    assert need['purpose'] == 'dimension_evidence_v4'
    assert need['source_id'] == 'original'
    assert need['source_url'] == 'https://openpyxl.readthedocs.io/en/stable/'
    assert need['capture_sha256'] == digest(facts['reads']['original']['observation'])
    assert need['query'] == 'openpyxl formula calculation'
    assert need['topic'] == env.goal['queries'][0]
    assert need['dimensions'] == env.goal['comparison']['dimensions']
    assert facts == before
