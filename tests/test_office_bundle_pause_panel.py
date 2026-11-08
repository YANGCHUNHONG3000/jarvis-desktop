"""GoalPanel Office bundle pause handoff; no API or native Office execution."""
import threading
import tkinter as tk
from tkinter import ttk

import pytest

from agent_service import computer_goal_panel as ui
from agent_service.computer_goal_panel import _InstructionResult, attach_goal_tab


@pytest.fixture(scope="module")
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(hidden_root):
    tabs = ttk.Notebook(hidden_root)
    changes = []
    value = attach_goal_tab(tabs, hidden_root, on_busy=changes.append)
    yield value, changes
    value.close()
    tabs.destroy()


class UiBoundBoolean:
    """Fail if the execute-time pause choice is read from a worker thread."""
    def __init__(self, variable):
        self.owner = threading.get_ident()
        self.variable = variable
        self.reads = 0

    def get(self):
        assert threading.get_ident() == self.owner
        self.reads += 1
        return self.variable.get()

    def set(self, value):
        self.variable.set(value)


@pytest.mark.parametrize("pause", [False, True])
def test_office_bundle_execute_captures_pause_and_preserves_approval_stop_ticket_and_runner_args(
        panel, monkeypatch, pause):
    from agent_service import office_bundle, office_workflow

    p, _changes = panel
    plan = {"mode": "office_bundle", "schema": 1,
            "sha256": "a" * 64, "instruction": "compare all tables"}
    ticket = {"revision": 8, "text": "compare all tables"}
    p.mode.set(ui.MODES[11])
    p.plan = plan
    p.plan_instruction_binding = None
    p._update_pause_checkbox()
    assert str(p.pause_checkbox["state"]) == "normal"
    if pause:
        p.pause_checkbox.invoke()
    variable = p.pause_each_step
    p.pause_each_step = UiBoundBoolean(variable)
    assert p.pause_each_step.get() is pause
    p.pause_each_step.reads = 0
    monkeypatch.setattr(p, "instruction_ticket", lambda _plan: ticket)

    started = []
    monkeypatch.setattr(p, "start", lambda kind, operation: started.append((kind, operation)))
    calls = {}
    steps, root = [{"id": "one"}, {"id": "two"}], object()
    options = {"deadline": 17, "cancel_requested": lambda: False}

    def execute(session, actual_plan, **kwargs):
        calls["execute"] = (session, actual_plan, kwargs)
        if pause:
            calls["workflow_result"] = kwargs["workflow_runner"](steps, root, **options)
        else:
            assert "workflow_runner" not in kwargs
        return {"ok": True, "state": "succeeded"}

    monkeypatch.setattr(office_bundle, "execute", execute)
    delegated = []
    monkeypatch.setattr(office_workflow, "run_office_workflow",
                        lambda actual_steps, actual_root, **kw:
                        delegated.append((actual_steps, actual_root, kw)) or {"paused": True})

    p.execute()
    assert p.pause_each_step.reads == 1
    # A later checkbox change must not affect this operation or be read by it.
    p.pause_each_step.set(not pause)

    errors = []
    result = []
    def worker():
        try:
            result.append(started[0][1](object()))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert not errors
    assert started[0][0] == "office_bundle_execute"
    assert isinstance(result[0], _InstructionResult)
    assert result[0].ticket == ticket
    assert p.pause_each_step.reads == 1
    session, actual_plan, kwargs = calls["execute"]
    assert actual_plan is plan and session is not None
    assert kwargs["approval"] == plan["sha256"]
    assert kwargs["stop"]() is False
    p.stopped.set()
    assert kwargs["stop"]() is True
    p.stopped.clear()
    if pause:
        assert set(kwargs) == {"approval", "stop", "workflow_runner"}
        assert calls["workflow_result"] == {"paused": True}
        assert delegated == [(steps, root, {**options, "pause_after_step": True})]
    else:
        assert set(kwargs) == {"approval", "stop"}
        assert delegated == []


@pytest.mark.parametrize("guard", ["invalid_history", "stale_draft", "busy"])
def test_office_bundle_execute_keeps_existing_guards(panel, guard):
    p, _changes = panel
    p.plan = {"mode": "office_bundle", "sha256": "b" * 64}
    started = []
    p.start_instruction = lambda *args: started.append(args)

    if guard == "invalid_history":
        p.capability_history_seed = {"is_current": lambda: False}
        p.capability_source_generation = 3
        p.capability_sources_validated_generation = 3
    elif guard == "stale_draft":
        p.plan_instruction_binding = {"plan_sha256": "old"}
        p.instruction_ticket = lambda _plan: None
    else:
        p.running = True

    p.execute()

    assert started == []
    if guard in {"invalid_history", "stale_draft"}:
        assert p.plan is None
        assert p.status.get()
    else:
        assert p.plan is not None


@pytest.mark.parametrize("plan,expected", [
    (None, "disabled"),
    ({"mode": "office_bundle", "schema": 1}, "normal"),
    ({"mode": "office_bundle", "schema": 2}, "disabled"),
    ({"mode": "capability", "schema": 4, "capability": "unsupported"}, "disabled"),
    ({"mode": "capability", "schema": 4, "capability": "mail_understanding"}, "normal"),
])
def test_pause_checkbox_state_tracks_reviewed_mail_and_bundle_plans(panel, plan, expected):
    p, _changes = panel
    p.mode.set(ui.MODES[11])
    p.plan = plan

    p._update_pause_checkbox()

    assert str(p.pause_checkbox["state"]) == expected


def test_pause_checkbox_stays_disabled_while_running_and_keeps_other_mode_behavior(panel):
    p, _changes = panel
    p.mode.set(ui.MODES[11])
    p.plan = {"mode": "office_bundle", "schema": 1}
    p.running = True
    p._update_pause_checkbox()
    assert str(p.pause_checkbox["state"]) == "disabled"

    p.running = False
    p.mode.set(ui.MODES[0])
    p.plan = None
    p._update_pause_checkbox()
    assert str(p.pause_checkbox["state"]) == "normal"
