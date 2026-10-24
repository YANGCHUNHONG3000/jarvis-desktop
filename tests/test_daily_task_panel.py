"""GUI wiring for bounded natural language task routing."""

from types import SimpleNamespace

import pytest
import tkinter as tk
from tkinter import ttk

from agent_service import computer_goal_panel as ui


@pytest.fixture(scope="module")
def root():
    window = tk.Tk()
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def panel(root):
    tabs = ttk.Notebook(root)
    result = ui.attach_goal_tab(tabs, root)
    yield result
    result.close()
    tabs.destroy()


def _ready(panel, refs, choice):
    panel.mode.set(ui.MODES[11])
    panel.capability_sources = refs
    panel.capability_source_generation += 1
    panel.capability_sources_validated_generation = panel.capability_source_generation
    panel.control.configure(values=['自动按任务选择输入', '无来源文件'] +
                            [f"{i}. {ref['artifact']}" for i, ref in enumerate(refs, 1)])
    panel.control.current(choice)
    panel.text.insert('1.0', '按任务生成报告')
    panel.text.edit_modified(False)


@pytest.mark.parametrize('choice,expected', [
    (0, [{'artifact': 'one.docx'}, {'artifact': 'two.xlsx'}]),
    (2, [{'artifact': 'one.docx'}]),
    (1, []),
])
def test_route_button_defers_call_and_passes_selected_refs(panel, monkeypatch, choice, expected):
    from agent_service import daily_task_routing

    refs = [{'artifact': 'one.docx'}, {'artifact': 'two.xlsx'}]
    _ready(panel, refs, choice)
    pending, calls = [], []
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda session, instruction, source_refs, **kwargs:
                        calls.append((session, instruction, source_refs, kwargs)) or
                        {'state': 'blocked', 'reason': 'done', 'plan': None, 'routing': 'none'})
    monkeypatch.setattr(panel, 'start', lambda kind, operation: pending.append((kind, operation)))

    panel.propose_daily_task()

    assert calls == []
    assert pending[0][0] == 'daily_task_route'
    pending[0][1]('session')
    assert calls[0][0:3] == ('session', '按任务生成报告', expected)


def test_ready_route_displays_existing_plan_and_uses_existing_execute_path(panel, monkeypatch):
    from agent_service import daily_task_routing, capability_router

    refs = [{'artifact': 'one.docx'}]
    _ready(panel, refs, 0)
    plan = {'mode': 'capability', 'sha256': 'a' * 64, 'instruction': 'approved plan',
            'source_ref': None, 'candidates': []}
    pending, executions = [], []
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda *_args, **_kwargs: {'state': 'ready', 'reason': '',
                                                   'plan': plan, 'routing': 'capability'})
    monkeypatch.setattr(panel, 'start', lambda kind, operation: pending.append((kind, operation)))
    panel.propose_daily_task()
    envelope = pending[0][1]('session')
    panel.set_busy(False)
    panel.messages.put(('daily_task_route', envelope, None))
    panel.poll()

    assert panel.plan is plan
    assert str(panel.execute_button['state']) == 'normal'
    monkeypatch.setattr(capability_router, 'execute',
                        lambda session, selected, **kwargs:
                        executions.append((session, selected, kwargs)) or {'ok': True})
    monkeypatch.setattr(ui, 'input_epoch', lambda: 1)
    panel.start = lambda kind, operation: executions.append((kind, operation))
    panel.execute()
    assert executions[0][0] == 'capability_execute'


def test_stale_control_source_and_stop_results_are_discarded(panel, monkeypatch):
    from agent_service import daily_task_routing

    refs = [{'artifact': 'one.docx'}, {'artifact': 'two.xlsx'}]
    _ready(panel, refs, 0)
    pending = []
    plan = {'mode': 'capability', 'sha256': 'b' * 64}
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda *_args, **_kwargs: {'state': 'ready', 'plan': plan,
                                                   'reason': '', 'routing': 'capability'})
    monkeypatch.setattr(panel, 'start', lambda kind, operation: pending.append((kind, operation)))

    panel.propose_daily_task()
    late = pending.pop()[1]('session')
    panel.control.current(2)
    panel.messages.put(('daily_task_route', late, None))
    panel.poll()
    assert panel.plan is None

    panel.propose_daily_task()
    late = pending.pop()[1]('session')
    panel.text.delete('1.0', 'end')
    panel.text.insert('1.0', '改变后的任务')
    panel.text.edit_modified(False)
    panel.messages.put(('daily_task_route', late, None))
    panel.poll()
    assert panel.plan is None

    panel.control.current(0)
    panel.capability_source_generation += 1
    panel.capability_sources_validated_generation = panel.capability_source_generation
    panel.propose_daily_task()
    late = pending.pop()[1]('session')
    panel.capability_source_generation += 1
    panel.capability_sources_validated_generation = panel.capability_source_generation
    panel.messages.put(('daily_task_route', late, None))
    panel.poll()
    assert panel.plan is None

    panel.propose_daily_task()
    late = pending.pop()[1]('session')
    panel.stop()
    panel.messages.put(('daily_task_route', late, None))
    panel.poll()
    assert panel.plan is None


def test_requires_instruction_validated_sources_and_rejects_busy(panel, monkeypatch):
    from agent_service import daily_task_routing

    calls, starts = [], []
    monkeypatch.setattr(daily_task_routing, 'propose', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(panel, 'start', lambda *args: starts.append(args))
    panel.mode.set(ui.MODES[11])
    panel.propose_daily_task()
    assert starts == []

    _ready(panel, [{'artifact': 'one.docx'}], 0)
    panel.capability_sources_validated_generation = None
    panel.propose_daily_task()
    assert starts == []
    panel.capability_sources_validated_generation = panel.capability_source_generation
    panel.running = True
    panel.propose_daily_task()
    assert starts == []
    panel.running = False
    panel.external_busy = lambda: True
    panel.propose_daily_task()
    assert starts == []
    assert calls == []


def test_history_binding_change_discards_late_route(panel, monkeypatch):
    from agent_service import daily_task_routing

    _ready(panel, [{'artifact': 'history.docx'}], 0)
    current = [True]
    panel.capability_history_seed = {
        'task_id': 'task-history', 'records': [], 'is_current': lambda: current[0]}
    pending = []
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda *_args, **_kwargs: {'state': 'blocked', 'reason': 'late',
                                                   'plan': None, 'routing': 'none'})
    monkeypatch.setattr(panel, 'start', lambda kind, operation: pending.append((kind, operation)))
    panel.propose_daily_task()
    late = pending[0][1]('session')
    current[0] = False
    panel.messages.put(('daily_task_route', late, None))
    panel.poll()
    assert panel.plan is None
    assert 'late' not in panel.status.get()


def test_needs_input_and_blocked_never_enable_execution(panel, monkeypatch):
    from agent_service import daily_task_routing

    _ready(panel, [], 1)
    pending = []
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda *_args, **_kwargs: {'state': 'needs_input', 'reason': '请明确来源',
                                                   'plan': None, 'routing': 'clarify'})
    monkeypatch.setattr(panel, 'start', lambda kind, operation: pending.append((kind, operation)))
    panel.propose_daily_task()
    envelope = pending[0][1]('session')
    panel.set_busy(False)
    panel.messages.put(('daily_task_route', envelope, None))
    panel.poll()

    assert panel.plan is None
    assert str(panel.execute_button['state']) == 'disabled'
    assert '请明确来源' in panel.status.get()


def test_route_button_tracks_capability_mode_and_busy(panel):
    panel.refresh_capability_sources = lambda: None
    panel.mode.set(ui.MODES[11])
    panel.mode_changed()
    assert str(panel.daily_task_route_button['state']) == 'normal'
    panel.set_busy(True)
    assert str(panel.daily_task_route_button['state']) == 'disabled'
    panel.set_busy(False)
    panel.mode.set(ui.MODES[0])
    panel.mode_changed()
    assert str(panel.daily_task_route_button['state']) == 'disabled'
