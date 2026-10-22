"""Local GUI draft preview and artifact selection without opening mail apps."""

from email.message import EmailMessage
from email.policy import SMTP
from hashlib import sha256
from pathlib import Path

import pytest

from agent_service.email_draft import execute_draft
from run_jarvis_assistant import _office_artifacts, _read_draft_preview


def _draft(tmp_path):
    root = tmp_path / ("workflow-" + "a" * 32) / "s1"
    root.mkdir(parents=True)
    result = execute_draft({"kind": "email_draft", "subject": "中文主题",
                            "body": "第一行。\n第二行。", "to": []}, root)
    assert result["ok"] is True
    return Path(result["artifact"])


def test_preview_shows_chinese_subject_and_body_as_unsent(tmp_path):
    path = _draft(tmp_path)
    preview = _read_draft_preview(path)
    assert preview["subject"] == "中文主题"
    assert "第一行。" in preview["body"] and "第二行。" in preview["body"]
    assert preview["to"] == "未填写" and preview["attachments"] == []


@pytest.mark.parametrize("headers", [
    {"Subject": "Text"},  # no X-Unsent
    {"Subject": "Text", "X-Unsent": "0"},
    {"Subject": "Text", "X-Unsent": "1", "From": "sender@example.org"},
])
def test_preview_rejects_missing_unsent_marker_or_sent_like_header(tmp_path, headers):
    message = EmailMessage(policy=SMTP)
    for key, value in headers.items():
        message[key] = value
    message.set_content("Body")
    path = tmp_path / "bad.eml"
    path.write_bytes(message.as_bytes(policy=SMTP))
    with pytest.raises(ValueError, match="draft_invalid"):
        _read_draft_preview(path)


@pytest.mark.parametrize("content", [b"garbage", b"", b"X-Unsent: 1\r\n\r\n"])
def test_preview_rejects_malformed_or_empty_message(tmp_path, content):
    path = tmp_path / "bad.eml"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        _read_draft_preview(path)


def test_office_artifacts_accepts_verified_eml_above_office_limit(tmp_path):
    path = tmp_path / "workflow-1" / "s1" / "draft.eml"
    path.parent.mkdir(parents=True)
    with path.open("wb") as file:
        file.truncate(20_000_001)
    digest = sha256(path.read_bytes()).hexdigest()
    result = {"kind": "office", "ok": True, "state": "succeeded",
              "artifacts": [{"step": "s1", "path": str(path), "sha256": digest}]}
    assert _office_artifacts(result, tmp_path) == [("s1 · 未发送邮件草稿", path.resolve())]


def test_office_artifacts_rejects_eml_over_45mb(tmp_path):
    path = tmp_path / "workflow-1" / "s1" / "draft.eml"
    path.parent.mkdir(parents=True)
    with path.open("wb") as file:
        file.truncate(45_000_001)
    result = {"kind": "office", "artifacts": [
        {"step": "s1", "path": str(path), "sha256": "0" * 64}]}
    assert _office_artifacts(result, tmp_path) == []
