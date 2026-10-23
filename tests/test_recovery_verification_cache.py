"""Full production recovery replay, cached only behind live byte/lease probes."""
from copy import deepcopy
import os
from pathlib import Path

import pytest

from agent_service import research_recovery as recovery
from agent_service.computer import ComputerError, read_record
from test_computer_agent_tools import ctx as tool_ctx
from test_research_recovery import ctx, plan, run
from test_comparison_loop import ctx as comparison_ctx, run as comparison_run
from test_comparison_research_seed import completed as research_completed
from test_comparison_compact_seed import graph_tree


@pytest.fixture(autouse=True)
def empty_cache():
    recovery._VERIFIED.clear()
    yield
    recovery._VERIFIED.clear()


def test_hit_rehashes_returns_copy_and_never_replays(ctx, monkeypatch):
    stopped = run(ctx, plan(ctx), halt=3)
    root = Path(stopped['run'])
    before = recovery._inspect(ctx['session'], root)
    assert recovery._VERIFIED
    from agent_service import interaction_recovery
    original_hash = interaction_recovery._hash
    hashed = []
    def tracked(path):
        hashed.append(Path(path))
        return original_hash(path)
    monkeypatch.setattr(interaction_recovery, '_hash', tracked)
    def no_replay(*args, **kwargs):
        pytest.fail('unchanged, fully checked cache hit replayed production facts')
    monkeypatch.setattr(recovery, '_inspect_verified', no_replay)
    before['records'].clear()
    before['goal']['queries'].clear()
    again = recovery._inspect(ctx['session'], root)
    assert again['records'] and again['goal']['queries']
    assert hashed.count(root / 'outcome.json') == 2
    assert hashed.count(Path(stopped['artifacts'][0]['path'])) == 2
    assert len(ctx['effects']) == 1


@pytest.mark.parametrize('target', ['page', 'artifact', 'receipt', 'outcome', 'plan', 'new_file', 'empty_step'])
def test_warm_ancestor_changed_bytes_or_membership_refuse_same_mtime(ctx, target):
    stopped = run(ctx, plan(ctx), halt=3)
    root = Path(stopped['run'])
    continuation = recovery.prepare(ctx['session'], root)['plan']
    recovery.validate(ctx['session'], continuation)
    paths = {'page': root / 'step-02' / 'environment-result.json',
             'artifact': Path(stopped['artifacts'][0]['path']),
             'receipt': Path(stopped['artifacts'][0]['path']).parent / 'receipt.json',
             'outcome': root / 'outcome.json', 'plan': root.with_suffix('.json')}
    if target in paths:
        path = paths[target]
        stat = path.stat()
        path.write_bytes(path.read_bytes() + b' ')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif target == 'new_file':
        (root / 'unexpected.bin').write_bytes(b'new evidence')
    else:
        (root / 'step-99').mkdir()
    with pytest.raises((ComputerError, ValueError)):
        recovery.validate(ctx['session'], continuation)
    assert len(ctx['effects']) == 1


def test_warm_active_lease_is_checked_even_when_all_bytes_unchanged(ctx):
    from agent_service.run_lifecycle import _lock, _unlock
    stopped = run(ctx, plan(ctx), halt=1)
    root = Path(stopped['run'])
    assert recovery.inspect(ctx['session'], root)['eligible']
    with (root / 'owner.lease').open('r+b', buffering=0) as lease:
        _lock(lease)
        try:
            assert recovery.inspect(ctx['session'], root)['code'] == 'run_owner_active'
        finally:
            _unlock(lease)
    assert recovery.inspect(ctx['session'], root)['eligible']


def test_warm_claim_owner_and_required_claim_still_checked(ctx):
    stopped = run(ctx, plan(ctx), halt=1)
    child = recovery.prepare(ctx['session'], stopped['run'])['plan']
    recovery.validate(ctx['session'], child)
    with pytest.raises(ComputerError, match='recovery_lineage_claim_missing'):
        recovery.validate(ctx['session'], child, claimed=True)
    recovery.claim(ctx['session'], child)
    recovery.validate(ctx['session'], child, claimed=True)
    assert recovery.inspect(ctx['session'], stopped['run'])['code'] == 'recovery_already_claimed'
    path = Path(stopped['run']) / 'continuation-claim.json'
    path.write_text('{"child":"wrong"}', encoding='utf-8')
    with pytest.raises(ComputerError, match='recovery_already_claimed'):
        recovery.validate(ctx['session'], child, claimed=True)


def test_warm_grandparent_changed_is_not_hidden_by_child_cache(ctx):
    initial = run(ctx, plan(ctx), halt=1)
    child = recovery.prepare(ctx['session'], initial['run'])['plan']
    stopped_child = run(ctx, child, halt=0)
    grandchild = recovery.prepare(ctx['session'], stopped_child['run'])['plan']
    recovery.validate(ctx['session'], grandchild)
    path = Path(initial['run']) / 'outcome.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ComputerError, match='recovery_lineage_changed'):
        recovery.validate(ctx['session'], grandchild)


@pytest.mark.parametrize('target', ['page', 'artifact'])
def test_warm_equal_size_equal_mtime_tamper_is_still_rehashed(ctx, target):
    stopped = run(ctx, plan(ctx), halt=3)
    root = Path(stopped['run'])
    child = recovery.prepare(ctx['session'], root)['plan']
    recovery.validate(ctx['session'], child)
    path = (root / 'step-02' / 'environment-result.json' if target == 'page'
            else Path(stopped['artifacts'][0]['path']))
    original, stat = path.read_bytes(), path.stat()
    path.write_bytes(b'!' + original[1:])
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == len(original) and path.stat().st_mtime_ns == stat.st_mtime_ns
    with pytest.raises((ComputerError, ValueError)):
        recovery.validate(ctx['session'], child)


@pytest.mark.parametrize('when', ['miss', 'hit'])
def test_snapshot_exit_change_rejected_and_not_cached(ctx, monkeypatch, when):
    stopped = run(ctx, plan(ctx), halt=1)
    root = Path(stopped['run'])
    if when == 'hit':
        recovery._inspect(ctx['session'], root)
    original = recovery._verification_snapshot
    calls = []
    def mutate(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            path = root / 'outcome.json'
            path.write_bytes(path.read_bytes() + b' ')
        return value
    monkeypatch.setattr(recovery, '_verification_snapshot', mutate)
    with pytest.raises(ComputerError, match='recovery_evidence_changed'):
        recovery._inspect(ctx['session'], root)
    if when == 'miss':
        assert not recovery._VERIFIED


def test_v3_recovery_replays_facts_once_and_cache_is_bounded(comparison_ctx, monkeypatch):
    from agent_service.comparison_completion import CompletionEnvironment
    from agent_service.computer_agent import prepare
    c = comparison_ctx
    approved = prepare(c['session'], 'cache actual completion recovery', c['goal'])
    stopped = comparison_run(c, approved, halt=2)
    original = CompletionEnvironment._facts
    calls = []
    def counted(self):
        calls.append(1)
        return original(self)
    monkeypatch.setattr(CompletionEnvironment, '_facts', counted)
    recovery._inspect(c['session'], stopped['run'])
    assert len(calls) == 1
    recovery._inspect(c['session'], stopped['run'])
    assert len(calls) == 1
    for index in range(recovery._CACHE_LIMIT + 1):
        c['goal']['queries'] = ['first', 'second']
        next_plan = prepare(c['session'], f'bounded cache {index}', deepcopy(c['goal']))
        next_run = comparison_run(c, next_plan, halt=0)
        recovery._inspect(c['session'], next_run['run'])
    assert len(recovery._VERIFIED) == recovery._CACHE_LIMIT


def test_unsupported_graph_does_not_cache_or_skip_production_replay(ctx, monkeypatch):
    stopped = run(ctx, plan(ctx), halt=1)
    def unsupported(*args, **kwargs):
        raise ComputerError('unsupported_future_seed')
    monkeypatch.setattr(recovery, '_verification_snapshot', unsupported)
    assert recovery.inspect(ctx['session'], stopped['run'])['eligible']
    assert not recovery._VERIFIED


def test_missing_environment_receipt_is_never_recreated(ctx, monkeypatch):
    stopped = run(ctx, plan(ctx), halt=1)
    root = Path(stopped['run'])
    original = recovery.AgentEnvironment._facts
    def vanished(self):
        (self.run / 'environment-goal.json').unlink()
        return original(self)
    monkeypatch.setattr(recovery.AgentEnvironment, '_facts', vanished)
    assert not recovery.inspect(ctx['session'], root)['eligible']
    assert not (root / 'environment-goal.json').exists()


@pytest.mark.parametrize('target', ['source_bytes', 'native_output', 'active', 'claim'])
def test_warm_comparison_import_rechecks_seed_and_native_outputs(research_completed, target):
    from agent_service.comparison_crosssite import prepare_followup
    from agent_service.run_lifecycle import _lock, _unlock
    c, ancestor, original, outcome = research_completed
    goal = deepcopy(original['goal'])
    goal['required_terms'] = []
    goal['comparison'] = {'dimensions': ['fact'], 'min_domains': 2}
    followup = prepare_followup(c['session'], 'cache seeded comparison recovery', goal, ancestor)
    stopped = comparison_run(c, followup, halt=0)
    root = Path(stopped['run'])
    expected = recovery._inspect(c['session'], root)
    assert expected['remaining'][0] == 'compare' and recovery._VERIFIED
    if target == 'active':
        with (ancestor / 'owner.lease').open('r+b', buffering=0) as lease:
            _lock(lease)
            try:
                assert not recovery.inspect(c['session'], root)['eligible']
            finally:
                _unlock(lease)
    else:
        if target == 'claim':
            (ancestor / 'continuation-claim.json').write_text('{"child":"other"}', encoding='utf-8')
        else:
            path = (ancestor / 'step-02' / 'environment-result.json' if target == 'source_bytes'
                    else Path(outcome['artifacts'][0]['path']))
            stat = path.stat()
            path.write_bytes(path.read_bytes() + b' ')
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert not recovery.inspect(c['session'], root)['eligible']
    assert len(c['effects']) == 2


@pytest.fixture
def compact_parent(comparison_ctx, monkeypatch):
    from agent_service import comparison_compact_seed as compact
    from agent_service.computer_agent import prepare
    from test_comparison_failed_seed import install_roots, failed_model
    from test_computer_agent import Client
    c = comparison_ctx
    install_roots(c, monkeypatch)
    c['client'] = Client(failed_model)
    approved = prepare(c['session'], 'offline compact cache producer', c['goal'])
    failed = comparison_run(c, approved)
    assert not failed['ok'] and failed['code'] == 'agent_no_safe_next_action'
    ancestor = Path(failed['run'])
    manifest = compact.create_manifest(ancestor)
    goal = deepcopy(approved['goal'])
    goal['comparison'].update(evidence_version=4, seed=manifest,
        completion_policy='dimension_first_v4', dimension_routing=5)
    child = prepare(c['session'], 'offline seeded compact pause', goal)
    stopped = comparison_run(c, child, halt=0)
    root = Path(stopped['run'])
    assert not stopped['ok'] and not stopped['attempts'] and not c['effects']
    return c, root, ancestor, manifest


def test_compact_hit_rechecks_exact_graph_twice_without_seed_loader(compact_parent, monkeypatch):
    from agent_service import comparison_compact_seed as compact
    from agent_service.comparison_completion import CompletionEnvironment
    c, root, ancestor, manifest = compact_parent
    original_facts, facts_calls = CompletionEnvironment._facts, []
    def facts(self):
        facts_calls.append(1)
        return original_facts(self)
    monkeypatch.setattr(CompletionEnvironment, '_facts', facts)
    expected = recovery._inspect(c['session'], root)
    assert len(facts_calls) == 1 and recovery._VERIFIED
    snapshot = recovery._verification_snapshot(c['session'], root, child=None, allow_unclaimed=False)
    assert snapshot[str(ancestor) + '|compact'] == manifest['dependency_graph']
    original_graph, graph_calls = compact._graph, []
    def graph(value):
        graph_calls.append(value)
        return original_graph(value)
    monkeypatch.setattr(compact, '_graph', graph)
    monkeypatch.setattr(compact, 'load_bundle', lambda *_: pytest.fail('warm graph hit invoked seed loader'))
    assert recovery._inspect(c['session'], root) == expected
    assert graph_calls == [ancestor, ancestor] and len(facts_calls) == 1


@pytest.mark.parametrize('target', ['bytes', 'claim', 'active'])
def test_compact_warm_graph_changes_and_owner_checks_never_return_cached(compact_parent, target):
    from agent_service.run_lifecycle import _lock, _unlock
    c, root, ancestor, _ = compact_parent
    assert recovery.inspect(c['session'], root)['eligible']
    if target == 'active':
        with (ancestor / 'owner.lease').open('r+b', buffering=0) as lease:
            _lock(lease)
            try:
                assert not recovery.inspect(c['session'], root)['eligible']
            finally:
                _unlock(lease)
    else:
        if target == 'bytes':
            path = ancestor / 'step-02' / 'environment-result.json'
            stat = path.stat()
            path.write_bytes(b'!' + path.read_bytes()[1:])
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        else:
            (ancestor / 'continuation-claim.json').write_text('{"child":"other"}', encoding='utf-8')
        assert not recovery.inspect(c['session'], root)['eligible']
    assert not c['effects']


def test_compact_cache_support_keeps_legacy_failed_depth_limit(graph_tree):
    from agent_service import comparison_failed_seed as failed
    from agent_service import comparison_compact_seed as compact
    create, _ = graph_tree
    root = create(4)[-1]
    assert len(compact._graph(root)['nodes']) == 4
    with pytest.raises(ComputerError, match='comparison_failed_seed_invalid'):
        failed._dependencies(read_record(root.with_suffix('.json')), root)
