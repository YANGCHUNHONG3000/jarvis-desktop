"""Explicit, zero-model continuation of one terminal assistant Office task."""

from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from . import office_workflow as workflow
from . import task_history as history


_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = {"succeeded", "cancelled", "timed_out", "unknown", "failed"}
_PREFLIGHT_BLOCKS = {"office_ui_blocked", "office_ui_unavailable"}
_MAX_PARENT = 100_000


class ResumeError(ValueError):
    """A stable rejection code without local file contents or paths."""


def _linked(path):
    current = Path(path)
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _small_bytes(path, maximum):
    if _linked(path) or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise ResumeError("resume_record_invalid")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ResumeError("resume_record_invalid") from exc
    if not 0 < len(raw) <= maximum:
        raise ResumeError("resume_record_invalid")
    return raw


def _json_bytes(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ResumeError("resume_record_invalid") from exc
    if type(value) is not dict:
        raise ResumeError("resume_record_invalid")
    return value


def _task(task_root, base):
    base = history._base(base)
    root = history._task(task_root, base)
    if _linked(root):
        raise ResumeError("resume_task_scope_invalid")
    return root


def _manifest(root, result):
    named = result.get("workflow_root")
    workflow_root = history._workflow_root(named, root)
    if workflow_root is None or str(workflow_root) != named or _linked(workflow_root):
        raise ResumeError("resume_workflow_scope_invalid")
    candidates = [entry for entry in root.iterdir() if entry.name.startswith("workflow-")]
    if len(candidates) != 1 or candidates[0] != workflow_root:
        raise ResumeError("resume_workflow_ambiguous")
    raw = _small_bytes(workflow_root / "manifest.json", workflow._MAX_JSON)
    manifest = _json_bytes(raw)
    if (set(manifest) != {"version", "steps", "steps_sha256"}
            or type(manifest["version"]) is not int or manifest["version"] != 1):
        raise ResumeError("resume_manifest_invalid")
    try:
        steps = workflow.validate_steps(manifest["steps"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ResumeError("resume_manifest_invalid") from exc
    if steps != manifest["steps"] or workflow._hash(steps) != manifest["steps_sha256"]:
        raise ResumeError("resume_manifest_invalid")
    return workflow_root, steps, sha256(raw).hexdigest()


def _parent(root):
    raw = _small_bytes(root / "result.json", _MAX_PARENT)
    result = _json_bytes(raw)
    if (result.get("root") != str(root) or result.get("kind") != "office"
            or result.get("state") not in _TERMINAL or type(result.get("ok")) is not bool
            or (result["state"] == "succeeded") != result["ok"]):
        raise ResumeError("resume_parent_invalid")
    return result, sha256(raw).hexdigest()


def _assert_plain_office(root):
    """A skill task has its own gate; generic Office continuation cannot run it."""
    request = root / "skill-request.json"
    if _linked(request) or request.exists():
        raise ResumeError("resume_skill_owned")
    result, _ = _parent(root)
    if 'bundle_sha256' in result or (root / 'bundle-request.json').exists():
        # Joint-source recovery needs its own reviewed task guard before enabling.
        raise ResumeError('resume_bundle_owned')
    if "skill" in result:
        raise ResumeError("resume_skill_owned")


def _original_prefix(result, steps, receipts):
    completed = result.get("completed")
    artifacts = result.get("artifacts")
    if type(completed) is not list or type(artifacts) is not list or len(completed) != len(artifacts):
        raise ResumeError("resume_parent_invalid")
    expected = [step["id"] for step in steps[:len(completed)]]
    if completed != expected or (result["state"] == "succeeded" and len(completed) != len(steps)):
        raise ResumeError("resume_parent_invalid")
    for step_id, artifact in zip(completed, artifacts):
        receipt = receipts.get(step_id)
        if (receipt is None or type(artifact) is not dict
                or set(artifact) != {"step", "path", "sha256"}
                or artifact != {"step": step_id, "path": receipt["artifact"],
                                "sha256": receipt["sha256"]}):
            raise ResumeError("resume_parent_mismatch")


def _snapshot(task_root, base):
    root = _task(task_root, base)
    result, original_hash = _parent(root)
    workflow_root, steps, manifest_hash = _manifest(root, result)
    try:
        receipts, unknown = workflow._resume_preflight(steps, workflow_root)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "resume_workflow_invalid"
        raise ResumeError(code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                          else "resume_workflow_invalid") from exc
    if unknown is not None:
        raise ResumeError("resume_unknown_effect")
    _original_prefix(result, steps, receipts)
    receipt_hashes = []
    for step_id, receipt in receipts.items():
        artifact = Path(receipt["artifact"])
        if not artifact.is_absolute() or artifact.parent != workflow_root / step_id:
            raise ResumeError("resume_artifact_scope_invalid")
        raw = _small_bytes(workflow_root / step_id / "receipt.json", workflow._MAX_JSON)
        receipt_hashes.append([step_id, sha256(raw).hexdigest(), receipt["sha256"]])
    token_data = {"original": original_hash, "manifest": manifest_hash,
                  "receipts": receipt_hashes}
    token = workflow._hash(token_data)
    if 'bundle_sha256' in result or (root / 'bundle-request.json').exists():
        from .office_bundle import verify_task
        verify_task(root, base, steps)
    return {"root": root, "workflow_root": workflow_root, "steps": steps,
            "receipts": receipts, "original_sha256": original_hash,
            "manifest_sha256": manifest_hash, "token": token,
            "original_state": result["state"],
            "original_completed": len(result["completed"])}


def _public(snapshot):
    completed = len(snapshot["receipts"])
    total = len(snapshot["steps"])
    return {"eligible": True, "reason": "ready", "task_root": str(snapshot["root"]),
            "workflow_root": str(snapshot["workflow_root"]), "token": snapshot["token"],
            "completed": completed, "total": total, "remaining": total - completed}


def _owned_snapshot(task_root, base, owner_guard):
    root = _task(task_root, base)
    proof = owner_guard(root)
    snapshot = _snapshot(root, base)
    if proof is not None:
        snapshot["token"] = workflow._hash({"snapshot": snapshot["token"], "owner": proof})
    return snapshot


def _preview_resume(task_root, base, *, owner_guard) -> dict:
    """Read-only proof of a contiguous, hash-verified Office continuation."""
    empty = {"eligible": False, "reason": "resume_unavailable", "task_root": None,
             "workflow_root": None, "token": None, "completed": 0, "total": 0,
             "remaining": 0}
    try:
        owner_guard(_task(task_root, base))
        snapshot = _owned_snapshot(task_root, base, owner_guard)
        public = _public(snapshot)
        prior = _latest_continuation(snapshot["root"], base, owner_guard=owner_guard)
        if (public["completed"] > snapshot["original_completed"] and prior is None
                and public["remaining"] > 0):
            public.update(eligible=False, reason="resume_unclaimed_receipts")
        if (public["remaining"] == 0 and snapshot["original_state"] == "succeeded"
                or prior is not None and prior.get("state") == "succeeded"):
            public.update(eligible=False, reason="resume_already_complete")
        return public
    except (ValueError, TypeError, KeyError, OSError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "resume_unavailable"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            empty["reason"] = code
        return empty


def _publish(path, value):
    if path.exists() or _linked(path):
        raise ResumeError("resume_result_exists")
    temporary = path.parent / (path.name + "." + uuid.uuid4().hex + ".tmp")
    workflow._write_new(temporary, value)
    os.replace(temporary, path)


def _outcome(snapshot, attempt_id, before_token, state, *, error=None):
    completed = len(snapshot["receipts"]) if snapshot is not None else 0
    total = len(snapshot["steps"]) if snapshot is not None else 0
    result = {"ok": state == "succeeded" and completed == total,
              "state": state, "attempt_id": attempt_id,
              "task_root": str(snapshot["root"]) if snapshot else None,
              "workflow_root": str(snapshot["workflow_root"]) if snapshot else None,
              "completed": completed, "total": total, "remaining": total - completed,
              "artifacts": ([{"step": step_id, "path": receipt["artifact"],
                              "sha256": receipt["sha256"]}
                             for step_id, receipt in snapshot["receipts"].items()]
                            if snapshot else []),
              "original_sha256": snapshot["original_sha256"] if snapshot else None,
              "manifest_sha256": snapshot["manifest_sha256"] if snapshot else None,
              "token_before": before_token,
              "token_after": snapshot["token"] if snapshot else None}
    if error is not None:
        result["error"] = error
    return result


def _durable_progress(outcome):
    return (outcome.get("state") in {"succeeded", "cancelled", "timed_out"}
            or (outcome.get("state") == "blocked" and outcome.get("error") in _PREFLIGHT_BLOCKS))


def _resume_task(task_root, base, expected_token, attempt_id, *, owner_guard, execute=None, deadline=None) -> dict:
    """Reserve one attempt, recheck under locks, and execute only missing steps."""
    if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
        raise ResumeError("resume_attempt_invalid")
    if type(expected_token) is not str or not _DIGEST.fullmatch(expected_token):
        raise ResumeError("resume_token_invalid")
    if deadline is None:
        deadline = time.monotonic() + 600
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ResumeError("resume_deadline_invalid")
    root = _task(task_root, base)
    owner_guard(root)
    attempt = root / ("resume-" + attempt_id)
    try:
        with workflow._owner(root):
            owner_guard(root)
            if _linked(attempt):
                raise ResumeError("resume_attempt_scope_invalid")
            if attempt.exists():
                if not attempt.is_dir() or any(child.name != "cancel.flag" for child in attempt.iterdir()):
                    raise ResumeError("resume_attempt_exists")
            else:
                attempt.mkdir(exist_ok=False)
            cancel = attempt / "cancel.flag"
            if _linked(cancel) or (cancel.exists() and not cancel.is_file()):
                raise ResumeError("resume_attempt_scope_invalid")
            request = {"version": 1, "attempt_id": attempt_id, "task_root": str(root),
                       "expected_token": expected_token}
            workflow._write_new(attempt / "request.json", request)
            before = None
            try:
                before = _owned_snapshot(root, base, owner_guard)
                if (before["original_state"] == "succeeded"
                        and len(before["receipts"]) == len(before["steps"])):
                    raise ResumeError("resume_already_complete")
                prior = _latest_continuation(root, base, owner_guard=owner_guard)
                if prior is not None and prior.get("state") == "succeeded":
                    raise ResumeError("resume_already_complete")
                if (len(before["receipts"]) > before["original_completed"]
                        and prior is None and len(before["receipts"]) < len(before["steps"])):
                    raise ResumeError("resume_unclaimed_receipts")
                if before["token"] != expected_token:
                    raise ResumeError("resume_preview_stale")

                def guarded():
                    current = _owned_snapshot(root, base, owner_guard)
                    if current["token"] != expected_token:
                        raise ResumeError("resume_preview_stale")

                run = workflow.run_office_workflow(
                    before["steps"], before["workflow_root"], execute=execute, resume=True,
                    deadline=deadline, cancel_file=cancel, resume_guard=guarded,
                    result_root=attempt)
                after = _owned_snapshot(root, base, owner_guard)
                if run.get("state") == "succeeded" and len(after["receipts"]) == len(after["steps"]):
                    state = "succeeded"
                elif run.get("state") in {"cancelled", "timed_out"}:
                    state = run["state"]
                elif run.get("state") == "failed" and run.get("error") in _PREFLIGHT_BLOCKS:
                    state = "blocked"
                else:
                    state = "unknown"
                outcome = _outcome(after, attempt_id, expected_token, state,
                                   error=run["error"] if state == "blocked" else None)
            except (ValueError, TypeError, KeyError, OSError) as exc:
                code = str(exc) if isinstance(exc, ValueError) else "resume_failed"
                code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "resume_failed"
                try:
                    after = _owned_snapshot(root, base, owner_guard)
                except (ValueError, TypeError, KeyError, OSError):
                    after = None
                state = "blocked" if code in {"resume_preview_stale", "workflow_busy",
                                              "later_step_conflict"} else "unknown"
                outcome = _outcome(after or before, attempt_id, expected_token, state,
                                   error=code)
            _publish(attempt / "outcome.json", outcome)
            if outcome["token_after"] is not None and _durable_progress(outcome):
                pointer = {"version": 1, "attempt_id": attempt_id,
                           "original_sha256": outcome["original_sha256"],
                           "manifest_sha256": outcome["manifest_sha256"],
                           "token_after": outcome["token_after"],
                           "outcome_sha256": sha256((attempt / "outcome.json").read_bytes()).hexdigest()}
                pointer_temp = root / ("resume-latest." + uuid.uuid4().hex + ".tmp")
                workflow._write_new(pointer_temp, pointer)
                os.replace(pointer_temp, root / "resume-latest.json")
            return outcome
    except workflow.WorkflowError as exc:
        raise ResumeError(str(exc)) from exc


def _read_resume_result(task_root, base, attempt_id, *, owner_guard) -> dict:
    """Read durable attempt evidence and recompute every claimed artifact hash."""
    unknown = {"ok": False, "state": "unknown", "attempt_id": attempt_id}
    if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
        return unknown
    try:
        root = _task(task_root, base)
        owner_guard(root)
        attempt = root / ("resume-" + attempt_id)
        if _linked(attempt) or not attempt.is_dir():
            return unknown
        request = _json_bytes(_small_bytes(attempt / "request.json", workflow._MAX_JSON))
        outcome = _json_bytes(_small_bytes(attempt / "outcome.json", workflow._MAX_JSON))
        if (request != {"version": 1, "attempt_id": attempt_id, "task_root": str(root),
                        "expected_token": outcome.get("token_before")}
                or outcome.get("attempt_id") != attempt_id
                or outcome.get("task_root") not in (None, str(root))):
            return unknown
        if (type(outcome.get("token_before")) is not str
                or not _DIGEST.fullmatch(outcome["token_before"])
                or outcome.get("state") not in {"succeeded", "cancelled", "timed_out",
                                                "blocked", "unknown"}
                or type(outcome.get("ok")) is not bool):
            return unknown
        snapshot = _owned_snapshot(root, base, owner_guard)
        if (outcome.get("original_sha256") != snapshot["original_sha256"]
                or outcome.get("manifest_sha256") != snapshot["manifest_sha256"]
                or outcome.get("token_after") != snapshot["token"]
                or outcome.get("workflow_root") != str(snapshot["workflow_root"])
                or outcome.get("completed") != len(snapshot["receipts"])
                or outcome.get("total") != len(snapshot["steps"])
                or outcome.get("remaining") != len(snapshot["steps"]) - len(snapshot["receipts"])
                or outcome.get("artifacts") != _outcome(snapshot, attempt_id,
                                                         outcome["token_before"],
                                                         outcome["state"])["artifacts"]):
            return unknown
        if (outcome.get("ok") is True) != (outcome.get("state") == "succeeded"
                                               and len(snapshot["receipts"]) == len(snapshot["steps"])):
            return unknown
        if _durable_progress(outcome):
            run = _json_bytes(_small_bytes(attempt / "result.json", workflow._MAX_JSON))
            blocked = outcome["state"] == "blocked"
            if (run.get("state") != ("failed" if blocked else outcome["state"])
                    or run.get("ok") is not outcome["ok"]
                    or run.get("root") != str(snapshot["workflow_root"])
                    or run.get("completed") != list(snapshot["receipts"])
                    or run.get("receipts") != snapshot["receipts"]):
                return unknown
            if blocked and (outcome["remaining"] < 1 or run.get("error") != outcome["error"]
                    or run.get("step") != snapshot["steps"][len(snapshot["receipts"])]["id"]):
                return unknown
        return outcome
    except (ValueError, TypeError, KeyError, OSError):
        return unknown


def _latest_continuation(task_root, base, *, owner_guard):
    """Return a trusted latest overlay only when pointer and outcome still agree."""
    try:
        root = _task(task_root, base)
        owner_guard(root)
        pointer = _json_bytes(_small_bytes(root / "resume-latest.json", workflow._MAX_JSON))
        if set(pointer) != {"version", "attempt_id", "original_sha256", "manifest_sha256",
                            "token_after", "outcome_sha256"} or pointer["version"] != 1:
            return None
        attempt_id = pointer["attempt_id"]
        if type(attempt_id) is not str or not _ATTEMPT.fullmatch(attempt_id):
            return None
        path = root / ("resume-" + attempt_id) / "outcome.json"
        raw = _small_bytes(path, workflow._MAX_JSON)
        if sha256(raw).hexdigest() != pointer["outcome_sha256"]:
            return None
        outcome = _read_resume_result(root, base, attempt_id, owner_guard=owner_guard)
        if (not _durable_progress(outcome)
                or pointer["original_sha256"] != outcome["original_sha256"]
                or pointer["manifest_sha256"] != outcome["manifest_sha256"]
                or pointer["token_after"] != outcome["token_after"]):
            return None
        return outcome
    except (ValueError, TypeError, KeyError, OSError):
        return None


def preview_resume(task_root, base):
    return _preview_resume(task_root, base, owner_guard=_assert_plain_office)


def resume_task(task_root, base, expected_token, attempt_id, *, execute=None, deadline=None):
    return _resume_task(task_root, base, expected_token, attempt_id,
                        owner_guard=_assert_plain_office, execute=execute, deadline=deadline)


def read_resume_result(task_root, base, attempt_id):
    return _read_resume_result(task_root, base, attempt_id, owner_guard=_assert_plain_office)


def latest_continuation(task_root, base):
    return _latest_continuation(task_root, base, owner_guard=_assert_plain_office)
