"""Generic control policy and native dispatch, without desktop/UI dependencies."""
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_controls as controls


def observation(**updates):
    return {'application': 'explorer', 'dialogs': [], 'truncated': False, **updates}


def control(kind='CheckBox', **updates):
    data = dict(id='target', type=kind, name='显示扩展名', automation_id='check1',
                visible=True, enabled=True, patterns=['toggle'], checked=0,
                ancestors=[])
    data.update(updates)
    return data


@pytest.mark.parametrize('application', ['explorer', 'wps'])
@pytest.mark.parametrize('initial,target', [(0, True), (1, False)])
def test_checkbox_candidate_is_desired_state(application, initial, target):
    c = control(checked=initial)
    assert controls.candidate(observation(application=application),
                              {'state': 'checked', 'value': target}, c) == {
        'control_id': 'target', 'action': 'toggle', 'value': str(int(target))}


@pytest.mark.parametrize('value', [0, 1, False, True])
def test_checkbox_already_satisfied_has_no_action(value):
    assert controls.candidate(observation(), {'state': 'checked', 'value': value},
                              control(checked=int(value))) is None


@pytest.mark.parametrize('actual', [None, 2, -1, '0', 0.0])
def test_unknown_or_indeterminate_toggle_never_offered(actual):
    c = control(checked=actual)
    assert not controls.allowed(observation(), c, 'toggle', '1')
    assert controls.states(observation(), c) == []


@pytest.mark.parametrize('kind', ['TabItem', 'RadioButton'])
def test_tab_and_radio_selection_is_state_verifiable(kind):
    c = control(kind, name='详细信息', patterns=['select'], selected=False)
    assert controls.states(observation(), c) == ['selected']
    assert controls.candidate(observation(), {'state': 'selected', 'value': True}, c)['action'] == 'select'
    c['selected'] = True
    assert controls.candidate(observation(), {'state': 'selected', 'value': True}, c) is None


@pytest.mark.parametrize('kind', ['ListItem', 'TreeItem', 'Button', 'Document', 'Window'])
def test_module_does_not_expand_existing_or_unverifiable_actions(kind):
    c = control(kind, patterns=['select', 'toggle', 'invoke', 'set_value'], selected=False)
    assert controls.states(observation(), c) == []
    assert not controls.allowed(observation(), c, 'invoke', '')


@pytest.mark.parametrize('label', ['删除记录', '发送', '购买', '更改权限', '登录', '安全设置',
                                  '文件路径', 'Run command', 'Save as', 'DeleteAll',
                                  'send_message', 'Privacy settings', '启用宏',
                                  '允许访问', 'Allow editing', 'Trust certificate'])
@pytest.mark.parametrize('where', ['name', 'ancestor', 'automation_id'])
def test_dangerous_label_or_context_blocks_all_generic_actions(label, where):
    c = control()
    if where == 'ancestor':
        c['ancestors'] = [{'name': label, 'automation_id': '', 'type': 'Pane'}]
    else:
        c[where] = label
    assert not controls.allowed(observation(), c, 'toggle', '1')


@pytest.mark.parametrize('label', ['查找内容(N)', '搜索此电脑', 'Find what:', 'Search', '搜尋'])
@pytest.mark.parametrize('kind', ['Edit', 'ComboBox'])
def test_only_semantically_named_search_fields_allow_values(label, kind):
    c = control(kind, name=label, patterns=['set_value'], value='before')
    assert controls.allowed(observation(), c, 'set_value', '删除仅作搜索词')
    assert controls.candidate(observation(), {'state': 'value', 'value': 'after'}, c)['value'] == 'after'
    assert controls.candidate(observation(), {'state': 'value', 'value': 'before'}, c) is None


@pytest.mark.parametrize('label', ['', '正文', '文件名', 'Address', 'Search command', '查找并替换'])
def test_document_path_command_or_ambiguous_fields_refused(label):
    c = control('Edit', name=label, patterns=['set_value'])
    assert not controls.allowed(observation(), c, 'set_value', 'changed')


@pytest.mark.parametrize('value', ['x\n', 'x\t', '\x00', 'x' * 301, None])
def test_search_values_remain_bounded_single_line(value):
    c = control('Edit', name='Search', patterns=['set_value'])
    assert not controls.allowed(observation(), c, 'set_value', value)


@pytest.mark.parametrize('change', [dict(application='unknown'), dict(blocked_reason='popup'),
                                    dict(truncated=True), dict(dialogs=[{'dialog_kind': 'file_status'}]),
                                    dict(dialogs=None)])
def test_incomplete_untrusted_or_blocked_observation_denies(change):
    o = observation()
    o.update(change)
    assert not controls.allowed(o, control(), 'toggle', '1')


@pytest.mark.parametrize('change', [dict(visible=False), dict(enabled=False), dict(password=True),
                                    dict(is_password=True), dict(ancestors=None), dict(patterns=[])])
def test_protected_unavailable_or_unreadable_control_denies(change):
    assert not controls.allowed(observation(), control(**change), 'toggle', '1')


class MissingPattern(Exception):
    pass


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines',
                        SimpleNamespace(NoPatternInterfaceError=MissingPattern))
    state = SimpleNamespace(CurrentToggleState=0, calls=[])
    state.Toggle = lambda: state.calls.append('toggle')
    wrapper = SimpleNamespace(iface_toggle=state,
                              rectangle=lambda: SimpleNamespace(left=-20, top=10, right=180, bottom=80))
    return wrapper, state


def test_enrich_reads_bounds_and_toggle_without_mutating_other_fields(native):
    wrapper, state = native
    record = control(patterns=[])
    before = deepcopy(record)
    assert controls.enrich(wrapper, record) is record
    assert record['bounds'] == [-20, 10, 180, 80]
    assert record['checked'] == 0 and record['patterns'] == ['toggle']
    assert record['name'] == before['name']


@pytest.mark.parametrize('state', [1, 2])
def test_enrich_retains_indeterminate_without_authorizing(native, state):
    wrapper, pattern = native
    pattern.CurrentToggleState = state
    enriched = controls.enrich(wrapper, control())
    assert enriched['checked'] == state
    assert controls.allowed(observation(), enriched, 'toggle', '0') == (state == 1)


def test_pattern_absence_is_optional_but_provider_failure_propagates(native):
    wrapper, _ = native
    del wrapper.iface_toggle
    assert controls.enrich(wrapper, control())['checked'] is None

    class Broken:
        rectangle = staticmethod(wrapper.rectangle)
        @property
        def iface_toggle(self):
            raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError, match='provider unavailable'):
        controls.enrich(Broken(), control())


def test_enrich_skips_protected_record_and_empty_bounds(native):
    wrapper, _ = native
    assert controls.enrich(wrapper, None) is None
    wrapper.rectangle = lambda: SimpleNamespace(left=0, top=0, right=0, bottom=0)
    assert controls.enrich(wrapper, control())['bounds'] is None


def test_toggle_rechecks_state_and_dispatches_once(native):
    wrapper, pattern = native
    controls.perform(wrapper, control(), 'toggle', '1')
    assert pattern.calls == ['toggle']
    pattern.CurrentToggleState = 1
    controls.perform(wrapper, control(checked=1), 'toggle', '1')
    assert pattern.calls == ['toggle']
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        controls.perform(wrapper, control(), 'toggle', '0')
    assert pattern.calls == ['toggle']


def test_native_helpers_never_invoke_buttons_or_write_readonly_fields():
    wrapper = SimpleNamespace(iface_value=SimpleNamespace(CurrentIsReadOnly=True))
    with pytest.raises(ComputerError, match='control_readonly'):
        controls.perform(wrapper, control('Edit'), 'set_value', 'query')
    with pytest.raises(ComputerError, match='windows_action_denied'):
        controls.perform(wrapper, control('Button'), 'invoke', '')
