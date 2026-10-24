"""Offline email workflow checks; no Office application or network is used."""

from email.parser import BytesParser
from email.policy import SMTP
from hashlib import sha256
from pathlib import Path
import uuid

import pytest

from agent_service.email_draft import execute_draft
from agent_service.office_workflow import WorkflowError, run_office_workflow, validate_steps


def _root(tmp_path):
    return tmp_path / ("workflow-" + uuid.uuid4().hex)


def _steps(attachments=None):
    return [
        {"id": "s1", "action": "spreadsheet", "data": {"kind": "spreadsheet",
         "title": "Budget", "headers": ["Item", "Cost"], "rows": [["Paper", 3]],
         "sum_columns": [1]}},
        {"id": "s2", "action": "document", "data": {"kind": "document",
         "title": "Summary", "paragraphs": ["See the budget."]}},
        {"id": "s3", "action": "email_draft", "data": {"subject": "Budget review",
         "body": "Please review the attached files.", "to": ["review@example.org"]},
         "attachments": ["s1", "s2"] if attachments is None else attachments},
    ]


def _office_result(spec, step_root):
    is_sheet = spec["kind"] == "spreadsheet"
    artifact = step_root / ("spreadsheet.xlsx" if is_sheet else "document.docx")
    data = ("owned " + spec["kind"]).encode()
    artifact.write_bytes(data)
    observed = ({"headers": spec["headers"], "rows": spec["rows"], "totals": {"1": 3}}
                if is_sheet else {"text": "Summary\nSee the budget."})
    return {"ok": True, "state": "succeeded", "artifact": str(artifact),
            "sha256": sha256(data).hexdigest(), "observed": observed,
            "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
            "application_verified": True, "file_verified": True}


def _execute(calls):
    def execute(spec, step_root, should_stop):
        calls.append(spec)
        if spec["kind"] == "email_draft":
            return execute_draft(spec, step_root, should_stop)
        return _office_result(spec, step_root)
    return execute


def test_draft_attaches_only_verified_prior_files_and_resumes_without_replay(tmp_path):
    root = _root(tmp_path)
    calls = []
    steps = _steps()
    result = run_office_workflow(steps, root, execute=_execute(calls))
    assert result["state"] == "succeeded" and len(calls) == 3
    draft_spec = calls[-1]
    assert draft_spec["kind"] == "email_draft"
    assert draft_spec["attachments"] == [
        {"step": step, "path": result["receipts"][step]["artifact"],
         "sha256": result["receipts"][step]["sha256"]} for step in ("s1", "s2")]
    receipt = result["receipts"]["s3"]
    assert receipt["file_verified"] is True and receipt["draft_verified"] is True
    assert "app" not in receipt and "application_verified" not in receipt
    artifact = Path(receipt["artifact"])
    message = BytesParser(policy=SMTP).parsebytes(artifact.read_bytes())
    assert message["X-Unsent"] == "1" and message["To"] == "review@example.org"
    assert [part.get_filename() for part in message.iter_attachments()] == ["s1.xlsx", "s2.docx"]
    assert [sha256(part.get_payload(decode=True)).hexdigest()
            for part in message.iter_attachments()] == [result["receipts"][step]["sha256"]
                                                      for step in ("s1", "s2")]
    resumed = run_office_workflow(steps, root, execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert resumed["state"] == "succeeded" and resumed["receipts"] == result["receipts"]


def test_default_dispatch_uses_office_host_for_office_and_local_draft_for_email(tmp_path, monkeypatch):
    from agent_service import office_host, office_readiness
    calls = []

    def fake_host(spec, step_root, should_stop):
        calls.append(spec["kind"])
        return _office_result(spec, step_root)

    monkeypatch.setattr(office_host, "execute_office_supervised", fake_host)
    monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: None)
    result = run_office_workflow(_steps([]), _root(tmp_path))
    assert result["state"] == "succeeded" and calls == ["spreadsheet", "document"]
    assert Path(result["receipts"]["s3"]["artifact"]).name == "draft.eml"


@pytest.mark.parametrize("attachments", [["s3"], ["s1", "s1"], ["s2", "s4"],
                                          ["s1", "s2", "s1", "s2", "s1"]])
def test_invalid_attachment_refs_rejected_before_effect(tmp_path, attachments):
    root = _root(tmp_path)
    with pytest.raises(WorkflowError, match="step_reference_invalid"):
        run_office_workflow(_steps(attachments), root,
                            execute=lambda *_: pytest.fail("executed"))
    assert not root.exists()


def test_email_ref_cannot_be_used_as_an_attachment(tmp_path):
    steps = _steps([])
    steps.append({"id": "s4", "action": "email_draft",
                  "data": {"subject": "Forward", "body": "", "to": []},
                  "attachments": ["s3"]})
    with pytest.raises(WorkflowError, match="step_reference_invalid"):
        validate_steps(steps)


def test_changed_attachment_blocks_draft_before_write(tmp_path):
    root = _root(tmp_path)
    calls = []
    steps = _steps()

    def change_after_office(spec, step_root, should_stop):
        value = _execute(calls)(spec, step_root, should_stop)
        if step_root.name == "s2":
            (root / "s1" / "spreadsheet.xlsx").write_bytes(b"changed")
        return value

    with pytest.raises(WorkflowError, match="artifact_hash_mismatch"):
        run_office_workflow(steps, root, execute=change_after_office)
    assert not (root / "s3").exists()


def test_unverified_draft_keeps_prior_receipts_and_never_replays_intent(tmp_path):
    root = _root(tmp_path)
    calls = []
    steps = _steps()

    def unverified(spec, step_root, should_stop):
        if spec["kind"] == "email_draft":
            calls.append(spec)
            artifact = step_root / "draft.eml"
            artifact.write_bytes(b"Subject: unsure\r\n\r\n")
            return {"ok": True, "state": "succeeded", "artifact": str(artifact),
                    "sha256": sha256(artifact.read_bytes()).hexdigest(),
                    "file_verified": True, "draft_verified": False, "observed": {}}
        return _execute(calls)(spec, step_root, should_stop)

    first = run_office_workflow(steps, root, execute=unverified)
    assert first["state"] == "unknown" and first["step"] == "s3"
    assert list(first["receipts"]) == ["s1", "s2"]
    assert not (root / "s3" / "receipt.json").exists()
    second = run_office_workflow(steps, root, execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert second["state"] == "unknown" and second["step"] == "s3"


def test_draft_receipt_rejects_application_claim_on_resume(tmp_path):
    root = _root(tmp_path)
    steps = _steps([])
    result = run_office_workflow(steps, root, execute=_execute([]))
    receipt_path = root / "s3" / "receipt.json"
    import json
    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["application_verified"] = True
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(WorkflowError, match="effect_unverified"):
        run_office_workflow(steps, root, execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert result["receipts"]["s3"]["draft_verified"] is True
