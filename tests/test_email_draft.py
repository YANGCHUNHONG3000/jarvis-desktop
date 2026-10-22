"""Offline MIME draft contracts; no mailbox, Office app, or network access."""

from email.parser import BytesParser
from email.policy import SMTP
from hashlib import sha256
from pathlib import Path

import pytest

from agent_service import email_draft


def _roots(tmp_path):
    workflow = tmp_path / ("workflow-" + "a" * 32)
    workflow.mkdir()
    prior = workflow / "s1"
    prior.mkdir()
    current = workflow / "s2"
    current.mkdir()
    return prior, current


def _attachment(prior, content=b"verified-office-bytes", filename="document.docx"):
    path = prior / filename
    path.write_bytes(content)
    return {"step": "s1", "path": str(path), "sha256": sha256(content).hexdigest()}


def test_validate_draft_normalizes_only_text_and_explicit_recipients():
    value = email_draft.validate_draft({"subject": "  中文报告  ", "body": "第一行\r\n第二行",
                                        "to": ["User.Name@EXAMPLE.org"]})
    assert value == {"subject": "中文报告", "body": "第一行\n第二行",
                     "to": ["User.Name@example.org"]}
    assert email_draft.validate_draft({"subject": "Draft", "body": ""})["to"] == []


@pytest.mark.parametrize("data", [
    {"subject": "bad\r\nBcc: a@example.org", "body": "ok"},
    {"subject": "", "body": "ok"},
    {"subject": "x" * 201, "body": "ok"},
    {"subject": "good", "body": "bad\x00body"},
    {"subject": "good", "body": "x" * 20001},
    {"subject": "good", "body": "ok", "to": ["a@example.org\r\nBcc: b@example.org"]},
    {"subject": "good", "body": "ok", "to": ["A@example.org", "a@example.org"]},
    {"subject": "good", "body": "ok", "to": ["Name <a@example.org>"]},
    {"subject": "good", "body": "ok", "to": ["a@localhost"]},
    {"subject": "good", "body": "ok", "to": ["a@example.org"] * 11},
    {"subject": "good", "body": "ok", "from": "guess@example.org"},
])
def test_validate_draft_rejects_header_injection_and_unknown_fields(data):
    with pytest.raises(email_draft.DraftError):
        email_draft.validate_draft(data)


def test_writes_unsent_eml_and_independently_verifies_mime(tmp_path):
    prior, current = _roots(tmp_path)
    attachment = _attachment(prior)
    spec = {"kind": "email_draft", "subject": "资料汇总", "body": "已整理好。",
            "to": ["review@example.org"], "attachments": [attachment]}
    result = email_draft.execute_draft(spec, current)
    assert result["ok"] is True and result["state"] == "succeeded"
    assert result["file_verified"] is True and result["draft_verified"] is True
    assert result["observed"] == {"subject": "资料汇总", "body": "已整理好。",
                                  "to": ["review@example.org"],
                                  "attachments": [{"step": "s1", "filename": "s1.docx",
                                                   "sha256": attachment["sha256"]}]}
    data = Path(result["artifact"]).read_bytes()
    assert sha256(data).hexdigest() == result["sha256"]
    parsed = BytesParser(policy=SMTP).parsebytes(data)
    assert parsed["X-Unsent"] == "1" and "From" not in parsed
    assert parsed["To"] == "review@example.org"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "已整理好。"
    assert list(parsed.iter_attachments())[0].get_payload(decode=True) == b"verified-office-bytes"


def test_empty_recipient_draft_has_no_to_header(tmp_path):
    _, current = _roots(tmp_path)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "草稿", "body": "待补收件人"}, current)
    assert result["ok"] is True and result["observed"]["to"] == []
    parsed = BytesParser(policy=SMTP).parsebytes(Path(result["artifact"]).read_bytes())
    assert "To" not in parsed and "From" not in parsed


def test_body_trailing_newlines_are_preserved_in_verified_observation(tmp_path):
    _, current = _roots(tmp_path)
    body = "Line one\n\n"
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Notes", "body": body}, current)
    assert result["ok"] is True and result["observed"]["body"] == body
    assert email_draft._parsed_body(BytesParser(policy=SMTP).parsebytes(
        Path(result["artifact"]).read_bytes())) == body


@pytest.mark.parametrize("override", [
    {"step": "s2"},  # current/future step
    {"step": "s8"},
    {"sha256": "0" * 64},
    {"path": "C:/outside/document.docx"},
])
def test_attachment_reference_scope_or_hash_failure_is_prewrite(tmp_path, override):
    prior, current = _roots(tmp_path)
    attachment = _attachment(prior)
    attachment.update(override)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text",
                                        "attachments": [attachment]}, current)
    assert result["ok"] is False and result["state"] == "failed"
    assert not (current / "draft.eml").exists()


def test_cancel_before_effect_leaves_no_draft(tmp_path):
    _, current = _roots(tmp_path)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text"},
                                       current, should_stop=lambda: True)
    assert result["state"] == "cancelled" and not (current / "draft.eml").exists()


def test_cancel_after_write_is_unknown_and_preserves_file(tmp_path):
    _, current = _roots(tmp_path)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text"},
                                       current, should_stop=lambda: (current / "draft.eml").exists())
    assert result["ok"] is False and result["state"] == "unknown"
    assert (current / "draft.eml").is_file()


def test_existing_draft_path_is_conflict_and_never_replayed(tmp_path):
    _, current = _roots(tmp_path)
    path = current / "draft.eml"
    path.write_bytes(b"previous draft")
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text"}, current)
    assert result["ok"] is False and result["code"] == "draft_path_conflict"
    assert path.read_bytes() == b"previous draft"


def test_attachment_is_reverified_just_before_draft_write(tmp_path, monkeypatch):
    prior, current = _roots(tmp_path)
    attachment = _attachment(prior)
    original = email_draft._attachment
    calls = 0

    def mutate_after_first_read(spec, root, index):
        nonlocal calls
        calls += 1
        result = original(spec, root, index)
        if calls == 1:
            (prior / "document.docx").write_bytes(b"changed")
        return result

    monkeypatch.setattr(email_draft, "_attachment", mutate_after_first_read)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text",
                                        "attachments": [attachment]}, current)
    assert calls == 2 and result["ok"] is False
    assert not (current / "draft.eml").exists()


def test_total_attachment_limit_is_enforced_before_write(tmp_path, monkeypatch):
    workflow = tmp_path / ("workflow-" + "b" * 32)
    workflow.mkdir()
    first = workflow / "s1"
    second = workflow / "s2"
    current = workflow / "s3"
    for folder in (first, second, current):
        folder.mkdir()
    first_ref = _attachment(first, b"a" * 7)
    second_ref = _attachment(second, b"b" * 7, filename="spreadsheet.xlsx")
    second_ref["step"] = "s2"
    monkeypatch.setattr(email_draft, "_MAX_ATTACHMENT", 10)
    monkeypatch.setattr(email_draft, "_MAX_TOTAL", 12)
    result = email_draft.execute_draft({"kind": "email_draft", "subject": "Draft", "body": "Text",
                                        "attachments": [first_ref, second_ref]}, current)
    assert result["ok"] is False and result["code"] == "draft_attachment_size_invalid"
    assert not (current / "draft.eml").exists()
