"""Office preflight reasons remain visible after selecting task history."""

import pytest
from agent_service import task_history
from test_history_resume_gui import _Thread, _Widget, _pump, desktop


@pytest.mark.parametrize("skill", [False, True])
@pytest.mark.parametrize("reason, text", [
    ("office_ui_blocked", "Office窗口被对话框阻塞或无响应"),
    ("office_ui_unavailable", "暂时无法检查Office窗口"),
])
def test_history_explains_preflight_and_does_not_start_execution(desktop, monkeypatch, reason, text, skill):
    root, task_root, state, tree, original = desktop
    inspect = task_history.inspect_task

    def with_reason(*args):
        value = {**inspect(*args), "preflight_block": reason,
                 "continuation": {"state": "blocked", "attempt_id": "a" * 32}}
        if skill:
            value["resume"] = {**value["resume"], "kind": "skill_resume"}
            value["continuation"]["kind"] = "skill_resume"
        return value

    monkeypatch.setattr(task_history, "inspect_task", with_reason)
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    displayed = "\n".join(str(w.options["textvariable"].get())
                          for w in _Widget.all if w.options.get("textvariable"))
    assert text in displayed and "当前步骤尚未派发" in displayed
    assert "等待处理阻塞条件" in displayed
    assert (task_root / "result.json").read_bytes() == original

def test_completed_verified_skill_does_not_display_restart_as_failure(desktop, monkeypatch):
    root, task_root, state, tree, original = desktop
    inspect = task_history.inspect_task
    monkeypatch.setattr(task_history, "inspect_task", lambda *args: {
        **inspect(*args), "state": "succeeded", "original_state": "succeeded",
        "resume": {"eligible": False, "reason": "resume_skill_owned"}})
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    displayed = "\n".join(str(w.options["textvariable"].get())
                          for w in _Widget.all if w.options.get("textvariable"))
    assert "已完成；可将成果用于下一任务" in displayed
    assert "这是受限技能任务" not in displayed
