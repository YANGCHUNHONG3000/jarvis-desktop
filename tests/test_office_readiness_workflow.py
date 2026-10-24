"""Readiness gating and durable resume overlays for Office workflows."""

import json
import uuid

import pytest

from agent_service import office_host, office_readiness, office_workflow
from agent_service.office_resume import (latest_continuation, preview_resume,
                                         read_resume_result, resume_task)
from agent_service.office_workflow import run_office_workflow
from agent_service.task_history import inspect_task
from test_office_resume import fake, fixture, steps


def _native_stub(calls):
    def execute(spec, step_root, stop):
        calls.append(step_root.name)
        return fake(spec, step_root, stop)
    return execute


def _resume_empty_parent(tmp_path):
    """Create an ordinary cancelled Office task with no step intents."""
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True)
    root = base / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    workspace = root / ("workflow-" + uuid.uuid4().hex)
    (workspace / "cancel.flag").parent.mkdir(parents=True)
    (workspace / "cancel.flag").touch()
    run = run_office_workflow(steps(), workspace, execute=fake)
    assert run["state"] == "cancelled" and not run["completed"]
    parent = {"ok": False, "state": "cancelled", "kind": "office",
              "root": str(root), "workflow_root": str(workspace),
              "completed": [], "artifacts": []}
    (root / "result.json").write_text(json.dumps(parent), encoding="utf-8")
    (root / "cancel.flag").touch()
    return root, base, workspace


def _attempt(root, base, probe, host_calls):
    preview = preview_resume(root, base)
    assert preview["eligible"], preview
    old = office_readiness.preflight_office_window
    old_host = office_host.execute_office_supervised
    office_readiness.preflight_office_window = probe
    office_host.execute_office_supervised = _native_stub(host_calls)
    try:
        return resume_task(root, base, preview["token"], uuid.uuid4().hex)
    finally:
        office_readiness.preflight_office_window = old
        office_host.execute_office_supervised = old_host


def test_initial_block_has_no_step_or_intent_and_never_calls_host(monkeypatch, tmp_path):
    root = tmp_path / ("workflow-" + uuid.uuid4().hex)
    root.mkdir()
    calls = []
    probes = []
    monkeypatch.setattr(office_readiness, "preflight_office_window",
                        lambda: probes.append("probe") or "office_ui_blocked")
    monkeypatch.setattr("agent_service.office_host.execute_office_supervised",
                        _native_stub(calls))
    run = run_office_workflow(steps(), root)
    assert run["state"] == "failed" and run["error"] == "office_ui_blocked"
    assert run["step"] == "s1" and probes == ["probe"] and calls == []
    assert not (root / "s1").exists()


def test_cancel_during_probe_leaves_step_absent(monkeypatch, tmp_path):
    root = tmp_path / ("workflow-" + uuid.uuid4().hex)
    root.mkdir()
    monkeypatch.setattr(office_readiness, "preflight_office_window",
                        lambda: (root / "cancel.flag").touch() or None)
    run = run_office_workflow(steps(), root)
    assert run["state"] == "cancelled"
    assert not (root / "s1").exists()


@pytest.mark.parametrize("probe", [
    lambda: "office_ui_unavailable",
    lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
    lambda: "unexpected-status",
    lambda: [],
    lambda: {},
])
def test_readiness_unavailable_and_invalid_values_fail_closed(monkeypatch, tmp_path, probe):
    root = tmp_path / ("workflow-" + uuid.uuid4().hex)
    root.mkdir()
    monkeypatch.setattr(office_readiness, "preflight_office_window", probe)
    run = run_office_workflow(steps(), root)
    assert run["state"] == "failed" and run["error"] == "office_ui_unavailable"
    assert not (root / "s1" / "intent.json").exists()


def test_unknown_existing_intent_skips_readiness_and_is_not_replayed(monkeypatch, tmp_path):
    root, base, workspace = fixture(tmp_path)
    (workspace / "s2").mkdir()
    (workspace / "s2" / "intent.json").write_text("{}", encoding="utf-8")
    probes, calls = [], []
    monkeypatch.setattr(office_readiness, "preflight_office_window",
                        lambda: probes.append(1))
    monkeypatch.setattr(office_host, "execute_office_supervised", _native_stub(calls))
    result = run_office_workflow(steps(), workspace, resume=True)
    assert result["state"] == "unknown" and probes == [] and calls == []


def test_repeated_preflight_blocks_are_durable_and_then_clear(monkeypatch, tmp_path):
    root, base, workspace = fixture(tmp_path)
    parent_before = (root / "result.json").read_bytes()
    calls = []
    first = _attempt(root, base, lambda: "office_ui_blocked", calls)
    first_id = first["attempt_id"]
    assert first["state"] == "blocked" and first["error"] == "office_ui_blocked"
    assert read_resume_result(root, base, first_id) == first
    assert latest_continuation(root, base) == first
    assert inspect_task(root, base)["preflight_block"] == "office_ui_blocked"
    assert not (workspace / "s2").exists() and calls == []

    second = _attempt(root, base, lambda: "office_ui_unavailable", calls)
    assert second["state"] == "blocked" and second["attempt_id"] != first_id
    assert read_resume_result(root, base, first_id) == first
    assert read_resume_result(root, base, second["attempt_id"]) == second
    assert latest_continuation(root, base) == second
    assert inspect_task(root, base)["preflight_block"] == "office_ui_unavailable"
    assert (root / "result.json").read_bytes() == parent_before
    first_outcome_before = (root / ("resume-" + first_id) / "outcome.json").read_bytes()
    second_outcome_before = (root / ("resume-" + second["attempt_id"]) / "outcome.json").read_bytes()

    success = _attempt(root, base, lambda: None, calls)
    assert success["state"] == "succeeded" and calls == ["s2"]
    assert latest_continuation(root, base) == success
    assert "preflight_block" not in inspect_task(root, base)
    assert (root / ("resume-" + first_id) / "outcome.json").read_bytes() == first_outcome_before
    assert (root / ("resume-" + second["attempt_id"]) / "outcome.json").read_bytes() == second_outcome_before
    assert (root / "result.json").read_bytes() == parent_before


def test_resume_records_new_prefix_before_block_and_later_runs_only_remainder(monkeypatch, tmp_path):
    root, base, workspace = _resume_empty_parent(tmp_path)
    calls, probe_calls = [], []

    def probe_once_then_block():
        probe_calls.append(len(probe_calls))
        return None if len(probe_calls) == 1 else "office_ui_blocked"

    blocked = _attempt(root, base, probe_once_then_block, calls)
    assert blocked["state"] == "blocked" and blocked["completed"] == 1
    assert calls == ["s1"] and len(probe_calls) == 2
    assert latest_continuation(root, base) == blocked
    assert (workspace / "s1" / "receipt.json").is_file()
    assert not (workspace / "s2").exists()

    calls.clear()
    probe_calls.clear()
    success = _attempt(root, base, lambda: probe_calls.append(1) or None, calls)
    assert success["state"] == "succeeded" and success["completed"] == 3
    assert calls == ["s2"]
    assert probe_calls == [1]  # Email draft skips the Office readiness probe.


@pytest.mark.parametrize("field,value", [
    ("step", "s1"),
    ("error", "office_ui_unavailable"),
    ("receipts", {}),
])
def test_tampered_blocked_run_evidence_is_rejected(monkeypatch, tmp_path, field, value):
    root, base, _workspace = fixture(tmp_path)
    blocked = _attempt(root, base, lambda: "office_ui_blocked", [])
    run_path = root / ("resume-" + blocked["attempt_id"]) / "result.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run[field] = value
    run_path.write_text(json.dumps(run), encoding="utf-8")
    assert read_resume_result(root, base, blocked["attempt_id"])["state"] == "unknown"
    assert latest_continuation(root, base) is None


def test_ordinary_workflow_busy_block_does_not_publish_resumable_overlay(monkeypatch, tmp_path):
    root, base, _workspace = fixture(tmp_path)
    preview = preview_resume(root, base)

    def busy(*args, **kwargs):
        raise office_workflow.WorkflowError("workflow_busy")

    monkeypatch.setattr(office_workflow, "run_office_workflow", busy)
    outcome = resume_task(root, base, preview["token"], uuid.uuid4().hex, execute=fake)
    assert outcome["state"] == "blocked" and outcome["error"] == "workflow_busy"
    assert latest_continuation(root, base) is None


def test_successful_office_steps_probe_but_email_draft_does_not(monkeypatch, tmp_path):
    calls, probes = [], []
    monkeypatch.setattr(office_readiness, "preflight_office_window",
                        lambda: probes.append(1))
    monkeypatch.setattr("agent_service.office_host.execute_office_supervised",
                        _native_stub(calls))
    root = tmp_path / ("workflow-" + uuid.uuid4().hex)
    root.mkdir()
    run = run_office_workflow(steps(), root)
    assert run["state"] == "succeeded"
    assert calls == ["s1", "s2"]
    assert probes == [1, 1]
