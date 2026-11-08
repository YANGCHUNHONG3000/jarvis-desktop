"""Strict independent visual readback, with no real UI/input/OCR or cloud."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError, digest, write_new
from agent_service import windows_task as task
from agent_service import windows_visual as visual
from agent_service import windows_visual_recovery as recovery
from agent_service import windows_panel as panel


WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'offline-start', 'image': 'C:/wps.exe',
          'class_name': 'OpusApp', 'title': 'Offline workbook', 'profile': 'observe_only'}
PREFIX = {'id': 'first', 'window': WINDOW, 'selector': {'type': 'TreeItem', 'name': 'Items', 'automation_id': 'items'},
          'state': 'selected', 'value': True, 'retain': False}
TARGET = visual.condition(WINDOW, '视图', ['冻结窗格', '重排窗口'], number=2)


def current(met=False):
    words = [{'text': name, 'box': [x, 50, 28, 16]} for name, x in [('开始', 100), ('插入', 150), ('视图', 200)]]
    if met:
        words += [{'text': name, 'box': [x, 100, 60, 16]} for name, x in [('冻结窗格', 300), ('重排窗口', 400)]]
    frame = {'schema': 1, 'sha256': 'a' * 64, 'width': 900, 'height': 240, 'words': words}
    c, details = visual.presentation(frame, TARGET['selector'], TARGET['value'])
    details['rect'] = [0, 0, 900, 800]
    return {'window': deepcopy(WINDOW), 'application': 'wps', 'controls': [c], 'dialogs': [],
            'scope': 'visual_navigation', 'truncated': False, 'visual': details}


class SourceBackend:
    def __init__(self, code='windows_visual_postcondition_failed', native_files=True):
        self.code, self.native_files = code, native_files

    def observe_condition(self, condition, *, stop):
        if visual.is_goal(condition['selector']):
            return current(False)
        return {'window': deepcopy(WINDOW), 'controls': [{'id': 'items', **PREFIX['selector'],
                'visible': True, 'enabled': True, 'selected': True}], 'truncated': False}

    candidates = staticmethod(visual.candidates)

    def act(self, window, control, action, value, before_sha, operation_root, *, stop):
        result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': self.code,
                  'observation': current(False)}
        if self.native_files:
            request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                       'value': value, 'before_sha': before_sha, 'selector': TARGET['selector'], 'input_epoch': 99}
            write_new(operation_root / 'native-request.json', request)
            write_new(operation_root / 'native-intent.json', {'request_sha': digest(request), 'point': [214, 58]})
            write_new(operation_root / 'native-outcome.json', {**result, 'input_epoch': 101})
        return result


def failed_task(tmp_path, *, code='windows_visual_postcondition_failed', trailing=False, native_files=True):
    conditions = [deepcopy(PREFIX), deepcopy(TARGET)]
    if trailing:
        conditions.append({**deepcopy(PREFIX), 'id': 'third'})
    root = task.create(tmp_path / 'tasks', {'schema': 1, 'instruction': 'Offline visual navigation',
                       'conditions': conditions, 'max_steps': 12})
    result = task.run(root, SourceBackend(code, native_files))
    assert result['state'] == 'unknown' and result['completed'] == ['first']
    return root


class ReadOnlyBackend:
    def __init__(self, observation=None):
        self.observation = current(True) if observation is None else observation
        self.calls = []
        self.callback = None

    def observe_condition(self, condition, *, stop):
        self.calls.append(deepcopy(condition))
        if self.callback:
            self.callback()
        return deepcopy(self.observation)

    def act(self, *args, **kwargs):
        pytest.fail('Independent readback must never dispatch input')


def snapshot(root):
    return {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}


def test_only_current_last_page_is_verified_original_unknown_and_bytes_unchanged(tmp_path):
    original = failed_task(tmp_path)
    before = snapshot(original)
    backend = ReadOnlyBackend()
    result = recovery.run(original, backend, parent=tmp_path / 'rechecks')
    assert result['ok'] and result['code'] == 'windows_visual_current_page_verified'
    assert result['original_state'] == 'unknown' and result['actions'] == [] and not result['resumable']
    assert backend.calls == [TARGET]
    assert snapshot(original) == before
    assert task.inspect(original)['state'] == 'unknown'
    assert recovery.load(result['root'])['ok']
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(original, backend)


@pytest.mark.parametrize('code', ['user_takeover', 'windows_worker_interrupted', 'windows_backend_interrupted'])
def test_other_unknown_effects_cannot_be_rechecked(tmp_path, code):
    original = failed_task(tmp_path, code=code)
    backend = ReadOnlyBackend()
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.run(original, backend, parent=tmp_path / 'rechecks')
    assert not backend.calls and not (tmp_path / 'rechecks').exists()


def test_a_later_unstarted_goal_blocks_readback_qualification(tmp_path):
    original = failed_task(tmp_path, trailing=True)
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(original)


def test_missing_native_proof_does_not_inherit_generic_unknown_result(tmp_path):
    original = failed_task(tmp_path, native_files=False)
    with pytest.raises(ComputerError, match='ineligible'):
        recovery.inspect(original)


def test_old_unmet_frame_cannot_be_inherited_as_current_success(tmp_path):
    original = failed_task(tmp_path)
    before = snapshot(original)
    result = recovery.run(original, ReadOnlyBackend(current(False)), parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_visual_current_page_unverified'
    assert (tmp_path / 'rechecks').is_dir()
    assert snapshot(original) == before and not recovery.load(result['root'])['ok']


def test_claimed_control_success_is_recomputed_from_current_exact_ocr(tmp_path):
    original = failed_task(tmp_path)
    forged = current(False)
    forged['controls'][0]['value'] = TARGET['value']
    forged['visual']['verified'] = True
    result = recovery.run(original, ReadOnlyBackend(forged), parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_visual_recheck_observation_invalid'


def test_result_wrapper_cannot_substitute_for_observe_condition(tmp_path):
    original = failed_task(tmp_path)
    inherited = {'ok': True, 'state': 'verified', 'observation': current(True)}
    result = recovery.run(original, ReadOnlyBackend(inherited), parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_observation_invalid'


def test_changed_original_operation_rejects_before_fresh_observation(tmp_path):
    original = failed_task(tmp_path)
    (original / 'attempts/0001/operations/0001/native-outcome.json').write_text('{}')
    backend = ReadOnlyBackend()
    with pytest.raises(ComputerError, match='evidence_changed'):
        recovery.run(original, backend, parent=tmp_path / 'rechecks')
    assert backend.calls == []


def test_source_changes_during_observation_create_only_failed_separate_receipt(tmp_path):
    original = failed_task(tmp_path)
    backend = ReadOnlyBackend()
    backend.callback = lambda: (original / 'attempts/0001/operations/0001/native-outcome.json').write_text('{}')
    result = recovery.run(original, backend, parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_visual_recheck_source_changed'
    assert (recovery.safe_root(result['root']) / 'receipt.json').is_file()


def test_recheck_receipt_changed_or_new_evidence_invalidates_load(tmp_path):
    original = failed_task(tmp_path)
    result = recovery.run(original, ReadOnlyBackend(), parent=tmp_path / 'rechecks')
    folder = recovery.safe_root(result['root'])
    (folder / 'extra.json').write_text('{}')
    with pytest.raises(ComputerError, match='recheck_evidence_changed'):
        recovery.load(folder)


def test_rechecks_never_write_inside_original_task(tmp_path):
    original = failed_task(tmp_path)
    before = snapshot(original)
    with pytest.raises(ComputerError, match='destination_invalid'):
        recovery.run(original, ReadOnlyBackend(), parent=original / 'rechecks')
    assert snapshot(original) == before


def test_observation_error_still_has_an_immutable_failure_receipt(tmp_path):
    original = failed_task(tmp_path)
    backend = ReadOnlyBackend()
    def popup():
        raise ComputerError('windows_visual_popup_requires_attention')
    backend.callback = popup
    result = recovery.run(original, backend, parent=tmp_path / 'rechecks')
    assert not result['ok'] and result['code'] == 'windows_visual_popup_requires_attention'
    assert not recovery.load(result['root'])['ok']


def test_panel_readback_entry_does_not_route_to_normal_task_run(monkeypatch, tmp_path):
    calls = []
    sentinel = object()
    monkeypatch.setattr(recovery, 'run', lambda root, **kw: calls.append((root, kw)) or sentinel)
    p = SimpleNamespace(running=False, external_busy=lambda: False, task_root=tmp_path,
                        stopped=SimpleNamespace(is_set=lambda: False))
    p.start = lambda kind, operation: calls.append(kind) or operation(object())
    panel.WindowsPanel.recheck_visual(p)
    assert calls[0] == 'visual_recheck' and calls[1][0] == tmp_path
    assert 'backend' not in calls[1][1]


@pytest.mark.parametrize('forged', [False, True])
def test_default_production_backend_requires_matching_new_native_readback(monkeypatch, tmp_path, forged):
    from agent_service import windows_access as access
    original = failed_task(tmp_path)
    class Worker:
        def __init__(self, parent):
            self.parent = parent
        def observe_condition(self, condition, *, stop):
            operation = self.parent / ('observe-' + '0' * 32)
            operation.mkdir(parents=True)
            write_new(operation / 'native-request.json', {'op': 'observe', 'window': WINDOW,
                      'selector': TARGET['selector'], 'document_token': TARGET['value']})
            write_new(operation / 'native-outcome.json', {'ok': True, 'observation': current(not forged)})
            # The real worker crosses a JSON boundary before returning.
            return json.loads(json.dumps(current(True)))
    monkeypatch.setattr(access, 'WindowsBackend', Worker)
    result = recovery.run(original, parent=tmp_path / 'rechecks')
    assert result['ok'] is (not forged) and result['native_required'], result
    if forged:
        assert result['code'] == 'windows_visual_recheck_native_evidence_invalid'
    else:
        assert result['native_proof']['operation'].startswith('native/observe-')
        assert recovery.load(result['root'])['native_required']
