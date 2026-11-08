"""Main GUI wiring for the Office task-entry controller."""

from pathlib import Path

import pytest

import test_history_resume_gui as history_gui
from test_history_resume_gui import _button


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    value = history_gui.desktop.__wrapped__(monkeypatch, tmp_path)
    panel = value[2]["goal_panel"]
    panel.text = history_gui._Widget(height=6, wrap="word")
    panel.instruction_draft = history_gui._FakeDraft(panel.text)
    panel.closed = False
    panel.host = "offline-test"
    return value


def _controller():
    command = _button("按任务规划（云端）").options["command"]
    return command.__self__


def _closure(function):
    return dict(zip(function.__code__.co_freevars,
                    (cell.cell_contents for cell in function.__closure__ or ())))


def test_main_route_row_selected_and_batch_scope_are_explicit(desktop, tmp_path):
    entry = _controller()
    route_button = _button("按任务规划（云端）")
    edit_button = _button("编辑本批成果…")
    assert callable(route_button.options["command"])
    assert edit_button.options["text"] == "编辑本批成果…"
    assert route_button.parent is not edit_button.parent
    values = next(widget.options.get("values") for widget in history_gui._Widget.all
                  if widget.options.get("values") == ("当前选中资料", "当前成果批次"))
    assert values == ("当前选中资料", "当前成果批次")
    closure = _closure(entry.context)
    source_choice = closure["source_choice"]
    source_label = closure["source_label"]
    route_scope = closure["route_scope"]
    task_text = closure["task_text"]
    goal_panel = closure["goal_panel"]
    task_text.insert("1.0", "更新选中资料中的标题")

    selected = tmp_path / "selected.docx"
    ref = {"artifact": str(selected), "sha256": "a" * 64}
    source_choice.update(artifact_ref=ref, pending=False, auto_required=False,
                         retry_ref=None, retry_changed=False)
    source_choice["setting"] = True
    try:
        source_label.set(str(selected))
    finally:
        source_choice["setting"] = False
    route_scope.set("当前选中资料")
    current = entry.context()
    assert current["records"] == [{"path": str(selected), "sha256": "a" * 64}]
    assert current["expected_refs"] == [ref] and current["unavailable"] is False

    goal_panel.text.insert("1.0", "连续任务目标草稿")
    outputs = closure["agent_outputs"]
    hashes = closure["agent_output_hashes"]
    outputs.update({"word": tmp_path / "batch.docx", "excel": tmp_path / "batch.xlsx",
                    "pdf": tmp_path / "ignored.pdf"})
    hashes.update({"word": "b" * 64, "excel": "c" * 64, "pdf": "d" * 64})
    route_scope.set("当前成果批次")
    current = entry.context()
    assert current["records"] == [
        {"path": str(tmp_path / "batch.docx"), "sha256": "b" * 64},
        {"path": str(tmp_path / "batch.xlsx"), "sha256": "c" * 64},
    ]
    assert current["expected_refs"] is None and current["unavailable"] is False
    assert current["draft"] == "连续任务目标草稿"


def test_main_window_close_closes_route_controller(desktop):
    root, _task_root, state, _tree, _original = desktop
    entry = _controller()
    panel = state["goal_panel"]
    closed = []
    panel.close = lambda: closed.append(True)

    root.callbacks["WM_DELETE_WINDOW"]()

    assert entry.closed is True
    assert root.destroyed is True
    assert closed == [True]


def test_unknown_route_scope_is_refused_before_worker_starts(desktop):
    entry = _controller()
    closure = _closure(entry.context)
    closure["task_text"].insert("1.0", "更新指定资料")
    closure["route_scope"].set("未知来源范围")
    closure["agent_outputs"].update({"word": Path("batch.docx")})
    closure["agent_output_hashes"]["word"] = "a" * 64

    assert entry.context()["unavailable"] is True
    assert entry.start() is False
    assert entry.pending is None
    assert not any(thread.name == "office-task-planning" for thread in history_gui._Thread.all)
    assert any("资料绑定尚未就绪" in str(widget.options["textvariable"].get())
               for widget in history_gui._Widget.all if widget.options.get("textvariable"))


def test_ready_route_handoff_selects_panel_tab_without_frame_attribute(
        desktop, monkeypatch):
    _root, _task_root, state, _tree, _original = desktop
    entry = _controller()
    closure = _closure(entry.context)
    panel = state["goal_panel"]
    assert hasattr(panel, "tab") and not hasattr(panel, "frame")
    closure["task_text"].insert("1.0", "更新所选文档标题")
    selected = Path("selected.docx").resolve()
    ref = {"artifact": str(selected), "sha256": "a" * 64}
    source_choice = closure["source_choice"]
    source_choice.update(artifact_ref=ref, pending=False, auto_required=False,
                         retry_ref=None, retry_changed=False)
    source_choice["setting"] = True
    try:
        closure["source_label"].set(str(selected))
    finally:
        source_choice["setting"] = False

    from agent_service import daily_task_routing, office_handoff
    monkeypatch.setattr(office_handoff, "select_outputs", lambda records, **_kwargs: [dict(ref)])
    monkeypatch.setattr(daily_task_routing, "propose",
                        lambda *_args, **_kwargs: {"state": "ready", "plan": {"steps": []}})
    entry.session_factory = lambda: object()
    panel.use_task_route = lambda *_args: True
    prepared = _closure(_closure(entry.prepare)['office_chain_route_binding'].adopt)
    selected_tabs = []
    prepared["tabs"].select = selected_tabs.append

    assert entry.start() is True
    history_gui._Thread.run("office-task-planning")
    entry.poll()

    assert selected_tabs == [panel.tab]
    assert hasattr(_button("编辑本批成果…"), "options")


@pytest.mark.parametrize("changed_binding", ["source", "target"])
def test_route_plan_stays_stale_after_source_or_draft_changes_back(
        desktop, monkeypatch, changed_binding):
    _root, _task_root, state, _tree, _original = desktop
    entry = _controller()
    context = _closure(entry.context)
    panel = state["goal_panel"]
    context["task_text"].insert("1.0", "按所选资料整理报告")
    panel.text.insert("1.0", "按所选资料整理报告")
    selected = Path("selected.docx").resolve()
    ref = {"artifact": str(selected), "sha256": "a" * 64}
    source = context["source_choice"]
    source.update(artifact_ref=ref, pending=False, auto_required=False,
                  retry_ref=None, retry_changed=False)
    source["setting"] = True
    try:
        context["source_label"].set(str(selected))
    finally:
        source["setting"] = False

    from agent_service import daily_task_routing, office_handoff
    monkeypatch.setattr(office_handoff, "select_outputs", lambda *_args, **_kwargs: [dict(ref)])
    monkeypatch.setattr(daily_task_routing, "propose",
                        lambda *_args, **_kwargs: {"state": "ready", "plan": {"steps": []}})
    entry.session_factory = lambda: object()
    delivered, selected_tabs = [], []
    panel.use_task_route = lambda *_args: delivered.append(True) or True
    prepared = _closure(_closure(entry.prepare)['office_chain_route_binding'].adopt)
    prepared["tabs"].select = selected_tabs.append

    assert entry.start() is True
    history_gui._Thread.run("office-task-planning")
    if changed_binding == "source":
        generation = source["generation"]
        source["artifact_ref"] = {"artifact": str(Path("other.docx").resolve()),
                                  "sha256": "b" * 64}
        source["generation"] = generation + 1
        source["artifact_ref"] = ref
    else:
        panel.text.insert("1.0", "临时修改")
        panel.text.delete("1.0", "end")
        panel.text.insert("1.0", "按所选资料整理报告")
    entry.poll()

    assert delivered == []
    assert selected_tabs == []
    assert "未接入" in entry.status.__self__.get()
