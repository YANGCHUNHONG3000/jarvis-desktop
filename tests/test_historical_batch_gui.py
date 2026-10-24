"""Explicit historical batch entry into capability mode, without execution."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tkinter as tk
from tkinter import ttk

import test_history_resume_gui as history_gui
from agent_service import computer_goal_panel as goal_ui
from test_history_resume_gui import _Thread, _button, _pump


@pytest.fixture
def desktop(monkeypatch, tmp_path, gui_compat):
    result = history_gui.desktop.__wrapped__(monkeypatch, tmp_path)
    panel = _goal_panel()
    panel.calls = []
    panel.use_history_artifact_batch = lambda task_id, records, is_current: \
        panel.calls.append((task_id, records, is_current)) or True
    return result


@pytest.fixture
def gui_compat(monkeypatch):
    from agent_service import computer_panel
    monkeypatch.setattr(computer_panel, "attach_computer_tab",
                        lambda tabs, _root, **_kwargs: SimpleNamespace(
                            tab=history_gui._Widget(), stop=lambda: None, close=lambda: None))
    from agent_service import computer_goal_panel

    class GoalPanelStub:
        def __init__(self):
            self.tab = history_gui._Widget()
            self.running = False
            self.on_artifacts = None
            self.calls = []

        def use_history_artifact_batch(self, task_id, records, is_current):
            self.calls.append((task_id, records, is_current))

        def use_generated_artifacts(self, records):
            self.last_artifacts = records

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(computer_goal_panel, "attach_goal_tab",
                        lambda *_args, **_kwargs: GoalPanelStub())
    monkeypatch.setattr(history_gui._Widget, "set",
                        lambda self, value: self.options.__setitem__("value", value), raising=False)
    monkeypatch.setattr(history_gui._Widget, "current", lambda self, *_: 0, raising=False)


def _function_state(callback):
    return dict(zip(callback.__code__.co_freevars,
                    (cell.cell_contents for cell in callback.__closure__ or ())))


def _goal_panel():
    callback = _button("整批用于能力模式").options["command"]
    selection = _function_state(callback)["select_history_artifact_batch"]
    return _function_state(selection)["goal_panel"]


def _history_batch(root, task_root, state, tree, count=2, duplicate_labels=False):
    workflow = task_root / ("workflow-" + "c" * 32)
    artifacts = []
    for i in range(count):
        ext = ".docx" if i % 2 == 0 else ".xlsx"
        body = f"generated artifact {i}".encode()
        path = workflow / f"step-{i}" / f"artifact-{i}{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        artifacts.append({"label": "duplicate output" if duplicate_labels else f"output {i}", "path": str(path),
                          "sha256": hashlib.sha256(body).hexdigest()})
    state["history_artifacts"] = artifacts
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    return artifacts


def test_selected_historical_batch_passes_all_office_artifacts(gui_compat, desktop):
    root, task_root, state, tree, _original = desktop
    artifacts = _history_batch(root, task_root, state, tree, count=4, duplicate_labels=True)
    panel = _goal_panel()
    button = _button("整批用于能力模式")

    assert button.options["state"] == "normal"
    button.invoke()

    task_id, records, is_current = panel.calls[0]
    selection = _function_state(button.options["command"])["select_history_artifact_batch"]
    assert task_id == _function_state(selection)["history_selected"]["id"]
    assert records == [{"path": item["path"], "sha256": item["sha256"]} for item in artifacts]
    assert is_current()


def test_historical_batch_over_four_is_rejected_without_truncation(gui_compat, desktop):
    root, task_root, state, tree, _original = desktop
    _history_batch(root, task_root, state, tree, count=5)
    panel = _goal_panel()
    button = _button("整批用于能力模式")

    assert button.options["state"] == "disabled"
    button.invoke()
    assert panel.calls == []


def test_history_reselection_invalidates_captured_batch_binding(gui_compat, desktop):
    root, task_root, state, tree, _original = desktop
    _history_batch(root, task_root, state, tree)
    panel = _goal_panel()
    button = _button("整批用于能力模式")
    button.invoke()
    _, _, is_current = panel.calls[0]

    tree.select(tree.selection()[0])

    assert not is_current()


def test_history_batch_entry_guards_busy_and_uninspected_selection(gui_compat, desktop):
    root, task_root, state, tree, _original = desktop
    _history_batch(root, task_root, state, tree)
    panel = _goal_panel()
    button = _button("整批用于能力模式")
    panel.running = True
    button.invoke()
    panel.running = False
    assert panel.calls == []


@pytest.fixture(scope="module")
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def goal_panel(hidden_root):
    tabs = ttk.Notebook(hidden_root)
    panel = goal_ui.attach_goal_tab(tabs, hidden_root)
    yield panel
    panel.close()
    tabs.destroy()


def _begin_history_seed(panel, monkeypatch, count=2):
    from agent_service import office_handoff

    records = [{'path': f'history-{i}.{"docx" if i % 2 == 0 else "xlsx"}',
                'sha256': f'{i + 1:064x}'} for i in range(count)]
    refs = [{'artifact': item['path'], 'sha256': item['sha256']} for item in records]
    calls = []
    pending = []
    monkeypatch.setattr(office_handoff, 'select_outputs',
                        lambda value: calls.append(value) or refs)
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    current = [True]
    assert panel.use_history_artifact_batch('task-history-1', records, lambda: current[0])
    return records, refs, calls, pending, current


def test_goal_panel_rechecks_and_keeps_complete_history_batch(goal_panel, monkeypatch):
    panel = goal_panel
    records, refs, calls, pending, _current = _begin_history_seed(panel, monkeypatch, 4)

    assert len(pending) == 1 and pending[0][0] == 'capability_sources'
    selected = pending[0][1](object())
    assert calls == [records]
    assert selected['refs'] == refs and selected['task_id'] == 'task-history-1'
    panel.messages.put(('capability_sources', selected, None))
    panel.poll()

    assert panel.capability_sources == refs
    assert panel.capability_history_seed['records'] == records
    assert 'task-history-1' in panel.capability_source_label.get()
    assert str(panel.office_bundle_button['state']) == 'normal'


def test_goal_panel_rejects_over_limit_without_truncating(goal_panel, monkeypatch):
    from agent_service import office_handoff

    monkeypatch.setattr(office_handoff, 'select_outputs',
                        lambda *_: pytest.fail('over-limit batch reached revalidation'))
    assert not goal_panel.use_history_artifact_batch(
        'task-history-2', [{'path': f'{i}.docx', 'sha256': 'a' * 64} for i in range(5)],
        lambda: True)
    assert goal_panel.capability_history_seed is None


def test_goal_panel_discards_late_history_source_after_mode_change(goal_panel, monkeypatch):
    panel = goal_panel
    _records, _refs, _calls, pending, _current = _begin_history_seed(panel, monkeypatch)
    late = pending[0][1](object())
    panel.mode.set(goal_ui.MODES[9])
    panel.mode_changed()
    panel.messages.put(('capability_sources', late, None))
    panel.poll()

    assert panel.capability_sources == []
    assert panel.plan is None
    assert str(panel.office_bundle_button['state']) == 'disabled'


def test_goal_panel_revalidates_retained_seed_on_mode_reentry(goal_panel, monkeypatch):
    panel = goal_panel
    records, _refs, calls, pending, _current = _begin_history_seed(panel, monkeypatch)
    first = pending.pop()[1](object())
    panel.messages.put(('capability_sources', first, None))
    panel.poll()
    panel.mode.set(goal_ui.MODES[9])
    panel.mode_changed()
    panel.mode.set(goal_ui.MODES[11])
    panel.mode_changed()

    assert pending[-1][0] == 'capability_sources'
    refreshed = pending[-1][1](object())
    assert calls == [records, records]
    panel.messages.put(('capability_sources', refreshed, None))
    panel.poll()
    assert panel.capability_history_seed['records'] == records
    assert panel.capability_sources == refreshed['refs']


def test_goal_panel_explicit_clear_restores_previous_task_source(goal_panel, monkeypatch):
    panel = goal_panel
    records, _refs, _calls, pending, _current = _begin_history_seed(panel, monkeypatch)
    panel.get_artifacts = lambda: [{'path': 'previous.docx', 'sha256': 'b' * 64}]
    from agent_service import office_handoff
    previous_ref = {'artifact': 'previous.docx', 'sha256': 'b' * 64}
    monkeypatch.setattr(office_handoff, 'select_outputs', lambda value: [previous_ref])
    # Complete the historical validation first, then explicitly switch sources.
    first = pending.pop()[1](object())
    panel.messages.put(('capability_sources', first, None))
    panel.poll()
    panel.use_previous_task_artifacts()

    assert panel.capability_history_seed is None
    result = pending[-1][1](object())
    assert result['task_id'] is None
    panel.messages.put(('capability_sources', result, None))
    panel.poll()
    assert panel.capability_sources == [previous_ref]
    assert '上一任务成果' in panel.capability_source_label.get()


def _validate_seed_sources(panel, monkeypatch, seed_records):
    from agent_service import office_handoff

    refs = [{'artifact': item['path'], 'sha256': item['sha256']} for item in seed_records]
    monkeypatch.setattr(office_handoff, 'select_outputs', lambda _records: refs)
    pending = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    panel.refresh_capability_sources()
    result = pending[-1][1](object())
    panel.messages.put(('capability_sources', result, None))
    panel.poll()
    return refs


def test_history_reselection_clears_verified_refs_and_blocks_new_preview(goal_panel, monkeypatch):
    panel = goal_panel
    records, _refs, _calls, _pending, _current = _begin_history_seed(panel, monkeypatch)
    _validate_seed_sources(panel, monkeypatch, records)
    preview_calls = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: preview_calls.append((kind, work)))

    panel.invalidate_history_batch_source('history changed')
    panel.preview_capability('summarize both outputs')

    assert panel.capability_sources == []
    assert panel.plan is None
    assert preview_calls == []
    assert '历史批次绑定已失效' in panel.status.get()


def test_history_reselection_blocks_existing_preview_execution(goal_panel, monkeypatch):
    panel = goal_panel
    records, _refs, _calls, _pending, _current = _begin_history_seed(panel, monkeypatch)
    _validate_seed_sources(panel, monkeypatch, records)
    panel.plan = {'mode': 'capability', 'sha256': 'f' * 64}
    executions = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: executions.append((kind, work)))

    panel.invalidate_history_batch_source('history changed')
    panel.plan = {'mode': 'capability', 'sha256': 'f' * 64}
    panel.execute()

    assert panel.plan is None
    assert executions == []


def test_history_reselection_discards_late_capability_preview(goal_panel, monkeypatch):
    from agent_service import capability_router

    panel = goal_panel
    records, _refs, _calls, _pending, _current = _begin_history_seed(panel, monkeypatch)
    _validate_seed_sources(panel, monkeypatch, records)
    plan = {'mode': 'capability', 'sha256': 'e' * 64, 'source_ref': None,
            'instruction': 'summarize', 'candidates': []}
    monkeypatch.setattr(capability_router, 'prepare', lambda *_args, **_kwargs: plan)
    pending = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    panel.preview_capability('summarize both outputs')
    late = pending[0][1](object())
    panel.invalidate_history_batch_source('history changed')
    panel.messages.put(('capability_preview', late, None))
    panel.poll()

    assert panel.plan is None
    assert '本地预览完成' not in panel.status.get()


def test_history_reselection_discards_late_office_bundle_preview(goal_panel, monkeypatch):
    from agent_service import office_bundle

    panel = goal_panel
    records, _refs, _calls, _pending, _current = _begin_history_seed(panel, monkeypatch, 2)
    _validate_seed_sources(panel, monkeypatch, records)
    plan = {'mode': 'office_bundle', 'sha256': '9' * 64,
            'instruction': 'compare', 'sources': []}
    monkeypatch.setattr(office_bundle, 'prepare', lambda *_args, **_kwargs: plan)
    pending = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    panel.preview_office_bundle()
    late = pending[0][1](object())
    panel.invalidate_history_batch_source('history changed')
    panel.messages.put(('office_bundle_preview', late, None))
    panel.poll()

    assert panel.plan is None
    assert '联合核对预览完成' not in panel.status.get()


def test_real_start_busy_callback_does_not_invalidate_own_source_worker(goal_panel, monkeypatch):
    from agent_service import office_handoff

    panel = goal_panel
    records = [{'path': 'live.docx', 'sha256': 'a' * 64}]
    refs = [{'artifact': 'live.docx', 'sha256': 'a' * 64}]
    panel.get_artifacts = lambda: records
    monkeypatch.setattr(office_handoff, 'select_outputs', lambda _records: refs)
    state = {'active': None}
    panel.external_busy = lambda: state['active'] is not None
    panel.on_busy = lambda value: state.update(active='computer_goal' if value else None)
    panel.session = object()

    class SynchronousThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(goal_ui.threading, 'Thread', SynchronousThread)
    panel.mode.set(goal_ui.MODES[11])
    panel.refresh_capability_sources()
    panel.poll()

    assert panel.capability_sources == refs
    assert panel.capability_sources_validated_generation == panel.capability_source_generation


def test_short_external_activity_invalidates_plan_even_after_busy_ends(goal_panel):
    panel = goal_panel
    panel.plan = {'mode': 'capability', 'sha256': 'd' * 64}

    panel.notify_external_activity(True)
    panel.notify_external_activity(False)

    assert panel.plan is None
    assert panel.capability_source_generation > 0


def test_consecutive_external_starts_each_invalidate_without_idle_poll(goal_panel):
    panel = goal_panel
    panel.plan = {'mode': 'capability', 'sha256': 'e' * 64}
    before = panel.capability_source_generation

    panel.notify_external_activity(True, force=True)
    first = panel.capability_source_generation
    assert first > before
    assert panel.plan is None

    panel.plan = {'mode': 'capability', 'sha256': 'f' * 64}
    panel.notify_external_activity(True, force=True)

    assert panel.capability_source_generation > first
    assert panel.plan is None


def test_verified_history_batch_can_choose_single_input_without_losing_validation(goal_panel, monkeypatch):
    from agent_service import capability_router

    panel = goal_panel
    records, refs, _calls, _pending, _current = _begin_history_seed(panel, monkeypatch)
    _validate_seed_sources(panel, monkeypatch, records)
    panel.plan = {'mode': 'capability', 'sha256': 'c' * 64}
    panel.control.current(2)
    panel.capability_control_changed()
    planned = {'mode': 'capability', 'sha256': 'b' * 64,
               'source_ref': refs[0], 'instruction': 'use first', 'candidates': []}
    calls = []
    monkeypatch.setattr(capability_router, 'prepare',
                        lambda session, instruction, **options:
                        calls.append((session, instruction, options)) or planned)
    pending = []
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    panel.preview_capability('use the selected file')

    assert panel.plan is None
    assert pending[0][0] == 'capability_preview'
    pending[0][1]('session')
    assert calls == [('session', 'use the selected file', {'source_ref': refs[0]})]
    assert panel._history_source_is_validated()
