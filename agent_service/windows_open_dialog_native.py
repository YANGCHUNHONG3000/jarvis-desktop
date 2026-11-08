"""Native, reviewed filename and Cancel roles in an existing WPS Open dialog.

This adapter never opens a file or creates a dialog. Approval, immutable native
request/outcome proof, target-file tokens and durable intent belong to the caller.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from .computer import ComputerError, safe_root
from . import computer_uia as uia, windows_access as access
from . import windows_file_dialog as files, windows_visual_binding as binding

CANCEL_NAMES = {'取消', '取消(C)', '取消(&C)', 'Cancel', '&Cancel'}
_WINDOW_KEYS = {'hwnd', 'pid', 'created', 'image', 'class_name', 'profile', 'title'}
_CONTROL_KEYS = {'id', 'runtime_id', 'type', 'name', 'automation_id', 'value',
                 'enabled', 'visible', 'patterns', 'ancestors', 'owner_hwnd'}


class _DialogDisappeared(ComputerError):
    """An exact candidate HWND disappeared during a read, never an action."""


def _window(value):
    if (type(value) is not dict or set(value) != _WINDOW_KEYS
            or any(type(value[k]) is not int or value[k] <= 0 for k in ('hwnd', 'pid'))
            or any(type(value[k]) is not str or not value[k] or len(value[k]) > 32768
                   for k in _WINDOW_KEYS - {'hwnd', 'pid'})):
        raise ComputerError('windows_open_dialog_identity_invalid')


def _process_facts(pid):
    """Read exact process identity without requiring a visible HWND."""
    import ctypes as c
    from ctypes import wintypes as w
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x1000 | 0x100000, False, pid)
    try:
        query = c.WinDLL('kernel32', use_last_error=True).QueryFullProcessImageNameW
        query.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        query.restype = w.BOOL
        length = w.DWORD(32768)
        image = c.create_unicode_buffer(length.value)
        if not query(int(handle), 0, image, c.byref(length)):
            raise ComputerError('windows_open_dialog_process_unavailable')
        return image.value, str(win32process.GetProcessTimes(handle)['CreationTime'])
    finally:
        handle.Close()


def _hidden_identity(expected, parent, check):
    """Attest a hidden original handle; hidden never proves cancellation."""
    import win32gui
    import win32process
    hwnd = expected['hwnd']
    check()
    try:
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        owner = win32gui.GetWindow(hwnd, 4)
        owner_pid = win32process.GetWindowThreadProcessId(owner)[1] if owner else None
        owner_root = win32gui.GetAncestor(owner, 2) if owner else None
        visible = win32gui.IsWindowVisible(hwnd)
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        owned = access._owned(hwnd, parent['hwnd'])
        owner_child = bool(win32gui.IsChild(parent['hwnd'], owner))
    except Exception as exc:
        # Only the native invalid-window-handle error is a disappearance hint.
        # Permission, process and root errors are never converted to success.
        if getattr(exc, 'winerror', None) == 1400 and not win32gui.IsWindow(hwnd):
            raise _DialogDisappeared('windows_open_dialog_changed') from exc
        raise
    if not win32gui.IsWindow(hwnd):
        raise _DialogDisappeared('windows_open_dialog_changed')
    if (visible or pid != expected['pid'] or title != expected['title']
            or cls != expected['class_name'] or not owned
            or owner_pid != parent['pid']
            or not (owner == parent['hwnd'] or (owner_child and owner_root == parent['hwnd']))
            or _process_facts(pid) != (expected['image'], expected['created'])
            or not access._process_owned(expected, parent)):
        raise ComputerError('windows_open_dialog_changed')
    if not win32gui.IsWindow(hwnd):
        raise _DialogDisappeared('windows_open_dialog_changed')
    check()


def _thread_input(tid):
    """Native input ownership for one already bound application thread."""
    import ctypes as c
    from ctypes import wintypes as w
    class GUI(c.Structure):
        _fields_ = [('size', w.DWORD), ('flags', w.DWORD), ('active', w.HWND),
                    ('focus', w.HWND), ('capture', w.HWND), ('menu_owner', w.HWND),
                    ('move_size', w.HWND), ('caret', w.HWND), ('rect', w.RECT)]
    info = GUI()
    info.size = c.sizeof(info)
    query = c.WinDLL('user32', use_last_error=True).GetGUIThreadInfo
    query.argtypes = [w.DWORD, c.POINTER(GUI)]
    query.restype = w.BOOL
    if not query(tid, c.byref(info)):
        raise ComputerError('windows_open_dialog_input_unavailable')
    return {key: getattr(info, key) for key in ('flags', 'capture', 'menu_owner', 'move_size')}


def _input_guard(parent, dialog, check):
    """Reject thread-local menus/capture even when a Qt popup has no owner."""
    import win32gui
    import win32process
    threads = set()
    for identity in (parent, dialog):
        if identity is None or not win32gui.IsWindow(identity['hwnd']):
            continue
        check()
        tid, pid = win32process.GetWindowThreadProcessId(identity['hwnd'])
        if not tid or pid != identity['pid']:
            raise ComputerError('windows_open_dialog_changed')
        threads.add((tid, pid))
    for tid, _ in threads:
        state = _thread_input(tid)
        # GUI_INMOVESIZE / INMENUMODE / SYSTEMMENUMODE / POPUPMENUMODE.
        # WPS Qt menus can report flags=0 while retaining native mouse capture.
        if (state['flags'] & 0x1e or state['capture'] or state['menu_owner'] or state['move_size']):
            raise ComputerError('windows_open_dialog_input_busy')
    def visit(hwnd, _):
        check()
        if not win32gui.IsWindowVisible(hwnd):
            return
        thread = tuple(win32process.GetWindowThreadProcessId(hwnd))
        if (thread not in threads and not (dialog is not None and thread[1] == dialog['pid']
                                          and not win32gui.IsWindow(dialog['hwnd']))):
            return
        cls = win32gui.GetClassName(hwnd)
        # The source has no persisted TID. After original HWND destruction,
        # matching its PID can only reject a visible menu, never grant authority.
        if cls.startswith('Qt5QWindowPopup') or cls == '#32768':
            raise ComputerError('windows_open_dialog_input_busy')
    win32gui.EnumWindows(visit, None)
    check()
    if uia._recheck(parent) != parent:
        raise ComputerError('windows_open_dialog_window_changed')


def _guard(window, check, *, expected=None, allow_closed=False, foreground=False, _closed_retry=False):
    """Only an exact owner/registered-process Open dialog may be observed."""
    import win32gui
    import win32process
    check()
    parent = uia._recheck(window)
    if parent != window or access._kind(parent) != 'wps':
        raise ComputerError('windows_open_dialog_window_changed')
    dialogs = []
    vanished = False

    def visit(hwnd, _):
        nonlocal vanished
        check()
        if hwnd == parent['hwnd'] or not win32gui.IsWindowVisible(hwnd) or not access._owned(hwnd, parent['hwnd']):
            return
        cls = win32gui.GetClassName(hwnd)
        if cls.casefold() == 'tooltips_class32':
            return
        if (cls == 'KLiteMainWindowShadowBorder' and not win32gui.GetWindowText(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == parent['pid']
                and win32gui.GetWindowLong(hwnd, -20) & 0x80080 == 0x80080):
            return
        try:
            candidate = uia._identity(hwnd)
        except uia.UIAError as exc:
            # EnumWindows may deliver the original dialog just as its single
            # approved Cancel destroys it. Only proven HWND destruction permits
            # a fresh read; hidden/reused handles and every other error fail.
            if str(exc) != 'window_unavailable' or win32gui.IsWindow(hwnd):
                raise
            vanished = True
            return
        owner = win32gui.GetWindow(hwnd, 4)
        owner_pid = win32process.GetWindowThreadProcessId(owner)[1] if owner else None
        owner_root = win32gui.GetAncestor(owner, 2) if owner else None
        predicates = {
            'open_dialog': files.is_open_dialog(candidate, ownership_verified=True),
            'process_binding': access._process_owned(candidate, parent),
            'owner_binding': (owner_pid == parent['pid'] and (owner == parent['hwnd']
                or (bool(win32gui.IsChild(parent['hwnd'], owner)) and owner_root == parent['hwnd']))),
        }
        failed = next((key for key, valid in predicates.items() if not valid), None)
        if failed:
            error = ComputerError('windows_open_dialog_popup_requires_attention')
            error.content_diagnostic = {'predicate': failed, 'hwnd': hwnd, 'owner_hwnd': owner,
                                        'owner_pid': owner_pid, 'owner_root_hwnd': owner_root}
            raise error
        dialogs.append(candidate)

    for attempt in range(2):
        dialogs.clear()
        vanished = False
        win32gui.EnumWindows(visit, None)
        check()
        if uia._recheck(window) != parent:
            raise ComputerError('windows_open_dialog_window_changed')
        if not vanished:
            break
        if attempt:
            raise ComputerError('windows_open_dialog_changed')
    if len(dialogs) > 1:
        raise ComputerError('windows_open_dialog_ambiguous')
    _input_guard(parent, dialogs[0] if dialogs else expected, check)
    if not dialogs:
        if not allow_closed or expected is None:
            raise ComputerError('windows_open_dialog_missing')
        hwnd = expected['hwnd']
        if win32gui.IsWindow(hwnd):
            # Hidden never proves cancellation. Reused handles are also rejected.
            try:
                _hidden_identity(expected, parent, check)
            except _DialogDisappeared:
                if _closed_retry or win32gui.IsWindow(hwnd):
                    raise ComputerError('windows_open_dialog_changed') from None
                # Start again from the exact root and enumerate every popup;
                # absence alone never skips a replacement or owner change.
                return _guard(window, check, expected=expected, allow_closed=allow_closed,
                              foreground=foreground, _closed_retry=True)
            return parent, None, 'hidden'
        return parent, None, 'destroyed'
    dialog = dialogs[0]
    if expected is not None and dialog != expected:
        raise ComputerError('windows_open_dialog_changed')
    if not win32gui.IsWindowEnabled(dialog['hwnd']):
        raise ComputerError('windows_open_dialog_popup_requires_attention')
    if foreground and win32gui.GetForegroundWindow() != dialog['hwnd']:
        raise ComputerError('windows_open_dialog_foreground_required')
    check()
    return parent, dialog, 'visible'


def _document(window, check, *, expected_dialog, allow_closed=False):
    """Read the active FullName/status and fixed bytes through existing COM."""
    from . import windows_workbook_binding as books
    parent, dialog, presence = _guard(window, check, expected=expected_dialog, allow_closed=allow_closed)
    paths = set()
    diagnostics = []
    for kind in ('docx', 'xlsx'):
        check()
        try:
            if kind == 'docx':
                _, app = files._document_application(parent, check)
                path = files._local_path(app.ActiveDocument.FullName)
            else:
                _, app, _ = books._workbook_application(parent, check)
                path = files._local_path(app.ActiveWorkbook.FullName)
        except ComputerError as exc:
            if str(exc) not in {'windows_document_anchor_unavailable', 'windows_workbook_anchor_unavailable'}:
                raise
            diagnostics.append({'format': kind, 'stage': 'anchor', 'error': str(exc)})
            continue
        if path.suffix.casefold() != '.' + kind:
            diagnostics.append({'format': kind, 'stage': 'suffix', 'matches': False})
            continue
        status = files.document_status(parent, path, check)
        diagnostics.append({'format': kind, 'stage': 'status', 'status': deepcopy(status)})
        if status.get('active') is True:
            if status.get('saved') is not True or type(status.get('read_only')) is not bool:
                raise ComputerError('windows_open_dialog_document_unsaved')
            paths.add((str(path), status['read_only']))
    if len(paths) != 1:
        error = ComputerError('windows_open_dialog_document_unavailable')
        error.content_diagnostic = {'candidates': diagnostics, 'active_document_count': len(paths)}
        if diagnostics and all(item['stage'] == 'anchor' for item in diagnostics):
            anchors = _anchor_diagnostic(window, check)
            error.content_diagnostic['workbook_anchors'] = anchors
            applications = anchors.get('application_candidates', [])
            if (any(item.get('hresult') == -2147418111 for item in applications)
                    and not any(item.get('active_workbook_readable') is True for item in applications)):
                error.content_diagnostic['reason'] = 'wps_modal_com_rejected'
        raise error
    path, readonly = next(iter(paths))
    facts = binding._file(path)
    if (files.document_status(parent, path, check) != {'active': True, 'saved': True, 'read_only': readonly}
            or binding._file(path) != facts):
        raise ComputerError('windows_open_dialog_document_changed')
    if _guard(window, check, expected=expected_dialog, allow_closed=allow_closed) != (parent, dialog, presence):
        raise ComputerError('windows_open_dialog_changed')
    result = {'schema': 1, **facts, 'format': Path(path).suffix[1:].casefold(), 'application': 'wps',
              'registered_app': str(safe_root(parent['image'])), 'read_only': readonly, 'saved': True}
    return binding.verify(result, require_native=False)


def _anchor_diagnostic(window, check):
    """Bounded PROPERTYGET-only failure evidence; never document authority."""
    from . import windows_workbook_binding as books
    pythoncom, table, iterator, context = books._rot(check)
    result = {'monikers_seen': 0, 'xlsx_candidates': [], 'application_candidates': []}
    for moniker, name in books._monikers(iterator, context, check):
        result['monikers_seen'] += 1
        app_family = {'!{00024500-0000-0000-C000-000000000046}': 'excel',
                      '!{45540001-5750-5300-4B49-4E47534F4655}': 'wps'}.get(name.upper()) if type(name) is str else None
        if app_family and len(result['application_candidates']) < 8:
            app_candidate = {'family': app_family, 'active_workbook_readable': False}
            result['application_candidates'].append(app_candidate)
            try:
                # GetObject binds an existing ROT entry; PROPERTYGET only.
                # This is diagnostic evidence, never an active-document grant.
                app = books._acquire(pythoncom, table, moniker, check)
                book = app.ActiveWorkbook
                path = books._path(book.FullName)
                books._stable_workbook(book, window, path, check, visible=True)
                app_candidate['active_workbook_readable'] = True
            except ComputerError as exc:
                app_candidate['error'] = str(exc)
            except Exception as exc:
                app_candidate['exception_type'] = type(exc).__name__
                code = getattr(exc, 'hresult', None)
                if type(code) is int:
                    app_candidate['hresult'] = code
        if type(name) is not str or not Path(name).is_absolute() or Path(name).suffix.casefold() != '.xlsx':
            continue
        if len(result['xlsx_candidates']) >= 8:
            result['candidate_limit'] = True
            break
        candidate = {}
        result['xlsx_candidates'].append(candidate)
        try:
            approved = books._path(name)
            book = books._acquire(pythoncom, table, moniker, check)
            books._stable_workbook(book, window, approved, check, visible=False)
            candidate['metadata_bound'] = True
        except ComputerError as exc:
            candidate['metadata_bound'] = False
            candidate['error'] = str(exc)
        except Exception as exc:
            candidate['metadata_bound'] = False
            candidate['exception_type'] = type(exc).__name__
        try:
            candidate['visibility'] = books.visibility_diagnostic(window, name, check)
        except ComputerError as exc:
            candidate['visibility_error'] = str(exc)
        except Exception as exc:
            candidate['visibility_exception_type'] = type(exc).__name__
    return result


def _controls(dialog, check):
    # The traversal rejects protected/foreign descendants before reading labels.
    from .windows_visual_form_native import _password_guard
    _password_guard(dialog, check)
    observation, wrappers = access._tree(dialog, check)
    if observation.get('truncated'):
        raise ComputerError('windows_open_dialog_tree_limit')
    records = [{**{k: deepcopy(c[k]) for k in _CONTROL_KEYS - {'owner_hwnd'}},
                'owner_hwnd': dialog['hwnd']} for c in observation['controls']]
    native = {'application': 'wps', 'controls': records,
              'dialogs': [{**dialog, 'dialog_kind': 'open_file'}]}
    field, edit = files.filename_edit(native)
    if not access._direct_child(wrappers[edit['id']], field):
        raise ComputerError('windows_open_dialog_parent_unverified')
    pattern = wrappers[edit['id']].iface_value
    if pattern.CurrentIsReadOnly or pattern.CurrentValue != edit['value']:
        raise ComputerError('windows_open_dialog_value_unverified')
    buttons = [c for c in records if c['type'] == 'Button' and c['name'] in CANCEL_NAMES
               and c['automation_id'] in {'', '2'} and c['visible'] and c['enabled']
               and 'invoke' in c['patterns']
               and not any(a['type'] == 'TitleBar' for a in c['ancestors'])]
    if len(buttons) != 1:
        raise ComputerError('windows_open_dialog_cancel_ambiguous')
    return [{'role': 'filename', 'label': field['name'], 'control': edit, 'parent': field},
            {'role': 'cancel', 'label': buttons[0]['name'], 'control': buttons[0], 'parent': None}], wrappers


def _control(value, hwnd):
    if (type(value) is not dict or set(value) != _CONTROL_KEYS
            or type(value['id']) is not str or re.fullmatch('[a-f0-9]{24}', value['id']) is None
            or type(value['runtime_id']) is not list or not 1 <= len(value['runtime_id']) <= 32
            or any(type(v) is not int for v in value['runtime_id'])
            or sha256(json.dumps(value['runtime_id']).encode()).hexdigest()[:24] != value['id']
            or any(type(value[k]) is not str or len(value[k]) > 300 for k in ('type', 'name', 'automation_id'))
            or value['enabled'] is not True or value['visible'] is not True
            or value['owner_hwnd'] != hwnd or type(value['owner_hwnd']) is not int
            or (value['value'] is not None and (type(value['value']) is not str or len(value['value']) > 4000))
            or type(value['patterns']) is not list or len(value['patterns']) > 16
            or any(type(p) is not str for p in value['patterns'])
            or len(set(value['patterns'])) != len(value['patterns'])
            or type(value['ancestors']) is not list or len(value['ancestors']) > 4):
        raise ComputerError('windows_open_dialog_control_invalid')
    for ancestor in value['ancestors']:
        if (type(ancestor) is not dict or set(ancestor) != {'type', 'name', 'automation_id'}
                or any(type(v) is not str or len(v) > 300 for v in ancestor.values())):
            raise ComputerError('windows_open_dialog_control_invalid')


def _validate(source):
    if (type(source) is not dict or set(source) - {'native_proof'} != {'window', 'application', 'kind', 'binding', 'controls'}
            or source['application'] != 'wps' or source['kind'] != 'open_dialog'
            or type(source['binding']) is not dict
            or not (set(source['binding']) == {'document', 'dialog'}
                    or (set(source['binding']) == {'scope', 'dialog'}
                        and source['binding']['scope'] == 'dialog_controls'))
            or type(source['controls']) is not list or len(source['controls']) != 2):
        raise ComputerError('windows_open_dialog_source_invalid')
    _window(source['window'])
    dialog = source['binding']['dialog']
    _window(dialog)
    if (dialog['hwnd'] == source['window']['hwnd']
            or not files.is_open_dialog(dialog, ownership_verified=True)):
        raise ComputerError('windows_open_dialog_identity_invalid')
    if 'document' in source['binding']:
        _validate_document(source)
    if [item.get('role') for item in source['controls'] if type(item) is dict] != ['filename', 'cancel']:
        raise ComputerError('windows_open_dialog_role_invalid')
    for item in source['controls']:
        if set(item) != {'role', 'label', 'control', 'parent'}:
            raise ComputerError('windows_open_dialog_role_invalid')
        _control(item['control'], dialog['hwnd'])
    filename, cancel = source['controls']
    field, edit, button = filename['parent'], filename['control'], cancel['control']
    _control(field, dialog['hwnd'])
    if (field['type'] != 'ComboBox' or field['name'] not in files.FILENAME_NAMES or field['automation_id'] != ''
            or filename['label'] != field['name'] or edit['type'] != 'Edit' or edit['name'] != ''
            or edit['automation_id'] != '' or 'set_value' not in edit['patterns']
            or not edit['ancestors'] or edit['ancestors'][-1] != {k: field[k] for k in ('type', 'name', 'automation_id')}
            or cancel['parent'] is not None or cancel['label'] != button['name']
            or button['type'] != 'Button' or button['name'] not in CANCEL_NAMES
            or button['automation_id'] not in {'', '2'} or 'invoke' not in button['patterns']
            or any(a['type'] == 'TitleBar' for a in button['ancestors'])
            or len({edit['id'], field['id'], button['id']}) != 3):
        raise ComputerError('windows_open_dialog_role_invalid')
    return source


def _validate_document(source):
    # The historical document-bound source remains a separate strict contract.
    # In particular, missing/unreadable COM authority is never a scope downgrade.
    document = source['binding']['document']
    if (type(document) is not dict or set(document) != {'schema', 'path', 'sha256', 'size', 'format', 'application',
                                                       'registered_app', 'read_only', 'saved'}
            or type(document['schema']) is not int or document['schema'] != 1
            or document['application'] != 'wps' or document['format'] not in {'docx', 'xlsx'}
            or type(document['path']) is not str or not Path(document['path']).is_absolute()
            or Path(document['path']).suffix.casefold() != '.' + document['format']
            or type(document['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', document['sha256']) is None
            or type(document['size']) is not int or not 0 < document['size'] <= files.MAX_FILE
            or type(document['read_only']) is not bool or document['saved'] is not True
            or document['registered_app'] != source['window']['image']):
        raise ComputerError('windows_open_dialog_document_invalid')


def validate(source):
    """Pure shape/role validation; no UI, filesystem or approval side effects."""
    try:
        return _validate(source)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        if isinstance(exc, ComputerError):
            raise
        raise ComputerError('windows_open_dialog_source_invalid') from exc


def snapshot(window, check):
    """Observe only the current dialog; no activation, OCR, typing or invocation."""
    parent, dialog, _ = _guard(window, check)
    controls, _ = _controls(dialog, check)
    current, _ = _controls(dialog, check)
    if current != controls or _guard(window, check, expected=dialog) != (parent, dialog, 'visible'):
        raise ComputerError('screen_changed_reobserve')
    return validate({'window': parent, 'application': 'wps', 'kind': 'open_dialog',
                     'binding': {'scope': 'dialog_controls', 'dialog': dialog}, 'controls': controls})


def _request(window, source, role, text):
    validate(source)
    if (window != source['window'] or type(role) is not str
            or role not in {'filename', 'cancel'} or type(text) is not str):
        raise ComputerError('windows_open_dialog_request_invalid')
    if role == 'filename':
        # No command, URL, shell syntax, network path or unverified Office package.
        return files.snapshot_file(text)
    if text != '':
        raise ComputerError('windows_open_dialog_request_invalid')
    return None


def _same_control(current, expected):
    # A filename step intentionally changes the value, while native identity and
    # writable semantics must remain exact for a following Cancel step.
    return {k: v for k, v in current.items() if k != 'value'} == {k: v for k, v in expected.items() if k != 'value'}


def _current(window, source, check, *, foreground=False):
    dialog = source['binding']['dialog']
    _, _, presence = _guard(window, check, expected=dialog, allow_closed=True, foreground=foreground)
    document = (_document(window, check, expected_dialog=dialog, allow_closed=True)
                if 'document' in source['binding'] else None)
    if 'document' in source['binding'] and document != source['binding']['document']:
        raise ComputerError('windows_open_dialog_document_changed')
    if presence != 'visible':
        return presence, document, None, None
    controls, wrappers = _controls(dialog, check)
    for now, before in zip(controls, source['controls']):
        if (now['role'] != before['role'] or now['label'] != before['label']
                or not _same_control(now['control'], before['control'])
                or (now['parent'] is None) != (before['parent'] is None)
                or (now['parent'] is not None and not _same_control(now['parent'], before['parent']))):
            raise ComputerError('windows_open_dialog_control_changed')
    if (_guard(window, check, expected=dialog) != (window, dialog, 'visible')
            or ('document' in source['binding']
                and _document(window, check, expected_dialog=dialog) != document)):
        raise ComputerError('windows_open_dialog_changed')
    return presence, document, controls, wrappers


def _prepare(window, source, check):
    """Raise only the approved, still-identical owned dialog before dispatch."""
    import win32gui
    dialog = source['binding']['dialog']
    # Full applicable binding/control/owner proof precedes any activation.
    # Snapshot and observe never raise a window; only an approved action may.
    presence, _, _, _ = _current(window, source, check)
    if presence != 'visible':
        raise ComputerError('windows_open_dialog_missing')
    _guard(window, check, expected=dialog)
    if win32gui.GetForegroundWindow() != dialog['hwnd']:
        check()
        win32gui.SetForegroundWindow(dialog['hwnd'])
        until = time.monotonic() + 1.5
        while win32gui.GetForegroundWindow() != dialog['hwnd']:
            check()
            _guard(window, check, expected=dialog)
            if time.monotonic() >= until:
                raise ComputerError('windows_open_dialog_foreground_required')
            time.sleep(.03)
    _guard(window, check, expected=dialog, foreground=True)


def observe(window, source, role, text, check):
    """Read back the exact approved scope; passive scope claims no document."""
    target = _request(window, source, role, text)
    presence, document, controls, _ = _current(window, source, check)
    control = parent = value = None
    verified = role == 'cancel' and presence == 'destroyed'
    if controls is not None:
        chosen = next(item for item in controls if item['role'] == role)
        control, parent = chosen['control'], chosen['parent']
        value = controls[0]['control']['value']
        verified = role == 'filename' and value == text
    if target is not None and files.snapshot_file(text) != target:
        raise ComputerError('windows_file_changed')
    if 'document' in source['binding']:
        return {'verified': verified, 'facts': {'dialog_presence': presence, 'document': document,
                'control': control, 'parent': parent, 'field_value': value}}
    return {'verified': verified, 'facts': {'dialog_presence': presence,
            'binding_scope': 'dialog_controls', 'document_state': 'not_claimed',
            'control': control, 'parent': parent, 'field_value': value}}


def act(window, source, role, text, check, epoch):
    """One exact ValuePattern/Cancel dispatch, after the caller persists intent."""
    target = _request(window, source, role, text)
    _prepare(window, source, check)
    presence, _, controls, wrappers = _current(window, source, check, foreground=True)
    if presence != 'visible':
        raise ComputerError('windows_open_dialog_missing')
    chosen = next(item for item in controls if item['role'] == role)
    wrapper = wrappers[chosen['control']['id']]
    _guard(window, check, expected=source['binding']['dialog'], foreground=True)
    if target is not None and files.snapshot_file(text) != target:
        raise ComputerError('windows_file_changed')
    check()
    if 'scope' in source['binding']:
        # Read the exact wrapper again at dispatch, rather than relying only on
        # its dictionary key in the earlier traversal. Ancestors/owner were
        # independently verified by _current and the native dialog guard.
        record = access._native_record(wrapper, source['binding']['dialog']['pid'])
        keys = _CONTROL_KEYS - {'owner_hwnd', 'ancestors'}
        if record is None or any(record.get(k) != chosen['control'][k] for k in keys):
            raise ComputerError('windows_open_dialog_control_changed')
    if role == 'filename':
        if not access._direct_child(wrapper, chosen['parent']):
            raise ComputerError('windows_open_dialog_parent_unverified')
        pattern = wrapper.iface_value
        if pattern.CurrentIsReadOnly or pattern.CurrentValue != chosen['control']['value']:
            raise ComputerError('windows_open_dialog_value_unverified')
        if 'scope' in source['binding']:
            if pattern.CurrentValue == text:
                # An already satisfied passive field never repeats SetValue.
                return
            if pattern.CurrentValue != source['controls'][0]['control']['value']:
                raise ComputerError('windows_open_dialog_draft_changed')
        check()
        pattern.SetValue(text)
        check()
        if pattern.CurrentValue != text:
            raise ComputerError('windows_open_dialog_value_unverified')
    else:
        wrapper.iface_invoke.Invoke()
        # WPS can return before destroying its original HWND. This is a bounded
        # wait, never another Invoke; observation independently attests bytes.
        until = time.monotonic() + 1.5
        while True:
            check()
            _, _, presence = _guard(window, check, expected=source['binding']['dialog'], allow_closed=True)
            if presence == 'destroyed':
                break
            if time.monotonic() >= until:
                raise ComputerError('windows_open_dialog_cancel_unproven')
            time.sleep(.03)
