"""Offline GUI checks for selected-mail and Office output handoff."""

import hashlib
import json
from pathlib import Path

from test_history_resume_gui import (
    _Process, _Thread, _Widget, _artifact_ref, _button, _pump, desktop,
)


def _task_text():
    widget = next(w for w in _Widget.all if w.options.get("height") == 6)
    widget.get = lambda *_args: "根据所选邮件生成办公文件"
    return widget


def _write_artifact(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _complete_mail_export(desktop, monkeypatch, *, stopped=False):
    root, _old_root, state, _tree, _original = desktop
    from agent_service import mail_export

    monkeypatch.setattr(mail_export, "write_export_request",
                        lambda task_root, base, _snapshot: (task_root.mkdir(parents=True), "a" * 64)[1])
    assert state["on_export"]({"synthetic": "mail snapshot"}) is True
    _Thread.run("jarvis-mail-export-prepare")
    _pump(root)
    _Thread.run("jarvis-mail_export")
    process = _Process.created[-1]
    task_root = Path(process.command[process.command.index("--task-root") + 1])
    artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
    digest = _write_artifact(artifact, b"synthetic verified mail Word")
    result = {"root": str(task_root), "kind": "office", "ok": True,
              "state": "succeeded", "artifacts": [{"step": "s1", "path": str(artifact),
                                                        "sha256": digest}]}
    (task_root / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if stopped:
        _button("停止").invoke()
    state["export_inspection"] = {"root": str(task_root), "kind": "office",
                                  "state": "succeeded", "artifacts": [
                                      {"label": "s1 · 文档 (.docx)", "path": str(artifact),
                                       "sha256": digest}]}
    _pump(root)
    _Thread.run("jarvis-mail-export-reconcile")
    _pump(root)
    return root, task_root, artifact, digest


def test_mail_export_auto_stages_managed_source_without_starting_model(desktop, monkeypatch):
    root, task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    from agent_service import artifact_source

    ref = _artifact_ref(task_root, artifact, digest)
    calls = []
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *args: (calls.append(args), dict(ref))[1])
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)

    assert len(calls) == 1
    assert _Process.created[-1].command[1].endswith("run_jarvis_mail_export.py")
    assert not any("run_jarvis_task.py" in " ".join(p.command) for p in _Process.created)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == str(artifact)
    assert _button("开始任务").cget("state") == "normal"


def test_start_revalidates_automatic_mail_source_and_never_falls_back(desktop, monkeypatch):
    root, task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    from agent_service import artifact_source

    ref = _artifact_ref(task_root, artifact, digest)
    calls = []

    def prepare(*_args):
        calls.append(True)
        if len(calls) == 2:
            raise artifact_source.ArtifactSourceError("changed")
        return dict(ref)

    monkeypatch.setattr(artifact_source, "prepare_artifact_source", prepare)
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    _task_text()
    _button("开始任务").invoke()
    assert _Process.created[-1].command[1].endswith("run_jarvis_mail_export.py")
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    assert len(calls) == 2
    assert len(_Process.created) == 1
    assert not any(t.name == "jarvis-agent" for t in _Thread.all)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == str(artifact)
    _button("开始任务").invoke()
    assert len(_Process.created) == 1
    assert not any(t.name == "jarvis-agent" for t in _Thread.all)
    _button("清除").invoke()
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    assert "--source-file" not in _Process.created[-1].command
    assert "--source-artifact-json" not in _Process.created[-1].command


def test_start_success_passes_managed_artifact_reference(desktop, monkeypatch):
    root, task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    from agent_service import artifact_source

    ref = _artifact_ref(task_root, artifact, digest)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: dict(ref))
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    _task_text()
    _button("开始任务").invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[-1].command
    assert "--source-artifact-json" in command
    assert json.loads(command[command.index("--source-artifact-json") + 1]) == ref
    assert "--source-file" not in command


def test_mail_export_durable_success_after_stop_does_not_auto_bind(desktop, monkeypatch):
    root, _task_root, artifact, _digest = _complete_mail_export(desktop, monkeypatch, stopped=True)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == ""
    assert not any(t.name == "jarvis-artifact-source-select" for t in _Thread.all)
    assert artifact.is_file()  # Durable success remains visible as an output.


def test_failed_mail_auto_selection_clears_previous_source(desktop, monkeypatch, tmp_path):
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    old_source = tmp_path / "older.csv"
    old_source.write_text("a,b\n1,2\n", encoding="utf-8")
    source.options["textvariable"].set(str(old_source))
    root, _task_root, artifact, digest = _complete_mail_export(desktop, monkeypatch)
    from agent_service import artifact_source

    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: (_ for _ in ()).throw(
                            artifact_source.ArtifactSourceError("verification failed")))
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert source.options["textvariable"].get() == str(artifact)
    _task_text()
    _button("开始任务").invoke()
    assert len(_Process.created) == 1
    assert not any(t.name == "jarvis-agent" for t in _Thread.all)
    _button("清除").invoke()
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    command = _Process.created[-1].command
    assert "--source-file" not in command
    assert "--source-artifact-json" not in command


def test_selection_change_while_handoff_pending_discards_old_callback(desktop, monkeypatch):
    root, task_root, first, first_sha = _complete_mail_export(desktop, monkeypatch)
    from agent_service import artifact_source

    second = task_root / ("workflow-" + "e" * 32) / "s2" / "other.docx"
    second_sha = _write_artifact(second, b"other synthetic Word")
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: _artifact_ref(task_root, first, first_sha))
    _Thread.run("jarvis-artifact-source-select")
    # The output selection no longer matches the selection captured for the worker.
    output = next(w for w in _Widget.all if w.options.get("values") == ("已读邮件本机 Word · .docx",))
    output.options["textvariable"].set("different selection")
    _pump(root)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == str(first)
    assert _Process.created[-1].command[1].endswith("run_jarvis_mail_export.py")
    _task_text()
    _button("开始任务").invoke()
    assert len(_Process.created) == 1
    assert not any(t.name == "jarvis-agent" for t in _Thread.all)
    _button("清除").invoke()
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    assert "--source-file" not in _Process.created[-1].command
    assert "--source-artifact-json" not in _Process.created[-1].command


def test_automatic_source_timeout_stays_blocked_until_clear(desktop, monkeypatch):
    root, _task_root, artifact, _digest = _complete_mail_export(desktop, monkeypatch)
    import run_jarvis_assistant as launcher

    monkeypatch.setattr(launcher.time, "monotonic", lambda: 1_000_000_000)
    _pump(root)
    _task_text()
    _button("开始任务").invoke()
    assert len(_Process.created) == 1
    assert not any(t.name == "jarvis-agent" for t in _Thread.all)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == str(artifact)
    _button("清除").invoke()
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    assert "--source-file" not in _Process.created[-1].command
    assert "--source-artifact-json" not in _Process.created[-1].command


def test_pending_source_rejects_second_mail_export(desktop, monkeypatch):
    _root, _task_root, state, _tree, _original = desktop
    _complete_mail_export(desktop, monkeypatch)
    # Successful export has scheduled artifact-source-select, which is still pending.
    assert state["on_export"]({"synthetic": "mail"}) is False
    assert not any(t.name == "jarvis-mail-export-prepare" for t in _Thread.all)


def test_office_success_selects_word_and_keeps_eml_in_output_picker(desktop, monkeypatch):
    root, _old_root, _state, _tree, _original = desktop
    _task_text()

    def durable_office(process):
        task_root = Path(process.command[process.command.index("--task-root") + 1])
        workflow = task_root / ("workflow-" + "e" * 32)
        word = workflow / "s1" / "document.docx"
        draft = workflow / "s2" / "draft.eml"
        word_sha = _write_artifact(word, b"verified Word")
        draft_sha = _write_artifact(draft, b"verified unsent EML")
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": True, "state": "succeeded",
            "completed": ["s1", "s2"], "artifacts": [
                {"step": "s1", "path": str(word), "sha256": word_sha},
                {"step": "s2", "path": str(draft), "sha256": draft_sha}]}), encoding="utf-8")

    _Process.on_communicate = durable_office
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    _pump(root)
    outputs = next(w for w in _Widget.all if "未发送邮件草稿" in str(w.options.get("values")))
    assert len(outputs.options["values"]) == 2
    assert outputs.options["textvariable"].get() == "s1 · 文档 (.docx)"
    assert len([t for t in _Thread.all if t.name == "jarvis-artifact-source-select"]) == 1
    from agent_service import artifact_source
    task_root = Path(_Process.created[-1].command[
        _Process.created[-1].command.index("--task-root") + 1])
    word = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
    digest = hashlib.sha256(word.read_bytes()).hexdigest()
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: _artifact_ref(task_root, word, digest))
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert any("本批邮件草稿仅保存在本机，未发送" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_partial_or_failed_office_does_not_auto_bind_source(desktop):
    root, _old_root, _state, _tree, _original = desktop
    _task_text()

    def partial_office(process):
        task_root = Path(process.command[process.command.index("--task-root") + 1])
        artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
        digest = _write_artifact(artifact, b"partial Word")
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": False, "state": "failed",
            "completed": ["s1"], "artifacts": [
                {"step": "s1", "path": str(artifact), "sha256": digest}]}), encoding="utf-8")

    _Process.on_communicate = partial_office
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    _pump(root)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == ""
    assert not any(t.name == "jarvis-artifact-source-select" for t in _Thread.all)


def test_office_durable_success_after_stop_does_not_auto_bind(desktop):
    root, _old_root, _state, _tree, _original = desktop
    _task_text()

    def durable_office(process):
        task_root = Path(process.command[process.command.index("--task-root") + 1])
        artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
        digest = _write_artifact(artifact, b"durable Word despite stop")
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": True, "state": "succeeded",
            "completed": ["s1"], "artifacts": [
                {"step": "s1", "path": str(artifact), "sha256": digest}]}), encoding="utf-8")

    _Process.on_communicate = durable_office
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    _button("停止").invoke()
    _pump(root)
    source = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source.options["textvariable"].get() == ""
    assert not any(t.name == "jarvis-artifact-source-select" for t in _Thread.all)
