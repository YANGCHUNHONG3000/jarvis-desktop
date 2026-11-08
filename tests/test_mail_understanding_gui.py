"""Offline GUI checks for the explicit cloud mail-understanding launch."""

import json
import sys
from types import ModuleType

from test_history_resume_gui import (
    _Process, _Thread, _Widget, _artifact_ref, _button, _pump, desktop,
)
from test_mail_office_handoff_gui import _complete_mail_export, _task_text
from test_history_resume_gui import _replace_package_module


def _stage_mail_artifact(root, task_root, artifact, digest, monkeypatch):
    from agent_service import artifact_source, mail_understanding

    ref = _artifact_ref(task_root, artifact, digest)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: dict(ref))
    monkeypatch.setattr(mail_understanding, "load_mail",
                        lambda *_args: {"subject": "synthetic", "body": "synthetic"})
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    return ref


def test_test_module_stubs_restore_package_attribute(monkeypatch):
    import agent_service

    name = "agent_service.office_resume"
    sentinel = object()
    old_attr = getattr(agent_service, "office_resume", sentinel)
    old_module = sys.modules.get(name, sentinel)
    replacement = ModuleType(name)
    with monkeypatch.context() as scoped:
        _replace_package_module(scoped, name, replacement)
        assert agent_service.office_resume is replacement
        assert sys.modules[name] is replacement
    assert getattr(agent_service, "office_resume", sentinel) is old_attr
    assert sys.modules.get(name, sentinel) is old_module


def test_explicit_mail_button_revalidates_and_launches_mail_mode(desktop, monkeypatch):
    root, task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    ref = _stage_mail_artifact(root, task_root, artifact, digest, monkeypatch)
    button = _button("理解邮件（云端）")
    assert button.cget("state") == "normal"

    _task_text()
    button.invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    _Thread.run("jarvis-agent")

    command = _Process.created[-1].command
    assert "--mail-understanding" in command
    assert "--source-artifact-json" in command
    assert json.loads(command[command.index("--source-artifact-json") + 1]) == ref
    assert "--source-file" not in command


def test_mail_button_stays_disabled_without_selected_mail_word(desktop):
    _root, _old_root, _state, _tree, _original = desktop
    button = _button("理解邮件（云端）")
    assert button.cget("state") == "disabled"
    button.invoke()
    assert not any("run_jarvis_task.py" in " ".join(p.command) for p in _Process.created)


def test_regular_start_with_mail_word_keeps_normal_task_mode(desktop, monkeypatch):
    root, task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    _stage_mail_artifact(root, task_root, artifact, digest, monkeypatch)
    _task_text()
    _button("开始任务").invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    _Thread.run("jarvis-agent")

    command = _Process.created[-1].command
    assert "--source-artifact-json" in command
    assert "--mail-understanding" not in command


def test_historical_mail_word_enables_explicit_mail_understanding(desktop, monkeypatch):
    from test_history_resume_gui import _history_source_output
    from agent_service import artifact_source, mail_understanding

    root, task_root, state, tree, _original = desktop
    artifact, digest = _history_source_output(root, task_root, state, tree)
    ref = _artifact_ref(task_root, artifact, digest)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: dict(ref))
    monkeypatch.setattr(mail_understanding, "load_mail",
                        lambda *_args: {"subject": "synthetic", "body": "synthetic"})

    _button("用于下一任务").invoke()
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    mail_button = _button("理解邮件（云端）")
    assert mail_button.cget("state") == "normal"

    _task_text()
    mail_button.invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[-1].command
    assert "--mail-understanding" in command
    assert json.loads(command[command.index("--source-artifact-json") + 1]) == ref


def test_historical_regular_word_is_not_mail_but_remains_selectable(desktop, monkeypatch):
    from test_history_resume_gui import _history_source_output
    from agent_service import artifact_source, mail_understanding

    root, task_root, state, tree, _original = desktop
    artifact, digest = _history_source_output(root, task_root, state, tree)
    ref = _artifact_ref(task_root, artifact, digest)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: dict(ref))

    def reject_as_non_mail(*_args):
        raise mail_understanding.MailUnderstandingError("mail_analysis_requires_export")

    monkeypatch.setattr(mail_understanding, "load_mail", reject_as_non_mail)
    _button("用于下一任务").invoke()
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)

    assert _button("理解邮件（云端）").cget("state") == "disabled"
    assert _button("开始任务").cget("state") != "disabled"
    _task_text()
    _button("开始任务").invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[-1].command
    assert "--source-artifact-json" in command
    assert "--mail-understanding" not in command


def test_mail_failure_label_uses_reviewed_reason_without_provider_text():
    from run_jarvis_assistant import _mail_understanding_error_label

    label = _mail_understanding_error_label({
        "error": "mail_analysis_review_rejected",
        "reason": "引用邮件原文未说明负责人",
        "detail": "provider raw secret text",
    })
    assert label.startswith("邮件语义复核未通过。")
    assert "引用邮件原文未说明负责人" in label
    assert "provider raw secret text" not in label
    assert "API_KEY" not in label
    fallback = _mail_understanding_error_label({
        "error": "mail_analysis_api_error",
        "reason": "provider raw secret text",
    })
    assert "provider raw secret text" not in fallback


def test_truncated_mail_response_has_actionable_label_without_raw_provider_content():
    from run_jarvis_assistant import _mail_understanding_error_label
    label = _mail_understanding_error_label({
        "error": "model_response_incomplete", "reason": "private provider text",
        "details": "private email body", "completed": [],
    })
    assert "回复被截断" in label and "未生成办公成果" in label
    assert "失败记录已保留" in label and "重新发起" in label
    assert "private" not in label


def test_mail_clarification_and_unsupported_labels_show_bounded_reason_and_next_action():
    from run_jarvis_assistant import _mail_understanding_error_label

    for code in ("mail_analysis_needs_input", "mail_analysis_unsupported"):
        label = _mail_understanding_error_label({
            "error": code,
            "reason": "请限定主题\n" + "范围" * 200,
            "detail": "provider raw secret text",
        })
        assert "请限定主题" in label
        assert "重新点击“理解邮件”" in label and "原记录保留" in label
        assert "provider raw secret text" not in label
        assert len("范围" * 200) > 160
        reason = label.split("问题：", 1)[1].split("。", 1)[0]
        assert len(reason) <= 160
        assert "\n" not in reason


def test_mail_action_has_own_compact_row_above_original_controls(desktop):
    _root, _task_root, _state, _tree, _original = desktop
    mail_button = _button("理解邮件（云端）")
    task_start = _button("开始任务")
    assert mail_button.parent is not task_start.parent

    mail_row = mail_button.parent
    notice = next(w for w in _Widget.all
                  if w.parent is mail_row
                  and w.options.get("text") == "理解邮件会上传所选邮件完整正文作上下文审查；编辑成果先本地选择范围，规划时才上传")
    assert notice.options.get("wraplength") == 440
    assert _button("编辑本批成果…").parent is mail_row

    controls = task_start.parent
    ordered = [w.options.get("text", "<combobox>")
               for w in _Widget.all if w.parent is controls
               and (w.options.get("text") in {"开始任务", "停止", "打开成果",
                                               "打开目录", "用于下一任务"}
                    or "textvariable" in w.options)]
    assert ordered == ["开始任务", "停止", "打开成果", "<combobox>",
                       "打开目录", "用于下一任务"]
