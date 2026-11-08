"""Reviewed Explorer search-field drafts, without Shell directory assumptions.

Only a proved native search editor is writable. The exact Preview button is
available only for an empty native item selection, with independently proved
preview host/markers. No Enter, document open, Shell automation or model calls.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re

from .computer import ComputerError
from . import computer_uia as uia, windows_access as access
from . import windows_explorer_native as explorer

_CONTROL = {'id', 'runtime_id', 'type', 'name', 'automation_id', 'value',
            'enabled', 'visible', 'patterns', 'ancestors', 'bounds'}
_PARENT = {'type', 'name', 'automation_id', 'runtime_id'}
_TAB = {'id', 'runtime_id', 'type', 'automation_id', 'selected'}
_PREVIEW_IDS = {'RPaneCoverSheet_NoSel', 'PreviewThumbnailInner'}
_ELEMENT_NOT_AVAILABLE = -2147220991  # UIA_E_ELEMENTNOTAVAILABLE (0x80040201).


def _native_hwnd(hwnd, window, class_name, *, parent_hwnd=None):
    import win32gui, win32process
    if (type(hwnd) is not int or hwnd <= 0 or hwnd == window['hwnd']
            or not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)
            or win32process.GetWindowThreadProcessId(hwnd)[1] != window['pid']
            or not win32gui.IsChild(window['hwnd'], hwnd)
            or win32gui.GetAncestor(hwnd, 2) != window['hwnd']
            or win32gui.GetClassName(hwnd) != class_name
            or (parent_hwnd is not None and win32gui.GetParent(hwnd) != parent_hwnd)):
        raise ComputerError('windows_explorer_view_native_parent_unproven')
    return {'hwnd': hwnd, 'pid': window['pid'], 'class_name': class_name,
            'root_hwnd': window['hwnd'], 'parent_hwnd': int(win32gui.GetParent(hwnd))}


def _native_parent(wrapper):
    from pywinauto.uia_defines import IUIA
    element = IUIA().iuia.ControlViewWalker.GetParentElement(wrapper.element_info.element)
    if not element:
        raise ComputerError('windows_explorer_view_native_parent_unproven')
    return element


def _visible_children(hwnd):
    import win32gui
    children = []
    def visit(child, _):
        if win32gui.GetParent(child) == hwnd and win32gui.IsWindowVisible(child):
            children.append(child)
    win32gui.EnumChildWindows(hwnd, visit, None)
    return sorted(children)


def _raw_fact(element):
    box = element.CurrentBoundingRectangle
    return {'runtime_id': [int(n) for n in element.GetRuntimeId()], 'type': int(element.CurrentControlType),
            'class_name': element.CurrentClassName, 'automation_id': element.CurrentAutomationId,
            'native_hwnd': int(element.CurrentNativeWindowHandle or 0), 'pid': int(element.CurrentProcessId),
            'offscreen': bool(element.CurrentIsOffscreen), 'is_control': bool(element.CurrentIsControlElement),
            'bounds': [int(box.left), int(box.top), int(box.right), int(box.bottom)]}


def _contains(outer, inner):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _source_layout(window, markers, wrappers, host, items_wrapper, check):
    """Bind the actual RawView ReadingPane, rather than its shared HWND host."""
    from pywinauto.uia_defines import IUIA
    from .windows_controls import bounds
    walker = IUIA().iuia.RawViewWalker
    chains = []
    for marker in markers:
        element = wrappers[marker['id']].element_info.element
        chain = []
        for _ in range(12):
            check()
            element = walker.GetParentElement(element)
            if not element or element.CurrentIsPassword:
                raise ComputerError('windows_explorer_view_preview_layout_unproven')
            facts = _raw_fact(element)
            if facts['pid'] != window['pid']:
                raise ComputerError('windows_explorer_view_preview_layout_unproven')
            chain.append(facts)
            if facts['native_hwnd'] == window['hwnd']:
                break
        chains.append(chain)
    if chains[0] != chains[1]:
        raise ComputerError('windows_explorer_view_preview_layout_unproven')
    chain = chains[0]
    if (len(chain) != 6 or [(f['type'], f['class_name'], f['automation_id']) for f in chain[:3]] !=
            [(50025, 'TemplateBackground', 'ReadingPane'), (50033, 'Element', 'FolderLayoutContainer'),
             (50033, 'HWNDView', 'main')]
            or [f['native_hwnd'] for f in chain[3:]] != [host['hwnd'], host['parent_hwnd'], window['hwnd']]
            or any(f['offscreen'] for f in chain) or chain[0]['is_control'] is not False):
        raise ComputerError('windows_explorer_view_preview_layout_unproven')
    pane, item_bounds = chain[0]['bounds'], bounds(items_wrapper)
    if (item_bounds is None or not _contains(chain[3]['bounds'], pane)
            or item_bounds[2] > pane[0] or pane[0] - item_bounds[2] > 64
            or item_bounds[1] != pane[1] or item_bounds[3] != pane[3]
            or pane[2] != chain[3]['bounds'][2]):
        raise ComputerError('windows_explorer_view_preview_layout_unproven')
    return {'pane': chain[0], 'ancestors': chain[1:], 'items_bounds': item_bounds}


def _view_state(window, observation, wrappers, check):
    """Native empty selection and paired no-file preview markers, never contents."""
    check()
    views = [item for item in observation['controls'] if item['type'] == 'List'
             and wrappers[item['id']].element_info.element.CurrentClassName == 'UIItemsView']
    if len(views) != 1:
        raise ComputerError('windows_explorer_view_items_ambiguous')
    item = views[0]
    wrapper = wrappers[item['id']]
    parent = explorer._parent(wrapper, window['pid'])
    parent_element = _native_parent(wrapper)
    if parent['type'] != 'Pane' or parent['automation_id'] != 'listview':
        raise ComputerError('windows_explorer_view_items_ambiguous')
    parent_native = _native_hwnd(int(parent_element.CurrentNativeWindowHandle), window, 'SHELLDLL_DefView')
    native = _native_hwnd(int(wrapper.element_info.element.CurrentNativeWindowHandle), window,
                          'DirectUIHWND', parent_hwnd=parent_native['hwnd'])
    count = wrapper.iface_selection.GetCurrentSelection().Length
    if type(count) is not int or not 0 <= count <= 4096:
        raise ComputerError('windows_explorer_view_selection_unavailable')
    items = {key: deepcopy(item[key]) for key in ('id', 'runtime_id', 'type', 'automation_id')}
    items.update(native=native, parent=parent, parent_native=parent_native, selection_count=count)
    if count:
        return {'items': items, 'preview': None}
    markers = [c for c in observation['controls'] if c['automation_id'] in _PREVIEW_IDS]
    if not markers:
        # Absence could mean loading or an unsupported layout, never permission
        # to infer "off" and toggle blindly. Only an original host's independently
        # proven closure can establish a False state during post-observation.
        return {'items': items, 'preview': None}
    if len(markers) != 2 or {c['automation_id'] for c in markers} != _PREVIEW_IDS:
        raise ComputerError('windows_explorer_view_preview_unproven')
    proofs, host = [], None
    for marker in sorted(markers, key=lambda c: c['automation_id']):
        wrapper = wrappers[marker['id']]
        expected_type = 'Image' if marker['automation_id'] == 'PreviewThumbnailInner' else 'Text'
        expected_class = 'PreviewThumbnailInner' if expected_type == 'Image' else 'Element'
        parent = explorer._parent(wrapper, window['pid'])
        parent_element = _native_parent(wrapper)
        current_host = _native_hwnd(int(parent_element.CurrentNativeWindowHandle), window, 'DUIViewWndClassName')
        _native_hwnd(current_host['parent_hwnd'], window, 'ShellTabWindowClass')
        if (not marker['visible'] or marker['type'] != expected_type or parent['type'] != 'Pane'
                or wrapper.element_info.element.CurrentClassName != expected_class
                or parent['runtime_id'] != [42, current_host['hwnd']]
                or _visible_children(current_host['parent_hwnd']) != [current_host['hwnd']]
                or (host is not None and current_host != host)):
            raise ComputerError('windows_explorer_view_preview_unproven')
        host = current_host
        proofs.append({key: deepcopy(marker[key]) for key in ('id', 'runtime_id', 'type', 'automation_id')}
                      | {'parent': parent, 'class_name': expected_class})
    layout = _source_layout(window, markers, wrappers, host, wrappers[item['id']], check)
    return {'items': items, 'preview': {'visible': True, 'host': host, 'markers': proofs, 'layout': layout}}


def _scene(window, check):
    import win32gui
    current = explorer._guard(window, check)
    # Explorer may update its caption when SetValue starts asynchronous search.
    # _guard still proves the same HWND/process lifetime/class/installed profile.
    return {'title': current['title'], 'rect': list(win32gui.GetWindowRect(current['hwnd']))}


def _tab(record):
    return {key: deepcopy(record[key]) for key in _TAB}


def _tree(window, check):
    explorer._guard(window, check)
    observation, wrappers = access._tree(window, check)
    if observation.get('truncated'):
        raise ComputerError('windows_explorer_view_tree_limit')
    tabs = [item for item in observation['controls'] if item['type'] == 'TabItem'
            and any(a.get('automation_id') == 'TabListView' for a in item.get('ancestors', []))]
    if len(tabs) != 1 or tabs[0].get('selected') is not True:
        raise ComputerError('windows_explorer_view_tab_ambiguous')
    return observation, wrappers, _tab(tabs[0])


def _roles(window, check, *, source_preview=None):
    from .windows_controls import bounds
    observation, wrappers, tab = _tree(window, check)
    matches = []
    for item in observation['controls']:
        if (item['type'] != 'Edit' or item['automation_id'] != 'TextBox'
                or not item['enabled'] or not item['visible'] or 'set_value' not in item['patterns']
                or not any(a.get('automation_id') == 'FileExplorerSearchBox' for a in item.get('ancestors', []))):
            continue
        wrapper = wrappers[item['id']]
        parent = explorer._parent(wrapper, window['pid'])
        if parent['type'] != 'Group' or parent['automation_id'] != 'FileExplorerSearchBox':
            continue
        control = {key: deepcopy(item[key]) for key in _CONTROL - {'bounds'}}
        control['bounds'] = bounds(wrapper)
        pattern = wrapper.iface_value
        if pattern.CurrentIsReadOnly or pattern.CurrentValue != control['value']:
            raise ComputerError('windows_explorer_view_value_unverified')
        matches.append({'role': 'search_text', 'label': control['name'], 'control': control, 'parent': parent})
    if len(matches) != 1:
        raise ComputerError('windows_explorer_view_search_ambiguous')
    state = _view_state(window, observation, wrappers, check)
    if (source_preview is not None and source_preview['visible'] is True
            and state['items']['selection_count'] == 0 and state['preview'] is None
            and not any(item['automation_id'] in _PREVIEW_IDS for item in observation['controls'])):
        proof = _closed_original({'binding': {'preview': source_preview}, 'window': window}, check,
                                 items=state['items'])
        state['preview'] = {'visible': False, 'host': None, 'markers': [], 'closure_proof': proof}
    buttons = [item for item in observation['controls'] if item['automation_id'] == 'PreviewPaneToggleButton']
    if len(buttons) > 1:
        raise ComputerError('windows_explorer_view_preview_ambiguous')
    if buttons and state['preview'] is not None:
        item = buttons[0]
        wrapper = wrappers[item['id']]
        parent = explorer._parent(wrapper, window['pid'])
        if (item['type'] != 'Button' or item['name'] not in {'预览', 'Preview'}
                or not item['enabled'] or not item['visible'] or 'invoke' not in item['patterns']
                or parent['type'] != 'AppBar' or parent['automation_id'] != 'FileExplorerSecondaryCommandBar'):
            raise ComputerError('windows_explorer_view_preview_unproven')
        control = {key: deepcopy(item[key]) for key in _CONTROL - {'bounds'}}
        control['bounds'] = bounds(wrapper)
        matches.append({'role': 'preview_pane', 'label': control['name'], 'control': control, 'parent': parent})
    return matches, tab, wrappers, state


def diagnose(window, check):
    """Read-only purpose-built capability facts, without reading user file rows."""
    observation, wrappers, tab = _tree(window, check)
    roles, after_tab, _, state = _roles(window, check)
    if tab != after_tab:
        raise ComputerError('windows_explorer_view_tab_changed')
    buttons = []
    for item in observation['controls']:
        if item.get('automation_id') != 'PreviewPaneToggleButton':
            continue
        wrapper = wrappers[item['id']]
        record = {key: deepcopy(item[key]) for key in ('id', 'runtime_id', 'type', 'name', 'automation_id',
                                                       'enabled', 'visible', 'patterns')}
        record['parent'] = explorer._parent(wrapper, window['pid'])
        try:
            record['toggle_state'] = wrapper.iface_toggle.CurrentToggleState
        except Exception as exc:
            record['toggle_exception'] = type(exc).__name__
        try:
            from pywinauto.uia_defines import get_elem_interface
            legacy = get_elem_interface(wrapper.element_info.element, 'LegacyIAccessible')
            record['legacy'] = {'role': int(legacy.CurrentRole), 'state': int(legacy.CurrentState)}
        except Exception as exc:
            record['legacy_exception'] = type(exc).__name__
        record['offered'] = any(entry['role'] == 'preview_pane' for entry in roles)
        record['reason'] = 'empty_selection_and_native_preview_host' if record['offered'] else 'preview_state_not_independently_proven'
        buttons.append(record)
    explorer._guard(window, check)
    return {'window': deepcopy(window), 'truncated': False, 'controls_count': len(observation['controls']),
            'tab': tab, 'offered': roles, 'view_state': state, 'preview_buttons': buttons}


def _runtime(value):
    return (type(value) is list and 1 <= len(value) <= 32
            and all(type(number) is int for number in value))


def _native_record(value, window, class_name):
    if (type(value) is not dict or set(value) != {'hwnd', 'pid', 'class_name', 'root_hwnd', 'parent_hwnd'}
            or value['class_name'] != class_name or value['pid'] != window['pid']
            or value['root_hwnd'] != window['hwnd'] or value['hwnd'] == window['hwnd']
            or any(type(value[key]) is not int or value[key] <= 0 for key in ('hwnd', 'pid', 'root_hwnd', 'parent_hwnd'))):
        raise ComputerError('windows_explorer_view_source_invalid')


def _parent_record(value):
    if (type(value) is not dict or set(value) != _PARENT or not _runtime(value['runtime_id'])
            or any(type(value[key]) is not str or len(value[key]) > 300 for key in ('type', 'name', 'automation_id'))):
        raise ComputerError('windows_explorer_view_source_invalid')


def _id_record(value):
    if (not _runtime(value['runtime_id'])
            or value['id'] != sha256(json.dumps(value['runtime_id']).encode()).hexdigest()[:24]):
        raise ComputerError('windows_explorer_view_source_invalid')


def _box(value):
    return (type(value) is list and len(value) == 4 and all(type(v) is int for v in value)
            and value[0] < value[2] and value[1] < value[3])


def _layout_record(layout, preview, window):
    keys = {'runtime_id', 'type', 'class_name', 'automation_id', 'native_hwnd', 'pid',
            'offscreen', 'is_control', 'bounds'}
    if (type(layout) is not dict or set(layout) != {'pane', 'ancestors', 'items_bounds'}
            or type(layout['ancestors']) is not list or len(layout['ancestors']) != 5
            or not _box(layout['items_bounds'])):
        raise ComputerError('windows_explorer_view_source_invalid')
    chain = [layout['pane'], *layout['ancestors']]
    for facts in chain:
        if (type(facts) is not dict or set(facts) != keys or not _runtime(facts['runtime_id'])
                or type(facts['type']) is not int or type(facts['native_hwnd']) is not int or facts['native_hwnd'] < 0
                or type(facts['pid']) is not int or facts['pid'] != window['pid'] or facts['offscreen'] is not False
                or type(facts['is_control']) is not bool or not _box(facts['bounds'])
                or any(type(facts[k]) is not str or len(facts[k]) > 300 for k in ('class_name', 'automation_id'))):
            raise ComputerError('windows_explorer_view_source_invalid')
    host = preview['host']
    if ([(f['type'], f['class_name'], f['automation_id']) for f in chain[:3]] !=
            [(50025, 'TemplateBackground', 'ReadingPane'), (50033, 'Element', 'FolderLayoutContainer'),
             (50033, 'HWNDView', 'main')]
            or chain[0]['is_control'] is not False or chain[0]['native_hwnd'] != 0
            or [f['native_hwnd'] for f in chain[3:]] != [host['hwnd'], host['parent_hwnd'], window['hwnd']]
            or [f['class_name'] for f in chain[3:]] != ['DUIViewWndClassName', 'ShellTabWindowClass', 'CabinetWClass']
            or not _contains(chain[3]['bounds'], chain[0]['bounds'])
            or layout['items_bounds'][2] > chain[0]['bounds'][0]
            or chain[0]['bounds'][0] - layout['items_bounds'][2] > 64
            or layout['items_bounds'][1] != chain[0]['bounds'][1]
            or layout['items_bounds'][3] != chain[0]['bounds'][3]
            or chain[0]['bounds'][2] != chain[3]['bounds'][2]):
        raise ComputerError('windows_explorer_view_source_invalid')


def _state_record(binding, window):
    items, preview = binding['items'], binding['preview']
    if (type(items) is not dict or set(items) != {'id', 'runtime_id', 'type', 'automation_id', 'native',
                                               'parent', 'parent_native', 'selection_count'}
            or items['type'] != 'List' or type(items['automation_id']) is not str
            or type(items['selection_count']) is not int or not 0 <= items['selection_count'] <= 4096):
        raise ComputerError('windows_explorer_view_source_invalid')
    _id_record(items)
    _parent_record(items['parent'])
    _native_record(items['native'], window, 'DirectUIHWND')
    _native_record(items['parent_native'], window, 'SHELLDLL_DefView')
    if (items['native']['parent_hwnd'] != items['parent_native']['hwnd']
            or items['parent']['type'] != 'Pane' or items['parent']['automation_id'] != 'listview'
            or items['parent']['runtime_id'] != [42, items['parent_native']['hwnd']]):
        raise ComputerError('windows_explorer_view_source_invalid')
    if preview is None:
        return
    if (items['selection_count'] != 0 or type(preview) is not dict
            or set(preview) not in ({'visible', 'host', 'markers'}, {'visible', 'host', 'markers', 'layout'})
            or type(preview['visible']) is not bool
            or type(preview['markers']) is not list):
        raise ComputerError('windows_explorer_view_source_invalid')
    if not preview['visible']:
        if preview['host'] is not None or preview['markers'] != []:
            raise ComputerError('windows_explorer_view_source_invalid')
        return
    _native_record(preview['host'], window, 'DUIViewWndClassName')
    if len(preview['markers']) != 2:
        raise ComputerError('windows_explorer_view_source_invalid')
    if [m.get('automation_id') for m in preview['markers']] != sorted(_PREVIEW_IDS):
        raise ComputerError('windows_explorer_view_source_invalid')
    for marker in preview['markers']:
        if (type(marker) is not dict or set(marker) != {'id', 'runtime_id', 'type', 'automation_id', 'parent', 'class_name'}):
            raise ComputerError('windows_explorer_view_source_invalid')
        _id_record(marker)
        _parent_record(marker['parent'])
        kind = 'Image' if marker['automation_id'] == 'PreviewThumbnailInner' else 'Text'
        cls = 'PreviewThumbnailInner' if kind == 'Image' else 'Element'
        if (marker['type'] != kind or marker['class_name'] != cls or marker['parent']['type'] != 'Pane'
                or marker['parent']['runtime_id'] != [42, preview['host']['hwnd']]):
            raise ComputerError('windows_explorer_view_source_invalid')
    if 'layout' in preview:
        _layout_record(preview['layout'], preview, window)


def _validate(source):
    from .windows_visual_memory import _window
    if (type(source) is not dict or set(source) - {'native_proof'} != {'window', 'application', 'kind', 'binding', 'controls'}
            or source['application'] != 'explorer' or source['kind'] != 'explorer_view'):
        raise ComputerError('windows_explorer_view_source_invalid')
    window = _window(source['window'])
    if window != source['window'] or window['class_name'] != 'CabinetWClass' or window['profile'] != 'explorer':
        raise ComputerError('windows_explorer_view_source_invalid')
    binding = source['binding']
    if type(binding) is not dict or set(binding) != {'tab', 'scene', 'items', 'preview'}:
        raise ComputerError('windows_explorer_view_source_invalid')
    tab, scene = binding['tab'], binding['scene']
    if (type(tab) is not dict or set(tab) != _TAB or tab['type'] != 'TabItem'
            or type(tab['automation_id']) is not str or tab['selected'] is not True or not _runtime(tab['runtime_id'])
            or tab['id'] != sha256(json.dumps(tab['runtime_id']).encode()).hexdigest()[:24]
            or type(scene) is not dict or set(scene) != {'title', 'rect'} or scene['title'] != window['title']
            or type(scene['rect']) is not list or len(scene['rect']) != 4
            or any(type(v) is not int for v in scene['rect'])
            or scene['rect'][0] >= scene['rect'][2] or scene['rect'][1] >= scene['rect'][3]):
        raise ComputerError('windows_explorer_view_source_invalid')
    _state_record(binding, window)
    if (type(source['controls']) is not list or not 1 <= len(source['controls']) <= 2
            or [entry.get('role') for entry in source['controls']] not in
               [['search_text'], ['search_text', 'preview_pane']]):
        raise ComputerError('windows_explorer_view_source_invalid')
    for entry in source['controls']:
        if type(entry) is not dict or set(entry) != {'role', 'label', 'control', 'parent'}:
            raise ComputerError('windows_explorer_view_role_invalid')
        control, parent = entry['control'], entry['parent']
        if (type(control) is not dict or set(control) != _CONTROL
            or not _runtime(control['runtime_id'])
            or control['id'] != sha256(json.dumps(control['runtime_id']).encode()).hexdigest()[:24]
            or type(control['name']) is not str or not control['name'] or len(control['name']) > 300
            or entry['label'] != control['name'] or control['visible'] is not True or control['enabled'] is not True
            or (control['value'] is not None and (type(control['value']) is not str or len(control['value']) > 4000))
            or type(control['patterns']) is not list or any(type(p) is not str for p in control['patterns'])
            or len(set(control['patterns'])) != len(control['patterns'])
            or type(control['bounds']) is not list or len(control['bounds']) != 4
            or any(type(v) is not int for v in control['bounds'])
            or control['bounds'][0] >= control['bounds'][2] or control['bounds'][1] >= control['bounds'][3]
            or type(parent) is not dict or set(parent) != _PARENT or not _runtime(parent['runtime_id'])
            or type(parent['name']) is not str or len(parent['name']) > 300
            or type(control['ancestors']) is not list or not control['ancestors'] or len(control['ancestors']) > 4
            or control['ancestors'][-1] != {key: parent[key] for key in ('type', 'name', 'automation_id')}):
            raise ComputerError('windows_explorer_view_control_invalid')
        if entry['role'] == 'search_text':
            if (control['type'] != 'Edit' or control['automation_id'] != 'TextBox' or type(control['value']) is not str
                    or 'set_value' not in control['patterns'] or parent['type'] != 'Group'
                    or parent['automation_id'] != 'FileExplorerSearchBox'):
                raise ComputerError('windows_explorer_view_control_invalid')
        elif (control['type'] != 'Button' or control['automation_id'] != 'PreviewPaneToggleButton'
                or control['name'] not in {'预览', 'Preview'} or 'invoke' not in control['patterns']
                or parent['type'] != 'AppBar' or parent['automation_id'] != 'FileExplorerSecondaryCommandBar'
                or binding['preview'] is None or binding['preview']['visible'] is not True
                or binding['items']['selection_count'] != 0):
            raise ComputerError('windows_explorer_view_control_invalid')
        for ancestor in control['ancestors']:
            if (type(ancestor) is not dict or set(ancestor) != {'type', 'name', 'automation_id'}
                or any(type(value) is not str or len(value) > 300 for value in ancestor.values())):
                raise ComputerError('windows_explorer_view_control_invalid')
    return source


def validate(source):
    """Static source validation only; no current UI or filesystem reads."""
    try:
        return _validate(source)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, ComputerError):
            raise
        raise ComputerError('windows_explorer_view_source_invalid') from exc


def text(value):
    """A bounded field value, not a shell string, pathname or search-result claim."""
    if (type(value) is not str or not 1 <= len(value) <= 300 or any(ord(c) < 32 for c in value)
            or uia._DENY_SENSITIVE_TITLE.search(value)):
        raise ComputerError('windows_explorer_view_text_invalid')
    return value


def snapshot(window, check):
    scene = _scene(window, check)
    controls, tab, _, state = _roles(window, check)
    latest, after_tab, _, after_state = _roles(window, check)
    if controls != latest or tab != after_tab or state != after_state or _scene(window, check) != scene:
        raise ComputerError('screen_changed_reobserve')
    return validate({'window': deepcopy(window), 'application': 'explorer', 'kind': 'explorer_view',
                     'binding': {'tab': tab, 'scene': scene, **state}, 'controls': controls})


def _request(window, source, role, value):
    validate(source)
    if (window != source['window'] or type(role) is not str
            or role not in {entry['role'] for entry in source['controls']}):
        raise ComputerError('windows_explorer_view_request_invalid')
    if role == 'search_text':
        return text(value)
    if value != '' or type(value) is not str or source['binding']['preview'] is None:
        raise ComputerError('windows_explorer_view_request_invalid')
    return not source['binding']['preview']['visible']


def _current(window, source, role, check):
    scene = _scene(window, check)
    if scene['rect'] != source['binding']['scene']['rect']:
        raise ComputerError('windows_explorer_view_scene_changed')
    controls, tab, wrappers, state = _roles(window, check,
        source_preview=source['binding']['preview'] if role == 'preview_pane' else None)
    if tab != source['binding']['tab']:
        raise ComputerError('windows_explorer_view_tab_changed')
    # Search can replace ItemsView while retaining this exact window/tab/search
    # editor and preview host. _view_state independently re-attests the current
    # unique native view/parent/root and selection, rather than trusting old IDs.
    if role == 'preview_pane' and (state['items']['selection_count'] != 0 or state['preview'] is None):
        raise ComputerError('windows_explorer_view_selection_changed')
    source_preview, current_preview = source['binding']['preview'], state['preview']
    if (source_preview is not None and current_preview is not None
            and source_preview['visible'] and current_preview['visible']
            and current_preview != source_preview):
        raise ComputerError('windows_explorer_view_preview_changed')
    matches = [entry for entry in controls if entry['role'] == role]
    approved = next(entry for entry in source['controls'] if entry['role'] == role)
    if len(matches) != 1:
        raise ComputerError('windows_explorer_view_control_changed')
    now = matches[0]
    def stable(control):
        result = {key: value for key, value in control.items() if key not in {'value', 'ancestors'}}
        result['ancestors'] = [
            {**ancestor, 'name': ''} if (ancestor['type'] == 'Window' and ancestor['automation_id'] == ''
                and ancestor['name'] in {source['window']['title'], scene['title']}) else ancestor
            for ancestor in control['ancestors']]
        return result
    if (now['role'] != approved['role'] or now['label'] != approved['label'] or now['parent'] != approved['parent']
            or stable(now['control']) != stable(approved['control'])):
        raise ComputerError('windows_explorer_view_control_changed')
    if _scene(window, check) != scene:
        raise ComputerError('windows_explorer_view_scene_changed')
    return now, wrappers, {'tab': tab, 'scene': scene, **state}


def _raw_tree(window, check):
    """Bounded raw identities/geometry only, including non-ControlView panes."""
    from pywinauto.uia_defines import IUIA
    client = IUIA().iuia
    walker = client.RawViewWalker
    result = {}
    def visit(element, depth, parent_runtime):
        check()
        if len(result) >= 1600 or depth > 24:
            raise ComputerError('windows_explorer_view_tree_limit')
        if element.CurrentIsPassword or element.CurrentProcessId != window['pid']:
            raise ComputerError('windows_explorer_view_protected_control')
        facts = _raw_fact(element)
        runtime = tuple(facts['runtime_id'])
        if runtime in result:
            raise ComputerError('windows_explorer_view_raw_identity_ambiguous')
        result[runtime] = {'facts': facts, 'parent': parent_runtime}
        child = walker.GetFirstChildElement(element)
        while child:
            visit(child, depth + 1, runtime)
            child = walker.GetNextSiblingElement(child)
    visit(client.ElementFromHandle(window['hwnd']), 0, None)
    return result


def _list_hit(client, walker, point, items, window, check):
    from ctypes import wintypes
    element = client.ElementFromPoint(wintypes.POINT(*point))
    for _ in range(24):
        check()
        if not element or element.CurrentIsPassword or element.CurrentProcessId != window['pid']:
            break
        if [int(v) for v in element.GetRuntimeId()] == items['runtime_id']:
            if (int(element.CurrentNativeWindowHandle or 0) == items['native']['hwnd']
                    and element.CurrentClassName == 'UIItemsView'):
                return
            break
        element = walker.GetParentElement(element)
    raise ComputerError('windows_explorer_view_preview_region_occupied')


def _closed_original(source, check, *, items=None):
    """Prove the dedicated ReadingPane closed; the shared host may remain."""
    from pywinauto.uia_defines import IUIA, get_elem_interface
    import win32gui
    preview = source['binding']['preview']
    if preview is None or not preview['visible'] or 'layout' not in preview:
        raise ComputerError('windows_explorer_view_preview_new_source_required')
    if items is None or items['selection_count'] != 0:
        raise ComputerError('windows_explorer_view_selection_changed')
    window, layout, host = source['window'], preview['layout'], preview['host']
    explorer._guard(window, check, foreground=True)
    if (_native_hwnd(host['hwnd'], window, 'DUIViewWndClassName', parent_hwnd=host['parent_hwnd']) != host
            or not win32gui.IsChild(host['hwnd'], items['native']['hwnd'])):
        raise ComputerError('windows_explorer_view_preview_changed')
    tree = _raw_tree(window, check)
    ancestors = layout['ancestors']
    for index, expected in enumerate(ancestors):
        current = tree.get(tuple(expected['runtime_id']))
        parent = tuple(ancestors[index + 1]['runtime_id']) if index + 1 < len(ancestors) else None
        if current is None or current['facts'] != expected or current['parent'] != parent:
            raise ComputerError('windows_explorer_view_preview_layout_changed')
    pane = layout['pane']
    original = tree.get(tuple(pane['runtime_id']))
    for node in tree.values():
        facts = node['facts']
        if (facts['automation_id'] in (_PREVIEW_IDS | {'ReadingPane'})
                and not facts['offscreen'] and _box(facts['bounds'])):
            raise ComputerError('windows_explorer_view_preview_close_unproven')
    if original is not None:
        ignored = {'offscreen', 'bounds'}
        if ({k: v for k, v in original['facts'].items() if k not in ignored}
                != {k: v for k, v in pane.items() if k not in ignored}
                or original['parent'] != tuple(ancestors[0]['runtime_id'])):
            raise ComputerError('windows_explorer_view_preview_changed')
    client = IUIA().iuia
    element = client.ElementFromHandle(items['native']['hwnd'])
    current = _raw_fact(element)
    bounds, old_bounds, region = current['bounds'], layout['items_bounds'], pane['bounds']
    if (current['runtime_id'] != items['runtime_id'] or current['offscreen']
            or current['class_name'] != 'UIItemsView' or current['native_hwnd'] != items['native']['hwnd']
            or not _contains(bounds, region) or bounds[:2] != old_bounds[:2]
            or bounds[3] != old_bounds[3] or bounds[2] <= old_bounds[2]):
        raise ComputerError('windows_explorer_view_preview_region_unproven')
    selection = get_elem_interface(element, 'Selection')
    if selection.GetCurrentSelection().Length != 0:
        raise ComputerError('windows_explorer_view_selection_changed')
    xs = [region[0] + max(1, (region[2] - region[0]) * part // 4) for part in (1, 2, 3)]
    ys = [region[1] + max(1, (region[3] - region[1]) * part // 4) for part in (1, 2, 3)]
    points = [[x, y] for x in xs for y in ys]
    for point in points:
        _list_hit(client, client.RawViewWalker, point, items, window, check)
    explorer._guard(window, check, foreground=True)
    if selection.GetCurrentSelection().Length != 0:
        raise ComputerError('windows_explorer_view_selection_changed')
    return {'pane_presence': 'absent' if original is None else 'offscreen_or_empty',
            'pane_runtime_id': pane['runtime_id'], 'pane_bounds': region,
            'items_runtime_id': items['runtime_id'], 'items_bounds': bounds, 'list_hit_points': points}


def observe(window, source, role, value, check):
    target = _request(window, source, role, value)
    for attempt in range(3):
        try:
            current, _, binding = _current(window, source, role, check)
            break
        except Exception as exc:
            code = getattr(exc, 'hresult', None)
            if type(code) is not int or code != _ELEMENT_NOT_AVAILABLE:
                raise
            check()
            explorer._guard(window, check)
            if attempt == 2:
                error = ComputerError('windows_explorer_view_element_unavailable')
                error.content_diagnostic = {'hresult': code, 'read_attempts': 3}
                raise error from exc
    verified = current['control']['value'] == target if role == 'search_text' else binding['preview']['visible'] == target
    if role == 'preview_pane' and verified and target is False:
        _closed_original(source, check, items=binding['items'])
    return {'verified': verified,
            'facts': {'window': deepcopy(window), 'binding': binding,
                      'role': role, 'control': current['control'], 'parent': current['parent']}}


def act(window, source, role, value, check, epoch):
    """One exact SetValue or Preview Invoke, after durable approved intent."""
    target = _request(window, source, role, value)
    if role == 'preview_pane' and 'layout' not in source['binding']['preview']:
        raise ComputerError('windows_explorer_view_preview_new_source_required')
    current, _, binding = _current(window, source, role, check)
    approved = next(entry for entry in source['controls'] if entry['role'] == role)
    # An unrelated new draft cannot be silently overwritten under old approval.
    if role == 'search_text' and current['control']['value'] != approved['control']['value']:
        raise ComputerError('windows_explorer_view_value_changed')
    if role == 'preview_pane' and binding['preview'] != source['binding']['preview']:
        raise ComputerError('windows_explorer_view_preview_changed')
    explorer._prepare(window, check)
    current, wrappers, binding = _current(window, source, role, check)
    wrapper = wrappers[current['control']['id']]
    explorer._guard(window, check, foreground=True)
    if explorer._parent(wrapper, window['pid']) != approved['parent']:
        raise ComputerError('windows_explorer_view_parent_changed')
    if role == 'preview_pane':
        if binding['preview'] != source['binding']['preview']:
            raise ComputerError('windows_explorer_view_preview_changed')
        check()
        wrapper.iface_invoke.Invoke()
        return
    pattern = wrapper.iface_value
    if pattern.CurrentIsReadOnly or pattern.CurrentValue != approved['control']['value']:
        raise ComputerError('windows_explorer_view_value_changed')
    check()
    pattern.SetValue(target)
    check()
    if pattern.CurrentValue != target:
        raise ComputerError('windows_explorer_view_value_unverified')
