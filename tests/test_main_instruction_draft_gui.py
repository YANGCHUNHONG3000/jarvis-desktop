"""Main GUI instruction-ticket wiring tests; no Office or model calls."""
from pathlib import Path
import subprocess

import pytest

import test_history_resume_gui as history_gui


def _closure(function):
    return dict(zip(function.__code__.co_freevars,
                    (cell.cell_contents for cell in function.__closure__ or ())))


def _desktop(monkeypatch, tmp_path):
    desktop = history_gui.desktop.__wrapped__(monkeypatch, tmp_path)
    root, task_root, state, tree, original = desktop
    panel = state["goal_panel"]
    panel.text = history_gui._Widget(height=6, wrap="word")
    panel.instruction_draft = history_gui._FakeDraft(panel.text)
    panel.closed = False
    panel.host = "offline-test"
    return root, task_root, state, tree, original


def _main(monkeypatch, tmp_path):
    root, task_root, state, tree, original = _desktop(monkeypatch, tmp_path)
    start = history_gui._button("开始任务").options["command"]
    values = _closure(start)
    return root, task_root, state, tree, original, values


def _office_success(monkeypatch, values, *, result=None, outputs=None):
    from agent_service import task_history
    outcome = result or {"root": "", "kind": "office", "ok": True,
                         "state": "succeeded", "artifacts": [{"path": "synthetic.docx",
                                                                  "sha256": "a" * 64}]}
    checked_outputs = outputs if outputs is not None else [("Word · .docx", Path("synthetic.docx"))]

    def durable(root, _base):
        return {**outcome, "root": str(root)}

    monkeypatch.setattr(history_gui.launcher, "_durable_task_result", durable)
    monkeypatch.setattr(history_gui.launcher, "_office_artifacts",
                        lambda _result, _root: checked_outputs)


def _start(values, text="生成周报"):
    values["task_text"].insert("1.0", text)
    history_gui._button("开始任务").invoke()
    submitted = values["submitted_instruction"]
    assert submitted["task_root"]
    assert submitted["ticket"] == values["task_draft"].snapshot()
    return submitted["task_root"], submitted["ticket"]


def _finish_success(root, *, timed_out=False):
    history_gui._Thread.run("jarvis-agent")
    if timed_out:
        raise AssertionError("use timeout process helper")
    history_gui._pump(root)


@pytest.fixture
def started(monkeypatch, tmp_path):
    root, _task_root, state, _tree, _original, values = _main(monkeypatch, tmp_path)
    return root, state, values


def test_task_start_binds_root_and_ticket_then_clears_verified_success(started, monkeypatch):
    root, _state, values = started
    _office_success(monkeypatch, values)
    task_root, ticket = _start(values)
    assert values["submitted_instruction"] == {"task_root": task_root, "ticket": ticket}
    assert values["active"]["task_root"] == Path(task_root)

    _finish_success(root)

    assert values["task_text"].get("1.0", "end-1c") == ""
    assert values["submitted_instruction"] == {"task_root": None, "ticket": None}


@pytest.mark.parametrize("edit_back", [False, True])
def test_new_or_changed_back_draft_survives_old_task_success(started, monkeypatch, edit_back):
    root, _state, values = started
    _office_success(monkeypatch, values)
    task_root, original_ticket = _start(values)
    original_text = original_ticket["text"]
    if edit_back:
        values["task_text"].delete("1.0", "end")
        values["task_text"].insert("1.0", original_text)
    else:
        values["task_text"].delete("1.0", "end")
        values["task_text"].insert("1.0", "下一份任务草稿")

    _finish_success(root)

    assert values["task_text"].get("1.0", "end-1c") == (
        original_text if edit_back else "下一份任务草稿")
    assert values["submitted_instruction"]["task_root"] is None


@pytest.mark.parametrize("case", ["failed", "partial", "no_outputs", "nonzero", "stopped"])
def test_unsuccessful_or_interrupted_office_keeps_submitted_instruction(
        started, monkeypatch, case):
    root, _state, values = started
    outcome = {"kind": "office", "ok": False, "state": "partial",
               "artifacts": [{"path": "partial.docx", "sha256": "b" * 64}]}
    if case == "failed":
        outcome.update(state="failed", artifacts=[])
    elif case == "nonzero":
        outcome.update(ok=True, state="succeeded")
    elif case == "stopped":
        outcome.update(ok=True, state="succeeded")
    outputs = [] if case in {"failed", "no_outputs"} else None
    if case == "no_outputs":
        outcome.update(ok=True, state="succeeded")
    _office_success(monkeypatch, values, result=outcome, outputs=outputs)
    task_root, ticket = _start(values)
    if case == "nonzero":
        history_gui._Process.on_communicate = lambda process: setattr(process, "terminated", True)
    if case == "stopped":
        values["active"]["stop"] = True

    _finish_success(root)

    assert values["task_text"].get("1.0", "end-1c") == ticket["text"]
    assert values["submitted_instruction"] == {"task_root": task_root, "ticket": ticket}


def test_timeout_keeps_instruction_even_with_durable_success(started, monkeypatch):
    root, _state, values = started
    _office_success(monkeypatch, values, result={
        "kind": "office", "ok": True, "state": "succeeded",
        "artifacts": [{"path": "word.docx", "sha256": "a" * 64}]})
    task_root, ticket = _start(values)

    class TimeoutProcess(history_gui._Process):
        first = True

        def communicate(self, timeout=None):
            if self.first:
                self.first = False
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.returncode = 1
            return ("", None)

    monkeypatch.setattr(history_gui.launcher.subprocess, "Popen", TimeoutProcess)
    _finish_success(root)

    assert values["task_text"].get("1.0", "end-1c") == ticket["text"]
    assert values["submitted_instruction"]["task_root"] == task_root


def test_route_completion_is_bound_to_original_main_and_goal_tickets(
        monkeypatch, tmp_path):
    _root, _task_root, state, _tree, _original = _desktop(monkeypatch, tmp_path)
    from agent_service import daily_task_routing, office_handoff
    entry = history_gui._button("按任务规划（云端）").options["command"].__self__
    context = _closure(entry.context)
    panel = state["goal_panel"]
    context["task_text"].insert("1.0", "生成所选资料的交接表")
    panel.text.insert("1.0", "生成所选资料的交接表")
    selected = Path("source.docx").resolve()
    ref = {"artifact": str(selected), "sha256": "a" * 64}
    source = context["source_choice"]
    source.update(artifact_ref=ref, pending=False, auto_required=False,
                  retry_ref=None, retry_changed=False)
    source["setting"] = True
    try:
        context["source_label"].set(str(selected))
    finally:
        source["setting"] = False
    monkeypatch.setattr(office_handoff, "select_outputs", lambda *_args, **_kwargs: [dict(ref)])
    monkeypatch.setattr(daily_task_routing, "propose",
                        lambda *_args, **_kwargs: {"state": "ready", "plan": {"steps": []}})
    entry.session_factory = lambda: object()
    panel.use_task_route = lambda *_args: True
    prepared = _closure(panel.on_instruction_complete)
    assert entry.start()
    history_gui._Thread.run("office-task-planning")
    entry.poll()
    routed = prepared["routed_instruction"]
    main_ticket, goal_ticket = routed["main"], routed["goal"]
    assert main_ticket["text"] == "生成所选资料的交接表"
    assert goal_ticket["text"] == "生成所选资料的交接表"

    # A wrong goal ticket cannot consume the main instruction.
    panel.on_instruction_complete({"revision": goal_ticket["revision"] + 1,
                                   "text": goal_ticket["text"]})
    assert context["task_text"].get("1.0", "end-1c") == main_ticket["text"]

    # The exact route ticket completes the old request. A newly typed main
    # draft survives, including the edit-then-return case tracked by revision.
    panel.on_instruction_complete(goal_ticket)
    assert context["task_text"].get("1.0", "end-1c") == ""
    assert routed == {"main": None, "goal": None}


@pytest.mark.parametrize("edit_back", [False, True])
def test_route_completion_preserves_new_main_draft(monkeypatch, tmp_path, edit_back):
    _root, _task_root, state, _tree, _original = _desktop(monkeypatch, tmp_path)
    from agent_service import daily_task_routing, office_handoff
    entry = history_gui._button("按任务规划（云端）").options["command"].__self__
    context = _closure(entry.context)
    panel = state["goal_panel"]
    context["task_text"].insert("1.0", "创建资料总结")
    panel.text.insert("1.0", "创建资料总结")
    selected = Path("source.docx").resolve()
    ref = {"artifact": str(selected), "sha256": "a" * 64}
    source = context["source_choice"]
    source.update(artifact_ref=ref, pending=False, auto_required=False,
                  retry_ref=None, retry_changed=False)
    source["setting"] = True
    try:
        context["source_label"].set(str(selected))
    finally:
        source["setting"] = False
    monkeypatch.setattr(office_handoff, "select_outputs", lambda *_args, **_kwargs: [dict(ref)])
    monkeypatch.setattr(daily_task_routing, "propose",
                        lambda *_args, **_kwargs: {"state": "ready", "plan": {"steps": []}})
    entry.session_factory = lambda: object()
    panel.use_task_route = lambda *_args: True
    prepared = _closure(panel.on_instruction_complete)
    assert entry.start()
    history_gui._Thread.run("office-task-planning")
    entry.poll()
    main_ticket = prepared["routed_instruction"]["main"]
    if edit_back:
        context["task_text"].delete("1.0", "end")
        context["task_text"].insert("1.0", main_ticket["text"])
        expected = main_ticket["text"]
    else:
        context["task_text"].delete("1.0", "end")
        context["task_text"].insert("1.0", "新写的任务")
        expected = "新写的任务"

    panel.on_instruction_complete(prepared["routed_instruction"]["goal"])

    assert context["task_text"].get("1.0", "end-1c") == expected
