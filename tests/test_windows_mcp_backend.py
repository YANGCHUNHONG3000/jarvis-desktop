"""MCP integration policy and lifecycle; no desktop, subprocess or model calls."""
from contextlib import nullcontext
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from agent_service import windows_access as access, windows_mcp as mcp
from agent_service import windows_winapp as winapp
from agent_service.computer import ComputerError, digest


def control(kind='TabItem', **changes):
    result = dict(id='target', runtime_id=[10, 20], type=kind, name='View', automation_id='target',
                  patterns=['select'], enabled=True, visible=True, selected=False,
                  focused=False, value=None, checked=None, bounds=[10, 20, 100, 60],
                  expanded=None, ancestors=[], owner_hwnd=100)
    result.update(changes)
    return result


def observation(c):
    return dict(window={'hwnd': 100, 'pid': 10}, application='wps', dialogs=[],
                controls=[c], truncated=False)


@pytest.mark.parametrize('kind', ['TabItem', 'RadioButton'])
def test_safe_selections_offer_mcp_and_keep_original_policy(kind):
    c = control(kind)
    obs = observation(c)
    condition = {'selector': {k: c[k] for k in ('type', 'name', 'automation_id')},
                 'state': 'selected', 'value': True}
    assert access.WindowsMcpBackend.allowed(obs, c, 'select', '')
    assert access.WindowsMcpBackend.candidates(obs, condition) == [
        {'control_id': 'target', 'action': 'select', 'value': ''}]
    c['name'] = 'Delete account'
    assert not access.WindowsMcpBackend.allowed(obs, c, 'select', '')


@pytest.mark.parametrize('kind,action', [('ListItem', 'select'), ('TreeItem', 'select'),
    ('TreeItem', 'expand'), ('TreeItem', 'collapse'), ('TabItem', 'focus'), ('Button', 'invoke')])
def test_mcp_cannot_inherit_extra_native_or_winapp_actions(kind, action):
    c = control(kind, patterns=[action], expanded=0)
    assert not access.WindowsMcpBackend.allowed(observation(c), c, action, '')


def test_mcp_existing_checkbox_contract():
    c = control('CheckBox', patterns=['toggle'], checked=0)
    assert access.WindowsMcpBackend.allowed(observation(c), c, 'toggle', '1')
    for bounds in (None, [0, 0, 0, 4], [0.0, 0, 4, 4]):
        c['bounds'] = bounds
        assert not access.WindowsMcpBackend.allowed(observation(c), c, 'toggle', '1')


@pytest.mark.parametrize('kind', ['Edit', 'ComboBox'])
def test_mcp_disables_typing_without_changing_native_or_winapp_input(kind):
    c = control(kind, patterns=['set_value'], name='Search option', value='')
    obs = observation(c)
    assert not access.WindowsMcpBackend.allowed(obs, c, 'set_value', 'needle')
    condition = {'selector': {k: c[k] for k in ('type', 'name', 'automation_id')},
                 'state': 'value', 'value': 'needle'}
    assert access.WindowsMcpBackend.candidates(obs, condition) == []
    assert access.allowed(obs, c, 'set_value', 'needle')
    assert access.WinappBackend.allowed(obs, c, 'set_value', 'needle')


@pytest.mark.parametrize('reason', ['truncated', 'blocked_reason'])
def test_mcp_rejects_incomplete_or_modal_observation(reason):
    c = control()
    obs = observation(c)
    obs[reason] = True
    assert not access.WindowsMcpBackend.allowed(obs, c, 'select', '')


def test_provider_bindings_remain_distinct_and_restorable(tmp_path):
    backend = access.WindowsMcpBackend(tmp_path)
    assert backend.seconds == 30 and backend.allow_visual_options is False
    assert not getattr(backend, 'allow_office_options', False)
    assert winapp.valid_provider_binding(mcp.PROVIDER_BINDING)
    assert access.backend_for_binding(mcp.PROVIDER_BINDING, fallback=backend) is backend
    assert type(access.backend_for_binding(mcp.PROVIDER_BINDING)) is access.WindowsMcpBackend
    assert type(access.backend_for_binding(winapp.PROVIDER_BINDING)) is access.WinappBackend
    assert type(access.backend_for_binding(winapp.OFFICE_PROVIDER_BINDING)) is access.WinappOfficeBackend
    invalid = dict(mcp.PROVIDER_BINDING, version='0.0.0')
    assert not winapp.valid_provider_binding(invalid)
    with pytest.raises(ComputerError, match='windows_provider_binding_invalid'):
        access.backend_for_binding(invalid)


def test_mcp_task_keeps_binding_and_rejects_wrong_backend_before_observation(tmp_path):
    from agent_service import windows_task as task
    from test_windows_task import Backend, goal
    root = task.create(tmp_path, goal(), provider_binding=mcp.PROVIDER_BINDING)
    assert task.load(root)['provider_binding'] == mcp.PROVIDER_BINDING
    assert type(access.backend_for_task(root)) is access.WindowsMcpBackend
    backend = Backend()
    with pytest.raises(ComputerError, match='windows_provider_binding_mismatch'):
        task.run(root, backend)
    assert backend.observations == 0 and backend.calls == []


def test_mcp_backend_dispatch_preserves_bound_provider_and_native_sha(tmp_path, monkeypatch):
    backend = access.WindowsMcpBackend(tmp_path)
    c = control()
    seen = []
    monkeypatch.setattr(backend, '_call', lambda request, **kwargs: seen.append((request, kwargs)) or {'ok': True})
    backend.act({'hwnd': 100}, c, 'select', '', 'native-sha', tmp_path / 'operation')
    assert seen[0][0]['provider'] == mcp.PROVIDER_BINDING
    assert seen[0][0]['before_sha'] == 'native-sha'
    assert seen[0][1]['root'] == tmp_path / 'operation'


@pytest.fixture
def native(monkeypatch, tmp_path):
    c = control()
    obs = observation(c)
    state = SimpleNamespace(c=c, obs=obs, calls=[], foreground=100, fault=None, input_checks=0)
    wrapper = SimpleNamespace()
    wrappers = {'target': wrapper}
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        GetForegroundWindow=lambda: state.foreground,
        SetForegroundWindow=lambda hwnd: setattr(state, 'foreground', hwnd)))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(
        NoPatternInterfaceError=type('NoPatternInterfaceError', (Exception,), {})))
    monkeypatch.setattr(access.uia, '_native', nullcontext)
    monkeypatch.setattr(access.uia, '_recheck', lambda window: window)
    monkeypatch.setattr(access, '_native_record', lambda *args: deepcopy(c))
    monkeypatch.setattr(access, '_input_epoch', lambda: 99)
    monkeypatch.setattr(access, '_foreground_context', lambda: {
        'caller_pid': 10, 'foreground_pid': 20, 'foreground_hwnd': state.foreground})
    def observe(request, check):
        check()
        state.calls.append('native-observe')
        if (state.fault in {'popup', 'early-popup'}
                and state.calls.count('native-observe') == (2 if state.fault == 'early-popup' else 3)):
            return {**deepcopy(obs), 'blocked_reason': 'owned_window_requires_manual_attention',
                    'blocked_windows': [{'hwnd': 300, 'class_name': '#32770'}]}, wrappers
        if state.fault == 'changed' and state.calls.count('native-observe') == 3:
            changed = deepcopy(obs)
            changed['controls'][0]['name'] = 'Changed'
            return changed, wrappers
        return deepcopy(obs), wrappers
    monkeypatch.setattr(access, '_request_observation', observe)
    class Reader:
        def __init__(self, root, *, timeout, check):
            assert timeout == 12
            state.calls.append('reader-open')
        def bind(self, hwnd, target, wrapper, *, action):
            state.calls.append('bind')
            if state.fault == 'bind':
                raise ComputerError('binding_failed')
            if state.fault == 'foreground':
                state.foreground = 999
            return {'test-binding': True}
        def perform_bound(self, hwnd, binding, action, value):
            assert (tmp_path / 'native-intent.json').is_file()
            state.calls.append('dispatch')
            if state.fault == 'dispatch':
                raise ComputerError('dispatch_failed')
            if state.fault != 'unverified':
                c['selected'] = True
        def close(self):
            state.calls.append('reader-close')
        def check_input(self):
            state.input_checks += 1
            if state.fault == 'post-input':
                raise ComputerError('user_takeover')
    monkeypatch.setattr(mcp, 'McpReader', Reader)
    monkeypatch.setattr(winapp, 'WinAppReader', lambda *args, **kw: pytest.fail('MCP must not use Winapp'))
    state.request = dict(op='act', window=obs['window'], before_sha=digest(obs),
                         provider=mcp.PROVIDER_BINDING, control=deepcopy(c), action='select', value='')
    return state


def test_mcp_dispatch_has_intent_native_readback_and_finally_cleanup(native, tmp_path):
    epoch = [1]
    result = access._perform(native.request, tmp_path, lambda: None, epoch)
    assert result['ok'] and result['state'] == 'verified'
    assert native.calls.count('dispatch') == 1 and epoch == [99]
    assert native.input_checks >= 3
    assert native.calls[-2:] == ['native-observe', 'reader-close']


def test_mcp_waits_for_asynchronous_foreground_once_before_binding(native, tmp_path, monkeypatch):
    native.foreground = 999
    requests, waits = [], []
    monkeypatch.setattr(sys.modules['win32gui'], 'SetForegroundWindow', requests.append)
    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            native.foreground = 100
    monkeypatch.setattr(access.time, 'sleep', wait)
    result = access._perform(native.request, tmp_path, lambda: None, [1])
    assert result['state'] == 'verified' and requests == [100] and waits == [.03, .03]
    assert native.calls.count('bind') == 1 and native.calls.count('dispatch') == 1


def test_mcp_activation_state_change_is_reobserved_before_reader_preparation(native, tmp_path, monkeypatch):
    native.foreground = 999
    def activate(hwnd):
        native.foreground = hwnd
        native.c['focused'] = True
    monkeypatch.setattr(sys.modules['win32gui'], 'SetForegroundWindow', activate)
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert native.calls == ['native-observe', 'native-observe']
    assert not (tmp_path / 'native-intent.json').exists()


def test_mcp_activation_popup_stops_before_reader_preparation(native, tmp_path):
    native.fault = 'early-popup'
    result = access._perform(native.request, tmp_path, lambda: None, [1])
    assert result['code'] == 'windows_owned_dialog_before_action' and result['effects'] == 'none'
    assert native.calls == ['native-observe', 'native-observe']
    assert not (tmp_path / 'native-intent.json').exists()


def test_mcp_foreground_timeout_never_prepares_or_dispatches(native, tmp_path, monkeypatch):
    native.foreground = 999
    requests = []
    monkeypatch.setattr(sys.modules['win32gui'], 'SetForegroundWindow', requests.append)
    ticks = iter([0, 3])
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(ComputerError, match='window_foreground_denied') as captured:
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert requests == [100] and native.calls == ['native-observe']
    assert not (tmp_path / 'native-intent.json').exists()
    assert captured.value.input_diagnostic == {'phase': 'mcp_foreground_validation',
        'expected_hwnd': 100, 'caller_pid': 10, 'foreground_pid': 20, 'foreground_hwnd': 999}


def test_mcp_foreground_api_failure_retains_bounded_diagnostic(native, tmp_path, monkeypatch):
    native.foreground = 999
    def denied(hwnd):
        raise OSError('synthetic foreground failure')
    monkeypatch.setattr(sys.modules['win32gui'], 'SetForegroundWindow', denied)
    with pytest.raises(ComputerError, match='window_foreground_denied') as captured:
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert captured.value.input_diagnostic['expected_hwnd'] == 100
    assert captured.value.input_diagnostic['foreground_hwnd'] == 999
    assert native.calls == ['native-observe'] and not (tmp_path / 'native-intent.json').exists()


def test_forged_file_selection_cannot_start_mcp_reader(native, tmp_path):
    native.c['type'] = 'ListItem'
    native.obs['application'] = 'explorer'
    native.request['control'] = deepcopy(native.c)
    native.request['before_sha'] = digest(native.obs)
    with pytest.raises(ComputerError, match='windows_provider_action_denied'):
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert native.calls == ['native-observe']
    assert not (tmp_path / 'native-intent.json').exists()


def test_forged_mcp_typing_stops_before_native_observation(native, tmp_path):
    native.request.update(action='set_value', value='needle')
    with pytest.raises(ComputerError, match='windows_provider_action_denied'):
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert native.calls == [] and not (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('fault,expected,intent', [('bind', 'binding_failed', False),
    ('changed', 'screen_changed_reobserve', False), ('foreground', 'user_takeover', False),
    ('post-input', 'user_takeover', True),
    ('dispatch', 'dispatch_failed', True)])
def test_mcp_cleanup_survives_preparation_and_dispatch_failures(native, tmp_path, fault, expected, intent):
    native.fault = fault
    with pytest.raises(ComputerError, match=expected):
        access._perform(native.request, tmp_path, lambda: None, [1])
    assert native.calls[-1] == 'reader-close'
    assert (tmp_path / 'native-intent.json').exists() is intent
    assert native.calls.count('dispatch') == int(intent)


def test_mcp_popup_during_binding_closes_without_journal_or_dispatch(native, tmp_path):
    native.fault = 'popup'
    result = access._perform(native.request, tmp_path, lambda: None, [1])
    assert result['code'] == 'windows_owned_dialog_before_action'
    assert result['effects'] == 'none' and not (tmp_path / 'native-intent.json').exists()
    assert 'dispatch' not in native.calls and native.calls[-1] == 'reader-close'


def test_mcp_success_message_cannot_replace_native_state_verification(native, tmp_path, monkeypatch):
    native.fault = 'unverified'
    ticks = iter([0, 10])
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    result = access._perform(native.request, tmp_path, lambda: None, [1])
    assert result['state'] == 'unknown' and result['code'] == 'windows_postcondition_failed'
    assert native.calls.count('dispatch') == 1 and native.calls[-1] == 'reader-close'


@pytest.mark.parametrize('field', ['conditions', 'sources', 'task_root', 'proposal_task', 'saved_task'])
def test_mcp_ui_switch_preserves_existing_task_without_tk(field):
    from agent_service.windows_panel import WindowsPanel
    p = SimpleNamespace(running=False, external_busy=lambda: False, conditions=[], sources=[],
                        task_root=None, proposal_task=None, saved_task=None, backend='old')
    p.status = SimpleNamespace(set=lambda text: None)
    setattr(p, field, ['bound'] if field in ('conditions', 'sources') else 'bound')
    WindowsPanel.use_mcp(p)
    assert p.backend == 'old'


def test_mcp_ui_switch_discards_observation_and_exposes_no_other_permissions(tmp_path):
    from agent_service.windows_panel import WindowsPanel
    deleted = []
    p = SimpleNamespace(running=False, external_busy=lambda: False, conditions=[], sources=[],
                        task_root=None, proposal_task=None, saved_task=None, backend=None,
                        observation={'old': True}, approved_observations=[{'old': True}],
                        status=SimpleNamespace(set=lambda text: None),
                        tree=SimpleNamespace(get_children=lambda: ['old-control'], delete=lambda *ids: deleted.extend(ids)))
    WindowsPanel.use_mcp(p)
    assert type(p.backend) is access.WindowsMcpBackend
    assert p.observation is None and p.approved_observations == [] and deleted == ['old-control']
    assert not getattr(p.backend, 'allow_office_options', False)
