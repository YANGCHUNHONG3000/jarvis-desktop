"""Offline role/identity/side-effect boundaries, separate from native acceptance."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_open_dialog_native as native

PARENT = {'hwnd': 10, 'pid': 20, 'created': 'now', 'image': 'C:\\wps.exe',
          'class_name': 'XLMAIN', 'title': 'original.xlsx', 'profile': 'observe_only'}
DIALOG = {**PARENT, 'hwnd': 11, 'class_name': 'Qt5QWindow', 'title': '打开文件'}


def control(number, kind, name='', patterns=(), value=None, ancestors=()):
    runtime = [42, number]
    return {'id': sha256(json.dumps(runtime).encode()).hexdigest()[:24], 'runtime_id': runtime,
            'type': kind, 'name': name, 'automation_id': '', 'value': value,
            'enabled': True, 'visible': True, 'patterns': list(patterns),
            'ancestors': list(ancestors), 'owner_hwnd': 11}


@pytest.fixture
def source(tmp_path):
    document = {'schema': 1, 'path': str(tmp_path / 'original.xlsx'), 'sha256': 'a' * 64, 'size': 10,
                'format': 'xlsx', 'application': 'wps', 'registered_app': PARENT['image'],
                'read_only': False, 'saved': True}
    field = control(1, 'ComboBox', '文件名称(N)', ['set_value'], '')
    edit = control(2, 'Edit', patterns=['set_value'], value='',
                   ancestors=[{k: field[k] for k in ('type', 'name', 'automation_id')}])
    cancel = control(3, 'Button', '取消', ['invoke'])
    return {'window': deepcopy(PARENT), 'application': 'wps', 'kind': 'open_dialog',
            'binding': {'document': document, 'dialog': deepcopy(DIALOG)}, 'controls': [
                {'role': 'filename', 'label': field['name'], 'control': edit, 'parent': field},
                {'role': 'cancel', 'label': cancel['name'], 'control': cancel, 'parent': None}]}


@pytest.fixture
def windows(monkeypatch):
    state = {'visible': [11], 'exists': {10, 11}, 'foreground': 11, 'owner': {11: 10}, 'raises': [],
             'children': set(), 'roots': {10: 10, 11: 11},
             'tids': {10: 100, 11: 101},
             'identities': {10: deepcopy(PARENT), 11: deepcopy(DIALOG)}}
    gui = SimpleNamespace(
        EnumWindows=lambda callback, unused: [callback(hwnd, None) for hwnd in list(state['visible'])],
        IsWindow=lambda hwnd: hwnd in state['exists'],
        IsWindowVisible=lambda hwnd: hwnd in state['visible'],
        IsWindowEnabled=lambda hwnd: True,
        GetClassName=lambda hwnd: state['identities'][hwnd]['class_name'],
        GetWindowText=lambda hwnd: state['identities'][hwnd]['title'],
        GetWindow=lambda hwnd, flag: state['owner'].get(hwnd, 0),
        IsChild=lambda parent, child: (parent, child) in state['children'],
        GetAncestor=lambda hwnd, flag: state['roots'].get(hwnd, hwnd),
        GetWindowLong=lambda hwnd, flag: 0,
        GetForegroundWindow=lambda: state['foreground'],
        SetForegroundWindow=lambda hwnd: (state['raises'].append(hwnd), state.update(foreground=hwnd)))
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (state['tids'].get(hwnd, 102), state['identities'][hwnd]['pid'])))
    monkeypatch.setattr(native.uia, '_recheck', lambda value: deepcopy(state['identities'][value['hwnd']]))
    monkeypatch.setattr(native.uia, '_identity', lambda hwnd: deepcopy(state['identities'][hwnd]))
    monkeypatch.setattr(native.access, '_kind', lambda value: 'wps')
    monkeypatch.setattr(native.access, '_owned', lambda hwnd, root: hwnd != root)
    monkeypatch.setattr(native.access, '_process_owned', lambda child, parent:
                        all(child[k] == parent[k] for k in ('pid', 'created', 'image')))
    monkeypatch.setattr(native, '_process_facts', lambda pid: next(
        (item['image'], item['created']) for item in state['identities'].values() if item['pid'] == pid))
    monkeypatch.setattr(native, '_thread_input', lambda tid:
                        {'flags': 0, 'capture': 0, 'menu_owner': 0, 'move_size': 0})
    return state


def test_validate_is_static_even_for_missing_documents(source, monkeypatch):
    monkeypatch.setattr(native.binding, '_file', lambda *args: pytest.fail('static must not read files'))
    monkeypatch.setattr(native.uia, '_recheck', lambda *args: pytest.fail('static must not read UI'))
    assert native.validate(source) == source
    source['native_proof'] = {'owned_by_common': True}
    assert native.validate(source) == source


@pytest.mark.parametrize('case', ['role', 'label', 'open_button', 'titlebar', 'extra', 'runtime',
                                  'duplicate', 'parent', 'disabled', 'patterns', 'saved', 'document',
                                  'dialog', 'window', 'boolean_schema', 'unhashable'])
def test_validate_rejects_forged_roles_and_shapes(source, case):
    filename, cancel = source['controls']
    if case == 'role': cancel['role'] = 'open'
    elif case == 'label': cancel['label'] = 'Open'
    elif case == 'open_button': cancel['control']['name'] = cancel['label'] = '打开(O)'
    elif case == 'titlebar': cancel['control']['ancestors'] = [{'type': 'TitleBar', 'name': '', 'automation_id': ''}]
    elif case == 'extra': source['arbitrary_invoke'] = True
    elif case == 'runtime': cancel['control']['runtime_id'][1] += 1
    elif case == 'duplicate': cancel['control']['id'] = filename['control']['id']
    elif case == 'parent': filename['control']['ancestors'] = []
    elif case == 'disabled': cancel['control']['enabled'] = False
    elif case == 'patterns': cancel['control']['patterns'] = []
    elif case == 'saved': source['binding']['document']['saved'] = False
    elif case == 'document': source['binding']['document']['sha256'] = 'broken'
    elif case == 'dialog': source['binding']['dialog']['title'] = 'Save As'
    elif case == 'window': source['window']['hwnd'] = True
    elif case == 'boolean_schema': source['binding']['document']['schema'] = True
    elif case == 'unhashable': source['binding']['document']['format'] = []
    with pytest.raises(ComputerError):
        native.validate(source)


def test_guard_existing_open_dialog(windows):
    assert native._guard(PARENT, lambda: None, foreground=True) == (PARENT, DIALOG, 'visible')


@pytest.mark.parametrize('case', ['unknown', 'foreign', 'nested', 'duplicate', 'changed', 'foreground', 'class'])
def test_guard_rejects_popup_identity_and_ownership_changes(windows, case):
    if case in {'unknown', 'foreign', 'nested', 'duplicate'}:
        windows['visible'].append(12)
        windows['exists'].add(12)
        windows['identities'][12] = {**DIALOG, 'hwnd': 12}
        windows['owner'][12] = 10
    if case == 'unknown': windows['identities'][12]['title'] = 'Payment'
    elif case == 'foreign': windows['identities'][12]['pid'] = 99
    elif case == 'nested': windows['owner'][12] = 11
    elif case == 'changed': windows['identities'][10]['title'] = 'other.xlsx'
    elif case == 'foreground': windows['foreground'] = 10
    elif case == 'class': windows['identities'][11]['class_name'] = 'arbitrary class'
    with pytest.raises(ComputerError):
        native._guard(PARENT, lambda: None, foreground=True)


@pytest.mark.parametrize('case', ['valid', 'foreign_process', 'wrong_root', 'not_child'])
def test_guard_exact_native_child_owner(windows, case):
    windows['identities'][12] = {**PARENT, 'hwnd': 12}
    windows['owner'][11] = 12
    windows['children'].add((10, 12))
    windows['roots'][12] = 10
    if case == 'foreign_process': windows['identities'][12]['pid'] = 99
    elif case == 'wrong_root': windows['roots'][12] = 99
    elif case == 'not_child': windows['children'].clear()
    if case == 'valid':
        assert native._guard(PARENT, lambda: None)[1] == DIALOG
    else:
        with pytest.raises(ComputerError, match='popup_requires_attention'):
            native._guard(PARENT, lambda: None)


@pytest.mark.parametrize('registered', [True, False])
def test_guard_registered_companion_lineage(windows, monkeypatch, registered):
    windows['identities'][11].update(pid=21, image='C:\\et.exe', created='later')
    calls = []
    monkeypatch.setattr(native.access, '_process_owned', lambda child, parent:
                        calls.append((child, parent)) or registered)
    if registered:
        assert native._guard(PARENT, lambda: None)[1]['pid'] == 21
    else:
        with pytest.raises(ComputerError, match='popup_requires_attention'):
            native._guard(PARENT, lambda: None)
    assert len(calls) == 1


@pytest.mark.parametrize('state', ['visible', 'hidden', 'destroyed', 'reused', 'replacement'])
def test_original_dialog_cannot_migrate_or_hide_to_cancel(windows, state):
    if state != 'visible': windows['visible'].clear()
    if state == 'destroyed': windows['exists'].remove(11)
    elif state == 'reused': windows['identities'][11]['pid'] = 99
    elif state == 'replacement':
        windows['exists'].remove(11)
        windows['identities'][12] = {**DIALOG, 'hwnd': 12}
        windows['owner'][12] = 10
        windows['visible'].append(12)
    if state in {'reused', 'replacement'}:
        with pytest.raises(ComputerError, match='changed'):
            native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    else:
        assert native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)[2] == state


@pytest.fixture
def controls(source, monkeypatch):
    from agent_service import windows_visual_form_native as forms
    rows = deepcopy([source['controls'][0]['parent'], source['controls'][0]['control'], source['controls'][1]['control']])
    value = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue='')
    calls = []
    def set_value(text):
        calls.append(('set', text))
        value.CurrentValue = text
        rows[0]['value'] = rows[1]['value'] = text
    value.SetValue = set_value
    wrappers = {row['id']: SimpleNamespace(iface_value=value,
                iface_invoke=SimpleNamespace(Invoke=lambda: calls.append(('invoke', None)))) for row in rows}
    monkeypatch.setattr(forms, '_password_guard', lambda *args: None)
    monkeypatch.setattr(native.access, '_tree', lambda *args: ({'controls': deepcopy(rows), 'truncated': False}, wrappers))
    monkeypatch.setattr(native.access, '_direct_child', lambda *args: True)
    return SimpleNamespace(rows=rows, value=value, wrappers=wrappers, calls=calls)


def test_control_roles_use_actual_native_parent_and_readback(controls, source):
    actual, _ = native._controls(DIALOG, lambda: None)
    assert actual == source['controls']


@pytest.mark.parametrize('failure', ['native_parent', 'read_only', 'readback', 'duplicate_cancel', 'titlebar', 'truncated'])
def test_control_native_proof_is_required(controls, source, monkeypatch, failure):
    if failure == 'native_parent': monkeypatch.setattr(native.access, '_direct_child', lambda *args: False)
    elif failure == 'read_only': controls.value.CurrentIsReadOnly = True
    elif failure == 'readback': controls.value.CurrentValue = 'different'
    elif failure == 'duplicate_cancel': controls.rows.append(control(4, 'Button', '取消', ['invoke']))
    elif failure == 'titlebar': controls.rows[-1]['ancestors'] = [{'type': 'TitleBar', 'name': '', 'automation_id': ''}]
    elif failure == 'truncated': monkeypatch.setattr(native.access, '_tree', lambda *args: ({'truncated': True}, {}))
    with pytest.raises(ComputerError):
        native._controls(DIALOG, lambda: None)
    assert controls.calls == []


@pytest.fixture
def current(windows, source, controls, monkeypatch):
    monkeypatch.setattr(native, '_document', lambda *args, **kwargs: deepcopy(source['binding']['document']))
    return controls


def document_file(tmp_path):
    path = tmp_path / 'target.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override PartName="/word/document.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
    return path


def test_filename_single_value_dispatch_and_independent_readback(current, source, tmp_path):
    path = str(document_file(tmp_path))
    original = deepcopy(source)
    before = native.observe(PARENT, source, 'filename', path, lambda: None)
    assert before['verified'] is False
    native.act(PARENT, source, 'filename', path, lambda: None, [0])
    assert current.calls == [('set', path)]
    after = native.observe(PARENT, source, 'filename', path, lambda: None)
    assert after['verified'] is True and after['facts']['field_value'] == path
    assert source == original


def test_filename_rechecks_native_parent_immediately_before_dispatch(current, source, tmp_path, monkeypatch):
    calls = []
    def parent(*args):
        calls.append(True)
        return len(calls) <= 2
    monkeypatch.setattr(native.access, '_direct_child', parent)
    with pytest.raises(ComputerError, match='parent_unverified'):
        native.act(PARENT, source, 'filename', str(document_file(tmp_path)), lambda: None, [0])
    assert len(calls) == 3 and current.calls == []


def test_filename_readback_failure_does_not_repeat_set(current, source, tmp_path):
    path = str(document_file(tmp_path))
    current.value.SetValue = lambda value: current.calls.append(('set', value))
    with pytest.raises(ComputerError, match='value_unverified'):
        native.act(PARENT, source, 'filename', path, lambda: None, [0])
    assert current.calls == [('set', path)]


def test_target_file_race_stops_before_dispatch(current, source, tmp_path, monkeypatch):
    path = str(document_file(tmp_path))
    original = native.files.snapshot_file
    calls = []
    def snapshot(value):
        token = original(value)
        calls.append(True)
        if len(calls) > 1: token['sha256'] = 'b' * 64
        return token
    monkeypatch.setattr(native.files, 'snapshot_file', snapshot)
    with pytest.raises(ComputerError, match='file_changed'):
        native.act(PARENT, source, 'filename', path, lambda: None, [0])
    assert len(calls) == 2 and current.calls == []


@pytest.mark.parametrize('text', ['cmd.exe', 'https://example.org/file.docx', '..\\file.docx',
                                 'C:\\does-not-exist.docx', '\\\\server\\file.docx', 'calc.exe\n'])
def test_filename_rejects_commands_urls_network_and_missing_files(current, source, text):
    with pytest.raises(ComputerError):
        native.act(PARENT, source, 'filename', text, lambda: None, [0])
    assert current.calls == []


def test_cancel_invokes_exact_button_once_then_observes_destruction(current, windows, source):
    chosen = source['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
        windows['exists'].remove(11)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    native.act(PARENT, source, 'cancel', '', lambda: None, [0])
    assert current.calls == [('cancel', chosen)]
    assert native.observe(PARENT, source, 'cancel', '', lambda: None) == {
        'verified': True, 'facts': {'dialog_presence': 'destroyed', 'document': source['binding']['document'],
                                  'control': None, 'parent': None, 'field_value': None}}


@pytest.mark.parametrize('failure', ['inactive', 'bytes', 'unknown_popup'])
def test_destroyed_cancel_still_requires_unchanged_native_document(current, windows, source, monkeypatch, failure):
    windows['visible'].clear()
    windows['exists'].remove(11)
    if failure == 'unknown_popup':
        windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': 'Unknown popup'}
        windows['owner'][12] = 10
        windows['visible'].append(12)
    else:
        doc = deepcopy(source['binding']['document'])
        doc['saved' if failure == 'inactive' else 'sha256'] = False if failure == 'inactive' else 'b' * 64
        monkeypatch.setattr(native, '_document', lambda *args, **kwargs: doc)
    with pytest.raises(ComputerError):
        native.observe(PARENT, source, 'cancel', '', lambda: None)
    assert current.calls == []


@pytest.mark.parametrize('effect', ['hidden', 'replacement', 'popup'])
def test_cancel_non_destruction_or_popup_never_invokes_twice(current, windows, source, monkeypatch, effect):
    chosen = source['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
        if effect != 'hidden':
            windows['exists'].remove(11)
            windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': 'Payment' if effect == 'popup' else DIALOG['title']}
            windows['owner'][12] = 10
            windows['visible'].append(12)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    clock = iter([0., 2.])
    monkeypatch.setattr(native.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ComputerError):
        native.act(PARENT, source, 'cancel', '', lambda: None, [0])
    assert len(current.calls) == 1


@pytest.mark.parametrize('failure', ['document', 'control', 'text', 'window'])
def test_act_guard_failure_is_zero_dispatch(current, source, windows, monkeypatch, failure):
    request_window, text = PARENT, ''
    if failure == 'document': monkeypatch.setattr(native, '_document', lambda *args, **kwargs: {})
    elif failure == 'control': current.rows[-1]['runtime_id'][1] += 1
    elif failure == 'text': text = 'must be empty'
    elif failure == 'window': request_window = {**PARENT, 'hwnd': 100}
    with pytest.raises(ComputerError):
        native.act(request_window, source, 'cancel', text, lambda: None, [0])
    assert current.calls == []


def test_approved_act_raises_only_original_dialog_then_dispatches(current, source, windows, tmp_path):
    windows['foreground'] = 10
    path = str(document_file(tmp_path))
    native.act(PARENT, source, 'filename', path, lambda: None, [0])
    assert windows['raises'] == [11] and current.calls == [('set', path)]


def test_foreground_denial_dispatches_nothing(current, source, windows, tmp_path, monkeypatch):
    windows['foreground'] = 10
    sys.modules['win32gui'].SetForegroundWindow = lambda hwnd: windows['raises'].append(hwnd)
    clock = iter([0., 2.])
    monkeypatch.setattr(native.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ComputerError, match='foreground_required'):
        native.act(PARENT, source, 'filename', str(document_file(tmp_path)), lambda: None, [0])
    assert windows['raises'] == [11] and current.calls == []


def test_unknown_popup_blocks_even_foreground_activation(current, source, windows, tmp_path):
    windows['foreground'] = 10
    windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': 'Unknown popup'}
    windows['owner'][12] = 10
    windows['visible'].append(12)
    with pytest.raises(ComputerError, match='popup_requires_attention'):
        native.act(PARENT, source, 'filename', str(document_file(tmp_path)), lambda: None, [0])
    assert windows['raises'] == [] and current.calls == []


def test_snapshot_and_observe_never_activate_dialog(current, source, windows):
    windows['foreground'] = 10
    native.snapshot(PARENT, lambda: None)
    native.observe(PARENT, source, 'cancel', '', lambda: None)
    assert windows['raises'] == [] and current.calls == []


def test_snapshot_detects_control_race(current, source, monkeypatch):
    original = native._controls
    calls = []
    def changed(*args):
        rows, wrappers = original(*args)
        calls.append(True)
        if len(calls) > 1: rows[0]['control']['value'] = 'raced'
        return rows, wrappers
    monkeypatch.setattr(native, '_controls', changed)
    with pytest.raises(ComputerError, match='screen_changed'):
        native.snapshot(PARENT, lambda: None)
    assert current.calls == []


@pytest.mark.parametrize('failure', ['', 'unsaved', 'bytes', 'inactive'])
def test_document_existing_fullname_and_status_are_independent(windows, source, monkeypatch, failure):
    from agent_service import windows_workbook_binding as books
    doc = source['binding']['document']
    def no_word(*args): raise ComputerError('windows_document_anchor_unavailable')
    monkeypatch.setattr(native.files, '_document_application', no_word)
    monkeypatch.setattr(books, '_workbook_application', lambda *args:
                        (None, SimpleNamespace(ActiveWorkbook=SimpleNamespace(FullName=doc['path'])), None))
    monkeypatch.setattr(native.files, '_local_path', lambda path: Path(path))
    monkeypatch.setattr(native.files, 'document_status', lambda *args:
                        {'active': failure != 'inactive', 'saved': failure != 'unsaved', 'read_only': False})
    reads = []
    def facts(path):
        reads.append(path)
        return {k: ('b' * 64 if k == 'sha256' and failure == 'bytes' and len(reads) > 1 else doc[k])
                for k in ('path', 'sha256', 'size')}
    monkeypatch.setattr(native.binding, '_file', facts)
    monkeypatch.setattr(native.binding, 'verify', lambda result, **kwargs: result)
    if failure:
        with pytest.raises(ComputerError): native._document(PARENT, lambda: None, expected_dialog=DIALOG)
    else:
        assert native._document(PARENT, lambda: None, expected_dialog=DIALOG) == doc


def test_anchor_diagnostic_reports_exact_failure_without_paths(source, monkeypatch):
    from agent_service import windows_workbook_binding as books
    path = source['binding']['document']['path']
    monkeypatch.setattr(books, '_rot', lambda check: (None, None, None, None))
    monkeypatch.setattr(books, '_monikers', lambda *args: iter([(object(), path)]))
    monkeypatch.setattr(books, '_path', lambda value: Path(value))
    monkeypatch.setattr(books, '_acquire', lambda *args: object())
    def mismatch(*args, **kwargs): raise ComputerError('windows_document_identity_mismatch')
    monkeypatch.setattr(books, '_stable_workbook', mismatch)
    monkeypatch.setattr(books, 'visibility_diagnostic', lambda *args:
                        {'registered': True, 'stable': True, 'first': {'active_path_matches': True}})
    facts = native._anchor_diagnostic(PARENT, lambda: None)
    assert facts['monikers_seen'] == 1
    assert facts['xlsx_candidates'][0]['error'] == 'windows_document_identity_mismatch'
    assert path not in json.dumps(facts)


def test_document_missing_both_anchors_reports_bounded_diagnostic(windows, monkeypatch):
    from agent_service import windows_workbook_binding as books
    def no_word(*args): raise ComputerError('windows_document_anchor_unavailable')
    def no_book(*args): raise ComputerError('windows_workbook_anchor_unavailable')
    monkeypatch.setattr(native.files, '_document_application', no_word)
    monkeypatch.setattr(books, '_workbook_application', no_book)
    monkeypatch.setattr(native, '_anchor_diagnostic', lambda *args: {'monikers_seen': 0, 'xlsx_candidates': []})
    with pytest.raises(ComputerError, match='document_unavailable') as error:
        native._document(PARENT, lambda: None, expected_dialog=DIALOG)
    assert error.value.content_diagnostic['workbook_anchors']['monikers_seen'] == 0


@pytest.fixture
def passive(source, current, monkeypatch):
    value = deepcopy(source)
    value['binding'] = {'scope': 'dialog_controls', 'dialog': deepcopy(DIALOG)}
    monkeypatch.setattr(native, '_document', lambda *args, **kwargs:
                        pytest.fail('dialog controls must never claim COM document authority'))
    def live_record(wrapper, pid):
        assert pid == DIALOG['pid']
        return next(deepcopy(row) for row in current.rows
                    if current.wrappers[row['id']] is wrapper)
    monkeypatch.setattr(native.access, '_native_record', live_record)
    return value


def test_passive_snapshot_explicit_scope_no_document_or_com(passive, current, windows):
    assert native.snapshot(PARENT, lambda: None) == passive
    assert current.calls == [] and windows['raises'] == []
    assert set(passive['binding']) == {'scope', 'dialog'}


@pytest.mark.parametrize('case', ['unknown_scope', 'missing_scope', 'document', 'saved', 'registered_app',
                                  'claimed_state', 'top_document', 'scope_type', 'dialog', 'extra'])
def test_passive_source_rejects_claims_and_nonexact_binding(passive, case):
    if case == 'unknown_scope': passive['binding']['scope'] = 'document'
    elif case == 'missing_scope': del passive['binding']['scope']
    elif case == 'document': passive['binding']['document'] = {}
    elif case == 'saved': passive['binding']['saved'] = True
    elif case == 'registered_app': passive['binding']['registered_app'] = PARENT['image']
    elif case == 'claimed_state': passive['binding']['document_state'] = 'not_claimed'
    elif case == 'top_document': passive['document'] = {}
    elif case == 'scope_type': passive['binding']['scope'] = []
    elif case == 'dialog': passive['binding']['dialog']['title'] = 'Save As'
    elif case == 'extra': passive['binding']['arbitrary_invoke'] = True
    with pytest.raises(ComputerError):
        native.validate(passive)


def test_legacy_source_does_not_downgrade_when_com_unavailable(current, source, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ComputerError('windows_open_dialog_document_unavailable')
    monkeypatch.setattr(native, '_document', unavailable)
    original = deepcopy(source)
    with pytest.raises(ComputerError, match='document_unavailable'):
        native.observe(PARENT, source, 'cancel', '', lambda: None)
    with pytest.raises(ComputerError, match='document_unavailable'):
        native.act(PARENT, source, 'cancel', '', lambda: None, [0])
    assert source == original and current.calls == []


def test_passive_single_input_readback_and_original_source_unchanged(passive, current, tmp_path):
    path = str(document_file(tmp_path))
    original = deepcopy(passive)
    assert native.observe(PARENT, passive, 'filename', path, lambda: None)['verified'] is False
    native.act(PARENT, passive, 'filename', path, lambda: None, [0])
    after = native.observe(PARENT, passive, 'filename', path, lambda: None)
    assert after['verified'] is True
    assert after['facts']['field_value'] == path
    assert after['facts']['binding_scope'] == 'dialog_controls'
    assert after['facts']['document_state'] == 'not_claimed'
    assert 'document' not in after['facts']
    assert current.calls == [('set', path)] and passive == original


@pytest.mark.parametrize('at_target', [False, True])
def test_passive_manual_draft_change_never_overwrites(passive, current, tmp_path, at_target):
    path = str(document_file(tmp_path))
    manual = path if at_target else 'manual draft.docx'
    current.rows[0]['value'] = current.rows[1]['value'] = current.value.CurrentValue = manual
    result = native.observe(PARENT, passive, 'filename', path, lambda: None)
    assert result['verified'] is at_target
    if at_target:
        native.act(PARENT, passive, 'filename', path, lambda: None, [0])
    else:
        with pytest.raises(ComputerError, match='draft_changed'):
            native.act(PARENT, passive, 'filename', path, lambda: None, [0])
    assert current.calls == []


def test_passive_input_readback_failure_never_repeats_set(passive, current, tmp_path):
    path = str(document_file(tmp_path))
    current.value.SetValue = lambda text: current.calls.append(('set', text))
    with pytest.raises(ComputerError, match='value_unverified'):
        native.act(PARENT, passive, 'filename', path, lambda: None, [0])
    assert current.calls == [('set', path)]


@pytest.mark.parametrize('role', ['filename', 'cancel'])
@pytest.mark.parametrize('changed', ['runtime_id', 'enabled', 'visible', 'patterns', 'name', 'value', 'missing'])
def test_passive_live_wrapper_revalidated_at_dispatch(passive, current, tmp_path, monkeypatch, role, changed):
    original = native.access._native_record
    def changed_record(wrapper, pid):
        record = original(wrapper, pid)
        if changed == 'missing': return None
        if changed == 'runtime_id': record['runtime_id'] = [99, 8]
        elif changed in {'enabled', 'visible'}: record[changed] = False
        elif changed == 'patterns': record[changed] = []
        elif changed == 'name': record['name'] = 'Other'
        elif changed == 'value': record['value'] = 'raced'
        return record
    monkeypatch.setattr(native.access, '_native_record', changed_record)
    text = str(document_file(tmp_path)) if role == 'filename' else ''
    with pytest.raises(ComputerError, match='control_changed'):
        native.act(PARENT, passive, role, text, lambda: None, [0])
    assert current.calls == []


def test_passive_cancel_exact_once_original_hwnd_destroyed(passive, current, windows):
    chosen = passive['controls'][1]['control']['id']
    # A successful preceding field action may change both editor/combobox value.
    current.rows[0]['value'] = current.rows[1]['value'] = current.value.CurrentValue = 'previously approved'
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
        windows['exists'].remove(11)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == [('cancel', chosen)]
    assert native.observe(PARENT, passive, 'cancel', '', lambda: None) == {
        'verified': True, 'facts': {'dialog_presence': 'destroyed', 'binding_scope': 'dialog_controls',
                                  'document_state': 'not_claimed', 'control': None,
                                  'parent': None, 'field_value': None}}


@pytest.mark.parametrize('effect', ['hidden', 'replacement', 'popup'])
def test_passive_cancel_hidden_replaced_or_popup_not_success(passive, current, windows, monkeypatch, effect):
    chosen = passive['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
        if effect != 'hidden':
            windows['exists'].remove(11)
            windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': 'Payment' if effect == 'popup' else DIALOG['title']}
            windows['owner'][12] = 10
            windows['visible'].append(12)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    ticks = iter([0., 2.])
    monkeypatch.setattr(native.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(ComputerError):
        native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert len(current.calls) == 1
    if effect == 'hidden':
        assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is False
    else:
        with pytest.raises(ComputerError):
            native.observe(PARENT, passive, 'cancel', '', lambda: None)


def disappearing_identity(windows, monkeypatch, *, alive=False, code='window_unavailable', replacement=None):
    """Destroy only the enumerated candidate during its identity read."""
    original = native.uia._identity
    enumerations = []
    enum = sys.modules['win32gui'].EnumWindows
    monkeypatch.setattr(sys.modules['win32gui'], 'EnumWindows',
                        lambda *args: (enumerations.append(True) if args[0].__qualname__.startswith('_guard.')
                                       else None, enum(*args))[-1])
    def identity(hwnd):
        if hwnd == 11:
            windows['visible'].remove(11)
            if not alive:
                windows['exists'].remove(11)
            if replacement is not None:
                windows['visible'].append(12)
                windows['exists'].add(12)
                windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': replacement}
                windows['owner'][12] = 10
            raise native.uia.UIAError(code)
        return original(hwnd)
    monkeypatch.setattr(native.uia, '_identity', identity)
    return enumerations


def test_guard_destroyed_candidate_rereads_entire_window_set(windows, monkeypatch):
    enumerations = disappearing_identity(windows, monkeypatch)
    assert native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True) == (PARENT, None, 'destroyed')
    assert len(enumerations) == 2


@pytest.mark.parametrize('alive,code', [(True, 'window_unavailable'), (False, 'window_denied')])
def test_guard_never_skips_hidden_or_unrelated_identity_failure(windows, monkeypatch, alive, code):
    enumerations = disappearing_identity(windows, monkeypatch, alive=alive, code=code)
    with pytest.raises(native.uia.UIAError, match=code):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(enumerations) == 1


@pytest.mark.parametrize('root_state', ['destroyed', 'identity_changed'])
def test_guard_destroyed_candidate_does_not_hide_root_failure(windows, monkeypatch, root_state):
    enumerations = disappearing_identity(windows, monkeypatch)
    def root(value):
        if 11 not in windows['exists']:
            if root_state == 'destroyed':
                raise native.uia.UIAError('window_unavailable')
            return {**PARENT, 'created': 'new root'}
        return deepcopy(PARENT)
    monkeypatch.setattr(native.uia, '_recheck', root)
    with pytest.raises((ComputerError, native.uia.UIAError)):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(enumerations) == 1


@pytest.mark.parametrize('replacement', [DIALOG['title'], 'Payment'])
def test_guard_destroyed_candidate_full_reread_still_rejects_replacement_popup(windows, monkeypatch, replacement):
    enumerations = disappearing_identity(windows, monkeypatch, replacement=replacement)
    with pytest.raises(ComputerError, match='changed|popup_requires_attention'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(enumerations) == 2


def test_guard_repeated_destruction_race_is_bounded(windows, monkeypatch):
    enumerations = []
    def enum(callback, _):
        enumerations.append(True)
        hwnd = 10 + len(enumerations)
        windows['visible'][:] = [hwnd]
        windows['exists'].add(hwnd)
        windows['identities'][hwnd] = {**DIALOG, 'hwnd': hwnd}
        windows['owner'][hwnd] = 10
        callback(hwnd, None)
    def identity(hwnd):
        windows['visible'].remove(hwnd)
        windows['exists'].remove(hwnd)
        raise native.uia.UIAError('window_unavailable')
    monkeypatch.setattr(sys.modules['win32gui'], 'EnumWindows', enum)
    monkeypatch.setattr(native.uia, '_identity', identity)
    with pytest.raises(ComputerError, match='changed'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(enumerations) == 2


def test_passive_cancel_destruction_identity_race_never_reinvokes(passive, current, windows, monkeypatch):
    chosen = passive['controls'][1]['control']['id']
    closing = []
    original = native.uia._identity
    def identity(hwnd):
        if hwnd == 11 and closing:
            windows['visible'].remove(11)
            windows['exists'].remove(11)
            raise native.uia.UIAError('window_unavailable')
        return original(hwnd)
    def invoke():
        current.calls.append(('cancel', chosen))
        closing.append(True)
    monkeypatch.setattr(native.uia, '_identity', identity)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == [('cancel', chosen)]
    assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is True


def reject_hidden_uia_identity(windows, monkeypatch):
    original = native.uia._identity
    def identity(hwnd):
        if hwnd not in windows['visible']:
            raise native.uia.UIAError('window_unavailable')
        return original(hwnd)
    monkeypatch.setattr(native.uia, '_identity', identity)


def test_hidden_original_uses_raw_identity_even_when_uia_requires_visible(windows, monkeypatch):
    reject_hidden_uia_identity(windows, monkeypatch)
    windows['visible'].clear()
    with pytest.raises(native.uia.UIAError, match='window_unavailable'):
        native.uia._identity(11)
    assert native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True) == (PARENT, None, 'hidden')


@pytest.mark.parametrize('changed', ['pid', 'title', 'class', 'image', 'created', 'owner', 'nested', 'process'])
def test_hidden_raw_original_still_rejects_identity_and_owner_change(windows, monkeypatch, changed):
    reject_hidden_uia_identity(windows, monkeypatch)
    windows['visible'].clear()
    if changed == 'pid': windows['identities'][11]['pid'] = 99
    elif changed == 'title': windows['identities'][11]['title'] = 'Other'
    elif changed == 'class': windows['identities'][11]['class_name'] = 'Other'
    elif changed == 'image': monkeypatch.setattr(native, '_process_facts', lambda pid: ('foreign.exe', DIALOG['created']))
    elif changed == 'created': monkeypatch.setattr(native, '_process_facts', lambda pid: (DIALOG['image'], 'new process'))
    elif changed == 'owner': windows['owner'][11] = 0
    elif changed == 'nested':
        windows['identities'][12] = {**PARENT, 'hwnd': 12}
        windows['owner'][11] = 12
    elif changed == 'process': monkeypatch.setattr(native.access, '_process_owned', lambda *args: False)
    with pytest.raises(ComputerError, match='changed'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)


def test_hidden_registered_companion_retains_exact_process_identity(windows, monkeypatch):
    expected = {**DIALOG, 'pid': 21, 'image': 'C:\\et.exe', 'created': 'later'}
    windows['identities'][11] = deepcopy(expected)
    windows['visible'].clear()
    reject_hidden_uia_identity(windows, monkeypatch)
    monkeypatch.setattr(native.access, '_process_owned', lambda child, parent: child == expected and parent == PARENT)
    assert native._guard(PARENT, lambda: None, expected=expected, allow_closed=True)[2] == 'hidden'


@pytest.mark.parametrize('eventually_destroyed', [False, True])
def test_passive_cancel_hidden_transition_never_success_until_destroyed(passive, current, windows,
                                                                      monkeypatch, eventually_destroyed):
    reject_hidden_uia_identity(windows, monkeypatch)
    chosen = passive['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    ticks = iter([0., 0.1, 2.])
    monkeypatch.setattr(native.time, 'monotonic', lambda: next(ticks))
    sleeps = []
    def sleep(_):
        sleeps.append(True)
        assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is False
        if eventually_destroyed:
            windows['exists'].remove(11)
    monkeypatch.setattr(native.time, 'sleep', sleep)
    if eventually_destroyed:
        native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
        assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is True
    else:
        with pytest.raises(ComputerError, match='cancel_unproven'):
            native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
        assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is False
    assert current.calls == [('cancel', chosen)] and len(sleeps) == 1


@pytest.mark.parametrize('stage', ['raw_return', 'raw_invalid_handle', 'process_read'])
@pytest.mark.parametrize('after', ['destroyed', 'replacement', 'popup', 'root_gone'])
def test_hidden_disappearance_requires_complete_root_and_popup_reread(windows, monkeypatch, stage, after):
    windows['visible'].clear()
    reject_hidden_uia_identity(windows, monkeypatch)
    scans = []
    enum = sys.modules['win32gui'].EnumWindows
    monkeypatch.setattr(sys.modules['win32gui'], 'EnumWindows',
                        lambda *args: (scans.append(True) if args[0].__qualname__.startswith('_guard.')
                                       else None, enum(*args))[-1])
    def disappear():
        windows['exists'].remove(11)
        if after in {'replacement', 'popup'}:
            windows['exists'].add(12)
            windows['visible'].append(12)
            windows['identities'][12] = {**DIALOG, 'hwnd': 12,
                                       'title': DIALOG['title'] if after == 'replacement' else 'Payment'}
            windows['owner'][12] = 10
        elif after == 'root_gone':
            windows['exists'].remove(10)
    if stage == 'process_read':
        def process(pid):
            disappear()
            return DIALOG['image'], DIALOG['created']
        monkeypatch.setattr(native, '_process_facts', process)
    else:
        def class_name(hwnd):
            if hwnd == 11:
                disappear()
                if stage == 'raw_invalid_handle':
                    error = OSError('invalid window handle')
                    error.winerror = 1400
                    raise error
            return windows['identities'][hwnd]['class_name']
        monkeypatch.setattr(sys.modules['win32gui'], 'GetClassName', class_name)
    def root(value):
        if 10 not in windows['exists']:
            raise native.uia.UIAError('window_unavailable')
        return deepcopy(PARENT)
    monkeypatch.setattr(native.uia, '_recheck', root)
    if after == 'destroyed':
        assert native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)[2] == 'destroyed'
    else:
        with pytest.raises((ComputerError, native.uia.UIAError)):
            native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(scans) == (1 if after == 'root_gone' else 2)


@pytest.mark.parametrize('stage', ['raw', 'process'])
def test_hidden_disappearance_never_swallows_permission_error(windows, monkeypatch, stage):
    windows['visible'].clear()
    def denied(*args):
        windows['exists'].remove(11)
        error = OSError('access denied')
        error.winerror = 5
        raise error
    if stage == 'raw': monkeypatch.setattr(sys.modules['win32gui'], 'GetClassName', denied)
    else: monkeypatch.setattr(native, '_process_facts', denied)
    with pytest.raises(OSError, match='access denied'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)


def test_hidden_raw_repeated_disappearance_is_bounded(windows, monkeypatch):
    windows['visible'].clear()
    reads = []
    def hidden(*args):
        reads.append(True)
        windows['exists'].remove(11)
        raise native._DialogDisappeared('windows_open_dialog_changed')
    enum = sys.modules['win32gui'].EnumWindows
    def enumerate_windows(*args):
        windows['exists'].add(11)
        return enum(*args)
    monkeypatch.setattr(sys.modules['win32gui'], 'EnumWindows', enumerate_windows)
    monkeypatch.setattr(native, '_hidden_identity', hidden)
    with pytest.raises(ComputerError, match='changed'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)
    assert len(reads) == 2


def test_passive_cancel_hidden_raw_destruction_retry_remains_single_invoke(passive, current, windows, monkeypatch):
    chosen = passive['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
    def process(pid):
        windows['exists'].remove(11)
        return DIALOG['image'], DIALOG['created']
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    monkeypatch.setattr(native, '_process_facts', process)
    reject_hidden_uia_identity(windows, monkeypatch)
    native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == [('cancel', chosen)]
    assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is True


@pytest.mark.parametrize('tid', [100, 101])
@pytest.mark.parametrize('key,value', [('flags', 2), ('flags', 4), ('flags', 8), ('flags', 16),
                                      ('capture', 9999), ('capture', 11), ('menu_owner', 9999),
                                      ('move_size', 11)])
def test_bound_thread_input_ownership_blocks_before_activation_or_dispatch(passive, current, windows,
                                                                         monkeypatch, tid, key, value):
    windows['foreground'] = 10
    def state(thread):
        result = {'flags': 0, 'capture': 0, 'menu_owner': 0, 'move_size': 0}
        if thread == tid: result[key] = value
        return result
    monkeypatch.setattr(native, '_thread_input', state)
    with pytest.raises(ComputerError, match='input_busy'):
        native.observe(PARENT, passive, 'cancel', '', lambda: None)
    with pytest.raises(ComputerError, match='input_busy'):
        native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == [] and windows['raises'] == []


@pytest.mark.parametrize('cls', ['Qt5QWindowPopupDropShadowSaveBits', 'Qt5QWindowPopup', '#32768'])
@pytest.mark.parametrize('thread', [100, 101, 999])
def test_ownerless_menu_only_bound_native_threads_block(windows, monkeypatch, cls, thread):
    windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'class_name': cls, 'title': ''}
    windows['owner'][12] = 0
    windows['visible'].append(12)
    windows['exists'].add(12)
    windows['tids'][12] = thread
    monkeypatch.setattr(native.access, '_owned', lambda hwnd, root: windows['owner'].get(hwnd) == root)
    original = sys.modules['win32gui'].GetClassName
    read = []
    def class_name(hwnd):
        read.append(hwnd)
        return original(hwnd)
    monkeypatch.setattr(sys.modules['win32gui'], 'GetClassName', class_name)
    if thread in {100, 101}:
        with pytest.raises(ComputerError, match='input_busy'):
            native._guard(PARENT, lambda: None, expected=DIALOG)
    else:
        assert native._guard(PARENT, lambda: None, expected=DIALOG)[2] == 'visible'
        assert 12 not in read


@pytest.mark.parametrize('error', [ComputerError('windows_open_dialog_input_unavailable'), OSError('access denied')])
def test_thread_input_read_failure_never_dispatches(passive, current, monkeypatch, error):
    def fail(tid): raise error
    monkeypatch.setattr(native, '_thread_input', fail)
    with pytest.raises(type(error)):
        native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == []


def test_destroyed_dialog_cancel_post_read_still_checks_root_input_ownership(passive, current, windows, monkeypatch):
    windows['visible'].clear()
    windows['exists'].remove(11)
    monkeypatch.setattr(native, '_thread_input', lambda tid:
                        {'flags': 0, 'capture': 9704314, 'menu_owner': 0, 'move_size': 0})
    with pytest.raises(ComputerError, match='input_busy'):
        native.observe(PARENT, passive, 'cancel', '', lambda: None)
    assert current.calls == []


def test_thread_input_does_not_query_external_threads(windows, monkeypatch):
    calls = []
    monkeypatch.setattr(native, '_thread_input', lambda tid:
                        calls.append(tid) or {'flags': 1, 'capture': 0, 'menu_owner': 0, 'move_size': 0})
    assert native._guard(PARENT, lambda: None, expected=DIALOG)[2] == 'visible'
    assert set(calls) == {100, 101}


@pytest.mark.parametrize('cls', ['Qt5QWindowPopupDropShadowSaveBits', '#32768'])
def test_destroyed_companion_dialog_still_rejects_its_ownerless_visible_menu(windows, monkeypatch, cls):
    expected = {**DIALOG, 'pid': 21, 'image': 'C:\\et.exe', 'created': 'later'}
    windows['visible'][:] = [12]
    windows['exists'].remove(11)
    windows['exists'].add(12)
    windows['identities'][12] = {**expected, 'hwnd': 12, 'class_name': cls, 'title': ''}
    windows['owner'][12] = 0
    windows['tids'][12] = 101
    monkeypatch.setattr(native.access, '_owned', lambda hwnd, root: windows['owner'].get(hwnd) == root)
    with pytest.raises(ComputerError, match='input_busy'):
        native._guard(PARENT, lambda: None, expected=expected, allow_closed=True)


def test_dialog_disappearing_during_input_guard_cannot_hide_companion_menu(windows, monkeypatch):
    expected = {**DIALOG, 'pid': 21, 'image': 'C:\\et.exe', 'created': 'later'}
    windows['identities'][11] = deepcopy(expected)
    windows['identities'][12] = {**expected, 'hwnd': 12, 'class_name': 'Qt5QWindowPopup', 'title': ''}
    windows['owner'][12] = 0
    windows['tids'][12] = 101
    windows['visible'][:] = [12]
    windows['exists'].add(12)
    monkeypatch.setattr(native.access, '_owned', lambda hwnd, root: windows['owner'].get(hwnd) == root)
    original = sys.modules['win32gui'].IsWindow
    reads = []
    def exists(hwnd):
        if hwnd == 11:
            reads.append(True)
            # A prior successful presence check cannot cache away the fallback.
            if len(reads) == 1: return True
            windows['exists'].discard(11)
            return False
        return original(hwnd)
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindow', exists)
    # Exercise the successful initial check followed by disappearance before
    # thread collection, as can occur between two independent native reads.
    assert sys.modules['win32gui'].IsWindow(11)
    with pytest.raises(ComputerError, match='input_busy'):
        native._input_guard(PARENT, expected, lambda: None)


def test_native_thread_process_list_return_is_normalized_before_set_lookup(passive, current, windows, monkeypatch):
    original = sys.modules['win32process'].GetWindowThreadProcessId
    monkeypatch.setattr(sys.modules['win32process'], 'GetWindowThreadProcessId', lambda hwnd: list(original(hwnd)))
    assert native._guard(PARENT, lambda: None, expected=DIALOG)[2] == 'visible'
    chosen = passive['controls'][1]['control']['id']
    def invoke():
        current.calls.append(('cancel', chosen))
        windows['visible'].clear()
        windows['exists'].remove(11)
    current.wrappers[chosen].iface_invoke.Invoke = invoke
    native.act(PARENT, passive, 'cancel', '', lambda: None, [0])
    assert current.calls == [('cancel', chosen)]
    assert native.observe(PARENT, passive, 'cancel', '', lambda: None)['verified'] is True
