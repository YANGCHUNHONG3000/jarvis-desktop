"""Bounded native adapter for semantic Windows tasks; never edits documents."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new, same_identity
from . import computer_uia as uia

BASE = Path(__file__).resolve().parents[1] / 'data' / 'windows_observations'
FIND_NAMES = {'查找和替换', 'Find and Replace'}
OPEN_NAMES = {'打开文件', '打开', 'Open', 'Open File'}
FIND_FIELDS = {'查找内容(N)', 'Find what:'}
FIND_SELECTOR = {'type': 'ComboBox', 'name': '查找内容(N)', 'automation_id': ''}


def _input_epoch():
    import ctypes as c
    from ctypes import wintypes as w
    class Info(c.Structure):
        _fields_ = [('size', w.UINT), ('time', w.DWORD)]
    info = Info(c.sizeof(Info), 0)
    if not c.windll.user32.GetLastInputInfo(c.byref(info)):
        raise ComputerError('windows_input_unavailable')
    return info.time


def _foreground_context():
    import ctypes as c
    from ctypes import wintypes as w
    api = c.windll.user32
    api.GetForegroundWindow.restype = w.HWND
    hwnd = api.GetForegroundWindow()
    pid = w.DWORD()
    api.GetWindowThreadProcessId(w.HWND(hwnd), c.byref(pid))
    return {'caller_pid': os.getpid(), 'foreground_pid': pid.value, 'foreground_hwnd': hwnd}


def _owned_worker(job, ready):
    """A venv launcher PID can differ from the Python worker's actual PID."""
    import ctypes as c
    from ctypes import wintypes as w
    pid = ready.get('pid')
    if type(pid) is not int or pid <= 0:
        raise ComputerError('windows_worker_identity_invalid')
    api = c.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.OpenProcess(0x1000, False, pid)
    owned = w.BOOL()
    try:
        if not handle or not api.IsProcessInJob(handle, job.handle, c.byref(owned)) or not owned.value:
            raise ComputerError('windows_worker_identity_invalid')
        return pid
    finally:
        if handle:
            api.CloseHandle(handle)


def _wps(identity):
    from .word_open import _registered_app_paths
    path = Path(identity['image'])
    if path.name.casefold() == 'et.exe' and identity['class_name'] == 'XLMAIN':
        from .workbook_open import resolve_workbook_application
        try:
            return path.resolve() == Path(resolve_workbook_application()['executable'])
        except ValueError:
            return False
    if path.name.casefold() != 'wps.exe' or identity['class_name'] not in {'OpusApp', 'XLMAIN'}:
        return False
    for filename, _, _, raw in _registered_app_paths():
        if filename.casefold() != 'wps.exe' or type(raw) is not str:
            continue
        candidate = Path(os.path.expandvars(raw.strip().strip('"')))
        if candidate.is_absolute() and not str(candidate).startswith(('\\\\', '//')):
            if candidate.is_file() and candidate.resolve() == path.resolve():
                return True
    return False


def _file_application(window, path):
    """Unified WPS frame class is not the active document's format.

    _kind verifies installation identity; package and format-specific native
    bindings verify the actual document, never the frame title/class alone.
    """
    suffix = Path(path).suffix.casefold()
    image = Path(window.get('image', '')).name.casefold()
    return (suffix in {'.docx', '.xlsx'} and
            ((image == 'wps.exe' and window.get('class_name') in {'OpusApp', 'XLMAIN'})
             or (suffix == '.xlsx' and image == 'et.exe' and window.get('class_name') == 'XLMAIN')))


def _kind(identity):
    if identity['profile'] == 'explorer' and identity['class_name'] == 'CabinetWClass':
        return 'explorer'
    if _wps(identity):
        return 'wps'
    raise ComputerError('windows_application_unsupported')


def _owned(hwnd, root):
    import win32gui
    seen = set()
    while hwnd and hwnd not in seen and len(seen) < 12:
        seen.add(hwnd)
        hwnd = win32gui.GetWindow(hwnd, 4)  # GW_OWNER, never title/PID alone.
        if hwnd == root:
            return True
    return False


def _process_owned(child, parent):
    if child['image'] != parent['image']:
        # Unified WPS hosts the XLMAIN frame in wps.exe while its owned file
        # dialog runs in the registered companion et.exe child process.
        if (parent.get('class_name') not in {'XLMAIN', 'OpusApp'} or child['pid'] == parent['pid']
                or Path(parent['image']).name.casefold() != 'wps.exe' or not _wps(parent)):
            return False
        from .workbook_open import resolve_workbook_application
        try:
            if (Path(child['image']).resolve() != Path(resolve_workbook_application()['executable'])
                    or Path(child['image']).resolve().parent != Path(parent['image']).resolve().parent):
                return False
        except ValueError:
            return False
    if child['pid'] == parent['pid']:
        return child['created'] == parent['created']
    import ctypes as c
    from ctypes import wintypes as w
    class Entry(c.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', c.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('exe', w.WCHAR * 260)]
    api = c.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    api.CreateToolhelp32Snapshot.restype = w.HANDLE
    api.Process32FirstW.argtypes = api.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == c.c_void_p(-1).value:
        return False
    parents = {}
    try:
        entry = Entry()
        entry.size = c.sizeof(entry)
        found = api.Process32FirstW(handle, c.byref(entry))
        while found:
            parents[entry.pid] = entry.parent
            found = api.Process32NextW(handle, c.byref(entry))
    finally:
        api.CloseHandle(handle)
    pid = child['pid']
    for _ in range(12):
        pid = parents.get(pid)
        if pid == parent['pid']:
            return child['created'] >= parent['created']
        if not pid:
            return False
    return False


def _native_record(wrapper, pid):
    record = uia._record(wrapper, pid)
    if record is not None:
        if record.get('type') == 'CheckBox':
            from .windows_controls import enrich
            enrich(wrapper, record)
        from pywinauto.uia_defines import NoPatternInterfaceError
        try:
            wrapper.iface_scroll_item
        except (NoPatternInterfaceError, AttributeError):
            pass
        else:
            record['patterns'].append('reveal')
    return record


def _tree(identity, check, target_selector=None, *, reject_password=False):
    """Visible controls plus only the exact authorized offscreen target."""
    from pywinauto import Desktop
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    import win32gui
    fresh = uia._recheck(identity)
    root = Desktop(backend='uia').window(handle=fresh['hwnd']).wrapper_object()
    walker = IUIA().iuia.ControlViewWalker
    controls, wrappers = [], {}
    walked = 0
    truncated = False
    def visit(element, depth, ancestors):
        nonlocal walked, truncated
        check()
        walked += 1
        if walked > 1600 or depth > 24 or len(controls) >= 180:
            truncated = True
            return
        wrapper = UIAWrapper(UIAElementInfo(element))
        if element.CurrentIsPassword:
            if reject_password and not element.CurrentIsOffscreen:
                raise ComputerError('windows_vision_password_surface')
            return
        if wrapper.element_info.process_id != fresh['pid']:
            return
        next_ancestors = ancestors
        target_match = False
        if target_selector is not None:
            target_match = (wrapper.element_info.control_type == target_selector['type']
                            and (wrapper.element_info.automation_id or '') == target_selector['automation_id']
                            and (wrapper.element_info.name or '') == target_selector['name'])
            if 'ancestor' in target_selector:
                target_match = target_match and target_selector['ancestor'] in ancestors[-4:]
        if not depth or not element.CurrentIsOffscreen or target_match:
            record = _native_record(wrapper, fresh['pid'])
            if record is None:
                return
            if depth == 0:
                record['focused'] = win32gui.GetForegroundWindow() == fresh['hwnd']
            record['ancestors'] = ancestors[-4:]
            controls.append(record)
            wrappers[record['id']] = wrapper
            next_ancestors = [*ancestors, {k: record[k] for k in ('type', 'name', 'automation_id')}]
        elif target_selector is not None and 'ancestor' in target_selector:
            expected = target_selector['ancestor']
            if (wrapper.element_info.control_type == expected['type']
                    and (wrapper.element_info.automation_id or '') == expected['automation_id']
                    and (wrapper.element_info.name or '') == expected['name']):
                next_ancestors = [*ancestors, dict(expected)]
        # A scrolled-out tree parent can still have visible children. Traverse
        # it without reading/storing its label; do not silently lose those targets.
        child = walker.GetFirstChildElement(element)
        while child:
            visit(child, depth + 1, next_ancestors)
            if truncated:
                break
            check()
            child = walker.GetNextSiblingElement(child)
    visit(root.element_info.element, 0, [])
    uia._recheck(identity)
    return {'window': fresh, 'controls': controls, 'truncated': truncated,
            'scope': 'visible_controls'}, wrappers


def _safe_blocked_observation(observation):
    """Retain only bounded popup identifiers, never popup titles or content."""
    if (type(observation) is not dict or type(observation.get('window')) is not dict
            or observation.get('application') not in {'explorer', 'wps'}
            or type(observation.get('blocked_reason')) is not str or not observation['blocked_reason']
            or type(observation.get('truncated', False)) is not bool):
        raise ComputerError('windows_blocked_metadata_invalid')
    windows = observation.get('blocked_windows') or observation.get('dialogs')
    if type(windows) is not list or not 1 <= len(windows) <= 16:
        raise ComputerError('windows_blocked_metadata_invalid')
    safe = []
    for window in windows:
        if type(window) is not dict:
            raise ComputerError('windows_blocked_metadata_invalid')
        hwnd, class_name = window.get('hwnd'), window.get('class_name')
        if (type(hwnd) is not int or not 0 < hwnd <= 0xffffffffffffffff
                or type(class_name) is not str or not 1 <= len(class_name) <= 128
                or any(ord(char) < 32 or ord(char) == 127 for char in class_name)):
            raise ComputerError('windows_blocked_metadata_invalid')
        safe.append({'hwnd': hwnd, 'class_name': class_name})
    if len({item['hwnd'] for item in safe}) != len(safe):
        raise ComputerError('windows_blocked_metadata_invalid')
    reason = observation['blocked_reason']
    if reason not in {'owned_window_requires_manual_attention', 'multiple_find_dialogs'}:
        reason = 'windows_application_blocked'
    return {'window': observation['window'], 'application': observation['application'],
            'scope': 'visible_controls', 'controls': [], 'dialogs': [],
            'truncated': observation.get('truncated', False),
            'blocked_reason': reason, 'blocked_windows': safe}


def _observe(window, check, target_selector=None, *, file_goal=False, winapp_find=False):
    import win32gui
    fresh = uia._recheck(window)
    kind = _kind(fresh)
    if kind == 'wps' and not file_goal and fresh['title'] != window['title']:
        raise ComputerError('windows_document_window_changed')
    observation, wrappers = (_tree(fresh, check) if target_selector is None
                             else _tree(fresh, check, target_selector))
    if target_selector is not None:
        observation['target_selector'] = target_selector
    observation['application'] = kind
    observation['dialogs'] = []
    owned = []
    def visit(hwnd, _):
        if hwnd != fresh['hwnd'] and win32gui.IsWindowVisible(hwnd) and _owned(hwnd, fresh['hwnd']):
            # Native hover tips are informational, not application dialogs.
            # Do not read their text or treat them as an authorization prompt.
            if win32gui.GetClassName(hwnd).casefold() == 'tooltips_class32':
                return
            # WPS's untitled layered shadow is a decoration, not a dialog.
            if kind == 'wps' and win32gui.GetClassName(hwnd) == 'KLiteMainWindowShadowBorder':
                import win32process
                if (win32process.GetWindowThreadProcessId(hwnd)[1] == fresh['pid']
                        and win32gui.GetWindowLong(hwnd, -20) & 0x80080 == 0x80080
                        and not win32gui.GetWindowText(hwnd)):
                    return
            owned.append(hwnd)
    win32gui.EnumWindows(visit, None)
    for hwnd in owned:
        check()
        try:
            identity = uia._identity(hwnd)
        except Exception:
            observation['blocked_reason'] = 'owned_window_requires_manual_attention'
            observation.setdefault('blocked_windows', []).append({'hwnd': hwnd,
                'class_name': win32gui.GetClassName(hwnd)})
            break
        if (kind != 'wps' or not _process_owned(identity, fresh)
                or identity['title'] not in (FIND_NAMES | OPEN_NAMES | ({'查找', 'Find'} if winapp_find else set()) | ({'WPS 文字', '文件正在使用'} if file_goal else set()))):
            observation['blocked_reason'] = 'owned_window_requires_manual_attention'
            observation.setdefault('blocked_windows', []).append({key: identity[key]
                for key in ('hwnd', 'class_name')})
            break
        if observation['dialogs']:
            observation['blocked_reason'] = 'multiple_find_dialogs'
            observation['blocked_windows'] = [{k: d[k] for k in ('hwnd', 'class_name')}
                                               for d in (observation['dialogs'][0], identity)]
            break
        child, child_wrappers = (_tree(identity, check) if target_selector is None
                                 else _tree(identity, check, target_selector))
        identity['dialog_kind'] = ('file_status' if identity['title'] in {'WPS 文字', '文件正在使用'}
                                   else 'open_file' if identity['title'] in OPEN_NAMES else 'find')
        observation['dialogs'].append(identity)
        if child['truncated']:
            observation['truncated'] = True
        ancestor = {'type': 'Window', 'name': identity['title'], 'automation_id': ''}
        for control in child['controls']:
            control['owner_hwnd'] = hwnd
            if ancestor not in control.get('ancestors', []):
                control['ancestors'] = [ancestor, *control.get('ancestors', [])]
            observation['controls'].append(control)
        wrappers.update(child_wrappers)
    if len(observation['dialogs']) > 1:
        observation['blocked_reason'] = 'multiple_find_dialogs'
    if observation.get('blocked_reason'):
        return _safe_blocked_observation(observation), {}
    if len(observation['controls']) > 200:
        observation['truncated'] = True
        observation['controls'] = observation['controls'][:200]
    ids = [c['id'] for c in observation['controls']]
    if len(ids) != len(set(ids)):
        raise ComputerError('windows_control_ambiguous')
    if winapp_find:
        # UIA IsOffscreen can be false for logical navigation nodes that have
        # no drawable rectangle. Do not offer these as executable Winapp
        # targets: the CLI binding independently requires the target's own
        # positive native and CLI geometry to match exactly.
        from .windows_controls import bounds
        kinds = {'ListItem', 'TreeItem', 'TabItem', 'RadioButton', 'CheckBox', 'Edit', 'ComboBox'}
        for control in observation['controls']:
            if (control.get('type') not in kinds or not control.get('enabled') or not control.get('visible')
                    or not set(control.get('patterns', ())) & {'select', 'expand', 'collapse', 'toggle', 'set_value'}):
                continue
            check()
            try:
                control['bounds'] = bounds(wrappers[control['id']])
            except Exception:
                check()
                control['bounds'] = None
    return observation, wrappers


def _find_field(control, observation):
    return (observation.get('application') == 'wps'
            and control.get('owner_hwnd') in {d['hwnd'] for d in observation.get('dialogs', [])}
            and control['type'] == 'ComboBox' and control['name'] in FIND_FIELDS)


def _direct_child(wrapper, parent):
    from pywinauto.uia_defines import IUIA
    element = IUIA().iuia.ControlViewWalker.GetParentElement(wrapper.element_info.element)
    return bool(element) and [int(n) for n in element.GetRuntimeId()] == parent['runtime_id']


def allowed(observation, control, action, value):
    if observation.get('blocked_reason') or observation.get('truncated'):
        return False
    from . import windows_visual as visual, windows_controls as generic
    if action == visual.ACTION:
        return visual.allowed(observation, control, action, value)
    legacy_find = control and control.get('type') == 'ComboBox' and control.get('name') in FIND_FIELDS
    if not legacy_find and generic.allowed(observation, control, action, value):
        return True
    from . import windows_file_actions as file_actions
    from . import windows_document_lifecycle as lifecycle
    from . import windows_content_find as content
    if action == content.ACTION:
        return content.allowed(observation, control, action, value)
    if action in lifecycle.ACTIONS:
        return lifecycle.allowed(observation, control, action, value)
    if action in file_actions.ACTIONS:
        return file_actions.allowed(observation, control, action, value)
    if any(d.get('dialog_kind') == 'file_status' for d in observation.get('dialogs', [])):
        return False
    if action == 'open_find':
        return (control is None and value == '' and observation.get('application') == 'wps'
                and not observation.get('dialogs'))
    if (control and action == 'reveal' and value == '' and control.get('enabled')
            and not control.get('visible') and 'reveal' in control.get('patterns', [])
            and control['type'] in {'ListItem', 'TreeItem', 'DataItem'}
            and observation.get('target_selector')):
        from .windows_task import locate
        try:
            return locate(observation, observation['target_selector']) == control
        except ComputerError:
            return False
    if not control or not control.get('enabled') or not control.get('visible'):
        return False
    if action not in control.get('patterns', []):
        return False
    if action == 'set_value':
        return (_find_field(control, observation) and type(value) is str and len(value) <= 300
                and not any(ord(c) < 32 for c in value))
    if value != '':
        return False
    if action == 'focus':
        return control['type'] in {'Window', 'ListItem', 'TreeItem', 'ComboBox'}
    if observation.get('application') == 'explorer':
        return ((control['type'] == 'ListItem' and action == 'select')
                or (control['type'] == 'TreeItem' and action in {'select', 'expand', 'collapse'}))
    return False


def candidates(observation, condition):
    from .windows_task import locate
    from . import windows_reviewed_controls as reviewed
    if reviewed.is_goal(condition['selector']):return reviewed.actions(observation,condition)
    from . import windows_visual_forms as forms
    if forms.is_goal(condition['selector']):return forms.actions(observation,condition)
    from . import windows_visual as visual, windows_controls as generic
    if visual.is_goal(condition['selector']):
        return visual.candidates(observation, condition)
    from . import windows_file_actions as file_actions
    from . import windows_document_lifecycle as lifecycle
    from . import windows_content_find as content
    if content.is_goal(condition['selector']):
        return content.candidates(observation, condition)
    if lifecycle.is_goal(condition['selector']):
        return lifecycle.candidates(observation, condition)
    if file_actions.is_goal(condition['selector']):
        return file_actions.candidates(observation, condition)
    try:
        control = locate(observation, condition['selector'])
    except ComputerError as exc:
        if str(exc) != 'windows_control_missing':
            return []
        control = None
    if control is None:
        selector = condition['selector']
        if (condition['state'] == 'value' and selector['type'] == 'ComboBox'
                and selector['name'] in FIND_FIELDS and not selector['automation_id']
                and allowed(observation, None, 'open_find', '')):
            return [{'control_id': None, 'action': 'open_find', 'value': ''}]
        return []
    if not control.get('visible') and allowed(observation, control, 'reveal', ''):
        return [{'control_id': control['id'], 'action': 'reveal', 'value': ''}]
    legacy_find = control.get('type') == 'ComboBox' and control.get('name') in FIND_FIELDS
    extra = None if legacy_find else generic.candidate(observation, condition, control)
    if extra is not None:
        return [extra]
    if condition['state'] == 'checked':
        return []
    action = {'selected': 'select', 'expanded': 'expand', 'collapsed': 'collapse',
              'focused': 'focus', 'value': 'set_value'}[condition['state']]
    value = condition['value'] if action == 'set_value' else ''
    return ([{'control_id': control['id'], 'action': action, 'value': value}]
            if allowed(observation, control, action, value) else [])


def _request_observation(request, check):
    selector = request.get('selector')
    from . import windows_reviewed_controls as reviewed
    if reviewed.is_goal(selector):
        return reviewed.observation(request['window'],selector,request.get('document_token'),check), {}
    from . import windows_visual_forms as forms
    if forms.is_goal(selector):
        return forms.observation(request['window'],selector,request.get('document_token'),check), {}
    from . import windows_visual as visual
    if visual.is_goal(selector):
        return visual.observe(request['window'], selector, request.get('document_token'), None, check)
    from . import windows_file_actions as file_actions
    from . import windows_document_lifecycle as lifecycle
    from . import windows_content_find as content
    if content.is_goal(selector):
        return content.observe(request['window'], request.get('document_token'), check,
            lambda: _observe(request['window'], check, selector, file_goal=True))
    if lifecycle.is_goal(selector):
        return lifecycle.observe(request['window'], request.get('document_token'), selector, check,
            lambda: _observe(request['window'], check, selector, file_goal=True))
    if file_actions.is_goal(selector):
        return file_actions.observe(request['window'], request.get('document_token'), check,
            lambda: _observe(request['window'], check, selector, file_goal=True))
    if request.get('provider') is not None or 'auto_route_sha256' in request:
        return _observe(request['window'], check, selector, winapp_find=True)
    return (_observe(request['window'], check) if selector is None
            else _observe(request['window'], check, selector))


def _perform(request, root, check, epoch):
    import win32gui
    from .windows_task import locate
    if 'auto_route_sha256' in request:
        from .windows_auto import verify_request
        verify_request(request, root)
    with uia._native(), ExitStack() as resources:
        if request['op']=='vision_frame':
            from .windows_vision_context import capture_frame
            return {'ok':True, 'frame':capture_frame(request['window'], root, check)}
        if request['op']=='reviewed_snapshot':
            from .windows_reviewed_controls import native
            return {'ok':True,'snapshot':native(request['kind']).snapshot(request['window'],check)}
        if request['op']=='visual_form_snapshot':
            from .windows_visual_form_native import snapshot
            return {'ok':True,'snapshot':snapshot(request['window'],check)}
        if request['op'] == 'office_snapshot':
            from .windows_winapp import OFFICE_PROVIDER_BINDING
            if request.get('provider') != OFFICE_PROVIDER_BINDING:
                raise ComputerError('windows_provider_binding_invalid')
            from .windows_winapp_office import snapshot
            return {'ok': True, 'snapshot': snapshot(request['window'], check)}
        if request['op'] == 'window_presence':
            hwnd = request.get('window', {}).get('hwnd')
            if type(hwnd) is not int or hwnd <= 0:
                raise ComputerError('window_identity_invalid')
            check()
            # Existence is independent of visibility, access, title, or identity.
            # In particular, a reused HWND must not count as a vanished window.
            exists = bool(win32gui.IsWindow(hwnd))
            check()
            return {'ok': True, 'exists': exists}
        if request['op'] == 'visual_document_identity':
            from .windows_visual_binding import capture
            return {'ok': True, 'document': capture(request['window'], check)}
        if request['op'] == 'visual_snapshot':
            from .windows_visual_discovery import capture_snapshot
            return {'ok': True, 'snapshot': capture_snapshot(request['window'], check)}
        if request['op'] == 'list':
            windows = []
            for w in uia.list_windows():
                try:
                    _kind(w)
                    windows.append(w)
                except ComputerError:
                    pass
            return {'ok': True, 'windows': windows}
        selector = request.get('selector')
        if selector is not None:
            from .windows_task import _selector
            _selector(selector)
        from . import windows_winapp_office as office
        if office.is_goal(selector):
            from .windows_winapp import OFFICE_PROVIDER_BINDING
            if request.get('provider') != OFFICE_PROVIDER_BINDING:
                raise ComputerError('windows_provider_action_denied')
            if request['op'] == 'observe':
                return {'ok': True, 'observation': office.observe(request['window'], selector,
                    request.get('document_token'), check)}
            if request['op'] == 'act':
                return office.perform(request, root, check, epoch)
            raise ComputerError('windows_provider_action_denied')
        if request.get('provider') is not None:
            from .windows_winapp import valid_provider_binding
            from .windows_mcp import PROVIDER_BINDING as MCP_PROVIDER_BINDING
            provider_actions = ({'select', 'toggle'} if request['provider'] == MCP_PROVIDER_BINDING
                                else {'set_value', 'select', 'expand', 'collapse', 'toggle'})
            if (not valid_provider_binding(request['provider']) or request['op'] not in {'observe', 'act'}
                    or (request['op'] == 'act' and request.get('action') not in provider_actions)):
                raise ComputerError('windows_provider_action_denied')
        from . import windows_visual as visual, windows_visual_forms as forms
        from . import windows_reviewed_controls as reviewed
        if request.get('provider') is not None:
            from . import windows_file_actions as file_actions, windows_document_lifecycle as lifecycle, windows_content_find as content
            if any(module.is_goal(selector) for module in (reviewed, forms, visual, file_actions, lifecycle, content)):
                raise ComputerError('windows_provider_action_denied')
        if reviewed.is_goal(selector):
            if request['op']=='observe':
                return {'ok':True,'observation':reviewed.observation(request['window'],selector,request.get('document_token'),check,evidence_root=root)}
            if request['op']!='act' or request.get('action')!=reviewed.ACTION:
                raise ComputerError('windows_request_invalid')
            return reviewed.perform(request,root,check,epoch)
        if forms.is_goal(selector):
            if request['op']=='observe':
                return {'ok':True,'observation':forms.observation(request['window'],selector,request.get('document_token'),check,evidence_root=root)}
            if request['op']!='act' or request.get('action')!=forms.ACTION:
                raise ComputerError('windows_request_invalid')
            return forms.perform(request,root,check,epoch)
        if visual.is_goal(selector):
            if request['op'] == 'observe':
                visual.prepare(request['window'], check)
                before, _ = _request_observation(request, check)
                return {'ok': True, 'observation': before}
            if request['op'] != 'act' or request.get('action') != visual.ACTION:
                raise ComputerError('windows_request_invalid')
            return visual.perform(request, root, check, epoch)
        before, wrappers = _request_observation(request, check)
        if request['op'] == 'observe':
            if before.get('blocked_reason'):
                before = _safe_blocked_observation(before)
            return {'ok': True, 'observation': before}
        if request['op'] != 'act':
            raise ComputerError('windows_request_invalid')
        if before.get('blocked_reason'):
            return {'ok': False, 'state': 'failed', 'effects': 'none',
                    'code': 'windows_owned_dialog_before_action', 'observation': _safe_blocked_observation(before)}
        if digest(before) != request['before_sha']:
            raise ComputerError('screen_changed_reobserve')
        control = request['control']
        if control is not None:
            matches = [c for c in before['controls'] if c['id'] == control['id']]
            if len(matches) != 1 or matches[0] != control:
                raise ComputerError('screen_changed_reobserve')
        action, value = request['action'], request['value']
        if not allowed(before, control, action, value):
            raise ComputerError('windows_action_denied')
        from .windows_mcp import PROVIDER_BINDING as MCP_PROVIDER_BINDING
        using_mcp = request.get('provider') == MCP_PROVIDER_BINDING
        if using_mcp and not WindowsMcpBackend.allowed(before, control, action, value):
            raise ComputerError('windows_provider_action_denied')
        effective = control
        file_fields = None
        occupied_message = None
        if action == 'set_file_path':
            from .windows_file_dialog import filename_edit
            _, effective = filename_edit(before)
        if action == 'open_local_file':
            from .windows_file_dialog import filename_edit
            file_fields = filename_edit(before)
        if action == 'open_readonly':
            from .windows_file_dialog import decode_token
            from .windows_file_status import occupied_controls
            occupied_message, _ = occupied_controls(before, decode_token(value)['path'])
        if action == 'set_value' and _find_field(control, before):
            combo = {k: control[k] for k in ('type', 'name', 'automation_id')}
            edits = [c for c in before['controls'] if c['type'] == 'Edit'
                     and c.get('owner_hwnd') == control.get('owner_hwnd')
                     and combo in c.get('ancestors', [])
                     and c['visible'] and c['enabled'] and 'set_value' in c['patterns']]
            if len(edits) != 1:
                raise ComputerError('windows_find_editor_ambiguous')
            effective = edits[0]
            if wrappers[effective['id']].iface_value.CurrentIsReadOnly:
                raise ComputerError('control_readonly')
        def recheck_target():
            check()
            if action == 'select_content_match':
                from .windows_content_find import preflight
                preflight(request['window'], value, before['content_match'], check)
            if action in {'save_document_copy', 'close_saved_document'}:
                from .windows_document_lifecycle import preflight
                preflight(request['window'], value, action, check)
            if action in {'open_file_dialog', 'set_file_path', 'open_local_file', 'open_readonly'}:
                from .windows_file_dialog import decode_token
                decode_token(value)
            uia._recheck(request['window'])
            for dialog in before['dialogs']:
                uia._recheck(dialog)
                if not _owned(dialog['hwnd'], before['window']['hwnd']) or not _process_owned(dialog, before['window']):
                    raise ComputerError('windows_dialog_identity_changed')
            if effective is not None:
                owner = next((d for d in before['dialogs'] if d['hwnd'] == effective.get('owner_hwnd')), before['window'])
                expected_controls = [control, effective] if effective != control else [control]
                if file_fields:
                    expected_controls = [*expected_controls, *file_fields]
                if occupied_message:
                    expected_controls.append(occupied_message)
                for expected in expected_controls:
                    current = _native_record(wrappers[expected['id']], owner['pid'])
                    if current is not None and 'bounds' in expected and 'bounds' not in current:
                        from .windows_controls import bounds
                        current['bounds'] = bounds(wrappers[expected['id']])
                    if current is None or any(current.get(k) != v for k, v in expected.items()
                            if k not in {'focused', 'ancestors', 'owner_hwnd'}):
                        raise ComputerError('screen_changed_reobserve')
                if effective != control and not _direct_child(wrappers[effective['id']], control):
                    raise ComputerError('screen_changed_reobserve')
                if file_fields and not _direct_child(wrappers[file_fields[1]['id']], file_fields[0]):
                    raise ComputerError('screen_changed_reobserve')
        recheck_target()
        check()
        winapp_element = None
        winapp_reader = None
        mcp_reader = None
        mcp_element = None
        mcp_hwnd = None
        provider_route = None
        if request.get('provider') is not None:
            from .windows_winapp import ROUTES, WinAppReader, PROVIDER_BINDING, OFFICE_PROVIDER_BINDING
            if using_mcp:
                from .windows_mcp import ROUTES as MCP_ROUTES, McpReader
                provider_route = MCP_ROUTES.get(action)
                if provider_route is None:
                    raise ComputerError('windows_provider_action_denied')
                mcp_hwnd = effective.get('owner_hwnd', before['window']['hwnd'])
                if mcp_hwnd != before['window']['hwnd'] and mcp_hwnd not in {d['hwnd'] for d in before['dialogs']}:
                    raise ComputerError('windows_mcp_target_unsupported')
                def mcp_foreground_error(code='window_foreground_denied'):
                    error = ComputerError(code)
                    error.input_diagnostic = {'phase': 'mcp_foreground_validation',
                        'expected_hwnd': mcp_hwnd, **_foreground_context()}
                    return error
                if win32gui.GetForegroundWindow() != mcp_hwnd:
                    try:
                        win32gui.SetForegroundWindow(mcp_hwnd)
                    except Exception:
                        raise mcp_foreground_error() from None
                    focus_until = time.monotonic() + 2
                    while win32gui.GetForegroundWindow() != mcp_hwnd:
                        check()
                        if time.monotonic() >= focus_until:
                            raise mcp_foreground_error()
                        time.sleep(.03)
                check()
                if win32gui.GetForegroundWindow() != mcp_hwnd:
                    raise mcp_foreground_error()
                # Activating the owner can change native focus/state. Reject a
                # stale approval before paying for MCP preparation, retaining
                # the independent post-bind observation below as well.
                foreground_observation, _ = _request_observation(request, check)
                if foreground_observation.get('blocked_reason'):
                    return {'ok': False, 'state': 'failed', 'effects': 'none',
                            'code': 'windows_owned_dialog_before_action',
                            'observation': _safe_blocked_observation(foreground_observation)}
                if digest(foreground_observation) != digest(before):
                    raise ComputerError('screen_changed_reobserve')
                mcp_reader = McpReader(root / 'mcp', timeout=12, check=check)
                resources.callback(mcp_reader.close)
                mcp_element = mcp_reader.bind(mcp_hwnd, effective, wrappers[effective['id']], action=action)
                recheck_target()
                check()
            elif request['provider'] not in (PROVIDER_BINDING, OFFICE_PROVIDER_BINDING):
                raise ComputerError('windows_provider_binding_invalid')
            else:
                provider_route = ROUTES.get(action)
            if provider_route is None:
                raise ComputerError('windows_provider_action_denied')
            if provider_route == 'winapp-explicit-invoke':
                if effective != control:
                    raise ComputerError('winapp_target_unsupported')
                winapp_hwnd = effective.get('owner_hwnd', before['window']['hwnd'])
                if winapp_hwnd != before['window']['hwnd'] and winapp_hwnd not in {d['hwnd'] for d in before['dialogs']}:
                    raise ComputerError('winapp_target_unsupported')
                winapp_reader = WinAppReader(root / 'winapp', timeout=5)
                winapp_element = winapp_reader.bind(winapp_hwnd, effective, wrappers[effective['id']], action=action)
                recheck_target()
                check()
        # A popup may have appeared while the CLI target was being bound. Read
        # once more before journaling/dispatch; never observe it again here.
        latest, _ = _request_observation(request, check)
        if latest.get('blocked_reason'):
            return {'ok': False, 'state': 'failed', 'effects': 'none',
                    'code': 'windows_owned_dialog_before_action', 'observation': _safe_blocked_observation(latest)}
        if digest(latest) != digest(before):
            raise ComputerError('screen_changed_reobserve')
        recheck_target()
        check()
        if using_mcp and win32gui.GetForegroundWindow() != mcp_hwnd:
            raise mcp_foreground_error('user_takeover')
        intent_record = {'request_sha': digest(request), 'at': time.time()}
        if provider_route is not None:
            intent_record['route'] = provider_route
        write_new(root / 'native-intent.json', intent_record)
        if action in {'open_find', 'open_file_dialog', 'close_saved_document', 'focus', 'select_content_match'}:
            target = control.get('owner_hwnd', before['window']['hwnd']) if control else before['window']['hwnd']
            if win32gui.GetForegroundWindow() != target:
                win32gui.SetForegroundWindow(target)
            focus_until = time.monotonic() + 2
            while win32gui.GetForegroundWindow() != target:
                check()
                if time.monotonic() >= focus_until:
                    raise ComputerError('window_foreground_denied')
                time.sleep(.03)
            check()
        if action == 'select_content_match':
            from .windows_content_find import select_match
            recheck_target()
            select_match(request['window'], value, before['content_match'], check)
        elif action == 'save_document_copy':
            from .windows_document_copy import save_copy
            recheck_target()
            receipt = save_copy(request['window'], value, check, root)
            # The exact COM Saved/path and durable bytes have already been
            # independently attested. A later UI tooltip must not turn this
            # known save into an unknown effect or cause another SaveAs.
            # The task still observes the goal independently before completion.
            return {'ok': True, 'state': 'verified', 'effects': 'verified',
                    'copy_receipt_sha256': digest(receipt)}
        elif action in {'open_find', 'open_file_dialog', 'close_saved_document'}:
            from .windows_input import send_find, send_open, send_close
            if action == 'close_saved_document':
                recheck_target()
            send = {'open_find': send_find, 'open_file_dialog': send_open, 'close_saved_document': send_close}[action]
            def key_check():
                check()
                if action == 'close_saved_document':
                    from .windows_document_lifecycle import preflight
                    preflight(request['window'], value, action, check)
            epoch[0] = send(before['window']['hwnd'], check=key_check,
                owns=lambda hwnd: _owned(hwnd, before['window']['hwnd']),
                should_stop=lambda: (root / 'cancel.flag').exists())
            if win32gui.GetForegroundWindow() not in [before['window']['hwnd']] and not _owned(win32gui.GetForegroundWindow(), before['window']['hwnd']):
                raise ComputerError('user_takeover')
        else:
            recheck_target()
            wrapper = wrappers[effective['id']]
            if mcp_reader is not None:
                if win32gui.GetForegroundWindow() != mcp_hwnd:
                    raise mcp_foreground_error('user_takeover')
                mcp_reader.perform_bound(mcp_hwnd, mcp_element, action, value)
                # Synthetic MCP input must not look like human takeover to the
                # subsequent independent native readback checks.
                epoch[0] = _input_epoch()
                mcp_reader.check_input()
                original_check = check
                def mcp_check():
                    original_check()
                    mcp_reader.check_input()
                check = mcp_check
            elif winapp_reader is not None:
                winapp_reader.perform_bound(winapp_hwnd, winapp_element, action, value)
            elif action == 'focus':
                if control['type'] != 'Window':
                    wrapper.element_info.element.SetFocus()
            elif action in {'set_value', 'set_file_path'}:
                if wrapper.iface_value.CurrentIsReadOnly:
                    raise ComputerError('control_readonly')
                if action == 'set_file_path':
                    from .windows_file_dialog import decode_token
                    wrapper.iface_value.SetValue(decode_token(value)['path'])
                else:
                    wrapper.iface_value.SetValue(value)
            elif action in {'open_local_file', 'open_readonly'}:
                wrapper.iface_invoke.Invoke()
            elif action == 'select':
                wrapper.iface_selection_item.Select()
            elif action == 'expand':
                wrapper.iface_expand_collapse.Expand()
            elif action == 'collapse':
                wrapper.iface_expand_collapse.Collapse()
            elif action == 'reveal':
                wrapper.iface_scroll_item.ScrollIntoView()
            elif action == 'toggle':
                from .windows_controls import perform
                perform(wrapper, control, action, value)
        deadline = time.monotonic() + 3
        workbook_readonly_used = False
        spreadsheet_open = False
        if action == 'open_local_file':
            from .windows_file_dialog import decode_token
            spreadsheet_open = Path(decode_token(value)['path']).suffix.casefold() == '.xlsx'
        while True:
            check()
            after, after_wrappers = _request_observation(request, check)
            if after.get('blocked_reason'):
                return {'ok': False, 'state': 'unknown', 'effects': 'unknown',
                        'code': 'windows_owned_dialog_after_action', 'observation': _safe_blocked_observation(after)}
            if (spreadsheet_open and not workbook_readonly_used
                    and any(d.get('title') == '文件正在使用' for d in after.get('dialogs', []))):
                from .windows_file_dialog import decode_token
                from .windows_file_status import workbook_occupied_controls
                target_file = decode_token(value)
                # This same native operation has just verified and invoked the
                # full approved path. Never reuse a basename-only prompt from
                # an earlier/unknown operation or authorize a generic invoke.
                if file_fields is None or any(c.get('value') != target_file['path'] for c in file_fields):
                    raise ComputerError('windows_occupied_prompt_unverified')
                message, button = workbook_occupied_controls(after, target_file['path'])
                current, current_wrappers = _request_observation(request, check)
                if digest(current) != digest(after):
                    raise ComputerError('screen_changed_reobserve')
                dialog = after['dialogs'][0]
                uia._recheck(request['window'])
                uia._recheck(dialog)
                if not _owned(dialog['hwnd'], request['window']['hwnd']) or not _process_owned(dialog, request['window']):
                    raise ComputerError('windows_dialog_identity_changed')
                for expected in (message, button):
                    actual = _native_record(current_wrappers[expected['id']], dialog['pid'])
                    if actual is None or any(actual.get(k) != v for k, v in expected.items()
                            if k not in {'owner_hwnd', 'ancestors'}):
                        raise ComputerError('screen_changed_reobserve')
                check()
                decode_token(value)
                write_new(root / 'workbook-readonly-intent.json', {
                    'request_sha': digest(request), 'approved_file': target_file,
                    'dialog_sha': digest(after), 'button_id': button['id']})
                workbook_readonly_used = True
                current_wrappers[button['id']].iface_invoke.Invoke()
                deadline = time.monotonic() + 3
                continue
            verified = False
            if action == 'select_content_match':
                from .windows_content_find import verified as content_verified
                verified = content_verified(after, value)
            elif action in {'save_document_copy', 'close_saved_document'}:
                from .windows_document_lifecycle import verified as lifecycle_verified
                verified = lifecycle_verified(action, after, value)
            elif action in {'open_file_dialog', 'set_file_path', 'open_local_file', 'open_readonly'}:
                from .windows_file_actions import verified as file_verified
                verified = file_verified(action, after, value)
                if verified and (action == 'open_readonly' or workbook_readonly_used):
                    from .windows_file_dialog import document_status, decode_token
                    status = document_status(request['window'], decode_token(value)['path'], check)
                    after['readonly_attestation'] = status
                    verified = status['active'] and status['read_only'] is True
            elif action == 'open_find':
                verified = len(after['dialogs']) == 1
            else:
                selector = {k: control[k] for k in ('type', 'name', 'automation_id')}
                options = [selector, *[{**selector, 'ancestor': a} for a in control.get('ancestors', [])
                                      if a.get('name') or a.get('automation_id')]]
                if action == 'reveal':
                    options = [request['selector']]
                actual = None
                for option in options:
                    try:
                        actual = locate(after, option)
                        if actual and actual.get('owner_hwnd') == control.get('owner_hwnd'):
                            break
                        actual = None
                    except ComputerError:
                        continue
                if actual:
                    verified = ((action == 'set_value' and actual['value'] == value)
                                or (action == 'focus' and actual['focused'])
                                or (action == 'select' and actual['selected'] is True)
                                or (action == 'expand' and actual['expanded'] == 1)
                                or (action == 'collapse' and actual['expanded'] == 0))
                    if action == 'toggle':
                        verified = type(actual.get('checked')) is int and actual['checked'] == int(value)
                    if action == 'reveal':
                        verified = actual['visible'] and all(actual.get(k) == control.get(k)
                            for k in ('selected', 'expanded', 'value'))
            if verified and not after.get('truncated') and not after.get('blocked_reason'):
                return {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': after}
            if time.monotonic() > deadline:
                return {'ok': False, 'state': 'unknown', 'effects': 'unknown',
                        'code': 'windows_postcondition_failed', 'observation': after}
            time.sleep(.08)


class WindowsBackend:
    def __init__(self, parent=BASE, *, seconds=20):
        if not 0 < seconds <= 30:
            raise ComputerError('deadline_invalid')
        self.parent = safe_root(parent)
        self.seconds = seconds
        self.input_epoch = None
        self.target_selectors = {}
        self.document_tokens = {}

    def begin_run(self):
        self.input_epoch = _input_epoch()

    def end_run(self):
        self.input_epoch = None
        self.target_selectors.clear()
        self.document_tokens.clear()

    def _call(self, request, *, stop=lambda: False, root=None):
        from run_jarvis_desktop import desktop_python
        from .computer_host import grant_foreground
        from .process import WindowsJob
        root = safe_root(root or self.parent / ('observe-' + uuid.uuid4().hex))
        root.mkdir(parents=True, exist_ok=True)
        if request['op'] == 'act' or self.input_epoch is not None:
            if self.input_epoch is None:
                self.input_epoch = _input_epoch()
            request = dict(request, input_epoch=self.input_epoch)
        write_new(root / 'native-request.json', request)
        if stop():
            return {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'user_cancelled'}
        from .windows_visual_forms import ACTION as form_action, is_goal as form_goal
        from .windows_reviewed_controls import ACTION as reviewed_action, is_goal as reviewed_goal
        run_seconds = (2*self.seconds if request.get('op') == 'act'
                       and ((request.get('action') == form_action and form_goal(request.get('selector')))
                            or (request.get('action') == reviewed_action and reviewed_goal(request.get('selector'))))
                       else self.seconds)
        command = [desktop_python(), '-B', '-m', 'agent_service.windows_access', str(root), '--seconds', str(run_seconds)]
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        job = None
        try:
            job = WindowsJob(process)
            ready_path = root / 'native-ready.json'
            ready_until = time.monotonic() + min(4, self.seconds)
            while not ready_path.exists() and process.poll() is None and not stop() and time.monotonic() < ready_until:
                time.sleep(.02)
            if not ready_path.exists() or stop():
                return {'ok': False, 'state': 'failed', 'effects': 'none', 'code': 'windows_worker_not_ready'}
            worker_pid = _owned_worker(job, read_record(ready_path))
            from .windows_visual import is_goal as visual_goal
            from .windows_mcp import PROVIDER_BINDING as MCP_PROVIDER_BINDING
            if (request.get('action') in {'open_find', 'open_file_dialog', 'close_saved_document', 'focus', 'select_content_match'}
                    or (request.get('op') == 'act' and request.get('provider') == MCP_PROVIDER_BINDING)
                    or request.get('op') in {'visual_snapshot','visual_form_snapshot','reviewed_snapshot','vision_frame'}
                    or request.get('selector',{}).get('type') in {'VisualForm','ReviewedControl'}
                    or (request.get('op') == 'observe' and visual_goal(request.get('selector')))):
                if not grant_foreground(worker_pid):
                    return {'ok': False, 'state': 'failed', 'effects': 'none',
                            'code': 'foreground_permission_denied',
                            'foreground': _foreground_context()}
            process.stdin.write(b'1')
            process.stdin.flush()
            end = time.monotonic() + run_seconds
            while process.poll() is None and time.monotonic() < end and not stop():
                time.sleep(.05)
            if process.poll() is None:
                (root / 'cancel.flag').touch(exist_ok=True)
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
            if (root / 'native-outcome.json').is_file():
                result = read_record(root / 'native-outcome.json')
                if result.get('input_epoch') is not None:
                    self.input_epoch = result.pop('input_epoch')
                return result
            effect = (root / 'native-intent.json').exists()
            return {'ok': False, 'state': 'unknown' if effect else 'failed',
                    'effects': 'unknown' if effect else 'none', 'code': 'windows_worker_interrupted'}
        except Exception as exc:
            effect = (root / 'native-intent.json').exists()
            return {'ok': False, 'state': 'unknown' if effect else 'failed',
                    'effects': 'unknown' if effect else 'none',
                    'code': str(exc) if isinstance(exc, ComputerError) else 'windows_supervisor_failed'}
        finally:
            if job:
                job.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdin.close()

    def list_windows(self, *, stop=lambda: False):
        result = self._call({'op': 'list'}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['windows']

    def window_presence(self, window, *, stop=lambda: False):
        result = self._call({'op': 'window_presence', 'window': window}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        if type(result.get('exists')) is not bool:
            raise ComputerError('windows_visual_rebind_presence_invalid')
        return result['exists']

    def visual_document_identity(self, window, *, stop=lambda: False):
        root = safe_root(self.parent / ('observe-' + uuid.uuid4().hex))
        result = self._call({'op': 'visual_document_identity', 'window': window}, stop=stop, root=root)
        if not result['ok']:
            raise ComputerError(result['code'])
        from .windows_visual_binding import verify
        from hashlib import sha256
        document = deepcopy(result['document'])
        document['native_proof'] = {'root': str(root),
            'request_sha256': sha256((root / 'native-request.json').read_bytes()).hexdigest(),
            'outcome_sha256': sha256((root / 'native-outcome.json').read_bytes()).hexdigest()}
        return verify(document, window=window)

    def vision_frame(self, window, *, stop=lambda: False):
        result = self._call({'op': 'vision_frame', 'window': window}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['frame']

    def visual_snapshot(self, window, *, stop=lambda: False):
        result = self._call({'op': 'visual_snapshot', 'window': window}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['snapshot']

    def observe(self, window, *, stop=lambda: False):
        result = self._call({'op': 'observe', 'window': window}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        observed=result['observation']
        from .windows_visual_form_native import FIND_TITLES
        popups=observed.get('blocked_windows',[]) or observed.get('dialogs',[])
        from .windows_file_dialog import OPEN_NAMES
        reviewed_kind=('explorer_view' if observed.get('application')=='explorer' and not popups and not observed.get('blocked_reason')
                       else 'open_dialog' if observed.get('application')=='wps' and len(popups)==1 and popups[0].get('title') in OPEN_NAMES else None)
        if reviewed_kind:
            snapshot_window = window
            if reviewed_kind == 'explorer_view':
                current_window = observed.get('window')
                if type(current_window) is not dict or not same_identity(current_window, window):
                    raise ComputerError('windows_observation_identity_mismatch')
                # Explorer navigation changes its title. Use the identity just
                # read by the guarded native worker, without widening identity.
                snapshot_window = current_window
            try:
                snapshot=self.reviewed_snapshot(snapshot_window,reviewed_kind,stop=stop)
            except ComputerError as exc:
                if stop():raise
                # Ordinary observations remain available; a failed specialised
                # proof never provides a candidate or weakens a popup gate.
                observed['reviewed_unavailable']=str(exc)
                if reviewed_kind=='open_dialog':observed['blocked_reason']=str(exc)
            else:
                # A specialised Explorer proof supplements a complete UIA tree.
                # Dropping it made ordinary navigation disappear from planning.
                if (reviewed_kind == 'explorer_view' and observed.get('window') == snapshot['window']
                        and observed.get('truncated') is False and observed.get('dialogs') == []):
                    return {**observed, 'reviewed_snapshot': snapshot}
                return {'window':snapshot['window'],'application':snapshot['application'],'controls':[],
                        'dialogs':[],'scope':'visible_controls','truncated':False,'reviewed_snapshot':snapshot}
        if (observed.get('application')=='wps' and len(popups)==1
                and popups[0].get('title') in FIND_TITLES and not observed.get('truncated')):
            snapshot=self.visual_form_snapshot(window,stop=stop)
            return {'window':snapshot['window'],'application':'wps','controls':[],
                    'dialogs':[{**snapshot['dialog'],'dialog_kind':'reviewed_find'}],
                    'scope':'visible_controls','truncated':False,'form_snapshot':snapshot}
        return observed

    def reviewed_snapshot(self,window,kind,*,stop=lambda:False):
        from hashlib import sha256
        from .windows_reviewed_controls import verify_snapshot
        root=safe_root(self.parent/('observe-'+uuid.uuid4().hex))
        result=self._call({'op':'reviewed_snapshot','window':window,'kind':kind},stop=stop,root=root)
        if not result['ok']:raise ComputerError(result['code'])
        snapshot=deepcopy(result['snapshot'])
        snapshot['native_proof']={'root':str(root),
            'request_sha256':sha256((root/'native-request.json').read_bytes()).hexdigest(),
            'outcome_sha256':sha256((root/'native-outcome.json').read_bytes()).hexdigest()}
        return verify_snapshot(snapshot)

    def visual_form_snapshot(self,window,*,stop=lambda:False):
        from hashlib import sha256
        from .windows_visual_forms import verify_snapshot
        root=safe_root(self.parent/('observe-'+uuid.uuid4().hex))
        result=self._call({'op':'visual_form_snapshot','window':window},stop=stop,root=root)
        if not result['ok']:raise ComputerError(result['code'])
        snapshot=deepcopy(result['snapshot'])
        snapshot['native_proof']={'root':str(root),
            'request_sha256':sha256((root/'native-request.json').read_bytes()).hexdigest(),
            'outcome_sha256':sha256((root/'native-outcome.json').read_bytes()).hexdigest()}
        return verify_snapshot(snapshot)

    def observe_target(self, window, selector, *, stop=lambda: False):
        from .windows_task import _selector
        _selector(selector)
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens.pop(window['hwnd'], None)
        result = self._call({'op': 'observe', 'window': window, 'selector': selector}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def observe_condition(self, condition, *, stop=lambda: False):
        from .windows_file_actions import is_goal
        from .windows_document_lifecycle import is_goal as lifecycle_goal
        from .windows_content_find import is_goal as content_goal
        from .windows_visual import is_goal as visual_goal, decode as visual_decode
        window, selector = condition['window'], condition['selector']
        from .windows_visual_forms import is_goal as form_goal, decode as form_decode
        from .windows_reviewed_controls import is_goal as reviewed_goal, decode as reviewed_decode
        if visual_goal(selector) or form_goal(selector) or reviewed_goal(selector):
            if condition['state'] != 'value':
                raise ComputerError('windows_visual_goal_invalid')
            (reviewed_decode if reviewed_goal(selector) else form_decode if form_goal(selector) else visual_decode)(selector, condition['value'])
            self.target_selectors[window['hwnd']] = deepcopy(selector)
            self.document_tokens[window['hwnd']] = condition['value']
            result = self._call({'op':'observe', 'window':window, 'selector':selector,
                                'document_token':condition['value']},stop=stop)
            if not result['ok']:
                raise ComputerError(result['code'])
            return result['observation']
        if not is_goal(selector) and not lifecycle_goal(selector) and not content_goal(selector):
            return self.observe_target(window, selector, stop=stop)
        if condition['state'] != 'value':
            raise ComputerError('windows_file_goal_invalid')
        if content_goal(selector):
            from .windows_content_query import decode
            decode(condition['value'])
        elif lifecycle_goal(selector):
            from .windows_document_copy import decode
            decode(condition['value'])
        else:
            from .windows_file_dialog import decode_token
            decode_token(condition['value'])
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens[window['hwnd']] = condition['value']
        result = self._call({'op': 'observe', 'window': window, 'selector': selector,
                             'document_token': condition['value']}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    candidates = staticmethod(candidates)

    def act(self, window, control, action, value, before_sha, operation_root, *, stop=lambda: False):
        request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                   'value': value, 'before_sha': before_sha}
        if window['hwnd'] in self.target_selectors:
            request['selector'] = self.target_selectors[window['hwnd']]
        if window['hwnd'] in self.document_tokens:
            request['document_token'] = self.document_tokens[window['hwnd']]
        return self._call(request, root=operation_root, stop=stop)


class WinappBackend(WindowsBackend):
    """Opt-in provider: existing observations, policy and independent readback."""
    from .windows_winapp import PROVIDER_BINDING as provider_binding
    allow_visual_options = False

    @staticmethod
    def allowed(observation, control, action, value):
        bounds = control.get('bounds') if type(control) is dict else None
        return (action in {'set_value', 'select', 'expand', 'collapse', 'toggle'}
                and type(bounds) is list and len(bounds) == 4 and all(type(v) is int for v in bounds)
                and bounds[2] > bounds[0] and bounds[3] > bounds[1]
                and (action not in {'expand', 'collapse'} or
                     (type(control.get('expanded')) is int and control['expanded'] in (0, 1)))
                and allowed(observation, control, action, value))

    def observe(self, window, *, stop=lambda: False):
        result = self._call({'op': 'observe', 'window': window, 'provider': self.provider_binding}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def observe_target(self, window, selector, *, stop=lambda: False):
        from .windows_task import _selector
        _selector(selector)
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens.pop(window['hwnd'], None)
        result = self._call({'op': 'observe', 'window': window, 'selector': selector,
                             'provider': self.provider_binding}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def observe_condition(self, condition, *, stop=lambda: False):
        return self.observe_target(condition['window'], condition['selector'], stop=stop)

    @staticmethod
    def candidates(observation, condition):
        from . import windows_visual as visual, windows_visual_forms as forms, windows_reviewed_controls as reviewed
        from . import windows_file_actions as file_actions, windows_document_lifecycle as lifecycle, windows_content_find as content
        if any(module.is_goal(condition['selector']) for module in (visual, forms, reviewed, file_actions, lifecycle, content)):
            return []
        options = candidates(observation, condition)
        controls = {control['id']: control for control in observation['controls']}
        return [item for item in options if WinappBackend.allowed(observation, controls.get(item['control_id']),
                                                                 item['action'], item['value'])]

    def act(self, window, control, action, value, before_sha, operation_root, *, stop=lambda: False):
        request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                   'value': value, 'before_sha': before_sha, 'provider': self.provider_binding}
        if window['hwnd'] in self.target_selectors:
            request['selector'] = self.target_selectors[window['hwnd']]
        if window['hwnd'] in self.document_tokens:
            request['document_token'] = self.document_tokens[window['hwnd']]
        return self._call(request, root=operation_root, stop=stop)


def backend_for_task(root, fallback=None):
    """Recover the backend named by a locked, validated task snapshot."""
    from .windows_task import load
    binding = load(root)['provider_binding']
    return backend_for_binding(binding, fallback=fallback)


def backend_for_binding(binding, fallback=None):
    from .windows_winapp import PROVIDER_BINDING, OFFICE_PROVIDER_BINDING
    from .windows_mcp import PROVIDER_BINDING as MCP_PROVIDER_BINDING
    from .windows_auto import PROVIDER_BINDING as AUTO_PROVIDER_BINDING
    if binding is None:
        if fallback is not None and getattr(fallback, 'provider_binding', None) is None:
            return fallback
        return WindowsBackend()
    if binding not in (PROVIDER_BINDING, OFFICE_PROVIDER_BINDING, MCP_PROVIDER_BINDING, AUTO_PROVIDER_BINDING):
        raise ComputerError('windows_provider_binding_invalid')
    if fallback is not None and getattr(fallback, 'provider_binding', None) == binding:
        return fallback
    if binding == MCP_PROVIDER_BINDING:
        return WindowsMcpBackend()
    if binding == AUTO_PROVIDER_BINDING:
        return AutoWindowsBackend()
    return WinappOfficeBackend() if binding == OFFICE_PROVIDER_BINDING else WinappBackend()


class WindowsMcpBackend(WinappBackend):
    """Isolated MCP dispatch with original native policy and readback."""
    from .windows_mcp import PROVIDER_BINDING as provider_binding

    def __init__(self, parent=BASE, *, seconds=30):
        super().__init__(parent, seconds=seconds)

    @staticmethod
    def allowed(observation, control, action, value):
        return (action in {'select', 'toggle'}
                and (action != 'select' or type(control) is dict
                     and control.get('type') in {'TabItem', 'RadioButton'})
                and WinappBackend.allowed(observation, control, action, value))

    @staticmethod
    def candidates(observation, condition):
        options = WinappBackend.candidates(observation, condition)
        controls = {control['id']: control for control in observation['controls']}
        return [item for item in options if WindowsMcpBackend.allowed(observation,
                controls.get(item['control_id']), item['action'], item['value'])]


class WinappOfficeBackend(WinappBackend):
    from .windows_winapp import OFFICE_PROVIDER_BINDING as provider_binding
    allow_office_options = True

    def office_snapshot(self, window, *, stop=lambda: False):
        from hashlib import sha256
        from .windows_winapp_office import verify_snapshot
        root = safe_root(self.parent / ('observe-' + uuid.uuid4().hex))
        result = self._call({'op': 'office_snapshot', 'window': window, 'provider': self.provider_binding},
                            stop=stop, root=root)
        if not result['ok']:
            raise ComputerError(result['code'])
        snapshot = deepcopy(result['snapshot'])
        snapshot['native_proof'] = {'root': str(root),
            'request_sha256': sha256((root / 'native-request.json').read_bytes()).hexdigest(),
            'outcome_sha256': sha256((root / 'native-outcome.json').read_bytes()).hexdigest()}
        return verify_snapshot(snapshot)

    def observe_condition(self, condition, *, stop=lambda: False):
        from .windows_winapp_office import is_goal, decode
        if not is_goal(condition['selector']):
            return super().observe_condition(condition, stop=stop)
        decode(condition['selector'], condition['value'])
        window, selector = condition['window'], condition['selector']
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens[window['hwnd']] = condition['value']
        result = self._call({'op': 'observe', 'window': window, 'selector': selector,
            'document_token': condition['value'], 'provider': self.provider_binding}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    @staticmethod
    def candidates(observation, condition):
        from .windows_winapp_office import is_goal, candidates as office_candidates
        return office_candidates(observation, condition) if is_goal(condition['selector']) else WinappBackend.candidates(observation, condition)


class AutoWindowsBackend(WindowsBackend):
    """Persist one reviewed route per action while retaining the auto task binding."""
    from .windows_auto import PROVIDER_BINDING as provider_binding
    allow_visual_options = False
    allow_office_options = True

    def __init__(self, parent=BASE, *, seconds=30):
        super().__init__(parent, seconds=seconds)
        self.auto_pending = {}

    def end_run(self):
        super().end_run()
        self.auto_pending.clear()

    @staticmethod
    def allowed(observation, control, action, value):
        from .windows_auto import route
        return route(observation, control, action, value) is not None

    def observe(self, window, *, stop=lambda: False):
        from .windows_winapp import PROVIDER_BINDING
        result = self._call({'op': 'observe', 'window': window, 'provider': PROVIDER_BINDING}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def office_snapshot(self, window, *, stop=lambda: False):
        # Source proofs keep the concrete Office binding expected by its verifier.
        backend = WinappOfficeBackend(self.parent, seconds=self.seconds)
        return backend.office_snapshot(window, stop=stop)

    def observe_target(self, window, selector, *, stop=lambda: False):
        from .windows_winapp import PROVIDER_BINDING
        from .windows_task import _selector
        _selector(selector)
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens.pop(window['hwnd'], None)
        result = self._call({'op': 'observe', 'window': window, 'selector': selector,
                             'provider': PROVIDER_BINDING}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def observe_condition(self, condition, *, stop=lambda: False):
        from .windows_winapp_office import is_goal, decode
        from .windows_winapp import OFFICE_PROVIDER_BINDING
        if not is_goal(condition['selector']):
            return super().observe_condition(condition, stop=stop)
        decode(condition['selector'], condition['value'])
        window, selector = condition['window'], condition['selector']
        self.target_selectors[window['hwnd']] = deepcopy(selector)
        self.document_tokens[window['hwnd']] = condition['value']
        result = self._call({'op': 'observe', 'window': window, 'selector': selector,
            'document_token': condition['value'], 'provider': OFFICE_PROVIDER_BINDING}, stop=stop)
        if not result['ok']:
            raise ComputerError(result['code'])
        return result['observation']

    def candidates(self, observation, condition):
        from .windows_auto import options
        result = options(observation, condition)
        self.auto_pending[condition['window']['hwnd']] = (deepcopy(observation), deepcopy(condition))
        return result

    def act(self, window, control, action, value, before_sha, operation_root, *, stop=lambda: False):
        from .windows_auto import route, options, verify_request
        pending = self.auto_pending.pop(window['hwnd'], None)
        if pending is None:
            raise ComputerError('windows_auto_route_missing')
        observed, condition = pending
        candidate = {'control_id': control['id'] if control else None, 'action': action, 'value': value}
        selected = route(observed, control, action, value, condition['selector'])
        if (digest(observed) != before_sha or condition['window'] != window or selected is None
                or options(observed, condition) != [candidate]):
            raise ComputerError('windows_auto_route_invalid')
        root = safe_root(operation_root)
        root.mkdir(parents=True, exist_ok=True)
        proof = {'schema': 1, 'policy_binding': deepcopy(self.provider_binding),
                 'condition': condition, 'observation': observed, 'selected': selected}
        write_new(root / 'auto-route.json', proof)
        request = {'op': 'act', 'window': window, 'control': control, 'action': action,
                   'value': value, 'before_sha': before_sha, 'selector': condition['selector'],
                   'auto_route_sha256': digest(proof)}
        if selected['provider'] is not None:
            request['provider'] = selected['provider']
        if window['hwnd'] in self.document_tokens:
            request['document_token'] = self.document_tokens[window['hwnd']]
        verify_request(request, root)
        return self._call(request, root=root, stop=stop)


def worker(root, seconds):
    root = safe_root(root)
    # Initialize this actual worker's USER message queue before asking its parent
    # for a foreground grant. A venv launcher is not the UIA executor.
    import ctypes as c
    from ctypes import wintypes as w
    msg = w.MSG()
    c.windll.user32.PeekMessageW(c.byref(msg), None, 0, 0, 0)
    write_new(root / 'native-ready.pending', {'pid': os.getpid()})
    os.rename(root / 'native-ready.pending', root / 'native-ready.json')
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    request = read_record(root / 'native-request.json')
    deadline = time.monotonic() + seconds
    cancelled = threading.Event()
    finished = threading.Event()
    epoch = [request.get('input_epoch')]
    def parent_watch():
        sys.stdin.buffer.read(1)
        if not finished.is_set():
            cancelled.set()
    def watchdog():
        while not finished.wait(.1):
            if cancelled.is_set() or time.monotonic() > deadline + 1:
                os._exit(3)
    threading.Thread(target=parent_watch, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    def check():
        if cancelled.is_set() or (root / 'cancel.flag').exists() or time.monotonic() >= deadline:
            raise ComputerError('windows_cancelled_or_timeout')
        if epoch[0] is not None:
            import win32api
            if win32api.GetLastInputInfo() != epoch[0]:
                raise ComputerError('user_takeover')
    try:
        check()
        result = _perform(request, root, check, epoch)
    except Exception as exc:
        effect = (root / 'native-intent.json').exists()
        copy_diagnostic = None
        if request.get('action') == 'save_document_copy':
            from .windows_document_copy import CopyError
            if type(exc) is CopyError:
                if exc.no_external_effect is True:
                    effect = False
                copy_diagnostic = exc.copy_diagnostic
        result = {'ok': False, 'state': 'unknown' if effect else 'failed',
                  'effects': 'unknown' if effect else 'none',
                  'code': str(exc) if isinstance(exc, (ComputerError, uia.UIAError)) else 'windows_native_failed'}
        if copy_diagnostic is not None:
            result['copy_diagnostic'] = copy_diagnostic
        if getattr(exc, 'input_diagnostic', None) is not None:
            result['input_diagnostic'] = exc.input_diagnostic
        if getattr(exc, 'document_diagnostic', None) is not None:
            result['document_diagnostic'] = exc.document_diagnostic
        if getattr(exc, 'content_diagnostic', None) is not None:
            result['content_diagnostic'] = exc.content_diagnostic
        if result['code'] == 'windows_native_failed':
            import traceback
            frames = traceback.extract_tb(exc.__traceback__)
            result['error_type'] = type(exc).__name__
            if type(getattr(exc,'hresult',None)) is int:
                result['error_hresult'] = exc.hresult
            if frames and Path(frames[-1].filename).parent == Path(__file__).parent:
                result['failure_location'] = {'file': Path(frames[-1].filename).name,
                                              'line': frames[-1].lineno, 'function': frames[-1].name}
    if epoch[0] is not None:
        result['input_epoch'] = epoch[0]
    write_new(root / 'native-outcome.json', result)
    finished.set()
    os._exit(0 if result['ok'] else 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('--seconds', type=float, default=20)
    args = parser.parse_args()
    worker(Path(args.root), args.seconds)
