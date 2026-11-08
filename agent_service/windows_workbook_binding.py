"""Read-only attestation of existing XLSX workbooks in one selected WPS root.

No Dispatch, activation, open, save or close calls. ROT absence is never used
as closure evidence: closure requires a surviving, exactly bound workbook and
two complete stable Workbooks metadata enumerations.
"""
import os
from pathlib import Path

from .computer import ComputerError, same_identity
from . import windows_file_dialog as files


class _ExistingObject:
    """Expose only bounded PROPERTYGETs on an existing IDispatch pointer."""
    _names = {'Application', 'FullName', 'ActiveWorkbook', 'ActiveWindow', 'Hwnd',
              'Windows', 'Workbooks', 'Count', 'ReadOnly', 'Saved', 'Visible'}

    def __init__(self, pointer):
        self.pointer = pointer

    def __getattr__(self, name):
        if name not in self._names:
            raise AttributeError(name)
        member = self.pointer.GetIDsOfNames(0, name)
        value = self.pointer.Invoke(member, 0, 2, 1)
        return _ExistingObject(value) if hasattr(value, 'Invoke') else value

    def item(self, index):
        if type(index) is not int or not 1 <= index <= 64:
            raise ComputerError('windows_workbook_collection_invalid')
        member = self.pointer.GetIDsOfNames(0, 'Item')
        value = self.pointer.Invoke(member, 0, 2, 1, index)
        if not hasattr(value, 'Invoke'):
            raise ComputerError('windows_workbook_collection_invalid')
        return _ExistingObject(value)


def _path(value):
    path = files._local_path(value)
    if path.suffix.casefold() != '.xlsx':
        raise ComputerError('windows_file_path_invalid')
    return path


def _rot(check):
    import pythoncom
    check()
    try:
        table = pythoncom.GetRunningObjectTable()
        iterator = iter(table.EnumRunning())
        context = pythoncom.CreateBindCtx(0)
    except Exception as exc:
        raise ComputerError('windows_workbook_rot_unavailable') from exc
    return pythoncom, table, iterator, context


def _monikers(iterator, context, check):
    try:
        for count, moniker in enumerate(iterator, 1):
            check()
            if count > 128:
                raise ComputerError('windows_workbook_rot_scope_exceeded')
            yield moniker, moniker.GetDisplayName(context, None)
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_workbook_rot_unreadable') from exc


def _acquire(pythoncom, table, moniker, check):
    check()
    try:
        pointer = table.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
    except Exception as exc:
        raise ComputerError('windows_workbook_rot_unreadable') from exc
    check()
    return _ExistingObject(pointer)


def _exact_rot_workbook(approved, check):
    pythoncom, table, iterator, context = _rot(check)
    matches = [moniker for moniker, name in _monikers(iterator, context, check)
               if type(name) is str and Path(name).is_absolute()
               and os.path.normcase(os.path.normpath(name)) == os.path.normcase(str(approved))]
    if len(matches) > 1:
        raise ComputerError('windows_workbook_rot_ambiguous')
    return _acquire(pythoncom, table, matches[0], check) if matches else None


def _handles(book, app, window, *, visible=True):
    try:
        windows = book.Windows
        count = windows.Count
        if type(count) is not int or not 1 <= count <= 64:
            raise ComputerError('windows_workbook_collection_invalid')
        book_hwnd = windows.item(1).Hwnd
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_workbook_hwnd_unsupported') from exc
    # WPS versions differ: some expose Application.Hwnd while ActiveWindow has
    # no Hwnd. A property that is present must still bind, even if another does.
    app_handles = []
    active_handle = None
    for index, read in enumerate((lambda: app.Hwnd, lambda: app.ActiveWindow.Hwnd)):
        try:
            handle = read()
        except Exception:
            continue
        app_handles.append(handle)
        if index == 1:
            active_handle = handle
    if not app_handles:
        raise ComputerError('windows_workbook_hwnd_unsupported')
    handles = (book_hwnd, *app_handles)
    for handle in handles:
        files._bound_hwnd(handle, window)
    # Component ActiveWorkbook/Visible may remain stale on a hidden WPS tab.
    # Require its native non-root editor window to be visible. Application.Hwnd
    # can be an invisible container, so only its root/process binding is used.
    if visible:
        files._visible_bound_hwnd(book_hwnd, window)
        if active_handle is not None:
            files._visible_bound_hwnd(active_handle, window)
    return handles


def visibility_diagnostic(window, path, check):
    """Read bounded visibility facts twice; this is not active-tab authority.

    WPS component ActiveWorkbook and a shared root HWND may survive switching
    to a Word tab. Visible=True on that shared root is therefore inconclusive.
    No title, focus change, application activation or document body is read.
    """
    from . import computer_uia as uia
    import win32gui
    approved = _path(path)
    check()
    fresh = uia._recheck(window)
    book = _exact_rot_workbook(approved, check)
    if book is None:
        check()
        uia._recheck(window)
        return {'registered': False, 'stable': None}

    def read_property(obj, name):
        check()
        try:
            return getattr(obj, name)
        except Exception:
            return None

    def handle_facts(handle):
        result = {'hwnd': handle if type(handle) is int else None,
                  'bound': False, 'child': False, 'native_visible': None}
        check()
        if type(handle) is not int or handle <= 0:
            return result
        try:
            files._bound_hwnd(handle, fresh)
        except ComputerError:
            return result
        result['bound'] = True
        result['child'] = handle != fresh['hwnd']
        check()
        try:
            visible = win32gui.IsWindowVisible(handle)
        except Exception:
            return result
        if type(visible) in (bool, int) and visible in (0, 1):
            result['native_visible'] = bool(visible)
        return result

    def window_facts(obj):
        result = handle_facts(read_property(obj, 'Hwnd'))
        visible = read_property(obj, 'Visible')
        result['com_visible'] = visible if type(visible) is bool else None
        return result

    def read():
        check()
        try:
            app = book.Application
            name, active_name = book.FullName, app.ActiveWorkbook.FullName
            windows = book.Windows
            count = windows.Count
            if type(count) is not int or not 1 <= count <= 64:
                raise ComputerError('windows_workbook_collection_invalid')
            book_window = windows.item(1)
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError('windows_workbook_com_unreadable') from exc
        check()
        active_window = read_property(app, 'ActiveWindow')
        return {'book_path_matches': _path(name) == approved,
                'active_path_matches': _path(active_name) == approved,
                'workbook_window': window_facts(book_window),
                'application_window': window_facts(active_window),
                'application_hwnd': handle_facts(read_property(app, 'Hwnd'))}

    first, second = read(), read()
    check()
    if not same_identity(uia._recheck(window), fresh):
        raise ComputerError('windows_document_identity_mismatch')
    return {'registered': True, 'stable': first == second, 'first': first, 'second': second}


def _facts(book, window, path, check, *, visible=True):
    check()
    try:
        app = book.Application
        names = book.FullName, app.ActiveWorkbook.FullName
        flags = book.ReadOnly, book.Saved
    except Exception as exc:
        raise ComputerError('windows_workbook_com_unreadable') from exc
    if any(_path(name) != path for name in names):
        raise ComputerError('windows_workbook_changed')
    if any(type(flag) is not bool for flag in flags):
        raise ComputerError('windows_workbook_flag_invalid')
    handles = _handles(book, app, window, visible=visible)
    check()
    return names, handles, flags


def _changed(first, second):
    error = ComputerError('windows_workbook_state_changed')
    error.document_diagnostic = {'name_changed': first[0] != second[0],
                                 'hwnd_changed': first[1] != second[1],
                                 'flags_changed': first[2] != second[2]}
    return error


def _stable_workbook(book, window, path, check, *, visible):
    from . import computer_uia as uia
    approved = _path(path)
    check()
    fresh = uia._recheck(window)
    first = _facts(book, fresh, approved, check, visible=visible)
    try:
        second = _facts(book, fresh, approved, check, visible=visible)
    except ComputerError as exc:
        if str(exc) in {'windows_workbook_changed', 'windows_document_identity_mismatch',
                        'windows_document_not_visible'}:
            error = ComputerError('windows_workbook_state_changed')
            error.document_diagnostic = {'identity_changed': True}
            raise error from exc
        raise
    check()
    if not same_identity(uia._recheck(window), fresh):
        raise ComputerError('windows_document_identity_mismatch')
    if first != second:
        raise _changed(first, second)
    return first


def same_workbook(book, window, path, check):
    """Return stable visible (names, handles, flags), matching Word copy shape."""
    return _stable_workbook(book, window, path, check, visible=True)


def _attested_workbook(window, path, check):
    from . import computer_uia as uia
    check()
    uia._recheck(window)
    approved = _path(path)
    book = _exact_rot_workbook(approved, check)
    inactive = {'active': False, 'read_only': None, 'saved': None}
    if book is None:
        check()
        uia._recheck(window)
        return None, inactive
    try:
        facts = same_workbook(book, window, approved, check)
    except ComputerError as exc:
        if str(exc) in {'windows_workbook_changed', 'windows_document_identity_mismatch',
                        'windows_document_not_visible'}:
            check()
            uia._recheck(window)
            return None, inactive
        raise
    return book, {'active': True, 'read_only': facts[2][0], 'saved': facts[2][1]}


def workbook_status(window, path, check):
    return _attested_workbook(window, path, check)[1]


def bound_workbook(window, path, check):
    book, status = _attested_workbook(window, path, check)
    if not status['active']:
        raise ComputerError('windows_workbook_not_active')
    return book, status


def _workbook_application(window, check):
    """Bind a running XLSX component for metadata, without requiring its tab."""
    from . import computer_uia as uia
    check()
    uia._recheck(window)
    pythoncom, table, iterator, context = _rot(check)
    for moniker, name in _monikers(iterator, context, check):
        if type(name) is not str or not Path(name).is_absolute() or Path(name).suffix.casefold() != '.xlsx':
            continue
        try:
            approved = _path(name)
            book = _acquire(pythoncom, table, moniker, check)
            _stable_workbook(book, window, approved, check, visible=False)
            app = book.Application
        except ComputerError as exc:
            # Skip unsupported/disconnected candidates, never a guard failure.
            if str(exc) in {'windows_file_path_invalid', 'windows_workbook_rot_unreadable',
                            'windows_workbook_com_unreadable', 'windows_workbook_changed',
                            'windows_workbook_state_changed', 'windows_workbook_flag_invalid',
                            'windows_workbook_hwnd_unsupported', 'windows_workbook_collection_invalid',
                            'windows_document_identity_mismatch', 'windows_document_not_visible',
                            'windows_document_visibility_unavailable'}:
                continue
            raise
        except Exception:
            continue
        check()
        uia._recheck(window)
        return book, app, approved
    raise ComputerError('windows_workbook_anchor_unavailable')


def workbook_present(path, check, *, window):
    """Prove presence/absence from two complete stable bound Workbooks lists."""
    from . import computer_uia as uia
    approved = _path(path)
    check()
    uia._recheck(window)
    anchor, app, anchor_path = _workbook_application(window, check)
    acquired = [anchor, app]

    def read():
        check()
        before = _facts(anchor, window, anchor_path, check, visible=False)
        try:
            _handles(anchor, app, window, visible=False)
            if _path(app.ActiveWorkbook.FullName) != anchor_path:
                raise ComputerError('windows_workbook_state_changed')
            collection = app.Workbooks
            acquired.append(collection)
            count = collection.Count
            if type(count) is not int or not 1 <= count <= 64:
                raise ComputerError('windows_workbook_collection_invalid')
            names = []
            for index in range(1, count + 1):
                check()
                book = collection.item(index)
                acquired.append(book)
                name = book.FullName
                if (type(name) is not str or not 0 < len(name) <= 3000
                        or any(ord(char) < 32 for char in name)
                        or name.startswith(('\\\\?\\', '\\\\.\\'))):
                    raise ComputerError('windows_workbook_collection_invalid')
                names.append(os.path.normcase(os.path.normpath(name)))
            after_count = collection.Count
            if type(after_count) is not int:
                raise ComputerError('windows_workbook_collection_invalid')
            if after_count != count:
                raise ComputerError('windows_workbook_state_changed')
            _handles(anchor, app, window, visible=False)
            if _path(app.ActiveWorkbook.FullName) != anchor_path:
                raise ComputerError('windows_workbook_state_changed')
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError('windows_workbook_collection_unavailable') from exc
        after = _facts(anchor, window, anchor_path, check, visible=False)
        if before != after:
            raise _changed(before, after)
        check()
        uia._recheck(window)
        return tuple(names), after

    first, second = read(), read()
    check()
    uia._recheck(window)
    target = os.path.normcase(str(approved))
    if first != second:
        error = ComputerError('windows_workbook_state_changed')
        error.document_diagnostic = {'collection_changed': first[0] != second[0],
                                     'first_present': target in first[0],
                                     'second_present': target in second[0]}
        raise error
    return target in second[0]
