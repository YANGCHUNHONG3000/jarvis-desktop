"""Offline checks of exact existing-workbook identity and closure evidence."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from agent_service.computer import ComputerError
from agent_service import computer_uia as uia
from agent_service import windows_workbook_binding as wb


class Collection:
    def __init__(self, items):
        self.items = items
        self.calls = []

    @property
    def Count(self):
        return len(self.items)

    def item(self, index):
        self.calls.append(index)
        return self.items[index - 1]


@pytest.fixture
def active(tmp_path, monkeypatch):
    path = tmp_path / 'existing.xlsx'
    path.write_bytes(b'identity fixture')
    window = dict(hwnd=100, pid=123, created='2026', image='et.exe',
                  class_name='XLMAIN', profile='observe_only')
    book = NS(FullName=str(path), ReadOnly=False, Saved=True,
              Windows=Collection([NS(Hwnd=101)]))
    app = NS(Hwnd=100, ActiveWindow=NS(Hwnd=101), ActiveWorkbook=book,
             Workbooks=Collection([book]))
    book.Application = app
    monkeypatch.setattr(uia, '_recheck', lambda win: dict(window))
    monkeypatch.setattr(uia, '_identity', lambda hwnd: dict(window))
    monkeypatch.setitem(sys.modules, 'win32gui', NS(
        GetAncestor=lambda hwnd, kind: 100 if hwnd in (100, 101, 102) else hwnd,
        IsWindowVisible=lambda hwnd: hwnd in (101, 102)))
    def visible_bound(handle, selected):
        wb.files._bound_hwnd(handle, selected)
        if handle == selected['hwnd']:
            raise ComputerError('windows_document_visibility_unavailable')
        if not sys.modules['win32gui'].IsWindowVisible(handle):
            raise ComputerError('windows_document_not_visible')
    monkeypatch.setattr(wb.files, '_visible_bound_hwnd', visible_bound, raising=False)
    monkeypatch.setattr(wb, '_exact_rot_workbook', lambda approved, check: book)
    monkeypatch.setattr(wb, '_workbook_application', lambda win, check: (book, app, path))
    return window, path, book, app


@pytest.mark.parametrize('read_only,saved', [(False, False), (False, True), (True, False), (True, True)])
def test_bound_object_and_flags_are_from_same_double_attestation(active, read_only, saved):
    window, path, book, _ = active
    book.ReadOnly, book.Saved = read_only, saved
    obj, status = wb.bound_workbook(window, path, lambda: None)
    assert obj is book
    assert status == {'active': True, 'read_only': read_only, 'saved': saved}
    names, handles, flags = wb.same_workbook(book, window, path, lambda: None)
    assert names == (str(path), str(path))
    assert handles == (101, 100, 101)
    assert flags == (read_only, saved)


@pytest.mark.parametrize('member', ['ReadOnly', 'Saved'])
@pytest.mark.parametrize('value', [0, 1, None, 'True'])
def test_flags_require_real_boolean(active, member, value):
    window, path, book, _ = active
    setattr(book, member, value)
    with pytest.raises(ComputerError, match='^windows_workbook_flag_invalid$'):
        wb.workbook_status(window, path, lambda: None)


def test_same_basename_in_other_directory_is_inactive(active):
    window, path, _, app = active
    other = path.parent / 'other' / path.name
    other.parent.mkdir()
    other.write_bytes(b'other')
    app.ActiveWorkbook = NS(FullName=str(other))
    assert wb.workbook_status(window, path, lambda: None)['active'] is False
    with pytest.raises(ComputerError, match='^windows_workbook_not_active$'):
        wb.bound_workbook(window, path, lambda: None)


@pytest.mark.parametrize('which', ['book', 'app', 'active_window'])
def test_every_available_handle_must_bind_exact_selected_root(active, which):
    window, path, book, app = active
    if which == 'book':
        book.Windows.items[0].Hwnd = 999
    elif which == 'app':
        app.Hwnd = 999
    else:
        app.ActiveWindow.Hwnd = 999
    assert wb.workbook_status(window, path, lambda: None)['active'] is False


def test_reused_process_identity_cannot_bind(active, monkeypatch):
    window, path, _, _ = active
    monkeypatch.setattr(uia, '_identity', lambda hwnd: {**window, 'created': 'reused'})
    assert wb.workbook_status(window, path, lambda: None)['active'] is False


@pytest.mark.parametrize('missing', ['application', 'active_window'])
def test_supported_application_hwnd_variants(active, missing):
    window, path, _, app = active
    if missing == 'application':
        del app.Hwnd
    else:
        del app.ActiveWindow.Hwnd
    assert wb.workbook_status(window, path, lambda: None)['active'] is True


def test_no_hwnd_capability_fails_explicitly(active):
    window, path, _, app = active
    del app.Hwnd
    del app.ActiveWindow.Hwnd
    with pytest.raises(ComputerError, match='^windows_workbook_hwnd_unsupported$'):
        wb.workbook_status(window, path, lambda: None)


def test_book_window_hwnd_is_required_even_if_app_matches(active):
    window, path, book, _ = active
    del book.Windows.items[0].Hwnd
    with pytest.raises(ComputerError, match='^windows_workbook_hwnd_unsupported$'):
        wb.workbook_status(window, path, lambda: None)


@pytest.mark.parametrize('member', ['ReadOnly', 'Saved'])
def test_flags_change_between_reads_is_not_stable(active, monkeypatch, member):
    window, path, book, _ = active
    class Racing:
        reads = 0

        def __getattr__(self, name):
            if name == member:
                self.reads += 1
                return self.reads == 1
            return getattr(book, name)
    monkeypatch.setattr(wb, '_exact_rot_workbook', lambda approved, check: Racing())
    with pytest.raises(ComputerError, match='^windows_workbook_state_changed$') as raised:
        wb.workbook_status(window, path, lambda: None)
    assert raised.value.document_diagnostic['flags_changed']


def test_active_workbook_changes_between_reads(active):
    window, path, book, _ = active
    class RacingApp:
        Hwnd = 100
        ActiveWindow = NS(Hwnd=101)
        reads = 0

        @property
        def ActiveWorkbook(self):
            self.reads += 1
            return book if self.reads == 1 else NS(FullName=str(path.parent / 'other.xlsx'))
    (path.parent / 'other.xlsx').write_bytes(b'other')
    book.Application = RacingApp()
    with pytest.raises(ComputerError, match='^windows_workbook_state_changed$'):
        wb.workbook_status(window, path, lambda: None)


def test_rot_absence_is_only_inactive_and_never_used_for_closure(active, monkeypatch):
    window, path, _, _ = active
    monkeypatch.setattr(wb, '_exact_rot_workbook', lambda approved, check: None)
    assert wb.workbook_status(window, path, lambda: None) == {'active': False, 'read_only': None, 'saved': None}
    assert wb.workbook_present(path, lambda: None, window=window) is True


@pytest.mark.parametrize('present', [True, False])
def test_presence_requires_two_complete_lists_in_same_bound_application(active, present):
    window, path, _, app = active
    app.Workbooks = Collection([NS(FullName=str(path) if present else 'Unsaved Book1'),
                                NS(FullName=str(path.parent / 'nonexistent.xlsx'))])
    assert wb.workbook_present(path, lambda: None, window=window) is present
    assert app.Workbooks.calls == [1, 2, 1, 2]


@pytest.mark.parametrize('count', [True, False, None, 0, -1, 65, 1.0, '1'])
def test_invalid_count_cannot_prove_closed(active, count):
    window, path, _, app = active
    app.Workbooks = NS(Count=count)
    with pytest.raises(ComputerError, match='^windows_workbook_collection_invalid$'):
        wb.workbook_present(path, lambda: None, window=window)


@pytest.mark.parametrize('after,code', [(True, 'collection_invalid'), (2, 'state_changed')])
def test_count_is_rechecked_after_full_enumeration(active, after, code):
    window, path, _, app = active
    class Racing:
        reads = 0

        @property
        def Count(self):
            self.reads += 1
            return 1 if self.reads == 1 else after

        def item(self, index):
            return NS(FullName='Unsaved Book1')
    app.Workbooks = Racing()
    with pytest.raises(ComputerError, match='^windows_workbook_' + code + '$'):
        wb.workbook_present(path, lambda: None, window=window)


def test_changed_lists_cannot_prove_closed(active):
    window, path, _, app = active
    class Racing:
        Count = 1
        calls = 0

        def item(self, index):
            self.calls += 1
            return NS(FullName=str(path) if self.calls == 1 else 'Unsaved Book1')
    app.Workbooks = Racing()
    with pytest.raises(ComputerError, match='^windows_workbook_state_changed$') as raised:
        wb.workbook_present(path, lambda: None, window=window)
    assert raised.value.document_diagnostic == {'collection_changed': True, 'first_present': True, 'second_present': False}


@pytest.mark.parametrize('name', ['', None, 'a\nb', '\\\\?\\C:\\book.xlsx'])
def test_malformed_metadata_cannot_prove_closed(active, name):
    window, path, _, app = active
    app.Workbooks = Collection([NS(FullName=name)])
    with pytest.raises(ComputerError, match='^windows_workbook_collection_invalid$'):
        wb.workbook_present(path, lambda: None, window=window)


def test_wrong_root_does_not_read_collection(active):
    window, path, book, app = active
    book.Windows.items[0].Hwnd = 999
    with pytest.raises(ComputerError, match='^windows_document_identity_mismatch$'):
        wb.workbook_present(path, lambda: None, window=window)
    assert app.Workbooks.calls == []


def test_missing_anchor_never_means_closed(active, monkeypatch):
    window, path, _, _ = active
    def absent(*args):
        raise ComputerError('windows_workbook_anchor_unavailable')
    monkeypatch.setattr(wb, '_workbook_application', absent)
    with pytest.raises(ComputerError, match='^windows_workbook_anchor_unavailable$'):
        wb.workbook_present(path, lambda: None, window=window)


def test_other_application_cannot_supply_collection_under_good_anchor(active, monkeypatch):
    window, path, book, app = active
    other = NS(Hwnd=999, ActiveWindow=NS(Hwnd=999), ActiveWorkbook=book,
               Workbooks=Collection([NS(FullName='Unsaved Book1')]))
    monkeypatch.setattr(wb, '_workbook_application', lambda win, check: (book, other, path))
    with pytest.raises(ComputerError, match='^windows_document_identity_mismatch$'):
        wb.workbook_present(path, lambda: None, window=window)
    assert other.Workbooks.calls == []


@pytest.mark.parametrize('valid', [True, False])
def test_production_anchor_requires_exact_fullname_active_book_and_root(active, monkeypatch, valid):
    window, path, book, app = active
    records = [(object(), str(path))]
    monkeypatch.setattr(wb, '_rot', lambda check: (None, None, None, None))
    monkeypatch.setattr(wb, '_monikers', lambda *args: iter(records))
    monkeypatch.setattr(wb, '_acquire', lambda *args: book)
    if not valid:
        app.Hwnd = 999
        with pytest.raises(ComputerError, match='^windows_workbook_anchor_unavailable$'):
            _PRODUCTION_APPLICATION(window, lambda: None)
    else:
        assert _PRODUCTION_APPLICATION(window, lambda: None) == (book, app, path)


def test_production_anchor_does_not_swallow_control_guard(active, monkeypatch):
    window, path, _, _ = active
    monkeypatch.setattr(wb, '_rot', lambda check: (None, None, None, None))
    monkeypatch.setattr(wb, '_monikers', lambda *args: iter([(object(), str(path))]))
    def cancelled(*args):
        raise ComputerError('cancelled_by_user')
    monkeypatch.setattr(wb, '_acquire', cancelled)
    with pytest.raises(ComputerError, match='^cancelled_by_user$'):
        _PRODUCTION_APPLICATION(window, lambda: None)


def test_facade_only_exposes_propertyget_and_bounded_items():
    calls = []
    class Pointer:
        def GetIDsOfNames(self, lcid, name):
            calls.append(('name', lcid, name))
            return 7

        def Invoke(self, *args):
            calls.append(('get', *args))
            return Pointer() if len(args) == 5 else 100
    obj = wb._ExistingObject(Pointer())
    assert obj.Hwnd == 100
    assert isinstance(obj.item(1), wb._ExistingObject)
    for name in ('Save', 'Open', 'Close', 'Dispatch', 'Activate'):
        with pytest.raises(AttributeError):
            getattr(obj, name)
    for index in (True, 0, 65, 1.0):
        with pytest.raises(ComputerError, match='^windows_workbook_collection_invalid$'):
            obj.item(index)
    assert calls == [('name', 0, 'Hwnd'), ('get', 7, 0, 2, 1),
                     ('name', 0, 'Item'), ('get', 7, 0, 2, 1, 1)]


@pytest.mark.parametrize('mode', ['exact', 'missing', 'duplicate', 'unreadable'])
def test_rot_acquires_only_unique_exact_running_moniker(active, monkeypatch, mode):
    _, path, _, _ = active
    acquired = []
    def moniker(name):
        return NS(GetDisplayName=lambda context, unused: name)
    exact = moniker(str(path))
    items = [moniker(str(path.parent / 'other' / path.name))]
    if mode != 'missing':
        items.append(exact)
    if mode == 'duplicate':
        items.append(moniker(str(path)))
    class Table:
        def EnumRunning(self):
            return iter(items)

        def GetObject(self, item):
            acquired.append(item)
            if mode == 'unreadable':
                raise RuntimeError('disconnected')
            return NS(QueryInterface=lambda iid: NS())
    monkeypatch.setitem(sys.modules, 'pythoncom', NS(GetRunningObjectTable=Table,
                                                    CreateBindCtx=lambda flags: object(), IID_IDispatch='existing'))
    # Restore the production function hidden by the normal active fixture.
    exact_fn = _PRODUCTION_EXACT
    if mode in {'duplicate', 'unreadable'}:
        with pytest.raises(ComputerError, match='^windows_workbook_rot_' + ('ambiguous' if mode == 'duplicate' else 'unreadable') + '$'):
            exact_fn(path, lambda: None)
    else:
        result = exact_fn(path, lambda: None)
        assert (result is None) is (mode == 'missing')
    assert acquired == ([exact] if mode in {'exact', 'unreadable'} else [])


_PRODUCTION_EXACT = wb._exact_rot_workbook
_PRODUCTION_APPLICATION = wb._workbook_application


@pytest.mark.parametrize('hwnd,native,com_visible', [(101, True, True), (101, False, True),
                                                   (101, True, False), (100, True, True)])
def test_visibility_diagnostic_distinguishes_child_hidden_and_shared_root(
        active, monkeypatch, hwnd, native, com_visible):
    window, path, book, app = active
    book.Windows.items[0].Hwnd = hwnd
    book.Windows.items[0].Visible = com_visible
    app.ActiveWindow.Visible = com_visible
    calls = []
    def visible(handle):
        calls.append(handle)
        return native
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', visible, raising=False)
    result = wb.visibility_diagnostic(window, path, lambda: None)
    assert result['registered'] is True and result['stable'] is True
    evidence = result['first']['workbook_window']
    assert evidence == {'hwnd': hwnd, 'bound': True, 'child': hwnd != 100,
                        'native_visible': native, 'com_visible': com_visible}
    assert len(calls) == 6
    assert result['first'] == result['second']


def test_visibility_diagnostic_unsupported_com_visibility_stays_unknown(active):
    window, path, _, _ = active
    result = wb.visibility_diagnostic(window, path, lambda: None)
    assert result['first']['workbook_window']['com_visible'] is None
    assert result['first']['workbook_window']['native_visible'] is True


def test_visibility_diagnostic_wrong_root_never_reads_native_visibility(active, monkeypatch):
    window, path, book, app = active
    book.Windows.items[0].Hwnd = app.Hwnd = app.ActiveWindow.Hwnd = 999
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible',
                        lambda hwnd: pytest.fail('must bind before visibility'), raising=False)
    result = wb.visibility_diagnostic(window, path, lambda: None)
    assert result['first']['workbook_window']['bound'] is False
    assert result['first']['workbook_window']['native_visible'] is None


def test_visibility_diagnostic_detects_tab_visibility_transition(active, monkeypatch):
    window, path, _, _ = active
    values = iter([True, True, True, False, False, False])
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: next(values), raising=False)
    result = wb.visibility_diagnostic(window, path, lambda: None)
    assert result['stable'] is False
    assert result['first']['workbook_window']['native_visible'] is True
    assert result['second']['workbook_window']['native_visible'] is False


def test_visibility_diagnostic_does_not_swallow_guard(active):
    window, path, _, _ = active
    def cancelled():
        raise ComputerError('cancelled_by_user')
    with pytest.raises(ComputerError, match='^cancelled_by_user$'):
        wb.visibility_diagnostic(window, path, cancelled)


def test_visibility_diagnostic_missing_rot_is_not_closure(active, monkeypatch):
    window, path, _, _ = active
    monkeypatch.setattr(wb, '_exact_rot_workbook', lambda approved, check: None)
    assert wb.visibility_diagnostic(window, path, lambda: None) == {'registered': False, 'stable': None}


def test_component_active_workbook_with_hidden_native_tab_is_not_active(active, monkeypatch):
    window, path, book, app = active
    book.Windows.items[0].Visible = app.ActiveWindow.Visible = True
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: False)
    assert wb.workbook_status(window, path, lambda: None) == {'active': False, 'read_only': None, 'saved': None}
    with pytest.raises(ComputerError, match='^windows_workbook_not_active$'):
        wb.bound_workbook(window, path, lambda: None)
    with pytest.raises(ComputerError, match='^windows_document_not_visible$'):
        wb.same_workbook(book, window, path, lambda: None)


def test_native_child_visibility_wins_over_window_title_or_com_visible(active):
    window, path, book, app = active
    window['title'] = 'untrusted title.docx'
    book.Windows.items[0].Visible = app.ActiveWindow.Visible = True
    assert wb.workbook_status(window, path, lambda: None)['active'] is True


def test_invisible_application_container_is_not_the_editor_visibility_test(active):
    window, path, _, app = active
    assert app.Hwnd == window['hwnd']
    assert sys.modules['win32gui'].IsWindowVisible(app.Hwnd) is False
    assert wb.workbook_status(window, path, lambda: None)['active'] is True


@pytest.mark.parametrize('which', ['workbook', 'active_window'])
def test_root_handle_alone_never_proves_visible_workbook(active, monkeypatch, which):
    window, path, book, app = active
    if which == 'workbook':
        book.Windows.items[0].Hwnd = window['hwnd']
    else:
        app.ActiveWindow.Hwnd = window['hwnd']
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: True)
    with pytest.raises(ComputerError, match='^windows_document_visibility_unavailable$'):
        wb.workbook_status(window, path, lambda: None)


def test_visibility_change_between_two_binding_reads_is_state_change(active, monkeypatch):
    window, path, book, _ = active
    reads = []
    def visible(hwnd):
        reads.append(hwnd)
        return len(reads) <= 2
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', visible)
    with pytest.raises(ComputerError, match='^windows_workbook_state_changed$'):
        wb.workbook_status(window, path, lambda: None)


def test_active_window_hidden_even_when_workbook_window_visible_is_inactive(active, monkeypatch):
    window, path, book, app = active
    app.ActiveWindow.Hwnd = 102
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: hwnd == 101)
    assert wb.workbook_status(window, path, lambda: None)['active'] is False


@pytest.mark.parametrize('present', [True, False])
def test_hidden_component_can_anchor_complete_metadata_lists_without_authorizing_input(active, monkeypatch, present):
    window, path, book, app = active
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: False)
    monkeypatch.setattr(wb, '_rot', lambda check: (None, None, None, None))
    monkeypatch.setattr(wb, '_monikers', lambda *args: iter([(object(), str(path))]))
    monkeypatch.setattr(wb, '_acquire', lambda *args: book)
    anchor = _PRODUCTION_APPLICATION(window, lambda: None)
    assert anchor == (book, app, path)
    monkeypatch.setattr(wb, '_workbook_application', lambda *args: anchor)
    app.Workbooks = Collection([NS(FullName=str(path) if present else 'Unsaved Book1')])
    assert wb.workbook_present(path, lambda: None, window=window) is present
    assert app.Workbooks.calls == [1, 1]
    assert wb.workbook_status(window, path, lambda: None)['active'] is False
    with pytest.raises(ComputerError, match='^windows_document_not_visible$'):
        wb.same_workbook(book, window, path, lambda: None)
