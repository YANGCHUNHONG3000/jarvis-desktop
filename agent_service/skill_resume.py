"""One-shot continuation of an explicitly cancelled, supervised local skill.

The publisher grants one task only after its original result and disabled
candidate state are durable.  This module never enables the candidate again.
"""

from __future__ import annotations

from hashlib import sha256
import math
import os
from pathlib import Path
import re
import time
import uuid

from . import local_skills as skills
from . import office_resume as ordinary
from . import office_workflow as workflow


_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PERMIT = "skill-resume-permit.json"
_POINTER = "skill-resume-latest.json"


class SkillResumeError(ValueError):
    """Stable local rejection code."""


def _raw(path: Path, limit=workflow._MAX_JSON, *, allow_empty=False):
    if ordinary._linked(path) or not path.is_file() or not 0 <= path.stat().st_size <= limit:
        raise SkillResumeError("skill_resume_record_invalid")
    data = path.read_bytes()
    if not (allow_empty or data) or len(data) > limit:
        raise SkillResumeError("skill_resume_record_invalid")
    return data


def _digest(path: Path, limit=workflow._MAX_JSON, *, allow_empty=False):
    return sha256(_raw(path, limit, allow_empty=allow_empty)).hexdigest()


def _identity(task, base, store, *, current):
    task = ordinary._task(task, base)
    parent, original_hash = ordinary._parent(task)
    info = parent.get("skill")
    if (type(info) is not dict or set(info) != {"id", "sha256", "implementation_sha256", "mode"}
            or type(info["id"]) is not str or not skills._ID.fullmatch(info["id"])
            or type(info["sha256"]) is not str or not _HASH.fullmatch(info["sha256"])
            or type(info["implementation_sha256"]) is not str
            or not _HASH.fullmatch(info["implementation_sha256"])
            or info["mode"] != "active" or parent["state"] != "cancelled"
            or parent["ok"] is not False):
        raise SkillResumeError("skill_resume_parent_invalid")
    request = workflow._read_json(task / "skill-request.json")
    root = skills._store(store) if current else Path(store).absolute()
    if current:
        version, candidate = skills._version(root, info["id"], info["sha256"])
    else:
        version = None
        candidate = request.get("manifest")
        if (type(candidate) is not dict or candidate.get("id") != info["id"]
                or workflow._hash(candidate) != info["sha256"]):
            raise SkillResumeError("skill_resume_request_invalid")
    snapshot = ordinary._snapshot(task, base)
    workflow_root = snapshot["workflow_root"]
    if (request != {"manifest": candidate, "sha256": info["sha256"],
                    "implementation_sha256": info["implementation_sha256"],
                    "mode": "active", "executor": "supervised_office",
                    "workflow_root": str(workflow_root)}):
        raise SkillResumeError("skill_resume_request_invalid")
    original_count = snapshot["original_completed"]
    if not 1 <= original_count < len(snapshot["steps"]):
        raise SkillResumeError("skill_resume_boundary_invalid")
    if not snapshot["receipts"] or len(snapshot["receipts"]) < original_count:
        raise SkillResumeError("skill_resume_boundary_invalid")
    cancelled = task / "cancel.flag"
    if ordinary._linked(cancelled) or not cancelled.is_file():
        raise SkillResumeError("skill_resume_cancel_invalid")
    run = workflow._read_json(workflow_root / "result.json")
    original_ids = [step["id"] for step in snapshot["steps"][:original_count]]
    if (run.get("ok") is not False or run.get("state") != "cancelled"
            or run.get("root") != str(workflow_root)
            or run.get("completed") != original_ids
            or run.get("receipts") != {key: snapshot["receipts"][key] for key in original_ids}):
        raise SkillResumeError("skill_resume_boundary_invalid")
    if current:
        state = skills._state(root, info["id"])
        if (state["active"] is not None or state["inflight"] is not None
                or state["remaining_runs"] != 0 or info["sha256"] not in state["blocked"]):
            raise SkillResumeError("skill_resume_candidate_not_blocked")
    return task, parent, original_hash, info, root, version, candidate, snapshot


def _native_prefix(snapshot, count):
    """Require real supervised worker evidence and independent OOXML content."""
    from .office import verify_office_file
    receipts = {}
    for step in snapshot["steps"][:count]:
        step_id = step["id"]
        receipt = snapshot["receipts"][step_id]
        step_root = snapshot["workflow_root"] / step_id
        if step["action"] == "email_draft":
            from .email_draft import _verify_eml
            spec = workflow._spec(step, receipts, snapshot["workflow_root"])
            observed = _verify_eml(Path(receipt["artifact"]), spec, receipt["observed"]["attachments"])
            if observed != receipt["observed"]:
                raise SkillResumeError("skill_resume_draft_unverified")
            receipts[step_id] = receipt
            continue
        if receipt.get("app", {}).get("progid") not in {
                "KWPS.Application", "KET.Application", "Word.Application", "Excel.Application"}:
            raise SkillResumeError("skill_resume_native_unverified")
        spec = workflow._spec(step, receipts, snapshot["workflow_root"])
        worker_request = workflow._read_json(step_root / "worker-request.json")
        worker_outcome = workflow._read_json(step_root / "worker-outcome.json")
        if (worker_request != {"spec": spec}
                or workflow._receipt(step, step_root, workflow._hash(spec), worker_outcome, spec) != receipt):
            raise SkillResumeError("skill_resume_native_unverified")
        verify_office_file(Path(receipt["artifact"]), spec)
        receipts[step_id] = receipt


def _prefix_token(snapshot):
    count = snapshot["original_completed"]
    hashes = []
    for step in snapshot["steps"][:count]:
        step_id = step["id"]
        receipt = snapshot["receipts"][step_id]
        hashes.append([step_id, _digest(snapshot["workflow_root"] / step_id / "receipt.json"),
                       receipt["sha256"]])
    return workflow._hash({"original": snapshot["original_sha256"],
                           "manifest": snapshot["manifest_sha256"], "receipts": hashes})


def _evidence(task, base, store, *, current, archived=None):
    task, parent, original_hash, info, root, version, candidate, snapshot = _identity(
        task, base, store, current=current)
    original_count = snapshot["original_completed"]
    if current:
        if (len(snapshot["receipts"]) != original_count
                or info["implementation_sha256"] != skills.implementation_sha256()):
            raise SkillResumeError("skill_resume_changed")
        source = workflow._read_json(task / "sources" / "source.json")
        source = skills._prepared_snapshot(task, source, base)
        if skills.build_steps(candidate, source) != snapshot["steps"]:
            raise SkillResumeError("skill_resume_plan_changed")
        for gate in ("offline.json", "review.json"):
            skills._gate_file(version, gate, info["sha256"])
        skills._live_check(version, info["sha256"], base)
    _native_prefix(snapshot, original_count)
    files = {
        "result.json": original_hash,
        "skill-request.json": _digest(task / "skill-request.json"),
        "cancel.flag": _digest(task / "cancel.flag", 100_000, allow_empty=True),
        "sources/source.json": _digest(task / "sources" / "source.json"),
        "workflow/manifest.json": snapshot["manifest_sha256"],
        "workflow/result.json": _digest(snapshot["workflow_root"] / "result.json"),
    }
    if current:
        files["store/manifest.json"] = _digest(version / "manifest.json")
        files["store/active.json"] = _digest(root / info["id"] / "active.json")
        for gate in ("offline.json", "review.json", "live.json"):
            files["store/" + gate] = _digest(version / gate)
    else:
        if type(archived) is not dict or type(archived.get("files")) is not dict:
            raise SkillResumeError("skill_resume_permit_invalid")
        for name in ("store/manifest.json", "store/active.json", "store/offline.json",
                     "store/review.json", "store/live.json"):
            value = archived["files"].get(name)
            if type(value) is not str or not _HASH.fullmatch(value):
                raise SkillResumeError("skill_resume_permit_invalid")
            files[name] = value
    source_dir = task / "sources"
    copied = [item for item in source_dir.iterdir() if item.name in
              {"source.csv", "source.xlsx", "source.docx"}]
    if len(copied) != 1:
        raise SkillResumeError("skill_resume_source_invalid")
    files["sources/" + copied[0].name] = _digest(copied[0], 20_000_000)
    provenance = source_dir / "provenance.json"
    if provenance.exists() or ordinary._linked(provenance):
        files["sources/provenance.json"] = _digest(provenance)
    for step in snapshot["steps"][:original_count]:
        step_id = step["id"]
        step_root = snapshot["workflow_root"] / step_id
        for name in ("intent.json", "receipt.json", "worker-request.json", "worker-outcome.json"):
            files[f"workflow/{step_id}/{name}"] = _digest(step_root / name)
        receipt = snapshot["receipts"][step_id]
        files[f"workflow/{step_id}/artifact"] = _digest(Path(receipt["artifact"]), 20_000_000)
    return {"schema": 1, "task_root": str(task), "workflow_root": str(snapshot["workflow_root"]),
            "store_root": str(root), "skill_id": info["id"], "candidate_sha256": info["sha256"],
            "implementation_sha256": info["implementation_sha256"],
            "original_sha256": original_hash, "manifest_sha256": snapshot["manifest_sha256"],
            "completed": original_count, "total": len(snapshot["steps"]),
            "prefix_token": _prefix_token(snapshot), "files": files}, snapshot


def _permit(task):
    path = task / _PERMIT
    value = workflow._read_json(path)
    if type(value) is not dict or set(value) != {"evidence", "evidence_sha256"}:
        raise SkillResumeError("skill_resume_permit_invalid")
    if value["evidence_sha256"] != workflow._hash(value["evidence"]):
        raise SkillResumeError("skill_resume_permit_invalid")
    return value


def mint_permit(task_root, base, store):
    """Publisher-only call after a durable cancelled parent and blocked state."""
    task = ordinary._task(task_root, base)
    # Caller already owns store and task. Workflow is acquired last.
    parent = workflow._read_json(task / "result.json")
    workflow_root = ordinary._manifest(task, parent)[0]
    with workflow._owner(workflow_root):
        evidence, _ = _evidence(task, base, store, current=True)
        if (task / _PERMIT).exists() or ordinary._linked(task / _PERMIT):
            raise SkillResumeError("skill_resume_permit_exists")
        workflow._write_new(task / _PERMIT,
                            {"evidence": evidence, "evidence_sha256": workflow._hash(evidence)})


def _store_default(base):
    return Path(base).absolute().parent / "capabilities"


def _ready(task_root, base, store, *, current):
    task = ordinary._task(task_root, base)
    permit = _permit(task)
    if not current:
        bound_store = permit["evidence"].get("store_root")
        if type(bound_store) is not str or not Path(bound_store).is_absolute():
            raise SkillResumeError("skill_resume_permit_invalid")
        store = bound_store
    evidence, snapshot = _evidence(task, base, store, current=current,
                                   archived=permit["evidence"] if not current else None)
    if evidence != permit["evidence"]:
        raise SkillResumeError("skill_resume_evidence_changed")
    return permit, snapshot


def _token(permit, snapshot):
    return workflow._hash({"permit": workflow._hash(permit),
                           "receipts": permit["evidence"]["prefix_token"]})


def _attempts(task):
    return [p for p in task.iterdir() if p.name.startswith("skill-resume-")
            and p.name not in {_PERMIT, _POINTER}]


def preview_resume(task_root, base, store=None):
    empty = {"eligible": False, "reason": "skill_resume_unavailable", "kind": "skill_resume",
             "task_root": None, "workflow_root": None, "token": None,
             "completed": 0, "total": 0, "remaining": 0}
    try:
        task = ordinary._task(task_root, base)
        permit, snapshot = _ready(task, base, store or _store_default(base), current=True)
        if _attempts(task) or (task / _POINTER).exists() or ordinary._linked(task / _POINTER):
            raise SkillResumeError("skill_resume_already_attempted")
        return {"eligible": True, "reason": "ready", "kind": "skill_resume",
                "task_root": str(task), "workflow_root": str(snapshot["workflow_root"]),
                "token": _token(permit, snapshot), "completed": len(snapshot["receipts"]),
                "total": len(snapshot["steps"]), "remaining": len(snapshot["steps"]) - len(snapshot["receipts"])}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "skill_resume_unavailable"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            empty["reason"] = code
        return empty


def _outcome(snapshot, attempt_id, token, state, error=None):
    value = ordinary._outcome(snapshot, attempt_id, token, state, error=error)
    value["kind"] = "skill_resume"
    return value


def resume_task(task_root, base, *, expected_token, attempt_id, store=None, deadline=None):
    if type(expected_token) is not str or not _HASH.fullmatch(expected_token):
        raise SkillResumeError("skill_resume_token_invalid")
    if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
        raise SkillResumeError("skill_resume_attempt_invalid")
    deadline = time.monotonic() + 600 if deadline is None else deadline
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise SkillResumeError("skill_resume_deadline_invalid")
    task = ordinary._task(task_root, base)
    store = skills._store(store or _store_default(base))
    # The store lock precedes task; run_office_workflow acquires workflow last.
    with workflow._owner(store):
        with workflow._owner(task):
            permit, before = _ready(task, base, store, current=True)
            if _token(permit, before) != expected_token:
                raise SkillResumeError("skill_resume_preview_stale")
            if (any(p != task / ("skill-resume-" + attempt_id) for p in _attempts(task))
                    or (task / _POINTER).exists() or ordinary._linked(task / _POINTER)):
                raise SkillResumeError("skill_resume_already_attempted")
            attempt = task / ("skill-resume-" + attempt_id)
            if ordinary._linked(attempt):
                raise SkillResumeError("skill_resume_attempt_invalid")
            if attempt.exists():
                if not attempt.is_dir() or any(p.name != "cancel.flag" for p in attempt.iterdir()):
                    raise SkillResumeError("skill_resume_already_attempted")
            else:
                attempt.mkdir(exist_ok=False)
            cancel = attempt / "cancel.flag"
            if ordinary._linked(cancel) or (cancel.exists() and not cancel.is_file()):
                raise SkillResumeError("skill_resume_attempt_invalid")
            request = {"version": 1, "attempt_id": attempt_id, "task_root": str(task),
                       "permit_sha256": _digest(task / _PERMIT), "expected_token": expected_token}
            workflow._write_new(attempt / "request.json", request)
            # The exclusive request is the one-shot claim; GUI may create the
            # directory and cancellation flag before the worker starts.
            try:
                def guard():
                    present, snapshot = _ready(task, base, store, current=True)
                    if _token(present, snapshot) != expected_token:
                        raise SkillResumeError("skill_resume_preview_stale")

                run = workflow.run_office_workflow(
                    before["steps"], before["workflow_root"], resume=True,
                    deadline=deadline, cancel_file=cancel, result_root=attempt,
                    resume_guard=guard)
                after = ordinary._snapshot(task, base)
                state = run.get("state")
                if state == "succeeded" and len(after["receipts"]) == len(after["steps"]):
                    pass
                elif state not in {"cancelled", "timed_out"}:
                    state = "unknown"
                result = _outcome(after, attempt_id, expected_token, state)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                try:
                    after = ordinary._snapshot(task, base)
                except (ValueError, OSError, TypeError, KeyError):
                    after = before
                code = str(exc) if isinstance(exc, ValueError) else "skill_resume_failed"
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                    code = "skill_resume_failed"
                result = _outcome(after, attempt_id, expected_token, "unknown", code)
            ordinary._publish(attempt / "outcome.json", result)
            if result["state"] in {"succeeded", "cancelled", "timed_out"}:
                pointer = {"version": 1, "attempt_id": attempt_id,
                           "original_sha256": result["original_sha256"],
                           "manifest_sha256": result["manifest_sha256"],
                           "token_after": result["token_after"],
                           "outcome_sha256": _digest(attempt / "outcome.json")}
                ordinary._publish(task / _POINTER, pointer)
            return result


def read_resume_result(task_root, attempt_root, base):
    unknown = {"ok": False, "state": "unknown", "kind": "skill_resume", "attempt_id": None}
    try:
        task = ordinary._task(task_root, base)
        raw_attempt = Path(attempt_root)
        if (not raw_attempt.is_absolute() or ".." in raw_attempt.parts
                or ordinary._linked(raw_attempt) or not raw_attempt.is_dir()):
            return unknown
        attempt = raw_attempt.resolve(strict=True)
        if (not _ATTEMPT.fullmatch(attempt.name.removeprefix("skill-resume-"))
                or attempt.name != "skill-resume-" + attempt.name.removeprefix("skill-resume-")
                or attempt.parent != task):
            return unknown
        attempt_id = attempt.name[len("skill-resume-"):]
        unknown["attempt_id"] = attempt_id
        permit, snapshot = _ready(task, base, _store_default(base), current=False)
        request = workflow._read_json(attempt / "request.json")
        outcome = workflow._read_json(attempt / "outcome.json")
        if request != {"version": 1, "attempt_id": attempt_id, "task_root": str(task),
                       "permit_sha256": _digest(task / _PERMIT),
                       "expected_token": outcome.get("token_before")}:
            return unknown
        if (outcome.get("kind") != "skill_resume" or outcome.get("attempt_id") != attempt_id
                or type(outcome.get("token_before")) is not str
                or not _HASH.fullmatch(outcome["token_before"])
                or outcome.get("original_sha256") != snapshot["original_sha256"]
                or outcome.get("manifest_sha256") != snapshot["manifest_sha256"]
                or outcome.get("token_after") != snapshot["token"]
                or outcome.get("workflow_root") != str(snapshot["workflow_root"])
                or outcome.get("task_root") != str(task)
                or outcome.get("state") not in {"succeeded", "cancelled", "timed_out"}):
            return unknown
        if outcome["token_before"] != _token(permit, snapshot):
            return unknown
        if _attempts(task) != [attempt]:
            return unknown
        _native_prefix(snapshot, len(snapshot["receipts"]))
        expected = _outcome(snapshot, attempt_id, outcome["token_before"], outcome["state"])
        if outcome != expected:
            return unknown
        run = workflow._read_json(attempt / "result.json")
        if (run.get("state") != outcome["state"] or run.get("ok") != outcome["ok"]
                or run.get("root") != str(snapshot["workflow_root"])
                or run.get("completed") != list(snapshot["receipts"])
                or run.get("receipts") != snapshot["receipts"]):
            return unknown
        if outcome["state"] == "succeeded" and len(snapshot["receipts"]) != len(snapshot["steps"]):
            return unknown
        return outcome
    except (ValueError, OSError, TypeError, KeyError):
        return unknown


def latest_continuation(task_root, base):
    try:
        task = ordinary._task(task_root, base)
        pointer = workflow._read_json(task / _POINTER)
        if (set(pointer) != {"version", "attempt_id", "original_sha256", "manifest_sha256",
                             "token_after", "outcome_sha256"} or pointer["version"] != 1
                or type(pointer["attempt_id"]) is not str
                or not _ATTEMPT.fullmatch(pointer["attempt_id"])):
            return None
        attempt = task / ("skill-resume-" + pointer["attempt_id"])
        if _digest(attempt / "outcome.json") != pointer["outcome_sha256"]:
            return None
        outcome = read_resume_result(task, attempt, base)
        if (outcome.get("state") not in {"succeeded", "cancelled", "timed_out"}
                or pointer["original_sha256"] != outcome["original_sha256"]
                or pointer["manifest_sha256"] != outcome["manifest_sha256"]
                or pointer["token_after"] != outcome["token_after"]):
            return None
        return outcome
    except (ValueError, OSError, TypeError, KeyError):
        return None
