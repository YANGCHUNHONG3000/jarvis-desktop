"""Offline v4 original-query fallback and immutable recovery receipts."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import comparison_seed, computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, read_record
from agent_service.computer_agent_tools import _selection
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.contracts import Observation
from agent_service.source_comparison import validate_comparison
from test_comparison_crosssite import execute, v4
from test_computer_agent_tools import STAMP, ctx as tool_ctx


def _frontier_facts(*, records=3, candidates=2, pending=True, changed=False):
    """A first negative review and an exhausted supplement need."""
    first = {'id': 'a', 'url': 'https://alpha.example/', 'query_index': 0}
    second = {'id': 'b', 'url': 'https://beta.other/', 'query_index': 1}
    items = [first, second]
    if candidates > 2:
        items.append({'id': 'unused', 'url': 'https://gamma.test/', 'query_index': 1})
    for i in range(3, candidates):
        items.append({'id': f'extra{i}', 'url': f'https://extra{i}.test/', 'query_index': 1,
                      'need_id': 'spent'})
    active = ['a', 'b']
    return {
        'records': [None] * records, 'office_blocked': False, 'comparison': None,
        'done': [], 'office_started': False,
        'comparisons': [{'active_ids': ['a'] if changed else active,
                         'comparison': {'assessment': {'group_details': [
                             {'ids': ['a'], 'complete': False},
                             {'ids': ['b'], 'complete': True}]}}}],
        'needs': [{'need_id': 'spent', 'host': 'alpha.example'}],
        'need_reads': {}, 'supplements': ['spent'], 'candidates': items,
        'reads': {'a': {'result': {'ok': True}}, 'b': {'result': {'ok': True}},
                  **{f'extra{i}': {'result': {'ok': False}} for i in range(3, candidates)}},
        'active_ids': active, 'queries': [0] if pending else [0, 1],
    }


def _env(policy=True):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['spreadsheet', 'document'],
                'comparison': {'completion_policy': 'original_query_fallback_v1' if policy else None}}
    return env


def test_fallback_prefers_unread_original_then_pending_query():
    env = _env()
    assert env._frontier(_frontier_facts(candidates=3)) == [
        {'kind': 'read', 'candidate_id': 'unused'}]
    assert env._frontier(_frontier_facts()) == [{'kind': 'search', 'query_index': 1}]
    assert _env(False)._frontier(_frontier_facts()) == []


@pytest.mark.parametrize('records,candidates,expected', [
    (8, 2, []),  # Search + read + final comparison + both outputs need five actions.
    (7, 2, [{'kind': 'search', 'query_index': 1}]),
    (3, 7, []),  # A two-result search could exceed the eight-candidate bound.
])
def test_fallback_reserves_actions_and_candidate_capacity(records, candidates, expected):
    facts = _frontier_facts(records=records, candidates=candidates)
    if candidates == 7:
        facts['reads']['unused'] = {'result': {'ok': False}}
    assert _env()._frontier(facts) == expected


@pytest.mark.parametrize('version,seed,policy', [
    (4, True, 'unknown'),
    (4, True, None),
    (3, True, 'original_query_fallback_v1'),
    (4, False, 'original_query_fallback_v1'),
])
def test_completion_policy_requires_exact_v4_seed(monkeypatch, version, seed, policy):
    monkeypatch.setattr(comparison_seed, 'validate_manifest', lambda value: deepcopy(value))
    comparison = {'dimensions': ['format'], 'min_domains': 2,
                  'evidence_version': version, 'completion_policy': policy}
    if seed:
        comparison['seed'] = {'version': 1}
    with pytest.raises(ComputerError, match='comparison_invalid'):
        validate_comparison(comparison)


def test_fallback_does_not_run_after_terminal_or_successful_review():
    env = _env()
    facts = _frontier_facts()
    facts['comparisons'].append(deepcopy(facts['comparisons'][0]))
    assert env._frontier(facts) == []
    facts = _frontier_facts()
    facts['office_blocked'] = True
    assert env._frontier(facts) == []
    facts = _frontier_facts()
    facts['comparison'] = {'verified': True}
    assert env._frontier(facts) == [{'kind': 'spreadsheet'}]


def test_failed_original_candidate_is_consumed_and_unchanged_set_stops():
    facts = _frontier_facts(candidates=3, pending=False)
    facts['reads']['unused'] = {'result': {'ok': False}}
    assert _env()._frontier(facts) == []


def test_changed_set_keeps_final_review_when_fallback_cannot_fit():
    facts = _frontier_facts(records=9, changed=True)
    assert _env()._frontier(facts) == [{'kind': 'compare'}]


def _configure_fallback(v4, monkeypatch, *, third_result=None):
    c = v4
    c['goal']['comparison']['seed']['version'] = 1
    c['goal']['comparison']['completion_policy'] = 'original_query_fallback_v1'
    previous = comparison_seed.load_seed

    def captured(manifest):
        rows = previous(manifest)
        # Both imported hosts came from the first query. Beta is complete,
        # while alpha needs a format quote from the untouched second query.
        rows[1][0]['query_index'] = 0
        text = 'Published by Group1. Editing supported. ODF format supported.'
        rows[1][1]['observation'] = Observation(
            rows[1][0]['url'], rows[1][0]['url'], 'Source', text, STAMP).to_dict()
        rows[1][1]['excerpt'] = _selection(text, 'Editing', [])
        return rows

    monkeypatch.setattr(comparison_seed, 'load_seed', captured)
    gamma = 'https://docs.alpha.example/formats'
    c['browser'].text[gamma] = 'Published by Group0. Editing supported. ODF format supported.'
    c['search'].results['second'] = [gamma] if third_result is None else third_result
    original_search = c['search'].search

    def search(query, *args, **kwargs):
        if query == 'second':
            return original_search(query, *args, **kwargs)
        c['search'].calls.append(query)
        raise ComputerError('search_unavailable')

    c['search'].search = search
    c['options']['search'] = c['search']
    return c, gamma


@pytest.mark.parametrize('halt', [None, 2, 4])
def test_failed_supplement_uses_approved_query_and_completes_office(v4, monkeypatch, halt):
    c, gamma = _configure_fallback(v4, monkeypatch)
    plan = engine.prepare(c['session'], '补充原查询并核验', c['goal'])
    first = execute(c, plan, halt)
    preserved = {}
    if halt:
        assert not first['ok']
        preserved = {p: p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
        plan = recovery.prepare(c['session'], first['run'])['plan']
        result = execute(c, plan)
    else:
        result = first
    assert result['ok'], result
    paths = [Path(first['run'])] if not halt else [Path(first['run']), Path(result['run'])]
    rows = [read_record(p)['data'] for root in paths
            for p in sorted(root.glob('step-*/environment-result.json'))]
    assert [row['action']['kind'] for row in rows] == [
        'compare', 'supplement', 'search', 'read', 'compare', 'spreadsheet', 'document']
    assert rows[1]['result']['code'] == 'search_unavailable'
    assert rows[2]['action']['query_index'] == 1
    assert rows[3]['read_purpose'] == {'purpose': 'initial'}
    assert rows[4]['comparison']['assessment']['complete_independent_count'] == 2
    assert c['browser'].calls == [gamma]
    assert c['search'].calls.count('second') == 1
    assert len(c['client'].calls) == 4 and len(c['effects']) == 2
    assert all(p.read_bytes() == original for p, original in preserved.items())


def test_failed_original_query_stops_without_second_review_or_office(v4, monkeypatch):
    c, gamma = _configure_fallback(v4, monkeypatch, third_result=ComputerError('search_unavailable'))
    result = execute(c, engine.prepare(c['session'], '原查询失败', c['goal']))
    assert not result['ok'] and not c['effects']
    rows = [read_record(p)['data'] for p in sorted(Path(result['run']).glob('step-*/environment-result.json'))]
    assert [row['action']['kind'] for row in rows] == ['compare', 'supplement', 'search']
    assert c['search'].calls.count('second') == 1 and gamma not in c['browser'].calls
    assert len(c['client'].calls) == 2
