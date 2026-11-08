"""Native facts and single-dispatch primitives for reviewed WPS Find controls.

This module grants no generic Button or Edit permission. The pure form contract
must validate OCR/role/geometry and the approved source before these primitives.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import struct
import time

from .computer import ComputerError, safe_root
from . import computer_uia as uia, windows_access as access
from . import windows_visual as visual, windows_visual_binding as binding
from . import windows_file_dialog as files

FIND_TITLES = {'查找', '查找和替换', 'Find', 'Find and Replace'}


def _guard(window, check, *, expected=None, allow_closed=False, foreground=False, max_dialog_height=260):
    import win32gui
    import win32process
    if max_dialog_height not in (260, 720):
        raise ComputerError('windows_visual_form_frame_limit')
    check()
    parent = uia._recheck(window)
    if access._kind(parent) != 'wps' or parent != window:
        raise ComputerError('windows_visual_form_window_changed')
    dialogs = []
    def visit(hwnd, _):
        if hwnd == parent['hwnd'] or not win32gui.IsWindowVisible(hwnd) or not access._owned(hwnd, parent['hwnd']):
            return
        cls = win32gui.GetClassName(hwnd)
        if cls.casefold() == 'tooltips_class32':
            return
        if (cls == 'KLiteMainWindowShadowBorder' and not win32gui.GetWindowText(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == parent['pid']
                and win32gui.GetWindowLong(hwnd, -20) & 0x80080 == 0x80080):
            return
        owner = win32gui.GetWindow(hwnd, 4)  # GW_OWNER; WPS may own through its editor child.
        owner_pid = win32process.GetWindowThreadProcessId(owner)[1] if owner else None
        owner_root = win32gui.GetAncestor(owner, 2) if owner else None
        candidate = uia._identity(hwnd)
        predicates = {
            'find_title': candidate['title'] in FIND_TITLES,
            'process_binding': access._process_owned(candidate, parent),
            'owner_binding': (owner_pid == parent['pid'] and (owner == parent['hwnd']
                or (bool(win32gui.IsChild(parent['hwnd'], owner)) and owner_root == parent['hwnd']))),
        }
        failed = next((key for key, valid in predicates.items() if not valid), None)
        if failed:
            error = ComputerError('windows_visual_form_popup_requires_attention')
            error.content_diagnostic = {'predicate': failed, 'hwnd': hwnd,
                'class_name': cls, 'title': candidate['title'], 'pid': candidate['pid'],
                'owner_hwnd': owner, 'owner_class': win32gui.GetClassName(owner) if owner else '',
                'owner_pid': owner_pid, 'owner_root_hwnd': owner_root}
            raise error
        dialogs.append(candidate)
    win32gui.EnumWindows(visit, None)
    if len(dialogs) > 1:
        raise ComputerError('windows_visual_form_dialog_ambiguous')
    if not dialogs:
        if not allow_closed or expected is None:
            raise ComputerError('windows_visual_form_dialog_missing')
        # A hidden original dialog is accepted only with its original raw native
        # identity intact. The caller separately verifies document preservation.
        hwnd = expected['hwnd']
        exists = bool(win32gui.IsWindow(hwnd))
        if exists and (win32gui.IsWindowVisible(hwnd)
                or win32process.GetWindowThreadProcessId(hwnd)[1] != expected['pid']
                or win32gui.GetWindowText(hwnd) != expected['title']
                or win32gui.GetClassName(hwnd) != expected['class_name']
                or not access._owned(hwnd, parent['hwnd'])):
            raise ComputerError('windows_visual_form_dialog_changed')
        return parent, None, None
    dialog = dialogs[0]
    if expected is not None and dialog != expected:
        raise ComputerError('windows_visual_form_dialog_changed')
    if not win32gui.IsWindowEnabled(dialog['hwnd']):
        raise ComputerError('windows_visual_form_popup_requires_attention')
    if foreground and win32gui.GetForegroundWindow() != dialog['hwnd']:
        raise ComputerError('windows_visual_form_foreground_required')
    rect = list(win32gui.GetWindowRect(dialog['hwnd']))
    if not 200 <= rect[2]-rect[0] <= 1600 or not 60 <= rect[3]-rect[1] <= max_dialog_height:
        raise ComputerError('windows_visual_form_frame_limit')
    check()
    return parent, dialog, rect


def _password_guard(dialog, check):
    """Check protected/foreign descendants before reading labels or pixels."""
    from pywinauto import Desktop
    from pywinauto.uia_defines import IUIA
    root = Desktop(backend='uia').window(handle=dialog['hwnd']).wrapper_object()
    walker = IUIA().iuia.ControlViewWalker
    count = 0
    def visit(element, depth):
        nonlocal count
        check()
        count += 1
        if count > 1600 or depth > 24:
            raise ComputerError('windows_visual_form_tree_limit')
        if element.CurrentIsPassword or element.CurrentProcessId != dialog['pid']:
            raise ComputerError('windows_visual_form_protected_control')
        child = walker.GetFirstChildElement(element)
        while child:
            visit(child, depth+1)
            child = walker.GetNextSiblingElement(child)
    visit(root.element_info.element, 0)


def prepare(window, check, expected=None):
    """Raise only an already proven unique owned Find dialog, without input."""
    import win32gui
    _, dialog, _ = _guard(window, check, expected=expected)
    if win32gui.GetForegroundWindow() != dialog['hwnd']:
        win32gui.SetForegroundWindow(dialog['hwnd'])
        until = time.monotonic()+1.5
        while win32gui.GetForegroundWindow() != dialog['hwnd']:
            check()
            if time.monotonic() > until:
                raise ComputerError('windows_visual_form_foreground_required')
            time.sleep(.03)
    _guard(window, check, expected=dialog, foreground=True)
    return dialog


def document(window, check, *, expected_dialog=None, allow_closed=False, max_dialog_height=260):
    """Attest the active saved document without weakening the global popup gate."""
    from . import windows_workbook_binding as books
    parent, dialog, _ = _guard(window, check, expected=expected_dialog, allow_closed=allow_closed, max_dialog_height=max_dialog_height)
    paths = set()
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
            continue
        if path.suffix.casefold() != '.'+kind:
            continue
        status = files.document_status(parent, path, check)
        if status.get('active') is True:
            if status.get('saved') is not True or type(status.get('read_only')) is not bool:
                raise ComputerError('windows_visual_document_unsaved')
            paths.add((str(path), status['read_only']))
    if len(paths) != 1:
        raise ComputerError('windows_visual_document_identity_unavailable')
    path, readonly = next(iter(paths))
    facts = binding._file(path)
    if (files.document_status(parent, path, check) != {'active': True, 'saved': True, 'read_only': readonly}
            or binding._file(path) != facts):
        raise ComputerError('windows_visual_document_changed')
    current, after, _ = _guard(window, check, expected=dialog or expected_dialog, allow_closed=allow_closed, max_dialog_height=max_dialog_height)
    if current != parent or after != dialog:
        raise ComputerError('windows_visual_form_dialog_changed')
    result = {'schema': 1, **facts, 'format': Path(path).suffix[1:].casefold(), 'application': 'wps',
              'registered_app': str(safe_root(parent['image'])),
              'read_only': readonly, 'saved': True}
    return binding.verify(result, require_native=False)


def _capture(window, dialog, check):
    """Full bounded dialog BMP using HWND rendering, never a desktop capture."""
    import ctypes
    from ctypes import wintypes as w
    import win32gui, win32ui
    with visual._physical_pixels():
        _, current, rect = _guard(window, check, expected=dialog, foreground=True)
        _password_guard(current, check)
        width, height = rect[2]-rect[0], rect[3]-rect[1]
        dc = win32gui.GetWindowDC(dialog['hwnd'])
        source = memory = bitmap = previous = None
        try:
            source = win32ui.CreateDCFromHandle(dc)
            memory = source.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            previous = memory.SelectObject(bitmap)
            api = ctypes.windll.user32.PrintWindow
            api.argtypes, api.restype = [w.HWND, w.HDC, w.UINT], w.BOOL
            if not api(dialog['hwnd'], memory.GetSafeHdc(), 2):
                raise ComputerError('windows_visual_capture_failed')
            raw = bitmap.GetBitmapBits(True)
            if bitmap.GetInfo()['bmBitsPixel'] != 32 or len(raw) != width*height*4:
                raise ComputerError('windows_visual_capture_failed')
            data = struct.pack('<2sIHHI', b'BM', 54+len(raw), 0, 0, 54)
            data += struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, len(raw), 0, 0, 0, 0)+raw
        finally:
            if memory is not None and previous is not None:
                memory.SelectObject(previous)
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if memory is not None:
                memory.DeleteDC()
            if source is not None:
                source.DeleteDC()
            win32gui.ReleaseDC(dialog['hwnd'], dc)
        if _guard(window, check, expected=dialog, foreground=True)[2] != rect:
            raise ComputerError('screen_changed_reobserve')
        return data, rect


def _controls(dialog, check):
    _password_guard(dialog, check)
    observation, wrappers = access._tree(dialog, check)
    if observation.get('truncated'):
        raise ComputerError('windows_visual_form_tree_limit')
    result = []
    for item in observation['controls']:
        if item['type'] not in {'Edit', 'ComboBox', 'Button', 'Text', 'TabItem'}:
            continue
        record = deepcopy(item)
        from .windows_controls import bounds
        record['bounds'] = bounds(wrappers[item['id']])
        record['owner_hwnd'] = dialog['hwnd']
        result.append(record)
    return result, wrappers


def snapshot(window, check):
    prepare(window, check)
    parent, dialog, _ = _guard(window, check, foreground=True)
    facts = document(window, check, expected_dialog=dialog)
    for _ in range(3):
        check()
        controls, _ = _controls(dialog, check)
        pixels, rect = _capture(window, dialog, check)
        frame = visual.recognize(pixels, None, check,profile='forms')
        latest, latest_rect = _capture(window, dialog, check)
        current_controls, _ = _controls(dialog, check)
        if latest != pixels or latest_rect != rect or controls != current_controls:
            continue
        if document(window, check, expected_dialog=dialog) != facts:
            raise ComputerError('windows_visual_document_changed')
        return {'window': parent, 'dialog': dialog, 'document': facts, 'frame': frame,
                'rect': rect, 'controls': controls}
    raise ComputerError('screen_changed_reobserve')


def _field(source, control_id, check):
    _guard(source['window'], check, expected=source['dialog'], foreground=True)
    controls, wrappers = _controls(source['dialog'], check)
    found = [item for item in controls if item['id'] == control_id]
    expected = [item for item in source['controls'] if item['id'] == control_id]
    if (len(found) != 1 or found != expected or found[0]['type'] not in {'Edit', 'ComboBox'}
            or not found[0]['enabled'] or not found[0]['visible'] or 'set_value' not in found[0]['patterns']):
        raise ComputerError('screen_changed_reobserve')
    pattern = wrappers[control_id].iface_value
    if pattern.CurrentIsReadOnly or pattern.CurrentValue != found[0]['value']:
        raise ComputerError('screen_changed_reobserve')
    return pattern


def read_field(source, control_id, check):
    return _field(source, control_id, check).CurrentValue


def prepare_close_focus(source, check):
    """Focus only the already reviewed footer Close; never invoke or type.

    Moving focus away from the editor stops its caret animation so the existing
    whole-frame Close proof can remain exact without masking any image pixels.
    """
    window, expected = source['window'], source['dialog']
    dialog=prepare(window,check,expected=expected)
    def footer(items):
        found=[c for c in items if c['type']=='Button' and c['name'] in {'关闭','Close'}
               and c['visible'] and c['enabled'] and c.get('owner_hwnd')==dialog['hwnd']
               and not any(p.get('type')=='TitleBar' for p in c.get('ancestors',[]))]
        if len(found)!=1:raise ComputerError('windows_visual_form_close_ambiguous')
        return found[0]
    def editor(items):
        combos=[c for c in items if c['type']=='ComboBox' and c['name'] in access.FIND_FIELDS]
        if len(combos)!=1:raise ComputerError('windows_visual_form_field_ambiguous')
        selector={k:combos[0][k] for k in ('type','name','automation_id')}
        edits=[c for c in items if c['type']=='Edit' and c['visible'] and c['enabled']
               and selector in c.get('ancestors',[])]
        if len(edits)!=1:raise ComputerError('windows_visual_form_field_ambiguous')
        return edits[0]
    wanted=footer(source['controls'])
    items,wrappers=_controls(dialog,check)
    current=footer(items); field=editor(items)
    keys=('id','runtime_id','type','name','automation_id','bounds','ancestors','owner_hwnd')
    if any(current.get(k)!=wanted.get(k) for k in keys) or 'focus' not in current.get('patterns',[]):
        raise ComputerError('windows_visual_form_close_changed')
    _guard(window,check,expected=dialog,foreground=True)
    if not current.get('focused'):
        check()
        wrappers[current['id']].element_info.element.SetFocus()
    until=time.monotonic()+1.5
    while True:
        check()
        _guard(window,check,expected=dialog,foreground=True)
        after,_=_controls(dialog,check)
        button,edit=footer(after),editor(after)
        if (any(button.get(k)!=wanted.get(k) for k in keys)
                or edit['id']!=field['id'] or edit['value']!=field['value']):
            raise ComputerError('windows_visual_form_close_changed')
        if button.get('focused') is True and edit.get('focused') is False:
            return
        if time.monotonic()>until:
            raise ComputerError('windows_visual_form_close_focus_unverified')
        time.sleep(.03)


def set_field(source, control_id, text, check):
    """One exact ValuePattern dispatch; caller owns reviewed field authorization."""
    if type(text) is not str or len(text) > 300 or any(ord(c) < 32 for c in text):
        raise ComputerError('windows_visual_form_text_invalid')
    pattern = _field(source, control_id, check)
    check()
    pattern.SetValue(text)
    check()
    _guard(source['window'], check, expected=source['dialog'], foreground=True)
    value = pattern.CurrentValue
    if value != text:
        raise ComputerError('windows_visual_form_value_unverified')
    return value


def click_close(source, point, check, epoch, *, should_stop=lambda: False):
    """One hook-guarded click after caller proves a unique Close OCR/UIA overlap."""
    import win32gui
    from .windows_pointer import guarded_click, _target
    if (type(point) not in (list, tuple) or len(point) != 2
            or any(type(v) is not int for v in point)):
        raise ComputerError('windows_visual_form_point_invalid')
    _, _, rect = _guard(source['window'], check, expected=source['dialog'], foreground=True)
    if rect != source['rect'] or not rect[0] <= point[0] < rect[2] or not rect[1] <= point[1] < rect[3]:
        raise ComputerError('screen_changed_reobserve')
    pixels, current_rect = _capture(source['window'], source['dialog'], check)
    if current_rect != source['rect'] or sha256(pixels).hexdigest() != source['frame']['sha256']:
        raise ComputerError('screen_changed_reobserve')
    def guarded():
        check()
        if _guard(source['window'], check, expected=source['dialog'], foreground=True)[2] != source['rect']:
            raise ComputerError('screen_changed_reobserve')
    def after_target(user, hwnd, x, y):
        if win32gui.IsWindow(hwnd):
            # Still-existing/hidden windows retain the original pointer rule.
            _target(user, hwnd, x, y)
            return
        # Own pointer input has changed the worker epoch. Do not use its stale
        # check here: the live input monitor still rejects takeover/cancellation,
        # and its returned epoch is installed before document postverification.
        parent, dialog, _ = _guard(source['window'], lambda: None,
                                    expected=source['dialog'], allow_closed=True)
        if dialog is not None or int(user.GetForegroundWindow() or 0) != parent['hwnd']:
            raise ComputerError('windows_visual_form_close_foreground_changed')
    epoch[0] = guarded_click(source['dialog']['hwnd'], *point, check=guarded,
                             should_stop=should_stop, after_target=after_target)
    # WPS may return from its input hook before destroying the dialog HWND.
    # Wait only for that original handle to disappear; never repeat the click
    # or hide a guard/document failure. The caller still independently proves
    # the preserved document and absence of other popups after this boundary.
    until=time.monotonic()+1.5
    while True:
        check()
        if should_stop():raise ComputerError('windows_input_cancelled')
        if not win32gui.IsWindow(source['dialog']['hwnd']):return
        if time.monotonic()>=until:raise ComputerError('windows_form_close_unproven')
        time.sleep(.03)


def closed(source, check, *, max_dialog_height=260):
    """Original dialog state plus unchanged active document, no action.

    Hidden is reported separately and is never a proven Close completion.
    """
    import win32gui
    _, dialog, _ = _guard(source['window'], check, expected=source['dialog'], allow_closed=True, max_dialog_height=max_dialog_height)
    presence = 'visible' if dialog else 'hidden' if win32gui.IsWindow(source['dialog']['hwnd']) else 'destroyed'
    facts = document(source['window'], check, expected_dialog=source['dialog'], allow_closed=True, max_dialog_height=max_dialog_height)
    if facts != source['document']:
        raise ComputerError('windows_visual_document_changed')
    _, after, _ = _guard(source['window'], check, expected=source['dialog'], allow_closed=True, max_dialog_height=max_dialog_height)
    latest = 'visible' if after else 'hidden' if win32gui.IsWindow(source['dialog']['hwnd']) else 'destroyed'
    if latest != presence:
        raise ComputerError('screen_changed_reobserve')
    return {'dialog_open': dialog is not None, 'document': facts, 'dialog_presence': presence}


def observe(window, source_snapshot, kind, text, check):
    """Read the current form or prove the original dialog's native destruction."""
    if kind not in {'search_text', 'close_find'} or window != source_snapshot['window']:
        raise ComputerError('windows_visual_form_request_invalid')
    state = closed(source_snapshot, check)
    if state['dialog_presence'] != 'visible':
        if kind != 'close_find':
            raise ComputerError('windows_visual_form_dialog_missing')
        return {'snapshot': None, **state, 'field_value': None}
    # Both approved form operations use native ValuePattern/readback for text;
    # neither needs the editor focused. Stabilize its caret only through the
    # same independently proven footer control, never during initial discovery.
    prepare_close_focus(source_snapshot,check)
    current = snapshot(window, check)
    if current['dialog'] != source_snapshot['dialog'] or current['document'] != source_snapshot['document']:
        raise ComputerError('windows_visual_form_dialog_changed')
    # UIA's actual writable editor is the child of the observed Find ComboBox.
    combos = [c for c in current['controls'] if c['type'] == 'ComboBox' and c['name'] in access.FIND_FIELDS]
    fields = []
    if len(combos) == 1:
        selector = {key: combos[0][key] for key in ('type', 'name', 'automation_id')}
        fields = [c for c in current['controls'] if c['type'] == 'Edit' and c['visible'] and c['enabled']
                  and 'set_value' in c['patterns'] and selector in c.get('ancestors', [])]
    if len(fields) != 1:
        raise ComputerError('windows_visual_form_field_ambiguous')
    value = read_field(current, fields[0]['id'], check)
    return {'snapshot': current, **state, 'field_value': value}
