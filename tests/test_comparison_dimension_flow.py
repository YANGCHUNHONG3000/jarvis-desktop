"""Formula gaps traverse two bounded needs and resume without consumed work.

Production failed capture loaders, saved receipts, lineage, and OOXML receipt
checks are real. Discovery/model/Office executors use explicit offline fixtures.
"""
from copy import deepcopy
from itertools import combinations
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest

from agent_service import research_recovery as recovery
from agent_service.comparison_crosssite import CrossSiteEnvironment, prepare_followup
from agent_service.comparison_repair import candidate_count
from agent_service.computer import ComputerError
from test_comparison_capture_repair import run_cached_imports
from test_comparison_failed_seed import failed, records, snapshot
from test_comparison_loop import ctx
from test_comparison_query_fallback import _frontier_facts
from test_computer_agent import Client
from test_computer_agent_tools import ctx as tool_ctx, A, B, STAMP


DIMENSION = '是否计算Excel单元格公式的结果'
QUOTE = 'Does not calculate formulas.'


def formula_model(data):
    if 'selection' not in data:
        return {'sources': [{'id': item['id'], 'spans': [
            {'quote': QUOTE} if QUOTE in item['text'] else None]} for item in data['sources']]}
    selected = {row['id']: row['spans'][0] for row in data['selection']['sources']}
    def name(item):
        return 'Alpha' if urlsplit(item['url']).hostname == urlsplit(A).hostname else 'Beta'
    return {'sources': [{'id': item['id'], 'publisher': {
                'name': name(item), 'quote': 'Publisher ' + name(item)},
                'cells': [{'status': 'relevant' if selected[item['id']] else 'missing',
                           'reason': 'Formula behavior is stated only in the selected sentence.'}]}
                        for item in data['sources']],
            'pairs': [{'left': left['id'], 'right': right['id'],
                       'relation': 'same' if name(left) == name(right) else 'independent',
                       'reason': 'Captured publisher identity.'}
                      for left, right in combinations(data['sources'], 2)]}


def configure_dimension(failed, monkeypatch, *, routing=None):
    c, root, original, _ = failed
    goal = deepcopy(original['goal'])
    goal['comparison']['dimensions'] = [DIMENSION]
    c['client'] = Client(formula_model)
    for base, group in ((A, 'Alpha'), (B, 'Beta')):
        c['browser'].text[base + 'formula'] = f'Publisher {group}; {QUOTE} alpha omega'
    c['options']['search'] = None
    calls = []
    def discover(self, source, action, excluded):
        assert action['purpose'] == (f'dimension_evidence_v{routing}' if routing else 'dimension_evidence_v1')
        assert action['dimensions'] == [DIMENSION]
        if routing:
            assert DIMENSION in action['link_query'] and 'formula calculation' in action['query']
            assert action['source_url'] == source
        else:
            assert DIMENSION in action['query'] and 'formula calculate' in action['query']
        query_index = 0 if urlsplit(source).hostname == urlsplit(A).hostname else 1
        assert goal['queries'][query_index] in action['query']
        assert source in excluded
        target = (A if query_index == 0 else B) + 'formula'
        calls.append((source, deepcopy(action), list(excluded)))
        return {'discovery': {'urls': [target], 'searched_at': STAMP},
                'proof': {'fixture': True, 'source': source, 'query': action['link_query'],
                          'excluded': list(excluded)}}
    def validate(self, linked, source, action, excluded):
        assert linked['proof'] == {'fixture': True, 'source': source,
                                  'query': action['link_query'], 'excluded': list(excluded)}
        assert len(linked['discovery']['urls']) == 1
        return deepcopy(linked)
    monkeypatch.setattr(CrossSiteEnvironment, '_discover_links', discover)
    monkeypatch.setattr(CrossSiteEnvironment, '_validate_links', validate)
    plan = prepare_followup(c['session'], 'repair formula gaps with captured history', goal, root)
    if routing:
        assert plan['goal']['comparison']['completion_policy'] == 'dimension_first_v4'
        assert plan['goal']['comparison']['dimension_routing'] == routing
    else:
        # Keep the approved v3 route immutable when the new task default advances.
        from agent_service.computer_goals import _save_goal
        body = {key: deepcopy(value) for key, value in plan.items() if key != 'sha256'}
        body['goal']['comparison']['completion_policy'] = 'dimension_repair_v3'
        body['goal']['comparison'].pop('dimension_routing', None)
        plan = _save_goal(c['session'], body)
        assert plan['goal']['comparison']['completion_policy'] == 'dimension_repair_v3'
    return c, root, plan, calls


def test_approved_v3_fixture_removes_new_routing_field(failed, monkeypatch):
    _, _, plan, _ = configure_dimension(failed, monkeypatch)
    assert plan['goal']['comparison']['completion_policy'] == 'dimension_repair_v3'
    assert 'dimension_routing' not in plan['goal']['comparison']


def test_route4_offline_double_supplement_resume_office_and_next_task_handoff(failed, monkeypatch):
    """Offline executors; real bundles/first parent replay, then verified parent copies."""
    from agent_service import comparison_failed_seed
    from agent_service.artifact_source import snapshot_artifact_source
    from agent_service.computer import read_record
    from agent_service.office_handoff import select_outputs
    from test_artifact_source import new_task
    from test_comparison_loop import run
    c, old_root, plan, discoveries = configure_dimension(failed, monkeypatch, routing=4)
    before = snapshot(old_root)
    old_searches, old_reads = list(c['search'].calls), list(c['browser'].calls)
    real_bundle = comparison_failed_seed.load_bundle
    verified_calls = []
    def counted(manifest):
        verified_calls.append(manifest['run'])
        return real_bundle(manifest)
    monkeypatch.setattr(comparison_failed_seed, 'load_bundle', counted)
    paused = run(c, plan, halt=4)
    assert not paused['ok'] and not c['effects'], paused
    paused_root = Path(paused['run'])
    initial_records = records(paused_root)
    assert [row['action']['kind'] for row in initial_records] == ['compare', 'supplement', 'read', 'supplement']
    assert len(discoveries) == 2 and c['browser'].calls == old_reads + [A + 'formula']
    preserved = snapshot(paused_root)
    # Verify actual paused receipts once. Subsequent inspection copies isolate
    # the new route from repeated parent replay; claim/lease checks stay real.
    # The complete read-only parent bytes are checked again at this test's end.
    inspect_parent = recovery._inspect
    verified_parent = inspect_parent(c['session'], paused_root)
    from agent_service.interaction_recovery import _claim
    from agent_service.run_lifecycle import assert_inactive
    def inspected(session, run, *, child=None, allow_unclaimed=False, seen=()):
        if Path(run) != paused_root:
            return inspect_parent(session, run, child=child, allow_unclaimed=allow_unclaimed, seen=seen)
        _claim(paused_root, child, allow_unclaimed)
        assert_inactive(paused_root)
        return deepcopy(verified_parent)
    monkeypatch.setattr(recovery, '_inspect', inspected)
    resumed = recovery.prepare(c['session'], paused_root)['plan']
    outcome = run(c, resumed)
    assert outcome['ok'], outcome
    assert [row['action']['kind'] for row in records(Path(outcome['run']))] == [
        'read', 'compare', 'spreadsheet', 'document']
    assert c['search'].calls == old_searches
    assert c['browser'].calls == old_reads + [A + 'formula', B + 'formula']
    assert len(discoveries) == 2 and len(c['client'].calls) == 4
    assert verified_calls and all(value == str(old_root) for value in verified_calls)
    assert [spec['kind'] for spec, _ in c['effects']] == ['spreadsheet', 'document']
    outputs = [{key: item[key] for key in ('path', 'sha256')} for item in outcome['artifacts']]
    base = c['options']['task_base']
    references = select_outputs(outputs, base=base)
    assert len(references) == 2
    for reference, output in zip(references, outputs):
        target = new_task(base)
        imported = snapshot_artifact_source(reference, target, base)
        assert imported['sha256'] == output['sha256']
        assert read_record(target / 'sources/provenance.json')['artifact'] == output['path']
        import json
        assert QUOTE in json.dumps(imported['observed'], ensure_ascii=False)
    assert snapshot(old_root) == before
    assert all(Path(path).read_bytes() == value for path, value in preserved.items())


@pytest.mark.parametrize('pause', [None, 2, 6], ids=['complete', 'after-discovery', 'before-office'])
def test_two_formula_needs_review_and_office_resume_without_research(failed, monkeypatch, pause):
    c, old_root, plan, discoveries = configure_dimension(failed, monkeypatch)
    before = snapshot(old_root)
    old_searches, old_reads = list(c['search'].calls), list(c['browser'].calls)
    outcome = run_cached_imports(c, plan, monkeypatch, halt=pause)
    all_records = records(Path(outcome['run']))
    if pause:
        assert not outcome['ok'], outcome
        stopped_root = Path(outcome['run'])
        preserved = snapshot(stopped_root)
        # The production recovery builder performs the real saved lineage replay.
        if pause == 6:
            from agent_service import comparison_failed_seed, comparison_seed
            manifest = plan['goal']['comparison']['seed']
            real_load, real_history = comparison_seed.load_seed, comparison_failed_seed.load_repair_history
            captured, consumed = real_load(manifest), real_history(manifest)
            # The other pause keeps full source loading; this branch isolates
            # actual Office-only lineage replay after the same verified import.
            with monkeypatch.context() as cache:
                cache.setattr(comparison_seed, 'load_seed', lambda value: deepcopy(captured)
                              if value == manifest else real_load(value))
                cache.setattr(comparison_failed_seed, 'load_repair_history', lambda value: deepcopy(consumed)
                              if value == manifest else real_history(value))
                resumed = recovery.prepare(c['session'], stopped_root)['plan']
        else:
            resumed = recovery.prepare(c['session'], stopped_root)['plan']
        outcome = run_cached_imports(c, resumed, monkeypatch)
        all_records += records(Path(outcome['run']))
        assert all(Path(path).read_bytes() == value for path, value in preserved.items())
        if pause == 6:
            assert [row['action']['kind'] for row in records(Path(outcome['run']))] == ['spreadsheet', 'document']
    assert outcome['ok'], outcome
    assert [row['action']['kind'] for row in all_records] == [
        'compare', 'supplement', 'read', 'supplement', 'read', 'compare', 'spreadsheet', 'document']
    assert all_records[0]['result']['code'] == 'comparison_semantic_insufficient'
    assert all_records[5]['result']['ok']
    assert len(discoveries) == 2
    assert [row['action']['purpose'] for row in all_records if row['action']['kind'] == 'supplement'] == [
        'dimension_evidence_v1', 'dimension_evidence_v1']
    assert c['search'].calls == old_searches
    assert c['browser'].calls == old_reads + [A + 'formula', B + 'formula']
    assert len(c['client'].calls) == 4
    assert [spec['kind'] for spec, _ in c['effects']] == ['spreadsheet', 'document']
    assert len(outcome['artifacts']) == 2 and outcome['source_reads'] == 6
    assert snapshot(old_root) == before


@pytest.mark.parametrize('size,expected', [(7, 'supplement'), (8, None)])
def test_dimension_capacity_reserves_exactly_one_slot(size, expected):
    facts = _frontier_facts(pending=False)
    facts['comparisons'][0]['result'] = {'ok': False, 'state': 'failed', 'code': 'comparison_semantic_insufficient'}
    facts['needs'] = [{'need_id': 'new', 'purpose': 'dimension_evidence_v1', 'query': 'formula', 'link_query': 'formula'}]
    facts['supplements'] = []
    facts['repair_history'] = {'read_urls': [f'https://consumed{i}.test/' for i in range(size - 2)],
                               'supplement_keys': []}
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['spreadsheet', 'document'],
                'comparison': {'completion_policy': 'dimension_repair_v3'}}
    assert candidate_count(facts) == size
    actual = env._frontier(facts)
    assert (actual[0]['kind'] if actual else None) == expected


def test_dimension_search_fallback_discards_old_reads_and_keeps_one(monkeypatch):
    from agent_service import comparison_failed_seed
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {'run': 'verified offline fixture'}}}
    env.stop, env.clock, env.deadline = lambda: False, time.monotonic, float('inf')
    class Search:
        def search(self, query, maximum, **kwargs):
            assert maximum == 2
            return {'urls': [A, B], 'searched_at': STAMP}
    env.search = Search()
    monkeypatch.setattr(comparison_failed_seed, 'load_repair_history', lambda _: {'read_urls': [A]})
    value = env._search_candidates({'purpose': 'dimension_evidence_v1'}, 'formula')
    assert value['urls'] == [B]
    monkeypatch.setattr(comparison_failed_seed, 'load_repair_history', lambda _: {'read_urls': [A, B]})
    with pytest.raises(ComputerError, match='search_unavailable'):
        env._search_candidates({'purpose': 'dimension_evidence_v1'}, 'formula')


@pytest.mark.parametrize('code,mapped', [('supplement_dimension_unavailable', 'supplement_links_unavailable'),
                                      ('supplement_dimension_timeout', 'supplement_links_timeout')])
def test_dimension_worker_failures_map_to_known_search_fallback(monkeypatch, code, mapped):
    from agent_service.comparison_dimension_discovery import DimensionDiscoveryProcess
    env = object.__new__(CrossSiteEnvironment)
    env.stop, env.clock, env.deadline = lambda: False, time.monotonic, float('inf')
    env.run = Path('offline-run')
    monkeypatch.setattr(env, '_prior_discoveries', lambda: [])
    def unavailable(*args, **kwargs):
        raise ComputerError(code)
    monkeypatch.setattr(DimensionDiscoveryProcess, 'discover', unavailable)
    with pytest.raises(ComputerError, match=mapped):
        env._discover_links(A, {'purpose': 'dimension_evidence_v1', 'link_query': 'formula'}, [])


def test_old_navigation_proof_keeps_its_validator_under_new_policy(monkeypatch):
    from test_comparison_crosssite_discovery import result, URL, QUERY
    linked, _ = result()
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'completion_policy': 'dimension_repair_v3'}}
    monkeypatch.setattr(env, '_prior_discoveries', lambda: pytest.fail('old proof requested dimension history'))
    assert env._validate_links(linked, URL, {'link_query': QUERY}, []) == linked


@pytest.mark.parametrize('target,expected', [
    ('https://github.com/owner/project/blob/main/formula.py', True),
    ('https://github.com/other/project/blob/main/formula.py', False),
    ('https://github.com/owner/unrelated/blob/main/formula.py', False),
])
def test_search_fallback_keeps_hosted_project_scope(monkeypatch, target, expected):
    from agent_service import comparison_failed_seed, comparison_seed
    source = 'https://github.com/owner/project'
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {'run': 'offline-verified-fixture'}}}
    monkeypatch.setattr(comparison_failed_seed, 'load_repair_history', lambda _: {'read_urls': []})
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda _: [({'id': 'source', 'url': source}, {})])
    assert env._supplement_allowed({}, {'purpose': 'dimension_evidence_v1', 'source_id': 'source'}, [target]) == expected


@pytest.mark.parametrize('policy,covered,purpose', [
    ('dimension_repair_v3', False, 'attribution'),
    ('dimension_first_v4', False, 'dimension_evidence_v1'),
    ('dimension_first_v4', True, 'attribution'),
])
def test_v3_attribution_order_is_preserved_and_v4_prioritizes_missing_dimension(policy, covered, purpose):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first approved topic', 'second approved topic'],
                'comparison': {'completion_policy': policy, 'publisher_policy': 'explicit_attribution_v1',
                               'dimensions': [DIMENSION]}}
    facts = {'candidates': [{'id': 'alpha', 'url': A, 'query_index': 0},
                            {'id': 'beta', 'url': B, 'query_index': 1}],
             'active_ids': ['alpha', 'beta'],
             'reads': {sid: {'observation': {'text': 'Publisher not yet established.'}}
                       for sid in ['alpha', 'beta']}}
    evidence = {'assessment': {'group_details': [
                    {'ids': [sid], 'complete': False, 'covered_dimensions': [covered]}
                    for sid in ['alpha', 'beta']],
                'publisher_support': {'alpha': None, 'beta': None}}}
    needs = env._needs(facts, evidence)
    assert len(needs) == 2 and all(need['purpose'] == purpose for need in needs)
    if purpose == 'dimension_evidence_v1':
        assert all(need['dimensions'] == [DIMENSION] and DIMENSION in need['query'] for need in needs)
        assert 'first approved topic' in needs[0]['query']
    else:
        assert all('authors maintainers' in need['query'] for need in needs)


@pytest.mark.parametrize('policy,routing,valid', [
    ('dimension_first_v4', 2, True), ('dimension_first_v4', 3, True),
    ('dimension_first_v4', 4, True), ('dimension_first_v4', 5, False),
    ('dimension_first_v4', 1, False),
    ('dimension_first_v4', True, False), ('dimension_first_v4', '3', False),
    ('dimension_repair_v3', 2, False), ('capture_repair_v2', 3, False),
])
def test_dimension_routing_is_bound_to_policy_and_exact_version(monkeypatch, policy, routing, valid):
    from agent_service import comparison_seed
    from agent_service.source_comparison import validate_comparison
    monkeypatch.setattr(comparison_seed, 'validate_manifest', deepcopy)
    value = {'dimensions': [DIMENSION], 'min_domains': 2, 'evidence_version': 4,
             'seed': {'version': 4}, 'completion_policy': policy, 'dimension_routing': routing}
    if valid:
        assert validate_comparison(value) == value
    else:
        with pytest.raises(ComputerError, match='comparison_invalid'):
            validate_comparison(value)
