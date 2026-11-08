"""Offline Office planning handoff and stale-result guards; no native edits."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

import pytest

from agent_service import office_intent
from agent_service.computer import ComputerSession
from agent_service.computer_goal_panel import attach_goal_tab, MODES
from test_office_edit import word_fixture
from test_office_intent import Client, valid_response


@pytest.fixture(scope='module')
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()
    import gc
    gc.collect()


@pytest.fixture
def panel(hidden_root):
    root = hidden_root
    tabs = ttk.Notebook(root)
    value = attach_goal_tab(tabs, root)
    yield value
    value.close()
    tabs.destroy()


@pytest.mark.parametrize('kind', ['edits', 'clarify', 'unsupported'])
def test_durable_request_and_explicit_outcome(tmp_path, kind):
    source = tmp_path / 'input.docx'
    word_fixture(source)
    session = ComputerSession.create(tmp_path / 'tasks')
    client = Client(valid_response if kind == 'edits' else
                    lambda _: {'kind': kind, 'reason': '请补充任务。'})
    result = office_intent.propose(session, '确认预算', [source], client=client)
    record = Path(result['office_result']['record'])
    assert record.is_absolute() and record.parent == session.root / 'office-planning'
    assert result['office_result']['kind'] == kind
    assert json.loads((record / 'request.json').read_text(encoding='utf-8')) == {
        'instruction': '确认预算', 'files': [str(source.absolute())],
        'artifact_refs': None, 'scope': None}
    assert json.loads((record / 'outcome.json').read_text(encoding='utf-8')) == result
    assert '_office_guard' not in result


def launch(panel, monkeypatch, *, with_hook=True, kind='clarify'):
    panel.mode.set(MODES[9])
    panel.mode_changed(refresh_sources=False)
    panel.office_planning_files = ('C:/fixture.docx',)
    panel.text.insert('1.0', '原任务')
    panel.text.edit_modified(False)
    panel.stopped.set()
    state = {'mode': MODES[9], 'scope': [{'cell': 'B2'}], 'revision': 1,
             'source': 'original', 'stopped': False}
    captured, callbacks = {}, []
    if with_hook:
        def context():
            assert panel.plan is None
            assert not panel.stopped.is_set()
            return state
        panel.office_planning_context = context
        panel.on_office_planning_result = lambda *args: callbacks.append(args)
    monkeypatch.setattr(office_intent, 'propose', lambda *a, **kw: {
        'plan': None, 'reason': '请明确哪个位置',
        'office_result': {'kind': kind, 'record': 'local'},
        '_office_guard': 'untrusted'})
    def start(kind, operation, ticket):
        captured.update(kind=kind, operation=operation, ticket=ticket)
    monkeypatch.setattr(panel, 'start_instruction', start)
    panel.propose()
    result = captured['operation'](None)
    assert result['_office_guard'] == (state if with_hook else None)
    assert result['_office_guard'] is not state
    return state, captured, result, callbacks


def deliver(panel, captured, result):
    panel.messages.put(('propose', result, None, captured['ticket']))
    panel.poll()


@pytest.mark.parametrize('kind', ['clarify', 'unsupported', 'edits'])
def test_current_explicit_result_callback(panel, monkeypatch, kind):
    state, captured, result, callbacks = launch(panel, monkeypatch, kind=kind)
    deliver(panel, captured, result)
    assert len(callbacks) == 1
    assert callbacks[0] == (result, captured['ticket'], state)
    assert panel.plan is None and panel.text.get('1.0', 'end-1c') == '原任务'
    assert callbacks[0][0]['office_result']['kind'] == kind


@pytest.mark.parametrize('field', ['mode', 'scope', 'source', 'revision'])
def test_changed_complete_context_rejects_late_result(panel, monkeypatch, field):
    state, captured, result, callbacks = launch(panel, monkeypatch)
    state[field] = 'changed'
    deliver(panel, captured, result)
    assert not callbacks and panel.plan is None
    assert '迟到规划' in panel.status.get()
    assert panel.text.get('1.0', 'end-1c') == '原任务'


def test_context_provider_error_rejects_result(panel, monkeypatch):
    _, captured, result, callbacks = launch(panel, monkeypatch)
    panel.office_planning_context = lambda: (_ for _ in ()).throw(ValueError('changed'))
    deliver(panel, captured, result)
    assert not callbacks and '迟到规划' in panel.status.get()


def test_changed_draft_rejects_result_after_context_matches(panel, monkeypatch):
    _, captured, result, callbacks = launch(panel, monkeypatch)
    panel.instruction_draft.adopt('新任务')
    deliver(panel, captured, result)
    assert not callbacks and '迟到规划' in panel.status.get()
    assert panel.text.get('1.0', 'end-1c') == '新任务'


def test_draft_changed_back_still_rejects_result(panel, monkeypatch):
    _, captured, result, callbacks = launch(panel, monkeypatch)
    panel.instruction_draft.adopt('新任务')
    panel.instruction_draft.adopt('原任务')
    deliver(panel, captured, result)
    assert not callbacks and '迟到规划' in panel.status.get()


def test_stop_rejects_queued_result(panel, monkeypatch):
    _, captured, result, callbacks = launch(panel, monkeypatch)
    panel.stopped.set()
    deliver(panel, captured, result)
    assert not callbacks and panel.plan is None


def test_legacy_no_hooks(panel, monkeypatch):
    _, captured, result, callbacks = launch(panel, monkeypatch, with_hook=False)
    deliver(panel, captured, result)
    assert not callbacks and panel.status.get() == '请明确哪个位置'


def test_error_never_calls_clarification_hook(panel, monkeypatch):
    _, _, _, callbacks = launch(panel, monkeypatch)
    panel.messages.put(('propose', None, 'office_planning_failed'))
    panel.poll()
    assert not callbacks and '未完成' in panel.status.get()
