"""Offline GUI coverage for successful local mail reply drafts."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from test_history_resume_gui import _Process, _Thread, _Widget, _button, _pump, desktop
from test_mail_office_handoff_gui import _complete_mail_export
from test_mail_understanding_gui import _stage_mail_artifact


@pytest.fixture(autouse=True)
def _adapt_unrelated_goal_panel_binding(monkeypatch):
    """The shared fake Goal panel predates this unrelated display binding."""
    import agent_service.office_chain_plan_binding as binding

    class OfflineBinding:
        def __init__(self, goal, chain):
            self.goal, self.chain = goal, chain

    monkeypatch.setattr(binding, "OfficeChainPlanBinding", OfflineBinding)


@pytest.mark.parametrize(
    ("outputs", "expected"),
    [
        ([('s1 · 文档 (.docx)', Path('report.docx'))], False),
        ([('s1 · 未发送邮件草稿', Path('draft.eml'))], True),
        ([('s1 · 文档 (.docx)', Path('report.docx')),
         ('s2 · 未发送邮件草稿', Path('draft.eml'))], True),
    ],
)
def test_success_label_mentions_reply_only_when_eml_is_present(outputs, expected):
    from run_jarvis_assistant import _mail_understanding_success_label

    label = _mail_understanding_success_label(outputs, 2)
    assert ("回复草稿仅保存在本机" in label) is expected
    assert "已核验" in label and "完成步骤 2 个" in label
    if expected:
        assert "尚未发送" in label and "收件人留空" in label


@pytest.mark.parametrize("edit_before_completion", [False, True])
def test_mail_success_previews_local_reply_and_preserves_instruction_draft(
        desktop, monkeypatch, edit_before_completion):
    """A successful mail run exposes its .eml preview, never a sourceable Office input."""
    root, source_task, _state, _tree, _original = desktop
    from run_jarvis_assistant import _read_draft_preview

    _export_root, _mail_task, _mail_artifact, _mail_digest = _complete_mail_export(
        desktop, monkeypatch)
    ref = _stage_mail_artifact(root, source_task, _mail_artifact, _mail_digest, monkeypatch)
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.insert("1.0", "根据邮件拟写简短回复")
    _button("理解邮件（云端）").invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)

    instruction_state = next(
        w.options["textvariable"] for w in _Widget.all
        if w.options.get("textvariable")
        and "完整成功后自动清空未改动的指令" in str(w.options["textvariable"].get())
    )
    process = None

    def create_success_result(proc):
        nonlocal process
        process = proc
        task_root = Path(proc.command[proc.command.index("--task-root") + 1])
        draft_path = task_root / ("workflow-" + "b" * 32) / "s1" / "reply.eml"
        draft_path.parent.mkdir(parents=True)
        draft_bytes = (
            b"X-Unsent: 1\r\nSubject: Re: local message\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Thanks for your note.\r\n"
        )
        draft_path.write_bytes(draft_bytes)
        digest = hashlib.sha256(draft_bytes).hexdigest()
        result = {"root": str(task_root), "kind": "office", "ok": True,
                  "state": "succeeded", "completed": ["s1"],
                  "artifacts": [{"step": "s1", "path": str(draft_path), "sha256": digest}]}
        (task_root / "result.json").write_text(json.dumps(result), encoding="utf-8")
        proc.__class__.reply = json.dumps(result)

    _Process.on_communicate = create_success_result
    _Thread.run("jarvis-agent")
    if edit_before_completion:
        task_text.insert("1.0", "新写的下一封邮件回复要求")
    monkeypatch.setattr(sys.modules["tkinter"], "Toplevel", _Widget, raising=False)
    _pump(root)

    assert process is not None
    assert "--mail-understanding" in process.command
    assert json.loads(process.command[process.command.index("--source-artifact-json") + 1]) == ref
    status = next(w.options["textvariable"].get() for w in _Widget.all
                  if w.options.get("textvariable") and "邮件理解完成" in str(w.options["textvariable"].get()))
    assert "回复草稿仅保存在本机" in status
    assert "尚未发送" in status and "收件人留空" in status

    outputs = next(w for w in _Widget.all if w.options.get("values")
                   and any("未发送邮件草稿" in str(value) for value in w.options["values"]))
    assert outputs.options["values"] == ("s1 · 未发送邮件草稿",)
    _button("打开成果").invoke()
    preview_subject = next(w for w in _Widget.all if w.options.get("text") == "主题：Re: local message")
    assert preview_subject is not None
    preview = _read_draft_preview(
        Path(process.command[process.command.index("--task-root") + 1]) /
        ("workflow-" + "b" * 32) / "s1" / "reply.eml")
    assert preview["to"] == "未填写" and "Thanks for your note." in preview["body"]

    use_source = next(w for w in _Widget.all if w.options.get("text") == "用于下一任务")
    assert use_source.cget("state") == "disabled"
    if edit_before_completion:
        assert task_text.get("1.0", "end-1c") == "新写的下一封邮件回复要求根据邮件拟写简短回复"
        assert "当前新草稿已保留" in instruction_state.get()
    else:
        assert task_text.get("1.0", "end-1c") == ""
        assert "上一指令已完成" in instruction_state.get()


def _verified_record(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def _patch_goal_display_context(monkeypatch, goal):
    from agent_service.office_workspace_panel import OfficeWorkspacePanel

    goal.instruction_draft = type("Draft", (), {"snapshot": lambda _self: {"revision": 0, "text": ""}})()
    goal.plan = None
    goal.mode = type("Mode", (), {"get": lambda _self: "natural_language"})()
    goal.daily_task_route_generation = 0
    goal.capability_source_generation = 0
    goal.stopped = type("Stopped", (), {"is_set": lambda _self: False})()
    monkeypatch.setattr(OfficeWorkspacePanel, "context", lambda _self: {"payload": {}})


def test_main_goal_handoff_accepts_reply_only_for_preview_and_keeps_bad_hash_atomic(
        desktop, monkeypatch):
    _root, task_root, _state, _tree, _original = desktop
    import run_jarvis_assistant as launcher
    from agent_service.office_task_chain_panel import OfficeTaskChainPanel

    monkeypatch.setattr(sys.modules["tkinter"], "Toplevel", _Widget, raising=False)
    announced = []
    monkeypatch.setattr(OfficeTaskChainPanel, "artifacts_announced",
                        lambda _self, records, **kwargs: announced.append((records, kwargs)))
    reply = _verified_record(task_root / "reply-only.eml",
                             b"X-Unsent: 1\r\nSubject: Re: local\r\n\r\nReply text.\r\n")
    goal = _state["goal_panel"]
    _patch_goal_display_context(monkeypatch, goal)

    goal.on_artifacts([reply])

    output_choice = next(w for w in _Widget.all if w.options.get("values") and
                         "未发送邮件草稿" in str(w.options["values"]))
    assert output_choice.options["values"] == ("连续任务 1 · 未发送邮件草稿",)
    assert goal.get_artifacts() == []
    assert not any(name == "jarvis-artifact-source-select" for name in
                   (thread.name for thread in _Thread.all))
    assert any("本地回复草稿已核验，未发送且收件人留空" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    use_source = next(w for w in _Widget.all if w.options.get("text") == "用于下一任务"
                      and w.parent is not None and w.cget("state") == "disabled")
    assert use_source.cget("state") == "disabled"
    _button("打开成果").invoke()
    assert any(w.options.get("text") == "主题：Re: local" for w in _Widget.all)
    assert announced and announced[-1][0] == []

    # The main callback owns the in-memory output list; a bad receipt must not replace it.
    before_choices = output_choice.options["values"]
    before_office = goal.get_artifacts()
    with pytest.raises(ValueError, match="goal_artifact_changed"):
        goal.on_artifacts([{**reply, "sha256": "0" * 64}])
    assert output_choice.options["values"] == before_choices
    assert goal.get_artifacts() == before_office == []
    assert launcher._verified_history_path(reply["path"], reply["sha256"], task_root) is not None


def test_main_goal_handoff_mixed_outputs_only_announce_and_select_office(
        desktop, monkeypatch):
    root, task_root, _state, _tree, _original = desktop
    from agent_service import artifact_source
    from agent_service.office_task_chain_panel import OfficeTaskChainPanel

    announced = []
    monkeypatch.setattr(OfficeTaskChainPanel, "artifacts_announced",
                        lambda _self, records, **kwargs: announced.append((records, kwargs)))
    office = _verified_record(task_root / "handoff-report.docx", b"verified office artifact")
    reply = _verified_record(task_root / "handoff-reply.eml",
                             b"X-Unsent: 1\r\nSubject: Re: local\r\n\r\nReply text.\r\n")
    ref = {"version": 1, "task_root": str(task_root), "workflow_root": str(task_root),
           "step_id": "s1", "artifact": office["path"], "sha256": office["sha256"],
           "receipt_sha256": "a" * 64, "token": "b" * 64}
    prepared = []
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda path, digest, _base: (prepared.append((str(path), digest)), dict(ref))[1])

    _patch_goal_display_context(monkeypatch, _state["goal_panel"])
    _state["goal_panel"].on_artifacts([office, reply])
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)

    output_choice = next(w for w in _Widget.all if w.options.get("values") and
                         "未发送邮件草稿" in str(w.options["values"]))
    assert output_choice.options["values"] == (
        "连续任务 1 · Word", "连续任务 2 · 未发送邮件草稿")
    assert _state["goal_panel"].get_artifacts() == [office]
    assert prepared == [(office["path"], office["sha256"])]
    assert announced and announced[-1][0] == [office]
    source_field = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source_field.options["textvariable"].get() == office["path"]
