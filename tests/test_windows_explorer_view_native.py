"""Offline native-role contracts; actual Explorer acceptance is independent."""
from copy import deepcopy
from hashlib import sha256
import json
import sys
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_explorer_view_native as native

WINDOW = {'hwnd': 10, 'pid': 20, 'created': 'now', 'image': 'C:\\Windows\\explorer.exe',
          'class_name': 'CabinetWClass', 'profile': 'explorer', 'title': 'fixture - Explorer'}


def identified(runtime, **values):
    return {'id': sha256(json.dumps(runtime).encode()).hexdigest()[:24], 'runtime_id': runtime, **values}


def parent(number, kind, automation_id):
    return {'runtime_id': [42, number], 'type': kind, 'name': '', 'automation_id': automation_id}


def hwnd(number, cls, parent_number=10):
    return {'hwnd': number, 'pid': 20, 'class_name': cls, 'root_hwnd': 10, 'parent_hwnd': parent_number}


def entry(number, role, kind, label, autoid, patterns, value, owner):
    control = identified([42, number], type=kind, name=label, automation_id=autoid, enabled=True,
                         visible=True, patterns=patterns, value=value,
                         ancestors=[{k: owner[k] for k in ('type', 'name', 'automation_id')}], bounds=[number, 1, number + 10, 20])
    return {'role': role, 'label': label, 'control': control, 'parent': owner}


def raw_fact(runtime, kind, cls, autoid, native, box, *, control=False):
    return {'runtime_id': runtime, 'type': kind, 'class_name': cls, 'automation_id': autoid,
            'native_hwnd': native, 'pid': 20, 'offscreen': False, 'is_control': control, 'bounds': box}


@pytest.fixture
def source():
    pane_parent = parent(80, 'Pane', '')
    markers = [identified([20, number, 0], type=kind, automation_id=autoid, parent=deepcopy(pane_parent), class_name=cls)
               for number, kind, autoid, cls in [(1001, 'Image', 'PreviewThumbnailInner', 'PreviewThumbnailInner'),
                                                 (1002, 'Text', 'RPaneCoverSheet_NoSel', 'Element')]]
    result = {'window': deepcopy(WINDOW), 'application': 'explorer', 'kind': 'explorer_view',
            'binding': {'tab': identified([42, 60], type='TabItem', automation_id='', selected=True),
                'scene': {'title': WINDOW['title'], 'rect': [0, 0, 900, 600]},
                'items': identified([42, 70], type='List', automation_id='', native=hwnd(70, 'DirectUIHWND', 71),
                                    parent=parent(71, 'Pane', 'listview'), parent_native=hwnd(71, 'SHELLDLL_DefView'), selection_count=0),
                'preview': {'visible': True, 'host': hwnd(80, 'DUIViewWndClassName', 81), 'markers': markers}},
            'controls': [entry(50, 'search_text', 'Edit', 'Search fixture', 'TextBox', ['focus', 'set_value'], '',
                               parent(51, 'Group', 'FileExplorerSearchBox')),
                         entry(52, 'preview_pane', 'Button', 'Preview', 'PreviewPaneToggleButton', ['focus', 'invoke'], None,
                               parent(53, 'AppBar', 'FileExplorerSecondaryCommandBar'))]}
    shared = [0, 100, 900, 600]
    result['binding']['preview']['layout'] = {
        'pane': raw_fact([20, 900, 0], 50025, 'TemplateBackground', 'ReadingPane', 0, [603, 100, 900, 550]),
        'ancestors': [raw_fact([20, 901, 0], 50033, 'Element', 'FolderLayoutContainer', 0, shared),
                      raw_fact([42, 83], 50033, 'HWNDView', 'main', 83, shared),
                      raw_fact([42, 80], 50033, 'DUIViewWndClassName', '', 80, shared, control=True),
                      raw_fact([42, 81], 50033, 'ShellTabWindowClass', '', 81, shared, control=True),
                      raw_fact([42, 10], 50032, 'CabinetWClass', '', 10, [0, 0, 900, 600], control=True)],
        'items_bounds': [100, 100, 600, 550]}
    return result


def test_static_validate_never_reads_ui_or_shell(source, monkeypatch):
    monkeypatch.setattr(native.explorer, '_shell_collection', lambda: pytest.fail('no Shell calls'))
    monkeypatch.setattr(native.explorer, '_guard', lambda *args, **kwargs: pytest.fail('static validation'))
    assert native.validate(source) == source
    source['native_proof'] = {'common_proves_this': True}
    assert native.validate(source) == source


@pytest.mark.parametrize('case', ['open_preview', 'unknown_preview', 'selected_file', 'wrong_search_parent',
                                  'address_field', 'wrong_button_parent', 'arbitrary_button', 'native_root',
                                  'marker_parent', 'missing_marker', 'source_kind', 'runtime', 'tab', 'scene',
                                  'button_text', 'extra', 'malformed'])
def test_validate_denies_unproved_or_forged_sources(source, case):
    field, button = source['controls']
    if case == 'open_preview': source['binding']['preview'] = {'visible': False, 'host': None, 'markers': []}
    elif case == 'unknown_preview': source['binding']['preview'] = None
    elif case == 'selected_file': source['binding']['items']['selection_count'] = 1
    elif case == 'wrong_search_parent': field['parent']['automation_id'] = 'PART_AutoSuggestBox'
    elif case == 'address_field': field['control']['automation_id'] = 'AddressBox'
    elif case == 'wrong_button_parent': button['parent']['automation_id'] = 'UntrustedBar'
    elif case == 'arbitrary_button': button['control']['automation_id'] = 'DeleteButton'
    elif case == 'native_root': source['binding']['preview']['host']['root_hwnd'] = 999
    elif case == 'marker_parent': source['binding']['preview']['markers'][0]['parent']['runtime_id'] = [42, 999]
    elif case == 'missing_marker': source['binding']['preview']['markers'].pop()
    elif case == 'source_kind': source['kind'] = 'generic'
    elif case == 'runtime': button['control']['runtime_id'][1] += 1
    elif case == 'tab': source['binding']['tab']['selected'] = False
    elif case == 'scene': source['binding']['scene']['rect'] = [0, 0, 0, 1]
    elif case == 'button_text': button['label'] = 'Arbitrary'
    elif case == 'extra': source['open_file'] = True
    elif case == 'malformed': source['controls'] = [None]
    with pytest.raises(ComputerError): native.validate(source)


@pytest.fixture
def live(source, monkeypatch):
    state = {'value': '', 'preview': deepcopy(source['binding']['preview']), 'items': deepcopy(source['binding']['items']),
             'tab': deepcopy(source['binding']['tab']), 'calls': [], 'foreground': False, 'popup': False,
             'host_exists': True, 'host_visible': True, 'replacement': False, 'host_pid': 20, 'activate': [], 'read_only': False,
             'pane_visible': True}
    def guard(*args, **kwargs):
        if state['popup']: raise ComputerError('windows_explorer_popup_requires_attention')
        if kwargs.get('foreground') and not state['foreground']: raise ComputerError('windows_explorer_foreground_required')
        return deepcopy(WINDOW)
    def scene(*args):
        guard()
        return deepcopy(source['binding']['scene'])
    def prepare(*args):
        guard(); state['activate'].append(10); state['foreground'] = True
    class Value:
        @property
        def CurrentValue(self): return state['value']
        @property
        def CurrentIsReadOnly(self): return state['read_only']
        def SetValue(self, value):
            state['calls'].append(('SetValue', value)); state['value'] = value
    def invoke():
        state['calls'].append(('Invoke', None)); state['preview'] = None
        state['pane_visible'] = False
    wrappers = {}
    for role in source['controls']:
        wrappers[role['control']['id']] = SimpleNamespace(
            iface_value=Value(), iface_invoke=SimpleNamespace(Invoke=invoke), owner=deepcopy(role['parent']))
    def roles(window, check, *, source_preview=None):
        guard(); check()
        entries = deepcopy(source['controls']); entries[0]['control']['value'] = state['value']
        preview = deepcopy(state['preview'])
        if preview is None:
            if source_preview is not None:
                native._closed_original({'binding': {'preview': source_preview}}, check)
                preview = {'visible': False, 'host': None, 'markers': []}
            else: entries = entries[:1]
        return entries, deepcopy(state['tab']), wrappers, {'items': deepcopy(state['items']), 'preview': preview}
    gui = SimpleNamespace(
        IsWindow=lambda h: state['host_exists'] if h == 80 else True,
        IsWindowVisible=lambda h: state['host_visible'] if h == 80 else h == 82 and state['replacement'],
        GetClassName=lambda h: 'DUIViewWndClassName' if h in (80, 82) else 'ShellTabWindowClass',
        GetAncestor=lambda h, flag: 10, GetParent=lambda h: 81 if h in (80, 82) else 10,
        EnumChildWindows=lambda h, callback, unused: [callback(item, None) for item in (80, 82)] if h == 81 else [])
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (0, state['host_pid'] if h == 80 else 20)))
    monkeypatch.setattr(native.explorer, '_guard', guard)
    monkeypatch.setattr(native.explorer, '_prepare', prepare)
    monkeypatch.setattr(native.explorer, '_parent', lambda wrapper, pid: deepcopy(wrapper.owner))
    monkeypatch.setattr(native, '_scene', scene)
    monkeypatch.setattr(native, '_roles', roles)
    def closed(original, check, *, items=None):
        check()
        if 'layout' not in original['binding']['preview']:
            raise ComputerError('windows_explorer_view_preview_new_source_required')
        if state['pane_visible'] or state['replacement'] or state['host_pid'] != 20 or state['popup']:
            raise ComputerError('windows_explorer_view_preview_close_unproven')
        return {'test_full_pane_proof': True}
    monkeypatch.setattr(native, '_closed_original', closed)
    return state, wrappers


def test_search_writes_once_readback_and_no_enter_or_shell(live, source, monkeypatch):
    state, _ = live; original = deepcopy(source)
    monkeypatch.setattr(native.explorer, '_send_enter', lambda *args: pytest.fail('must not Enter'))
    monkeypatch.setattr(native.explorer, '_shell_collection', lambda: pytest.fail('must not Shell'))
    assert native.observe(WINDOW, source, 'search_text', 'fixture phrase', lambda: None)['verified'] is False
    assert state['activate'] == []
    native.act(WINDOW, source, 'search_text', 'fixture phrase', lambda: None, [0])
    assert state['calls'] == [('SetValue', 'fixture phrase')]
    assert native.observe(WINDOW, source, 'search_text', 'fixture phrase', lambda: None)['verified'] is True
    assert source == original


def test_close_preview_invokes_once_then_independently_proves_hidden_host(live, source):
    state, _ = live
    assert native.observe(WINDOW, source, 'preview_pane', '', lambda: None)['verified'] is False
    native.act(WINDOW, source, 'preview_pane', '', lambda: None, [0])
    assert state['calls'] == [('Invoke', None)]
    after = native.observe(WINDOW, source, 'preview_pane', '', lambda: None)
    assert after['verified'] is True and after['facts']['binding']['preview']['visible'] is False
    with pytest.raises(ComputerError, match='preview_changed'):
        native.act(WINDOW, source, 'preview_pane', '', lambda: None, [0])
    assert len(state['calls']) == 1


@pytest.mark.parametrize('effect', ['markers_only_missing', 'replacement', 'reused', 'unknown_popup', 'file_selected', 'other_host'])
def test_preview_false_success_is_rejected(live, source, effect):
    state, _ = live
    state['preview'] = None
    state['host_visible'] = False
    if effect == 'markers_only_missing': state['host_visible'] = True
    elif effect == 'replacement': state['replacement'] = True
    elif effect == 'reused': state['host_pid'] = 999
    elif effect == 'unknown_popup': state['popup'] = True
    elif effect == 'file_selected': state['items']['selection_count'] = 1
    elif effect == 'other_host':
        state['preview'] = deepcopy(source['binding']['preview']); state['preview']['host']['hwnd'] = 999
    with pytest.raises(ComputerError): native.observe(WINDOW, source, 'preview_pane', '', lambda: None)
    assert state['calls'] == []


@pytest.mark.parametrize('change', ['tab', 'value', 'parent', 'read_only', 'popup'])
def test_pre_dispatch_changes_never_write_or_invoke(live, source, change):
    state, wrappers = live
    if change == 'tab': state['tab']['runtime_id'][1] += 1
    elif change == 'value': state['value'] = 'unrelated new draft'
    elif change == 'parent': wrappers[source['controls'][0]['control']['id']].owner['runtime_id'][1] += 1
    elif change == 'read_only': state['read_only'] = True
    elif change == 'popup': state['popup'] = True
    with pytest.raises(ComputerError): native.act(WINDOW, source, 'search_text', 'approved', lambda: None, [0])
    assert state['calls'] == []


@pytest.mark.parametrize('value', ['', '\n', 'a\x00b', 'a' * 301, None, ['text']])
def test_search_invalid_text_is_zero_dispatch(live, source, value):
    with pytest.raises(ComputerError): native.act(WINDOW, source, 'search_text', value, lambda: None, [0])
    assert live[0]['calls'] == []


def test_search_is_not_a_claim_about_search_results(live, source):
    state, _ = live
    state['value'] = 'some query'
    result = native.observe(WINDOW, source, 'search_text', 'some query', lambda: None)
    assert result['verified'] is True
    assert 'results' not in result['facts'] and 'directory' not in result['facts']


def test_absent_markers_never_offer_open_preview(live, source):
    state, _ = live
    state['preview'] = None
    state['host_visible'] = False
    current = native.snapshot(WINDOW, lambda: None)
    assert current['binding']['preview'] is None
    assert [entry['role'] for entry in current['controls']] == ['search_text']


def test_failed_value_readback_does_not_repeat(live, source):
    state, wrappers = live
    value = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue='',
                            SetValue=lambda text: state['calls'].append(('SetValue', text)))
    wrappers[source['controls'][0]['control']['id']].iface_value = value
    with pytest.raises(ComputerError, match='value_unverified'):
        native.act(WINDOW, source, 'search_text', 'wanted', lambda: None, [0])
    assert state['calls'] == [('SetValue', 'wanted')]


def test_snapshot_race_is_not_accepted(live, source, monkeypatch):
    roles = native._roles; calls = []
    def drift(*args, **kwargs):
        records, tab, wrappers, state = roles(*args, **kwargs)
        calls.append(True)
        if len(calls) > 1: records[0]['control']['value'] = 'raced'
        return records, tab, wrappers, state
    monkeypatch.setattr(native, '_roles', drift)
    with pytest.raises(ComputerError, match='screen_changed'): native.snapshot(WINDOW, lambda: None)


@pytest.fixture
def native_tree(source, monkeypatch):
    items = source['binding']['items']; preview = source['binding']['preview']
    records = [identified(items['runtime_id'], type='List', automation_id='', visible=True)]
    wrappers = {}
    item_element = SimpleNamespace(CurrentClassName='UIItemsView', CurrentNativeWindowHandle=70)
    item_wrapper = SimpleNamespace(element_info=SimpleNamespace(element=item_element),
                                   owner=deepcopy(items['parent']), native_parent=71,
                                   iface_selection=SimpleNamespace(GetCurrentSelection=lambda: SimpleNamespace(Length=0)))
    wrappers[records[0]['id']] = item_wrapper
    for marker in preview['markers']:
        records.append({**deepcopy(marker), 'visible': True})
        wrappers[marker['id']] = SimpleNamespace(owner=deepcopy(marker['parent']), native_parent=80,
            element_info=SimpleNamespace(element=SimpleNamespace(CurrentClassName=marker['class_name'])))
    monkeypatch.setattr(native.explorer, '_parent', lambda wrapper, pid: deepcopy(wrapper.owner))
    monkeypatch.setattr(native, '_native_parent', lambda wrapper: SimpleNamespace(CurrentNativeWindowHandle=wrapper.native_parent))
    def native_hwnd(number, window, cls, **kwargs):
        return hwnd(number, cls, 81 if number == 80 else 71 if number == 70 else 10)
    monkeypatch.setattr(native, '_native_hwnd', native_hwnd)
    monkeypatch.setattr(native, '_visible_children', lambda number: [80])
    monkeypatch.setattr(native, '_source_layout', lambda *args: deepcopy(source['binding']['preview']['layout']))
    return records, wrappers


def test_native_pair_and_empty_selection_are_independently_proven(native_tree, source):
    records, wrappers = native_tree
    result = native._view_state(WINDOW, {'controls': records}, wrappers, lambda: None)
    assert result == {key: source['binding'][key] for key in ('items', 'preview')}


@pytest.mark.parametrize('failure', ['one_marker', 'wrong_parent', 'wrong_class', 'other_visible_child'])
def test_native_pane_marker_lookalikes_rejected(native_tree, monkeypatch, failure):
    records, wrappers = native_tree
    if failure == 'one_marker': records.pop()
    elif failure == 'wrong_parent': wrappers[records[-1]['id']].owner['runtime_id'] = [42, 999]
    elif failure == 'wrong_class': wrappers[records[-1]['id']].element_info.element.CurrentClassName = 'Other'
    elif failure == 'other_visible_child': monkeypatch.setattr(native, '_visible_children', lambda h: [80, 82])
    with pytest.raises(ComputerError): native._view_state(WINDOW, {'controls': records}, wrappers, lambda: None)


def test_native_selected_file_disables_preview_inspection(native_tree):
    records, wrappers = native_tree
    wrappers[records[0]['id']].iface_selection.GetCurrentSelection = lambda: SimpleNamespace(Length=1)
    state = native._view_state(WINDOW, {'controls': records}, wrappers, lambda: None)
    assert state['preview'] is None and state['items']['selection_count'] == 1


def test_native_no_markers_is_unknown_instead_of_off(native_tree):
    records, wrappers = native_tree
    state = native._view_state(WINDOW, {'controls': records[:1]}, wrappers, lambda: None)
    assert state['preview'] is None


@pytest.mark.parametrize('value', ['Password', 'windows security', '登录资料', 'jarvis password'])
def test_protected_search_caption_is_rejected_before_activation_or_effect(live, source, value):
    with pytest.raises(ComputerError, match='text_invalid'):
        native.act(WINDOW, source, 'search_text', value, lambda: None, [0])
    assert live[0]['calls'] == [] and live[0]['activate'] == []


def test_live_search_caption_changes_preserve_window_tab_native_proofs(live, source, monkeypatch):
    state, _ = live
    root_ancestor = {'type': 'Window', 'name': WINDOW['title'], 'automation_id': ''}
    for entry in source['controls']:
        entry['control']['ancestors'].insert(0, deepcopy(root_ancestor))
    current_title = 'control_check_146 - Search results in fixture'
    roles = native._roles
    def current_roles(*args, **kwargs):
        records, tab, wrappers, binding = roles(*args, **kwargs)
        for record in records:
            record['control']['ancestors'][0]['name'] = current_title
        return records, tab, wrappers, binding
    monkeypatch.setattr(native, '_roles', current_roles)
    monkeypatch.setattr(native, '_scene', lambda *args: {'title': current_title, 'rect': source['binding']['scene']['rect']})
    state['value'] = 'control_check_146'
    after = native.observe(WINDOW, source, 'search_text', 'control_check_146', lambda: None)
    assert after['verified'] is True and after['facts']['binding']['scene']['title'] == current_title
    assert after['facts']['window'] == source['window']
    native.act(WINDOW, source, 'preview_pane', '', lambda: None, [0])
    assert state['calls'] == [('Invoke', None)]
    assert native.observe(WINDOW, source, 'preview_pane', '', lambda: None)['verified'] is True


def test_caption_compatibility_does_not_allow_unbound_ancestor(live, source, monkeypatch):
    for entry in source['controls']:
        entry['control']['ancestors'].insert(0, {'type': 'Window', 'name': WINDOW['title'], 'automation_id': ''})
    roles = native._roles
    def changed(*args, **kwargs):
        records, tab, wrappers, binding = roles(*args, **kwargs)
        records[0]['control']['ancestors'][0]['name'] = 'Unbound foreign window'
        return records, tab, wrappers, binding
    monkeypatch.setattr(native, '_roles', changed)
    with pytest.raises(ComputerError, match='control_changed'):
        native.act(WINDOW, source, 'search_text', 'control_check_146', lambda: None, [0])
    assert live[0]['calls'] == []


def test_caption_compatibility_keeps_geometry_gate(live, source, monkeypatch):
    monkeypatch.setattr(native, '_scene', lambda *args: {'title': 'Search results', 'rect': [1, 0, 900, 600]})
    with pytest.raises(ComputerError, match='scene_changed'):
        native.act(WINDOW, source, 'search_text', 'control_check_146', lambda: None, [0])
    assert live[0]['calls'] == []


def test_current_native_items_view_can_refresh_after_search(live, source):
    state, _ = live
    state['items']['native']['hwnd'] = 170
    state['items'].update(identified([42, 170]))
    state['value'] = 'control_check_146'
    after = native.observe(WINDOW, source, 'search_text', 'control_check_146', lambda: None)
    assert after['verified'] is True and after['facts']['binding']['items']['native']['hwnd'] == 170
    native.act(WINDOW, source, 'preview_pane', '', lambda: None, [0])
    assert state['calls'] == [('Invoke', None)]


@pytest.mark.parametrize('failures', [1, 2, 3])
def test_only_element_not_available_gets_three_bounded_read_attempts(live, source, monkeypatch, failures):
    current = native._current; calls = []
    class StaleElement(Exception): hresult = -2147220991
    def read(*args):
        calls.append(True)
        if len(calls) <= failures: raise StaleElement('test stale')
        return current(*args)
    monkeypatch.setattr(native, '_current', read)
    if failures == 3:
        with pytest.raises(ComputerError, match='element_unavailable') as error:
            native.observe(WINDOW, source, 'search_text', 'safe_query', lambda: None)
        assert error.value.content_diagnostic == {'hresult': -2147220991, 'read_attempts': 3}
    else:
        assert native.observe(WINDOW, source, 'search_text', 'safe_query', lambda: None)['verified'] is False
    assert len(calls) == min(failures + 1, 3)
    assert live[0]['calls'] == [] and live[0]['activate'] == []


def test_other_com_error_has_no_read_retry(live, source, monkeypatch):
    calls = []
    class RejectedCall(Exception): hresult = -2147418111
    def read(*args): calls.append(True); raise RejectedCall('not an unavailable element')
    monkeypatch.setattr(native, '_current', read)
    with pytest.raises(RejectedCall): native.observe(WINDOW, source, 'search_text', 'safe_query', lambda: None)
    assert len(calls) == 1 and live[0]['calls'] == []


@pytest.fixture
def reading_pane_closed(source, monkeypatch):
    """Actual closure helper with a native raw-tree/point provider, no action API."""
    from types import ModuleType
    layout = source['binding']['preview']['layout']
    tree = {}
    for index, facts in enumerate(layout['ancestors']):
        ancestor = tuple(layout['ancestors'][index + 1]['runtime_id']) if index + 1 < 5 else None
        tree[tuple(facts['runtime_id'])] = {'facts': deepcopy(facts), 'parent': ancestor}
    state = {'tree': tree, 'bounds': [100, 100, 900, 550], 'hits': [], 'overlay': False, 'selected': 0}
    def element(facts, owner=None):
        return SimpleNamespace(facts=facts, CurrentIsPassword=False, CurrentProcessId=20,
            CurrentNativeWindowHandle=facts['native_hwnd'], CurrentClassName=facts['class_name'],
            GetRuntimeId=lambda: facts['runtime_id'], owner=owner)
    current = raw_fact([42, 70], 50008, 'UIItemsView', '', 70, state['bounds'], control=True)
    item = element(current)
    hit = element(raw_fact([20, 333, 0], 50020, 'Element', '', 0, [650, 200, 700, 220]), item)
    def from_point(point):
        state['hits'].append([point.x, point.y])
        return element(raw_fact([20, 999, 0], 50033, 'Overlay', '', 0, state['bounds'])) if state['overlay'] else hit
    client = SimpleNamespace(ElementFromHandle=lambda h: item, ElementFromPoint=from_point,
                             RawViewWalker=SimpleNamespace(GetParentElement=lambda node: node.owner))
    definitions = ModuleType('pywinauto.uia_defines')
    definitions.IUIA = lambda: SimpleNamespace(iuia=client)
    definitions.get_elem_interface = lambda *args: SimpleNamespace(
        GetCurrentSelection=lambda: SimpleNamespace(Length=state['selected']))
    package = ModuleType('pywinauto'); package.__path__ = []
    monkeypatch.setitem(sys.modules, 'pywinauto', package)
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', definitions)
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(IsChild=lambda parent, child: parent == 80 and child == 70))
    monkeypatch.setattr(native, '_raw_fact', lambda elem: deepcopy(elem.facts))
    monkeypatch.setattr(native, '_raw_tree', lambda *args: deepcopy(state['tree']))
    monkeypatch.setattr(native, '_native_hwnd', lambda *args, **kwargs: deepcopy(source['binding']['preview']['host']))
    monkeypatch.setattr(native.explorer, '_guard', lambda *args, **kwargs: deepcopy(WINDOW))
    return state, current


def test_complete_reading_pane_region_is_replaced_by_native_items_view(reading_pane_closed, source):
    state, _ = reading_pane_closed
    result = native._closed_original(source, lambda: None, items=source['binding']['items'])
    assert result['pane_presence'] == 'absent'
    assert result['pane_bounds'] == [603, 100, 900, 550]
    assert result['items_bounds'] == [100, 100, 900, 550]
    assert len(state['hits']) == 9


@pytest.mark.parametrize('failure', ['partial_region', 'ancestor_changed', 'replacement_pane', 'overlay', 'selected'])
def test_marker_absence_alone_never_proves_complete_pane_closed(reading_pane_closed, source, failure):
    state, current = reading_pane_closed
    if failure == 'partial_region': current['bounds'] = [100, 100, 850, 550]
    elif failure == 'ancestor_changed': state['tree'][(42, 80)]['facts']['bounds'][2] -= 1
    elif failure == 'replacement_pane':
        pane = deepcopy(source['binding']['preview']['layout']['pane']); pane['runtime_id'] = [20, 999, 0]
        state['tree'][(20, 999, 0)] = {'facts': pane, 'parent': (20, 901, 0)}
    elif failure == 'overlay': state['overlay'] = True
    elif failure == 'selected': state['selected'] = 1
    with pytest.raises(ComputerError):
        native._closed_original(source, lambda: None, items=source['binding']['items'])


def test_original_reading_pane_can_be_structurally_offscreen(reading_pane_closed, source):
    state, _ = reading_pane_closed
    pane = deepcopy(source['binding']['preview']['layout']['pane']); pane['offscreen'] = True
    state['tree'][tuple(pane['runtime_id'])] = {'facts': pane, 'parent': (20, 901, 0)}
    assert native._closed_original(source, lambda: None, items=source['binding']['items'])['pane_presence'] == 'offscreen_or_empty'


def test_old_source_is_statically_readable_but_never_gets_new_close_authority(live, source):
    source['binding']['preview'].pop('layout')
    assert native.validate(source) == source
    with pytest.raises(ComputerError, match='new_source_required'):
        native.act(WINDOW, source, 'preview_pane', '', lambda: None, [0])
    assert live[0]['calls'] == [] and live[0]['activate'] == []


@pytest.mark.parametrize('value', ['jarvis_stage_149', 'Codex', 'ChatGPT', '贾维斯'])
def test_search_text_allows_brand_words(value):
    assert native.text(value) == value


def test_search_brand_query_writes_once_and_reads_back(live, source):
    state, _ = live
    native.act(WINDOW, source, 'search_text', 'jarvis_stage_149', lambda: None, [0])
    assert state['calls'] == [('SetValue', 'jarvis_stage_149')]
    assert native.observe(WINDOW, source, 'search_text', 'jarvis_stage_149', lambda: None)['verified'] is True


@pytest.mark.parametrize('value', ['jarvis password', 'Codex 登录', 'ChatGPT credential',
                                  '贾维斯 终端', '', 'x'*301, 'a\nb'])
def test_search_text_keeps_sensitive_and_length_guards(value):
    with pytest.raises(ComputerError, match='windows_explorer_view_text_invalid'):
        native.text(value)
