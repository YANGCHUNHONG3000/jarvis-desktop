"""Offline continuation evidence and fail-closed replay checks."""

from hashlib import sha256
import json
from pathlib import Path
import threading
import uuid

import pytest

from agent_service.email_draft import execute_draft
from agent_service.office_resume import (ResumeError, latest_continuation,
                                         preview_resume, read_resume_result,
                                         resume_task)
from agent_service.office_workflow import run_office_workflow
from agent_service.task_history import inspect_task
from run_jarvis_resume import main as resume_cli


def steps():
    return [
        {"id": "s1", "action": "spreadsheet", "data": {
            "kind": "spreadsheet", "title": "费用", "headers": ["项目", "金额"],
            "rows": [["纸", 3], ["笔", 4]], "sum_columns": [1]}},
        {"id": "s2", "action": "document_from_sheet", "from": "s1",
         "title": "汇总", "paragraphs": ["实际数据见下表。"]},
        {"id": "s3", "action": "email_draft",
         "data": {"subject": "汇总草稿", "body": "附件供查看。", "to": []},
         "attachments": ["s1", "s2"]},
    ]


def fake(spec, step_root, stop):
    assert not stop()
    if spec["kind"] == "email_draft":
        return execute_draft(spec, step_root, stop)
    suffix = ".xlsx" if spec["kind"] == "spreadsheet" else ".docx"
    path = step_root / ("spreadsheet.xlsx" if suffix == ".xlsx" else "document.docx")
    data = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    if spec["kind"] == "spreadsheet":
        observed = {"headers": spec["headers"], "rows": spec["rows"], "totals": {"1": 7}}
    else:
        observed = {"text": "\n".join([spec["title"], *spec["paragraphs"]]),
                    "table": {"headers": spec["table"]["headers"],
                              "rows": [[str(cell) for cell in row]
                                       for row in spec["table"]["rows"]]}}
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(data).hexdigest(), "observed": observed,
            "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
            "application_verified": True, "file_verified": True}


def fixture(tmp_path):
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True)
    root = base / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    workspace = root / ("workflow-" + uuid.uuid4().hex)

    def first(spec, step_root, stop):
        outcome = fake(spec, step_root, stop)
        (workspace / "cancel.flag").touch()
        return outcome

    run = run_office_workflow(steps(), workspace, execute=first)
    assert run["state"] == "cancelled" and run["completed"] == ["s1"]
    artifacts = [{"step": key, "path": receipt["artifact"], "sha256": receipt["sha256"]}
                 for key, receipt in run["receipts"].items()]
    parent = {"ok": False, "state": "cancelled", "kind": "office",
              "root": str(root), "workflow_root": str(workspace),
              "completed": ["s1"], "artifacts": artifacts}
    (root / "result.json").write_text(json.dumps(parent, ensure_ascii=False), encoding="utf-8")
    (root / "cancel.flag").touch()
    return root, base, workspace


def test_ordinary_readers_reject_skill_owned_parent_even_with_ordinary_pointer(tmp_path):
    root, base, _workspace = fixture(tmp_path)
    parent = json.loads((root / "result.json").read_text(encoding="utf-8"))
    parent["skill"] = {"id": "table-office-package", "sha256": "a" * 64,
                       "implementation_sha256": "b" * 64, "mode": "active"}
    (root / "result.json").write_text(json.dumps(parent), encoding="utf-8")
    (root / "resume-latest.json").write_text("{}", encoding="utf-8")
    attempt = uuid.uuid4().hex
    (root / ("resume-" + attempt)).mkdir()
    assert read_resume_result(root, base, attempt) == {
        "ok": False, "state": "unknown", "attempt_id": attempt}
    assert latest_continuation(root, base) is None
    assert preview_resume(root, base)["reason"] == "resume_skill_owned"


def test_continues_only_missing_steps_and_keeps_parent_and_old_cancel(tmp_path):
    root, base, workspace = fixture(tmp_path)
    parent_before = (root / "result.json").read_bytes()
    first_receipt = (workspace / "s1" / "receipt.json").read_bytes()
    preview = preview_resume(root, base)
    assert preview["eligible"] and (preview["completed"], preview["total"], preview["remaining"]) == (1, 3, 2)
    called = []

    def remaining(spec, step_root, stop):
        called.append(step_root.name)
        return fake(spec, step_root, stop)

    attempt = uuid.uuid4().hex
    outcome = resume_task(root, base, preview["token"], attempt, execute=remaining)
    assert outcome["ok"] and outcome["state"] == "succeeded", outcome
    assert called == ["s2", "s3"]
    assert (root / "result.json").read_bytes() == parent_before
    assert (workspace / "s1" / "receipt.json").read_bytes() == first_receipt
    assert (root / "cancel.flag").exists() and (workspace / "cancel.flag").exists()
    assert (root / f"resume-{attempt}" / "request.json").is_file()
    assert read_resume_result(root, base, attempt) == outcome
    overlay = inspect_task(root, base)
    assert overlay["state"] == "cancelled" and overlay["original_state"] == "cancelled"
    assert overlay["continuation"] == {"state": "succeeded", "attempt_id": attempt}
    assert len(overlay["artifacts"]) == 3


def test_later_step_directory_blocks_before_any_effect(tmp_path):
    root, base, workspace = fixture(tmp_path)
    (workspace / "s3").mkdir()
    preview = preview_resume(root, base)
    assert not preview["eligible"] and preview["reason"] == "later_step_conflict"
    with pytest.raises(ResumeError, match="resume_token_invalid"):
        resume_task(root, base, preview["token"], uuid.uuid4().hex, execute=lambda *_: pytest.fail("effect"))


def test_unknown_intent_and_mutated_artifact_block(tmp_path):
    root, base, workspace = fixture(tmp_path)
    (workspace / "s2").mkdir()
    (workspace / "s2" / "intent.json").write_text("{}", encoding="utf-8")
    assert preview_resume(root, base)["reason"] == "resume_unknown_effect"
    (workspace / "s2" / "intent.json").unlink()
    (workspace / "s2").rmdir()
    receipt = json.loads((workspace / "s1" / "receipt.json").read_text(encoding="utf-8"))
    Path(receipt["artifact"]).write_bytes(b"mutated")
    assert preview_resume(root, base)["reason"] == "artifact_hash_mismatch"


def test_stale_token_is_durable_block_without_new_step(tmp_path):
    root, base, workspace = fixture(tmp_path)
    token = preview_resume(root, base)["token"]
    path = root / "result.json"
    path.write_bytes(path.read_bytes() + b" ")
    attempt = uuid.uuid4().hex
    outcome = resume_task(root, base, token, attempt, execute=lambda *_: pytest.fail("effect"))
    assert outcome["state"] == "blocked" and outcome["error"] == "resume_preview_stale"
    assert not (workspace / "s2").exists()
    assert (root / f"resume-{attempt}" / "request.json").exists()
    assert read_resume_result(root, base, attempt)["state"] == "blocked"


def test_precreated_cancel_flag_stops_only_new_attempt(tmp_path):
    root, base, workspace = fixture(tmp_path)
    preview = preview_resume(root, base)
    attempt = uuid.uuid4().hex
    owned = root / ("resume-" + attempt)
    owned.mkdir()
    (owned / "cancel.flag").touch()
    outcome = resume_task(root, base, preview["token"], attempt,
                          execute=lambda *_: pytest.fail("effect"))
    assert outcome["state"] == "cancelled" and outcome["completed"] == 1
    assert not (workspace / "s2").exists()
    assert read_resume_result(root, base, attempt)["state"] == "cancelled"


def test_duplicate_attempt_and_unfinished_result_never_replay(tmp_path):
    root, base, workspace = fixture(tmp_path)
    preview = preview_resume(root, base)
    attempt = uuid.uuid4().hex
    owned = root / ("resume-" + attempt)
    owned.mkdir()
    (owned / "request.json").write_text("{}", encoding="utf-8")
    assert read_resume_result(root, base, attempt)["state"] == "unknown"
    with pytest.raises(ResumeError, match="resume_attempt_exists"):
        resume_task(root, base, preview["token"], attempt,
                    execute=lambda *_: pytest.fail("effect"))
    assert not (workspace / "s2").exists()


def test_concurrent_attempt_is_rejected_by_task_lock(tmp_path):
    root, base, workspace = fixture(tmp_path)
    preview = preview_resume(root, base)
    started = threading.Event()
    release = threading.Event()
    outcomes = []

    def waiting(spec, step_root, stop):
        started.set()
        assert release.wait(5)
        return fake(spec, step_root, stop)

    first_id = uuid.uuid4().hex
    thread = threading.Thread(target=lambda: outcomes.append(
        resume_task(root, base, preview["token"], first_id, execute=waiting)))
    thread.start()
    assert started.wait(5)
    with pytest.raises(ResumeError, match="workflow_busy"):
        resume_task(root, base, preview["token"], uuid.uuid4().hex,
                    execute=lambda *_: pytest.fail("concurrent effect"))
    release.set()
    thread.join(5)
    assert outcomes[0]["state"] == "succeeded", outcomes[0]


def test_tampered_latest_pointer_or_outcome_cannot_claim_success(tmp_path):
    root, base, _workspace = fixture(tmp_path)
    preview = preview_resume(root, base)
    attempt = uuid.uuid4().hex
    resume_task(root, base, preview["token"], attempt, execute=fake)
    pointer = root / "resume-latest.json"
    pointer.write_text('{"version":1}', encoding="utf-8")
    assert latest_continuation(root, base) is None
    assert "continuation" not in inspect_task(root, base)
    outcome = root / f"resume-{attempt}" / "outcome.json"
    data = json.loads(outcome.read_text(encoding="utf-8"))
    data["artifacts"][0]["sha256"] = "0" * 64
    outcome.write_text(json.dumps(data), encoding="utf-8")
    assert read_resume_result(root, base, attempt)["state"] == "unknown"


def test_claimed_success_requires_matching_owned_workflow_run(tmp_path):
    root, base, _workspace = fixture(tmp_path)
    token = preview_resume(root, base)["token"]
    attempt = uuid.uuid4().hex
    resume_task(root, base, token, attempt, execute=fake)
    (root / f"resume-{attempt}" / "result.json").unlink()
    assert read_resume_result(root, base, attempt)["state"] == "unknown"
    assert latest_continuation(root, base) is None
    assert "continuation" not in inspect_task(root, base)


def test_cli_emits_one_json_result_and_no_model(tmp_path, capsys):
    root, base, _workspace = fixture(tmp_path)
    preview = preview_resume(root, base)
    code = resume_cli(["--task-root", str(root), "--token", preview["token"],
                       "--attempt-id", uuid.uuid4().hex], execute=fake, repo_root=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert code == 0 and len(lines) == 1, lines
    assert json.loads(lines[0])["state"] == "succeeded"


def test_task_owner_lock_link_is_rejected_without_touching_external_file(tmp_path):
    root, base, workspace = fixture(tmp_path)
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"")
    try:
        (root / "owner.lock").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable on this host")
    token = preview_resume(root, base)["token"]
    with pytest.raises(ResumeError, match="workflow_lock_invalid"):
        resume_task(root, base, token, uuid.uuid4().hex,
                    execute=lambda *_: pytest.fail("effect"))
    assert outside.read_bytes() == b""
    assert not (workspace / "s2").exists()


def test_trusted_complete_continuation_disables_repeat_preview(tmp_path):
    root, base, _workspace = fixture(tmp_path)
    token = preview_resume(root, base)["token"]
    resume_task(root, base, token, uuid.uuid4().hex, execute=fake)
    preview = preview_resume(root, base)
    assert not preview["eligible"] and preview["reason"] == "resume_already_complete"
    assert preview["remaining"] == 0


def test_original_success_is_not_offered_for_continuation(tmp_path):
    root, base, workspace = fixture(tmp_path)
    (workspace / "cancel.flag").unlink()
    full = run_office_workflow(steps(), workspace, execute=fake, resume=True)
    assert full["state"] == "succeeded"
    parent = json.loads((root / "result.json").read_text(encoding="utf-8"))
    parent["ok"] = True
    parent["state"] = "succeeded"
    parent["completed"] = full["completed"]
    parent["artifacts"] = [
        {"step": key, "path": receipt["artifact"], "sha256": receipt["sha256"]}
        for key, receipt in full["receipts"].items()
    ]
    (root / "result.json").write_text(json.dumps(parent, ensure_ascii=False), encoding="utf-8")
    preview = preview_resume(root, base)
    assert not preview["eligible"] and preview["reason"] == "resume_already_complete"
    assert preview["remaining"] == 0


def test_unclaimed_middle_receipt_blocks_but_complete_chain_can_reconcile(tmp_path):
    root, base, workspace = fixture(tmp_path)
    (workspace / "cancel.flag").unlink()

    def stop_after_second(spec, step_root, stop):
        outcome = fake(spec, step_root, stop)
        (workspace / "cancel.flag").touch()
        return outcome

    partial = run_office_workflow(steps(), workspace, execute=stop_after_second,
                                  resume=True)
    assert partial["state"] == "cancelled" and partial["completed"] == ["s1", "s2"]
    preview = preview_resume(root, base)
    assert not preview["eligible"] and preview["reason"] == "resume_unclaimed_receipts"
    outcome = resume_task(root, base, preview["token"], uuid.uuid4().hex,
                          execute=lambda *_: pytest.fail("unclaimed effect"))
    assert outcome["state"] == "unknown" and not (workspace / "s3").exists()

    (workspace / "cancel.flag").unlink()
    full = run_office_workflow(steps(), workspace, execute=fake, resume=True)
    assert full["state"] == "succeeded"
    preview = preview_resume(root, base)
    assert preview["eligible"] and preview["remaining"] == 0
    attempt = uuid.uuid4().hex
    reconciled = resume_task(root, base, preview["token"], attempt,
                             execute=lambda *_: pytest.fail("no missing steps"))
    assert reconciled["state"] == "succeeded"
    assert read_resume_result(root, base, attempt)["state"] == "succeeded"
