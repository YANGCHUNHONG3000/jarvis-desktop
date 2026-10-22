"""Read-only task history from durable results and verified Office receipts."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service.office_workflow import run_office_workflow
from agent_service import office_workflow
from agent_service.task_history import inspect_task, list_tasks


def _task(tmp_path):
    root = tmp_path / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    return root


def _steps():
    return [{"id": "s1", "action": "document", "data": {
        "kind": "document", "title": "Report", "paragraphs": ["Saved text"]}},
        {"id": "s2", "action": "spreadsheet", "data": {
            "kind": "spreadsheet", "title": "Data", "headers": ["Count"],
            "rows": [[3]], "sum_columns": [0]}}]


def _fake_office(spec, step_root, should_stop):
    is_doc = spec["kind"] == "document"
    path = step_root / ("document.docx" if is_doc else "spreadsheet.xlsx")
    data = ("verified " + spec["kind"]).encode()
    path.write_bytes(data)
    observed = ({"text": "Report\nSaved text"} if is_doc else
                {"headers": ["Count"], "rows": [[3]], "totals": {"0": 3}})
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(data).hexdigest(), "observed": observed,
            "app": {"progid": "fake", "name": "Fake Office", "version": "1"},
            "application_verified": True, "file_verified": True}


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_parent_killed_after_first_receipt_recovers_only_verified_output_without_writes(tmp_path):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)

    def interrupted(spec, step_root, should_stop):
        if spec["kind"] == "spreadsheet":
            (step_root / "spreadsheet.xlsx").write_bytes(b"unverified")
            raise TimeoutError("parent died while native action was pending")
        return _fake_office(spec, step_root, should_stop)

    outcome = run_office_workflow(_steps(), workflow_root, execute=interrupted)
    assert outcome["state"] == "unknown" and outcome["completed"] == ["s1"]
    assert not (task / "result.json").exists()
    before = _files(task)
    recovered = inspect_task(task, tmp_path)
    assert recovered["state"] == "needs_review" and recovered["kind"] == "office"
    assert recovered["workflow_root"] == str(workflow_root)
    assert [(item["label"], Path(item["path"]).suffix)
            for item in recovered["artifacts"]] == [("s1 · 文档 (.docx)", ".docx")]
    assert _files(task) == before
    assert (workflow_root / "s2" / "intent.json").exists()
    assert not (workflow_root / "s2" / "receipt.json").exists()


def test_missing_parent_result_never_claims_task_success_even_if_all_steps_done(tmp_path):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)
    run_office_workflow(_steps(), workflow_root, execute=_fake_office)
    recovered = inspect_task(task, tmp_path)
    assert recovered["state"] == "recovered" and len(recovered["artifacts"]) == 2
    assert recovered["state"] != "succeeded"


def test_tampered_artifact_is_not_exposed_from_receipt_recovery(tmp_path):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)
    outcome = run_office_workflow(_steps(), workflow_root, execute=_fake_office)
    Path(outcome["receipts"]["s1"]["artifact"]).write_bytes(b"changed")
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "needs_review" and inspected["artifacts"] == []


def test_durable_cancelled_result_preserves_partial_state_and_verified_file(tmp_path):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)

    def cancel_after_first(spec, step_root, should_stop):
        value = _fake_office(spec, step_root, should_stop)
        (workflow_root / "cancel.flag").touch()
        return value

    workflow_result = run_office_workflow(_steps(), workflow_root, execute=cancel_after_first)
    assert workflow_result["state"] == "cancelled"
    first = workflow_result["receipts"]["s1"]
    result = {"ok": False, "state": "cancelled", "kind": "office", "root": str(task),
              "workflow_root": str(workflow_root), "completed": ["s1"],
              "artifacts": [{"step": "s1", "path": first["artifact"], "sha256": first["sha256"]}]}
    (task / "result.json").write_text(json.dumps(result), encoding="utf-8")
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "cancelled" and len(inspected["artifacts"]) == 1


def test_skill_history_uses_only_skill_continuation_for_new_outputs(tmp_path, monkeypatch):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)

    def stop_after_first(spec, step_root, should_stop):
        value = _fake_office(spec, step_root, should_stop)
        (workflow_root / "cancel.flag").touch()
        return value

    partial = run_office_workflow(_steps(), workflow_root, execute=stop_after_first)
    first = partial["receipts"]["s1"]
    (task / "result.json").write_text(json.dumps({
        "ok": False, "state": "cancelled", "kind": "office", "root": str(task),
        "workflow_root": str(workflow_root), "completed": ["s1"],
        "artifacts": [{"step": "s1", "path": first["artifact"], "sha256": first["sha256"]}],
        "skill": {"id": "table-office-package", "sha256": "a" * 64,
                  "implementation_sha256": "b" * 64, "mode": "active"}}), encoding="utf-8")
    (workflow_root / "cancel.flag").unlink()
    complete = run_office_workflow(_steps(), workflow_root, execute=_fake_office, resume=True)
    second = complete["receipts"]["s2"]
    (task / "resume-latest.json").write_text("{}", encoding="utf-8")
    from agent_service import skill_resume
    monkeypatch.setattr(skill_resume, "preview_resume", lambda *_args: {
        "kind": "skill_resume", "eligible": False, "reason": "skill_resume_already_attempted"})
    monkeypatch.setattr(skill_resume, "latest_continuation", lambda *_args: None)
    inspected = inspect_task(task, tmp_path)
    assert [item["path"] for item in inspected["artifacts"]] == [first["artifact"]]
    assert inspected["resume"]["kind"] == "skill_resume"
    monkeypatch.setattr(skill_resume, "latest_continuation", lambda *_args: {
        "kind": "skill_resume", "state": "succeeded", "attempt_id": "c" * 32,
        "artifacts": [{"step": "s1", "path": first["artifact"], "sha256": first["sha256"]},
                      {"step": "s2", "path": second["artifact"], "sha256": second["sha256"]}]})
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "cancelled"
    assert inspected["continuation"] == {"kind": "skill_resume", "state": "succeeded",
                                          "attempt_id": "c" * 32}
    assert [item["path"] for item in inspected["artifacts"]] == [
        first["artifact"], second["artifact"]]


def test_listing_reads_small_metadata_only_and_limits_recent_tasks(tmp_path, monkeypatch):
    older = _task(tmp_path)
    newer = _task(tmp_path)
    report = older / "huge.docx"
    report.write_bytes(b"do not hash in listing")
    for root, state in ((older, "cancelled"), (newer, "succeeded")):
        (root / "result.json").write_text(json.dumps({"root": str(root), "state": state,
                                                         "kind": "office"}), encoding="utf-8")
    import os
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    original = Path.read_bytes

    def block_large_read(path):
        if path == report:
            pytest.fail("listing hashed an artifact")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", block_large_read)
    listed = list_tasks(tmp_path, limit=1)
    assert len(listed) == 1 and listed[0]["id"] == newer.name
    assert listed[0]["state"] == "succeeded" and listed[0]["kind"] == "office"


def test_unknown_or_truncated_result_does_not_block_receipt_recovery(tmp_path):
    task = _task(tmp_path)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)
    run_office_workflow([_steps()[0]], workflow_root, execute=_fake_office)
    (task / "result.json").write_bytes(b'{"ok":')
    assert list_tasks(tmp_path)[0]["state"] == "needs_review"
    recovered = inspect_task(task, tmp_path)
    assert recovered["state"] == "recovered" and len(recovered["artifacts"]) == 1


def test_recovery_requires_exactly_one_owned_workflow(tmp_path):
    task = _task(tmp_path)
    for _ in range(2):
        workflow_root = task / ("workflow-" + uuid.uuid4().hex)
        run_office_workflow([_steps()[0]], workflow_root, execute=_fake_office)
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "needs_review" and inspected["artifacts"] == []


def test_durable_research_report_is_exposed_only_while_hash_matches(tmp_path):
    task = _task(tmp_path)
    reports = task / "artifacts"
    reports.mkdir()
    report = reports / ("report-" + uuid.uuid4().hex + ".md")
    report.write_bytes(b"# Verified evidence\n")
    result = {"ok": True, "state": "succeeded", "kind": "research", "root": str(task),
              "artifact": str(report), "readback_sha256": sha256(report.read_bytes()).hexdigest()}
    (task / "result.json").write_text(json.dumps(result), encoding="utf-8")
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "succeeded" and inspected["artifacts"] == [
        {"label": "资料归纳与来源 (.md)", "path": str(report),
         "sha256": result["readback_sha256"]}]
    report.write_bytes(b"changed")
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "needs_review" and inspected["artifacts"] == []


def test_task_link_is_excluded_from_listing_and_inspection(tmp_path):
    real_base = tmp_path / "real"
    real_base.mkdir()
    target = _task(real_base)
    link = tmp_path / target.name
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    assert list_tasks(tmp_path) == []
    with pytest.raises(ValueError, match="task_root_invalid"):
        inspect_task(link, tmp_path)


def test_ancestor_link_is_rejected_before_base_or_task_resolution(tmp_path):
    real = tmp_path / "real"
    base = real / "tasks"
    base.mkdir(parents=True)
    task = _task(base)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(ValueError, match="task_base_invalid"):
        list_tasks(alias / "tasks")
    with pytest.raises(ValueError, match="task_base_invalid"):
        inspect_task(alias / "tasks" / task.name, alias / "tasks")
    with pytest.raises(ValueError, match="task_root_invalid"):
        inspect_task(alias / "tasks" / task.name, base)


def test_unsupported_gap_is_shown_only_when_exact_owned_file_hash_matches(tmp_path):
    task = _task(tmp_path)
    gap = {"schema": 1, "instruction_sha256": "b" * 64,
           "model_reason": "暂不支持", "lookup": {"matched_ids": []},
           "discovery": {"schema": 1, "candidates": [{
               "id": "table-office-copy", "sha256": "a" * 64,
               "implementation_sha256": "c" * 64, "version": 1,
               "name": "表格原样复制", "recipe": "table_copy", "remaining_runs": 3,
               "status": "available", "reason": None, "source_kind": "table",
               "effects": []}], "hints": []},
           "development_request": {"state": "queued", "automatic_code_execution": False}}
    path = task / "capability-gap.json"
    path.write_text(json.dumps(gap, ensure_ascii=False), encoding="utf-8")
    reference = {"filename": path.name, "sha256": sha256(path.read_bytes()).hexdigest()}
    (task / "result.json").write_text(json.dumps({
        "root": str(task), "ok": False, "state": "unsupported", "kind": "unsupported",
        "capability_gap": reference}), encoding="utf-8")
    original = (task / "result.json").read_bytes()
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "unsupported"
    assert inspected["capability_gap"]["candidates"][0]["id"] == "table-office-copy"
    assert inspected["capability_gap"]["development_queued"] is True
    assert (task / "result.json").read_bytes() == original
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    changed = inspect_task(task, tmp_path)
    assert changed["state"] == "needs_review"
    assert "capability_gap" not in changed


def test_skill_preflight_failure_requires_matching_request_and_no_effect_dir(tmp_path):
    task = _task(tmp_path)
    manifest = {"schema": 1, "id": "document-table-office-package", "version": 1,
                "name": "Word表格转Excel与报告", "recipe": "document_table_report",
                "sum_columns": [], "draft": False}
    digest = office_workflow._hash(manifest)
    workflow_root = task / ("workflow-" + uuid.uuid4().hex)
    request = {"manifest": manifest, "sha256": digest,
               "implementation_sha256": "b" * 64, "mode": "active",
               "executor": "supervised_office", "workflow_root": str(workflow_root)}
    (task / "skill-request.json").write_text(json.dumps(request), encoding="utf-8")
    result = {"root": str(task), "ok": False, "state": "failed", "kind": "skill",
              "phase": "preflight", "effect_started": False,
              "error": "skill_document_table_required",
              "skill": {"id": manifest["id"], "sha256": digest,
                        "implementation_sha256": "b" * 64, "mode": "active"}}
    (task / "result.json").write_text(json.dumps(result), encoding="utf-8")
    inspected = inspect_task(task, tmp_path)
    assert inspected["state"] == "failed"
    assert inspected["input_failure"] == {"code": "skill_document_table_required",
                                           "skill_id": manifest["id"], "mode": "active"}
    (task / "workflow-unexpected").mkdir()
    changed = inspect_task(task, tmp_path)
    assert changed["state"] == "needs_review" and "input_failure" not in changed
