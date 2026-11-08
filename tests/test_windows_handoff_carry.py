"""Saved copy provenance survives completed open-only tasks without replay."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import windows_handoff as handoff, windows_task as wt
from agent_service import windows_file_dialog as files
from agent_service.computer import ComputerError, digest
from test_windows_handoff import saved, prepared, native, child_goal  # noqa: F401
from test_windows_task import Backend, condition, control, goal


def complete_open(parent, source_id, final, directory, *, readonly=False):
    target = child_goal(final, readonly=readonly)['conditions'][0]
    ref = handoff.capture(parent, source_id, target_id=target['id'])
    root = wt.create(directory, goal(target), sources=[ref])
    backend = Backend(control(**target['selector'], value=target['value']))
    assert wt.run(root, backend)['ok'] and not backend.calls
    return root, ref, backend


@pytest.mark.parametrize('readonly', [False, True])
def test_two_completed_generations_preserve_exact_original_binding(saved, tmp_path, readonly):
    original, _, receipt = saved
    before = wt._inventory(original)
    first, original_ref, backend = complete_open(original, 'save', receipt['final'], tmp_path/'one', readonly=readonly)
    first_before = wt._inventory(first)
    second, carried, second_backend = complete_open(first, 'open', receipt['final'], tmp_path/'two')
    assert carried == handoff.capture(first, 'open', target_id='open')
    assert carried['kind'] == 'carried' and carried['source_id'] == 'open'
    assert carried['source_sha256'] == digest(original_ref)
    assert carried['final'] == original_ref['final'] == receipt['final']
    ref = handoff.capture(second, 'open', target_id='open')
    assert ref['source_sha256'] == digest(carried)
    handoff.verify(goal(child_goal(receipt['final'])['conditions'][0]), [ref])
    assert wt.load(second)['sources'] == [carried]
    assert wt._inventory(original) == before and wt._inventory(first) == first_before
    assert not backend.calls and not second_backend.calls


@pytest.mark.parametrize('state', ['ready', 'paused', 'failed', 'unknown'])
def test_incomplete_or_unknown_intermediate_cannot_carry_or_replay(saved, tmp_path, state):
    original, _, receipt = saved
    target = child_goal(receipt['final'])['conditions'][0]
    ref = handoff.capture(original, 'save', target_id='open')
    root = wt.create(tmp_path/'next', goal(target, condition('pending')), sources=[ref])
    backend = Backend(control(**target['selector'], value=target['value']), control('pending'))
    if state in ('failed', 'unknown'):
        backend.result = {'ok': False, 'state': state, 'effects': 'none' if state == 'failed' else 'unknown'}
    if state != 'ready':
        assert wt.run(root, backend, pause_after_step=state == 'paused')['state'] == state
    before, calls, observations = wt._inventory(root), len(backend.calls), backend.observations
    expected = 'windows_handoff_source_unknown' if state == 'unknown' else 'windows_handoff_source_incomplete'
    with pytest.raises(ComputerError, match=expected):
        handoff.capture(root, 'open', target_id='next')
    assert wt._inventory(root) == before and len(backend.calls) == calls and backend.observations == observations


@pytest.mark.parametrize('ancestor', [False, True])
def test_changed_intermediate_or_ancestor_evidence_is_never_renewed(saved, tmp_path, ancestor):
    original, _, receipt = saved
    first, _, _ = complete_open(original, 'save', receipt['final'], tmp_path/'one')
    carried = handoff.capture(first, 'open', target_id='open')
    changed = original if ancestor else first
    (changed/'extra.json').write_text('{}')
    with pytest.raises(ComputerError, match='windows_handoff_source_changed'):
        handoff.verify(goal(child_goal(receipt['final'])['conditions'][0]), [carried])
    if ancestor:
        with pytest.raises(ComputerError, match='windows_handoff_source_changed'):
            handoff.capture(first, 'open', target_id='open')


def test_carried_task_load_and_execution_fail_before_observation_when_file_changes(saved, tmp_path):
    original, _, receipt = saved
    first, _, _ = complete_open(original, 'save', receipt['final'], tmp_path/'one')
    target = child_goal(receipt['final'])['conditions'][0]
    carried = handoff.capture(first, 'open', target_id='open')
    pending = wt.create(tmp_path/'pending', goal(target), sources=[carried])
    path = Path(receipt['final']['path'])
    path.write_bytes(path.read_bytes()+b'changed')
    backend = Backend()
    with pytest.raises(ComputerError):
        wt.load(pending)
    with pytest.raises(ComputerError):
        wt.run(pending, backend)
    assert backend.observations == 0 and not backend.calls
    assert not list((pending/'attempts').iterdir())


@pytest.mark.parametrize('field', ['parent_root', 'task_sha256', 'evidence_sha256', 'source_id',
                                  'source_sha256', 'token_sha256', 'final'])
def test_swapped_carried_binding_is_rejected(saved, tmp_path, field):
    original, _, receipt = saved
    first, _, _ = complete_open(original, 'save', receipt['final'], tmp_path/'one')
    carried = handoff.capture(first, 'open', target_id='open')
    if field == 'parent_root':
        carried[field] = str(original)
    elif field == 'source_id':
        carried[field] = 'missing'
    elif field == 'final':
        carried[field]['sha256'] = '0'*64
    else:
        carried[field] = '0'*64
    with pytest.raises(ComputerError):
        handoff.verify(goal(child_goal(receipt['final'])['conditions'][0]), [carried])


@pytest.mark.parametrize('bound', [False, True])
def test_unbound_open_cannot_gain_saved_copy_authority(saved, tmp_path, bound):
    original, _, receipt = saved
    target = child_goal(receipt['final'])['conditions'][0]
    other = deepcopy(target)
    other['id'] = 'other'
    refs = [handoff.capture(original, 'save', target_id='other')] if bound else None
    root = wt.create(tmp_path/'next', goal(target, other), sources=refs)
    assert wt.run(root, Backend(control(**target['selector'], value=target['value'])))['ok']
    with pytest.raises(ComputerError, match='windows_handoff_source_required' if bound else 'windows_handoff_save_required'):
        handoff.capture(root, 'open', target_id='next')


def test_carry_validation_is_pure_and_exact(saved, tmp_path, monkeypatch):
    original, _, receipt = saved
    first, _, _ = complete_open(original, 'save', receipt['final'], tmp_path/'one')
    carried = handoff.capture(first, 'open', target_id='open')
    target_goal = goal(child_goal(receipt['final'])['conditions'][0])
    monkeypatch.setattr(handoff, 'capture', lambda *a, **k: pytest.fail('shape read evidence'))
    monkeypatch.setattr(files, 'decode_token', lambda *a: pytest.fail('shape read file'))
    handoff.validate(target_goal, [carried])
    for changed in (dict(carried, kind='other'), dict(carried, source_id='../open'),
                    dict(carried, source_sha256=True), dict(carried, extra=1),
                    {k: v for k, v in carried.items() if k != 'kind'}):
        with pytest.raises(ComputerError, match='windows_handoff_reference_invalid'):
            handoff.validate(target_goal, [changed])


def test_actual_carry_depth_counts_all_roots(saved, tmp_path):
    root, _, receipt = saved
    source_id = 'save'
    for index in range(4):
        root, _, _ = complete_open(root, source_id, receipt['final'], tmp_path/str(index))
        source_id = 'open'
    with pytest.raises(ComputerError, match='windows_handoff_depth_limit'):
        handoff.capture(root, 'open', target_id='next')
    assert handoff._STACK.get() == ()


def test_carried_cycle_stops_before_second_lock(saved, tmp_path, monkeypatch):
    original, _, receipt = saved
    first, _, _ = complete_open(original, 'save', receipt['final'], tmp_path/'one')
    carried = handoff.capture(first, 'open', target_id='open')
    scan = wt._scan(first)
    scan['task']['sources'] = [carried]
    locks = []
    monkeypatch.setattr(wt, '_own', lambda path: (locks.append(path), nullcontext())[1])
    monkeypatch.setattr(wt, '_scan', lambda path: scan)
    with pytest.raises(ComputerError, match='windows_handoff_cycle'):
        handoff.capture(first, 'open', target_id='next')
    assert locks == [first] and handoff._STACK.get() == ()
