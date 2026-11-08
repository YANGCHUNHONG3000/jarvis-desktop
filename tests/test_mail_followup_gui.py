"""Offline GUI checks for preparing a new task from a stopped mail analysis."""

from types import ModuleType

from test_history_resume_gui import _Process, _Thread, _Widget, _button, _pump, desktop
from test_history_resume_gui import _replace_package_module


def _mail_source(task_root):
    return {
        "version": 1,
        "task_root": str(task_root),
        "workflow_root": str(task_root / ("workflow-" + "b" * 32)),
        "step_id": "s1",
        "artifact": str(task_root / ("workflow-" + "b" * 32) / "s1" / "mail.docx"),
        "sha256": "a" * 64,
        "receipt_sha256": "c" * 64,
        "token": "d" * 64,
    }


def _select_mail_followup(root, tree, task_root, monkeypatch, *, eligible=True):
    # This fixture represents a real mail-analysis task for the marker-gated
    # production history inspector.
    (task_root / "mail-analysis-request.json").write_text('{"schema": 2}', encoding="utf-8")
    inspection = ({"eligible": True, "code": "mail_followup_needs_input",
                   "question": "请说明要整理哪一项。", "token": "e" * 64,
                   "task_root": str(task_root)} if eligible else
                  {"eligible": False, "code": "mail_followup_not_mail"})
    mail_followup = ModuleType("agent_service.mail_followup")
    mail_followup.inspect_followup = lambda *_args: dict(inspection)
    mail_followup.prepare_followup = lambda *_args: None
    mail_followup.describe = lambda preview: "邮件分析阶段已停止，尚无可续跑的办公步骤。"
    _replace_package_module(monkeypatch, "agent_service.mail_followup", mail_followup)
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    return mail_followup


def _task_text():
    return next(w for w in _Widget.all if w.options.get("height") == 6)


def test_only_verified_mail_followup_inspection_enables_button(desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    _select_mail_followup(root, tree, task_root, monkeypatch)

    assert _button("补充邮件要求…").cget("state") == "normal"
    assert "邮件分析阶段已停止" in _text_variable("历史")
    assert "没有可续跑的 Office 步骤" in _text_variable("历史")
    assert "请说明要整理哪一项" in _text_variable("候选")
    assert _text_variable("候选").count("请说明要整理哪一项") == 1
    assert "若属技能任务" not in _text_variable("历史")


def test_review_rejection_has_specific_mail_status_and_history_label(desktop, monkeypatch):
    import run_jarvis_assistant as launcher

    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    mail_followup.inspect_followup = lambda *_args: {
        "eligible": True, "code": "mail_followup_review_rejected",
        "question": "请核对独立审查提示。", "token": "e" * 64,
        "task_root": str(task_root)}
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)

    assert any("邮件分析阶段停止（审查未通过）" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert "请核对独立审查提示" in _text_variable("候选")
    assert "若属技能任务" not in _text_variable("历史")
    assert launcher._history_list_state_label("needs_input") == "记录待补充·待核验"


def test_nonmail_history_never_enables_followup(desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    _select_mail_followup(root, tree, task_root, monkeypatch, eligible=False)

    assert _button("补充邮件要求…").cget("state") == "disabled"


def test_nonmail_inspection_failure_keeps_original_skill_input_reason(desktop, monkeypatch):
    from agent_service import task_history

    root, task_root, _state, tree, _original = desktop
    assert not (task_root / "mail-analysis-request.json").exists()
    assert not (task_root / "mail-analysis-claim.json").exists()
    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: {
        "root": str(task_root), "kind": "skill", "state": "failed",
        "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
        "input_failure": {"code": "source_numeric_cell_invalid",
                           "details": "private fixture detail"}})
    mail_followup = ModuleType("agent_service.mail_followup")

    def unexpected_call(*_args):
        raise AssertionError("non-mail history must not inspect mail follow-up")

    mail_followup.inspect_followup = unexpected_call
    mail_followup.describe = unexpected_call
    _replace_package_module(monkeypatch, "agent_service.mail_followup", mail_followup)
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)

    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert "含缺失或含糊的文本" in visible
    assert "作用尚未开始" in visible
    assert "原失败结果保留" in visible
    assert "private fixture detail" not in visible


def test_prepare_restores_original_instruction_and_same_mail_without_auto_run(
        desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    instruction = "整理周一邮件任务并生成Word和Excel"
    source_ref = _mail_source(task_root)
    calls = []
    monkeypatch.setattr(mail_followup, "prepare_followup",
                        lambda root, base, token: calls.append((root, base, token)) or {
                            "source_ref": source_ref, "instruction": instruction,
                            "question": "请说明周一邮件中要整理哪项。",
                            "token": "e" * 64, "task_root": str(task_root)})
    _button("补充邮件要求…").invoke()

    assert calls == []
    assert _task_text().get("1.0", "end-1c") == ""
    _Thread.run("jarvis-mail-followup-prepare")
    _pump(root)

    assert calls and calls[0][2] == "e" * 64
    assert _task_text().get("1.0", "end-1c") == instruction
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == source_ref["artifact"]
    assert _button("理解邮件（云端）").cget("state") == "normal"
    assert "请说明周一邮件中要整理哪项" in _text_variable("任务")
    assert not any("run_jarvis_task.py" in " ".join(p.command) for p in _Process.created)


def test_different_nonempty_task_draft_is_never_overwritten(desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    instruction = "整理周一邮件任务并生成Word和Excel"
    source_ref = _mail_source(task_root)
    monkeypatch.setattr(mail_followup, "prepare_followup", lambda *_args: {
        "source_ref": source_ref, "instruction": instruction,
        "question": "请说明周一邮件中要整理哪项。",
        "token": "e" * 64, "task_root": str(task_root)})
    draft = "另一个未保存任务"
    _task_text().insert("1.0", draft)
    _button("补充邮件要求…").invoke()
    _Thread.run("jarvis-mail-followup-prepare")
    _pump(root)

    assert _task_text().get("1.0", "end-1c") == draft
    assert "请先自行保存或清空" in _text_variable("候选")
    assert _button("理解邮件（云端）").cget("state") == "disabled"


def test_late_prepare_after_selection_change_is_ignored(desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    instruction = "整理周一邮件任务并生成Word和Excel"
    monkeypatch.setattr(mail_followup, "prepare_followup", lambda *_args: {
        "source_ref": _mail_source(task_root), "instruction": instruction,
        "question": "请说明周一邮件中要整理哪项。",
        "token": "e" * 64, "task_root": str(task_root)})
    _button("补充邮件要求…").invoke()
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-mail-followup-prepare")
    _pump(root)

    assert _task_text().get("1.0", "end-1c") == ""
    assert _button("理解邮件（云端）").cget("state") == "disabled"


def test_prepare_timeout_ignores_late_worker_result(desktop, monkeypatch):
    import run_jarvis_assistant as launcher

    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    instruction = "整理周一邮件任务并生成Word和Excel"
    monkeypatch.setattr(mail_followup, "prepare_followup", lambda *_args: {
        "source_ref": _mail_source(task_root), "instruction": instruction,
        "question": "请说明周一邮件中要整理哪项。",
        "token": "e" * 64, "task_root": str(task_root)})
    clock = [100.0]
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    _button("补充邮件要求…").invoke()
    clock[0] = 131.0
    _pump(root)
    _Thread.run("jarvis-mail-followup-prepare")
    _pump(root)

    assert _task_text().get("1.0", "end-1c") == ""
    assert "超时" in _text_variable("候选")
    assert _button("理解邮件（云端）").cget("state") == "disabled"


def test_activity_start_and_finish_invalidates_late_followup_result(desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    mail_followup = _select_mail_followup(root, tree, task_root, monkeypatch)
    instruction = "整理周一邮件任务并生成Word和Excel"
    monkeypatch.setattr(mail_followup, "prepare_followup", lambda *_args: {
        "source_ref": _mail_source(task_root), "instruction": instruction,
        "question": "请说明周一邮件中要整理哪项。",
        "token": "e" * 64, "task_root": str(task_root)})
    _button("补充邮件要求…").invoke()

    note_text = next(w for w in _Widget.all if w.options.get("height") == 11)
    note_text.insert("1.0", "离线测试任务")
    _button("开始记事本任务").invoke()
    _Thread.run("jarvis-notepad")
    _pump(root)
    assert _button("开始记事本任务").cget("state") == "normal"

    # The older read-only prepare finishes only after the intervening activity
    # has already ended; its generation must still be stale.
    _Thread.run("jarvis-mail-followup-prepare")
    _pump(root)
    assert _task_text().get("1.0", "end-1c") == ""
    assert _button("理解邮件（云端）").cget("state") == "disabled"
    assert len(_Process.created) == 1


def test_goal_panel_start_invalidates_followup_without_notifying_itself(desktop):
    _root, _task_root, state, _tree, _original = desktop
    panel = state["goal_panel"]
    original_sources = dict(panel.capability_sources)

    panel.on_busy(True)

    assert panel.external_activity_calls == 0
    assert panel.capability_sources == original_sources


def _text_variable(label):
    history_tab = next(w.parent for w in _Widget.all
                       if w.options.get('columns') == ('state', 'kind', 'date'))
    for widget in _Widget.all:
        if label in {'历史', '候选'} and widget.parent is not history_tab:
            continue
        value = widget.options.get("textvariable")
        if value is None:
            continue
        current = str(value.get())
        if label == "历史" and "邮件分析阶段已停止" in current:
            return current
        if label == "候选" and ("模型提示：" in current or "草稿" in current
                                or "超时" in current):
            return current
        if label == "任务" and "问题：" in current:
            return current
    raise AssertionError(f"missing {label} status; expected a matching text variable")
