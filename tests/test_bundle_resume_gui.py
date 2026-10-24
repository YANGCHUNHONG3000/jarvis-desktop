"""Offline GUI and CLI routing checks for bundle-owned continuation."""

import json
from pathlib import Path
from types import ModuleType

import run_jarvis_assistant as launcher
from test_history_resume_gui import _Thread, _Widget, _button, _pump, desktop


def test_bundle_history_routes_to_dedicated_cli_and_checks_kind(desktop, monkeypatch):
    root, task_root, state, tree, original = desktop
    state.update(preview_kind="bundle_resume",
                 outcome={"ok": True, "state": "succeeded", "kind": "bundle_resume"})
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    office = _button("继续剩余办公步骤")
    bundle = _button("继续联合核对步骤")
    assert office.cget("state") == "disabled"
    assert bundle.cget("state") == "normal"
    bundle.invoke()
    # The command has not started yet and the GUI must leave no owned attempt dir.
    assert not list(task_root.glob("resume-*"))
    _Thread.run("jarvis-resume")
    command = _Process_created(launcher)[0].command
    assert Path(command[1]).name == "run_jarvis_resume.py"
    assert "--bundle" in command
    attempt_id = command[command.index("--attempt-id") + 1]
    assert not (task_root / ("resume-" + attempt_id)).exists()
    assert (task_root / "result.json").read_bytes() == original

    bundle_module = ModuleType("agent_service.office_bundle_resume")
    bundle_module.read_resume_result = lambda _root, _base, attempt: {
        **state["outcome"], "attempt_id": attempt, "task_root": str(task_root), "artifacts": []}
    monkeypatch.setitem(__import__("sys").modules,
                        "agent_service.office_bundle_resume", bundle_module)
    state["continuation"] = {"state": "succeeded", "attempt_id": attempt_id,
                             "kind": "bundle_resume"}
    state["eligible"] = False
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert any("本次续跑已完成" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def _Process_created(_launcher):
    from test_history_resume_gui import _Process
    return _Process.created


def test_bundle_cli_flag_dispatches_without_changing_default(monkeypatch, tmp_path, capsys):
    import run_jarvis_resume as cli
    bundle = ModuleType("agent_service.office_bundle_resume")
    calls = []
    bundle.resume_task = lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "ok": True, "state": "succeeded", "kind": "bundle_resume"}
    monkeypatch.setitem(__import__("sys").modules, "agent_service.office_bundle_resume", bundle)
    code = cli.main(["--task-root", str(tmp_path / "task"), "--token", "a" * 64,
                     "--attempt-id", "b" * 32, "--bundle"], repo_root=tmp_path)
    assert code == 0
    assert calls and calls[0][0][0] == str(tmp_path / "task")
    assert calls[0][0][1] == tmp_path / "data" / "assistant_tasks"
    assert calls[0][0][2:] == ("a" * 64, "b" * 32)
    assert json.loads(capsys.readouterr().out)["kind"] == "bundle_resume"


def test_finished_non_resume_process_keeps_ordinary_history_resume_available(desktop):
    root, _task_root, state, _tree, _original = desktop
    state["preview_kind"] = None  # Legacy Office previews intentionally omit kind.
    note_text = next(widget for widget in _Widget.all if widget.options.get("height") == 11)
    note_text.insert("1.0", "audit test")
    _button("开始记事本任务").invoke()
    _Thread.run("jarvis-notepad")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "normal"


def test_bundle_start_failure_leaves_no_empty_attempt_directory(desktop, monkeypatch):
    root, task_root, state, tree, _original = desktop
    state["preview_kind"] = "bundle_resume"
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _button("继续联合核对步骤").invoke()
    commands = []
    def fail_start(*_args, **_kwargs):
        commands.append(_args[0])
        raise OSError("simulated process startup failure")

    monkeypatch.setattr(launcher.subprocess, "Popen", fail_start)
    _Thread.run("jarvis-resume")
    _pump(root)
    attempt_id = commands[0][commands[0].index("--attempt-id") + 1]
    assert not (task_root / ("resume-" + attempt_id)).exists()


def test_bundle_stop_before_attempt_creation_marks_owned_directory_when_it_appears(desktop):
    root, task_root, state, tree, _original = desktop
    state["preview_kind"] = "bundle_resume"
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _button("继续联合核对步骤").invoke()
    _Thread.run("jarvis-resume")
    process = _Process_created(launcher)[0]
    attempt_id = process.command[process.command.index("--attempt-id") + 1]
    attempt = task_root / ("resume-" + attempt_id)
    assert not attempt.exists()
    before = len(root.afters)
    _button("停止继续执行").invoke()
    assert len(root.afters) > before
    attempt.mkdir()
    root.afters[-1]()
    assert (attempt / "cancel.flag").is_file()


def test_ordinary_history_resume_keeps_precreated_attempt_contract(desktop):
    root, task_root, state, _tree, _original = desktop
    state["preview_kind"] = None
    _button("继续剩余办公步骤").invoke()
    _Thread.run("jarvis-resume")
    process = _Process_created(launcher)[0]
    command = process.command
    attempt_id = command[command.index("--attempt-id") + 1]
    assert "--bundle" not in command
    assert (task_root / ("resume-" + attempt_id)).is_dir()
