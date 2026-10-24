"""Durable, bounded Office steps above the frozen voice runtime.

The workflow accepts data, never a path or an application command from a model.
An intent without a verified receipt is an unknown effect and is never replayed.
"""

from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import time
import uuid


_WORKFLOW_NAME = re.compile(r"workflow-[0-9a-f]{32}\Z")
_RESUME_NAME = re.compile(r"(?:resume|skill-resume)-[0-9a-f]{32}\Z")
_STEP_NAME = re.compile(r"s[1-8]\Z")
_MAX_FILE = 20_000_000
_MAX_EMAIL_FILE = 45_000_000
_MAX_JSON = 1_000_000


class WorkflowError(ValueError):
    pass


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_new(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if len(data) > _MAX_JSON:
        raise WorkflowError("record_oversize")
    with path.open("xb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON:
        raise WorkflowError("record_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise WorkflowError("record_invalid") from exc
    if type(value) is not dict:
        raise WorkflowError("record_invalid")
    return value


def _finish(root, result):
    """Publish the latest outcome after fsync, including interrupted outcomes."""
    temporary = root / ("result-" + uuid.uuid4().hex + ".tmp")
    _write_new(temporary, result)
    os.replace(temporary, root / "result.json")
    return result


def _owned_dir(path):
    path = Path(path).absolute()
    if not _WORKFLOW_NAME.fullmatch(path.name) or path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()):
        raise WorkflowError("workflow_root_invalid")
    if path.exists():
        if not path.is_dir():
            raise WorkflowError("workflow_root_invalid")
    else:
        path.mkdir(parents=False, exist_ok=True)
    if not path.is_dir() or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise WorkflowError("workflow_root_invalid")
    return path.resolve()


@contextmanager
def _owner(root):
    """OS-held lock remains valid across processes and cannot go stale on crash."""
    path = root / "owner.lock"
    if (path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
            or (path.exists() and not path.is_file())):
        raise WorkflowError("workflow_lock_invalid")
    with path.open("a+b") as file:
        try:
            file.seek(0)
            if not file.read(1):
                file.seek(0)
                file.write(b"0")
                file.flush()
                os.fsync(file.fileno())
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkflowError("workflow_busy") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _office_spec(spec):
    from .office import validate_spec
    checked = validate_spec(spec)
    if type(checked) is not dict or checked.get("kind") != spec.get("kind"):
        raise WorkflowError("office_spec_invalid")
    return checked


def _draft_data(data):
    from .email_draft import validate_draft
    checked = validate_draft(data)
    if type(checked) is not dict or set(checked) != {"subject", "body", "to"}:
        raise WorkflowError("draft_spec_invalid")
    return checked


def _steps(steps):
    if type(steps) is not list or not 1 <= len(steps) <= 8:
        raise WorkflowError("steps_invalid")
    normalized = []
    prior = {}
    for index, step in enumerate(steps, 1):
        if type(step) is not dict or step.get("id") != f"s{index}" or not _STEP_NAME.fullmatch(step["id"]):
            raise WorkflowError("step_id_invalid")
        action = step.get("action")
        if action in {"document", "spreadsheet"}:
            if set(step) != {"id", "action", "data"} or type(step["data"]) is not dict:
                raise WorkflowError("step_schema_invalid")
            data = dict(step["data"])
            if data.get("kind") != action:
                raise WorkflowError("step_schema_invalid")
            checked = _office_spec(data)
            normalized.append({"id": step["id"], "action": action, "data": checked})
        elif action == "document_from_sheet":
            if set(step) != {"id", "action", "from", "title", "paragraphs"}:
                raise WorkflowError("step_schema_invalid")
            source = step["from"]
            if type(source) is not str or prior.get(source) != "spreadsheet":
                raise WorkflowError("step_reference_invalid")
            if type(step["title"]) is not str or type(step["paragraphs"]) is not list:
                raise WorkflowError("step_schema_invalid")
            # Validate the user-controlled part before any preceding step executes.
            checked = _office_spec({"kind": "document", "title": step["title"],
                                    "paragraphs": step["paragraphs"]})
            normalized.append({"id": step["id"], "action": action, "from": source,
                               "title": checked["title"], "paragraphs": checked["paragraphs"]})
        elif action == "email_draft":
            if set(step) != {"id", "action", "data", "attachments"}:
                raise WorkflowError("step_schema_invalid")
            attachments = step["attachments"]
            if type(attachments) is not list or len(attachments) > 4:
                raise WorkflowError("step_reference_invalid")
            if (any(type(ref) is not str or prior.get(ref) not in
                    {"document", "spreadsheet", "document_from_sheet"} for ref in attachments)
                    or len(set(attachments)) != len(attachments)):
                raise WorkflowError("step_reference_invalid")
            checked = _draft_data(step["data"])
            normalized.append({"id": step["id"], "action": action,
                               "data": checked, "attachments": list(attachments)})
        else:
            raise WorkflowError("action_unsupported")
        prior[step["id"]] = action
    return normalized


def validate_steps(steps):
    """Validate and canonicalize a model-proposed fixed Office step manifest."""
    return _steps(steps)


def _artifact(step_root, result, *, max_size=_MAX_FILE):
    path_value = result.get("artifact")
    reported = result.get("sha256")
    if type(path_value) is not str or type(reported) is not str or not re.fullmatch(r"[0-9a-f]{64}", reported):
        raise WorkflowError("receipt_invalid")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file() or (hasattr(path, "is_junction") and path.is_junction()):
        raise WorkflowError("artifact_invalid")
    resolved = path.resolve()
    if resolved.parent != step_root.resolve() or path.stat().st_size > max_size:
        raise WorkflowError("artifact_scope_invalid")
    if sha256(path.read_bytes()).hexdigest() != reported:
        raise WorkflowError("artifact_hash_mismatch")
    return resolved


def _observed(action, observed):
    if type(observed) is not dict:
        raise WorkflowError("observation_invalid")
    if action == "spreadsheet":
        if not {"headers", "rows", "totals"} <= observed.keys():
            raise WorkflowError("observation_invalid")
        if type(observed["headers"]) is not list or type(observed["rows"]) is not list or type(observed["totals"]) is not dict:
            raise WorkflowError("observation_invalid")
        width = len(observed["headers"])
        if (not 1 <= width <= 12 or len(observed["rows"]) > 200
                or any(type(header) is not str or not header for header in observed["headers"])
                or any(type(row) is not list or len(row) != width for row in observed["rows"])):
            raise WorkflowError("observation_invalid")
        if any(type(cell) not in (str, int, float, bool, type(None)) for row in observed["rows"] for cell in row):
            raise WorkflowError("observation_invalid")
        if any(type(key) is not str or not key.isdecimal() or not 0 <= int(key) < width
               or type(value) not in (int, float) for key, value in observed["totals"].items()):
            raise WorkflowError("observation_invalid")
    elif not (type(observed.get("text")) is str or
              (type(observed.get("title")) is str and type(observed.get("paragraphs")) is list
               and all(type(p) is str for p in observed["paragraphs"]))):
        raise WorkflowError("observation_invalid")
    if action != "spreadsheet" and "table" in observed:
        table = observed["table"]
        if (type(table) is not dict or set(table) != {"headers", "rows"}
                or type(table["headers"]) is not list or type(table["rows"]) is not list):
            raise WorkflowError("observation_invalid")
        width = len(table["headers"])
        if (not 1 <= width <= 12 or len(table["rows"]) > 201
                or any(type(header) is not str for header in table["headers"])
                or any(type(row) is not list or len(row) != width
                       or any(type(cell) is not str for cell in row) for row in table["rows"])):
            raise WorkflowError("observation_invalid")
    if len(json.dumps(observed, ensure_ascii=False).encode("utf-8")) > _MAX_JSON:
        raise WorkflowError("observation_invalid")
    return observed


def _receipt(step, step_root, spec_hash, value, spec=None):
    if step["action"] == "email_draft":
        if (type(value) is not dict or value.get("ok") is not True
                or value.get("state") != "succeeded"
                or value.get("file_verified") is not True
                or value.get("draft_verified") is not True
                or "app" in value or "application_verified" in value):
            raise WorkflowError("effect_unverified")
        artifact = _artifact(step_root, value, max_size=_MAX_EMAIL_FILE)
        if artifact.name != "draft.eml" or spec is None:
            raise WorkflowError("artifact_type_invalid")
        expected = {"subject": spec["subject"], "body": spec["body"],
                    "to": spec["to"], "attachments": [
                        {"step": item["step"], "filename": item["step"] + Path(item["path"]).suffix.lower(),
                         "sha256": item["sha256"]} for item in spec["attachments"]]}
        observed = value.get("observed")
        if type(observed) is not dict or observed != expected:
            raise WorkflowError("observation_invalid")
        return {"step_id": step["id"], "action": step["action"], "input_sha256": spec_hash,
                "artifact": str(artifact), "sha256": value["sha256"], "observed": observed,
                "draft_verified": True, "file_verified": True}
    app = value.get("app") if type(value) is dict else None
    app_valid = (type(app) is dict and set(app) == {"progid", "name", "version"}
                 and all(type(item) is str and 1 <= len(item) <= 200 for item in app.values()))
    if (type(value) is not dict or value.get("ok") is not True or value.get("state") != "succeeded"
            or value.get("application_verified") is not True or value.get("file_verified") is not True
            or not app_valid):
        raise WorkflowError("effect_unverified")
    artifact = _artifact(step_root, value)
    expected_suffix = ".xlsx" if step["action"] == "spreadsheet" else ".docx"
    if artifact.suffix.casefold() != expected_suffix:
        raise WorkflowError("artifact_type_invalid")
    observed = _observed("spreadsheet" if step["action"] == "spreadsheet" else "document", value.get("observed"))
    return {"step_id": step["id"], "action": step["action"], "input_sha256": spec_hash,
            "artifact": str(artifact), "sha256": value["sha256"], "observed": observed,
            "app": value["app"], "application_verified": True, "file_verified": True}


def _spec(step, receipts, workflow_root):
    if step["action"] == "email_draft":
        attachments = []
        for ref in step["attachments"]:
            receipt = receipts[ref]
            # Check the saved artifact again immediately before assembling the draft.
            artifact = _artifact(workflow_root / ref, receipt)
            if artifact.suffix.lower() not in {".docx", ".xlsx"}:
                raise WorkflowError("artifact_type_invalid")
            attachments.append({"step": ref, "path": str(artifact), "sha256": receipt["sha256"]})
        return {"kind": "email_draft", **step["data"], "attachments": attachments}
    if step["action"] != "document_from_sheet":
        return step["data"]
    source = receipts[step["from"]]
    observed = _observed("spreadsheet", source["observed"])
    headers = list(observed["headers"])
    rows = [list(row) for row in observed["rows"]]
    totals = observed["totals"]
    if totals:
        total_row = [""] * len(headers)
        total_row[0] = "合计"
        for key, value in totals.items():
            if type(key) is not str or not key.isdecimal() or not 0 <= int(key) < len(headers) or type(value) not in (int, float):
                raise WorkflowError("observation_invalid")
            total_row[int(key)] = value
        rows.append(total_row)
    return _office_spec({"kind": "document", "title": step["title"],
                         "paragraphs": step["paragraphs"],
                         "table": {"headers": headers, "rows": rows}})


def _resume_preflight(steps, workflow_root):
    """Verify every existing step before allowing the first new effect.

    An absent or uncertain step followed by any later step directory is a
    conflict, including when that later directory has no receipt yet.
    """
    names = {step["id"] for step in steps}
    for entry in workflow_root.iterdir():
        if re.fullmatch(r"s[0-9]+", entry.name) and entry.name not in names:
            raise WorkflowError("later_step_conflict")
    receipts = {}
    first_gap = None
    unknown = None
    for step in steps:
        step_root = workflow_root / step["id"]
        if step_root.is_symlink() or (hasattr(step_root, "is_junction") and step_root.is_junction()):
            raise WorkflowError("step_scope_invalid")
        if not step_root.exists():
            if first_gap is None:
                first_gap = step["id"]
            continue
        if first_gap is not None:
            raise WorkflowError("later_step_conflict")
        if not step_root.is_dir():
            raise WorkflowError("step_scope_invalid")
        receipt_path = step_root / "receipt.json"
        if not receipt_path.exists():
            first_gap = step["id"]
            unknown = step["id"]
            continue
        spec = _spec(step, receipts, workflow_root)
        spec_hash = _hash(spec)
        intent = _read_json(step_root / "intent.json")
        if intent != {"step_id": step["id"], "input_sha256": spec_hash}:
            raise WorkflowError("intent_mismatch")
        receipt = _read_json(receipt_path)
        if (receipt.get("input_sha256") != spec_hash or receipt.get("step_id") != step["id"]
                or receipt.get("action") != step["action"]):
            raise WorkflowError("receipt_mismatch")
        validated = _receipt(step, step_root, spec_hash,
                             {"ok": True, "state": "succeeded", **receipt}, spec)
        if validated != receipt:
            raise WorkflowError("receipt_mismatch")
        receipts[step["id"]] = receipt
    return receipts, unknown


def run_office_workflow(steps, root: Path, *, execute=None, resume=False,
                        deadline=None, cancel_requested=None, cancel_file=None,
                        resume_guard=None, result_root=None) -> dict:
    """Run an owned workflow; resume only verified receipts and fresh next steps.

    The injected executor takes (validated_spec, exact_step_directory, should_stop).
    The default executor is imported lazily so this module can be audited offline.
    """
    normalized = _steps(steps)
    deadline = time.monotonic() + 600 if deadline is None else deadline
    try:
        finite_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
    except (OverflowError, ValueError):
        finite_deadline = False
    if not finite_deadline:
        raise WorkflowError("deadline_invalid")
    if cancel_requested is None:
        cancel_requested = lambda: False
    if not callable(cancel_requested):
        raise WorkflowError("cancel_callback_invalid")
    if resume_guard is not None and not callable(resume_guard):
        raise WorkflowError("resume_guard_invalid")
    if resume and not Path(root).is_dir():
        raise WorkflowError("workflow_root_invalid")
    workflow_root = _owned_dir(root)
    if (cancel_file is None) != (result_root is None):
        raise WorkflowError("resume_scope_invalid")
    if cancel_file is not None:
        cancel_file = Path(cancel_file)
        if not cancel_file.is_absolute() or cancel_file.is_symlink():
            raise WorkflowError("resume_scope_invalid")
    if result_root is not None:
        result_root = Path(result_root)
        if (not resume or not result_root.is_absolute() or not result_root.is_dir()
                or result_root.is_symlink()
                or (hasattr(result_root, "is_junction") and result_root.is_junction())
                or not _RESUME_NAME.fullmatch(result_root.name)
                or result_root.parent.resolve() != workflow_root.parent
                or result_root.resolve().parent != workflow_root.parent
                or cancel_file != result_root / "cancel.flag"):
            raise WorkflowError("resume_scope_invalid")
    readiness = None
    if execute is None:
        from .office_host import execute_office_supervised
        from .email_draft import execute_draft
        from .office_readiness import preflight_office_window
        readiness = preflight_office_window

        def execute(spec, step_root, should_stop):
            if spec["kind"] == "email_draft":
                return execute_draft(spec, step_root, should_stop)
            return execute_office_supervised(spec, step_root, should_stop)
    if not callable(execute):
        raise WorkflowError("executor_invalid")
    manifest = {"version": 1, "steps": normalized, "steps_sha256": _hash(normalized)}
    with _owner(workflow_root):
        manifest_path = workflow_root / "manifest.json"
        if resume:
            saved = _read_json(manifest_path)
            if saved != manifest:
                raise WorkflowError("manifest_mismatch")
        elif manifest_path.exists():
            raise WorkflowError("workflow_exists")
        else:
            _write_new(manifest_path, manifest)
        preflight_receipts = {}
        uncertain_step = None
        if resume:
            preflight_receipts, uncertain_step = _resume_preflight(normalized, workflow_root)
        if resume_guard is not None:
            resume_guard()
        cancelled = lambda: ((cancel_file if cancel_file is not None else
                              workflow_root / "cancel.flag").exists() or bool(cancel_requested()))
        expired = lambda: time.monotonic() >= deadline
        stop = lambda: cancelled() or expired()
        receipts = {}

        def partial(state, *, step=None, error=None):
            result = {"ok": False, "state": state, "root": str(workflow_root),
                      "completed": list(receipts), "receipts": dict(receipts)}
            if step is not None:
                result["step"] = step
            if error is not None:
                result["error"] = error
            return _finish(result_root or workflow_root, result)

        if uncertain_step is not None:
            receipts.update(preflight_receipts)
            return partial("unknown", step=uncertain_step)

        for step in normalized:
            step_root = workflow_root / step["id"]
            if step_root.is_symlink() or (hasattr(step_root, "is_junction") and step_root.is_junction()):
                raise WorkflowError("step_scope_invalid")
            spec = _spec(step, receipts, workflow_root)
            spec_hash = _hash(spec)
            receipt_path = step_root / "receipt.json"
            intent_path = step_root / "intent.json"
            if receipt_path.exists():
                intent = _read_json(intent_path)
                if intent != {"step_id": step["id"], "input_sha256": spec_hash}:
                    raise WorkflowError("intent_mismatch")
                receipt = _read_json(receipt_path)
                if receipt.get("input_sha256") != spec_hash or receipt.get("step_id") != step["id"] or receipt.get("action") != step["action"]:
                    raise WorkflowError("receipt_mismatch")
                # Recompute the artifact hash; never trust an old success record alone.
                validated = _receipt(step, step_root, spec_hash,
                                     {"ok": True, "state": "succeeded", **receipt}, spec)
                if validated != receipt:
                    raise WorkflowError("receipt_mismatch")
                receipts[step["id"]] = receipt
                continue
            if intent_path.exists() or step_root.exists():
                return partial("unknown", step=step["id"])
            if stop():
                return partial("cancelled" if cancelled() else "timed_out")
            if readiness is not None and spec["kind"] != "email_draft":
                try:
                    blocked = readiness()
                except Exception:
                    blocked = "office_ui_unavailable"
                if stop():
                    return partial("cancelled" if cancelled() else "timed_out")
                if blocked is not None:
                    code = blocked if type(blocked) is str and blocked in {"office_ui_blocked", "office_ui_unavailable"} else "office_ui_unavailable"
                    return partial("failed", step=step["id"], error=code)
            step_root.mkdir(exist_ok=False)
            _write_new(intent_path, {"step_id": step["id"], "input_sha256": spec_hash})
            if stop():
                return partial("unknown", step=step["id"])
            try:
                outcome = execute(spec, step_root, stop)
                receipt = _receipt(step, step_root, spec_hash, outcome, spec)
                _write_new(receipt_path, receipt)
            except Exception as exc:
                # An RPC or save may have reached Office even if its response did not.
                return partial("unknown", step=step["id"], error=type(exc).__name__)
            receipts[step["id"]] = receipt
        result = {"ok": True, "state": "succeeded", "root": str(workflow_root),
                  "completed": list(receipts), "receipts": receipts}
        return _finish(result_root or workflow_root, result)
