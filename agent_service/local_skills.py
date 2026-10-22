"""Bounded declarative Office recipes, verified before limited local activation.

This is a local development publisher, not a security boundary against a user who
can rewrite the application. Candidate JSON cannot supply code or grant actions.
All effects go through the existing source/Office contracts, without a model.
"""

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
from .mail_export import _prepare_task, _publish_parent
from .office_source import _linked, snapshot_source
from .office_source_plan import resolve_source_steps


_ID = re.compile(r"[a-z][a-z0-9-]{2,47}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODE = ("local_skills.py", "office.py", "office_host.py", "office_workflow.py",
         "office_source.py", "office_source_plan.py", "artifact_source.py",
         "task_history.py", "email_draft.py", "mail_export.py", "office_resume.py",
         "assistant.py", "skill_matching.py", "skill_resume.py", "gap_recovery.py",
         "source_intake.py", "workbook_source.py", "workbook_source_host.py", "workbook_plan.py",
         "edited_artifact_source.py", "office_edit.py", "office_edit_host.py")


class SkillError(ValueError):
    pass


def implementation_sha256():
    root = Path(__file__).parent
    files = {name: sha256((root / name).read_bytes()).hexdigest() for name in _CODE}
    files["../run_jarvis_desktop.py"] = sha256((root.parent / "run_jarvis_desktop.py").read_bytes()).hexdigest()
    files["../run_jarvis_skill_resume.py"] = sha256((root.parent / "run_jarvis_skill_resume.py").read_bytes()).hexdigest()
    return workflow._hash(files)


def validate_manifest(value):
    required = {"schema", "id", "version", "name", "recipe", "sum_columns", "draft"}
    if type(value) is not dict or set(value) != required:
        raise SkillError("skill_manifest_invalid")
    if (type(value["schema"]) is not int or value["schema"] != 1
            or type(value["version"]) is not int or not 1 <= value["version"] <= 9999
            or type(value["id"]) is not str or not _ID.fullmatch(value["id"])
            or type(value["name"]) is not str or not 1 <= len(value["name"].strip()) <= 80
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value["name"])
            or value["recipe"] not in ("table_report", "table_copy", "document_copy", "document_table_report",
                                       "document_numeric_report")
            or type(value["draft"]) is not bool):
        raise SkillError("skill_manifest_invalid")
    columns = value["sum_columns"]
    if (type(columns) is not list or len(columns) > 12
            or any(type(c) is not int or not 0 <= c < 12 for c in columns)
            or len(set(columns)) != len(columns)
            or value["recipe"] not in {"table_report", "document_numeric_report"} and columns
            or value["recipe"] == "document_numeric_report" and (not columns or 0 in columns)
            or value["recipe"] == "table_copy" and value["draft"]):
        raise SkillError("skill_columns_invalid")
    return json.loads(json.dumps(value, ensure_ascii=False))


def build_steps(manifest, snapshot):
    candidate = validate_manifest(manifest)
    title = candidate["name"]
    if candidate["recipe"] in {"table_report", "table_copy"}:
        if snapshot.get("kind") != "table":
            raise SkillError("skill_source_kind_mismatch")
        width = len(snapshot["observed"]["headers"])
        steps = [{"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
                  "title": title, "columns": list(range(width)),
                  "sum_columns": candidate["sum_columns"]}]
        if candidate["recipe"] == "table_report":
            steps.append({"id": "s2", "action": "report_from_sheet", "from": "s1", "title": title + " · 汇总"})
    elif candidate["recipe"] in {"document_table_report", "document_numeric_report"}:
        if snapshot.get("kind") != "document" or "table" not in snapshot.get("observed", {}):
            raise SkillError("skill_document_table_required")
        width = len(snapshot["observed"]["table"]["headers"])
        first = {"id": "s1", "action": "spreadsheet_from_document_table", "source_id": "selected",
                 "title": title, "columns": list(range(width))}
        if candidate["recipe"] == "document_numeric_report":
            first["action"] = "spreadsheet_from_document_table_numeric"
            first["numeric_columns"] = candidate["sum_columns"][:]
            first["sum_columns"] = candidate["sum_columns"][:]
        steps = [first, {"id": "s2", "action": "report_from_sheet", "from": "s1",
                         "title": title + " · 报告"}]
    else:
        if snapshot.get("kind") != "document":
            raise SkillError("skill_source_kind_mismatch")
        steps = [{"id": "s1", "action": "document_from_source", "source_id": "selected", "title": title}]
    if candidate["draft"]:
        attachments = [step["id"] for step in steps]
        steps.append({"id": "s" + str(len(steps) + 1), "action": "email_draft",
                      "data": {"subject": title, "body": "附件为本次整理的文档。此邮件尚未发送。", "to": []},
                      "attachments": attachments})
    return resolve_source_steps(steps, snapshot)


def _store(path, create=False):
    path = Path(path).absolute()
    if ".." in path.parts or _linked(path):
        raise SkillError("skill_store_invalid")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or _linked(path):
        raise SkillError("skill_store_invalid")
    return path.resolve()


def _version(store, skill_id, digest):
    if (type(skill_id) is not str or not _ID.fullmatch(skill_id)
            or type(digest) is not str or not _HASH.fullmatch(digest)):
        raise SkillError("skill_identity_invalid")
    path = _store(store) / skill_id / digest
    if _linked(path) or not path.is_dir():
        raise SkillError("skill_candidate_missing")
    candidate = validate_manifest(workflow._read_json(path / "manifest.json"))
    if candidate["id"] != skill_id or workflow._hash(candidate) != digest:
        raise SkillError("skill_candidate_changed")
    return path, candidate


def _record(path, value):
    if _linked(path) or path.exists() and not path.is_file():
        raise SkillError("skill_record_invalid")
    temp = path.parent / (path.stem + "-" + uuid.uuid4().hex + ".tmp")
    workflow._write_new(temp, value)
    os.replace(temp, path)


def register_candidate(store, manifest):
    candidate = validate_manifest(manifest)
    digest = workflow._hash(candidate)
    root = _store(store, create=True)
    with workflow._owner(root):
        family = root / candidate["id"]
        if _linked(family):
            raise SkillError("skill_store_invalid")
        family.mkdir(exist_ok=True)
        version = family / digest
        if version.exists():
            _version(root, candidate["id"], digest)
        else:
            version.mkdir(exist_ok=False)
            workflow._write_new(version / "manifest.json", candidate)
    return {"id": candidate["id"], "sha256": digest, "state": "candidate", "enabled": False}


def _fixture(candidate):
    snapshot = {"source_id": "selected", "kind": "document", "sha256": "a" * 64,
                "display_name": "合成验收.docx", "observed": {"paragraphs": ["合成正文", "原文保留"]}}
    if candidate["recipe"] in {"table_report", "table_copy"}:
        width = max([1, *candidate["sum_columns"]]) + 1
        snapshot.update(kind="table", display_name="合成验收.csv",
                        observed={"headers": ["列" + str(i + 1) for i in range(width)],
                                  "rows": [[3] * width, [5] * width]})
    elif candidate["recipe"] == "document_table_report":
        snapshot["observed"]["table"] = {
            "headers": ["编号", "数量"],
            "rows": [["001", "3.0"], ["=1+1", "5.0"], ["合计", "8.0"], [" 原文 ", ""]]}
    elif candidate["recipe"] == "document_numeric_report":
        width = max([1, *candidate["sum_columns"]]) + 1
        headers = ["编号"] + ["数值" + str(index) for index in range(1, width)]
        snapshot["observed"]["table"] = {
            "headers": headers,
            "rows": [["001", *["3.0"] * (width - 1)],
                     ["=1+1", *["5.0"] * (width - 1)]]}
    return snapshot


def verify_offline(store, skill_id, digest):
    root = _store(store)
    with workflow._owner(root):
        path, candidate = _version(root, skill_id, digest)
        fixture = _fixture(candidate)
        steps = build_steps(candidate, fixture)
        # This is a deterministic contract check, never represented as COM/live proof.
        if any(step["action"] not in {"spreadsheet", "document", "document_from_sheet", "email_draft"}
               for step in steps):
            raise SkillError("skill_action_invalid")
        if candidate["recipe"] in {"table_report", "table_copy"} and steps[0]["data"]["rows"] != fixture["observed"]["rows"]:
            raise SkillError("skill_data_changed")
        if candidate["recipe"] == "table_copy" and (len(steps) != 1 or steps[0]["data"]["sum_columns"]):
            raise SkillError("skill_data_changed")
        if candidate["recipe"] == "document_table_report":
            table = fixture["observed"]["table"]
            sheet = steps[0]["data"]
            if (sheet["rows"] != table["rows"] or sheet["headers"] != table["headers"]
                    or sheet["sum_columns"]):
                raise SkillError("skill_data_changed")
        if candidate["recipe"] == "document_numeric_report":
            from .office import _expected
            table = fixture["observed"]["table"]
            sheet = steps[0]["data"]
            if (len(steps) < 2 or sheet["headers"] != table["headers"]
                    or sheet["sum_columns"] != candidate["sum_columns"]
                    or _expected(sheet)["totals"] != {str(column): 8.0 for column in candidate["sum_columns"]}
                    or sheet["rows"][0][0] != "001" or sheet["rows"][1][0] != "=1+1"
                    or any(type(row[column]) not in (int, float) for row in sheet["rows"]
                           for column in candidate["sum_columns"])
                    or "重新求和" not in steps[1]["paragraphs"][-1]):
                raise SkillError("skill_data_changed")
        wrong = dict(fixture, kind="table" if fixture["kind"] == "document" else "document")
        try:
            build_steps(candidate, wrong)
        except ValueError:
            pass
        else:
            raise SkillError("skill_rejection_failed")
        result = {"schema": 1, "candidate_sha256": digest, "implementation_sha256": implementation_sha256(),
                  "passed": True, "kind": "offline_contract", "fixture_steps_sha256": workflow._hash(steps)}
        _record(path / "offline.json", result)
        return result


def record_review(store, skill_id, digest, reviewer, note):
    """Maintainer entry point after independent review; never read from candidate JSON."""
    if (type(reviewer) is not str or not 1 <= len(reviewer.strip()) <= 100
            or type(note) is not str or not 1 <= len(note.strip()) <= 2000):
        raise SkillError("skill_review_invalid")
    root = _store(store)
    with workflow._owner(root):
        path, _ = _version(root, skill_id, digest)
        _gate_file(path, "offline.json", digest)
        result = {"schema": 1, "candidate_sha256": digest, "implementation_sha256": implementation_sha256(),
                  "passed": True, "kind": "maintainer_review", "reviewer": reviewer, "note": note}
        _record(path / "review.json", result)
        return result


def _gate_file(path, name, digest):
    record = workflow._read_json(path / name)
    if (record.get("schema") != 1 or record.get("candidate_sha256") != digest
            or record.get("implementation_sha256") != implementation_sha256()
            or record.get("passed") is not True):
        raise SkillError("skill_gate_stale")
    return record


def _state(root, skill_id):
    path = root / skill_id / "active.json"
    if not path.exists():
        if _linked(path):
            raise SkillError("skill_state_invalid")
        return {"active": None, "previous": None, "remaining_runs": 0, "inflight": None, "blocked": []}
    value = workflow._read_json(path)
    if (set(value) != {"active", "previous", "remaining_runs", "inflight", "blocked"}
            or type(value["remaining_runs"]) is not int or not 0 <= value["remaining_runs"] <= 10
            or type(value["blocked"]) is not list
            or any(type(d) is not str or not _HASH.fullmatch(d) for d in value["blocked"])
            or any(d is not None and (type(d) is not str or not _HASH.fullmatch(d))
                   for d in [value["active"], value["previous"]])
            or value["inflight"] is not None and type(value["inflight"]) is not str):
        raise SkillError("skill_state_invalid")
    return value


def _live_check(path, digest, base):
    record = _gate_file(path, "live.json", digest)
    root = history._task(record.get("task_root"), history._base(base))
    _verify_success(root, base, digest, record.get("task_result_sha256"))
    return record


def _verify_success(root, base, digest, expected_result_hash=None):
    root = history._task(root, history._base(base))
    if _linked(root):
        raise SkillError("skill_task_invalid")
    result_path = root / "result.json"
    result = workflow._read_json(result_path)
    if expected_result_hash is not None and sha256(result_path.read_bytes()).hexdigest() != expected_result_hash:
        raise SkillError("skill_live_result_changed")
    info = result.get("skill", {})
    if (result.get("ok") is not True or result.get("state") != "succeeded"
            or info.get("sha256") != digest or info.get("implementation_sha256") != implementation_sha256()
            or info.get("mode") not in {"trial", "active"}):
        raise SkillError("skill_live_result_invalid")
    inspected = history.inspect_task(root, base)
    if inspected["state"] != "succeeded" or not inspected["artifacts"]:
        raise SkillError("skill_live_receipts_invalid")
    workflow_root = Path(inspected["workflow_root"])
    manifest = workflow._read_json(workflow_root / "manifest.json")
    request = workflow._read_json(root / "skill-request.json")
    candidate = validate_manifest(request.get("manifest"))
    snapshot = workflow._read_json(root / "sources" / "source.json")
    if (workflow._hash(candidate) != digest or candidate["id"] != info.get("id")
            or request.get("sha256") != digest or request.get("mode") != info.get("mode")
            or request.get("executor") != "supervised_office"
            or request.get("implementation_sha256") != info.get("implementation_sha256")
            or request.get("workflow_root") != str(workflow_root)
            or build_steps(candidate, snapshot) != manifest["steps"]):
        raise SkillError("skill_live_plan_mismatch")
    source_files = [item for item in (root / "sources").iterdir() if item.name in
                    {"source.csv", "source.xlsx", "source.docx"}]
    if (len(source_files) != 1 or _linked(source_files[0]) or not source_files[0].is_file()
            or source_files[0].stat().st_size > 20_000_000
            or sha256(source_files[0].read_bytes()).hexdigest() != snapshot["sha256"]):
        raise SkillError("skill_live_source_changed")
    receipts, unknown = workflow._resume_preflight(manifest["steps"], workflow_root)
    if unknown or len(receipts) != len(manifest["steps"]):
        raise SkillError("skill_live_receipts_invalid")
    from .office import verify_office_file
    for step in manifest["steps"]:
        receipt = receipts[step["id"]]
        if step["action"] != "email_draft":
            if receipt["app"]["progid"] not in {"KWPS.Application", "KET.Application", "Word.Application", "Excel.Application"}:
                raise SkillError("skill_live_app_unverified")
            spec = workflow._spec(step, receipts, workflow_root)
            step_root = workflow_root / step["id"]
            worker_request = workflow._read_json(step_root / "worker-request.json")
            worker_result = workflow._read_json(step_root / "worker-outcome.json")
            if (worker_request != {"spec": spec}
                    or workflow._receipt(step, step_root, workflow._hash(spec), worker_result, spec) != receipt):
                raise SkillError("skill_live_worker_mismatch")
            verify_office_file(Path(receipt["artifact"]), spec)
    return result


def record_live(store, skill_id, digest, task_root, base):
    root = _store(store)
    with workflow._owner(root):
        path, _ = _version(root, skill_id, digest)
        for name in ("offline.json", "review.json"):
            _gate_file(path, name, digest)
        task = history._task(task_root, history._base(base))
        result = _verify_success(task, base, digest)
        if result["skill"]["id"] != skill_id:
            raise SkillError("skill_live_identity_mismatch")
        request = workflow._read_json(task / "skill-request.json")
        if request["manifest"] != validate_manifest(workflow._read_json(path / "manifest.json")):
            raise SkillError("skill_live_candidate_mismatch")
        result = {"schema": 1, "candidate_sha256": digest, "implementation_sha256": implementation_sha256(),
                  "passed": True, "kind": "live_office", "task_root": str(task),
                  "task_result_sha256": sha256((task / "result.json").read_bytes()).hexdigest()}
        _record(path / "live.json", result)
        return result


def activate(store, skill_id, digest, base, *, runs=10):
    if type(runs) is not int or not 1 <= runs <= 10:
        raise SkillError("skill_activation_limit_invalid")
    root = _store(store)
    with workflow._owner(root):
        path, _ = _version(root, skill_id, digest)
        for name in ("offline.json", "review.json"):
            _gate_file(path, name, digest)
        _live_check(path, digest, base)
        state = _state(root, skill_id)
        if digest in state["blocked"] or state["inflight"]:
            raise SkillError("skill_requires_review")
        if state["active"] != digest:
            state["previous"] = state["active"]
        state.update(active=digest, remaining_runs=runs)
        _record(root / skill_id / "active.json", state)
        return state


def status(store, skill_id, base=None):
    root = _store(store)
    if type(skill_id) is not str or not _ID.fullmatch(skill_id):
        raise SkillError("skill_identity_invalid")
    state = _state(root, skill_id)
    enabled = bool(state["active"] and state["remaining_runs"] and not state["inflight"]
                   and state["active"] not in state["blocked"])
    if enabled:
        try:
            path, _ = _version(root, skill_id, state["active"])
            for name in ("offline.json", "review.json"):
                _gate_file(path, name, state["active"])
            _live_check(path, state["active"], base or Path(__file__).resolve().parents[1] / "data" / "assistant_tasks")
        except (ValueError, OSError):
            enabled = False
    return dict(state, enabled=enabled)


def reconcile(store, skill_id, base):
    """Resolve a crashed publisher from durable evidence, without replaying effects."""
    if type(skill_id) is not str or not _ID.fullmatch(skill_id):
        raise SkillError("skill_identity_invalid")
    root = _store(store)
    with workflow._owner(root):
        state = _state(root, skill_id)
        if state["inflight"] is None:
            return state
        digest = state["active"]
        try:
            _verify_success(state["inflight"], base, digest)
        except (ValueError, OSError, TypeError, KeyError):
            if digest:
                state["blocked"] = list(dict.fromkeys([*state["blocked"], digest]))
            state.update(active=None, remaining_runs=0)
        state["inflight"] = None
        _record(root / skill_id / "active.json", state)
        return state


def rollback(store, skill_id, base):
    if type(skill_id) is not str or not _ID.fullmatch(skill_id):
        raise SkillError("skill_identity_invalid")
    root = _store(store)
    with workflow._owner(root):
        state = _state(root, skill_id)
        if state["inflight"]:
            raise SkillError("skill_inflight_unknown")
        previous = state["previous"]
        if previous is None or previous in state["blocked"]:
            raise SkillError("skill_no_verified_previous")
        path, _ = _version(root, skill_id, previous)
        for name in ("offline.json", "review.json"):
            _gate_file(path, name, previous)
        _live_check(path, previous, base)
        # Keep rejected/current candidates disabled; rollback does not undo files.
        if state["active"] is not None and state["active"] != previous:
            state["blocked"] = list(dict.fromkeys([*state["blocked"], state["active"]]))
        state.update(active=previous, previous=None, remaining_runs=10)
        _record(root / skill_id / "active.json", state)
        return state


def _deadline(value):
    value = time.monotonic() + 600 if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value):
        raise SkillError("skill_deadline_invalid")
    return value


def _selected(root, skill_id, base, trial_digest, expected_digest):
    if type(skill_id) is not str or not _ID.fullmatch(skill_id):
        raise SkillError("skill_identity_invalid")
    if expected_digest is not None and (type(expected_digest) is not str or not _HASH.fullmatch(expected_digest)):
        raise SkillError("skill_digest_invalid")
    state = _state(root, skill_id)
    mode = "trial" if trial_digest is not None else "active"
    digest = trial_digest if trial_digest is not None else state["active"]
    if expected_digest is not None and digest != expected_digest:
        raise SkillError("skill_selection_stale")
    if digest is None or digest in state["blocked"] or state["inflight"]:
        raise SkillError("skill_disabled_or_unknown")
    path, candidate = _version(root, skill_id, digest)
    for name in ("offline.json", "review.json"):
        _gate_file(path, name, digest)
    if mode == "active":
        if state["remaining_runs"] < 1:
            raise SkillError("skill_trial_limit_reached")
        _live_check(path, digest, base)
    return state, mode, digest, candidate


def _prepared_snapshot(task, supplied, base):
    """Re-read the persisted source, never trusting a caller's in-memory plan."""
    from . import office_source
    sources = task / "sources"
    record = sources / "source.json"
    if (_linked(sources) or _linked(record) or not sources.is_dir()
            or type(supplied) is not dict):
        raise SkillError("skill_source_invalid")
    snapshot = workflow._read_json(record)
    if snapshot != supplied or snapshot.get("source_id") != "selected":
        raise SkillError("skill_source_changed")
    files = [item for item in sources.iterdir() if item.name in
             {"source.csv", "source.xlsx", "source.docx"}]
    if (len(files) != 1 or _linked(files[0]) or not files[0].is_file()
            or not 0 < files[0].stat().st_size <= 20_000_000
            or files[0].stat().st_size != snapshot.get("bytes")
            or sha256(files[0].read_bytes()).hexdigest() != snapshot.get("sha256")):
        raise SkillError("skill_source_changed")
    source = files[0]
    provenance = sources / "provenance.json"
    if provenance.exists() or _linked(provenance):
        from . import artifact_source
        value = workflow._read_json(provenance)
        if (set(value) != {"kind", *artifact_source._FIELDS}
                or value.get("kind") != "verified_generated_office_artifact"):
            raise SkillError("skill_source_invalid")
        ref = {key: value.get(key) for key in artifact_source._FIELDS}
        artifact_source._reference(ref)
        current, original, receipt, index = artifact_source._verified(ref["artifact"], ref["sha256"], base)
        if current != ref:
            raise SkillError("skill_source_changed")
        kind, observed, coverage = artifact_source._extract(original, receipt, index, source)
    else:
        if source.suffix == ".csv":
            observed, coverage = office_source._csv(source)
            kind = "table"
        else:
            parts = office_source._package_parts(source)
            if source.suffix == ".xlsx":
                observed, coverage = office_source._workbook(source, parts)
                kind = "table"
            else:
                observed, coverage = office_source._document(parts)
                kind = "document"
    if (snapshot.get("kind") != kind or snapshot.get("observed") != observed
            or snapshot.get("coverage") != coverage
            or type(snapshot.get("display_name")) is not str
            or Path(snapshot["display_name"]).name != snapshot["display_name"]
            or not snapshot["display_name"].casefold().endswith(source.suffix)):
        raise SkillError("skill_source_changed")
    office_source._writer_compatible(snapshot["display_name"], kind, observed)
    return snapshot


def _error_code(exc):
    code = str(exc) if isinstance(exc, ValueError) else "skill_preflight_rejected"
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "skill_preflight_rejected"


def _publish_checked(task, result, publish):
    publish(result)
    target = task / "result.json"
    if _linked(target) or not target.is_file():
        raise SkillError("skill_result_missing")
    saved = workflow._read_json(target)
    if (saved.get("root") != str(task)
            or any(saved.get(key) != value for key, value in result.items())):
        raise SkillError("skill_result_mismatch")
    return saved


def _run_claimed(root, skill_id, state, mode, digest, candidate, task, base, deadline,
                 execute, publish, make_snapshot):
    cancel = task / "cancel.flag"
    implementation = implementation_sha256()
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)
    request = {"manifest": candidate, "sha256": digest, "implementation_sha256": implementation,
               "mode": mode, "executor": "supervised_office" if execute is None else "injected_test",
               "workflow_root": str(workflow_root)}
    workflow._write_new(task / "skill-request.json", request)
    try:
        snapshot = make_snapshot()
        steps = build_steps(candidate, snapshot)
        if cancel.exists() or time.monotonic() >= deadline:
            raise SkillError("skill_cancelled_before_effect")
    except (ValueError, TypeError, KeyError, OSError) as exc:
        # No workflow has been created: this is a provable input failure, not an
        # uncertain Office effect. Keep both request and result to block replay.
        if workflow_root.exists() or any(item.name.startswith("workflow-") for item in task.iterdir()):
            raise SkillError("skill_preflight_effect_unknown") from exc
        result = {"ok": False, "state": "failed", "kind": "skill", "phase": "preflight",
                  "effect_started": False, "root": str(task), "error": _error_code(exc),
                  "skill": {"id": skill_id, "sha256": digest,
                            "implementation_sha256": implementation, "mode": mode}}
        return _publish_checked(task, result, publish)
    if mode == "active":
        state.update(inflight=str(task), remaining_runs=state["remaining_runs"] - 1)
        _record(root / skill_id / "active.json", state)
    run = None
    error = None
    try:
        run = workflow.run_office_workflow(steps, workflow_root, execute=execute, deadline=deadline,
                                          cancel_requested=lambda: cancel.exists())
    except Exception:
        error = "skill_effect_unknown"
    receipts = {}
    try:
        if workflow_root.is_dir():
            receipts, unknown = workflow._resume_preflight(steps, workflow_root)
            if unknown:
                error = "skill_receipt_unknown"
    except (ValueError, TypeError, KeyError, OSError):
        error = "skill_receipt_unknown"
    outcome = run.get("state") if type(run) is dict else "unknown"
    if (error or outcome not in {"succeeded", "cancelled", "timed_out", "unknown"}
            or outcome == "succeeded" and len(receipts) != len(steps)):
        outcome = "unknown"
    artifacts = [{"step": key, "path": receipt["artifact"], "sha256": receipt["sha256"]}
                 for key, receipt in receipts.items()]
    result = {"ok": outcome == "succeeded", "state": outcome, "kind": "office", "root": str(task),
              "workflow_root": str(workflow_root), "completed": list(receipts), "artifacts": artifacts,
              "artifact": artifacts[-1]["path"] if artifacts else None,
              "readback_sha256": artifacts[-1]["sha256"] if artifacts else None,
              "skill": {"id": skill_id, "sha256": digest, "implementation_sha256": implementation, "mode": mode},
              "error": error or (run.get("error") if type(run) is dict else None)}
    published = _publish_checked(task, result, publish)
    if mode == "active":
        state["inflight"] = None
        if not result["ok"]:
            state["blocked"] = list(dict.fromkeys([*state["blocked"], digest]))
            state["active"] = None
            state["remaining_runs"] = 0
        _record(root / skill_id / "active.json", state)
        if (outcome == "cancelled" and execute is None and 0 < len(receipts) < len(steps)):
            # A permit is published only after the original parent result and
            # blocked candidate state are durable. Failure leaves cancellation
            # intact and simply makes this task ineligible for continuation.
            try:
                from .skill_resume import mint_permit
                mint_permit(task, base, root)
            except Exception:
                pass
    return published


def run_skill(store, skill_id, task_root, base, *, source_file=None,
              source_artifact=None, trial_digest=None, expected_digest=None,
              execute=None, deadline=None):
    if (source_file is None) == (source_artifact is None):
        raise SkillError("skill_requires_one_source")
    deadline = _deadline(deadline)
    root = _store(store)
    with workflow._owner(root):
        state, mode, digest, candidate = _selected(root, skill_id, base, trial_digest, expected_digest)
        task = _prepare_task(task_root, base)
        if any(item.name != "cancel.flag" for item in task.iterdir()):
            raise SkillError("skill_task_replay_blocked")
        cancel = task / "cancel.flag"
        if _linked(cancel) or cancel.exists() and not cancel.is_file():
            raise SkillError("skill_task_invalid")
        if cancel.exists() or time.monotonic() >= deadline:
            raise SkillError("skill_cancelled_before_start")
        with workflow._owner(task):
            def make_snapshot():
                if source_artifact is not None:
                    from .artifact_source import snapshot_artifact_source
                    return snapshot_artifact_source(source_artifact, task, base)
                from .artifact_source import require_external_source_file
                return snapshot_source(require_external_source_file(source_file, base), task)
            return _run_claimed(root, skill_id, state, mode, digest, candidate, task, base,
                                deadline, execute, lambda result: _publish_parent(task, result),
                                make_snapshot)


def run_prepared_skill(store, skill_id, task_root, base, *, snapshot, expected_digest,
                       publish, deadline=None):
    """Run a selected active recipe inside an assistant task already owning its source."""
    if not callable(publish):
        raise SkillError("skill_publisher_invalid")
    deadline = _deadline(deadline)
    root = _store(store)
    with workflow._owner(root):
        state, mode, digest, candidate = _selected(root, skill_id, base, None, expected_digest)
        task = _prepare_task(task_root, base)
        permitted = {"events.jsonl", "request.txt", "sources", "cancel.flag"}
        if any(item.name not in permitted for item in task.iterdir()):
            raise SkillError("skill_task_replay_blocked")
        events = task / "events.jsonl"
        if _linked(events) or not events.is_file() or _linked(task / "sources"):
            raise SkillError("skill_task_invalid")
        cancel = task / "cancel.flag"
        if _linked(cancel) or cancel.exists() and not cancel.is_file():
            raise SkillError("skill_task_invalid")
        if cancel.exists() or time.monotonic() >= deadline:
            raise SkillError("skill_cancelled_before_start")
        with workflow._owner(task):
            return _run_claimed(root, skill_id, state, mode, digest, candidate, task, base,
                                deadline, None, publish,
                                lambda: _prepared_snapshot(task, snapshot, base))
