"""Real task/policy/intent/panel boundaries with offline native substitutes."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError, digest
from agent_service import windows_access as access
from agent_service import windows_intent as intent
from agent_service import windows_panel as panel
from agent_service import windows_task as task


WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'offline-start',
          'image': 'C:/Windows/explorer.exe', 'class_name': 'CabinetWClass',
          'title': 'Offline navigation', 'profile': 'explorer'}


def checkbox(name='显示扩展名', checked=0, **updates):
    return {'id': name, 'type': 'CheckBox', 'name': name, 'automation_id': name,
            'patterns': ['toggle'], 'checked': checked, 'enabled': True,
            'visible': True, 'value': None, 'selected': None, 'expanded': None,
            'focused': False, 'ancestors': [], **updates}


def condition(c, value, *, number=1, retain=True):
    return {'id': f'target_{number}', 'window': deepcopy(WINDOW),
            'selector': {k: c[k] for k in ('type', 'name', 'automation_id')},
            'state': 'checked', 'value': value, 'retain': retain}


def goal(*conditions):
    return {'schema': 1, 'instruction': '切换明确选择的导航显示状态',
            'conditions': list(conditions), 'max_steps': 12}


def observation(*controls, **updates):
    return {'window': deepcopy(WINDOW), 'application': 'explorer',
            'scope': 'visible_controls', 'controls': list(controls),
            'truncated': False, 'dialogs': [], **updates}


class OfflineBackend:
    """Use actual production candidates; substitute only the native boundary."""
    def __init__(self, *controls):
        self.controls = deepcopy(list(controls))
        self.calls, self.observations = [], 0
        self.blocked, self.unknown = False, False
        self.interrupted = False

    def observe(self, window, *, stop):
        self.observations += 1
        assert not stop()
        return observation(*deepcopy(self.controls), window=deepcopy(window),
                           **({'blocked_reason': 'unknown_owned_popup'} if self.blocked else {}))

    candidates = staticmethod(access.candidates)

    def act(self, window, control, action, value, before_sha, operation_root, *, stop):
        assert before_sha == digest(self.observe(window, stop=stop))
        assert access.allowed(self.observe(window, stop=stop), control, action, value)
        self.calls.append((control['id'], action, value))
        (operation_root / 'native-intent.json').write_text(json.dumps({'action': action}), encoding='utf-8')
        if self.interrupted:
            raise RuntimeError('offline interruption after durable intent')
        if self.unknown:
            return {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_postcondition_failed'}
        assert action == 'toggle'
        actual = next(c for c in self.controls if c['id'] == control['id'])
        actual['checked'] = int(value)
        (operation_root / 'receipt.json').write_text(json.dumps({'checked': actual['checked']}), encoding='utf-8')
        return {'ok': True, 'state': 'verified', 'effects': 'verified'}


def frozen_files(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def assert_unchanged(root, previous):
    assert all((root / relative).read_bytes() == content for relative, content in previous.items())


def test_checked_pause_restart_runs_only_remaining_target_and_preserves_attempt(tmp_path):
    first, second = checkbox(), checkbox('显示详细信息', 1)
    root = task.create(tmp_path, goal(condition(first, True), condition(second, False, number=2)))
    backend = OfflineBackend(first, second)
    paused = task.run(root, backend, pause_after_step=True)
    assert paused['state'] == 'paused' and paused['completed'] == ['target_1']
    original = frozen_files(root / 'attempts' / '0001')
    # A new backend models process restart; state comes from fresh native readback.
    restarted = OfflineBackend(*backend.controls)
    completed = task.run(root, restarted)
    assert completed['ok'] and completed['completed'] == ['target_1', 'target_2']
    assert backend.calls == [(first['id'], 'toggle', '1')]
    assert restarted.calls == [(second['id'], 'toggle', '0')]
    assert_unchanged(root / 'attempts' / '0001', original)
    assert task.inspect(root)['resumable'] is False


@pytest.mark.parametrize('initial,target', [(0, False), (1, True)])
def test_already_satisfied_checkbox_is_verified_without_toggle(tmp_path, initial, target):
    c = checkbox(checked=initial)
    root = task.create(tmp_path, goal(condition(c, target)))
    backend = OfflineBackend(c)
    assert task.run(root, backend)['ok']
    assert backend.calls == [] and not list(root.rglob('native-intent.json'))


def test_unknown_popup_before_dispatch_has_no_intent_and_can_resume(tmp_path):
    c = checkbox()
    root = task.create(tmp_path, goal(condition(c, True)))
    backend = OfflineBackend(c)
    backend.blocked = True
    failed = task.run(root, backend)
    assert failed['state'] == 'failed' and failed['code'] == 'windows_application_blocked'
    assert backend.calls == [] and not list(root.rglob('native-intent.json'))
    assert task.inspect(root)['resumable']
    original = frozen_files(root / 'attempts' / '0001')
    backend.blocked = False
    assert task.run(root, backend)['ok']
    assert backend.calls == [(c['id'], 'toggle', '1')]
    assert_unchanged(root / 'attempts' / '0001', original)


def test_popup_on_resume_does_not_replay_completed_checkbox(tmp_path):
    first, second = checkbox(), checkbox('显示详细信息')
    root = task.create(tmp_path, goal(condition(first, True), condition(second, True, number=2)))
    backend = OfflineBackend(first, second)
    assert task.run(root, backend, pause_after_step=True)['state'] == 'paused'
    first_proof = frozen_files(root / 'attempts' / '0001')
    backend.blocked = True
    assert task.run(root, backend)['code'] == 'windows_application_blocked'
    assert len(backend.calls) == 1
    assert not list((root / 'attempts' / '0002').rglob('native-intent.json'))
    backend.blocked = False
    assert task.run(root, backend)['ok']
    assert backend.calls == [(first['id'], 'toggle', '1'), (second['id'], 'toggle', '1')]
    assert_unchanged(root / 'attempts' / '0001', first_proof)


@pytest.mark.parametrize('interrupted', [False, True])
def test_unknown_toggle_or_interruption_cannot_be_replayed(tmp_path, interrupted):
    c = checkbox()
    root = task.create(tmp_path, goal(condition(c, True)))
    backend = OfflineBackend(c)
    backend.unknown, backend.interrupted = True, interrupted
    result = task.run(root, backend)
    assert result['state'] == 'unknown' and len(backend.calls) == 1
    assert not task.inspect(root)['resumable']
    original = frozen_files(root)
    backend.unknown = backend.interrupted = False
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, backend)
    assert len(backend.calls) == 1
    assert frozen_files(root) == original


@pytest.mark.parametrize('checked', [None, 2])
def test_unreadable_checkbox_stops_before_durable_native_intent(tmp_path, checked):
    c = checkbox(checked=checked)
    root = task.create(tmp_path, goal(condition(c, True)))
    backend = OfflineBackend(c)
    result = task.run(root, backend)
    assert result['state'] == 'failed' and result['code'] == 'windows_no_safe_action'
    assert backend.calls == [] and not list(root.rglob('native-intent.json'))


def test_changed_retained_checkbox_blocks_remaining_action(tmp_path):
    first, second = checkbox(), checkbox('显示详细信息')
    root = task.create(tmp_path, goal(condition(first, True), condition(second, True, number=2)))
    backend = OfflineBackend(first, second)
    task.run(root, backend, pause_after_step=True)
    backend.controls[0]['checked'] = 0
    result = task.run(root, backend)
    assert result['code'] == 'windows_completed_state_changed'
    assert backend.calls == [(first['id'], 'toggle', '1')]


@pytest.mark.parametrize('field_name', ['查找内容(N)', 'Find what:'])
def test_legacy_find_without_owner_cannot_leak_value_state_to_model(field_name):
    unsafe = checkbox(field_name, id='field', type='ComboBox', patterns=['set_value'],
                      value='already matching', owner_hwnd=999)
    obs = observation(checkbox(), unsafe, application='wps', dialogs=[{'hwnd': 300, 'dialog_kind': 'find'}])
    context, targets = intent._context('修改明确搜索字段', [obs])
    rows = json.loads(context)['windows'][0]['candidates']
    assert [row['label'] for row in rows] == ['显示扩展名']
    assert all(target[1]['name'] != field_name for target in targets.values())


def test_equal_current_field_value_does_not_bypass_policy_in_goal_validation():
    field = checkbox('Find what:', id='field', type='ComboBox', patterns=['set_value'],
                     value='already matching', owner_hwnd=999)
    obs = observation(field, application='wps', dialogs=[{'hwnd': 300, 'dialog_kind': 'find'}])
    selector = {key: field[key] for key in ('type', 'name', 'automation_id')}
    targets = {'C1': (obs, selector, ['value'])}
    response = {'conditions': [{'id': 'C1', 'state': 'value', 'value': 'already matching', 'retain': True}]}
    assert task.satisfied(obs, {'selector': selector, 'state': 'value', 'value': 'already matching'})
    with pytest.raises(ComputerError, match='windows_intent_target_denied'):
        intent._goal('查找 already matching', response, targets)


@pytest.mark.parametrize('desired', [False, True])
def test_checkbox_intent_uses_boolean_desired_state_and_production_policy(desired):
    c = checkbox(checked=int(not desired))
    obs = observation(c)
    context, targets = intent._context('切换显示扩展名', [obs])
    row = json.loads(context)['windows'][0]['candidates'][0]
    assert row['states'] == ['checked'] and row['checked'] == int(not desired)
    response = {'conditions': [{'id': row['id'], 'state': 'checked', 'value': desired, 'retain': True}]}
    planned = intent._goal('切换显示扩展名', response, targets)
    assert access.candidates(obs, planned['conditions'][0]) == [
        {'control_id': c['id'], 'action': 'toggle', 'value': str(int(desired))}]


class ListSurface:
    def __init__(self):
        self.rows = []
    def delete(self, *args):
        self.rows.clear()
    def insert(self, position, text):
        self.rows.append(text)


def test_panel_review_distinguishes_check_and_uncheck_without_native_ui():
    c = checkbox()
    obs = observation(c)
    on = panel.condition_from_control(obs, c['id'], 'checked', number=1)
    off = panel.condition_from_control(obs, c['id'], 'unchecked', number=2)
    assert on['state'] == off['state'] == 'checked'
    assert on['value'] is True and off['value'] == 0
    surface = ListSurface()
    p = SimpleNamespace(conditions=[on, off], targets=surface,
                        content_source=SimpleNamespace(configure=lambda **kw: None, set=lambda text: None))
    panel.WindowsPanel.show_targets(p)
    assert len(surface.rows) == 2
    assert '取消勾选' not in surface.rows[0] and '勾选' in surface.rows[0]
    assert '取消勾选' in surface.rows[1]
