"""Main GUI routing for current and selected historical Office outputs."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_history_resume_gui as history_gui
from test_history_resume_gui import _Thread, _button, _pump


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    value = history_gui.desktop.__wrapped__(monkeypatch, tmp_path)
    panel = value[2]["goal_panel"]
    panel.text = history_gui._Widget(height=6, wrap="word")
    panel.closed = False
    return value


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
            self.closed = False
            self.text = history_gui._Widget(height=6, wrap="word")
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


def _current_edit_context():
    button = _button("编辑本批成果…")
    return button, button.options["command"], _function_state(button.options["command"])


def _set_current_outputs(outputs, records):
    outputs.clear()
    outputs.update({label: path for label, path, _digest in records})
    hashes = _function_state(_button("编辑本批成果…").options["command"])["agent_output_hashes"]
    hashes.clear()
    hashes.update({label: digest for label, _path, digest in records})
    output_list = next(widget for widget in history_gui._Widget.all
                       if widget.options.get("width") == 24
                       and widget.options.get("state") == "readonly"
                       and "<<ComboboxSelected>>" in widget.callbacks)
    output_list.configure(values=tuple(outputs))
    output_list.options["textvariable"].set(next(iter(outputs), ""))
    output_list.callbacks["<<ComboboxSelected>>"](None)


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


def test_edit_current_outputs_passes_explicit_office_batch_snapshot_and_filters_pdf(
        gui_compat, desktop, tmp_path):
    panel = _goal_panel_from_button()
    _button_edit, callback, state = _current_edit_context()
    calls = []
    panel.use_generated_artifacts = lambda records: calls.append(records)
    records = [("word", tmp_path / "one.docx", "a" * 64),
               ("excel", tmp_path / "two.xlsx", "b" * 64),
               ("report", tmp_path / "three.pdf", "c" * 64)]
    _set_current_outputs(state["agent_outputs"], records)

    assert _button("编辑本批成果…").cget("state") == "normal"
    callback()

    expected = [{"path": str(records[0][1]), "sha256": records[0][2]},
                {"path": str(records[1][1]), "sha256": records[1][2]}]
    assert calls == [expected]
    state["agent_outputs"]["word"] = tmp_path / "changed.docx"
    state["agent_output_hashes"]["word"] = "d" * 64
    assert calls[0] == expected


def test_edit_current_outputs_never_falls_back_to_history(gui_compat, desktop, monkeypatch):
    root, task_root, state, tree, _original = desktop
    _history_batch(root, task_root, state, tree)
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda records: calls.append(records)
    _button_edit, callback, closure = _current_edit_context()
    _set_current_outputs(closure["agent_outputs"], [])

    assert _button("编辑本批成果…").cget("state") == "disabled"
    callback()

    assert calls == []
    assert "当前批次没有完整的可核验 Word/Excel 成果" in _task_status_text()
    assert len(state["history_artifacts"]) == 3


@pytest.mark.parametrize("invalid", ["missing_sha", "relative_path"])
def test_edit_current_outputs_rejects_incomplete_or_relative_batch(
        gui_compat, desktop, tmp_path, invalid):
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda records: calls.append(records)
    _button_edit, callback, state = _current_edit_context()
    path = tmp_path / "one.docx"
    digest = "a" * 64
    if invalid == "missing_sha":
        digest = None
    else:
        path = Path("relative.docx")
    _set_current_outputs(state["agent_outputs"], [("word", path, digest)])

    callback()

    assert calls == []
    assert "没有完整的可核验 Word/Excel 成果" in _task_status_text()


def test_edit_current_outputs_preserves_nonempty_target_draft(gui_compat, desktop, tmp_path):
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda records: calls.append(records)
    _button_edit, callback, state = _current_edit_context()
    _set_current_outputs(state["agent_outputs"], [("word", tmp_path / "one.docx", "a" * 64)])
    panel.text.insert("1.0", "已有连续任务草稿")
    selected_tabs = []
    state["tabs"].select = selected_tabs.append

    callback()

    assert panel.text.get("1.0", "end-1c") == "已有连续任务草稿"
    assert calls == []
    assert "已有指令草稿" in _task_status_text()
    assert selected_tabs == []  # Keep the explanation visible on the main task page.


@pytest.mark.parametrize("guard", ["active", "pending", "running", "closed"])
def test_edit_current_outputs_rejects_busy_or_closed_target(gui_compat, desktop, tmp_path, guard):
    panel = _goal_panel_from_button()
    calls = []
    panel.use_generated_artifacts = lambda records: calls.append(records)
    _button_edit, callback, state = _current_edit_context()
    _set_current_outputs(state["agent_outputs"], [("word", tmp_path / "one.docx", "a" * 64)])
    if guard == "active":
        state["active"]["kind"] = "task"
    elif guard == "pending":
        state["source_choice"]["pending"] = True
    elif guard == "running":
        panel.running = True
    else:
        panel.closed = True

    callback()

    assert calls == []


def _task_status_text():
    for widget in history_gui._Widget.all:
        value = widget.options.get("textvariable")
        if value is not None and "当前批次没有完整的可核验" in str(value.get()):
            return str(value.get())
    for widget in history_gui._Widget.all:
        value = widget.options.get("textvariable")
        if value is not None and "已有指令草稿" in str(value.get()):
            return str(value.get())
    return ""
