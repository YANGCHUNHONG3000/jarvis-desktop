"""Offline GUI checks for expanding the local task history window."""

import pytest

from test_history_resume_gui import _Thread, _Widget, _button, _pump, desktop


@pytest.fixture(autouse=True)
def _adapt_unrelated_goal_panel_binding(monkeypatch):
    import agent_service.office_chain_plan_binding as binding

    class OfflineBinding:
        def __init__(self, goal, chain):
            self.goal, self.chain = goal, chain

    monkeypatch.setattr(binding, "OfficeChainPlanBinding", OfflineBinding)


def test_history_expands_to_200_restores_default_and_gap_filter_clears_old_selection(
        desktop, monkeypatch):
    root, task_root, _state, tree, _original = desktop
    from agent_service import task_history

    calls = []
    records = [{"id": f"task-{index:032x}",
                "root": str(task_root.parent / f"task-{index:032x}"),
                "state": "succeeded", "kind": "office", "mtime": 1000 - index}
               for index in range(80)]
    records[60] = {"id": "task-" + "f" * 32,
                   "root": str(task_root.parent / ("task-" + "f" * 32)),
                   "state": "unsupported", "kind": "unsupported", "mtime": 1}

    def list_tasks(_base, limit=30):
        calls.append(limit)
        return records[:limit]

    monkeypatch.setattr(task_history, "list_tasks", list_tasks)
    _button("刷新").invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 30
    assert len(tree.get_children()) == 30

    older = next(w for w in _Widget.all
                 if w.options.get("text") == "显示较早任务（最多200条）")
    older.options["variable"].set(True)
    older.invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 200
    assert len(tree.get_children()) == 80
    old_id = records[0]["id"]
    tree.selection_set(old_id)

    gaps = next(w for w in _Widget.all
                if w.options.get("text") == "仅看未支持任务／能力缺口")
    gaps.options["variable"].set(True)
    gaps.invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 200
    assert tree.get_children() == ("task-" + "f" * 32,)
    assert tree.selection() == ()

    gaps.options["variable"].set(False)
    gaps.invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 200
    assert len(tree.get_children()) == 80
    older.options["variable"].set(False)
    older.invoke()
    _Thread.run("jarvis-history-list")
    _pump(root)
    assert calls[-1] == 30
    assert len(tree.get_children()) == 30
