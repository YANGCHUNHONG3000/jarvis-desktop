"""Explicit continuation of a cancelled or pre-dispatch blocked local skill.

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
_PREFLIGHT_BLOCKS = {"office_ui_blocked", "office_ui_unavailable"}
_MAX_ATTEMPTS = 32


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
            or info["mode"] not in {"active", "trial"}
            or parent["state"] not in {"cancelled", "failed"}
            or parent["ok"] is not False):
        raise SkillResumeError("skill_resume_parent_invalid")
    blocked = parent["state"] == "failed"
    if blocked and parent.get("error") not in _PREFLIGHT_BLOCKS:
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
    expected_request = {"manifest": candidate, "sha256": info["sha256"],
                        "implementation_sha256": info["implementation_sha256"],
                        "mode": info["mode"], "executor": "supervised_office",
                        "workflow_root": str(workflow_root)}
    binding = request.get("source_retry")
    from .adapter_development import external_trial_binding
    development_binding = external_trial_binding(task, base, store) if current or not blocked else None
    if development_binding is not None and binding != development_binding:
        raise SkillResumeError("skill_resume_source_changed")
    if binding is not None:
        if (type(binding) is not dict or set(binding) != {"task_root", "token"}
                or any(type(binding[key]) is not str for key in binding)):
            raise SkillResumeError("skill_resume_source_changed")
        expected_request["source_retry"] = binding
        from .gap_recovery import verify_prepared_origin
        verify_prepared_origin(task, base)
    if request != expected_request:
        raise SkillResumeError("skill_resume_request_invalid")
    original_count = snapshot["original_completed"]
    if not (0 if blocked else 1) <= original_count < len(snapshot["steps"]):
        raise SkillResumeError("skill_resume_boundary_invalid")
    if len(snapshot["receipts"]) < original_count:
        raise SkillResumeError("skill_resume_boundary_invalid")
    cancelled = task / "cancel.flag"
    if not blocked and (ordinary._linked(cancelled) or not cancelled.is_file()):
        raise SkillResumeError("skill_resume_cancel_invalid")
    run = workflow._read_json(workflow_root / "result.json")
    original_ids = [step["id"] for step in snapshot["steps"][:original_count]]
    if (run.get("ok") is not False or run.get("state") != parent["state"]
            or run.get("root") != str(workflow_root)
            or run.get("completed") != original_ids
            or run.get("receipts") != {key: snapshot["receipts"][key] for key in original_ids}):
        raise SkillResumeError("skill_resume_boundary_invalid")
    if blocked and run != _v2_run(_prefix_snapshot(snapshot, original_count),
                                 "blocked", parent["error"]):
        raise SkillResumeError("skill_resume_boundary_invalid")
    if current and info["mode"] == "active":
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
    blocked = parent["state"] == "failed"
    if current:
        if (not blocked and len(snapshot["receipts"]) != original_count
                or info["implementation_sha256"] != skills.implementation_sha256()):
            raise SkillResumeError("skill_resume_changed")
        source = workflow._read_json(task / "sources" / "source.json")
        source = skills._prepared_snapshot(task, source, base)
        origin = task / "retry-origin.json"
        if origin.exists() or skills._linked(origin):
            from .gap_recovery import verify_prepared_origin
            verify_prepared_origin(task, base)
        if skills.build_steps(candidate, source) != snapshot["steps"]:
            raise SkillResumeError("skill_resume_plan_changed")
        for gate in ("offline.json", "review.json"):
            skills._gate_file(version, gate, info["sha256"])
        if info["mode"] == "active":
            skills._live_check(version, info["sha256"], base)
    _native_prefix(snapshot, len(snapshot["receipts"]) if blocked else original_count)
    files = {
        "result.json": original_hash,
        "skill-request.json": _digest(task / "skill-request.json"),
        "sources/source.json": _digest(task / "sources" / "source.json"),
        "workflow/manifest.json": snapshot["manifest_sha256"],
        "workflow/result.json": _digest(snapshot["workflow_root"] / "result.json"),
    }
    if not blocked:
        files["cancel.flag"] = _digest(task / "cancel.flag", 100_000, allow_empty=True)
    if current:
        files["store/manifest.json"] = _digest(version / "manifest.json")
        for gate in ("offline.json", "review.json"):
            files["store/" + gate] = _digest(version / gate)
        if info["mode"] == "active":
            files["store/active.json"] = _digest(root / info["id"] / "active.json")
            files["store/live.json"] = _digest(version / "live.json")
    else:
        if type(archived) is not dict or type(archived.get("files")) is not dict:
            raise SkillResumeError("skill_resume_permit_invalid")
        names = ["store/manifest.json", "store/offline.json", "store/review.json"]
        if info["mode"] == "active":
            names += ["store/active.json", "store/live.json"]
        for name in names:
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
        names = ("intent.json", "receipt.json", "worker-request.json", "worker-outcome.json")
        if blocked and step["action"] == "email_draft":
            names = ("intent.json", "receipt.json")
        for name in names:
            files[f"workflow/{step_id}/{name}"] = _digest(step_root / name)
        receipt = snapshot["receipts"][step_id]
        limit = 45_000_000 if blocked and step["action"] == "email_draft" else 20_000_000
        files[f"workflow/{step_id}/artifact"] = _digest(Path(receipt["artifact"]), limit)
    return {"schema": 2 if blocked else 1, "task_root": str(task), "workflow_root": str(snapshot["workflow_root"]),
            "store_root": str(root), "skill_id": info["id"], "candidate_sha256": info["sha256"],
            "mode": info["mode"],
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
    if (type(value["evidence"]) is not dict
            or type(value["evidence"].get("schema")) is not int
            or value["evidence"]["schema"] not in {1, 2}):
        raise SkillResumeError("skill_resume_permit_invalid")
    return value


def mint_permit(task_root, base, store):
    """Publisher-only call after a durable safe boundary and disabled state."""
    task = ordinary._task(task_root, base)
    # Caller already owns store and task. Workflow is acquired last.
    parent = workflow._read_json(task / "result.json")
    workflow_root = ordinary._manifest(task, parent)[0]
    with workflow._owner(workflow_root):
        evidence, snapshot = _evidence(task, base, store, current=True)
        if evidence["schema"] == 2 and (len(snapshot["receipts"]) != snapshot["original_completed"]
                or _attempts(task) or (task / _POINTER).exists() or ordinary._linked(task / _POINTER)):
            raise SkillResumeError("skill_resume_boundary_invalid")
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


def _prefix_snapshot(snapshot, count):
    """Reconstruct a historical contiguous prefix from currently verified bytes."""
    if (type(count) is not int
            or not snapshot["original_completed"] <= count <= len(snapshot["receipts"])):
        raise SkillResumeError("skill_resume_chain_invalid")
    value = dict(snapshot)
    ids = [step["id"] for step in snapshot["steps"][:count]]
    value["receipts"] = {key: snapshot["receipts"][key] for key in ids}
    hashes = [[key, _digest(snapshot["workflow_root"] / key / "receipt.json"),
               snapshot["receipts"][key]["sha256"]] for key in ids]
    value["token"] = workflow._hash({"original": snapshot["original_sha256"],
                                     "manifest": snapshot["manifest_sha256"], "receipts": hashes})
    return value


def _v2_run(snapshot, state, error=None):
    """Exact workflow boundary contract; an error name alone grants nothing."""
    count = len(snapshot["receipts"])
    total = len(snapshot["steps"])
    if (state not in {"blocked", "succeeded", "cancelled", "timed_out"}
            or state == "succeeded" and count != total
            or state != "succeeded" and count >= total):
        raise SkillResumeError("skill_resume_boundary_invalid")
    run = {"ok": state == "succeeded", "state": "failed" if state == "blocked" else state,
           "root": str(snapshot["workflow_root"]),
           "completed": list(snapshot["receipts"]), "receipts": snapshot["receipts"]}
    if state == "blocked":
        step = snapshot["steps"][count]
        if error not in _PREFLIGHT_BLOCKS or step["action"] == "email_draft":
            raise SkillResumeError("skill_resume_boundary_invalid")
        run.update(step=step["id"], error=error)
    elif error is not None:
        raise SkillResumeError("skill_resume_boundary_invalid")
    return run


def _v2_token(permit, snapshot, previous):
    return workflow._hash({"permit": workflow._hash(permit), "snapshot": snapshot["token"],
                           "previous_attempt_id": previous[0],
                           "previous_outcome_sha256": previous[1]})


def _v2_request(task, permit, snapshot, attempt_id, previous):
    return {"version": 2, "attempt_id": attempt_id, "task_root": str(task),
            "permit_sha256": _digest(task / _PERMIT),
            "expected_token": _v2_token(permit, snapshot, previous),
            "previous_attempt_id": previous[0], "previous_outcome_sha256": previous[1],
            "before_completed": len(snapshot["receipts"]),
            "before_snapshot_token": snapshot["token"]}


def _v2_outcome(snapshot, attempt_id, request, request_hash, run_hash, state, error=None):
    value = _outcome(snapshot, attempt_id, request["expected_token"], state, error)
    value.update(version=2, request_sha256=request_hash, run_sha256=run_hash)
    return value


def _v2_pointer(outcome, outcome_hash):
    return {"version": 2, "attempt_id": outcome["attempt_id"],
            "original_sha256": outcome["original_sha256"],
            "manifest_sha256": outcome["manifest_sha256"],
            "token_after": outcome["token_after"], "outcome_sha256": outcome_hash}


def _v2_record(path):
    raw = _raw(path)
    return ordinary._json_bytes(raw), sha256(raw).hexdigest()


def _v2_chain(task, permit, snapshot, *, exclude=None, check_pointer=True):
    """Validate the entire bounded chain, including historical prefix snapshots.

    No incomplete, branching, orphaned, or unknown attempt grants another run.
    A blocked attempt can extend the prefix, but only with verified receipts.
    """
    attempts = _attempts(task)
    if exclude is not None:
        attempts = [path for path in attempts if path != exclude]
    if len(attempts) > _MAX_ATTEMPTS:
        raise SkillResumeError("skill_resume_attempt_limit")
    successors = {}
    for attempt in attempts:
        attempt_id = attempt.name.removeprefix("skill-resume-")
        if (not _ATTEMPT.fullmatch(attempt_id) or ordinary._linked(attempt) or not attempt.is_dir()
                or any(child.name not in {"request.json", "result.json", "outcome.json", "cancel.flag"}
                       or ordinary._linked(child) or not child.is_file() for child in attempt.iterdir())):
            raise SkillResumeError("skill_resume_chain_invalid")
        request, request_hash = _v2_record(attempt / "request.json")
        outcome, outcome_hash = _v2_record(attempt / "outcome.json")
        run, run_hash = _v2_record(attempt / "result.json")
        previous_id = request.get("previous_attempt_id")
        if previous_id is not None and (type(previous_id) is not str or not _ATTEMPT.fullmatch(previous_id)):
            raise SkillResumeError("skill_resume_chain_invalid")
        if previous_id in successors:
            raise SkillResumeError("skill_resume_chain_invalid")
        successors[previous_id] = (attempt_id, request, request_hash, outcome, outcome_hash, run, run_hash)
    previous = (None, None)
    prefix = _prefix_snapshot(snapshot, snapshot["original_completed"])
    chain = []
    while previous[0] in successors:
        if chain and chain[-1]["outcome"]["state"] != "blocked":
            raise SkillResumeError("skill_resume_chain_closed")
        attempt_id, request, request_hash, outcome, outcome_hash, run, run_hash = successors.pop(previous[0])
        if request != _v2_request(task, permit, prefix, attempt_id, previous):
            raise SkillResumeError("skill_resume_chain_invalid")
        count = outcome.get("completed")
        if type(count) is not int or not len(prefix["receipts"]) <= count <= len(snapshot["receipts"]):
            raise SkillResumeError("skill_resume_chain_invalid")
        after = _prefix_snapshot(snapshot, count)
        state, error = outcome.get("state"), outcome.get("error")
        if run != _v2_run(after, state, error):
            raise SkillResumeError("skill_resume_chain_invalid")
        if outcome != _v2_outcome(after, attempt_id, request, request_hash, run_hash, state, error):
            raise SkillResumeError("skill_resume_chain_invalid")
        chain.append({"attempt_id": attempt_id, "outcome": outcome,
                      "outcome_sha256": outcome_hash})
        prefix = after
        previous = (attempt_id, outcome_hash)
    if successors or prefix["token"] != snapshot["token"]:
        raise SkillResumeError("skill_resume_chain_invalid")
    pointer = task / _POINTER
    if check_pointer:
        if chain:
            value, _ = _v2_record(pointer)
            if value != _v2_pointer(chain[-1]["outcome"], chain[-1]["outcome_sha256"]):
                raise SkillResumeError("skill_resume_chain_invalid")
        elif pointer.exists() or ordinary._linked(pointer):
            raise SkillResumeError("skill_resume_chain_invalid")
    return chain


def _v2_previous(chain):
    if not chain:
        return None, None
    return chain[-1]["attempt_id"], chain[-1]["outcome_sha256"]


def _v2_open(chain):
    if chain and chain[-1]["outcome"]["state"] != "blocked":
        raise SkillResumeError("skill_resume_already_attempted")
    if len(chain) >= _MAX_ATTEMPTS:
        raise SkillResumeError("skill_resume_attempt_limit")


def preview_resume(task_root, base, store=None):
    empty = {"eligible": False, "reason": "skill_resume_unavailable", "kind": "skill_resume",
             "task_root": None, "workflow_root": None, "token": None,
             "completed": 0, "total": 0, "remaining": 0}
    try:
        task = ordinary._task(task_root, base)
        permit, snapshot = _ready(task, base, store or _store_default(base), current=True)
        if permit["evidence"]["schema"] == 2:
            chain = _v2_chain(task, permit, snapshot)
            _v2_open(chain)
            token = _v2_token(permit, snapshot, _v2_previous(chain))
        elif _attempts(task) or (task / _POINTER).exists() or ordinary._linked(task / _POINTER):
            raise SkillResumeError("skill_resume_already_attempted")
        else:
            token = _token(permit, snapshot)
        return {"eligible": True, "reason": "ready", "kind": "skill_resume",
                "task_root": str(task), "workflow_root": str(snapshot["workflow_root"]),
                "token": token, "completed": len(snapshot["receipts"]),
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


def _resume_v2(task, base, store, permit, before, expected_token, attempt_id, deadline):
    """Called under store/task locks; the workflow acquires its lock last."""
    attempt = task / ("skill-resume-" + attempt_id)
    if ordinary._linked(attempt):
        raise SkillResumeError("skill_resume_attempt_invalid")
    if attempt.exists() and (not attempt.is_dir() or any(
            child.name != "cancel.flag" or ordinary._linked(child) or not child.is_file()
            for child in attempt.iterdir())):
        raise SkillResumeError("skill_resume_already_attempted")
    chain = _v2_chain(task, permit, before, exclude=attempt)
    _v2_open(chain)
    previous = _v2_previous(chain)
    if _v2_token(permit, before, previous) != expected_token:
        raise SkillResumeError("skill_resume_preview_stale")
    if not attempt.exists():
        attempt.mkdir(exist_ok=False)
    cancel = attempt / "cancel.flag"
    request = _v2_request(task, permit, before, attempt_id, previous)
    workflow._write_new(attempt / "request.json", request)
    request_hash = _digest(attempt / "request.json")
    after, run_hash = before, None
    try:
        def guard():
            present, snapshot = _ready(task, base, store, current=True)
            current_chain = _v2_chain(task, present, snapshot, exclude=attempt)
            _v2_open(current_chain)
            if (_v2_token(present, snapshot, _v2_previous(current_chain)) != expected_token
                    or _v2_record(attempt / "request.json") != (request, request_hash)
                    or any(child.name not in {"request.json", "cancel.flag"}
                           or ordinary._linked(child) or not child.is_file()
                           for child in attempt.iterdir())):
                raise SkillResumeError("skill_resume_preview_stale")

        run = workflow.run_office_workflow(
            before["steps"], before["workflow_root"], resume=True,
            deadline=deadline, cancel_file=cancel, result_root=attempt, resume_guard=guard)
        _, after = _ready(task, base, store, current=False)
        saved_run, run_hash = _v2_record(attempt / "result.json")
        state = run.get("state")
        error = None
        if state == "failed" and run.get("error") in _PREFLIGHT_BLOCKS:
            state, error = "blocked", run["error"]
        if saved_run != run or run != _v2_run(after, state, error):
            raise SkillResumeError("skill_resume_boundary_invalid")
        result = _v2_outcome(after, attempt_id, request, request_hash, run_hash, state, error)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else "skill_resume_failed"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "skill_resume_failed"
        result = _v2_outcome(after, attempt_id, request, request_hash, run_hash, "unknown", code)
    ordinary._publish(attempt / "outcome.json", result)
    if result["state"] in {"succeeded", "cancelled", "timed_out", "blocked"}:
        # Validate the newly persisted member before advancing the only mutable
        # pointer. A crash or malformed record leaves an unusable orphan.
        verified = _v2_chain(task, permit, after, check_pointer=False)
        pointer = _v2_pointer(result, verified[-1]["outcome_sha256"])
        temporary = task / (".skill-resume-latest." + uuid.uuid4().hex + ".tmp")
        if ordinary._linked(task / _POINTER):
            raise SkillResumeError("skill_resume_chain_invalid")
        workflow._write_new(temporary, pointer)
        os.replace(temporary, task / _POINTER)
    return result


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
            if permit["evidence"]["schema"] == 2:
                return _resume_v2(task, base, store, permit, before, expected_token, attempt_id, deadline)
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
        if permit["evidence"]["schema"] == 2:
            chain = _v2_chain(task, permit, snapshot)
            return next((item["outcome"] for item in chain if item["attempt_id"] == attempt_id), unknown)
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
        if pointer.get("version") == 2:
            permit, snapshot = _ready(task, base, _store_default(base), current=False)
            if permit["evidence"]["schema"] != 2:
                return None
            chain = _v2_chain(task, permit, snapshot)
            return chain[-1]["outcome"] if chain else None
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
