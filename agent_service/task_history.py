"""Read-only inspection of bounded assistant tasks and verified outputs.

Listing reads only small result metadata. Inspection rechecks owned workflow
receipts and file hashes; an unfinished intent never triggers execution.
"""

from hashlib import sha256
import json
from pathlib import Path
import re

from . import office_workflow as workflow


_TASK = re.compile(r"task-[0-9a-f]{32}\Z")
_WORKFLOW = re.compile(r"workflow-[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SKILL_ID = re.compile(r"[a-z][a-z0-9-]{2,47}\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_RESULT = 100_000
_MAX_REPORT = 100_000
_MAX_GAP = 100_000


def _linked(path):
    current = Path(path).absolute()
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _base(base):
    path = Path(base).absolute()
    if _linked(path):
        raise ValueError("task_base_invalid")
    return path.resolve()


def _task(path, base):
    candidate = Path(path).absolute()
    if (not _TASK.fullmatch(candidate.name) or _linked(candidate)
            or not candidate.is_dir() or candidate.parent.resolve() != base):
        raise ValueError("task_root_invalid")
    resolved = candidate.resolve()
    if resolved.parent != base:
        raise ValueError("task_root_invalid")
    return resolved


def _json_small(path, limit=_MAX_RESULT):
    try:
        if _linked(path) or not path.is_file() or path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if type(value) is dict else None
    except (OSError, UnicodeError, ValueError):
        return None


def _metadata(result, root):
    if result is None or result.get("root") != str(root):
        return "needs_review", "unknown"
    state = result.get("state")
    kind = result.get("kind")
    if type(state) is not str or len(state) > 40 or type(kind) is not str or len(kind) > 40:
        return "needs_review", "unknown"
    return state, kind


def list_tasks(base, limit=30):
    """Return recent small task records without hashing artifacts."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("task_limit_invalid")
    base = _base(base)
    if not base.is_dir():
        return []
    candidates = []
    try:
        for path in base.iterdir():
            if not _TASK.fullmatch(path.name) or _linked(path) or not path.is_dir():
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []
    candidates.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    records = []
    for mtime, path in candidates[:limit]:
        result = _json_small(path / "result.json")
        state, kind = _metadata(result, path.resolve())
        if result is None and (path/'edit'/'edit-manifest.json').is_file():
            native = _json_small(path/'edit'/'worker-outcome.json')
            state, kind = ('succeeded' if native and native.get('ok') is True else 'needs_review'), 'office_edit'
        records.append({"id": path.name, "root": str(path.resolve()),
                        "state": state, "kind": kind, "mtime": mtime})
    return records


def _workflow_root(path, task_root):
    if type(path) is not str:
        return None
    candidate = Path(path)
    if (not candidate.is_absolute() or not _WORKFLOW.fullmatch(candidate.name)
            or _linked(candidate) or not candidate.is_dir()
            or candidate.parent.resolve() != task_root):
        return None
    return candidate.resolve() if candidate.resolve().parent == task_root else None


def _single_workflow(task_root):
    candidates = []
    try:
        for path in task_root.iterdir():
            if _WORKFLOW.fullmatch(path.name) and not _linked(path) and path.is_dir():
                candidates.append(path)
    except OSError:
        return None
    return candidates[0].resolve() if len(candidates) == 1 else None


def _verified_workflow(workflow_root):
    """Read contiguous receipts; stop at the first absent or uncertain effect."""
    manifest = _json_small(workflow_root / "manifest.json", workflow._MAX_JSON)
    if (manifest is None or set(manifest) != {"version", "steps", "steps_sha256"}
            or type(manifest["version"]) is not int or manifest["version"] != 1):
        return None
    try:
        steps = workflow.validate_steps(manifest["steps"])
        if steps != manifest["steps"] or workflow._hash(steps) != manifest["steps_sha256"]:
            return None
    except (ValueError, TypeError, KeyError, OSError):
        return None
    receipts = {}
    for step in steps:
        step_root = workflow_root / step["id"]
        if _linked(step_root):
            return {"receipts": receipts, "complete": False, "uncertain": True}
        if not step_root.is_dir():
            return {"receipts": receipts, "complete": False, "uncertain": False}
        try:
            spec = workflow._spec(step, receipts, workflow_root)
            digest = workflow._hash(spec)
            intent = workflow._read_json(step_root / "intent.json")
            if intent != {"step_id": step["id"], "input_sha256": digest}:
                raise ValueError("intent_mismatch")
            receipt = workflow._read_json(step_root / "receipt.json")
            if (receipt.get("step_id") != step["id"] or receipt.get("action") != step["action"]
                    or receipt.get("input_sha256") != digest):
                raise ValueError("receipt_mismatch")
            checked = workflow._receipt(step, step_root, digest,
                                        {"ok": True, "state": "succeeded", **receipt}, spec)
            if checked != receipt:
                raise ValueError("receipt_mismatch")
            receipts[step["id"]] = receipt
        except (ValueError, TypeError, KeyError, OSError):
            return {"receipts": receipts, "complete": False, "uncertain": True}
    return {"receipts": receipts, "complete": True, "uncertain": False}


def _verified_report(path_value, digest, task_root):
    if type(path_value) is not str or type(digest) is not str or not _DIGEST.fullmatch(digest):
        return None
    path = Path(path_value)
    if (not path.is_absolute() or path.parent != task_root / "artifacts"
            or _linked(path) or _linked(path.parent) or not path.is_file()
            or path.suffix.lower() != ".md" or path.stat().st_size > _MAX_REPORT):
        return None
    resolved = path.resolve()
    if (resolved.parent != task_root / "artifacts"
            or not re.fullmatch(r"report-[0-9a-f]{32}\.md", resolved.name)):
        return None
    return resolved if sha256(resolved.read_bytes()).hexdigest() == digest else None


def _artifact_record(step, receipt):
    path = receipt["artifact"]
    suffix = Path(path).suffix.lower()
    label = {".docx": "文档 (.docx)", ".xlsx": "表格 (.xlsx)",
             ".eml": "未发送邮件草稿"}[suffix]
    return {"label": f"{step} · {label}", "path": path, "sha256": receipt["sha256"]}


def _verified_capability_gap(reference, root):
    """Project a hash-bound local gap record; never treat hints as executable."""
    if (type(reference) is not dict or set(reference) != {"filename", "sha256"}
            or reference["filename"] != "capability-gap.json"
            or type(reference["sha256"]) is not str
            or not _DIGEST.fullmatch(reference["sha256"])):
        return None
    path = root / "capability-gap.json"
    try:
        if (_linked(path) or not path.is_file() or not 0 < path.stat().st_size <= _MAX_GAP):
            return None
        raw = path.read_bytes()
        if len(raw) > _MAX_GAP or sha256(raw).hexdigest() != reference["sha256"]:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (type(value) is not dict or value.get("schema") != 1
            or type(value.get("instruction_sha256")) is not str
            or not _DIGEST.fullmatch(value["instruction_sha256"])):
        return None
    discovery = value.get("discovery")
    candidates = []
    if type(discovery) is dict and discovery.get("schema") == 1:
        raw_candidates = discovery.get("candidates")
        if type(raw_candidates) is not list or len(raw_candidates) > 50:
            return None
        for candidate in raw_candidates:
            if type(candidate) is not dict:
                return None
            skill_id = candidate.get("id")
            digest = candidate.get("sha256")
            implementation = candidate.get("implementation_sha256")
            status = candidate.get("status")
            name = candidate.get("name")
            recipe = candidate.get("recipe")
            remaining = candidate.get("remaining_runs")
            if (type(skill_id) is not str or not _SKILL_ID.fullmatch(skill_id)
                    or type(digest) is not str or not _DIGEST.fullmatch(digest)
                    or (implementation is not None and (type(implementation) is not str
                        or not _DIGEST.fullmatch(implementation)))
                    or status == "available" and implementation is None
                    or status not in {"available", "disabled", "incompatible"}
                    or type(name) is not str or not 1 <= len(name) <= 80
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or type(recipe) is not str or len(recipe) > 40
                    or type(remaining) is not int or not 0 <= remaining <= 10):
                return None
            candidates.append({"id": skill_id, "sha256": digest,
                               "implementation_sha256": implementation,
                               "name": name, "recipe": recipe, "status": status,
                               "remaining_runs": remaining})
    elif discovery is not None:
        return None
    request = value.get("development_request")
    development = (type(request) is dict and request.get("state") == "queued"
                   and request.get("automatic_code_execution") is False)
    return {"candidates": candidates, "development_queued": development,
            "sha256": reference["sha256"]}


def _verified_skill_preflight(result, root):
    """A claimed input failure is safe only before any Office workflow exists."""
    if (result.get("kind") != "skill" or result.get("state") != "failed"
            or result.get("phase") != "preflight" or result.get("effect_started") is not False
            or result.get("ok") is not False):
        return None
    code = result.get("error")
    skill = result.get("skill")
    if (type(code) is not str or not _ERROR_CODE.fullmatch(code)
            or type(skill) is not dict or set(skill) != {
                "id", "sha256", "implementation_sha256", "mode"}
            or type(skill["id"]) is not str or not _SKILL_ID.fullmatch(skill["id"])
            or any(type(skill[key]) is not str or not _DIGEST.fullmatch(skill[key])
                   for key in ("sha256", "implementation_sha256"))
            or skill["mode"] not in {"trial", "active"}):
        return None
    request = _json_small(root / "skill-request.json", workflow._MAX_JSON)
    if (request is None or type(request.get("manifest")) is not dict
            or request["manifest"].get("id") != skill["id"]
            or workflow._hash(request["manifest"]) != skill["sha256"]
            or request.get("sha256") != skill["sha256"]
            or request.get("implementation_sha256") != skill["implementation_sha256"]
            or request.get("mode") != skill["mode"]
            or request.get("executor") not in {"supervised_office", "injected_test"}):
        return None
    reserved = request.get("workflow_root")
    if (type(reserved) is not str or not Path(reserved).is_absolute()
            or not _WORKFLOW.fullmatch(Path(reserved).name)
            or Path(reserved).parent != root or _linked(Path(reserved))
            or Path(reserved).exists()):
        return None
    try:
        if any(entry.name.startswith("workflow-") for entry in root.iterdir()):
            return None
    except OSError:
        return None
    return {"code": code, "skill_id": skill["id"], "mode": skill["mode"]}


def inspect_task(task_root, base):
    """Inspect one task without writing, locking, or replaying an intent."""
    base = _base(base)
    root = _task(task_root, base)
    if (root/'edit'/'edit-manifest.json').is_file():
        output = {'root':str(root),'state':'needs_review','kind':'office_edit','artifacts':[],
                  'resume':{'eligible':False,'reason':'resume_unavailable'}}
        try:
            from .edited_artifact_source import prepare_reference
            from .computer_agent_tools import _load
            manifest, _ = _load(root/'edit'/'edit-manifest.json')
            record, _ = _load(Path(manifest['step'])/'environment-result.json')
            native = record['native']
            ref = prepare_reference(native['artifact'],native['sha256'],base)
            if Path(ref['artifact']).parent != root/'edit':
                raise ValueError('edit_history_owner_invalid')
            output.update(state='succeeded',artifacts=[{'label':'已编辑副本 ('+Path(ref['artifact']).suffix+')',
                           'path':ref['artifact'],'sha256':ref['sha256']}])
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return output
    result = _json_small(root / "result.json")
    state, kind = _metadata(result, root)
    skill_owned = (kind == "office" and type(result.get("skill")) is dict)
    if skill_owned and state == "cancelled":
        from .skill_resume import latest_continuation, preview_resume
    else:
        from .office_resume import latest_continuation, preview_resume
    output = {"root": str(root), "state": state, "kind": kind, "artifacts": [],
              "resume": preview_resume(root, base)}
    if result is not None and result.get("root") == str(root):
        if kind == "office":
            workflow_root = _workflow_root(result.get("workflow_root"), root)
            if workflow_root is None:
                output["state"] = "needs_review"
                return output
            output["workflow_root"] = str(workflow_root)
            verified = _verified_workflow(workflow_root)
            if verified is None:
                output["state"] = "needs_review"
                return output
            artifacts = result.get("artifacts")
            if type(artifacts) is not list:
                output["state"] = "needs_review"
                artifacts = []
            seen = set()
            for item in artifacts:
                if type(item) is not dict or set(item) != {"step", "path", "sha256"}:
                    output["state"] = "needs_review"
                    continue
                step = item["step"]
                receipt = verified["receipts"].get(step) if type(step) is str else None
                if (step in seen or receipt is None or item["path"] != receipt["artifact"]
                        or item["sha256"] != receipt["sha256"]):
                    output["state"] = "needs_review"
                    continue
                seen.add(step)
                output["artifacts"].append(_artifact_record(step, receipt))
            if (state == "succeeded" and (not verified["complete"]
                    or len(seen) != len(verified["receipts"]))):
                output["state"] = "needs_review"
            continuation = latest_continuation(root, base)
            if skill_owned and (type(continuation) is not dict
                                or continuation.get("kind") != "skill_resume"):
                continuation = None
            if continuation is not None:
                output["original_state"] = state
                output["continuation"] = {"state": continuation["state"],
                                          "attempt_id": continuation["attempt_id"]}
                if skill_owned:
                    output["continuation"]["kind"] = "skill_resume"
                for item in continuation["artifacts"]:
                    if item["step"] not in seen:
                        output["artifacts"].append(_artifact_record(
                            item["step"], {"artifact": item["path"], "sha256": item["sha256"]}))
                        seen.add(item["step"])
            report_path = result.get("research_artifact")
            report_hash = result.get("research_sha256")
        elif kind == "research":
            report_path = result.get("artifact")
            report_hash = result.get("readback_sha256")
        elif kind == "unsupported":
            reference = result.get("capability_gap")
            if reference is not None:
                gap = _verified_capability_gap(reference, root)
                if gap is None:
                    output["state"] = "needs_review"
                else:
                    output["capability_gap"] = gap
            return output
        elif kind == "skill":
            if result.get("phase") == "preflight":
                preflight = _verified_skill_preflight(result, root)
                if preflight is None:
                    output["state"] = "needs_review"
                else:
                    output["input_failure"] = preflight
            return output
        else:
            return output
        try:
            report = _verified_report(report_path, report_hash, root)
        except OSError:
            report = None
        if report is not None:
            output["artifacts"].append({"label": "资料归纳与来源 (.md)",
                                        "path": str(report), "sha256": report_hash})
        elif report_path is not None and state == "succeeded":
            output["state"] = "needs_review"
        return output

    # Without a final assistant result, only one owned Office workflow can be
    # identified unambiguously. A verified final step is not task success.
    workflow_root = _single_workflow(root)
    if workflow_root is None:
        return output
    output["kind"] = "office"
    output["workflow_root"] = str(workflow_root)
    verified = _verified_workflow(workflow_root)
    if verified is None:
        return output
    output["artifacts"] = [_artifact_record(step, receipt)
                           for step, receipt in verified["receipts"].items()]
    output["state"] = "recovered" if verified["complete"] else "needs_review"
    return output
