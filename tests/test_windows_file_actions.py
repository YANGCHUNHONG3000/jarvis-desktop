"""Approved DOCX file actions over synthetic UIA; no desktop or Office calls."""
from contextlib import nullcontext
from copy import deepcopy
import json
import sys
from types import SimpleNamespace
import zipfile

import pytest

from agent_service import windows_access as access
from agent_service import windows_file_actions as actions
from agent_service import windows_file_dialog as files
from agent_service import windows_task as task
from agent_service.computer import ComputerError, digest


def docx(path, text=''):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml',
            '<Types><Override PartName="/word/document.xml" ContentType="'
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p>' + text + '</w:p></w:body></w:document>')


def control(id, type, name, **changes):
    return dict(id=id, runtime_id=[1, len(id)], type=type, name=name, automation_id='',
                enabled=True, visible=True, owner_hwnd=200, patterns=[], value='',
                focused=False, selected=None, expanded=None, **changes)


@pytest.fixture
def file_view(tmp_path):
    source = tmp_path / 'approved.docx'
    docx(source)
    token = files.encode_token(source)
    field = control('filename', 'ComboBox', '文件名称(N)')
    field['patterns'] = ['set_value']
    editor = control('editor', 'Edit', '')
    editor.update(patterns=['set_value'], ancestors=[{k: field[k] for k in ('type', 'name', 'automation_id')}])
    button = control('open', 'Button', '打开(O)')
    button['patterns'] = ['invoke']
    observation = {'window': {'hwnd': 100, 'pid': 10, 'class_name': 'OpusApp',
                              'image': 'C:/wps.exe'}, 'application': 'wps',
        'controls': [field, editor, button], 'dialogs': [
            {'hwnd': 200, 'pid': 10, 'class_name': 'Qt5QWindow', 'title': '打开文件', 'dialog_kind': 'open_file'}],
        'target_selector': deepcopy(files.OPEN_DOCUMENT_SELECTOR), 'file_token': token, 'truncated': False}
    condition = {'selector': deepcopy(files.OPEN_DOCUMENT_SELECTOR), 'state': 'value', 'value': token}
    return SimpleNamespace(source=source, token=token, field=field, editor=editor, button=button,
                           observation=observation, condition=condition)


def test_file_candidates_advance_only_current_approved_stage(file_view):
    s = file_view
    assert actions.candidates(s.observation, s.condition) == [
        {'control_id': 'filename', 'action': 'set_file_path', 'value': s.token}]
    s.field['value'] = s.editor['value'] = str(s.source)
    assert actions.candidates(s.observation, s.condition) == [
        {'control_id': 'open', 'action': 'open_local_file', 'value': s.token}]
    s.observation.update(dialogs=[], controls=[])
    assert actions.candidates(s.observation, s.condition) == [
        {'control_id': None, 'action': 'open_file_dialog', 'value': s.token}]


@pytest.mark.parametrize('change', ['token', 'selector', 'application', 'blocked', 'truncated'])
def test_file_permission_requires_exact_scope_and_token(file_view, change):
    s = file_view
    if change == 'token':
        s.observation['file_token'] = 'other token'
    elif change == 'selector':
        s.observation['target_selector'] = {**files.OPEN_DOCUMENT_SELECTOR, 'name': 'different'}
    elif change == 'application':
        s.observation['application'] = 'explorer'
    elif change == 'blocked':
        s.observation['blocked_reason'] = 'unknown dialog'
    else:
        s.observation['truncated'] = True
    assert not actions.allowed(s.observation, s.field, 'set_file_path', s.token)


@pytest.mark.parametrize('change', ['editor_value', 'editor_missing', 'button_pattern', 'editor_parent'])
def test_open_permission_requires_exact_filename_editor_and_invoke_capability(file_view, change):
    s = file_view
    s.field['value'] = s.editor['value'] = str(s.source)
    if change == 'editor_value':
        s.editor['value'] = str(s.source.parent / 'unapproved.docx')
    elif change == 'editor_missing':
        s.observation['controls'].remove(s.editor)
    elif change == 'button_pattern':
        s.button['patterns'] = []
    else:
        s.editor['ancestors'] = []
    assert not actions.allowed(s.observation, s.button, 'open_local_file', s.token)
    assert actions.candidates(s.observation, s.condition) == []


def test_changed_pinned_bytes_never_produce_action_or_postcondition(file_view):
    s = file_view
    s.field['value'] = s.editor['value'] = str(s.source)
    docx(s.source, 'changed')
    assert not actions.allowed(s.observation, s.button, 'open_local_file', s.token)
    assert actions.candidates(s.observation, s.condition) == []
    with pytest.raises(ComputerError, match='windows_file_changed'):
        actions.verified('open_local_file', s.observation, s.token)


def test_set_path_requires_both_combo_and_editor_readback(file_view):
    s = file_view
    s.field['value'] = str(s.source)
    assert not actions.verified('set_file_path', s.observation, s.token)
    s.editor['value'] = str(s.source)
    assert actions.verified('set_file_path', s.observation, s.token)


@pytest.fixture
def file_native(monkeypatch, file_view):
    s = file_view
    s.effects, s.parent_id, s.on_record = [], s.field['runtime_id'], None
    class NoPatternInterfaceError(Exception):
        pass
    def set_value(value):
        s.effects.append(('set', value))
        s.field['value'] = s.editor['value'] = value
    def invoke():
        s.effects.append(('invoke', None))
        s.observation['dialogs'] = []
        s.observation['controls'] = [dict(files.OPEN_DOCUMENT_SELECTOR,
            id='jarvis-open-document', value=s.token)]
    wrappers = {
        'filename': SimpleNamespace(element_info=SimpleNamespace(element='filename')),
        'editor': SimpleNamespace(element_info=SimpleNamespace(element='editor'),
            iface_value=SimpleNamespace(CurrentIsReadOnly=False, SetValue=set_value)),
        'open': SimpleNamespace(iface_invoke=SimpleNamespace(Invoke=invoke)),
    }
    walker = SimpleNamespace(GetParentElement=lambda element:
        SimpleNamespace(GetRuntimeId=lambda: s.parent_id))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(
        NoPatternInterfaceError=NoPatternInterfaceError,
        IUIA=lambda: SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker))))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(GetForegroundWindow=lambda: 100))
    monkeypatch.setattr(access.uia, '_native', nullcontext)
    monkeypatch.setattr(access.uia, '_recheck', lambda w: w)
    monkeypatch.setattr(access, '_owned', lambda *args: True)
    monkeypatch.setattr(access, '_process_owned', lambda *args: True)
    monkeypatch.setattr(access, '_request_observation', lambda request, check:
        (deepcopy(s.observation), wrappers))
    def record(wrapper, pid):
        key = next(k for k, v in wrappers.items() if v is wrapper)
        if s.on_record:
            s.on_record(key)
        source = {'filename': s.field, 'editor': s.editor, 'open': s.button}[key]
        return {k: v for k, v in deepcopy(source).items() if k not in {'owner_hwnd', 'ancestors'}}
    monkeypatch.setattr(access.uia, '_record', record)
    s.wrappers = wrappers
    def request(action):
        c = s.field if action == 'set_file_path' else s.button
        return {'op': 'act', 'window': s.observation['window'], 'selector': files.OPEN_DOCUMENT_SELECTOR,
                'document_token': s.token, 'control': deepcopy(c), 'action': action, 'value': s.token,
                'before_sha': digest(s.observation)}
    s.request = request
    return s


def test_native_file_path_sets_only_the_pinned_full_path(tmp_path, file_native):
    s = file_native
    result = access._perform(s.request('set_file_path'), tmp_path, lambda: None, [0])
    assert result['ok'] and s.effects == [('set', str(s.source))]
    assert json.loads((tmp_path / 'native-intent.json').read_text())['request_sha']


def test_native_file_editor_wrong_direct_parent_stops_before_intent(tmp_path, file_native):
    s = file_native
    s.parent_id = [999]
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(s.request('set_file_path'), tmp_path, lambda: None, [0])
    assert not s.effects and not (tmp_path / 'native-intent.json').exists()


@pytest.mark.parametrize('change_at', [1, 2, 3])
def test_open_rechecks_filename_before_intent_and_before_invoke(tmp_path, file_native, change_at):
    s = file_native
    s.field['value'] = s.editor['value'] = str(s.source)
    button_reads = []
    def change(key):
        if key == 'open':
            button_reads.append(True)
            if len(button_reads) == change_at:
                s.field['value'] = s.editor['value'] = str(s.source.parent / 'unapproved.docx')
    s.on_record = change
    with pytest.raises(ComputerError):
        access._perform(s.request('open_local_file'), tmp_path, lambda: None, [0])
    assert not s.effects
    assert (tmp_path / 'native-intent.json').exists() == (change_at == 3)


def test_open_rechecks_filename_direct_parent_before_invocation(tmp_path, file_native):
    s = file_native
    s.field['value'] = s.editor['value'] = str(s.source)
    s.parent_id = [999]
    with pytest.raises(ComputerError):
        access._perform(s.request('open_local_file'), tmp_path, lambda: None, [0])
    assert not s.effects and not (tmp_path / 'native-intent.json').exists()


def test_native_open_dialog_uses_only_fixed_command_after_intent(monkeypatch, tmp_path, file_native):
    from agent_service import windows_input
    s = file_native
    dialog = deepcopy(s.observation['dialogs'])
    controls = s.observation['controls']
    s.observation.update(dialogs=[], controls=[])
    calls = []
    def send_open(hwnd, *, check, owns, should_stop):
        assert (tmp_path / 'native-intent.json').is_file()
        assert hwnd == 100
        calls.append(hwnd)
        s.observation.update(dialogs=dialog, controls=controls)
        return 9
    monkeypatch.setattr(windows_input, 'send_open', send_open)
    request = {'op': 'act', 'window': s.observation['window'], 'control': None,
        'selector': files.OPEN_DOCUMENT_SELECTOR, 'document_token': s.token,
        'action': 'open_file_dialog', 'value': s.token, 'before_sha': digest(s.observation)}
    epoch = [0]
    assert access._perform(request, tmp_path, lambda: None, epoch)['ok']
    assert calls == [100] and not s.effects and epoch == [9]


def test_file_observation_attests_only_the_approved_path(monkeypatch, file_view):
    s = file_view
    s.observation.update(dialogs=[], controls=[])
    checked = []
    def active(window, path, check):
        checked.append(path)
        return False
    monkeypatch.setattr(files, 'document_is_active', active)
    result, _ = actions.observe(s.observation['window'], s.token, lambda: None,
        lambda: (deepcopy(s.observation), {}))
    assert checked == [str(s.source)] and result['controls'][-1]['value'] == ''
    monkeypatch.setattr(files, 'document_is_active', lambda window, path, check: True)
    result, _ = actions.observe(s.observation['window'], s.token, lambda: None,
        lambda: (deepcopy(s.observation), {}))
    assert result['controls'][-1]['value'] == s.token


class FileBackend:
    def __init__(self, view):
        self.view, self.calls, self.opened, self.cancelled = view, [], False, False
        self.stop_after, self.unknown = None, False
        self.view.observation['dialogs'] = []
        self.dialog = {'hwnd': 200, 'pid': 10, 'class_name': 'Qt5QWindow',
                       'title': '打开文件', 'dialog_kind': 'open_file'}
        self.controls = self.view.observation['controls']
        self.view.observation['controls'] = []

    def observe_condition(self, condition, *, stop):
        assert condition['value'] == self.view.token
        observed = deepcopy(self.view.observation)
        observed['controls'].append(dict(files.OPEN_DOCUMENT_SELECTOR,
            id='jarvis-open-document', enabled=True, visible=True, value=self.view.token if self.opened else ''))
        return observed

    candidates = staticmethod(actions.candidates)

    def act(self, window, control, action, value, before_sha, operation, *, stop):
        self.calls.append(action)
        assert value == self.view.token
        if action == 'open_file_dialog':
            self.view.observation.update(dialogs=[self.dialog], controls=self.controls)
        elif action == 'set_file_path':
            self.view.field['value'] = self.view.editor['value'] = str(self.view.source)
        elif action == 'open_local_file':
            self.opened = True
            self.view.observation.update(dialogs=[], controls=[])
        (operation / 'receipt.json').write_text(json.dumps({'action': action}))
        if action == self.stop_after:
            self.cancelled = True
        return ({'ok': False, 'state': 'unknown', 'effects': 'unknown'} if self.unknown else
                {'ok': True, 'state': 'verified', 'effects': 'verified'})


def file_task(tmp_path, view, selector=None):
    view.observation['window'] = dict(hwnd=100, pid=10, created='start', image='C:/wps.exe',
                                    class_name='OpusApp', title='Original document', profile='wps')
    condition = dict(view.condition, id='open_doc', window=view.observation['window'], retain=True)
    if selector is not None:
        condition['selector'] = deepcopy(selector)
        view.observation['target_selector'] = deepcopy(selector)
    return task.create(tmp_path, {'schema': 1, 'instruction': 'Open the approved local DOCX',
        'conditions': [condition], 'max_steps': 6})


def test_file_resume_uses_current_dialog_stage_without_repeating_completed_effects(tmp_path, file_view):
    root = file_task(tmp_path, file_view)
    backend = FileBackend(file_view)
    backend.stop_after = 'set_file_path'
    first = task.run(root, backend, stop=lambda: backend.cancelled)
    assert first['state'] == 'paused' and first['completed'] == []
    assert backend.calls == ['open_file_dialog', 'set_file_path']
    old = {p: p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    backend.cancelled, backend.stop_after = False, None
    assert task.run(root, backend)['ok']
    assert backend.calls == ['open_file_dialog', 'set_file_path', 'open_local_file']
    assert all(p.read_bytes() == content for p, content in old.items())
    assert task.run(root, backend)['ok']
    assert len(backend.calls) == 3


def test_file_action_unknown_cannot_be_resumed_or_replayed(tmp_path, file_view):
    root = file_task(tmp_path, file_view)
    backend = FileBackend(file_view)
    backend.unknown = True
    assert task.run(root, backend)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, backend)
    assert backend.calls == ['open_file_dialog']


def occupied_view(view):
    message = control('occupied-message', 'Text',
        '文档已被占用，建议以“只读”模式打开。\n(' + str(view.source) + ')')
    button = control('readonly', 'Button', '只读打开')
    button['patterns'] = ['invoke']
    view.observation.update(controls=[message, button], dialogs=[{
        'hwnd': 200, 'pid': 10, 'title': 'WPS 文字', 'class_name': 'Qt5QWindow', 'dialog_kind': 'file_status'}])
    return message, button


def test_occupied_open_result_is_only_an_intermediate_verified_stage(file_view):
    s = file_view
    _, button = occupied_view(s)
    assert actions.verified('open_local_file', s.observation, s.token)
    assert actions.candidates(s.observation, s.condition) == [
        {'control_id': button['id'], 'action': 'open_readonly', 'value': s.token}]
    assert not actions.verified('open_readonly', s.observation, s.token)


@pytest.mark.parametrize('mismatch', ['same_name_other_path', 'unknown_prompt', 'changed_pin'])
def test_unverified_occupied_prompt_cannot_offer_readonly(file_view, mismatch):
    s = file_view
    message, button = occupied_view(s)
    if mismatch == 'same_name_other_path':
        message['name'] = message['name'].replace(str(s.source), str(s.source.parent / 'other' / s.source.name))
    elif mismatch == 'unknown_prompt':
        message['name'] = 'Other warning (' + str(s.source) + ')'
    else:
        docx(s.source, 'changed')
    assert not actions.allowed(s.observation, button, 'open_readonly', s.token)
    assert actions.candidates(s.observation, s.condition) == []


@pytest.mark.parametrize('read_only', [True, False])
def test_readonly_goal_requires_precise_active_document_readonly_attestation(monkeypatch, file_view, read_only):
    s = file_view
    s.observation.update(dialogs=[], controls=[], target_selector=actions.READONLY_SELECTOR)
    monkeypatch.setattr(files, 'document_is_active', lambda *args: True)
    calls = []
    def document_status(window, path, check):
        calls.append(path)
        return {'active': True, 'read_only': read_only}
    monkeypatch.setattr(files, 'document_status', document_status)
    observed, _ = actions.observe(s.observation['window'], s.token, lambda: None,
        lambda: (deepcopy(s.observation), {}))
    assert observed['controls'][-1]['value'] == (s.token if read_only else '')
    assert observed['controls'][-1]['automation_id'] == actions.READONLY_SELECTOR['automation_id']
    assert calls == [str(s.source)]


@pytest.fixture
def readonly_native(monkeypatch, file_native):
    s = file_native
    s.message, s.readonly = occupied_view(s)
    s.observation['target_selector'] = deepcopy(actions.READONLY_SELECTOR)
    def invoke():
        s.effects.append(('readonly', None))
        s.observation.update(dialogs=[], controls=[dict(actions.READONLY_SELECTOR,
            id='jarvis-open-document', value=s.token)])
    s.wrappers.update({
        s.message['id']: SimpleNamespace(),
        s.readonly['id']: SimpleNamespace(iface_invoke=SimpleNamespace(Invoke=invoke)),
    })
    def record(wrapper, pid):
        key = 'button' if wrapper is s.wrappers[s.readonly['id']] else 'message'
        if s.on_record:
            s.on_record(key)
        source = s.readonly if key == 'button' else s.message
        return {k: v for k, v in deepcopy(source).items() if k not in {'owner_hwnd', 'ancestors'}}
    monkeypatch.setattr(access.uia, '_record', record)
    monkeypatch.setattr(files, 'document_status', lambda window, path, check:
        {'active': True, 'read_only': True})
    s.readonly_request = {'op': 'act', 'window': s.observation['window'], 'control': deepcopy(s.readonly),
        'selector': deepcopy(actions.READONLY_SELECTOR), 'document_token': s.token,
        'action': 'open_readonly', 'value': s.token, 'before_sha': digest(s.observation)}
    return s


def test_native_readonly_invoke_requires_post_readonly_status(tmp_path, readonly_native):
    s = readonly_native
    result = access._perform(s.readonly_request, tmp_path, lambda: None, [0])
    assert result['ok'] and s.effects == [('readonly', None)]
    assert result['observation']['readonly_attestation'] == {'active': True, 'read_only': True}


@pytest.mark.parametrize('change_at', [1, 2, 3])
def test_readonly_message_path_race_stops_before_intent_or_invoke(tmp_path, readonly_native, change_at):
    s = readonly_native
    reads = []
    def change(key):
        if key == 'button':
            reads.append(True)
            if len(reads) == change_at:
                s.message['name'] = s.message['name'].replace(str(s.source),
                    str(s.source.parent / 'other' / s.source.name))
    s.on_record = change
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        access._perform(s.readonly_request, tmp_path, lambda: None, [0])
    assert not s.effects
    assert (tmp_path / 'native-intent.json').exists() == (change_at == 3)


def test_native_readonly_without_status_does_not_retry_invocation(monkeypatch, tmp_path, readonly_native):
    s = readonly_native
    monkeypatch.setattr(files, 'document_status', lambda window, path, check:
        {'active': True, 'read_only': False})
    ticks = iter([0, 4, 8])
    monkeypatch.setattr(access.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(access.time, 'sleep', lambda seconds: None)
    result = access._perform(s.readonly_request, tmp_path, lambda: None, [0])
    assert result['state'] == 'unknown' and result['effects'] == 'unknown'
    assert s.effects == [('readonly', None)]


@pytest.mark.parametrize('ownership', ['owner', 'process'])
def test_readonly_rechecks_native_dialog_ownership_before_intent(monkeypatch, tmp_path, readonly_native, ownership):
    s = readonly_native
    monkeypatch.setattr(access, '_owned' if ownership == 'owner' else '_process_owned', lambda *args: False)
    with pytest.raises(ComputerError, match='windows_dialog_identity_changed'):
        access._perform(s.readonly_request, tmp_path, lambda: None, [0])
    assert not s.effects and not (tmp_path / 'native-intent.json').exists()


class OccupiedBackend(FileBackend):
    def observe_condition(self, condition, *, stop):
        observed = super().observe_condition(condition, stop=stop)
        observed['controls'][-1].update(condition['selector'])
        return observed

    def act(self, window, control, action, value, before_sha, operation, *, stop):
        if action == 'open_readonly':
            self.calls.append(action)
            self.opened = True
            self.view.observation.update(dialogs=[], controls=[])
            (operation / 'receipt.json').write_text(json.dumps({'action': action}))
            if action == self.stop_after:
                self.cancelled = True
            return ({'ok': False, 'state': 'unknown', 'effects': 'unknown'} if self.unknown else
                    {'ok': True, 'state': 'verified', 'effects': 'verified'})
        result = super().act(window, control, action, value, before_sha, operation, stop=stop)
        if action == 'open_local_file':
            self.opened = False
            occupied_view(self.view)
        return result


def test_occupied_pause_resume_only_clicks_readonly_then_never_repeats_it(tmp_path, file_view):
    root = file_task(tmp_path, file_view, actions.READONLY_SELECTOR)
    backend = OccupiedBackend(file_view)
    backend.stop_after = 'open_local_file'
    first = task.run(root, backend, stop=lambda: backend.cancelled)
    assert first['state'] == 'paused' and first['completed'] == []
    assert backend.calls == ['open_file_dialog', 'set_file_path', 'open_local_file']
    evidence = {p: p.read_bytes() for p in (root / 'attempts' / '0001').rglob('*') if p.is_file()}
    backend.cancelled, backend.stop_after = False, 'open_readonly'
    second = task.run(root, backend, stop=lambda: backend.cancelled)
    assert second['state'] == 'paused' and second['completed'] == []
    assert backend.calls[-1] == 'open_readonly'
    backend.cancelled, backend.stop_after = False, None
    assert task.run(root, backend)['ok']
    assert backend.calls == ['open_file_dialog', 'set_file_path', 'open_local_file', 'open_readonly']
    assert all(p.read_bytes() == content for p, content in evidence.items())


def test_unknown_readonly_effect_blocks_any_resume(tmp_path, file_view):
    root = file_task(tmp_path, file_view, actions.READONLY_SELECTOR)
    backend = OccupiedBackend(file_view)
    backend.stop_after = 'open_local_file'
    assert task.run(root, backend, stop=lambda: backend.cancelled)['state'] == 'paused'
    backend.cancelled, backend.stop_after, backend.unknown = False, None, True
    assert task.run(root, backend)['state'] == 'unknown'
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(root, backend)
    assert backend.calls.count('open_readonly') == 1
