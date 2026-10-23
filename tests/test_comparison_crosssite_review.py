"""Focused offline regressions for v4 review findings; no durability claims."""
import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.comparison_crosssite_discovery import CrossSiteDiscoveryProcess
from agent_service.computer import ComputerError, read_record
from test_comparison_crosssite import v4, execute, tool_ctx, STAMP


_DISCOVER = CrossSiteEnvironment._discover_links


def test_candidate_capacity_blocks_discovery_before_effect():
    from agent_service.comparison_completion import CompletionEnvironment
    from unittest.mock import patch
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'outputs': ['spreadsheet', 'document'], 'comparison': {'evidence_version': 4}}
    facts = {'candidates': [{}] * 7, 'active_ids': ['new'],
             'comparisons': [{'active_ids': ['old']}], 'records': []}
    with patch.object(CompletionEnvironment, '_frontier', return_value=[{'kind': 'supplement'}]):
        assert env._frontier(facts) == [{'kind': 'compare'}]
        facts['active_ids'] = ['old']
        assert env._frontier(facts) == []


@pytest.mark.parametrize('original,expected', [
    ('supplement_crosssite_timeout', 'supplement_links_timeout'),
    ('supplement_crosssite_unavailable', 'supplement_links_unavailable'),
    ('supplement_crosssite_cancelled', 'supplement_crosssite_cancelled'),
    ('supplement_crosssite_proof_invalid', 'supplement_crosssite_proof_invalid'),
])
def test_discovery_failure_mapping_keeps_cancellation_and_invalid_proof_terminal(monkeypatch, original, expected):
    env = object.__new__(CrossSiteEnvironment)
    env.stop, env.clock, env.deadline = lambda: False, lambda: 0, 100
    def reject(*args, **kwargs):
        raise ComputerError(original)
    monkeypatch.setattr(CrossSiteDiscoveryProcess, 'discover', reject)
    with pytest.raises(ComputerError, match=f'^{expected}$'):
        _DISCOVER(env, 'https://alpha.example/', {'link_query': 'site:alpha.example format'}, [])


def test_timeout_is_retained_before_single_fixed_bing_fallback(v4, monkeypatch):
    from agent_service.computer_search import SearchProcess
    c = v4
    monkeypatch.setattr(CrossSiteEnvironment, '_discover_links', _DISCOVER)
    def timeout(*args, **kwargs):
        raise ComputerError('supplement_crosssite_timeout')
    monkeypatch.setattr(CrossSiteDiscoveryProcess, 'discover', timeout)
    calls = []
    def search(self, query, count, **kwargs):
        calls.append((query, count))
        return {'urls': ['https://alpha.example/formats'], 'searched_at': STAMP}
    monkeypatch.setattr(SearchProcess, 'search', search)
    plan = engine.prepare(c['session'], 'retain failed discovery', c['goal'])
    result = execute(c, plan, halt=2)
    assert not result['ok'] and len(calls) == 1
    record = read_record(c['session'].root / 'goals' / (plan['sha256'] + '.run') /
                         'step-02' / 'environment-result.json')['data']
    assert record.get('link_failure') == 'supplement_links_timeout', record
    assert record['result']['ok'] and record['discovery']['urls'] == ['https://alpha.example/formats']
    assert len(c['client'].calls) == 2 and not c['browser'].calls and not c['effects']


def test_eviction_preserves_only_exact_host_identity_anchor_and_query_group():
    env = object.__new__(CrossSiteEnvironment)
    ids = ['anchor', 'dependent', 'other1', 'other2', 'other3', 'other4', 'new']
    cells = {sid: [{'status': 'relevant' if sid == 'dependent' else 'missing'}] for sid in ids[:-1]}
    facts = {'active_ids': ids[:-1], 'replaced_ids': [],
             'candidates': [{'id': sid, 'query_index': 0 if i < 2 else 1}
                            for i, sid in enumerate(ids)],
             'comparisons': [{'comparison': {'assessment': {
                 'cells': cells, 'groups': [ids[:2], ids[2:6]],
                 'publisher_evidence': {sid: 'supported' for sid in ids[:-1]},
                 'publisher_support': {sid: 'anchor' if i < 2 else sid
                                       for i, sid in enumerate(ids[:-1])}}}}]}
    env._add_active(facts, 'new')
    assert 'anchor' in facts['active_ids'] and 'dependent' in facts['active_ids']
    assert facts['replaced_ids'] == ['other1']
    assert len(facts['active_ids']) == 6 and 'new' in facts['active_ids']


def test_v4_recovery_approval_reports_actual_supplement_frontier(v4):
    c = v4
    plan = engine.prepare(c['session'], 'recover supplement', c['goal'])
    result = execute(c, plan, halt=1)
    inspected = recovery.inspect(c['session'], result['run'])
    assert inspected['eligible'], inspected
    assert inspected['remaining_edits'][0].startswith('supplement:')
    assert 'compare' in inspected['remaining_edits']
    assert len(c['client'].calls) == 2 and not c['browser'].calls and not c['effects']


def test_v4_recovery_rejects_exhausted_frontier(v4, monkeypatch):
    c = v4
    monkeypatch.setattr(CrossSiteEnvironment, '_needs', lambda self, facts, evidence: [])
    plan = engine.prepare(c['session'], 'exhausted completion', c['goal'])
    result = execute(c, plan)
    assert not result['ok']
    inspected = recovery.inspect(c['session'], result['run'])
    assert not inspected['eligible'] and inspected['code'] == 'recovery_nothing_remaining'
    assert len(c['client'].calls) == 2 and not c['browser'].calls and not c['effects']


def test_v4_existing_six_session_claims_stop_before_model_or_supplement(v4):
    c = v4
    for i in range(6):
        c['session'].claim_model({'previous': i})
    result = execute(c, engine.prepare(c['session'], 'respect model claims', c['goal']))
    assert not result['ok'] and not c['client'].calls
    assert not c['discovery_calls'] and not c['browser'].calls and not c['effects']
    assert any(f['code'] == 'session_model_call_limit' for f in result['failures'])
