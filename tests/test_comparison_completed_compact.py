"""Completed compact-source tasks re-enter history without replaying effects."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import comparison_compact_seed as compact
from agent_service import comparison_seed as seed, comparison_history as history
from agent_service import computer, computer_agent as engine
from agent_service.computer import ComputerError, read_record
from test_comparison_failed_seed import failed, select_model, snapshot
from test_comparison_loop import ctx, run
from test_computer_agent_tools import ctx as tool_ctx
from test_computer_agent import Client


@pytest.fixture
def completed_compact(failed):
    c, ancestor, old_plan, old_outcome = failed
    manifest = compact.create_manifest(ancestor)
    c['session'] = computer.ComputerSession.create(computer.BASE)
    c['client'] = Client(select_model)
    goal = deepcopy(old_plan['goal'])
    goal['comparison'].update(evidence_version=4, seed=manifest,
        completion_policy='dimension_first_v4', dimension_routing=6)
    plan = engine.prepare(c['session'], 'complete retained captures', goal)
    outcome = run(c, plan)
    assert outcome['ok'], outcome
    assert [r['action']['kind'] for r in outcome['attempts']] == [
        'compare', 'spreadsheet', 'document']
    assert read_record(ancestor / 'outcome.json') == old_outcome
    return c, Path(outcome['run']), ancestor, outcome


def test_completed_compact_history_selection_and_preview_have_no_effects(completed_compact):
    c, root, ancestor, outcome = completed_compact
    before = snapshot(root), snapshot(ancestor)
    effects = len(c['client'].calls), list(c['browser'].calls), list(c['search'].calls), len(c['effects'])
    selection = history.select_source(root)
    assert selection['manifest']['version'] == 1
    assert len(selection['pages']) == 4
    assert 'seed' not in selection['goal']['comparison']
    assert 'recovery' not in selection['goal']
    session = computer.ComputerSession.create(computer.BASE)
    plan = history.prepare_selected(session, 'fresh semantic review', selection['goal'], selection)
    assert plan['goal']['queries'] == selection['manifest']['queries']
    assert plan['goal']['comparison']['seed'] == selection['manifest']
    assert before == (snapshot(root), snapshot(ancestor))
    assert effects == (len(c['client'].calls), c['browser'].calls, c['search'].calls, len(c['effects']))
    assert read_record(root / 'outcome.json') == outcome
    assert not (root / 'continuation-claim.json').exists()


@pytest.mark.parametrize('target', ['ancestor_capture', 'ancestor_outcome', 'native_output', 'child_result'])
def test_warm_completed_import_rechecks_ancestor_and_native_bytes(completed_compact, target):
    _, root, ancestor, outcome = completed_compact
    manifest = seed.create_manifest(root)
    expected = seed.load_seed(manifest)
    paths = {
        'ancestor_capture': next(p for p in ancestor.glob('step-*/environment-result.json')
            if read_record(p)['data']['action']['kind'] == 'read'),
        'ancestor_outcome': ancestor / 'outcome.json',
        'native_output': Path(outcome['artifacts'][0]['path']),
        'child_result': root / 'step-01/environment-result.json',
    }
    path = paths[target]
    original = path.read_bytes()
    try:
        path.write_bytes(original + b' ')
        with pytest.raises(ComputerError):
            seed.load_seed(manifest)
    finally:
        path.write_bytes(original)
    assert seed.load_seed(manifest) == expected


def test_successful_wrapper_does_not_allow_arbitrary_success_chains(completed_compact):
    c, root, _, _ = completed_compact
    selection = history.select_source(root)
    c['session'] = computer.ComputerSession.create(computer.BASE)
    plan = history.prepare_selected(c['session'], 'one more reviewed task', selection['goal'], selection)
    outcome = run(c, plan)
    assert outcome['ok'], outcome
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.create_manifest(outcome['run'])


@pytest.mark.parametrize('case', ['self', 'in_graph', 'ancestor_success', 'ancestor_zero', 'ancestor_missing'])
def test_compact_parent_guard_precedes_any_recursive_loader(monkeypatch, tmp_path, case):
    from agent_service import research_recovery
    root, ancestor = tmp_path/'child.run', tmp_path/'parent.run'
    parent = {'version': 5, 'run': str(root if case == 'self' else ancestor),
              'dependency_graph': {'nodes': [{'snapshot': {
                  'run': str(root if case == 'in_graph' else ancestor)}}]}}
    # Isolate only the topology gate; graph shape/replay has its own full tests.
    monkeypatch.setattr(research_recovery, '_plan', lambda *a: {'goal': {
        'comparison': {'evidence_version': 4, 'seed': parent}}})
    monkeypatch.setattr(seed, '_run_root', Path)
    monkeypatch.setattr(seed, 'validate_manifest', deepcopy)
    outcomes = {'ancestor_success': {'ok': True}, 'ancestor_zero': {'ok': 0},
                'ancestor_missing': {}}
    monkeypatch.setattr(seed, 'read_record', lambda path: outcomes.get(case, {'ok': False})
        if path.name == 'outcome.json' else {})
    monkeypatch.setattr(seed, 'load_seed', lambda *a: pytest.fail('must reject before recursive loading'))
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed._verified(root, {})
