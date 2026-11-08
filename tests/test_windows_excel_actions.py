"""Offline regression checks for compound XLSX read-only opening authority."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace as NS

from openpyxl import Workbook
import pytest

from agent_service.computer import ComputerError, digest
from agent_service import windows_access as access
from agent_service import windows_file_actions as actions
from agent_service import windows_file_dialog as files
from agent_service import windows_file_status as status
from agent_service import windows_document_lifecycle as lifecycle


def control(id, kind, name, *, owner=200, value='', patterns=()):
    return dict(id=id, runtime_id=[1, len(id)], type=kind, name=name, automation_id='',
                owner_hwnd=owner, enabled=True, visible=True, focused=False,
                selected=None, expanded=None, value=value, patterns=list(patterns))


@pytest.fixture
def opening(monkeypatch, tmp_path):
    source = tmp_path / 'approved.xlsx'
    book = Workbook()
    book.active['A1'] = 'authorized bytes'
    book.save(source)
    book.close()
    token = files.encode_token(source)
    window = dict(hwnd=100, pid=10, created='2026', image='C:/WPS/wps.exe', class_name='XLMAIN')
    dialog = dict(hwnd=200, pid=11, created='2026', image='C:/WPS/et.exe', class_name='#32770',
                  title='打开', dialog_kind='open_file')
    field = control('filename', 'ComboBox', '文件名称(N)', value=str(source), patterns=['set_value'])
    edit = control('edit', 'Edit', '', value=str(source), patterns=['set_value'])
    edit['ancestors'] = [{key: field[key] for key in ('type', 'name', 'automation_id')}]
    button = control('open', 'Button', '打开(O)', patterns=['invoke'])
    before = dict(window=window, application='wps', dialogs=[dialog], controls=[field, edit, button],
                  target_selector=files.OPEN_DOCUMENT_SELECTOR, file_token=token, truncated=False)
    message = control('message', 'Text', source.name + ' 正处于锁定状态,', owner=300)
    readonly = control('readonly', 'Button', '只读(R)', owner=300, patterns=['invoke'])
    notify = control('notify', 'Button', '通知(N)', owner=300, patterns=['invoke'])
    popup = {**before, 'dialogs': [{**dialog, 'hwnd': 300, 'title': '文件正在使用',
                                   'class_name': 'bosa_sdm_XL9', 'dialog_kind': 'file_status'}],
             'controls': [message, readonly, notify]}
    goal = {**before, 'dialogs': [], 'controls': [dict(files.OPEN_DOCUMENT_SELECTOR,
            id='jarvis-open-document', value=token)]}
    state = NS(source=source, token=token, window=window, before=before, popup=popup, goal=goal,
               field=field, edit=edit, button=button, readonly=readonly, message=message,
               effects=[], stage='before', reads=0, on_observe=None, on_record=None,
               readonly_status={'active': True, 'read_only': True, 'saved': True})

    def invoke_open():
        state.effects.append('open')
        state.stage = 'popup'

    def invoke_readonly():
        state.effects.append('readonly')
        state.stage = 'goal'

    wrappers = {c['id']: NS(id=c['id']) for c in [field, edit, button, message, readonly, notify]}
    wrappers['open'].iface_invoke = NS(Invoke=invoke_open)
    wrappers['readonly'].iface_invoke = NS(Invoke=invoke_readonly)
    wrappers['notify'].iface_invoke = NS(Invoke=lambda: state.effects.append('notify'))
    def observe(request, check):
        check()
        state.reads += 1
        observed = deepcopy(getattr(state, state.stage))
        if state.on_observe:
            state.on_observe(state.reads, observed)
        return observed, wrappers
    def record(wrapper, pid):
        actual = next(c for c in [*state.before['controls'], *state.popup['controls']] if c['id'] == wrapper.id)
        result = deepcopy(actual)
        if state.on_record:
            state.on_record(wrapper.id, result)
        return result
    monkeypatch.setitem(sys.modules, 'win32gui', NS())
    monkeypatch.setattr(access.uia, '_native', nullcontext)
    monkeypatch.setattr(access.uia, '_recheck', lambda win: win)
    monkeypatch.setattr(access, '_request_observation', observe)
    monkeypatch.setattr(access, '_native_record', record)
    monkeypatch.setattr(access, '_direct_child', lambda *args: True)
    monkeypatch.setattr(access, '_owned', lambda *args: True)
    monkeypatch.setattr(access, '_process_owned', lambda *args: True)
    monkeypatch.setattr(files, 'document_status', lambda *args: deepcopy(state.readonly_status))
    ticks = iter(range(0, 1000, 4))
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(access.time, 'sleep', lambda seconds: None)
    state.request = dict(op='act', window=window, control=deepcopy(button),
                         before_sha=digest(before), action='open_local_file', value=token,
                         selector=files.OPEN_DOCUMENT_SELECTOR, document_token=token)
    return state


def test_compound_open_invokes_only_readonly_after_fullpath_and_writes_distinct_intent(opening, tmp_path):
    s = opening
    result = access._perform(s.request, tmp_path, lambda: None, [0])
    assert result['ok'] and s.effects == ['open', 'readonly']
    assert (tmp_path / 'native-intent.json').is_file()
    assert (tmp_path / 'workbook-readonly-intent.json').is_file()
    assert result['observation']['readonly_attestation'] == s.readonly_status


@pytest.mark.parametrize('change', ['wrong_message', 'wrong_class', 'wrong_title', 'duplicate_button',
                                  'hidden_button', 'disabled_message', 'blocked', 'truncated'])
def test_popup_recognizer_rejects_nonexact_or_ambiguous_evidence(opening, change):
    s = opening
    popup = deepcopy(s.popup)
    if change == 'wrong_message':
        popup['controls'][0]['name'] = 'different.xlsx 正处于锁定状态,'
    elif change == 'wrong_class':
        popup['dialogs'][0]['class_name'] = 'Qt5QWindow'
    elif change == 'wrong_title':
        popup['dialogs'][0]['title'] = '通知'
    elif change == 'duplicate_button':
        popup['controls'].append({**popup['controls'][1], 'id': 'duplicate'})
    elif change == 'hidden_button':
        popup['controls'][1]['visible'] = False
    elif change == 'disabled_message':
        popup['controls'][0]['enabled'] = False
    elif change == 'blocked':
        popup['blocked_reason'] = 'unknown_modal'
    else:
        popup['truncated'] = True
    with pytest.raises(ComputerError, match='^windows_occupied_prompt_unverified$'):
        status.workbook_occupied_controls(popup, str(s.source))


def test_basename_only_standalone_popup_has_no_action_candidates_or_generic_invoke(opening):
    s = opening
    condition = {'selector': files.OPEN_DOCUMENT_SELECTOR, 'state': 'value', 'value': s.token}
    assert actions.candidates(s.popup, condition) == []
    for button in s.popup['controls'][1:]:
        assert not access.allowed(s.popup, button, 'invoke', '')
        assert not access.allowed(s.popup, button, 'open_readonly', s.token)


def test_standalone_readonly_request_cannot_reenter_compound_branch(opening, tmp_path):
    s = opening
    s.stage = 'popup'
    request = {**s.request, 'control': deepcopy(s.readonly), 'action': 'open_readonly',
               'before_sha': digest(s.popup)}
    with pytest.raises(ComputerError, match='^windows_action_denied$'):
        access._perform(request, tmp_path, lambda: None, [0])
    assert not s.effects and not (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('change', ['second_observation', 'message_record', 'button_record',
                                  'owner', 'process', 'source_bytes'])
def test_compound_rechecks_all_popup_authority_before_readonly(opening, tmp_path, monkeypatch, change):
    s = opening
    if change == 'second_observation':
        s.on_observe = lambda count, view: view.update(truncated=True) if count == 3 else None
    elif change in {'message_record', 'button_record'}:
        changed_id = 'message' if change == 'message_record' else 'readonly'
        s.on_record = lambda id, actual: actual.update(name='changed') if id == changed_id else None
    elif change == 'owner':
        monkeypatch.setattr(access, '_owned', lambda child, parent: child != 300)
    elif change == 'process':
        monkeypatch.setattr(access, '_process_owned', lambda child, parent: child['hwnd'] != 300)
    else:
        def mutate(count, view):
            if count == 3:
                s.source.write_bytes(b'changed after opening')
        s.on_observe = mutate
    with pytest.raises(ComputerError):
        access._perform(s.request, tmp_path, lambda: None, [0])
    assert s.effects == ['open']
    assert not (tmp_path / 'workbook-readonly-intent.json').exists()


@pytest.mark.parametrize('active,readonly', [(True, False), (False, True), (False, False)])
def test_compound_requires_exact_active_readonly_com_postcondition(opening, tmp_path, active, readonly):
    s = opening
    s.readonly_status.update(active=active, read_only=readonly)
    result = access._perform(s.request, tmp_path, lambda: None, [0])
    assert result['state'] == result['effects'] == 'unknown'
    assert s.effects == ['open', 'readonly']


def test_recovery_of_same_native_operation_cannot_invoke_either_button_again(opening, tmp_path):
    s = opening
    access._perform(s.request, tmp_path, lambda: None, [0])
    s.stage = 'before'
    with pytest.raises((ComputerError, FileExistsError)):
        access._perform(s.request, tmp_path, lambda: None, [0])
    assert s.effects == ['open', 'readonly']


@pytest.mark.parametrize('extension,klass,image,expected', [
    ('.xlsx', 'XLMAIN', 'wps.exe', True), ('.xlsx', 'XLMAIN', 'et.exe', True),
    ('.docx', 'OpusApp', 'wps.exe', True), ('.xlsx', 'OpusApp', 'wps.exe', True),
    ('.docx', 'XLMAIN', 'wps.exe', True), ('.docx', 'OpusApp', 'et.exe', False),
    ('.docx', 'XLMAIN', 'et.exe', False), ('.xlsx', 'OpusApp', 'et.exe', False),
    ('.xlsm', 'XLMAIN', 'wps.exe', False), ('.docx', 'OtherFrame', 'wps.exe', False)])
def test_format_and_application_pairing_is_explicit(extension, klass, image, expected):
    assert access._file_application({'class_name': klass, 'image': image}, 'C:/source' + extension) is expected


@pytest.mark.parametrize('kind', ['open', 'lifecycle'])
@pytest.mark.parametrize('suffix,klass', [('.xlsx', 'UnknownClass'), ('.docx', 'XLMAIN')])
def test_observation_rejects_format_interchange_before_any_native_read(opening, monkeypatch, suffix, klass, kind):
    s = opening
    token = 'test-token'
    path = str(s.source.with_suffix(suffix))
    window = {**s.window, 'class_name': klass, 'image': 'C:/WPS/et.exe'}
    called = []
    native = lambda: called.append(True)
    if kind == 'open':
        monkeypatch.setattr(files, 'decode_token', lambda value: {'path': path})
        operation = lambda: actions.observe(window, token, lambda: None, native)
    else:
        from agent_service import windows_document_copy as copies
        monkeypatch.setattr(copies, 'decode', lambda value: {'source': {'path': path}})
        operation = lambda: lifecycle.observe(window, token, lifecycle.SAVE_SELECTOR, lambda: None, native)
    with pytest.raises(ComputerError, match='^windows_file_application_invalid$'):
        operation()
    assert called == []


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.mark.parametrize('case,expected', [('exact', True), ('word_frame', True), ('other_exe', False), ('wrong_parent_class', False),
                                         ('wrong_registered_path', False), ('different_install', False),
                                         ('not_descendant', False), ('older_child', False)])
def test_cross_exe_owner_requires_exact_companion_install_and_descendant(monkeypatch, tmp_path, case, expected):
    import ctypes
    from agent_service import workbook_open
    folder = tmp_path / 'WPS'
    folder.mkdir()
    parent = dict(hwnd=100, pid=10, created='2026-01-01', image=str(folder / 'wps.exe'), class_name='XLMAIN')
    child = dict(hwnd=200, pid=11, created='2026-01-02', image=str(folder / 'et.exe'), class_name='#32770')
    registered = Path(child['image'])
    if case == 'other_exe':
        parent['image'] = str(folder / 'other.exe')
    elif case == 'wrong_parent_class':
        parent['class_name'] = 'UnknownClass'
    elif case == 'word_frame':
        parent['class_name'] = 'OpusApp'
    elif case == 'wrong_registered_path':
        registered = folder / 'different.exe'
    elif case == 'different_install':
        child['image'] = str(tmp_path / 'OtherWPS' / 'et.exe')
        registered = Path(child['image'])
    elif case == 'older_child':
        child['created'] = '2025-12-31'
    monkeypatch.setattr(access, '_wps', lambda identity: True)
    monkeypatch.setattr(workbook_open, 'resolve_workbook_application', lambda: {'executable': str(registered)})
    def first(handle, pointer):
        pointer._obj.pid = child['pid']
        pointer._obj.parent = 999 if case == 'not_descendant' else parent['pid']
        return True
    kernel = NS(CreateToolhelp32Snapshot=Function(lambda *args: 1),
                Process32FirstW=Function(first), Process32NextW=Function(lambda *args: False),
                CloseHandle=Function(lambda *args: True))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: kernel)
    assert access._process_owned(child, parent) is expected
