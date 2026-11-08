"""GUI wiring coverage for explicit, non-executing workspace recovery."""

from copy import deepcopy

import pytest

import test_history_resume_gui as history_gui
from test_history_resume_gui import _Process, _Thread, _Widget, _button, _pump
from agent_service import office_workspace_panel, workspace_resume, workspace_state


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    controllers = []
    original = workspace_resume.WorkspaceResume
    class Capture(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            if str(kwargs.get("store", "")).endswith("assistant_workspace"):
                controllers.append(self)
    monkeypatch.setattr(workspace_resume, "WorkspaceResume", Capture)
    monkeypatch.setattr(office_workspace_panel, "WorkspaceResume", Capture)
    result = history_gui.desktop.__wrapped__(monkeypatch, tmp_path)
    assert controllers, "assistant workspace controller was not constructed"
    result[2]["workspace_controller"] = controllers[-1]
    return result


def _text():
    return next(widget for widget in _Widget.all
                if widget.options.get("height") == 6)


def _seed(desktop, monkeypatch):
    _root, task_root, state, _tree, _original = desktop
    first = task_root / "one.docx"
    second = task_root / "two.xlsx"
    first.write_bytes(b"verified one")
    second.write_bytes(b"verified two")
    records = [{"path": str(first), "sha256": "a" * 64},
               {"path": str(second), "sha256": "b" * 64}]
    payload = {"draft": "  从上次接续\r\n", "records": records,
               "source": deepcopy(records[1])}
    store = state.get("workspace_store")
    if store is None:
        import run_jarvis_assistant as launcher
        store = launcher.ROOT / "data" / "assistant_workspace"
        state["workspace_store"] = store
    state["workspace_saved"] = workspace_state.save(store, payload, None)
    refs = [{"artifact": item["path"], "sha256": item["sha256"],
             "task_root": str(task_root)} for item in records]
    selected = deepcopy(refs[1])
    monkeypatch.setattr(workspace_state, "verify", lambda value, _base: {
        "payload": deepcopy(value), "refs": deepcopy(refs),
        "source_ref": deepcopy(selected)})
    return state, payload, refs, selected


def test_explicit_restore_applies_exact_draft_batch_and_selected_source_without_run(
        desktop, monkeypatch):
    root, _task_root, state, _tree, _original = desktop
    state, payload, refs, selected = _seed(desktop, monkeypatch)
    assert state["workspace_controller"].context()["unavailable"] is False
    button = _button("恢复上次工作（本机）")
    invoked = button.invoke()
    assert invoked is True, repr([w.options.get("textvariable").get()
                                  for w in _Widget.all if w.options.get("textvariable")])
    assert _Thread.all[-1].name == "workspace-restore"
    _Thread.run("workspace-restore")
    _pump(root)
    assert _text().get("1.0", "end-1c") == payload["draft"]
    assert state["goal_panel"].get_artifacts() == payload["records"]
    source_fields = [widget.options["textvariable"].get()
                     for widget in _Widget.all if "textvariable" in widget.options
                     and widget.options["textvariable"].get() == selected["artifact"]]
    assert source_fields
    assert not _Process.created
    assert state["workspace_saved"]["payload"] == payload


def test_edit_after_restore_can_be_explicitly_saved_with_batch_provenance(
        desktop, monkeypatch):
    root, _task_root, state, _tree, _original = desktop
    state, payload, _refs, _selected = _seed(desktop, monkeypatch)
    _button("恢复上次工作（本机）").invoke()
    _Thread.run("workspace-restore")
    _pump(root)
    task_text = _text()
    task_text.insert("end", "新的续接")
    assert _button("保存当前工作（本机）").invoke() is True
    saved = workspace_state.load(state["workspace_store"])
    assert saved["payload"]["draft"] == payload["draft"] + "新的续接"
    assert saved["payload"]["records"] == payload["records"]
    assert saved["payload"]["source"] == payload["source"]
    assert not _Process.created


def test_different_nonempty_draft_or_changed_source_refuses_restore(
        desktop, monkeypatch):
    root, _task_root, state, _tree, _original = desktop
    _seed(desktop, monkeypatch)
    task_text = _text()
    task_text.insert("1.0", "new draft")
    _button("恢复上次工作（本机）").invoke()
    assert not _Thread.all
    assert task_text.get("1.0", "end-1c") == "new draft"

    task_text.delete("1.0", "end")
    _button("恢复上次工作（本机）").invoke()
    _Thread.run("workspace-restore")
    _pump(root)
    source_var = next(widget.options["textvariable"] for widget in _Widget.all
                      if "textvariable" in widget.options
                      and isinstance(widget.options["textvariable"].get(), str)
                      and widget.options["textvariable"].get().endswith("two.xlsx"))
    source_var.set("changed-source.xlsx")
    _button("恢复上次工作（本机）").invoke()
    assert not _Thread.all
    assert not _Process.created


def test_edit_then_revert_while_verifying_and_close_discard_result(
        desktop, monkeypatch):
    root, _task_root, state, _tree, _original = desktop
    _seed(desktop, monkeypatch)
    task_text = _text()
    button = _button("恢复上次工作（本机）")
    button.invoke()
    task_text.insert("end", "transient")
    task_text.delete("end-8c", "end")
    _Thread.run("workspace-restore")
    _pump(root)
    assert task_text.get("1.0", "end-1c") == ""
    assert not state["goal_panel"].get_artifacts()
    assert not _Process.created

    # A separate pending attempt is invalidated through the normal close callback.
    button.invoke()
    assert root.callbacks["WM_DELETE_WINDOW"]
    root.callbacks["WM_DELETE_WINDOW"]()
    _Thread.run("workspace-restore")
    _pump(root)
    assert not state["goal_panel"].get_artifacts()
    assert not _Process.created
