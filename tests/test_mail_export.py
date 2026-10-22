"""Synthetic, offline evidence for explicit read-mail to local Word export."""

from hashlib import sha256
import json
from pathlib import Path
import time
import uuid

import pytest

from agent_service.mail_export import (MailExportError, run_mail_export,
                                       request_export_cancel, validate_snapshot,
                                       write_export_request)
from agent_service.task_history import inspect_task
from run_jarvis_mail_export import main as export_cli


def snapshot(**overrides):
    value = {
        "provider": "qq", "uid": "123", "uidvalidity": "456",
        "subject": "会议纪要", "from": "张三 <sender@qq.com>",
        "to": "李四 <reader@qq.com>", "date": "Fri, 17 Oct 2026 10:00:00 +0800",
        "body": "第一行\r\n第二\t列\n", "attachments": [
            {"filename": "安排.pdf", "content_type": "application/pdf", "size": 42}],
        "sha256": "a" * 64,
        "coverage": {"scope": "selected_full_message", "body_format": "text/plain",
                     "parts": 2, "attachments": "metadata_only",
                     "html_alternative_ignored": False},
    }
    value.update(overrides)
    return value


def task(tmp_path):
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True, exist_ok=True)
    root = base / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    return root, base


def fake(spec, step_root, stop):
    assert not stop()
    assert spec["kind"] == "document"
    path = step_root / "document.docx"
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(raw).hexdigest(),
            "observed": {"text": "\n".join([spec["title"], *spec["paragraphs"]])},
            "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
            "application_verified": True, "file_verified": True}


def test_synthetic_read_export_is_one_verified_document_and_history_compatible(tmp_path):
    root, base = task(tmp_path)
    digest = write_export_request(root, base, snapshot())
    request_before = (root / "mail-export-request.json").read_bytes()
    assert sha256(request_before).hexdigest() == digest
    observed_spec = []

    def capturing(spec, step_root, stop):
        observed_spec.append(spec)
        return fake(spec, step_root, stop)

    result = run_mail_export(root, base, digest, execute=capturing)
    assert result["ok"] and result["state"] == "succeeded"
    assert result["kind"] == "office" and result["completed"] == ["s1"]
    assert (root / "mail-export-request.json").read_bytes() == request_before
    assert result["mail_export"] == {"request_sha256": digest,
                                     "source_sha256": "a" * 64,
                                     "provenance": "local_read_mail_copy"}
    assert len(observed_spec) == 1 and observed_spec[0]["title"] == "邮件本地副本"
    paragraphs = observed_spec[0]["paragraphs"]
    assert "本地副本" in paragraphs[0]
    assert "UID" not in "\n".join(paragraphs)
    assert "SHA-256" not in "\n".join(paragraphs)
    assert "第二    列" in paragraphs and paragraphs.count("") == 1
    assert "附件内容未保存" in "\n".join(paragraphs)
    assert "安排.pdf" in "\n".join(paragraphs)
    assert "secret" not in request_before.decode("utf-8")
    inspected = inspect_task(root, base)
    assert inspected["state"] == "succeeded" and inspected["kind"] == "office"
    assert len(inspected["artifacts"]) == 1


@pytest.mark.parametrize("extra", [
    {"secret": "not-allowed"}, {"account": "reader@qq.com"},
    {"session": object()}, {"raw_mime": "opaque"},
])
def test_snapshot_rejects_unknown_fields(extra):
    with pytest.raises(MailExportError, match="mail_export_snapshot_invalid"):
        validate_snapshot(snapshot(**extra))


def test_snapshot_rejects_unbounded_or_incomplete_message_without_truncation():
    for changes in (
        {"body": "X" * 3001},
        {"body": "\n" * 40},
        {"body": "bad\x00text"},
        {"coverage": {**snapshot()["coverage"], "scope": "header_only"}},
        {"coverage": {**snapshot()["coverage"], "body_format": "none"}},
        {"provider": "gmail"},
    ):
        with pytest.raises(MailExportError):
            validate_snapshot(snapshot(**changes))


def test_html_extraction_and_attachment_metadata_are_explicit(tmp_path):
    root, base = task(tmp_path)
    value = snapshot(body="HTML 提取正文", coverage={**snapshot()["coverage"],
                                           "body_format": "html_to_plain"})
    digest = write_export_request(root, base, value)
    seen = []

    def capture(spec, step_root, stop):
        seen.append(spec)
        return fake(spec, step_root, stop)

    assert run_mail_export(root, base, digest, execute=capture)["state"] == "succeeded"
    text = "\n".join(seen[0]["paragraphs"])
    assert "从 HTML 提取的纯文本" in text
    assert "附件内容未保存" in text
    assert "HTML 提取正文" in text


def test_request_hash_and_replay_block_before_effect(tmp_path):
    root, base = task(tmp_path)
    digest = write_export_request(root, base, snapshot())
    with pytest.raises(MailExportError, match="mail_export_request_mismatch"):
        run_mail_export(root, base, "0" * 64, execute=lambda *_: pytest.fail("effect"))
    run_mail_export(root, base, digest, execute=fake)
    with pytest.raises(MailExportError, match="mail_export_replay_blocked"):
        run_mail_export(root, base, digest, execute=lambda *_: pytest.fail("replay"))


def test_cancel_and_unknown_effect_keep_durable_result_without_replay(tmp_path):
    root, base = task(tmp_path)
    (root / "cancel.flag").touch()
    digest = write_export_request(root, base, snapshot())
    result = run_mail_export(root, base, digest, execute=lambda *_: pytest.fail("effect"))
    assert result["state"] == "cancelled" and result["completed"] == []
    assert (root / "result.json").is_file()
    assert inspect_task(root, base)["state"] == "cancelled"

    second, base = task(tmp_path)
    digest = write_export_request(second, base, snapshot())
    result = run_mail_export(second, base, digest,
                             execute=lambda *_: (_ for _ in ()).throw(RuntimeError("private")))
    assert result["state"] == "unknown" and result["completed"] == []
    assert "private" not in (second / "result.json").read_text(encoding="utf-8")
    with pytest.raises(MailExportError, match="mail_export_replay_blocked"):
        run_mail_export(second, base, digest, execute=lambda *_: pytest.fail("replay"))


def test_scope_links_and_existing_task_contents_rejected(tmp_path):
    root, base = task(tmp_path)
    (root / "surprise.txt").write_text("x", encoding="utf-8")
    with pytest.raises(MailExportError, match="mail_export_task_not_empty"):
        write_export_request(root, base, snapshot())
    (root / "surprise.txt").unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "mail-export-request.json").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable on this host")
    with pytest.raises(MailExportError):
        write_export_request(root, base, snapshot())
    assert outside.read_text(encoding="utf-8") == "outside"


def test_linked_base_or_task_ancestor_cannot_write_outside_scope(tmp_path):
    root, base = task(tmp_path)
    alias = tmp_path / "linked-tasks"
    try:
        alias.symlink_to(base, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable on this host")
    linked_task = alias / root.name
    for task_path, task_base in ((root, alias), (linked_task, base)):
        with pytest.raises(MailExportError, match="mail_export_task_scope_invalid"):
            write_export_request(task_path, task_base, snapshot())
    assert not (root / "mail-export-request.json").exists()


def test_request_helper_creates_owned_task_without_gui_mkdir(tmp_path):
    base = tmp_path / "data" / "assistant_tasks"
    root = base / ("task-" + uuid.uuid4().hex)
    assert not base.exists()
    digest = write_export_request(root, base, snapshot())
    assert root.is_dir()
    assert sha256((root / "mail-export-request.json").read_bytes()).hexdigest() == digest


def test_linked_missing_base_ancestor_is_rejected_before_any_write(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    try:
        (repo / "data").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable on this host")
    base = repo / "data" / "assistant_tasks"
    root = base / ("task-" + uuid.uuid4().hex)
    with pytest.raises(MailExportError, match="mail_export_task_scope_invalid"):
        write_export_request(root, base, snapshot())
    assert not (outside / "assistant_tasks").exists()


def test_cancel_helper_rechecks_task_links_and_never_touches_outside(tmp_path):
    root, base = task(tmp_path)
    assert request_export_cancel(root, base)
    assert (root / "cancel.flag").is_file()
    assert request_export_cancel(root, base)
    assert not request_export_cancel(base / ("task-" + uuid.uuid4().hex), base)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_task = base / ("task-" + uuid.uuid4().hex)
    alias = tmp_path / "linked-base"
    try:
        linked_task.symlink_to(outside, target_is_directory=True)
        alias.symlink_to(base, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable on this host")
    assert not request_export_cancel(linked_task, base)
    assert not request_export_cancel(alias / root.name, alias)
    assert not (outside / "cancel.flag").exists()


def test_cli_emits_one_json_and_uses_no_model_or_mailbox(tmp_path, capsys):
    root, base = task(tmp_path)
    digest = write_export_request(root, base, snapshot())
    code = export_cli(["--task-root", str(root), "--request-sha256", digest],
                      execute=fake, repo_root=tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert code == 0 and len(lines) == 1
    assert json.loads(lines[0])["state"] == "succeeded"
    assert not any("secret" in text for text in lines)


def test_expired_deadline_stops_before_native_effect(tmp_path):
    root, base = task(tmp_path)
    digest = write_export_request(root, base, snapshot())
    result = run_mail_export(root, base, digest, deadline=time.monotonic() - 1,
                             execute=lambda *_: pytest.fail("effect"))
    assert result["state"] == "timed_out" and not result["artifacts"]
