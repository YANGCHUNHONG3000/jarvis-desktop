"""Offline controller checks for the main-page Office task router."""
from pathlib import Path

import pytest

from agent_service import office_task_entry as module


class DeferredThread:
    pending = []

    def __init__(self, *, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon

    def start(self):
        self.pending.append(self)

    @classmethod
    def run(cls, name="office-task-planning"):
        item = next(item for item in cls.pending if item.name == name)
        cls.pending.remove(item)
        item.target()


def _records(tmp_path, count=1):
    return [{"path": str(tmp_path / f"source-{index}.docx"), "sha256": f"{index:064x}"}
            for index in range(1, count + 1)]


def _entry(monkeypatch, tmp_path, *, records=None, draft="", context_extra=None):
    DeferredThread.pending = []
    monkeypatch.setattr(module.threading, "Thread", DeferredThread)
    context = {"instruction": "更新所选办公成果", "draft": draft,
               "records": _records(tmp_path) if records is None else records,
               "expected_refs": None, "unavailable": False}
    context.update(context_extra or {})
    status, busy, sessions, prepared = [], [], [], []

    def create_session():
        value = object()
        sessions.append(value)
        return value

    entry = module.OfficeTaskEntry(
        context=lambda: context, busy=lambda: False,
        on_busy=busy.append,
        prepare=lambda *args: prepared.append(args) or True,
        status=status.append, session_factory=create_session, task_base=tmp_path,
        clock=lambda: 100.0)
    return entry, context, status, busy, sessions, prepared


def _install_planner(monkeypatch, *, refs=None, routed=None):
    from agent_service import daily_task_routing, office_handoff

    selected, planned = [], []

    def select(records, *, base):
        selected.append((records, base))
        return list(refs if refs is not None else records)

    def propose(session, instruction, source_refs, **kwargs):
        planned.append((session, instruction, source_refs, kwargs))
        return dict(routed or {"state": "ready", "plan": {"steps": []}})

    monkeypatch.setattr(office_handoff, "select_outputs", select)
    monkeypatch.setattr(daily_task_routing, "propose", propose)
    return selected, planned


def test_start_accepts_only_explicit_one_to_four_records(monkeypatch, tmp_path):
    selected, planned = _install_planner(monkeypatch)
    entry, context, status, busy, _sessions, prepared = _entry(
        monkeypatch, tmp_path, records=_records(tmp_path, 4))

    assert entry.start() is True
    DeferredThread.run()
    entry.poll()

    assert len(selected) == 1 and selected[0][0] == context["records"]
    assert planned and prepared and busy == [True, False]

    selected.clear()
    context["records"] = _records(tmp_path, 5)
    assert entry.start() is False
    assert selected == []
    assert "1 至 4" in status[-1]


def test_nonempty_different_target_draft_is_preserved_and_rejected(monkeypatch, tmp_path):
    _install_planner(monkeypatch)
    entry, _context_value, status, busy, _sessions, prepared = _entry(
        monkeypatch, tmp_path, draft="保留的连续任务草稿")

    assert entry.start() is False
    assert entry.pending is None and not DeferredThread.pending
    assert busy == [] and prepared == []
    assert "已有不同草稿" in status[-1]


def test_session_is_cached_for_repeated_planning_in_same_entry(monkeypatch, tmp_path):
    _selected, planned = _install_planner(monkeypatch)
    entry, _context_value, _status, _busy, sessions, _prepared = _entry(monkeypatch, tmp_path)

    for _ in range(2):
        assert entry.start() is True
        DeferredThread.run()
        entry.poll()

    assert len(sessions) == 1
    assert len(planned) == 2 and planned[0][0] is planned[1][0] is sessions[0]


@pytest.mark.parametrize("interruption", ["stop", "close", "timeout", "changed"])
def test_interrupted_or_stale_worker_result_is_discarded(monkeypatch, tmp_path, interruption):
    _install_planner(monkeypatch)
    clock = [100.0]
    entry, context, _status, _busy, _sessions, prepared = _entry(monkeypatch, tmp_path)
    entry.clock = lambda: clock[0]

    assert entry.start() is True
    if interruption == "stop":
        entry.cancel()
    elif interruption == "close":
        entry.close()
    elif interruption == "timeout":
        clock[0] = 196.0
        entry.poll()
    else:
        context["records"] = _records(tmp_path, 2)
        entry.poll()

    DeferredThread.run()
    entry.poll()

    assert prepared == []
    assert entry.pending is None
    if interruption == "close":
        assert entry.closed is True


def test_expected_source_reference_change_blocks_proposal(monkeypatch, tmp_path):
    records = _records(tmp_path)
    mismatched_refs = [{"artifact": records[0]["path"], "sha256": "f" * 64}]
    selected, planned = _install_planner(monkeypatch, refs=mismatched_refs)
    entry, _context_value, status, _busy, _sessions, prepared = _entry(
        monkeypatch, tmp_path, records=records,
        context_extra={"expected_refs": [{"artifact": records[0]["path"],
                                           "sha256": records[0]["sha256"]}]})

    assert entry.start() is True
    DeferredThread.run()
    entry.poll()

    assert len(selected) == 1 and planned == [] and prepared == []
    assert "规划未完成" in status[-1]


def test_ready_handoff_current_check_survives_target_draft_write(monkeypatch, tmp_path):
    _install_planner(monkeypatch, routed={"state": "ready", "plan": {"steps": []}})
    entry, context, _status, _busy, _sessions, prepared = _entry(monkeypatch, tmp_path)
    checks = []

    def handoff(_session, instruction, _routed, _refs, is_current):
        context["draft"] = instruction
        context["draft_revision"] = 4
        checks.append(is_current())
        return True

    entry.prepare = handoff
    assert entry.start() is True
    DeferredThread.run()
    entry.poll()

    assert checks == [True]


@pytest.mark.parametrize('field', ['instruction_revision', 'draft_revision'])
def test_edited_then_restored_draft_discards_pending_route(monkeypatch, tmp_path, field):
    _install_planner(monkeypatch, routed={'state': 'ready', 'plan': {'steps': []}})
    entry, context, _status, _busy, _sessions, prepared = _entry(
        monkeypatch, tmp_path, context_extra={'instruction_revision': 1, 'draft_revision': 0})
    assert entry.start() is True
    context[field] += 2  # Original text restored; it is still a different draft.
    DeferredThread.run()
    entry.poll()
    assert prepared == [] and entry.pending is None


def test_handoff_callback_still_binds_source_revision(monkeypatch, tmp_path):
    _install_planner(monkeypatch, routed={'state': 'ready', 'plan': {'steps': []}})
    entry, context, _status, _busy, _sessions, _prepared = _entry(
        monkeypatch, tmp_path, context_extra={'instruction_revision': 1, 'draft_revision': 0})
    checks = []

    def handoff(_session, instruction, _routed, _refs, is_current):
        context.update(draft=instruction, draft_revision=1)
        checks.append(is_current())
        context['instruction_revision'] += 2
        checks.append(is_current())
        return True

    entry.prepare = handoff
    assert entry.start() is True
    DeferredThread.run()
    entry.poll()
    assert checks == [True, False]


@pytest.mark.parametrize('draft,accepted', [('  task\n', True), ('  \n', False)])
def test_target_whitespace_is_not_rewritten_by_coordinator(monkeypatch, tmp_path, draft, accepted):
    _install_planner(monkeypatch, routed={'state': 'ready', 'plan': {'steps': []}})
    entry, context, _status, _busy, _sessions, prepared = _entry(
        monkeypatch, tmp_path, context_extra={'instruction': 'task', 'draft': draft})
    assert entry.start() is accepted
    if accepted:
        DeferredThread.run()
        entry.poll()
        assert len(prepared) == 1
    assert context['draft'] == draft


def test_thread_start_failure_clears_pending_and_releases_busy(monkeypatch, tmp_path):
    _install_planner(monkeypatch)
    entry, _context_value, status, busy, _sessions, prepared = _entry(monkeypatch, tmp_path)

    class StartFailure:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(module.threading, "Thread", StartFailure)
    assert entry.start() is False
    assert entry.pending is None and busy == [True, False] and prepared == []
    assert "未能启动任务规划" in status[-1]
