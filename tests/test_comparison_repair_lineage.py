"""One additional failed import, verified historical work, and finite ancestry.

The real §71 fixture is read only and loaded once per module. Negative graph
cases use an in-memory filesystem boundary; no model, network, or Office calls.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import comparison_failed_seed as failed
from agent_service import comparison_seed as seed
from agent_service.comparison_repair import (
    DIMENSION_POLICY, POLICY, enabled, history, supplement_key,
)
from agent_service.computer import ComputerError, digest, read_record
from test_comparison_capture_repair import followup, run_cached_imports
from test_comparison_failed_seed import failed as failed_run, records, snapshot
from test_comparison_loop import ctx
from test_computer_agent_tools import ctx as tool_ctx


def test_offline_failed_import_exports_one_additional_verified_layer(failed_run, monkeypatch):
    c, root, original, _ = failed_run
    before = snapshot(root)
    calls = list(c['search'].calls), list(c['browser'].calls)
    plan = followup(c, root, original)
    outcome = run_cached_imports(c, plan, monkeypatch)
    assert not outcome['ok'] and not c['effects']
    current = Path(outcome['run'])
    assert [row['action']['kind'] for row in records(current)] == ['compare', 'compare']
    manifest = failed.create_manifest(current)
    assert len(seed.load_seed(manifest)) == 4
    value = failed.load_repair_history(manifest)
    assert value['queries'] == [0, 1]
    assert value['read_urls'] == sorted(c['browser'].calls)
    assert (c['search'].calls, c['browser'].calls) == calls
    assert snapshot(root) == before


def test_bundle_preserves_capture_history_contract_and_copy_isolation(failed_run):
    _, root, _, _ = failed_run
    manifest = failed.create_manifest(root)
    before = snapshot(root)
    captured, consumed = failed.load_bundle(manifest)
    assert captured == failed.load_seed(manifest)
    assert consumed == failed.load_repair_history(manifest)
    captured[0][1]['observation']['text'] = 'caller mutation'
    consumed['queries'].clear()
    again = failed.load_bundle(manifest)
    assert again[0][0][1]['observation']['text'] != 'caller mutation'
    assert again[1]['queries'] == [0, 1]
    assert snapshot(root) == before


def test_bundle_rejects_bytes_changed_after_verified_replay(failed_run, monkeypatch):
    _, root, _, _ = failed_run
    manifest = failed.create_manifest(root)
    target = root / 'step-01/environment-result.json'
    before = target.read_bytes()
    original = failed._verified
    def raced(root, frozen):
        value = original(root, frozen)
        target.write_bytes(before + b' ')
        return value
    monkeypatch.setattr(failed, '_verified', raced)
    try:
        with pytest.raises(ComputerError, match='comparison_seed_changed'):
            failed.load_bundle(manifest)
    finally:
        target.write_bytes(before)


@pytest.fixture(scope='module')
def formula_failure():
    entry = Path(__file__).resolve().parents[1] / 'validation/artifacts/capture-repair-loop-20261023/outcome.json'
    if not entry.exists():
        pytest.skip('local §71 production evidence is unavailable')
    root = Path(read_record(entry)['run'])
    plan = read_record(root.with_suffix('.json'))
    manifest = failed.create_manifest(root)
    dependencies = failed._dependencies(plan, root)
    roots = [root, *map(Path, dependencies)]
    paths = {path for origin in roots for path in origin.rglob('*') if path.is_file()}
    paths.update(origin.with_suffix('.json') for origin in roots)
    for snapshot in dependencies.values():
        for folder, inventory in snapshot.get('outputs', {}).items():
            paths.update(Path(folder) / name for name in inventory)
    before = {path: seed._hash(path) for path in paths}
    value = failed.load_repair_history(manifest)
    yield root, plan, manifest, dependencies, value
    assert {path: seed._hash(path) for path in paths} == before


def test_real_formula_failure_replays_one_failed_layer_and_all_history(formula_failure):
    root, plan, manifest, dependencies, value = formula_failure
    assert plan['goal']['comparison']['seed']['version'] == 4
    assert len(seed.load_seed(manifest)) == 4
    assert value['queries'] == [0, 1]
    assert len(value['read_urls']) == 6
    assert len(value['supplement_keys']) == 3
    assert len(value['discoveries']) == 3
    previous = failed.load_repair_history(plan['goal']['comparison']['seed'])
    assert set(previous['read_urls']) <= set(value['read_urls'])
    assert set(previous['supplement_keys']) < set(value['supplement_keys'])
    assert all(item in value['discoveries'] for item in previous['discoveries'])
    for origin in [root, *map(Path, dependencies)]:
        for path in origin.glob('step-*/environment-result.json'):
            record = read_record(path)['data']
            if record['action']['kind'] == 'supplement':
                assert supplement_key(record['action']) in value['supplement_keys']
                if 'link_result' in record:
                    assert record['link_result'] in value['discoveries']
    value['discoveries'][0]['proof']['pages'].clear()
    assert failed.load_repair_history(manifest)['discoveries'][0]['proof']['pages']


def test_nested_cache_rechecks_changed_bytes_active_and_claimed_ancestors(formula_failure, monkeypatch):
    _, plan, manifest, _, _ = formula_failure
    ancestor = Path(plan['goal']['comparison']['seed']['run'])
    target = ancestor / 'step-01/environment-result.json'
    real_hash = seed._hash
    with monkeypatch.context() as patch:
        patch.setattr(seed, '_hash', lambda path: '0' * 64 if Path(path) == target else real_hash(path))
        with pytest.raises(ComputerError, match='comparison_seed_changed'):
            seed.load_seed(manifest)
    from agent_service import run_lifecycle
    real_inactive = run_lifecycle.assert_inactive
    def inactive(root):
        if root == ancestor:
            raise ComputerError('run_owner_active')
        return real_inactive(root)
    with monkeypatch.context() as patch:
        patch.setattr(run_lifecycle, 'assert_inactive', inactive)
        with pytest.raises(ComputerError, match='run_owner_active'):
            seed.load_seed(manifest)
    exists = Path.exists
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'exists', lambda path: True if path == ancestor / 'continuation-claim.json' else exists(path))
        with pytest.raises(ComputerError, match='comparison_failed_seed_claimed'):
            seed.load_seed(manifest)
    assert seed.load_seed(manifest)


def test_nested_failed_office_dispatch_is_rejected_before_import(formula_failure, monkeypatch):
    _, plan, manifest, _, _ = formula_failure
    target = Path(plan['goal']['comparison']['seed']['run']) / 'outcome.json'
    real_read = failed.read_record
    def changed(path):
        value = real_read(path)
        if Path(path) == target:
            value['outputs'] = {'document': 'unapproved-office'}
        return value
    monkeypatch.setattr(failed, 'read_record', changed)
    with pytest.raises(ComputerError, match='comparison_failed_seed_invalid'):
        seed.load_seed(manifest)


@pytest.mark.parametrize('case', ['self', 'cycle', 'deeper', 'recovered-parent'])
def test_graph_rejects_cycles_and_deeper_failed_imports_before_load(tmp_path, monkeypatch, case):
    top, inner, research, deeper = (tmp_path / name for name in ('top.run', 'inner.run', 'research.run', 'deeper.run'))
    def goal(ancestor=None):
        return {'goal': {'comparison': {'evidence_version': 4,
                    **({'seed': ancestor} if ancestor else {})}}}
    ref = lambda root, version: {'run': str(root), 'version': version}
    top_plan = goal(ref(top if case == 'self' else inner, 4))
    inner_plan = goal(ref(top if case == 'cycle' else research, 4 if case == 'deeper' else 2))
    if case == 'recovered-parent':
        top_plan['recovery'] = {'parent_run': str(research)}
    plans = {top: top_plan, inner: inner_plan,
             research: goal(ref(deeper, 4)) if case == 'deeper' else {'goal': {}}}
    monkeypatch.setattr(seed, '_run_root', lambda path: Path(path))
    monkeypatch.setattr(failed, 'read_record', lambda path: plans[path.with_suffix('.run')])
    monkeypatch.setattr(failed, 'validate_manifest', lambda value: value)
    monkeypatch.setattr(failed, '_freeze', lambda root: {})
    from agent_service import comparison_research_seed as research_seed
    monkeypatch.setattr(research_seed, '_freeze', lambda root: {})
    with pytest.raises(ComputerError, match='comparison_failed_seed_invalid'):
        failed._dependencies(top_plan, top)


def test_graph_accepts_two_failed_layers_and_four_first_recovery_parents(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ('top.run', 'first.run', 'second.run', 'research.run')]
    parents = [tmp_path / f'parent{i}.run' for i in range(4)]
    plans = {}
    for index, root in enumerate(roots):
        comparison = ({'seed': {'version': 2 if index == 2 else 4, 'run': str(roots[index + 1])}}
                      if index < 3 else None)
        plans[root] = {'goal': {'comparison': comparison} if comparison else {},
                       'recovery': {'parent_run': str(parents[index])}}
        plans[parents[index]] = {'goal': {}}
    monkeypatch.setattr(seed, '_run_root', lambda path: Path(path))
    monkeypatch.setattr(seed, '_hash', lambda path: digest(str(path)))
    monkeypatch.setattr(seed, '_inventory', lambda path: {})
    monkeypatch.setattr(failed, 'read_record', lambda path: {} if path.name == 'outcome.json'
                        else plans[path.with_suffix('.run')])
    monkeypatch.setattr(failed, 'validate_manifest', lambda value: value)
    monkeypatch.setattr(failed, '_freeze', lambda root: {})
    from agent_service import comparison_research_seed, run_lifecycle
    monkeypatch.setattr(comparison_research_seed, '_freeze', lambda root: {})
    monkeypatch.setattr(run_lifecycle, 'assert_inactive', lambda root: None)
    snapshots = failed._dependencies(plans[roots[0]], roots[0])
    assert set(snapshots) == {str(root) for root in [*roots[1:], *parents]}


def test_history_unions_attempted_failures_pending_candidates_and_raw_proofs():
    old_action = {'kind': 'supplement', 'need_id': 'old', 'query': 'old', 'link_query': 'old'}
    new_action = {'kind': 'supplement', 'need_id': 'new', 'query': 'new', 'link_query': 'new'}
    old_proof = {'discovery': None, 'proof': {'pages': [{'body': 'historical HTML'}]}}
    new_proof = {'discovery': {'urls': ['https://example.test/new']}, 'proof': {'pages': []}}
    old_key = supplement_key(old_action)
    old_pending = {'id': 'old-candidate', 'url': 'https://example.test/old-pending',
                   'need_id': 'old', 'prior_need_key': old_key, 'query_index': 0}
    new_pending = {'id': 'new-candidate', 'url': 'https://example.test/new', 'need_id': 'new', 'query_index': 1}
    attempted = {'id': 'attempted', 'url': 'https://example.test/failed', 'query_index': 1}
    facts = {'queries': [1], 'reads': {'attempted': {'result': {'ok': False}}},
             'candidates': [new_pending, attempted],
             'records': [({'action': new_action, 'link_result': new_proof}, 'h1'),
                         ({'action': old_action, 'link_result': old_proof}, 'h2')],
             'repair_history': {'queries': [0], 'read_urls': ['https://example.test/old-failed'],
                 'candidates': [old_pending], 'supplement_keys': [old_key], 'discoveries': [old_proof]}}
    original = deepcopy(facts)
    result = history({'comparison': {}}, facts)
    assert result['queries'] == [0, 1]
    assert result['read_urls'] == ['https://example.test/failed', 'https://example.test/old-failed']
    assert result['supplement_keys'] == sorted([old_key, supplement_key(new_action)])
    assert result['candidates'] == [{**new_pending, 'prior_need_key': supplement_key(new_action)}, old_pending]
    assert result['discoveries'] == [old_proof, new_proof]
    result['discoveries'][0]['proof'].clear()
    assert facts == original


def test_history_without_proofs_preserves_original_shape_and_policies_are_exact():
    value = history({'comparison': {}}, {'queries': [], 'reads': {}, 'candidates': [], 'records': []})
    assert value == {'queries': [], 'read_urls': [], 'candidates': [], 'supplement_keys': []}
    assert enabled({'comparison': {'completion_policy': POLICY}})
    assert enabled({'comparison': {'completion_policy': DIMENSION_POLICY}})
    assert not enabled({'comparison': {'completion_policy': 'dimension_repair_v4'}})
    assert not enabled({'comparison': {'completion_policy': []}})
    assert not enabled({'comparison': {'completion_policy': {}}})
