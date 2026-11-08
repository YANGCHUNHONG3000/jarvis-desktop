"""Copy lifecycle scope, exact close guards and durable restart contracts."""
from contextlib import nullcontext
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest

from agent_service import windows_access as access
from agent_service import windows_document_copy as copies
from agent_service import windows_document_lifecycle as lifecycle
from agent_service import windows_file_dialog as files
from agent_service import windows_input
from agent_service import windows_task as task
from agent_service.computer import ComputerError, digest, write_new


TOKEN = 'approved-exclusive-copy-token'
WINDOW = dict(hwnd=100, pid=10, created='start', image='C:/wps.exe', class_name='OpusApp',
              title='Document', profile='wps')


@pytest.fixture
def state(monkeypatch, tmp_path):
    s = SimpleNamespace(saved=False, active=True, clean=True, read_only=True, present=True,
        calls=[], effects=[], selector=deepcopy(lifecycle.SAVE_SELECTOR), dialogs=[], blocked=None,
        truncated=False, cancelled=False, mutation=None)
    s.manifest = {'source': {'path': str(tmp_path / 'source.docx')}}
    s.receipt = {'staging': {'path': str(tmp_path / 'exclusive' / 'staging.docx')},
                 'final': {'path': str(tmp_path / 'exclusive' / 'saved-document.docx')}}
    def decode(token):
        assert token == TOKEN
        return deepcopy(s.manifest)
    def receipt(token):
        assert token == TOKEN
        return deepcopy(s.receipt) if s.saved else None
    def status(window, path, check):
        check()
        assert path == (s.receipt['staging']['path'] if s.saved else s.manifest['source']['path'])
        s.calls.append(('status', path))
        return {'active': s.active, 'read_only': s.read_only if s.active else None,
                'saved': s.clean if s.active else None}
    def present(path, check, *, window):
        check()
        assert window == WINDOW
        assert path == s.receipt['staging']['path']
        s.calls.append(('present', path))
        return s.present
    def bound(window, path, check):
        current = status(window, path, check)
        if not current['active']:
            raise ComputerError('windows_document_not_active')
        s.calls.append(('bound', path))
        return SimpleNamespace(), current
    monkeypatch.setattr(copies, 'decode', decode)
    monkeypatch.setattr(copies, 'saved_receipt', receipt)
    monkeypatch.setattr(files, 'document_status', status)
    monkeypatch.setattr(files, 'document_present', present)
    monkeypatch.setattr(files, 'bound_document', bound)
    s.native = lambda: ({'window': deepcopy(WINDOW), 'application': 'wps', 'controls': [],
        'target_selector': deepcopy(s.selector), 'dialogs': deepcopy(s.dialogs), 'truncated': s.truncated,
        **({'blocked_reason': s.blocked} if s.blocked else {})}, {})
    s.observe = lambda: lifecycle.observe(WINDOW, TOKEN, s.selector, lambda: None, s.native)[0]
    return s


def test_save_candidates_require_active_source_and_dedicated_goal(state):
    observed = state.observe()
    target = {'selector': lifecycle.SAVE_SELECTOR, 'state': 'value', 'value': TOKEN}
    assert lifecycle.candidates(observed, target) == [
        {'control_id': None, 'action': 'save_document_copy', 'value': TOKEN}]
    assert not lifecycle.allowed(observed, None, 'close_saved_document', TOKEN)
    state.active = False
    assert lifecycle.candidates(state.observe(), target) == []


@pytest.mark.parametrize('changed', ['token', 'selector', 'control', 'dialogs', 'blocked', 'truncated'])
def test_lifecycle_permission_does_not_widen_existing_action_scope(state, changed):
    observed = state.observe()
    control = None
    if changed == 'token':
        observed['lifecycle_token'] = 'other'
    elif changed == 'selector':
        observed['target_selector'] = files.OPEN_DOCUMENT_SELECTOR
    elif changed == 'control':
        control = {'id': 'arbitrary'}
    elif changed == 'dialogs':
        observed['dialogs'] = [{'hwnd': 200}]
    elif changed == 'blocked':
        observed['blocked_reason'] = 'unknown popup'
    else:
        observed['truncated'] = True
    assert not access.allowed(observed, control, 'save_document_copy', TOKEN)


def test_saved_receipt_is_sufficient_to_recheck_save_without_active_tab_or_resave(state):
    state.saved, state.active = True, False
    observed = state.observe()
    assert observed['controls'][-1]['value'] == TOKEN
    assert not lifecycle.allowed(observed, None, 'save_document_copy', TOKEN)
    assert state.calls == [('status', state.receipt['staging']['path']),
                           ('present', state.receipt['staging']['path'])]


def test_close_goal_requires_exact_staging_absence_not_just_inactive(state):
    state.saved, state.active, state.selector = True, False, lifecycle.CLOSE_SELECTOR
    assert state.observe()['controls'][-1]['value'] == ''
    state.present = False
    observed = state.observe()
    assert observed['controls'][-1]['value'] == TOKEN
    assert lifecycle.verified('close_saved_document', observed, TOKEN)


@pytest.mark.parametrize('change', ['not_saved', 'inactive', 'dirty', 'not_open'])
def test_close_candidates_reject_unsaved_inactive_dirty_or_absent_copy(state, change):
    state.saved, state.selector = True, lifecycle.CLOSE_SELECTOR
    setattr(state, {'not_saved': 'saved', 'inactive': 'active', 'dirty': 'clean', 'not_open': 'present'}[change], False)
    assert not lifecycle.allowed(state.observe(), None, 'close_saved_document', TOKEN)


def test_preflight_close_binds_only_receipt_staging_and_rejects_dirty(state):
    state.saved = True
    lifecycle.preflight(WINDOW, TOKEN, 'close_saved_document', lambda: None)
    assert ('bound', state.receipt['staging']['path']) in state.calls
    assert all(path != state.receipt['final']['path'] for _, path in state.calls)
    state.clean = False
    with pytest.raises(ComputerError, match='windows_document_unsaved_changes'):
        lifecycle.preflight(WINDOW, TOKEN, 'close_saved_document', lambda: None)


def test_preflight_never_resaves_existing_receipt_and_requires_receipt_for_close(state):
    with pytest.raises(ComputerError, match='windows_copy_receipt_required'):
        lifecycle.preflight(WINDOW, TOKEN, 'close_saved_document', lambda: None)
    state.saved = True
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        lifecycle.preflight(WINDOW, TOKEN, 'save_document_copy', lambda: None)


def test_invalid_or_incomplete_receipt_stops_before_native_observation(monkeypatch, state):
    def incomplete(token):
        raise ComputerError('windows_copy_incomplete')
    monkeypatch.setattr(copies, 'saved_receipt', incomplete)
    native = []
    with pytest.raises(ComputerError, match='windows_copy_incomplete'):
        lifecycle.observe(WINDOW, TOKEN, lifecycle.SAVE_SELECTOR, lambda: None,
            lambda: native.append(True))
    assert not native and not state.calls


@pytest.fixture
def native(monkeypatch, state):
    s = state
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(GetForegroundWindow=lambda: WINDOW['hwnd']))
    monkeypatch.setattr(access.uia, '_native', nullcontext)
    monkeypatch.setattr(access.uia, '_recheck', lambda w: w)
    monkeypatch.setattr(access, '_request_observation', lambda request, check:
        lifecycle.observe(WINDOW, TOKEN, request['selector'], check, s.native))
    def save(window, token, check, operation):
        assert (operation / 'native-intent.json').is_file()
        s.effects.append('save')
        s.saved, s.read_only = True, False
        return s.receipt
    def close(hwnd, *, check, owns, should_stop):
        s.effects.append('guard_started')
        if s.mutation:
            s.mutation()
        # The real guard calls the supplied check after hooks start, before Ctrl+W.
        check()
        s.effects.append('ctrl_w')
        s.active, s.present = False, False
        return 7
    monkeypatch.setattr(copies, 'save_copy', save)
    monkeypatch.setattr(windows_input, 'send_close', close)
    s.request = lambda action: {'op': 'act', 'window': deepcopy(WINDOW), 'selector': deepcopy(s.selector),
        'document_token': TOKEN, 'control': None, 'action': action, 'value': TOKEN,
        'before_sha': digest(s.observe())}
    return s


def test_native_save_returns_verified_transaction_receipt(tmp_path, native):
    s = native
    result = access._perform(s.request('save_document_copy'), tmp_path, lambda: None, [0])
    assert result['ok'] and s.effects == ['save']
    assert result['state'] == result['effects'] == 'verified'
    assert result['copy_receipt_sha256'] == digest(s.receipt)


def test_native_close_sends_fixed_guard_and_requires_rot_absence(tmp_path, native):
    s = native
    s.saved, s.selector = True, lifecycle.CLOSE_SELECTOR
    epoch = [0]
    result = access._perform(s.request('close_saved_document'), tmp_path, lambda: None, epoch)
    assert result['ok'] and s.effects == ['guard_started', 'ctrl_w'] and epoch == [7]
    assert result['observation']['copy_state']['open'] is False


@pytest.mark.parametrize('mutation', ['dirty', 'inactive'])
def test_close_guard_rechecks_exact_active_clean_copy_before_injection(tmp_path, native, mutation):
    s = native
    s.saved, s.selector = True, lifecycle.CLOSE_SELECTOR
    s.mutation = lambda: setattr(s, 'clean' if mutation == 'dirty' else 'active', False)
    with pytest.raises(ComputerError, match='windows_document_unsaved_changes|windows_document_not_active'):
        access._perform(s.request('close_saved_document'), tmp_path, lambda: None, [0])
    assert s.effects == ['guard_started']


class LifecycleBackend:
    """Real engine and native dispatch; only COM/copy/input boundaries are fake."""
    def __init__(self, state):
        self.state = state
        self.selector = None

    def observe_condition(self, condition, *, stop):
        self.selector = deepcopy(condition['selector'])
        self.state.selector = deepcopy(self.selector)
        return lifecycle.observe(WINDOW, condition['value'], self.selector, stop, self.state.native)[0]

    candidates = staticmethod(access.candidates)

    def act(self, window, control, action, value, before_sha, operation, *, stop):
        request = {'op': 'act', 'window': window, 'selector': self.selector, 'document_token': value,
                   'control': control, 'action': action, 'value': value, 'before_sha': before_sha}
        write_new(operation / 'native-request.json', request)
        try:
            return access._perform(request, operation, stop, [0])
        except Exception as exc:
            return {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': str(exc)}


def lifecycle_task(tmp_path):
    conditions = [dict(id=id, window=deepcopy(WINDOW), selector=deepcopy(selector),
                       state='value', value=TOKEN, retain=True)
                  for id, selector in [('save', lifecycle.SAVE_SELECTOR), ('close', lifecycle.CLOSE_SELECTOR)]]
    return task.create(tmp_path, dict(schema=1, instruction='Save a new copy then close that saved copy',
                                     conditions=conditions, max_steps=4))


def test_engine_save_pause_restart_closes_only_saved_copy_without_resaving(tmp_path, native):
    s = native
    root = lifecycle_task(tmp_path)
    first = task.run(root, LifecycleBackend(s), pause_after_step=True)
    assert first['state'] == 'paused' and first['completed'] == ['save']
    assert s.effects == ['save']
    old = {p: p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    assert task.run(root, LifecycleBackend(s))['ok']
    assert s.effects == ['save', 'guard_started', 'ctrl_w']
    assert all(p.read_bytes() == data for p, data in old.items())
    assert task.run(root, LifecycleBackend(s))['ok']
    assert s.effects.count('save') == s.effects.count('ctrl_w') == 1


@pytest.mark.parametrize('change', ['dirty', 'inactive'])
def test_engine_resume_after_save_refuses_modified_or_inactive_copy(tmp_path, native, change):
    s = native
    root = lifecycle_task(tmp_path)
    assert task.run(root, LifecycleBackend(s), pause_after_step=True)['completed'] == ['save']
    setattr(s, 'clean' if change == 'dirty' else 'active', False)
    result = task.run(root, LifecycleBackend(s))
    assert result['state'] == 'failed' and result['completed'] == ['save']
    assert s.effects == ['save']


def test_unknown_close_after_chord_never_replays_chord_or_save(monkeypatch, tmp_path, native):
    s = native
    root = lifecycle_task(tmp_path)
    assert task.run(root, LifecycleBackend(s), pause_after_step=True)['completed'] == ['save']
    def interrupted(hwnd, *, check, owns, should_stop):
        check()
        s.effects.append('ctrl_w_unknown')
        raise ComputerError('windows_input_partial')
    monkeypatch.setattr(windows_input, 'send_close', interrupted)
    result = task.run(root, LifecycleBackend(s))
    assert result['state'] == 'unknown' and result['completed'] == ['save']
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, LifecycleBackend(s))
    assert s.effects == ['save', 'ctrl_w_unknown']


def test_close_without_proven_rot_absence_does_not_reinject_chord(monkeypatch, tmp_path, native):
    s = native
    s.saved, s.selector = True, lifecycle.CLOSE_SELECTOR
    def no_close(hwnd, *, check, owns, should_stop):
        check()
        s.effects.append('ctrl_w')
        # The key delivery succeeded, but the exact document is still in ROT.
        return 7
    monkeypatch.setattr(windows_input, 'send_close', no_close)
    ticks = iter([0, 0, 4, 8])
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(access.time, 'sleep', lambda seconds: None)
    result = access._perform(s.request('close_saved_document'), tmp_path, lambda: None, [0])
    assert result['state'] == 'unknown' and result['effects'] == 'unknown'
    assert s.effects == ['ctrl_w']


def test_missing_window_never_becomes_success_from_saved_receipt_or_rot_absence(state):
    state.saved, state.present, state.selector = True, False, lifecycle.CLOSE_SELECTOR
    def missing():
        raise ComputerError('window_not_found')
    state.native = missing
    with pytest.raises(ComputerError, match='window_not_found'):
        state.observe()
    assert not state.calls


def test_window_disappearing_after_close_remains_unknown_without_replay(tmp_path, native):
    s = native
    root = lifecycle_task(tmp_path)
    assert task.run(root, LifecycleBackend(s), pause_after_step=True)['completed'] == ['save']
    before = s.native
    def disappearing():
        if not s.present:
            raise ComputerError('window_not_found')
        return before()
    s.native = disappearing
    result = task.run(root, LifecycleBackend(s))
    assert result['state'] == 'unknown' and result['completed'] == ['save']
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, LifecycleBackend(s))
    assert s.effects == ['save', 'guard_started', 'ctrl_w']


def test_successful_save_receipt_is_not_downgraded_by_later_uia_failure(monkeypatch, tmp_path, native):
    s = native
    observations = []
    request_observation = access._request_observation
    def fail_after_save(request, check):
        observations.append(True)
        if s.saved:
            raise ComputerError('windows_application_blocked')
        return request_observation(request, check)
    monkeypatch.setattr(access, '_request_observation', fail_after_save)
    result = access._perform(s.request('save_document_copy'), tmp_path, lambda: None, [0])
    assert result['ok'] and result['state'] == result['effects'] == 'verified'
    assert result['copy_receipt_sha256'] == digest(s.receipt)
    assert observations == [True, True] and s.effects == ['save']


def test_blocked_uia_after_saved_receipt_recovers_by_recheck_then_close_without_resave(
        monkeypatch, tmp_path, native):
    s = native
    root = lifecycle_task(tmp_path)
    original_save = copies.save_copy
    def save_then_block(window, token, check, operation):
        receipt = original_save(window, token, check, operation)
        s.blocked = 'unrecognized_Qt5QWindowToolSaveBits'
        return receipt
    monkeypatch.setattr(copies, 'save_copy', save_then_block)
    first = task.run(root, LifecycleBackend(s))
    assert first['state'] == 'failed' and first['code'] == 'windows_application_blocked'
    assert first['completed'] == [] and first['resumable']
    assert s.effects == ['save']
    original_evidence = {p: p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    results = [json.loads(p.read_text())['payload']['result'] for p in
        (root / 'attempts' / '0001' / 'events').iterdir()
        if json.loads(p.read_text())['payload']['kind'] == 'result']
    assert len(results) == 1 and results[0]['state'] == results[0]['effects'] == 'verified'
    s.blocked = None
    assert task.run(root, LifecycleBackend(s))['ok']
    assert s.effects == ['save', 'guard_started', 'ctrl_w']
    assert all(p.read_bytes() == data for p, data in original_evidence.items())


def test_receipt_found_after_unknown_save_does_not_rewrite_or_unlock_old_unknown(
        monkeypatch, tmp_path, native):
    s = native
    root = lifecycle_task(tmp_path)
    original_save = copies.save_copy
    def uncertain_return(window, token, check, operation):
        original_save(window, token, check, operation)
        raise copies.CopyError('windows_copy_save_unknown', no_external_effect=False)
    monkeypatch.setattr(copies, 'save_copy', uncertain_return)
    result = task.run(root, LifecycleBackend(s))
    assert result['state'] == 'unknown' and s.saved is True
    outcome = root / 'attempts' / '0001' / 'outcome.json'
    old = outcome.read_bytes()
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, LifecycleBackend(s))
    assert outcome.read_bytes() == old and s.effects == ['save']
