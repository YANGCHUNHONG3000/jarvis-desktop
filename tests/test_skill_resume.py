"""Offline contract tests. Synthetic worker data never counts as live certification."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import uuid

import pytest

from agent_service import (local_skills as skills, office_readiness,
                           office_workflow as workflow, skill_resume)
from test_local_skills import fake_office, manifest, source


@pytest.fixture
def short_tmp():
    # Windows legacy Office paths can exceed MAX_PATH under pytest's long names.
    with tempfile.TemporaryDirectory(prefix="sr-") as value:
        yield Path(value)


def _contract(tmp_path, monkeypatch):
    base = tmp_path / "data" / "assistant_tasks"
    store = tmp_path / "data" / "capabilities"
    candidate = manifest(draft=True)
    digest = skills.register_candidate(store, candidate)["sha256"]
    skills.verify_offline(store, "copy-doc", digest)
    skills.record_review(store, "copy-doc", digest, "Maintainer", "Synthetic contract")
    version = store / "copy-doc" / digest
    workflow._write_new(version / "live.json", {"schema": 1, "candidate_sha256": digest,
                         "implementation_sha256": skills.implementation_sha256(),
                         "passed": True, "kind": "live_office", "task_root": "synthetic",
                         "task_result_sha256": "0" * 64})
    monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
    skills.activate(store, "copy-doc", digest, base, runs=2)
    task = base / ("task-" + uuid.uuid4().hex)

    def supervised(spec, step_root, should_stop):
        result = fake_office(spec, step_root, should_stop)
        workflow._write_new(step_root / "worker-request.json", {"spec": spec})
        workflow._write_new(step_root / "worker-outcome.json", result)
        (task / "cancel.flag").touch()
        return result

    from agent_service import office_host
    monkeypatch.setattr(office_host, "execute_office_supervised", supervised)
    monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: None)
    outcome = skills.run_skill(store, "copy-doc", task, base, source_file=source(tmp_path))
    assert outcome["state"] == "cancelled" and outcome["completed"] == ["s1"]
    assert (task / "skill-resume-permit.json").is_file()
    return base, store, task, digest


def test_single_cancelled_skill_continuation_preserves_parent_and_history(short_tmp, monkeypatch):
    base, store, task, digest = _contract(short_tmp, monkeypatch)
    original = (task / "result.json").read_bytes()
    workflow_root = Path(json.loads(original)["workflow_root"])
    prefix = (workflow_root / "s1" / "receipt.json").read_bytes()
    preview = skill_resume.preview_resume(task, base, store)
    assert preview["eligible"] and preview["completed"] == 1 and preview["remaining"] == 1
    attempt_id = uuid.uuid4().hex
    attempt = task / ("skill-resume-" + attempt_id)
    attempt.mkdir()
    result = skill_resume.resume_task(task, base, expected_token=preview["token"],
                                      attempt_id=attempt_id, store=store)
    assert result["state"] == "succeeded" and result["completed"] == 2
    assert (task / "result.json").read_bytes() == original
    assert (workflow_root / "s1" / "receipt.json").read_bytes() == prefix
    try:
        skill_resume._ready(task, base, store, current=False)
    except Exception as exc:
        pytest.fail(f"archived evidence: {type(exc).__name__}: {exc}")
    snap = skill_resume.ordinary._snapshot(task, base)
    try:
        skill_resume._native_prefix(snap, len(snap["receipts"]))
    except Exception as exc:
        pytest.fail(f"new receipt: {type(exc).__name__}: {exc}")
    saved_outcome = json.loads((attempt / "outcome.json").read_text(encoding="utf-8"))
    assert saved_outcome == skill_resume._outcome(snap, attempt_id, saved_outcome["token_before"], saved_outcome["state"])
    assert skill_resume.read_resume_result(task, attempt, base)["state"] == "succeeded"
    assert skill_resume.latest_continuation(task, base)["state"] == "succeeded"
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    with pytest.raises(ValueError):
        skill_resume.resume_task(task, base, expected_token=preview["token"],
                                 attempt_id=uuid.uuid4().hex, store=store)
    # A later publisher state change cannot erase an already verified result.
    state_path = store / "copy-doc" / "active.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active"] = "f" * 64
    state["blocked"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert skill_resume.latest_continuation(task, base)["state"] == "succeeded"


def test_tampered_token_and_original_prefix_fail_closed(short_tmp, monkeypatch):
    base, store, task, _ = _contract(short_tmp, monkeypatch)
    preview = skill_resume.preview_resume(task, base, store)
    attempt_id = uuid.uuid4().hex
    result = skill_resume.resume_task(task, base, expected_token=preview["token"],
                                      attempt_id=attempt_id, store=store)
    assert result["state"] == "succeeded"
    attempt = task / ("skill-resume-" + attempt_id)
    request_path, outcome_path = attempt / "request.json", attempt / "outcome.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    request["expected_token"] = outcome["token_before"] = "a" * 64
    request_path.write_text(json.dumps(request), encoding="utf-8")
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")
    assert skill_resume.read_resume_result(task, attempt, base)["state"] == "unknown"


def test_active_injected_boundary_cancel_never_mints_permit(short_tmp, monkeypatch):
    store = short_tmp / "skills"
    base = short_tmp / "tasks"
    digest = skills.register_candidate(store, manifest(draft=True))["sha256"]
    skills.verify_offline(store, "copy-doc", digest)
    skills.record_review(store, "copy-doc", digest, "Maintainer", "Synthetic contract")
    monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
    skills.activate(store, "copy-doc", digest, base, runs=1)
    task = base / ("task-" + uuid.uuid4().hex)

    def injected(spec, step_root, should_stop):
        result = fake_office(spec, step_root, should_stop)
        (task / "cancel.flag").touch()
        return result

    result = skills.run_skill(store, "copy-doc", task, base, source_file=source(short_tmp),
                              execute=injected)
    assert result["state"] == "cancelled" and result["completed"] == ["s1"]
    assert digest in skills.status(store, "copy-doc", base)["blocked"]
    assert not (task / "skill-resume-permit.json").exists()
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
