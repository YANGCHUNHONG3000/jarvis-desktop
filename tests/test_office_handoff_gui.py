"""Main GUI routing for current and selected historical Office outputs."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_history_resume_gui as history_gui
from test_history_resume_gui import _Thread, _button, _pump


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    return history_gui.desktop.__wrapped__(monkeypatch, tmp_path)


@pytest.fixture
def gui_compat(monkeypatch):
    # The historical desktop fixture predates the main GUI's extra computer
    # panel. Keep this test focused on goal/history wiring with a small stub.
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


def _goal_panel_from_button():
    # The generated-source callback closes over goal_panel in the launcher.
    callback = _button("继续编辑").options["command"]
    return _function_state(callback)["goal_panel"]


def _history_batch(root, task_root, state, tree):
    workflow = task_root / ("workflow-" + "b" * 32)
    records = []
    for step, filename, body in (
            ("s1", "document.docx", b"Word output"),
            ("s2", "spreadsheet.xlsx", b"Excel output"),
            ("s3", "report.pdf", b"ignore PDF")):
        path = workflow / step / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        records.append({"label": step + " output", "path": str(path),
                        "sha256": hashlib.sha256(body).hexdigest()})
    state["history_artifacts"] = records
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    return records


def test_current_artifact_batch_includes_word_excel_and_ignores_other_types(gui_compat, desktop):
    panel = _goal_panel_from_button()
    function = panel.get_artifacts
    closure = _function_state(function)
    outputs, hashes = closure["agent_outputs"], closure["agent_output_hashes"]
    outputs.update({"word": Path("current.docx"), "excel": Path("current.xlsx"),
                    "report": Path("current.pdf")})
    hashes.update({"word": "a" * 64, "excel": "b" * 64, "report": "c" * 64})

    assert function() == [
        {"path": "current.docx", "sha256": "a" * 64},
        {"path": "current.xlsx", "sha256": "b" * 64},
    ]


def test_continue_editing_passes_whole_selected_history_office_batch(gui_compat, desktop, monkeypatch):
    root, task_root, state, tree, _original = desktop
    records = _history_batch(root, task_root, state, tree)
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda value: calls.append(value)

    _button("继续编辑").invoke()

    assert calls == [[{"path": records[0]["path"], "sha256": records[0]["sha256"]},
                      {"path": records[1]["path"], "sha256": records[1]["sha256"]}]]


def test_continue_editing_guards_busy_pending_and_uninspected(gui_compat, desktop):
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda value: calls.append(value)
    callback = _button("继续编辑").options["command"]
    state = _function_state(callback)
    active, source_choice = state["active"], state["source_choice"]
    history_selected = state["history_selected"]
    history_selected["inspection"] = {"root": "selected-task"}
    state["history_artifacts"]["office.docx"] = (Path("office.docx"), "a" * 64)

    active["kind"] = "task"
    callback()
    active["kind"] = None
    source_choice["pending"] = True
    callback()
    source_choice["pending"] = False
    panel.running = True
    callback()
    panel.running = False
    history_selected["inspection"] = None
    callback()

    assert calls == []
