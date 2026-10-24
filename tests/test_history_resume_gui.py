"""Offline desktop continuation tests: no Office app or model calls."""

import json
import hashlib
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

import run_jarvis_assistant as launcher


class _Variable:
    def __init__(self, value=None):
        self.value = value
        self.traces = []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in self.traces:
            callback()

    def trace_add(self, _mode, callback):
        self.traces.append(callback)
        return "trace"


class _Widget:
    all = []

    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.options = dict(kwargs)
        self.items = {}
        self.selected = ()
        self.callbacks = {}
        self.afters = []
        self.destroyed = False
        self.text = ""
        self.__class__.all.append(self)

    def pack(self, *_args, **_kwargs):
        pass

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def cget(self, key):
        return self.options.get(key)

    def title(self, *_args):
        pass

    def geometry(self, *_args):
        pass

    def protocol(self, name, callback):
        self.callbacks[name] = callback

    def after(self, _ms, callback):
        self.afters.append(callback)

    def mainloop(self):
        pass

    def destroy(self):
        self.destroyed = True

    def add(self, *_args, **_kwargs):
        pass

    def heading(self, *_args, **_kwargs):
        pass

    def column(self, *_args, **_kwargs):
        pass

    def bind(self, name, callback):
        self.callbacks[name] = callback

    def insert(self, _parent, _where, iid=None, values=None):
        if self.options.get("wrap") == "word" and iid is None:
            if _parent == "1.0":
                self.text = str(_where) + self.text
            else:
                self.text += str(_where)
            return
        if iid is not None:
            self.items[iid] = values

    def delete(self, item, _end=None):
        if self.options.get("wrap") == "word":
            self.text = ""
            return
        self.items.pop(item, None)
        if item in self.selected:
            self.selected = ()

    def get_children(self):
        return tuple(self.items)

    def selection(self):
        return self.selected

    def selection_set(self, item):
        self.selected = (item,)

    def select(self, item):
        if "<<TreeviewSelect>>" in self.callbacks:
            self.selected = (item,)
            self.callbacks["<<TreeviewSelect>>"]()

    def get(self, *_args):
        return self.text if self.options.get("wrap") == "word" else ""

    def invoke(self):
        if self.options.get("state") != "disabled":
            return self.options["command"]()


class _Thread:
    all = []

    def __init__(self, target=None, name="", **_kwargs):
        self.target = target
        self.name = name

    def start(self):
        self.__class__.all.append(self)

    @classmethod
    def run(cls, name):
        for index, thread in enumerate(cls.all):
            if thread.name == name:
                cls.all.pop(index)
                thread.target()
                return
        raise AssertionError(f"thread {name!r} was not scheduled")


class _Process:
    created = []
    reply = '{"ok": true, "state": "succeeded"}'
    on_communicate = None

    def __init__(self, command, **_kwargs):
        self.command = command
        self.returncode = None
        self.terminated = False
        self.__class__.created.append(self)

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.__class__.on_communicate is not None:
            self.__class__.on_communicate(self)
        self.returncode = 1 if self.terminated else 0
        return (self.__class__.reply, None)

    def terminate(self):
        self.terminated = True
        self.returncode = 1


def _button(text):
    return next(widget for widget in _Widget.all if widget.options.get("text") == text)


def _pump(root):
    assert root.afters
    root.afters.pop(0)()


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    task_base = tmp_path / "assistant_tasks"
    task_base.mkdir()
    task_id = "task-" + "a" * 32
    task_root = task_base / task_id
    task_root.mkdir()
    original = b'{"root":"original immutable result","kind":"office","state":"cancelled"}'
    (task_root / "result.json").write_bytes(original)
    _Widget.all = []
    _Thread.all = []
    _Process.created = []
    _Process.reply = '{"ok": true, "state": "succeeded"}'
    _Process.on_communicate = None
    tk = ModuleType("tkinter")
    tk.Tk = _Widget
    tk.StringVar = _Variable
    tk.IntVar = _Variable
    tk.BooleanVar = _Variable
    tk.Text = _Widget
    ttk = ModuleType("tkinter.ttk")
    for name in ("Notebook", "Frame", "Label", "Entry", "Combobox", "Button", "Treeview", "Checkbutton"):
        setattr(ttk, name, _Widget)
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    monkeypatch.setattr(launcher.threading, "Thread", _Thread)
    monkeypatch.setattr(launcher.subprocess, "Popen", _Process)
    monkeypatch.setattr(launcher, "TASK_ROOT", task_base)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "REQUESTS", tmp_path / "user_requests")
    monkeypatch.setattr(launcher, "PYTHON", "python-test")

    from agent_service import task_history, mailbox_panel, budget

    callback_box = {"value": None}

    def attach_mailbox(*args):
        callback_box["value"] = args[2] if len(args) > 2 else None
        return None

    monkeypatch.setattr(mailbox_panel, "attach_mailbox_tab", attach_mailbox)
    monkeypatch.setattr(budget, "BudgetLedger", lambda: type("B", (), {"snapshot": lambda self: {
        "charged_micro": 0, "remaining_micro": 5_000_000, "held_micro": 0}})())
    from agent_service import computer_panel, computer_goal_panel

    class Panel:
        running = False

        def __init__(self, tab):
            self.tab = tab

        def stop(self):
            pass

        def close(self):
            pass

        def use_generated_artifacts(self, _records):
            pass

    monkeypatch.setattr(computer_panel, "attach_computer_tab",
                        lambda tab, *_args, **_kwargs: Panel(tab))
    monkeypatch.setattr(computer_goal_panel, "attach_goal_tab",
                        lambda tab, *_args, **_kwargs: Panel(tab))
    state = {"eligible": True, "reason": "ready", "continuation": None,
             "original_state": "cancelled",
             "preview_kind": None,
             "outcome": {"ok": True, "state": "succeeded"}, "inspected": [],
             "export_inspection": None, "history_artifacts": []}
    monkeypatch.setattr(task_history, "list_tasks", lambda *_args, **_kwargs: [
        {"id": task_id, "root": str(task_root), "state": "cancelled",
         "kind": "office", "mtime": 1.0}])

    def inspect(*_args):
        state["inspected"].append(True)
        if Path(_args[0]) != task_root:
            return state["export_inspection"]
        preview = {"eligible": state["eligible"], "reason": state["reason"],
                   "task_root": str(task_root), "workflow_root": str(task_root / ("workflow-" + "b" * 32)),
                   "token": "c" * 64, "completed": 1, "total": 3, "remaining": 2}
        if state["preview_kind"] is not None:
            preview["kind"] = state["preview_kind"]
        answer = {"root": str(task_root), "kind": "office", "state": state["original_state"],
                  "original_state": state["original_state"],
                  "artifacts": state["history_artifacts"],
                  "resume": preview}
        if state["continuation"] is not None:
            answer["continuation"] = state["continuation"]
        return answer

    monkeypatch.setattr(task_history, "inspect_task", inspect)
    resume_module = ModuleType("agent_service.office_resume")

    def read_result(_root, _base, attempt_id):
        return {**state["outcome"], "attempt_id": attempt_id}

    resume_module.read_resume_result = read_result
    monkeypatch.setitem(sys.modules, "agent_service.office_resume", resume_module)
    skill_resume_module = ModuleType("agent_service.skill_resume")

    def read_skill_result(_root, attempt_root, _base):
        return {**state["outcome"], "kind": "skill_resume",
                "attempt_id": Path(attempt_root).name.removeprefix("skill-resume-")}

    skill_resume_module.read_resume_result = read_skill_result
    monkeypatch.setitem(sys.modules, "agent_service.skill_resume", skill_resume_module)
    launcher.main([])
    state["on_export"] = callback_box["value"]
    root = _Widget.all[0]
    _Thread.run("jarvis-history-list")
    _pump(root)
    tree = next(widget for widget in _Widget.all
                if widget.options.get("columns") == ("state", "kind", "date")
                and "<<TreeviewSelect>>" in widget.callbacks)
    tree.select(task_id)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    return root, task_root, state, tree, original


def test_resume_runs_only_dedicated_cli_and_reconciles_durable_outcome(desktop):
    root, task_root, state, _tree, original = desktop
    button = _button("继续剩余办公步骤")
    assert button.cget("state") == "normal"
    button.invoke()
    assert "正在继续 2 个办公步骤" in next(w for w in _Widget.all if w.options.get("textvariable") and "正在继续" in str(w.options["textvariable"].get())).options["textvariable"].get()
    _Thread.run("jarvis-resume")
    assert len(_Process.created) == 1
    command = _Process.created[0].command
    assert Path(command[1]).name == "run_jarvis_resume.py"
    assert "run_jarvis_task.py" not in " ".join(command)
    assert "--token" in command and "--attempt-id" in command
    attempt_id = command[command.index("--attempt-id") + 1]
    state["continuation"] = {"state": "succeeded", "attempt_id": attempt_id}
    state["eligible"] = False
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert (task_root / "result.json").read_bytes() == original
    assert "本次续跑已完成" in next(w.options["textvariable"].get() for w in _Widget.all
                           if w.options.get("textvariable") and "本次续跑已完成" in str(w.options["textvariable"].get()))
    assert button.cget("state") == "disabled"


def test_skill_resume_uses_dedicated_cli_and_durable_reader(desktop):
    root, task_root, state, tree, original = desktop
    state["preview_kind"] = "skill_resume"
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"
    button = _button("继续剩余技能步骤")
    assert button.cget("state") == "normal"
    button.invoke()
    assert _button("开始任务").cget("state") == "disabled"
    assert _button("运行本地技能（不调用模型）").cget("state") == "disabled"
    _Thread.run("jarvis-resume")
    command = _Process.created[0].command
    assert Path(command[1]).name == "run_jarvis_skill_resume.py"
    assert command[command.index("--token") + 1] == "c" * 64
    attempt_id = command[command.index("--attempt-id") + 1]
    attempt = task_root / ("skill-resume-" + attempt_id)
    assert attempt.is_dir()
    assert not list(task_root.glob("resume-" + attempt_id))
    state["continuation"] = {"state": "succeeded", "attempt_id": attempt_id,
                             "kind": "skill_resume"}
    state["eligible"] = False
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert (task_root / "result.json").read_bytes() == original
    assert any("本次续跑已完成" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_skill_resume_stop_marks_only_owned_attempt(desktop):
    root, task_root, state, tree, original = desktop
    state["preview_kind"] = "skill_resume"
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _button("继续剩余技能步骤").invoke()
    attempt = next(task_root.glob("skill-resume-*"))
    _button("停止继续执行").invoke()
    assert (attempt / "cancel.flag").is_file()
    assert not (task_root / "cancel.flag").exists()
    assert (task_root / "result.json").read_bytes() == original


def test_skill_resume_requires_permit_and_verified_durable_outcome(desktop):
    root, task_root, state, tree, original = desktop
    state.update(preview_kind="skill_resume", eligible=False,
                 reason="skill_resume_permit_invalid")
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    button = _button("继续剩余技能步骤")
    assert button.cget("state") == "disabled"
    button.invoke()
    assert _Process.created == []
    state.update(eligible=True, reason="ready", outcome={"ok": False, "state": "unknown"})
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    button.invoke()
    _Thread.run("jarvis-resume")
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert (task_root / "result.json").read_bytes() == original
    assert not any("本次续跑已完成" in str(w.options["textvariable"].get())
                   for w in _Widget.all if w.options.get("textvariable"))


def test_blocked_preview_cannot_launch(desktop):
    root, _task_root, state, tree, _original = desktop
    state["eligible"] = False
    state["reason"] = "unknown_effect"
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    button = _button("继续剩余办公步骤")
    assert button.cget("state") == "disabled"
    button.invoke()
    assert not _Process.created
    assert not any(thread.name == "jarvis-resume" for thread in _Thread.all)


def test_verified_unsupported_history_shows_recorded_candidate_and_prepares_new_task(
        desktop, monkeypatch):
    root, task_root, _state, tree, original = desktop
    from agent_service import task_history
    gap_module = ModuleType("agent_service.gap_recovery")
    preview = {"eligible": True, "reason": "ready", "task_root": str(task_root),
               "token": "b" * 64, "instruction": "原请求：仅生成 Excel",
               "source_kind": "table", "candidates": [{
                   "id": "table-office-copy", "name": "表格原样复制",
                   "status": "available", "newly_available": True}]}
    gap_module.preview_retry = lambda *_args: dict(preview)
    gap_module.prepare_retry = lambda *_args: dict(preview)
    monkeypatch.setitem(sys.modules, "agent_service.gap_recovery", gap_module)

    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: {
        "root": str(task_root), "kind": "unsupported", "state": "unsupported",
        "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
        "capability_gap": {"candidates": [{"id": "table-office-copy",
                                           "name": "表格原样复制", "status": "available"}],
                           "development_queued": True, "sha256": "a" * 64}})
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("查看候选／准备新任务").cget("state") == "disabled"
    _Thread.run("jarvis-gap-preview")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"
    prepare = _button("查看候选／准备新任务")
    assert prepare.cget("state") == "normal"
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert "表格原样复制（新可用线索）" in visible
    prepare.invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert _Process.created == []
    assert (task_root / "result.json").read_bytes() == original
    assert next(w for w in _Widget.all if w.options.get("height") == 6).text == preview["instruction"]
    assert any("原未支持请求已恢复为草稿" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def _gap_desktop(desktop, monkeypatch, *, eligible=True, source_kind="table",
                 reason=None, candidates=None):
    root, task_root, _state, tree, original = desktop
    from agent_service import task_history
    history = {"root": str(task_root), "kind": "unsupported", "state": "unsupported",
               "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
               "capability_gap": {"candidates": [], "development_queued": True,
                                  "sha256": "a" * 64}}
    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: dict(history))
    preview = {"eligible": eligible, "reason": reason or ("ready" if eligible else "gap_source_changed"),
               "task_root": str(task_root), "source_kind": source_kind,
               "candidates": candidates if candidates is not None else [{"id": "table-office-copy", "name": "表格副本",
                               "status": "available", "newly_available": True},
                              {"id": "other-disabled", "name": "旧候选",
                               "status": "disabled", "newly_available": False},
                              {"id": "wrong-source", "name": "文档候选",
                               "status": "incompatible", "newly_available": False}]}
    if eligible:
        preview.update(token="b" * 64, instruction="把旧表格转成新 Excel")
    events = {"preview": 0, "prepare": 0, "prepared": None}
    module = ModuleType("agent_service.gap_recovery")

    def read(*_args):
        events["preview"] += 1
        return dict(preview)

    def prepare(_task, _base, token):
        events["prepare"] += 1
        assert token == preview.get("token")
        return dict(events["prepared"] or preview)

    module.preview_retry = read
    module.prepare_retry = prepare
    monkeypatch.setitem(sys.modules, "agent_service.gap_recovery", module)
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _Thread.run("jarvis-gap-preview")
    _pump(root)
    return root, task_root, tree, original, preview, events


def test_gap_prepare_restores_draft_then_paid_start_uses_only_bound_retry_ref(desktop, monkeypatch):
    root, old_root, _tree, original, preview, events = _gap_desktop(desktop, monkeypatch)
    detail = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                       if w.options.get("textvariable"))
    assert "新可用线索" in detail and "当前停用" in detail and "来源不匹配" in detail
    prepare = _button("查看候选／准备新任务")
    prepare.invoke()
    prepare.invoke()
    assert len([thread for thread in _Thread.all if thread.name == "jarvis-gap-prepare"]) == 1
    assert _Process.created == [] and (old_root / "result.json").read_bytes() == original
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert events["prepare"] == 1
    task_editor = next(w for w in _Widget.all if w.options.get("height") == 6)
    assert task_editor.text == preview["instruction"]
    assert _button("运行本地技能（不调用模型）").cget("state") == "disabled"
    assert prepare.cget("state") == "disabled"
    _button("开始任务").invoke()
    assert _Process.created == []
    _Thread.run("jarvis-gap-start")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert Path(command[1]).name == "run_jarvis_task.py"
    assert "--source-retry-json" in command
    assert "--source-file" not in command and "--source-artifact-json" not in command
    assert json.loads(command[command.index("--source-retry-json") + 1]) == {
        "task_root": str(old_root), "token": preview["token"]}
    new_root = Path(command[command.index("--task-root") + 1])
    assert new_root != old_root and new_root.is_dir()
    assert (new_root / "request.txt").read_text(encoding="utf-8") == preview["instruction"]
    assert (old_root / "result.json").read_bytes() == original


def test_gap_changed_draft_or_stale_prepare_never_overwrites_user_input(desktop, monkeypatch):
    root, old_root, _tree, original, _preview, _events = _gap_desktop(desktop, monkeypatch)
    task_editor = next(w for w in _Widget.all if w.options.get("height") == 6)
    prepare = _button("查看候选／准备新任务")
    prepare.invoke()
    task_editor.insert("1.0", "用户自己的新草稿")
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert task_editor.text == "用户自己的新草稿"
    assert _Process.created == [] and (old_root / "result.json").read_bytes() == original


def test_gap_bound_instruction_edit_clears_retry_and_blocks_first_start(desktop, monkeypatch):
    root, old_root, _tree, original, _preview, _events = _gap_desktop(desktop, monkeypatch)
    _button("查看候选／准备新任务").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    task_editor = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_editor.delete("1.0", "end")
    task_editor.insert("1.0", "改过的任务")
    task_editor.callbacks["<KeyRelease>"]()
    _button("开始任务").invoke()
    assert not any(thread.name == "jarvis-gap-start" for thread in _Thread.all)
    assert _Process.created == []
    assert len(list(old_root.parent.glob("task-*"))) == 1
    assert (old_root / "result.json").read_bytes() == original
    assert any("绑定已取消" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_gap_cancel_and_stale_token_block_prepare_and_paid_start(desktop, monkeypatch):
    root, old_root, _tree, original, preview, events = _gap_desktop(desktop, monkeypatch)
    prepare = _button("查看候选／准备新任务")
    prepare.invoke()
    _button("取消缺口核验").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert _Process.created == []
    assert next(w for w in _Widget.all if w.options.get("height") == 6).text == ""
    assert (old_root / "result.json").read_bytes() == original
    assert prepare.cget("state") == "disabled"


def test_gap_filter_reads_200_and_shows_older_unsupported_without_touching_record(
        desktop, monkeypatch):
    root, old_root, _state, tree, original = desktop
    from agent_service import task_history
    calls = []
    records = [{"id": f"task-{index:032x}", "root": str(old_root.parent / f"task-{index:032x}"),
                "state": "succeeded", "kind": "office", "mtime": 1000 - index}
               for index in range(35)]
    records[31] = {"id": old_root.name, "root": str(old_root), "state": "unsupported",
                   "kind": "unsupported", "mtime": 1}
    def list_tasks(_base, limit=30):
        calls.append(limit)
        return records[:limit]
    monkeypatch.setattr(task_history, "list_tasks", list_tasks)
    checkbox = next(w for w in _Widget.all if w.options.get("text") ==
                    "仅看未支持任务／能力缺口")
    checkbox.options["variable"].set(True)
    checkbox.invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 200
    assert tree.get_children() == (old_root.name,)
    assert (old_root / "result.json").read_bytes() == original


def test_gap_ineligible_preview_shows_clue_but_never_prepares_or_executes(desktop, monkeypatch):
    root, old_root, _tree, original, _preview, events = _gap_desktop(
        desktop, monkeypatch, eligible=False)
    assert _button("查看候选／准备新任务").cget("state") == "disabled"
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert "当前可用线索" not in visible  # This candidate is newly available, still only a clue.
    assert "新可用线索" in visible and "原任务资料已变化" in visible
    _button("查看候选／准备新任务").invoke()
    assert events["prepare"] == 0 and _Process.created == []
    assert (old_root / "result.json").read_bytes() == original


def test_gap_stale_token_at_start_never_creates_new_task_or_calls_model(desktop, monkeypatch):
    root, old_root, _tree, original, preview, events = _gap_desktop(desktop, monkeypatch)
    _button("查看候选／准备新任务").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    events["prepared"] = {**preview, "token": "c" * 64}
    _button("开始任务").invoke()
    _Thread.run("jarvis-gap-start")
    _pump(root)
    assert _Process.created == []
    assert len(list(old_root.parent.glob("task-*"))) == 1
    assert (old_root / "result.json").read_bytes() == original
    assert any("开始前已变化" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_gap_no_source_ref_still_uses_bound_retry_and_no_plain_source(desktop, monkeypatch):
    root, old_root, _tree, original, preview, _events = _gap_desktop(
        desktop, monkeypatch, source_kind=None)
    _button("查看候选／准备新任务").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert any("原任务没有资料" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    _button("开始任务").invoke()
    _Thread.run("jarvis-gap-start")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert "--source-retry-json" in command and "--source-file" not in command
    assert json.loads(command[-1]) == {"task_root": str(old_root), "token": preview["token"]}
    assert (old_root / "result.json").read_bytes() == original


def test_gap_stop_during_paid_start_prep_ignores_late_readback(desktop, monkeypatch):
    root, old_root, _tree, original, _preview, _events = _gap_desktop(desktop, monkeypatch)
    _button("查看候选／准备新任务").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    _button("开始任务").invoke()
    _button("停止").invoke()
    _Thread.run("jarvis-gap-start")
    _pump(root)
    assert _Process.created == []
    assert len(list(old_root.parent.glob("task-*"))) == 1
    assert (old_root / "result.json").read_bytes() == original


def test_gap_prepare_timeout_discards_late_callback_and_preserves_draft(desktop, monkeypatch):
    root, old_root, _tree, original, _preview, _events = _gap_desktop(desktop, monkeypatch)
    clock = {"now": 0.0}
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock["now"])
    _button("查看候选／准备新任务").invoke()
    clock["now"] = 31.0
    _pump(root)
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    assert next(w for w in _Widget.all if w.options.get("height") == 6).text == ""
    assert _Process.created == []
    assert (old_root / "result.json").read_bytes() == original


def test_gap_preview_groups_versions_and_shows_available_family(desktop, monkeypatch):
    versions = [{"id": "older-family", "name": "旧版本", "status": "disabled",
                 "newly_available": False} for _ in range(8)]
    versions += [{"id": "new-family", "name": "可用新技能", "status": "available",
                  "newly_available": True}]
    root, _old_root, _tree, _original, _preview, _events = _gap_desktop(
        desktop, monkeypatch, candidates=versions)
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert "已核验 9 个候选版本，覆盖 2 个技能家族" in visible
    assert "可用新技能（新可用线索）" in visible
    assert visible.count("旧版本（当前停用）") == 1


@pytest.mark.parametrize(("reason", "expected"), [
    ("gap_instruction_missing", "原任务描述缺失"),
    ("gap_source_changed", "原任务资料已变化"),
    ("gap_effect_unknown", "结果未知的操作"),
])
def test_gap_preview_explains_specific_recovery_rejection(
        desktop, monkeypatch, reason, expected):
    root, _old_root, _tree, _original, _preview, _events = _gap_desktop(
        desktop, monkeypatch, eligible=False, reason=reason)
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert expected in visible
    assert _button("查看候选／准备新任务").cget("state") == "disabled"


def test_gap_history_selection_during_start_discards_late_retry(
        desktop, monkeypatch):
    root, old_root, tree, original, _preview, _events = _gap_desktop(
        desktop, monkeypatch)
    _button("查看候选／准备新任务").invoke()
    _Thread.run("jarvis-gap-prepare")
    _pump(root)
    _button("开始任务").invoke()
    tree.select(old_root.name)  # Same row, but a new history-selection generation.
    _Thread.run("jarvis-gap-start")
    _pump(root)
    assert _Process.created == []
    assert len(list(old_root.parent.glob("task-*"))) == 1
    assert (old_root / "result.json").read_bytes() == original
    assert _button("开始任务").cget("state") == "normal"


def test_unknown_agent_then_gap_preview_timeout_keeps_poll_running(
        desktop, monkeypatch):
    root, old_root, _state, tree, original = desktop
    editor = next(widget for widget in _Widget.all if widget.options.get("height") == 6)
    editor.insert("1.0", "新建受控办公任务")

    def uncertain(process):
        command = process.command
        task_root = Path(command[command.index("--task-root") + 1])
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": False,
            "state": "unknown", "error": "office_timeout", "artifacts": []}),
            encoding="utf-8")

    _Process.on_communicate = uncertain
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    _pump(root)
    assert root.afters  # Unknown office result did not stop the Tk poll.

    from agent_service import task_history
    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: {
        "root": str(old_root), "kind": "unsupported", "state": "unsupported",
        "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
        "capability_gap": {"candidates": [], "sha256": "a" * 64}})
    clock = {"now": 0.0}
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock["now"])
    tree.select(old_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert any(thread.name == "jarvis-gap-preview" for thread in _Thread.all)
    clock["now"] = 31.0
    _pump(root)
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert "缺口与当前候选核验超时" in visible
    assert _button("查看候选／准备新任务").cget("state") == "disabled"
    assert root.afters
    assert (old_root / "result.json").read_bytes() == original


def test_poll_rearms_after_callback_exception_without_hiding_it(desktop, monkeypatch):
    root, old_root, _state, tree, _original = desktop
    from agent_service import task_history
    clock = {"now": 0.0}
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock["now"])

    class BadId(str):
        def __lt__(self, other):
            raise RuntimeError("synthetic Tk callback error")

    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: {
        "root": str(old_root), "kind": "unsupported", "state": "unsupported",
        "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
        "capability_gap": {"candidates": [], "sha256": "a" * 64}})
    module = ModuleType("agent_service.gap_recovery")
    module.preview_retry = lambda *_args: {
        "eligible": False, "reason": "gap_source_changed", "task_root": str(old_root),
        "candidates": [{"id": BadId("first"), "name": "one", "status": "available"},
                       {"id": BadId("second"), "name": "two", "status": "available"}]}
    monkeypatch.setitem(sys.modules, "agent_service.gap_recovery", module)
    tree.select(old_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _Thread.run("jarvis-gap-preview")
    with pytest.raises(RuntimeError, match="synthetic Tk callback error"):
        _pump(root)
    assert root.afters  # The exception remains visible, but deadlines keep running.
    clock["now"] = 31.0
    _pump(root)
    assert root.afters
    assert any("缺口与当前候选核验超时" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


@pytest.mark.parametrize(("code", "expected"), [
    ("skill_document_table_required", "没有可读取的唯一表格"),
    ("source_existing_total_unsupported", "已有合计行，不能再次对它求和"),
    ("source_numeric_cell_invalid", "含缺失或含糊的文本"),
    ("source_numeric_values_invalid", "数据或精度不适合可靠求和"),
    ("source_numeric_columns_invalid", "数字列与求和列不匹配"),
])
def test_verified_skill_input_failure_explains_new_task_without_resume(
        desktop, monkeypatch, code, expected):
    root, task_root, _state, tree, original = desktop
    from agent_service import task_history

    monkeypatch.setattr(task_history, "inspect_task", lambda *_args: {
        "root": str(task_root), "kind": "skill", "state": "failed",
        "artifacts": [], "resume": {"eligible": False, "reason": "not_office"},
        "input_failure": {"code": code, "details": "synthetic-secret-value",
                          "skill_id": "document-table-office-package", "mode": "active"}})
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"
    assert _button("查看候选／准备新任务").cget("state") == "normal"
    visible = "\n".join(str(w.options["textvariable"].get()) for w in _Widget.all
                        if w.options.get("textvariable"))
    assert expected in visible
    assert "synthetic-secret-value" not in visible
    assert "作用尚未开始" in visible
    assert "原失败结果保留" in visible
    assert _Process.created == []
    assert (task_root / "result.json").read_bytes() == original


def test_successful_original_task_has_no_continue_action(desktop):
    root, _task_root, state, tree, _original = desktop
    state["original_state"] = "succeeded"
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"


def test_cancel_and_window_close_only_touch_attempt_flag(desktop):
    root, task_root, _state, _tree, original = desktop
    _button("继续剩余办公步骤").invoke()
    attempt_root = next(task_root.glob("resume-*"))
    _button("停止继续执行").invoke()
    assert (attempt_root / "cancel.flag").is_file()
    assert not (task_root / "cancel.flag").exists()
    root.callbacks["WM_DELETE_WINDOW"]()
    assert not root.destroyed
    assert (task_root / "result.json").read_bytes() == original


def test_missing_durable_result_never_promotes_stdout_success(desktop):
    root, _task_root, state, _tree, _original = desktop
    state["outcome"] = {"ok": False, "state": "unknown"}
    _button("继续剩余办公步骤").invoke()
    assert _button("开始任务").cget("state") == "disabled"
    assert _button("开始记事本任务").cget("state") == "disabled"
    assert _button("开始查资料").cget("state") == "disabled"
    _Thread.run("jarvis-resume")
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert not any("本次续跑已完成" in str(w.options["textvariable"].get())
                   for w in _Widget.all if w.options.get("textvariable"))
    assert _button("开始任务").cget("state") == "normal"


def test_blocked_cli_hint_is_safe_and_does_not_claim_success(desktop):
    root, _task_root, state, _tree, _original = desktop
    state["outcome"] = {"ok": False, "state": "unknown"}
    _Process.reply = '{"ok":false,"state":"blocked","error":"resume_preview_stale"}'
    _button("继续剩余办公步骤").invoke()
    _Thread.run("jarvis-resume")
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert any("任务记录已变化" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert not any("本次续跑已完成" in str(w.options["textvariable"].get())
                   for w in _Widget.all if w.options.get("textvariable"))


def test_inspection_queued_before_resume_cannot_override_progress(desktop):
    root, _task_root, _state, tree, _original = desktop
    tree.select(tree.selection()[0])
    tree.select(tree.selection()[0])
    newer = next(thread for thread in reversed(_Thread.all) if thread.name == "jarvis-history-inspect")
    _Thread.all.remove(newer)
    newer.target()
    _pump(root)
    _button("继续剩余办公步骤").invoke()
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"
    assert any("正在继续 2 个办公步骤" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_old_inspection_does_not_replace_new_selection(desktop):
    root, _task_root, state, tree, _original = desktop
    state["eligible"] = False
    state["reason"] = "unknown_effect"
    tree.select(tree.selection()[0])
    state["eligible"] = True
    state["reason"] = "ready"
    tree.select(tree.selection()[0])
    assert len([thread for thread in _Thread.all if thread.name == "jarvis-history-inspect"]) == 2
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "disabled"
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    assert _button("继续剩余办公步骤").cget("state") == "normal"


def test_hidden_tk_resume_controls_are_packed_together(tmp_path):
    # Tcl's process globals can be affected by the fake tkinter modules above;
    # isolate this real widget build in a child process.
    source = r'''
import json
import tkinter as tk
from tkinter import ttk
import run_jarvis_assistant as launcher
from agent_service import budget, mailbox_panel
from pathlib import Path
real_tk = tk.Tk
roots = []
def hidden_tk():
    root = real_tk()
    root.withdraw()
    root.mainloop = lambda: None
    roots.append(root)
    return root
class NoStartThread:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass
tk.Tk = hidden_tk
launcher.threading.Thread = NoStartThread
launcher.TASK_ROOT = Path(__TASK_BASE__)
mailbox_panel.attach_mailbox_tab = lambda *args: None
budget.BudgetLedger = lambda: type('B', (), {'snapshot': lambda self: {
    'charged_micro': 0, 'remaining_micro': 5000000, 'held_micro': 0}})()
launcher.main([])
root = roots[0]
def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)
buttons = [widget for widget in descendants(root) if isinstance(widget, ttk.Button)]
resume = next(widget for widget in buttons if widget.cget('text') == '继续剩余办公步骤')
skill_resume = next(widget for widget in buttons if widget.cget('text') == '继续剩余技能步骤')
stop = next(widget for widget in buttons if widget.cget('text') == '停止继续执行')
print(json.dumps({'same_parent': resume.master == skill_resume.master == stop.master,
                  'packed': resume.winfo_manager() == skill_resume.winfo_manager() == stop.winfo_manager() == 'pack',
                  'disabled': str(resume.cget('state')) == str(skill_resume.cget('state')) == str(stop.cget('state')) == 'disabled'}))
root.destroy()
'''.replace("__TASK_BASE__", repr(str(tmp_path / "assistant_tasks")))
    result = subprocess.run([sys.executable, "-c", source], cwd=Path(launcher.__file__).parent,
                            capture_output=True, text=True, timeout=20)
    if result.returncode and "TclError" in result.stderr:
        pytest.skip("Tk display is unavailable")
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "same_parent": True, "packed": True, "disabled": True}


def _mail_snapshot():
    return {"provider": "qq", "uid": "12", "uidvalidity": "7",
            "subject": "Synthetic message", "from": "sender@example.test",
            "to": "reader@qq.com", "date": "Sat, 17 Oct 2026 10:00:00 +0000",
            "body": "Offline body", "attachments": [], "sha256": "a" * 64,
            "coverage": {"scope": "selected_full_message", "body_format": "text/plain",
                         "parts": 1, "attachments": "metadata_only",
                         "html_alternative_ignored": False}}


@pytest.mark.parametrize("code,expected", [
    ("mail_export_document_limit", "超过本机 Word 文档容量"),
    ("mail_export_request_oversize", "超过本机导出请求容量"),
    ("mail_export_task_scope_invalid", "目录无法安全使用"),
])
def test_mail_export_prepare_error_text_is_safe(code, expected):
    message = launcher._mail_export_prepare_label(code)
    assert expected in message
    assert code not in message


def test_mail_export_uses_local_cli_and_verified_durable_office_result(desktop, monkeypatch):
    root, original_root, state, _tree, original = desktop
    from agent_service import mail_export

    def fake_write(task_root, base, snapshot):
        assert snapshot == _mail_snapshot()
        assert "secret" not in snapshot and "account" not in snapshot
        assert task_root.parent == base
        task_root.mkdir(parents=True, exist_ok=False)
        (task_root / "mail-export-request.json").write_text("local request", encoding="utf-8")
        return "d" * 64

    monkeypatch.setattr(mail_export, "write_export_request", fake_write)
    assert callable(state["on_export"])
    assert state["on_export"](_mail_snapshot()) is True
    assert state["on_export"](_mail_snapshot()) is False
    assert _button("开始任务").cget("state") == "disabled"
    _Thread.run("jarvis-mail-export-prepare")
    _pump(root)
    _Thread.run("jarvis-mail_export")
    command = _Process.created[0].command
    assert Path(command[1]).name == "run_jarvis_mail_export.py"
    assert "run_jarvis_task.py" not in " ".join(command)
    assert command[-2:] == ["--request-sha256", "d" * 64]
    export_root = Path(command[command.index("--task-root") + 1])
    artifact = export_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"synthetic docx readback")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (export_root / "result.json").write_text(json.dumps({
        "root": str(export_root), "kind": "office", "ok": True,
        "state": "succeeded", "artifacts": [
            {"step": "s1", "path": str(artifact), "sha256": digest}]
    }), encoding="utf-8")
    state["export_inspection"] = {
        "root": str(export_root), "kind": "office", "state": "succeeded",
        "artifacts": [{"label": "s1 · 文档 (.docx)", "path": str(artifact), "sha256": digest}]}
    _pump(root)
    _Thread.run("jarvis-mail-export-reconcile")
    _pump(root)
    assert _button("打开成果").cget("state") == "normal"
    assert any("本机 Word 并完成应用回读" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert (original_root / "result.json").read_bytes() == original


def test_mail_export_cancel_during_prepare_marks_only_new_task(desktop, monkeypatch):
    root, original_root, state, _tree, original = desktop
    from agent_service import mail_export
    def fake_write(task_root, base, _snapshot):
        assert task_root.parent == base
        task_root.mkdir(parents=True, exist_ok=False)
        return "d" * 64
    monkeypatch.setattr(mail_export, "write_export_request", fake_write)
    assert state["on_export"](_mail_snapshot()) is True
    _button("停止").invoke()
    _Thread.run("jarvis-mail-export-prepare")
    _pump(root)
    new_root = next(path for path in original_root.parent.glob("task-*") if path != original_root)
    assert (new_root / "cancel.flag").is_file()
    assert not (original_root / "cancel.flag").exists()
    assert (original_root / "result.json").read_bytes() == original


def test_mail_export_cancel_scope_failure_never_launches_office(desktop, monkeypatch):
    root, original_root, state, _tree, original = desktop
    from agent_service import mail_export

    def fake_write(task_root, base, _snapshot):
        task_root.mkdir(parents=True, exist_ok=False)
        return "d" * 64

    monkeypatch.setattr(mail_export, "write_export_request", fake_write)
    monkeypatch.setattr(mail_export, "request_export_cancel", lambda *_args: False)
    assert state["on_export"](_mail_snapshot()) is True
    _button("停止").invoke()
    _Thread.run("jarvis-mail-export-prepare")
    _pump(root)
    assert _Process.created == []
    assert _button("开始任务").cget("state") == "normal"
    assert (original_root / "result.json").read_bytes() == original


def test_mail_export_prepare_limit_explains_failure_without_creating_task(desktop, monkeypatch):
    root, original_root, state, _tree, original = desktop
    from agent_service import mail_export

    def reject(*_args):
        raise mail_export.MailExportError("mail_export_document_limit")

    monkeypatch.setattr(mail_export, "write_export_request", reject)
    assert state["on_export"](_mail_snapshot()) is True
    _Thread.run("jarvis-mail-export-prepare")
    _pump(root)
    assert not any(path != original_root for path in original_root.parent.glob("task-*"))
    assert any("超过本机 Word 文档容量" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert _button("开始任务").cget("state") == "normal"
    assert (original_root / "result.json").read_bytes() == original


def _history_source_output(root, original_root, state, tree):
    artifact = original_root / ("workflow-" + "b" * 32) / "s1" / "document.docx"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"verified synthetic Word output")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    state["history_artifacts"] = [{"label": "s1 · 文档 (.docx)",
                                    "path": str(artifact), "sha256": digest}]
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    return artifact, digest


def _artifact_ref(original_root, artifact, digest):
    return {"version": 1, "task_root": str(original_root),
            "workflow_root": str(artifact.parent.parent), "step_id": "s1",
            "artifact": str(artifact), "sha256": digest,
            "receipt_sha256": "c" * 64, "token": "d" * 64}


def test_history_output_requires_explicit_selection_and_paid_start(desktop, monkeypatch):
    root, original_root, state, tree, original = desktop
    from agent_service import artifact_source
    artifact, digest = _history_source_output(root, original_root, state, tree)
    ref = _artifact_ref(original_root, artifact, digest)
    calls = []

    def prepare(path, sha, base):
        calls.append((str(path), sha, base))
        return dict(ref)

    monkeypatch.setattr(artifact_source, "prepare_artifact_source", prepare)
    history_use = [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][0]
    assert history_use.cget("state") == "normal"
    history_use.invoke()
    assert _Process.created == []
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert len(calls) == 1
    assert _Process.created == []
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.get = lambda *_args: "请根据此文档生成下一份报告"
    _button("开始任务").invoke()
    assert _Process.created == []
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    assert len(calls) == 2
    assert _Process.created == []
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert "--source-artifact-json" in command
    assert "--source-file" not in command
    assert json.loads(command[-1]) == ref
    assert (original_root / "result.json").read_bytes() == original


def test_changed_history_output_blocks_before_model_and_clear_removes_ref(desktop, monkeypatch):
    root, original_root, state, tree, original = desktop
    from agent_service import artifact_source
    artifact, digest = _history_source_output(root, original_root, state, tree)
    ref = _artifact_ref(original_root, artifact, digest)
    count = {"value": 0}

    def prepare(_path, _sha, _base):
        count["value"] += 1
        if count["value"] == 2:
            raise artifact_source.ArtifactSourceError("artifact_source_receipt_mismatch")
        return dict(ref)

    monkeypatch.setattr(artifact_source, "prepare_artifact_source", prepare)
    history_use = [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][0]
    history_use.invoke()
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.get = lambda *_args: "根据这个文件继续"
    _button("开始任务").invoke()
    _Thread.run("jarvis-artifact-source-start")
    _pump(root)
    assert _Process.created == []
    assert len(list(original_root.parent.glob("task-*"))) == 1
    assert any("开始前已变化" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source_entry.options["textvariable"].get() == str(artifact)
    _button("开始任务").invoke()
    assert _Process.created == []
    assert len(list(original_root.parent.glob("task-*"))) == 1
    _button("清除").invoke()
    assert source_entry.options["textvariable"].get() == ""
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    command = _Process.created[-1].command
    assert "--source-file" not in command
    assert "--source-artifact-json" not in command
    assert (original_root / "result.json").read_bytes() == original


def test_current_office_output_populates_hash_and_can_be_selected(desktop, monkeypatch):
    root, original_root, _state, _tree, original = desktop
    from agent_service import artifact_source
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.get = lambda *_args: "创建一份办公文档"

    def durable_office(process):
        command = process.command
        task_root = Path(command[command.index("--task-root") + 1])
        artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"generated Word document")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": True,
            "state": "succeeded", "completed": ["s1"], "artifacts": [
                {"step": "s1", "path": str(artifact), "sha256": digest}]
        }), encoding="utf-8")

    _Process.on_communicate = durable_office
    _button("开始任务").invoke()
    _Thread.run("jarvis-agent")
    _pump(root)
    command = _Process.created[0].command
    task_root = Path(command[command.index("--task-root") + 1])
    artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "document.docx"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    seen = []

    def prepare(path, sha, base):
        seen.append((str(path), sha, base))
        return _artifact_ref(task_root, artifact, digest)

    monkeypatch.setattr(artifact_source, "prepare_artifact_source", prepare)
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert seen == [(str(artifact), digest, original_root.parent)]
    assert len(_Process.created) == 1
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source_entry.options["textvariable"].get() == str(artifact)
    assert (original_root / "result.json").read_bytes() == original


def test_replacing_generated_source_uses_plain_file_route(desktop, monkeypatch, tmp_path):
    root, original_root, state, tree, _original = desktop
    from agent_service import artifact_source
    artifact, digest = _history_source_output(root, original_root, state, tree)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: _artifact_ref(original_root, artifact, digest))
    [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][0].invoke()
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    csv = tmp_path / "selected.csv"
    csv.write_text("name,count\nexample,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.get = lambda *_args: "根据这个表格创建报告"
    _button("开始任务").invoke()
    assert not any(thread.name == "jarvis-artifact-source-start" for thread in _Thread.all)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert "--source-file" in command and str(csv) in command
    assert "--source-artifact-json" not in command
    _pump(root)
    _button("清除").invoke()
    assert source_entry.options["textvariable"].get() == ""


def test_history_source_selection_ignores_stale_inspection(desktop, monkeypatch):
    root, original_root, state, tree, _original = desktop
    from agent_service import artifact_source
    artifact, digest = _history_source_output(root, original_root, state, tree)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: _artifact_ref(original_root, artifact, digest))
    [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][0].invoke()
    tree.select(tree.selection()[0])
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert _Process.created == []
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    assert source_entry.options["textvariable"].get() == ""


def test_pasted_generated_office_path_cannot_bypass_provenance(desktop):
    root, original_root, state, tree, _original = desktop
    artifact, _digest = _history_source_output(root, original_root, state, tree)
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(artifact))
    task_text = next(w for w in _Widget.all if w.options.get("height") == 6)
    task_text.get = lambda *_args: "根据这个文件继续"
    _button("开始任务").invoke()
    assert _Process.created == []
    assert not any(thread.name == "jarvis-agent" for thread in _Thread.all)
    assert any("用于下一任务" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))


def test_capability_catalog_search_is_local_read_only(desktop, monkeypatch):
    _root, _task_root, _state, _tree, _original = desktop
    from agent_service import capabilities
    catalog_tree = next(w for w in _Widget.all
                        if w.options.get("columns") == ("name", "scope"))
    assert catalog_tree.items
    calls = []

    def local_list(query=None, limit=50):
        calls.append((query, limit))
        return [{"id": "office.document.create", "title": "创建新文档",
                 "scope": "仅新建任务文件", "inputs": ["标题"], "outputs": ["DOCX"],
                 "limits": ["最多 40 段"], "availability": "implemented",
                 "evidence_status": "offline_and_live_target"}]

    monkeypatch.setattr(capabilities, "list_capabilities", local_list)
    query = next(w for w in _Widget.all if w.options.get("width") == 52)
    query.get = lambda *_args: "文档"
    _button("查找本机能力").invoke()
    assert calls == [("文档", 50)]
    assert set(catalog_tree.items) == {"office.document.create"}
    catalog_tree.select("office.document.create")
    assert any("仅新建任务文件" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert _Process.created == []


def _select_local_skill(root, label):
    chooser = next(w for w in _Widget.all
                   if w.options.get("values") == tuple(launcher.SKILL_CHOICES))
    chooser.options["textvariable"].set(label)
    chooser.callbacks["<<ComboboxSelected>>"](None)
    _Thread.run("jarvis-skill-status")
    _pump(root)


def _mock_published_skill_version(monkeypatch):
    from agent_service import local_skills

    def version(_store, skill_id, _digest):
        recipe, sum_columns, draft = launcher.SKILL_CONTRACTS[skill_id]
        return Path("."), {"recipe": recipe, "sum_columns": sum_columns, "draft": draft}

    monkeypatch.setattr(local_skills, "_version", version)


def test_published_skill_status_rejects_changed_recipe(monkeypatch, tmp_path):
    from agent_service import local_skills

    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 1, "active": "a" * 64})
    actual = {"recipe": "table_report", "sum_columns": [1], "draft": True}
    checked = []

    def version(store, skill_id, digest):
        checked.append((store, skill_id, digest))
        return tmp_path, dict(actual)

    monkeypatch.setattr(local_skills, "_version", version)
    for skill_id, contract in launcher.SKILL_CONTRACTS.items():
        actual.update(zip(("recipe", "sum_columns", "draft"), contract))
        assert launcher._published_skill_status(tmp_path, skill_id, tmp_path)["enabled"] is True
        actual["draft"] = not contract[2]
        result = launcher._published_skill_status(tmp_path, skill_id, tmp_path)
        assert result["enabled"] is False and result["contract_mismatch"] is True
    assert len(checked) == 2 * len(launcher.SKILL_CONTRACTS)


def test_published_skill_status_requires_active_digest_and_gate(monkeypatch, tmp_path):
    from agent_service import local_skills

    def unexpected_version(*_args):
        raise AssertionError("disabled status must not read the manifest")

    monkeypatch.setattr(local_skills, "_version", unexpected_version)
    monkeypatch.setattr(local_skills, "status", lambda *_args: {"enabled": False})
    assert launcher._published_skill_status(tmp_path, "table-office-package", tmp_path) == {
        "enabled": False}
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 1})
    result = launcher._published_skill_status(tmp_path, "table-office-package", tmp_path)
    assert result["enabled"] is False and result["contract_mismatch"] is True


def test_published_skill_status_never_enables_zero_remaining(monkeypatch, tmp_path):
    from agent_service import local_skills
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 0, "active": "a" * 64})
    monkeypatch.setattr(local_skills, "_version", lambda *_args: pytest.fail(
        "zero-run gate must not read or enable a manifest"))
    assert launcher._published_skill_status(tmp_path, "table-office-package", tmp_path)[
        "enabled"] is False


def test_published_local_skill_uses_selected_external_source_without_model(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    calls = []

    def status(store, skill_id, base):
        calls.append((store, skill_id, base))
        return {"enabled": True, "remaining_runs": 2, "active": "a" * 64}

    monkeypatch.setattr(local_skills, "status", status)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    run = _button("运行本地技能（不调用模型）")
    assert run.cget("state") == "normal"
    run.invoke()
    assert _button("开始任务").cget("state") == "disabled"
    assert _button("开始记事本任务").cget("state") == "disabled"
    assert _button("开始查资料").cget("state") == "disabled"
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    assert len(calls) == 2
    assert _Process.created == []
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert Path(command[1]).name == "run_jarvis_skills.py"
    assert command[command.index("run") + 1] == "table-office-package"
    assert command[command.index("--expected-sha256") + 1] == "a" * 64
    assert command[-2:] == ["--source-file", str(csv)]
    assert "--trial-sha256" not in command and "--source-artifact-json" not in command
    task_root = Path(command[command.index("--task-root") + 1])
    assert task_root.is_dir() and list(task_root.iterdir()) == []
    assert (original_root / "result.json").read_bytes() == original


def test_table_copy_local_skill_is_excel_only_fixed_contract(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 2, "active": "a" * 64})
    source = tmp_path / "source.csv"
    source.write_text("name,code\na,001\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(source))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-copy")
    _select_local_skill(root, label)
    assert launcher.SKILL_CONTRACTS["table-office-copy"] == ("table_copy", [], False)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert command[command.index("run") + 1] == "table-office-copy"
    assert command[-2:] == ["--source-file", str(source)]
    assert "run_jarvis_task.py" not in " ".join(command)
    assert (original_root / "result.json").read_bytes() == original


def test_document_table_local_skill_uses_only_selected_word_and_fixed_recipe(
        desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 3, "active": "a" * 64})
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry.options["textvariable"].set(str(csv))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "document-table-office-package")
    _select_local_skill(root, label)
    run = _button("运行本地技能（不调用模型）")
    assert run.cget("state") == "disabled"
    document = tmp_path / "source.docx"
    document.write_bytes(b"synthetic source, never opened by GUI fake")
    source_entry.options["textvariable"].set(str(document))
    assert run.cget("state") == "normal"
    run.invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert command[command.index("run") + 1] == "document-table-office-package"
    assert command[-2:] == ["--source-file", str(document)]
    assert "--source-artifact-json" not in command and "--trial-sha256" not in command
    assert (original_root / "result.json").read_bytes() == original


def test_document_table_local_skill_manifest_mismatch_disables_gui(desktop, monkeypatch, tmp_path):
    root, _original_root, _state, _tree, _original = desktop
    from agent_service import local_skills
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 3, "active": "a" * 64})
    monkeypatch.setattr(local_skills, "_version", lambda *_args: (tmp_path, {
        "recipe": "document_table_report", "sum_columns": [], "draft": True}))
    document = tmp_path / "source.docx"
    document.write_bytes(b"synthetic source")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(document))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "document-table-office-package")
    _select_local_skill(root, label)
    assert _button("运行本地技能（不调用模型）").cget("state") == "disabled"
    assert any("需重新评审" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert _Process.created == []


def test_document_numeric_local_skill_requires_word_and_pinned_fixed_contract(
        desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 2, "active": "a" * 64})
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,3\n", encoding="utf-8")
    source_entry.options["textvariable"].set(str(csv))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "document-numeric-report")
    assert "第2列转数字求和" in label and "无草稿" in label
    _select_local_skill(root, label)
    run = _button("运行本地技能（不调用模型）")
    assert run.cget("state") == "disabled"
    source = tmp_path / "source.docx"
    source.write_bytes(b"synthetic DOCX, not parsed in GUI test")
    source_entry.options["textvariable"].set(str(source))
    assert run.cget("state") == "normal"
    assert launcher.SKILL_CONTRACTS["document-numeric-report"] == (
        "document_numeric_report", [1], False)
    run.invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert command[command.index("run") + 1] == "document-numeric-report"
    assert command[command.index("--expected-sha256") + 1] == "a" * 64
    assert command[-2:] == ["--source-file", str(source)]
    assert "run_jarvis_task.py" not in " ".join(command)
    assert (original_root / "result.json").read_bytes() == original


@pytest.mark.parametrize("skill_id", ["document-office-copy", "document-table-office-package"])
def test_local_skill_generated_source_uses_provenance_ref(desktop, monkeypatch, skill_id):
    root, original_root, state, tree, _original = desktop
    from agent_service import artifact_source, local_skills
    _mock_published_skill_version(monkeypatch)
    artifact, digest = _history_source_output(root, original_root, state, tree)
    ref = _artifact_ref(original_root, artifact, digest)
    monkeypatch.setattr(artifact_source, "prepare_artifact_source", lambda *_args: dict(ref))
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 1, "active": "a" * 64})
    [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][0].invoke()
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    label = next(label for label, selected_id in launcher.SKILL_CHOICES.items()
                 if selected_id == skill_id)
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    _Thread.run("jarvis-agent")
    command = _Process.created[0].command
    assert command[command.index("run") + 1] == skill_id
    assert "--source-artifact-json" in command
    assert json.loads(command[-1]) == ref
    assert "--source-file" not in command and "--trial-sha256" not in command


def test_local_skill_gate_failure_and_stop_do_not_launch(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    enabled = {"value": False}
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": enabled["value"], "remaining_runs": 1, "active": "a" * 64})
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    run = _button("运行本地技能（不调用模型）")
    assert run.cget("state") == "disabled"
    enabled["value"] = True
    _select_local_skill(root, label)
    assert run.cget("state") == "normal"
    run.invoke()
    _button("停止").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    assert any(thread.name == "jarvis-skill-status" for thread in _Thread.all)
    _Thread.run("jarvis-skill-status")
    _pump(root)
    assert run.cget("state") == "normal"
    assert _Process.created == []
    assert (original_root / "result.json").read_bytes() == original


def test_local_skill_rechecks_publication_before_cli(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    calls = {"count": 0}

    def changing_status(*_args):
        calls["count"] += 1
        return {"enabled": calls["count"] == 1, "remaining_runs": 1, "active": "a" * 64}

    monkeypatch.setattr(local_skills, "status", changing_status)
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    assert calls["count"] == 2
    assert _Process.created == []
    assert len(list(original_root.parent.glob("task-*"))) == 1
    assert (original_root / "result.json").read_bytes() == original


def test_local_skill_rejects_version_switch_between_prepare_and_start(
        desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    source = tmp_path / "source.csv"
    source.write_text("name,count\na,1\n", encoding="utf-8")
    next(w for w in _Widget.all if w.options.get("width") == 38).options[
        "textvariable"].set(str(source))
    calls = {"count": 0}

    def switched_status(*_args):
        calls["count"] += 1
        return {"enabled": True, "remaining_runs": 1,
                "active": ("a" if calls["count"] == 1 else "b") * 64}

    monkeypatch.setattr(local_skills, "status", switched_status)
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    assert calls["count"] == 2
    assert _Process.created == []
    assert (original_root / "result.json").read_bytes() == original


def test_local_skill_durable_office_result_uses_existing_output_and_history(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills, artifact_source
    _mock_published_skill_version(monkeypatch)
    gate = {"remaining": 2, "calls": 0}
    def status(*_args):
        gate["calls"] += 1
        return {"enabled": True, "remaining_runs": gate["remaining"], "active": "a" * 64}
    monkeypatch.setattr(local_skills, "status", status)
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)

    def durable_skill(process):
        gate["remaining"] = 1
        command = process.command
        task_root = Path(command[command.index("--task-root") + 1])
        artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "spreadsheet.xlsx"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"synthetic Excel output")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": True, "state": "succeeded",
            "completed": ["s1"], "artifacts": [
                {"step": "s1", "path": str(artifact), "sha256": digest}],
            "skill": {"id": "table-office-package"}
        }), encoding="utf-8")

    _Process.on_communicate = durable_skill
    _Thread.run("jarvis-agent")
    _pump(root)
    assert _button("打开成果").cget("state") == "normal"
    current_use = [w for w in _Widget.all if w.options.get("text") == "用于下一任务"][1]
    assert current_use.cget("state") == "disabled"
    command = _Process.created[-1].command
    task_root = Path(command[command.index("--task-root") + 1])
    artifact = task_root / ("workflow-" + "e" * 32) / "s1" / "spreadsheet.xlsx"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(artifact_source, "prepare_artifact_source",
                        lambda *_args: _artifact_ref(task_root, artifact, digest))
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert current_use.cget("state") == "normal"
    assert any("本地技能成果已保存并核验" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert any(thread.name == "jarvis-history-list" for thread in _Thread.all)
    assert any(thread.name == "jarvis-skill-status" for thread in _Thread.all)
    assert _button("运行本地技能（不调用模型）").cget("state") == "disabled"
    _Thread.run("jarvis-skill-status")
    _pump(root)
    assert gate["calls"] == 3
    assert _button("运行本地技能（不调用模型）").cget("state") == "normal"
    assert any("剩余 1 次受限运行" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert (original_root / "result.json").read_bytes() == original


@pytest.mark.parametrize("end_state", ["failed", "unknown"])
def test_local_skill_uncertain_end_rechecks_gate_and_stays_disabled(
        desktop, monkeypatch, tmp_path, end_state):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    gate = {"enabled": True, "remaining": 1, "calls": 0}

    def status(*_args):
        gate["calls"] += 1
        return {"enabled": gate["enabled"], "remaining_runs": gate["remaining"],
                "active": "a" * 64}

    monkeypatch.setattr(local_skills, "status", status)
    source = tmp_path / "source.csv"
    source.write_text("name,count\na,1\n", encoding="utf-8")
    next(w for w in _Widget.all if w.options.get("width") == 38).options[
        "textvariable"].set(str(source))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)

    def durable_failure(process):
        gate.update(enabled=False, remaining=0)
        command = process.command
        task_root = Path(command[command.index("--task-root") + 1])
        (task_root / "result.json").write_text(json.dumps({
            "root": str(task_root), "kind": "office", "ok": False,
            "state": end_state, "error": "synthetic_failure", "artifacts": [],
            "skill": {"id": "table-office-package"}}), encoding="utf-8")

    _Process.on_communicate = durable_failure
    _Thread.run("jarvis-agent")
    _pump(root)
    assert any(thread.name == "jarvis-skill-status" for thread in _Thread.all)
    _Thread.run("jarvis-skill-status")
    _pump(root)
    assert gate["calls"] == 3
    assert _button("运行本地技能（不调用模型）").cget("state") == "disabled"
    assert any("本地技能未确认完成" in str(w.options["textvariable"].get())
               for w in _Widget.all if w.options.get("textvariable"))
    assert (original_root / "result.json").read_bytes() == original


def test_local_skill_stop_after_preflight_marks_owned_empty_task(desktop, monkeypatch, tmp_path):
    root, original_root, _state, _tree, original = desktop
    from agent_service import local_skills
    _mock_published_skill_version(monkeypatch)
    monkeypatch.setattr(local_skills, "status", lambda *_args: {
        "enabled": True, "remaining_runs": 1, "active": "a" * 64})
    csv = tmp_path / "source.csv"
    csv.write_text("name,count\na,1\n", encoding="utf-8")
    source_entry = next(w for w in _Widget.all if w.options.get("width") == 38)
    source_entry.options["textvariable"].set(str(csv))
    label = next(label for label, skill_id in launcher.SKILL_CHOICES.items()
                 if skill_id == "table-office-package")
    _select_local_skill(root, label)
    _button("运行本地技能（不调用模型）").invoke()
    _Thread.run("jarvis-skill-prepare")
    _pump(root)
    owned = next(path for path in original_root.parent.glob("task-*") if path != original_root)
    assert list(owned.iterdir()) == []
    _button("停止").invoke()
    assert (owned / "cancel.flag").is_file()
    assert not (owned / "request.txt").exists()
    assert not (original_root / "cancel.flag").exists()
    assert (original_root / "result.json").read_bytes() == original
def test_completed_office_history_reason_is_not_reported_as_unknown():
    from run_jarvis_assistant import _resume_reason_label
    assert _resume_reason_label("resume_already_complete") == "办公步骤已经完成"

