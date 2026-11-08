from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import json
from pathlib import Path
import sys

import pytest

from agent_service import windows_access as access
from agent_service.computer import ComputerError, digest


class NoPatternInterfaceError(Exception):
    pass


@pytest.fixture(autouse=True)
def native_pattern_import(monkeypatch):
    """Mirror the optional COM pattern exception without importing desktop APIs."""
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines',
                        SimpleNamespace(NoPatternInterfaceError=NoPatternInterfaceError))


def control(**changes):
    return dict(id='item', runtime_id=[1], type='ListItem', name='existing.xlsx', automation_id='1',
                value=None, patterns=['select', 'focus'], visible=True, enabled=True,
                selected=False, expanded=None, focused=False, **changes)


def observation(c=None, **changes):
    return dict(window={'hwnd': 100, 'pid': 10}, controls=[c or control()],
                application='explorer', dialogs=[], truncated=False, **changes)


@pytest.mark.parametrize('action', ['invoke', 'delete', 'type_keys', 'set_value', 'open_find'])
def test_model_cannot_turn_file_selection_into_effect(action):
    c = control()
    c['patterns'].extend(['invoke', 'set_value'])
    assert not access.allowed(observation(c), c, action, '')


@pytest.mark.parametrize('reason', ['truncated', 'blocked_reason'])
def test_incomplete_or_modal_view_cannot_act(reason):
    o = observation()
    o[reason] = True
    assert not access.allowed(o, o['controls'][0], 'select', '')


@pytest.mark.parametrize('bounds', [None, [0, 0, 0, 0], [1, 1, 1, 9], [1, 1, 9, 1], [1.0, 1, 9, 9]])
def test_winapp_rejects_missing_or_invalid_target_geometry(tmp_path, bounds):
    c = control(bounds=bounds)
    obs = observation(c)
    condition = {'selector': {'type': 'ListItem', 'name': 'existing.xlsx', 'automation_id': '1'},
                 'state': 'selected', 'value': True}
    assert access.allowed(obs, c, 'select', '')
    assert not access.WinappBackend.allowed(obs, c, 'select', '')
    assert access.WinappBackend.candidates(obs, condition) == []


def test_winapp_accepts_only_real_positive_target_geometry(tmp_path):
    c = control(bounds=[10, 20, 100, 40])
    obs = observation(c)
    assert access.WinappBackend.allowed(obs, c, 'select', '')


def test_winapp_excludes_leaf_and_unknown_expansion_state(tmp_path):
    for expanded in (None, 2, 3, True):
        c = control(bounds=[10, 20, 100, 40])
        c.update(type='TreeItem', patterns=['expand', 'collapse'], expanded=expanded)
        assert not access.WinappBackend.allowed(observation(c), c, 'expand', '')
        assert not access.WinappBackend.allowed(observation(c), c, 'collapse', '')
    c['expanded'] = 0
    assert access.WinappBackend.allowed(observation(c), c, 'expand', '')


@pytest.mark.parametrize('rect, expected', [((0, 0, 0, 0), None), ((10, 20, 100, 40), [10, 20, 100, 40])])
def test_winapp_observation_records_target_bounds_without_changing_legacy(monkeypatch, rect, expected):
    c = control()
    obs = observation(c)
    wrapper = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3]))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(EnumWindows=lambda callback, value: None))
    monkeypatch.setattr(access.uia, '_recheck', lambda window: window)
    monkeypatch.setattr(access, '_kind', lambda window: 'explorer')
    monkeypatch.setattr(access, '_tree', lambda window, check: (deepcopy(obs), {'item': wrapper}))
    legacy, _ = access._observe(obs['window'], lambda: None)
    assert 'bounds' not in legacy['controls'][0]
    provider, _ = access._observe(obs['window'], lambda: None, winapp_find=True)
    assert provider['controls'][0]['bounds'] == expected


@pytest.mark.parametrize('changed', [False, True])
def test_winapp_action_rechecks_target_geometry_before_intent(monkeypatch, tmp_path, changed):
    from agent_service import windows_winapp
    c = control(bounds=[10, 20, 100, 40])
    obs = observation(c)
    reads = []
    def rectangle():
        reads.append(1)
        return SimpleNamespace(left=10, top=20, right=101 if changed and len(reads) > 1 else 100, bottom=40)
    wrapper = SimpleNamespace(rectangle=rectangle)
    native_fixture(monkeypatch, obs, {'item': wrapper})
    monkeypatch.setattr(access.uia, '_record', lambda *args: {k: v for k, v in c.items() if k != 'bounds'})
    class Reader:
        def __init__(self, *args, **kwargs): pass
        def bind(self, *args, **kwargs): return {'selector': 'itm-existing-1234'}
        def perform_bound(self, *args, **kwargs): raise RuntimeError('dispatch reached')
    monkeypatch.setattr(windows_winapp, 'WinAppReader', Reader)
    request = {'op': 'act', 'window': obs['window'], 'before_sha': digest(obs), 'provider': windows_winapp.PROVIDER_BINDING,
               'control': c, 'action': 'select', 'value': ''}
    if changed:
        with pytest.raises(ComputerError, match='screen_changed_reobserve'):
            access._perform(request, tmp_path, lambda: None, [0])
        assert not (tmp_path / 'native-intent.json').exists()
    else:
        with pytest.raises(RuntimeError, match='dispatch reached'):
            access._perform(request, tmp_path, lambda: None, [0])
        assert (tmp_path / 'native-intent.json').is_file()
    assert len(reads) >= 2


def test_wps_value_pattern_does_not_grant_document_writes():
    c = control()
    c.update(type='Document', name='正文', patterns=['set_value'])
    o = observation(c)
    o['application'] = 'wps'
    assert not access.allowed(o, c, 'set_value', 'changed')
    c.update(type='ComboBox', name='查找内容(N)', owner_hwnd=200)
    assert not access.allowed(o, c, 'set_value', 'query')
    o['dialogs'] = [{'hwnd': 200}]
    assert access.allowed(o, c, 'set_value', 'query')
    assert not access.allowed(o, c, 'set_value', 'query\n')
    assert not access.allowed(o, c, 'set_value', 'x' * 301)


def test_same_name_targets_do_not_produce_first_match():
    o = observation()
    o['controls'].append({**control(), 'id': 'other'})
    condition = {'selector': {'type': 'ListItem', 'name': 'existing.xlsx', 'automation_id': '1'},
                 'state': 'selected', 'value': True}
    assert access.candidates(o, condition) == []


def test_absent_find_target_can_only_offer_fixed_find_command():
    o = observation()
    o.update(application='wps', controls=[])
    condition = {'selector': access.FIND_SELECTOR, 'state': 'value', 'value': 'words'}
    assert access.candidates(o, condition) == [{'control_id': None, 'action': 'open_find', 'value': ''}]
    o['dialogs'] = [{'hwnd': 200}]
    assert access.candidates(o, condition) == []


def native_fixture(monkeypatch, o, wrappers):
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace())
    monkeypatch.setattr(access.uia, '_native', nullcontext)
    monkeypatch.setattr(access.uia, '_recheck', lambda window: window)
    monkeypatch.setattr(access, '_observe', lambda window, check, selector=None, **kwargs: (deepcopy(o), wrappers))


def test_changed_observation_stops_before_native_intent(monkeypatch, tmp_path):
    o = observation()
    native_fixture(monkeypatch, o, {})
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform({'op': 'act', 'window': o['window'], 'before_sha': 'stale'}, tmp_path, lambda: None, [0])
    assert not (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('point, expected_reads, expected_actions', [
    ('initial', 1, 0), ('before_dispatch', 2, 0), ('after_dispatch', 3, 1)])
def test_owned_popup_stops_at_first_observation(monkeypatch, tmp_path, point, expected_reads, expected_actions):
    c = control()
    normal = observation(c)
    blocked = {**deepcopy(normal), 'blocked_reason': 'owned_window_requires_manual_attention',
               'blocked_windows': [{'hwnd': 300, 'class_name': '#32770', 'title': ''}]}
    calls, actions = [], []
    wrapper = SimpleNamespace(iface_selection_item=SimpleNamespace(Select=lambda: actions.append('select')))
    native_fixture(monkeypatch, normal, {'item': wrapper})
    monkeypatch.setattr(access.uia, '_record', lambda *args: deepcopy(c))
    def get_observation(request, check):
        calls.append(1)
        found = (point == 'initial' and len(calls) == 1 or
                 point == 'before_dispatch' and len(calls) == 2 or
                 point == 'after_dispatch' and len(calls) == 3)
        return deepcopy(blocked if found else normal), {'item': wrapper}
    monkeypatch.setattr(access, '_request_observation', get_observation)
    request = {'op': 'act', 'window': normal['window'], 'before_sha': digest(normal),
               'control': c, 'action': 'select', 'value': ''}
    result = access._perform(request, tmp_path, lambda: None, [0])
    assert len(calls) == expected_reads and len(actions) == expected_actions
    assert result['observation'] == access._safe_blocked_observation(blocked)
    assert result['code'] == ('windows_owned_dialog_after_action' if expected_actions else 'windows_owned_dialog_before_action')
    assert (result['state'], result['effects']) == (('unknown', 'unknown') if expected_actions else ('failed', 'none'))
    assert (tmp_path / 'native-intent.json').exists() is bool(expected_actions)


def test_blocked_observation_strips_titles_controls_and_dialog_content():
    c = control()
    c['name'] = 'PRIVATE_CONTROL_MARKER'
    raw = {**observation(c), 'blocked_reason': 'owned_window_requires_manual_attention',
           'dialogs': [{'hwnd': 301, 'class_name': 'Dialog', 'title': 'PRIVATE_DIALOG_MARKER'}],
           'blocked_windows': [{'hwnd': 300, 'class_name': '#32770', 'title': 'PRIVATE_TITLE_MARKER',
                                'body': 'PRIVATE_BODY_MARKER', 'style': 123}]}
    sanitized = access._safe_blocked_observation(raw)
    assert sanitized['controls'] == [] and sanitized['dialogs'] == []
    assert sanitized['blocked_windows'] == [{'hwnd': 300, 'class_name': '#32770'}]
    assert 'PRIVATE_' not in json.dumps(sanitized, ensure_ascii=False)


def test_worker_observe_sanitizes_blocked_result_before_return(monkeypatch, tmp_path):
    raw = {**observation(control()), 'blocked_reason': 'owned_window_requires_manual_attention',
           'blocked_windows': [{'hwnd': 300, 'class_name': '#32770', 'title': 'PRIVATE_TITLE_MARKER'}]}
    native_fixture(monkeypatch, raw, {})
    result = access._perform({'op': 'observe', 'window': raw['window']}, tmp_path, lambda: None, [None])
    assert result['ok'] and result['observation']['controls'] == []
    assert 'PRIVATE_' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('metadata', [
    [], [{'hwnd': 0, 'class_name': 'Dialog'}], [{'hwnd': True, 'class_name': 'Dialog'}],
    [{'hwnd': 1, 'class_name': ''}], [{'hwnd': 1, 'class_name': 'A\nB'}],
    [{'hwnd': 1, 'class_name': 'A' * 129}], [{'hwnd': 1, 'class_name': 'A'}, {'hwnd': 1, 'class_name': 'B'}],
    [{'hwnd': i, 'class_name': 'A'} for i in range(1, 18)]])
def test_invalid_blocked_metadata_is_rejected(metadata):
    raw = {**observation(), 'blocked_reason': 'owned_window_requires_manual_attention', 'blocked_windows': metadata}
    with pytest.raises(ComputerError, match='windows_blocked_metadata_invalid'):
        access._safe_blocked_observation(raw)


def test_native_unknown_popup_returns_only_safe_metadata_and_stops_visiting(monkeypatch):
    c = control()
    c['name'] = 'PRIVATE_CONTROL_MARKER'
    obs = observation(c)
    seen, trees = [], []
    gui = SimpleNamespace(EnumWindows=lambda callback, unused: [callback(201, None), callback(202, None)],
                          IsWindowVisible=lambda hwnd: True, GetClassName=lambda hwnd: '#32770')
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setattr(access.uia, '_recheck', lambda window: window)
    monkeypatch.setattr(access, '_kind', lambda window: 'explorer')
    monkeypatch.setattr(access, '_owned', lambda hwnd, root: True)
    def tree(window, check):
        trees.append(window['hwnd'])
        return deepcopy(obs), {'item': object()}
    monkeypatch.setattr(access, '_tree', tree)
    def identity(hwnd):
        seen.append(hwnd)
        return {'hwnd': hwnd, 'class_name': '#32770', 'title': 'PRIVATE_POPUP_MARKER'}
    monkeypatch.setattr(access.uia, '_identity', identity)
    result, wrappers = access._observe(obs['window'], lambda: None)
    assert seen == [201] and trees == [obs['window']['hwnd']] and wrappers == {}
    assert result['blocked_windows'] == [{'hwnd': 201, 'class_name': '#32770'}]
    assert 'PRIVATE_' not in json.dumps(result, ensure_ascii=False)


def test_wrapper_rebound_after_snapshot_stops_before_intent(monkeypatch, tmp_path):
    c = control()
    o = observation(c)
    native_fixture(monkeypatch, o, {'item': object()})
    monkeypatch.setattr(access.uia, '_record', lambda wrapper, pid: {**c, 'name': 'other.xlsx'})
    request = {'op': 'act', 'window': o['window'], 'before_sha': digest(o),
               'control': c, 'action': 'select', 'value': ''}
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(request, tmp_path, lambda: None, [0])
    assert not (tmp_path / 'native-intent.json').exists()


def test_missing_qt_editor_stops_before_intent(monkeypatch, tmp_path):
    c = control()
    c.update(type='ComboBox', name='查找内容(N)', owner_hwnd=200, patterns=['set_value'], value='')
    o = observation(c)
    o.update(application='wps', dialogs=[{'hwnd': 200}])
    native_fixture(monkeypatch, o, {})
    request = {'op': 'act', 'window': o['window'], 'before_sha': digest(o),
               'control': c, 'action': 'set_value', 'value': 'query'}
    with pytest.raises(ComputerError, match='windows_find_editor_ambiguous'):
        access._perform(request, tmp_path, lambda: None, [0])
    assert not (tmp_path / 'native-intent.json').exists()


def test_new_approval_replaces_input_epoch_and_finish_clears(monkeypatch, tmp_path):
    b = access.WindowsBackend(tmp_path)
    monkeypatch.setattr(access, '_input_epoch', lambda: 4)
    b.begin_run()
    assert b.input_epoch == 4
    monkeypatch.setattr(access, '_input_epoch', lambda: 8)
    b.begin_run()
    assert b.input_epoch == 8
    b.end_run()
    assert b.input_epoch is None


def test_postcondition_uses_unique_target_before_unnamed_ancestor(monkeypatch, tmp_path):
    c = control()
    c.update(type='TreeItem', name='Downloads', expanded=1, patterns=['collapse'],
             ancestors=[{'type': 'Pane', 'name': '', 'automation_id': ''}])
    o = observation(c)
    wrapper = SimpleNamespace(iface_expand_collapse=SimpleNamespace(Collapse=lambda: c.update(expanded=0)))
    native_fixture(monkeypatch, o, {'item': wrapper})
    monkeypatch.setattr(access.uia, '_record', lambda wrapper, pid: {k:v for k,v in c.items() if k!='ancestors'})
    request = {'op': 'act', 'window': o['window'], 'before_sha': digest(o),
               'control': deepcopy(c), 'action': 'collapse', 'value': ''}
    result = access._perform(request, tmp_path, lambda: None, [0])
    assert result['ok'] and result['effects'] == 'verified'
    assert result['observation']['controls'][0]['expanded'] == 0


def test_visible_child_under_scrolled_parent_is_not_lost(monkeypatch):
    class Node:
        def __init__(self, name, hidden=False):
            self.name, self.CurrentIsOffscreen, self.CurrentIsPassword = name, hidden, False
            self.children = []
    root, hidden, target = Node('window'), Node('hidden-parent', True), Node('visible-file')
    root.children, hidden.children = [hidden], [target]
    class Wrapper:
        def __init__(self, node):
            self.node = node
            self.element_info = SimpleNamespace(element=node, process_id=10)
    walker = SimpleNamespace(GetFirstChildElement=lambda n: n.children[0] if n.children else None,
                             GetNextSiblingElement=lambda n: None)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Desktop=lambda **kw:
        SimpleNamespace(window=lambda **kw: SimpleNamespace(wrapper_object=lambda: Wrapper(root)))))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(NoPatternInterfaceError=NoPatternInterfaceError, IUIA=lambda:
        SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker))))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_element_info', SimpleNamespace(UIAElementInfo=lambda n:n))
    monkeypatch.setitem(sys.modules, 'pywinauto.controls.uiawrapper', SimpleNamespace(UIAWrapper=Wrapper))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(GetForegroundWindow=lambda:100))
    monkeypatch.setattr(access.uia, '_recheck', lambda w:w)
    read = []
    def record(wrapper, pid):
        read.append(wrapper.node.name)
        c = control()
        c.update(id=wrapper.node.name, name=wrapper.node.name)
        return c
    monkeypatch.setattr(access.uia, '_record', record)
    obs, _ = access._tree({'hwnd':100, 'pid':10}, lambda:None)
    assert read == ['window', 'visible-file']
    assert [c['name'] for c in obs['controls']] == read
    assert not obs['truncated']


@pytest.mark.parametrize('unknown', [False, True])
def test_tooltip_is_not_a_dialog_but_unknown_owned_popup_still_blocks(monkeypatch, unknown):
    o = observation()
    o['window']['title'] = 'folder'
    def enum(visit, arg):
        visit(201, arg)
        if unknown:
            visit(202, arg)
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(EnumWindows=enum,
        IsWindowVisible=lambda h:True, GetClassName=lambda h:'tooltips_class32' if h==201 else '#32770'))
    monkeypatch.setattr(access, '_owned', lambda hwnd, root:True)
    monkeypatch.setattr(access.uia, '_recheck', lambda w:w)
    monkeypatch.setattr(access.uia, '_identity', lambda hwnd:{
        'hwnd': hwnd, 'title': 'Attention', 'class_name': '#32770', 'pid': 10})
    monkeypatch.setattr(access, '_kind', lambda w:'explorer')
    monkeypatch.setattr(access, '_tree', lambda w, check:(deepcopy(o), {}))
    actual, _ = access._observe(o['window'], lambda:None)
    assert bool(actual.get('blocked_reason')) == unknown


@pytest.fixture
def find_native(monkeypatch):
    """Synthetic COM wrappers; never inspect or control a desktop."""
    combo = control()
    combo.update(id='combo', runtime_id=[10, 20], type='ComboBox', name='查找内容(N)',
                 automation_id='', owner_hwnd=200, patterns=['set_value'], value='')
    parent = {k: combo[k] for k in ('type', 'name', 'automation_id')}
    editor = {**combo, 'id': 'editor', 'runtime_id': [10, 30], 'type': 'Edit',
              'name': '', 'ancestors': [parent]}
    dialog = {'hwnd': 200, 'pid': 10}
    obs = observation(combo)
    obs.update(application='wps', dialogs=[dialog])
    obs['controls'].append(editor)
    state = SimpleNamespace(parent_ids=[[10, 20]], parent_checks=0, recorded=[], writes=[])
    combo_element = SimpleNamespace(GetRuntimeId=lambda: [10, 20])
    edit_element = SimpleNamespace(GetRuntimeId=lambda: [10, 30])
    def unexpected_write(value):
        state.writes.append(value)
        raise AssertionError('changed find target must stop before SetValue')
    wrappers = {
        'combo': SimpleNamespace(element_info=SimpleNamespace(element=combo_element)),
        'editor': SimpleNamespace(element_info=SimpleNamespace(element=edit_element),
                                  iface_value=SimpleNamespace(CurrentIsReadOnly=False, SetValue=unexpected_write)),
    }
    def get_parent(element):
        assert element is edit_element
        index = min(state.parent_checks, len(state.parent_ids) - 1)
        runtime_id = state.parent_ids[index]
        state.parent_checks += 1
        return SimpleNamespace(GetRuntimeId=lambda: runtime_id)
    walker = SimpleNamespace(GetParentElement=get_parent)
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(
        NoPatternInterfaceError=NoPatternInterfaceError,
        IUIA=lambda: SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker))))
    native_fixture(monkeypatch, obs, wrappers)
    monkeypatch.setattr(access, '_owned', lambda *args: True)
    monkeypatch.setattr(access, '_process_owned', lambda *args: True)
    def record(wrapper, pid):
        key = 'combo' if wrapper is wrappers['combo'] else 'editor'
        state.recorded.append(key)
        source = combo if key == 'combo' else editor
        return {k: v for k, v in source.items() if k not in {'ancestors', 'owner_hwnd'}}
    monkeypatch.setattr(access.uia, '_record', record)
    state.combo, state.editor, state.record = combo, editor, record
    state.request = {'op': 'act', 'window': obs['window'], 'before_sha': digest(obs),
                     'control': deepcopy(combo), 'action': 'set_value', 'value': 'synthetic query'}
    return state


def test_changed_approved_combo_stops_before_intent_and_edit_write(monkeypatch, tmp_path, find_native):
    state = find_native
    def changed(wrapper, pid):
        record = state.record(wrapper, pid)
        return {**record, 'type': 'Document', 'name': 'changed'} if record['id'] == 'combo' else record
    monkeypatch.setattr(access.uia, '_record', changed)
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(state.request, tmp_path, lambda: None, [0])
    assert 'combo' in state.recorded and not state.writes
    assert not (tmp_path / 'native-intent.json').exists()


def test_editor_under_other_runtime_parent_stops_before_intent(tmp_path, find_native):
    state = find_native
    state.parent_ids = [[999, 888]]
    with pytest.raises(ComputerError):
        access._perform(state.request, tmp_path, lambda: None, [0])
    assert state.parent_checks and not state.writes
    assert not (tmp_path / 'native-intent.json').exists()


def test_editor_reparented_after_intent_stops_before_write(tmp_path, find_native):
    state = find_native
    state.parent_ids = [[10, 20], [10, 20], [999, 888]]
    with pytest.raises(ComputerError):
        access._perform(state.request, tmp_path, lambda: None, [0])
    assert state.parent_checks >= 2 and not state.writes
    assert (tmp_path / 'native-intent.json').exists()


def test_winapp_value_route_is_native_and_recorded_before_write(monkeypatch, tmp_path, find_native):
    from agent_service import windows_winapp
    state = find_native
    state.request['provider'] = windows_winapp.PROVIDER_BINDING
    def no_cli(*args, **kwargs):
        raise AssertionError('value route must not instantiate CLI')
    monkeypatch.setattr(windows_winapp, 'WinAppReader', no_cli)
    with pytest.raises(AssertionError, match='changed find target must stop before SetValue'):
        access._perform(state.request, tmp_path, lambda: None, [0])
    assert state.writes == ['synthetic query']
    intent = json.loads((tmp_path / 'native-intent.json').read_text(encoding='utf-8'))
    assert intent['route'] == 'native-ValuePattern'


SELECTOR = {'type': 'ListItem', 'name': 'existing.xlsx', 'automation_id': '1'}
ANCESTOR = {'type': 'List', 'name': 'Authorized files', 'automation_id': 'files'}


@pytest.fixture
def offscreen_tree(monkeypatch):
    """A complete synthetic ControlView tree with independently logged reads."""
    class Node:
        def __init__(self, id, *, name='', type='Pane', automation_id='', hidden=False, children=()):
            self.id, self.name, self.type, self.automation_id = id, name, type, automation_id
            self.CurrentIsOffscreen, self.CurrentIsPassword = hidden, False
            self.children, self.next = list(children), None
            for first, second in zip(self.children, self.children[1:]):
                first.next = second
    class Wrapper:
        def __init__(self, node):
            self.node = node
            self.element_info = SimpleNamespace(element=node, process_id=10, name=node.name,
                                                control_type=node.type, automation_id=node.automation_id)
        @property
        def iface_scroll_item(self):
            if self.node.type != 'ListItem':
                raise NoPatternInterfaceError()
            return SimpleNamespace(ScrollIntoView=lambda: None)
    authorized = Node('authorized', **SELECTOR, hidden=True)
    other = Node('other', **SELECTOR, hidden=True)
    secret = Node('secret', type='ListItem', name='unrequested secret', hidden=True)
    good_parent = Node('good-parent', **ANCESTOR, hidden=True, children=[authorized, secret])
    bad_parent = Node('bad-parent', type='List', name='Other files', hidden=True, children=[other])
    root = Node('root', type='Window', name='folder', children=[good_parent, bad_parent])
    walker = SimpleNamespace(GetFirstChildElement=lambda n: n.children[0] if n.children else None,
                             GetNextSiblingElement=lambda n: n.next)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Desktop=lambda **kw:
        SimpleNamespace(window=lambda **kw: SimpleNamespace(wrapper_object=lambda: Wrapper(root)))))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(
        NoPatternInterfaceError=NoPatternInterfaceError,
        IUIA=lambda: SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker))))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_element_info', SimpleNamespace(UIAElementInfo=lambda n: n))
    monkeypatch.setitem(sys.modules, 'pywinauto.controls.uiawrapper', SimpleNamespace(UIAWrapper=Wrapper))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(GetForegroundWindow=lambda: 100))
    monkeypatch.setattr(access.uia, '_recheck', lambda w: w)
    read = []
    def record(wrapper, pid):
        node = wrapper.node
        read.append(node.id)
        c = control()
        c.update(id=node.id, name=node.name, type=node.type, automation_id=node.automation_id,
                 visible=not node.CurrentIsOffscreen)
        return c
    monkeypatch.setattr(access.uia, '_record', record)
    return SimpleNamespace(read=read, authorized=authorized, root=root)


@pytest.mark.parametrize('reject,hidden', [(True,False),(False,False),(True,True)])
def test_vision_tree_password_guard_does_not_read_value(offscreen_tree,reject,hidden):
    offscreen_tree.authorized.CurrentIsPassword=True
    offscreen_tree.authorized.CurrentIsOffscreen=hidden
    if reject and not hidden:
        with pytest.raises(ComputerError,match='windows_vision_password_surface'):
            access._tree({'hwnd':100,'pid':10},lambda:None,reject_password=reject)
    else:
        access._tree({'hwnd':100,'pid':10},lambda:None,reject_password=reject)
    assert 'authorized' not in offscreen_tree.read


def test_target_tree_only_reads_offscreen_match_with_authorized_ancestor(offscreen_tree):
    selector = {**SELECTOR, 'ancestor': ANCESTOR}
    observed, _ = access._tree({'hwnd': 100, 'pid': 10}, lambda: None, selector)
    assert offscreen_tree.read == ['root', 'authorized']
    found = next(c for c in observed['controls'] if c['id'] == 'authorized')
    assert found['ancestors'] == [
        {'type': 'Window', 'name': 'folder', 'automation_id': ''}, ANCESTOR]
    assert 'reveal' in found['patterns'] and not found['visible']


def test_unscoped_tree_never_reads_offscreen_matches(offscreen_tree):
    observed, _ = access._tree({'hwnd': 100, 'pid': 10}, lambda: None)
    assert offscreen_tree.read == ['root']
    assert [c['id'] for c in observed['controls']] == ['root']


@pytest.mark.parametrize('kind', ['missing', 'wrong', 'duplicate'])
def test_reveal_permission_requires_unique_exact_authorized_target(kind):
    c = control()
    c.update(visible=False, patterns=['select', 'reveal'])
    o = observation(c)
    o['target_selector'] = dict(SELECTOR)
    if kind == 'missing':
        o.pop('target_selector')
    elif kind == 'wrong':
        o['target_selector']['name'] = 'different.xlsx'
    else:
        o['controls'].append({**c, 'id': 'second'})
    assert not access.allowed(o, c, 'reveal', '')


def test_offscreen_candidates_only_offer_reveal():
    c = control()
    c.update(visible=False, selected=True, patterns=['select', 'reveal'])
    o = observation(c)
    o['target_selector'] = dict(SELECTOR)
    target = {'selector': SELECTOR, 'state': 'selected', 'value': True}
    assert access.candidates(o, target) == [{'control_id': 'item', 'action': 'reveal', 'value': ''}]
    assert not access.allowed(o, c, 'select', '')


@pytest.fixture
def reveal_native(monkeypatch):
    c = control()
    c.update(visible=False, selected=True, patterns=['select', 'reveal'], ancestors=[ANCESTOR])
    selector = {**SELECTOR, 'ancestor': ANCESTOR}
    o = observation(c)
    o['target_selector'] = selector
    state = SimpleNamespace(c=c, observation=o, scrolls=0, selectors=[], mutate=None)
    def scroll():
        state.scrolls += 1
        c['visible'] = True
        if state.mutate:
            state.mutate()
    wrapper = SimpleNamespace(iface_scroll_item=SimpleNamespace(ScrollIntoView=scroll))
    native_fixture(monkeypatch, o, {'item': wrapper})
    def observe(window, check, target_selector=None):
        state.selectors.append(deepcopy(target_selector))
        return deepcopy(o), {'item': wrapper}
    monkeypatch.setattr(access, '_observe', observe)
    monkeypatch.setattr(access.uia, '_record', lambda wrapper, pid:
        {**{k: v for k, v in deepcopy(c).items() if k != 'ancestors'}, 'patterns': ['select']})
    # Make postcondition failures immediate, while still running the real loop.
    ticks = iter([0, 4, 8, 12, 16, 20])
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(access.time, 'sleep', lambda seconds: None)
    state.request = {'op': 'act', 'window': o['window'], 'control': deepcopy(c),
                     'before_sha': digest(o), 'selector': deepcopy(selector), 'action': 'reveal', 'value': ''}
    return state


def test_reveal_reuses_targeted_sha_and_preserves_business_state(tmp_path, reveal_native):
    s = reveal_native
    result = access._perform(s.request, tmp_path, lambda: None, [0])
    assert result['ok'] and s.scrolls == 1
    assert s.selectors == [s.request['selector']] * 3
    assert s.c['selected'] is True and s.c['value'] is None
    assert (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('changed', ['selected', 'ancestor', 'missing', 'hidden'])
def test_reveal_postcondition_does_not_accept_changed_or_substitute_target(tmp_path, reveal_native, changed):
    s = reveal_native
    def mutate():
        if changed == 'selected':
            s.c['selected'] = False
        elif changed == 'ancestor':
            s.c['ancestors'] = [{**ANCESTOR, 'name': 'Other files'}]
        elif changed == 'missing':
            s.observation['controls'] = []
        else:
            s.c['visible'] = False
    s.mutate = mutate
    result = access._perform(s.request, tmp_path, lambda: None, [0])
    assert result['state'] == 'unknown' and result['effects'] == 'unknown'
    assert s.scrolls == 1


def test_reveal_changed_sha_never_calls_scroll_or_writes_intent(tmp_path, reveal_native):
    s = reveal_native
    s.request['before_sha'] = 'stale'
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(s.request, tmp_path, lambda: None, [0])
    assert s.scrolls == 0 and not (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('change_at', [1, 2, 3])
def test_reveal_rechecks_native_target_before_intent_and_immediately_before_scroll(
        monkeypatch, tmp_path, reveal_native, change_at):
    s = reveal_native
    reads = []
    def record(wrapper, pid):
        reads.append(True)
        current = {k: v for k, v in deepcopy(s.c).items() if k != 'ancestors'}
        current['patterns'] = ['select']
        if len(reads) == change_at:
            current['selected'] = False
        return current
    monkeypatch.setattr(access.uia, '_record', record)
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(s.request, tmp_path, lambda: None, [0])
    assert s.scrolls == 0
    assert (tmp_path / 'native-intent.json').exists() == (change_at == 3)


def test_native_scroll_pattern_absence_is_distinct_from_provider_failure(monkeypatch):
    monkeypatch.setattr(access.uia, '_record', lambda wrapper, pid: control())
    class Missing:
        @property
        def iface_scroll_item(self):
            raise NoPatternInterfaceError()
    class Failed:
        @property
        def iface_scroll_item(self):
            raise RuntimeError('provider unavailable')
    assert 'reveal' not in access._native_record(Missing(), 10)['patterns']
    with pytest.raises(RuntimeError, match='provider unavailable'):
        access._native_record(Failed(), 10)


def test_backend_target_observe_and_act_share_selector_then_cleanup(monkeypatch, tmp_path):
    backend = access.WindowsBackend(tmp_path)
    captured = []
    c = control()
    o = observation(c)
    o['target_selector'] = dict(SELECTOR)
    def call(request, **kwargs):
        captured.append(deepcopy(request))
        return {'ok': True, 'observation': deepcopy(o)}
    monkeypatch.setattr(backend, '_call', call)
    assert backend.observe_target(o['window'], SELECTOR) == o
    backend.act(o['window'], c, 'select', '', digest(o), tmp_path)
    assert captured[0]['selector'] == captured[1]['selector'] == SELECTOR
    backend.end_run()
    backend.act(o['window'], c, 'select', '', digest(o), tmp_path)
    assert 'selector' not in captured[-1]


@pytest.mark.parametrize('seconds',[20,.2])
@pytest.mark.parametrize('kind',['form_act','form_observe','form_snapshot','nav_act','invalid_form'])
def test_only_reviewed_form_act_doubles_worker_and_supervisor_deadlines(monkeypatch,tmp_path,seconds,kind):
    from agent_service import windows_visual_forms as forms, windows_visual as visual
    import run_jarvis_desktop
    from agent_service import computer_host, process as process_tools
    clock={'now':0.0};commands=[]
    root=tmp_path/'native';duration=seconds*1.5
    class Process:
        def __init__(self):
            self.done=False
            self.stdin=SimpleNamespace(write=lambda value:None,flush=lambda:None,close=lambda:None)
        def poll(self):
            if not self.done and clock['now']>=duration:
                self.done=True
                (root/'native-outcome.json').write_text(json.dumps({'ok':True,'state':'verified','effects':'verified'}),encoding='utf-8')
            return 0 if self.done else None
        def wait(self,timeout):
            if not self.done and timeout==.5:raise access.subprocess.TimeoutExpired('fake',timeout)
            return 0
        def terminate(self):self.done=True
        def kill(self):self.done=True
    def launch(command,**kwargs):
        commands.append(command)
        (root/'native-ready.json').write_text('{"pid":123}',encoding='utf-8')
        return Process()
    monkeypatch.setattr(access.subprocess,'Popen',launch)
    monkeypatch.setattr(access,'_owned_worker',lambda *args:123)
    monkeypatch.setattr(access,'_input_epoch',lambda:71)
    monkeypatch.setattr(process_tools,'WindowsJob',lambda process:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(run_jarvis_desktop,'desktop_python',lambda:'python-offline')
    monkeypatch.setattr(computer_host,'grant_foreground',lambda pid:True)
    monkeypatch.setattr(access.time,'monotonic',lambda:clock['now'])
    monkeypatch.setattr(access.time,'sleep',lambda delay:clock.update(now=clock['now']+delay))
    selector={'type':forms.TYPE,'automation_id':forms.ID,'name':'查找内容(N)'}
    request={'op':'act','selector':selector,'action':forms.ACTION}
    if kind=='form_observe':request['op']='observe'
    elif kind=='form_snapshot':request={'op':'visual_form_snapshot'}
    elif kind=='nav_act':request={'op':'act','action':visual.ACTION,'selector':{'type':visual.TYPE,'automation_id':visual.ID,'name':'视图'}}
    elif kind=='invalid_form':request['selector']={**selector,'automation_id':'unreviewed'}
    result=access.WindowsBackend(tmp_path,seconds=seconds)._call(request,root=root)
    expected=seconds*(2 if kind=='form_act' else 1)
    assert float(commands[0][commands[0].index('--seconds')+1])==expected
    assert result['ok'] is (kind=='form_act')
    assert (root/'cancel.flag').exists() is (kind!='form_act')
    assert expected<=clock['now']+.05 or kind=='form_act'


@pytest.mark.parametrize('case', ['exact', 'other_pid', 'title', 'layered_only', 'tool_only',
                                  'wrong_class', 'class_case', 'explorer'])
def test_only_exact_owned_wps_shadow_is_ignored(monkeypatch, case):
    o = observation()
    o['window']['title'] = 'Document'
    class_name = 'KLiteMainWindowShadowBorder'
    if case == 'wrong_class':
        class_name = '#32770'
    elif case == 'class_case':
        class_name = class_name.lower()
    title = 'Attention' if case == 'title' else ''
    pid = 99 if case == 'other_pid' else 10
    style = 0x80000 if case == 'layered_only' else 0x80 if case == 'tool_only' else 0x80080
    inspected = []
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        EnumWindows=lambda visit, arg: visit(201, arg), IsWindowVisible=lambda hwnd: True,
        GetClassName=lambda hwnd: class_name, GetWindowLong=lambda hwnd, index: style,
        GetWindowText=lambda hwnd: title, IsWindowEnabled=lambda hwnd: True))
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (1, pid)))
    monkeypatch.setattr(access, '_owned', lambda hwnd, root: True)
    monkeypatch.setattr(access, '_process_owned', lambda child, parent: child['pid'] == parent['pid'])
    monkeypatch.setattr(access, '_kind', lambda w: 'explorer' if case == 'explorer' else 'wps')
    monkeypatch.setattr(access.uia, '_recheck', lambda w: w)
    def identity(hwnd):
        inspected.append(hwnd)
        return {'hwnd': hwnd, 'pid': pid, 'class_name': class_name, 'title': title}
    monkeypatch.setattr(access.uia, '_identity', identity)
    monkeypatch.setattr(access, '_tree', lambda w, check: (deepcopy(o), {}))
    actual, _ = access._observe(o['window'], lambda: None)
    if case == 'exact':
        assert not inspected and not actual.get('blocked_reason')
    else:
        assert inspected == [201] and actual['blocked_reason'] == 'owned_window_requires_manual_attention'
        assert actual['blocked_windows'] == [{'hwnd': 201, 'class_name': class_name}]


def test_unreadable_owned_popup_remains_blocked_with_basic_diagnostics(monkeypatch):
    o = observation()
    o['window']['title'] = 'Document'
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        EnumWindows=lambda visit, arg: visit(201, arg), IsWindowVisible=lambda hwnd: True,
        GetClassName=lambda hwnd: '#32770', GetWindowLong=lambda hwnd, index: 0x80 if index == -20 else 1,
        IsWindowEnabled=lambda hwnd: False))
    monkeypatch.setattr(access, '_owned', lambda hwnd, root: True)
    monkeypatch.setattr(access, '_kind', lambda w: 'wps')
    monkeypatch.setattr(access.uia, '_recheck', lambda w: w)
    def unreadable(hwnd):
        raise ComputerError('window_unreadable')
    monkeypatch.setattr(access.uia, '_identity', unreadable)
    monkeypatch.setattr(access, '_tree', lambda w, check: (deepcopy(o), {}))
    actual, _ = access._observe(o['window'], lambda: None)
    assert actual['blocked_reason'] == 'owned_window_requires_manual_attention'
    assert actual['blocked_windows'] == [{'hwnd': 201, 'class_name': '#32770'}]
