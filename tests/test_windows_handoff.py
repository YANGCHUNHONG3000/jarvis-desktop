"""Saved copy bindings exercise real task evidence and passive Office packages."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service import windows_handoff as handoff
from agent_service import windows_task as wt
from agent_service import windows_document_copy as copies
from agent_service import windows_file_dialog as files
from agent_service.computer import ComputerError, digest
from agent_service.windows_document_lifecycle import SAVE_SELECTOR
from agent_service.windows_file_actions import READONLY_SELECTOR
from test_windows_document_copy import prepared, native  # noqa: F401
from test_windows_task import Backend, WINDOW, condition, control, goal


def save_target(token):
    return {'id': 'save', 'window': deepcopy(WINDOW), 'selector': deepcopy(SAVE_SELECTOR),
            'state': 'value', 'value': token, 'retain': True}


def save_control(token):
    return control(**SAVE_SELECTOR, value=token)


def child_goal(snapshot, *, readonly=False):
    target = save_target(files.encode_token(snapshot))
    target['id'] = 'open'
    target['selector'] = deepcopy(READONLY_SELECTOR if readonly else files.OPEN_DOCUMENT_SELECTOR)
    return goal(target, condition('Other_application'))


@pytest.fixture
def saved(prepared, native, tmp_path):
    _, token, _ = prepared
    _, window, operation = native
    receipt = copies.save_copy(window, token, lambda: None, operation)
    root = wt.create(tmp_path / 'tasks', goal(save_target(token)))
    assert wt.run(root, Backend(save_control(token)))['ok']
    return root, token, receipt


@pytest.mark.parametrize('readonly', [False, True])
def test_capture_and_verify_keep_legacy_goal_and_evidence_unchanged(saved, readonly):
    root, token, receipt = saved
    before = wt._inventory(root)
    with wt._own(root):
        assert wt._inventory(root) == before
    child = child_goal(receipt['final'], readonly=readonly)
    original = deepcopy(child)
    ref = handoff.capture(root, 'save', target_id='open')
    assert ref['receipt_sha256'] == digest(receipt)
    assert ref['final'] == receipt['final']
    handoff.verify(child, [ref])
    assert wt._inventory(root) == before and child == original
    assert set(child) == {'schema', 'instruction', 'conditions', 'max_steps'}


@pytest.mark.parametrize('field', ['parent_root', 'task_sha256', 'evidence_sha256',
                                  'save_id', 'token_sha256', 'receipt_sha256', 'final'])
def test_swapped_source_or_receipt_is_rejected(saved, field, tmp_path):
    root, _, receipt = saved
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    if field == 'parent_root':
        other = wt.create(tmp_path / 'other', goal())
        ref[field] = str(other)
    elif field == 'save_id':
        ref[field] = 'wrong'
    elif field == 'final':
        ref[field]['sha256'] = '0' * 64
    else:
        ref[field] = '0' * 64
    with pytest.raises(ComputerError):
        handoff.verify(child, [ref])


def test_opening_original_instead_of_saved_copy_is_rejected(saved, prepared):
    root, _, receipt = saved
    source, _, _ = prepared
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    child['conditions'][0]['value'] = files.encode_token(source)
    with pytest.raises(ComputerError, match='windows_handoff_target_changed'):
        handoff.verify(child, [ref])


def test_changed_saved_bytes_are_rejected(saved):
    root, _, receipt = saved
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    with Path(receipt['final']['path']).open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ComputerError):
        handoff.verify(child, [ref])


def test_new_parent_evidence_invalidates_binding(saved):
    root, _, receipt = saved
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    (root / 'unexpected.json').write_text('{}')
    with pytest.raises(ComputerError, match='windows_handoff_source_changed'):
        handoff.verify(child, [ref])


@pytest.mark.parametrize('partial', [False, True])
def test_missing_or_half_saved_receipt_never_becomes_source(prepared, tmp_path, partial):
    _, token, manifest = prepared
    root = wt.create(tmp_path / 'tasks', goal(save_target(token)))
    assert wt.run(root, Backend(save_control(token)))['ok']
    if partial:
        (Path(manifest['directory']) / 'save-started.json').write_text('{}')
    with pytest.raises(ComputerError, match='windows_copy_incomplete' if partial else 'windows_handoff_receipt_required'):
        handoff.capture(root, 'save', target_id='open')


def test_uncompleted_save_is_rejected(saved, tmp_path):
    _, token, _ = saved
    root = wt.create(tmp_path / 'tasks', goal(save_target(token)))
    with pytest.raises(ComputerError, match='windows_handoff_save_required'):
        handoff.capture(root, 'save', target_id='open')


def test_unknown_parent_cannot_be_bypassed_and_does_not_replay(saved, tmp_path):
    _, token, _ = saved
    root = wt.create(tmp_path / 'tasks', goal(save_target(token), condition('pending')))
    backend = Backend(save_control(token), control('pending'))
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    result = wt.run(root, backend)
    assert result['state'] == 'unknown' and result['completed'] == ['save']
    before = wt._inventory(root)
    with pytest.raises(ComputerError, match='windows_handoff_source_unknown'):
        handoff.capture(root, 'save', target_id='open')
    assert len(backend.calls) == 1 and wt._inventory(root) == before


def test_completed_save_in_paused_parent_is_usable(saved, tmp_path):
    _, token, receipt = saved
    root = wt.create(tmp_path / 'tasks', goal(save_target(token), condition('pending')))
    backend = Backend(save_control(token), control('pending'))
    result = wt.run(root, backend, pause_after_step=True)
    assert result['state'] == 'paused' and result['completed'] == ['save']
    ref = handoff.capture(root, 'save', target_id='open')
    handoff.verify(child_goal(receipt['final']), [ref])
    assert backend.calls == []


def test_shape_checks_are_pure_and_reject_duplicate_or_wrong_targets(saved, monkeypatch):
    root, _, receipt = saved
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    monkeypatch.setattr(handoff, 'capture', lambda *a, **k: pytest.fail('shape read evidence'))
    monkeypatch.setattr(files, 'decode_token', lambda *a: pytest.fail('shape read file'))
    handoff.validate(child, [ref])
    handoff.verify(child, None)
    for sources in ([], [ref] * 13, [ref, ref], [dict(ref, target_id='other')],
                    [dict(ref, save_id='../save')], [dict(ref, parent_root='relative')],
                    [dict(ref, parent_root=r'\\server\share\task')],
                    [dict(ref, final=dict(ref['final'], path=r'\\server\share\saved.xlsx'))],
                    [dict(ref, final=dict(ref['final'], size=True))], [dict(ref, extra=1)]):
        with pytest.raises(ComputerError):
            handoff.validate(child, sources)
    changed = deepcopy(child)
    changed['conditions'][0]['selector'] = deepcopy(SAVE_SELECTOR)
    with pytest.raises(ComputerError, match='windows_handoff_target_invalid'):
        handoff.validate(changed, [ref])


@pytest.mark.parametrize('length,cycle,error', [(1, True, 'windows_handoff_cycle'),
                                              (4, False, 'windows_handoff_depth_limit')])
def test_recursive_parent_cycle_and_depth_stop_before_lock(saved, tmp_path, monkeypatch, length, cycle, error):
    root, _, receipt = saved
    child = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    paths = [root] + [tmp_path / f'ancestor-{i}' for i in range(1, length + 1)]
    scans = {}
    original_scan = wt._scan(root)
    for index in range(length):
        next_path = root if cycle else paths[index + 1]
        scans[paths[index]] = {**deepcopy(original_scan), 'goal': child,
                             'task': {'schema': 2, 'sources': [dict(ref, parent_root=str(next_path))]}}
    locks = []
    def own(path):
        locks.append(path)
        return nullcontext()
    monkeypatch.setattr(wt, '_own', own)
    monkeypatch.setattr(wt, '_scan', lambda path: scans[path])
    with pytest.raises(ComputerError, match=error):
        handoff.capture(root, 'save', target_id='open')
    assert len(locks) == length
    assert handoff._STACK.get() == ()
