"""Offline pre-dispatch recovery contracts; synthetic workers are not live evidence."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service import (local_skills as skills, office_host, office_readiness,
                           office_workflow as workflow, skill_resume, task_history)
from test_local_skills import fake_office, manifest, source
from test_skill_resume import short_tmp


def contract(tmp, monkeypatch, *, active=True, initial_prefix=0):
    base, store = tmp / "data/assistant_tasks", tmp / "data/capabilities"
    candidate = manifest()
    # Two explicit native document steps isolate the continuation contract.
    build = skills.build_steps
    def two_steps(candidate, snapshot):
        first = build(candidate, snapshot)[0]
        return [first, {**first, "id": "s2"}]
    monkeypatch.setattr(skills, "build_steps", two_steps)
    digest = skills.register_candidate(store, candidate)["sha256"]
    skills.verify_offline(store, "copy-doc", digest)
    skills.record_review(store, "copy-doc", digest, "Test reviewer", "Synthetic contract only")
    if active:
        version = store / "copy-doc" / digest
        workflow._write_new(version / "live.json", {"schema": 1, "candidate_sha256": digest,
            "implementation_sha256": skills.implementation_sha256(), "passed": True,
            "kind": "live_office", "task_root": "synthetic", "task_result_sha256": "0" * 64})
        monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
        skills.activate(store, "copy-doc", digest, base, runs=1)
    control = {"blocked_at": initial_prefix, "calls": []}
    def probe():
        return "office_ui_blocked" if len(control["calls"]) >= control["blocked_at"] else None
    def supervised(spec, step_root, stop):
        control["calls"].append(step_root.name)
        result = fake_office(spec, step_root, stop)
        workflow._write_new(step_root / "worker-request.json", {"spec": spec})
        workflow._write_new(step_root / "worker-outcome.json", result)
        return result
    monkeypatch.setattr(office_readiness, "preflight_office_window", probe)
    monkeypatch.setattr(office_host, "execute_office_supervised", supervised)
    task = base / ("task-" + uuid.uuid4().hex)
    result = skills.run_skill(store, "copy-doc", task, base, source_file=source(tmp),
                              trial_digest=None if active else digest)
    assert result["state"] == "failed" and result["error"] == "office_ui_blocked"
    assert len(result["completed"]) == initial_prefix
    assert (task / "skill-resume-permit.json").is_file()
    assert not (task / "cancel.flag").exists()
    return base, store, task, digest, control


def advance(base, store, task):
    preview = skill_resume.preview_resume(task, base, store)
    assert preview["eligible"], preview
    attempt_id = uuid.uuid4().hex
    result = skill_resume.resume_task(task, base, store=store, expected_token=preview["token"],
                                      attempt_id=attempt_id)
    return result, task / ("skill-resume-" + attempt_id)


def protect(root):
    return {str(p): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("prefix", [0, 1])
def test_block_repeat_clear_retains_original_and_quota(short_tmp, monkeypatch, active, prefix):
    base, store, task, digest, ctl = contract(short_tmp, monkeypatch, active=active, initial_prefix=prefix)
    original = protect(task)
    old_state = (store / "copy-doc/active.json").read_bytes() if active else None
    for _ in range(2):
        blocked, attempt = advance(base, store, task)
        assert blocked["state"] == "blocked" and blocked["completed"] == prefix
        assert skill_resume.read_resume_result(task, attempt, base) == blocked
    inspection = task_history.inspect_task(task, base)
    assert inspection["resume"]["kind"] == "skill_resume" and inspection["resume"]["eligible"]
    assert inspection["preflight_block"] == "office_ui_blocked"
    ctl["blocked_at"] = 99
    result, attempt = advance(base, store, task)
    assert result["state"] == "succeeded" and ctl["calls"] == ["s1", "s2"]
    assert skill_resume.latest_continuation(task, base) == result
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    assert task_history.inspect_task(task, base)["continuation"]["kind"] == "skill_resume"
    assert "preflight_block" not in task_history.inspect_task(task, base)
    assert all(protect(task)[p] == data for p, data in original.items())
    if active:
        assert (store / "copy-doc/active.json").read_bytes() == old_state
        assert skills.status(store, "copy-doc", base)["remaining_runs"] == 0


def test_progress_then_second_block_skips_verified_step(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    ctl["blocked_at"] = 1
    blocked, attempt = advance(base, store, task)
    assert blocked["state"] == "blocked" and blocked["completed"] == 1
    prefix = protect(Path(blocked["workflow_root"]) / "s1")
    old_attempt = protect(attempt)
    for _ in range(2):
        assert advance(base, store, task)[0]["state"] == "blocked"
    ctl["blocked_at"] = 99
    result, _ = advance(base, store, task)
    assert result["state"] == "succeeded" and ctl["calls"] == ["s1", "s2"]
    assert protect(Path(result["workflow_root"]) / "s1") == prefix
    assert protect(attempt) == old_attempt
    assert skill_resume.read_resume_result(task, attempt, base) == blocked


@pytest.mark.parametrize("mutation", ["run_step", "run_error", "run_receipts", "outcome", "request",
    "orphan", "incomplete", "unknown_intent", "source", "gate", "state", "implementation"])
def test_corruption_prevents_further_dispatch(short_tmp, monkeypatch, mutation):
    base, store, task, digest, ctl = contract(short_tmp, monkeypatch)
    blocked, attempt = advance(base, store, task)
    preview = skill_resume.preview_resume(task, base, store)
    assert preview["eligible"]
    work = Path(blocked["workflow_root"])
    if mutation.startswith("run_"):
        path = attempt / "result.json"
        data = json.loads(path.read_text())
        data[{"run_step": "step", "run_error": "error", "run_receipts": "receipts"}[mutation]] = "changed"
        path.write_text(json.dumps(data))
    elif mutation in {"outcome", "request"}:
        path = attempt / (mutation + ".json")
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "orphan":
        (task / ("skill-resume-" + uuid.uuid4().hex)).mkdir()
    elif mutation == "incomplete":
        path = task / ("skill-resume-" + uuid.uuid4().hex)
        path.mkdir()
        (path / "request.json").write_text("{}")
    elif mutation == "unknown_intent":
        (work / "s1").mkdir()
        (work / "s1/intent.json").write_text("{}")
    elif mutation == "source":
        path = task / "sources/source.docx"
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation == "gate":
        path = store / "copy-doc" / digest / "review.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "state":
        path = store / "copy-doc/active.json"
        path.write_bytes(path.read_bytes() + b" ")
    else:
        monkeypatch.setattr(skills, "implementation_sha256", lambda: "0" * 64)
    ctl["blocked_at"] = 99
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    with pytest.raises((ValueError, OSError, TypeError, KeyError)):
        skill_resume.resume_task(task, base, store=store, expected_token=preview["token"],
                                  attempt_id=uuid.uuid4().hex)
    assert ctl["calls"] == []


@pytest.mark.parametrize("terminal", ["cancelled", "timed_out"])
def test_cancel_or_deadline_closes_chain(short_tmp, monkeypatch, terminal):
    import time
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    preview = skill_resume.preview_resume(task, base, store)
    ident = uuid.uuid4().hex
    attempt = task / ("skill-resume-" + ident)
    if terminal == "cancelled":
        attempt.mkdir()
        (attempt / "cancel.flag").touch()
    result = skill_resume.resume_task(task, base, store=store, expected_token=preview["token"],
        attempt_id=ident, deadline=time.monotonic() - 1 if terminal == "timed_out" else None)
    assert result["state"] == terminal and ctl["calls"] == []
    assert skill_resume.read_resume_result(task, attempt, base) == result
    assert not skill_resume.preview_resume(task, base, store)["eligible"]


def test_history_survives_later_store_changes_but_outputs_still_checked(short_tmp, monkeypatch):
    base, store, task, digest, ctl = contract(short_tmp, monkeypatch)
    advance(base, store, task)
    ctl["blocked_at"] = 99
    result, _ = advance(base, store, task)
    monkeypatch.setattr(skills, "implementation_sha256", lambda: "0" * 64)
    (store / "copy-doc" / digest / "review.json").write_text("{}")
    (store / "copy-doc/active.json").write_text("{}")
    assert skill_resume.latest_continuation(task, base) == result
    Path(result["artifacts"][-1]["path"]).write_bytes(b"changed")
    assert skill_resume.latest_continuation(task, base) is None


def test_unknown_dispatch_cannot_retry_or_mint_block_success(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    ctl["blocked_at"] = 99
    def uncertain(spec, step_root, stop):
        ctl["calls"].append(step_root.name)
        raise TimeoutError("unknown effect")
    monkeypatch.setattr(office_host, "execute_office_supervised", uncertain)
    result, attempt = advance(base, store, task)
    assert result["state"] == "unknown" and ctl["calls"] == ["s1"]
    assert (Path(result["workflow_root"]) / "s1/intent.json").exists()
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    assert skill_resume.latest_continuation(task, base) is None


def test_old_unknown_parent_not_reclassified_from_error_string(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    path = task / "result.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "unknown"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    with pytest.raises(ValueError):
        skill_resume.mint_permit(task, base, store)
    assert ctl["calls"] == []


def test_attempt_limit_closes_after_last_allowed_block(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    assert skill_resume._MAX_ATTEMPTS == 32
    monkeypatch.setattr(skill_resume, "_MAX_ATTEMPTS", 2)
    for _ in range(2):
        assert advance(base, store, task)[0]["state"] == "blocked"
    preview = skill_resume.preview_resume(task, base, store)
    assert not preview["eligible"] and preview["reason"] == "skill_resume_attempt_limit"
    assert ctl["calls"] == []


def test_failed_trial_success_can_bind_live_gate_without_changing_parent(short_tmp, monkeypatch):
    base, store, task, digest, ctl = contract(short_tmp, monkeypatch, active=False)
    original = (task / "result.json").read_bytes()
    ctl["blocked_at"] = 99
    result, _ = advance(base, store, task)
    assert result["state"] == "succeeded"
    # Synthetic gates are confined to this temporary store, not real certification.
    live = skills.record_live(store, "copy-doc", digest, task, base)
    assert live["continuation_sha256"] == sha256((task / "skill-resume-latest.json").read_bytes()).hexdigest()
    skills.activate(store, "copy-doc", digest, base, runs=1)
    assert skills.status(store, "copy-doc", base)["remaining_runs"] == 1
    assert (task / "result.json").read_bytes() == original


def test_observation_failure_remains_resumable_and_old_preview_is_stale(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    old = skill_resume.preview_resume(task, base, store)
    monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: "office_ui_unavailable")
    blocked, _ = advance(base, store, task)
    assert blocked["state"] == "blocked" and blocked["error"] == "office_ui_unavailable"
    new = skill_resume.preview_resume(task, base, store)
    assert new["eligible"] and new["token"] != old["token"]
    with pytest.raises(ValueError, match="skill_resume_preview_stale"):
        skill_resume.resume_task(task, base, store=store, expected_token=old["token"],
                                  attempt_id=uuid.uuid4().hex)
    assert ctl["calls"] == []


def test_source_rechecked_under_workflow_lock(short_tmp, monkeypatch):
    base, store, task, _, ctl = contract(short_tmp, monkeypatch)
    original_run = workflow.run_office_workflow
    def change_then_run(*args, **kwargs):
        path = task / "sources/source.docx"
        path.write_bytes(path.read_bytes() + b"changed")
        return original_run(*args, **kwargs)
    monkeypatch.setattr(workflow, "run_office_workflow", change_then_run)
    ctl["blocked_at"] = 99
    result, _ = advance(base, store, task)
    assert result["state"] == "unknown" and ctl["calls"] == []
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
