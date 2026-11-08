"""Target construction and real Tk approval state; no native app or API calls."""
from copy import deepcopy
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_panel as wp
from agent_service import windows_task as wt


WINDOW = {'hwnd': 100, 'pid': 200, 'created': 'start', 'image': 'C:/Windows/explorer.exe',
          'class_name': 'CabinetWClass', 'title': 'Synthetic folder', 'profile': 'explorer'}
WORD_WINDOW = {**WINDOW, 'image': 'C:/wps.exe', 'class_name': 'OpusApp', 'profile': 'observe_only'}


def observation():
    return {'window': deepcopy(WINDOW), 'truncated': False, 'controls': [
        {'id': 'folder', 'type': 'TreeItem', 'name': 'Synthetic child', 'automation_id': '',
         'enabled': True, 'visible': True, 'selected': False, 'expanded': 0, 'focused': False}]}


@pytest.fixture(scope='module')
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(hidden_root):
    notebook = ttk.Notebook(hidden_root)
    changes = []
    panel = wp.attach_windows_tab(notebook, hidden_root, on_busy=changes.append)
    yield panel, changes
    panel.close()
    notebook.destroy()


def test_condition_uses_semantic_target_and_does_not_retain_focus():
    target = wp.condition_from_control(observation(), 'folder', 'focused', retain=True)
    assert not target['retain']
    assert target['selector'] == {'type': 'TreeItem', 'name': 'Synthetic child', 'automation_id': ''}
    assert 'id' not in target['selector']


def test_ambiguous_selector_needs_distinct_ancestor():
    observed = observation()
    first = observed['controls'][0]
    observed['controls'].append({**first, 'id': 'other'})
    with pytest.raises(ComputerError, match='windows_target_ambiguous'):
        wp.condition_from_control(observed, 'folder', 'selected')
    first['ancestors'] = [{'type': 'Tree', 'name': 'Navigation', 'automation_id': ''}]
    assert wp.condition_from_control(observed, 'folder', 'selected')['selector']['ancestor']['name'] == 'Navigation'


def test_programmatic_or_pasted_instruction_change_invalidates_approval(panel, tmp_path):
    p, _ = panel
    p.task_root = tmp_path
    p.execute_button.configure(state='normal')
    p.resume_button.configure(state='normal')
    p.instruction_var.set('A changed user draft')
    assert p.task_root is None
    assert str(p.execute_button.cget('state')) == 'disabled'
    assert str(p.resume_button.cget('state')) == 'disabled'


def test_external_busy_does_not_construct_backend_or_start_thread(panel):
    p, changes = panel
    p.external_busy = lambda: True
    p.start('windows', lambda _: pytest.fail('must not run'))
    assert not p.running and p.backend is None and not changes


def test_unknown_outcome_disables_resume(panel):
    p, _ = panel
    p.show_outcome({'state': 'unknown', 'completed': [], 'resumable': False})
    assert str(p.resume_button.cget('state')) == 'disabled'
    assert '不提供自动重放' in p.status.get()


@pytest.mark.parametrize('state,enabled',[('paused',True),('unknown',False)])
def test_manual_attention_explains_handoff_without_unlocking_unknown(panel,state,enabled):
    p,_=panel
    p.show_outcome({'state':state,'completed':[],'resumable':True,
                   'manual_attention':{'condition_id':'target_1','after_action':state=='unknown'}})
    assert (str(p.resume_button.cget('state'))=='normal') is enabled
    assert '已停止自动观察' in p.status.get()
    if enabled: assert '已处理弹窗' in p.resume_button.cget('text')
    else: assert '不提供自动重放' in p.status.get()


def test_thread_start_failure_releases_shared_busy(panel, monkeypatch):
    p, changes = panel
    class NoThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError('synthetic')
    monkeypatch.setattr(wp.threading, 'Thread', NoThread)
    p.start('windows', lambda _: None)
    assert changes == [True, False] and not p.running
    assert str(p.window.cget('state')) == 'readonly'


def test_restored_goal_remains_bound_after_instruction_population(panel, tmp_path):
    p, changes = panel
    target = wp.condition_from_control(observation(), 'folder', 'selected')
    goal = {'schema': 1, 'instruction': 'Saved instruction', 'conditions': [target], 'max_steps': 12}
    root = wt.create(tmp_path, goal)
    p.messages.put(('restore', {'root': root, 'goal': goal, 'summary': wt.inspect(root)}, None))
    p.poll()
    assert p.task_root == root and p.instruction.get() == 'Saved instruction'
    assert str(p.resume_button.cget('state')) == 'normal'
    assert changes == [False]


def test_no_preview_cannot_execute(panel):
    p, changes = panel
    p.execute()
    assert not p.running and not changes


def test_cancel_open_picker_keeps_existing_goal_and_draft(panel, monkeypatch):
    from tkinter import filedialog
    from agent_service import windows_access as access
    p, _ = panel
    p.windows = [deepcopy(WORD_WINDOW)]
    p.window.configure(values=['approved WPS'])
    p.window.current(0)
    p.instruction_var.set('Keep new user draft')
    p.conditions = [{'existing': True}]
    monkeypatch.setattr(access, '_kind', lambda window: 'wps')
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **kw: '')
    p.add_open()
    assert p.conditions == [{'existing': True}]
    assert p.instruction.get() == 'Keep new user draft'


def test_open_picker_requires_selected_wps_and_preserves_explicit_file_token(panel, monkeypatch):
    from tkinter import filedialog
    from agent_service import windows_access as access, windows_file_dialog as files
    p, _ = panel
    chosen = []
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **kw: chosen.append(kw) or 'explicit.docx')
    p.add_open()
    assert not chosen
    p.windows = [deepcopy(WORD_WINDOW)]
    p.window.configure(values=['approved WPS'])
    p.window.current(0)
    monkeypatch.setattr(access, '_kind', lambda window: 'wps')
    import json
    token = json.dumps({'path': 'explicit.docx', 'sha256': 'a' * 64, 'size': 10})
    pinned = []
    monkeypatch.setattr(files, 'encode_token', lambda path: pinned.append(path) or token)
    p.add_open()
    assert pinned == ['explicit.docx'] and len(chosen) == 1
    assert len(p.conditions) == 1 and p.conditions[0]['value'] == token
    assert p.conditions[0]['selector'] == files.OPEN_DOCUMENT_SELECTOR
    assert p.task_root is None and str(p.execute_button.cget('state')) == 'disabled'
    assert 'explicit.docx' in p.targets.get(0) and 'sha256' not in p.targets.get(0)


def test_wps_find_target_can_be_added_before_dialog_exists():
    window = {**WINDOW, 'profile': 'observe_only', 'class_name': 'OpusApp', 'image': 'C:/WPS/wps.exe'}
    condition = wp.condition_for_find(window, 'Synthetic search')
    assert condition['selector'] == {'type': 'ComboBox', 'name': '查找内容(N)', 'automation_id': ''}
    assert condition['state'] == 'value' and condition['value'] == 'Synthetic search'
    assert condition['retain'] is True
    wt.validate_goal({'schema': 1, 'instruction': 'Fill find field', 'conditions': [condition], 'max_steps': 2})


@pytest.mark.parametrize('text', ['', 'line\nbreak', 'x' * 301])
def test_wps_find_target_rejects_nonliteral_or_unbounded_text(text):
    window = {**WINDOW, 'profile': 'observe_only', 'class_name': 'OpusApp'}
    with pytest.raises(ComputerError, match='windows_find_target_invalid'):
        wp.condition_for_find(window, text)


def test_cloud_proposal_requires_current_explicit_complete_observation(panel):
    p, _ = panel
    p.start = lambda *args: pytest.fail('must not start cloud call')
    p.propose()
    p.observation = {**observation(), 'blocked_reason': 'dialog'}
    p.approved_observations = [observation()]
    p.propose()


def test_cloud_button_forwards_only_explicitly_observed_local_context(panel, monkeypatch):
    from agent_service import windows_visual_proposal, model
    p, _ = panel
    observed = {**observation(), 'scope': 'visible_controls', 'application': 'explorer'}
    p.observation = observed
    p.approved_observations = [observed]
    captured = []
    fake_client = object()
    monkeypatch.setattr(model, 'CloudClient', lambda **options: fake_client)
    def generate(instruction, observations, backend, client, *, stop, task_base):
        assert client is fake_client and not stop()
        captured.append((instruction, observations))
        observations[0]['controls'].clear()
        return {'goal':{'conditions': []},'root':None,'visual':False}
    monkeypatch.setattr(windows_visual_proposal, 'propose', generate)
    p.start = lambda kind, operation: operation(None)
    p.propose()
    assert captured[0][0] == p.instruction.get()
    assert p.approved_observations[0]['controls']


def test_stop_discards_late_cloud_proposal(panel):
    p, _ = panel
    p.stop()
    p.messages.put(('propose', {'conditions': [{'unexpected': 'late'}]}, None))
    p.poll()
    assert p.conditions == [] and p.task_root is None
    assert str(p.execute_button.cget('state')) == 'disabled'


def logic_panel(conditions=()):
    """Call panel methods without constructing Tk or touching the desktop."""
    state = SimpleNamespace(running=False, external_busy=lambda: False,
        windows=[deepcopy(WORD_WINDOW)], window=SimpleNamespace(current=lambda: 0),
        conditions=deepcopy(list(conditions)), root=object(), events=[], task_root='approved-task')
    state.status = SimpleNamespace(set=lambda value: state.events.append(('status', value)))
    state.instruction = SimpleNamespace(get=lambda: 'Save a new copy and close it')
    state.invalidate = lambda: state.events.append(('invalidate', None))
    state.show_targets = lambda: state.events.append(('targets', None))
    return state


def test_copy_logic_conditions_bind_separate_open_and_copy_tokens(monkeypatch, tmp_path):
    from agent_service import windows_access as access, windows_document_copy as copies
    from agent_service import windows_document_lifecycle as lifecycle, windows_file_dialog as files
    import json
    source = str(tmp_path / 'approved.docx')
    open_token = json.dumps({'path': source, 'sha256': 'a' * 64, 'size': 10})
    copy_token = json.dumps({'directory': str(tmp_path / 'exclusive-copy')})
    prepared = []
    monkeypatch.setattr(access, '_kind', lambda window: 'wps')
    monkeypatch.setattr(files, 'encode_token', lambda path: open_token)
    monkeypatch.setattr(copies, 'prepare', lambda path, **kwargs:
        prepared.append((path, kwargs)) or copy_token)
    original = deepcopy(WORD_WINDOW)
    conditions = wp.conditions_for_copy(original, source, number=4, parent=tmp_path)
    assert [c['id'] for c in conditions] == ['target_4', 'target_5', 'target_6']
    assert [c['selector'] for c in conditions] == [files.OPEN_DOCUMENT_SELECTOR,
        lifecycle.SAVE_SELECTOR, lifecycle.CLOSE_SELECTOR]
    assert [c['retain'] for c in conditions] == [False, True, True]
    assert [c['value'] for c in conditions] == [open_token, copy_token, copy_token]
    assert prepared == [(source, {'parent': tmp_path})]
    original['title'] = 'changed outside'
    conditions[0]['window']['title'] = 'changed one condition'
    assert conditions[1]['window'] == conditions[2]['window'] == WORD_WINDOW
    conditions[1]['selector']['name'] = 'mutated copy'
    assert lifecycle.SAVE_SELECTOR['name'] != 'mutated copy'


def test_copy_logic_cancel_picker_keeps_current_task_and_draft(monkeypatch):
    from tkinter import filedialog
    state = logic_panel([{'existing': 'draft'}])
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **kwargs: '')
    monkeypatch.setattr(wp, 'conditions_for_copy', lambda *args, **kwargs: pytest.fail('cancelled selection'))
    wp.WindowsPanel.add_copy(state)
    assert state.conditions == [{'existing': 'draft'}] and state.task_root == 'approved-task'
    assert state.events == []


def test_copy_logic_releases_only_matching_prior_open_retention(monkeypatch, tmp_path):
    from tkinter import filedialog
    from agent_service import windows_access as access, windows_document_copy as copies
    from agent_service import windows_file_actions as file_actions, windows_file_dialog as files
    import json
    source = str(tmp_path / 'approved.docx')
    token = json.dumps({'path': source, 'sha256': 'a' * 64, 'size': 10})
    prior = dict(id='target_1', window=deepcopy(WORD_WINDOW), selector=deepcopy(file_actions.READONLY_SELECTOR),
                 state='value', value=token, retain=True)
    other_path = {**deepcopy(prior), 'id': 'target_2',
                  'value': json.dumps({'path': str(tmp_path / 'other.docx'), 'sha256': 'b' * 64, 'size': 10})}
    other_window = {**deepcopy(prior), 'id': 'target_3', 'window': {**WORD_WINDOW, 'hwnd': 999}}
    state = logic_panel([prior, other_path, other_window])
    monkeypatch.setattr(filedialog, 'askopenfilename', lambda **kwargs: source)
    monkeypatch.setattr(access, '_kind', lambda window: 'wps')
    monkeypatch.setattr(files, 'encode_token', lambda path: token)
    monkeypatch.setattr(copies, 'prepare', lambda path: json.dumps({'directory': str(tmp_path / 'exclusive')}))
    wp.WindowsPanel.add_copy(state)
    assert len(state.conditions) == 6
    assert [c['retain'] for c in state.conditions] == [False, True, True, False, True, True]
    assert prior['retain'] is True
    assert [kind for kind, _ in state.events] == ['invalidate', 'targets', 'status']


@pytest.mark.parametrize('case', ['no_target', 'no_receipt', 'invalid_receipt', 'valid_receipt', 'open_failure', 'busy'])
def test_copy_logic_open_button_requires_verified_receipt(monkeypatch, tmp_path, case):
    from agent_service import windows_document_copy as copies, windows_document_lifecycle as lifecycle
    from agent_service import word_open
    target = {'selector': deepcopy(lifecycle.SAVE_SELECTOR), 'value': 'last-copy-token'}
    state = logic_panel([] if case == 'no_target' else [target])
    state.running = case == 'busy'
    checked, opened = [], []
    def receipt(token):
        checked.append(token)
        if case == 'invalid_receipt':
            raise ComputerError('windows_copy_receipt_invalid')
        return {'final': {'path': str(tmp_path / 'verified-final.docx')}} if case in {'valid_receipt', 'open_failure'} else None
    monkeypatch.setattr(copies, 'saved_receipt', receipt)
    def open_word(path):
        opened.append(path)
        if case == 'open_failure':
            raise word_open.WordOpenError('registration unavailable')
    monkeypatch.setattr(word_open, 'request_open_word', open_word)
    wp.WindowsPanel.open_copy(state)
    assert opened == ([str(tmp_path / 'verified-final.docx')] if case in {'valid_receipt', 'open_failure'} else [])
    assert checked == ([] if case in {'no_target', 'busy'} else ['last-copy-token'])
    if case == 'open_failure':
        assert '副本已保留，打开失败' in state.events[-1][1]
        assert state.conditions == [target]
