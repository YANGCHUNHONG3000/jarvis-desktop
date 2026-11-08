"""Offline contracts for selected-mail understanding and its native Office plan."""

from copy import deepcopy
from hashlib import sha256
from html import escape
import json
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from agent_service import artifact_source, mail_export, mail_understanding as mail, office


BODY = "小王周五提交报价。请把最终版本交给小李。"


def _snapshot(body=BODY):
    return {
        "provider": "qq", "uid": "123", "uidvalidity": "456",
        "subject": "采购事项", "from": "Sender <sender@example.org>",
        "to": "Reader <reader@example.org>", "date": "Fri, 17 Oct 2026 10:00:00 +0800",
        "body": body, "attachments": [], "sha256": "a" * 64,
        "coverage": {"scope": "selected_full_message", "body_format": "text/plain",
                     "parts": 1, "attachments": "metadata_only",
                     "html_alternative_ignored": False},
    }


def _office_execute(spec, step_root, stop):
    assert stop() is False
    path = step_root / "document.docx"
    paragraphs = [spec["title"], *spec["paragraphs"]]
    body = "".join(
        "<w:p><w:r><w:t xml:space=\"preserve\">" + escape(text) + "</w:t></w:r></w:p>"
        for text in paragraphs)
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
           "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
           "<w:body>" + body + "<w:sectPr/></w:body></w:document>")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))
    raw = path.read_bytes()
    observed = office._public_observation(spec, office._expected(spec))
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(raw).hexdigest(),
            "observed": observed,
            "app": {"progid": "offline-test", "name": "offline test", "version": "1"},
            "application_verified": True, "file_verified": True}


def _mail_source(tmp_path, body=BODY):
    base = tmp_path / "mail-tasks"
    base.mkdir(parents=True, exist_ok=True)
    root = base / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    snapshot = _snapshot(body)
    digest = mail_export.write_export_request(root, base, snapshot)
    result = mail_export.run_mail_export(root, base, digest, execute=_office_execute)
    assert result["ok"] is True
    ref = artifact_source.prepare_artifact_source(result["artifact"], result["readback_sha256"], base)
    return snapshot, root, base, result, ref


def _context(body=BODY):
    return {"subject": "采购事项",
            "body_lines": {f"B{i}": line for i, line in enumerate(body.split("\n"), 1)},
            "coverage": _snapshot(body)["coverage"], "attachment_count": 0}


def _ready(*, body=BODY, formats=None, task="提交报价", owner="小王", deadline="周五", schema=2):
    context = _context(body)
    quote = context["body_lines"]["B1"]
    return {"state": "ready", "reason": "依据邮件正文整理", "title": "采购事项摘要",
            "scope": {"kind": "full", "description": "邮件全部事项",
                      "lines": 'all' if schema == 4 else list(context['body_lines'])},
            "formats": formats or ["spreadsheet", "document"],
            "summary": [{"text": "报价事项及交付安排", "evidence": [{"line": "B1", "quote": quote}]}],
            "items": [{"kind": "待办", "task": task, "owner": owner, "deadline": deadline,
                       "evidence": [{"line": "B1", "quote": quote}]}]}


class FakeClient:
    def __init__(self, *answers, on_call=None):
        self.answers = list(answers)
        self.calls = []
        self.on_call = on_call
        self.ledger = type("Ledger", (), {"snapshot": lambda _self: {}})()

    def complete(self, *, system, user, max_tokens):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.on_call:
            self.on_call(len(self.calls), self.calls[-1])
        return self.answers.pop(0)


def test_validate_requires_exact_cited_body_lines_and_verbatim_owner_date():
    context = _context()
    value = _ready()
    assert mail.validate(value, context) == value

    wrong_line = deepcopy(value)
    wrong_line["items"][0]["evidence"][0]["line"] = "B9"
    with pytest.raises(mail.MailUnderstandingError, match="mail_analysis_evidence_invalid"):
        mail.validate(wrong_line, context)

    guessed_owner = deepcopy(value)
    guessed_owner["items"][0]["owner"] = "小赵"
    with pytest.raises(mail.MailUnderstandingError, match="mail_analysis_field_unquoted"):
        mail.validate(guessed_owner, context)

    calculated_deadline = deepcopy(value)
    calculated_deadline["items"][0]["deadline"] = "2026年10月17日"
    with pytest.raises(mail.MailUnderstandingError, match="mail_analysis_field_unquoted"):
        mail.validate(calculated_deadline, context)


def test_schema_accepts_exact_quote_but_semantic_review_rejects_negated_action():
    body = "小王周五不要提交报价。"
    context = _context(body)
    candidate = _ready(body=body, task="提交报价", owner="小王", deadline="周五")
    assert mail.validate(candidate, context) == candidate  # Schema proves citation only.

def test_compile_steps_preserves_formats_citations_and_never_adds_formulas():
    value = mail.validate(_ready(), _context())
    steps = mail.compile_steps(value)

    assert [step["action"] for step in steps] == ["spreadsheet", "document"]
    assert [step["id"] for step in steps] == ["s1", "s2"]
    sheet = steps[0]["data"]
    assert sheet["title"] == value["title"]
    assert sheet["sum_columns"] == []
    assert sheet["headers"] == ["类型", "事项", "负责人（原文）", "期限（原文）", "依据位置", "原文依据"]
    assert sheet["rows"][0][4:] == ["覆盖声明", "仅生成本地新文件，未发送邮件、未修改原件。"]
    assert sheet["rows"][1][4:] == ["B1", BODY]
    document = steps[1]["data"]
    assert document["table"]["rows"] == sheet["rows"]
    assert "附件仅有元数据" in document["paragraphs"][2]


def test_load_mail_binds_ref_to_export_request_and_exact_office_plan(tmp_path):
    snapshot, source_root, base, _result, ref = _mail_source(tmp_path)

    assert mail.load_mail(ref, base) == snapshot
    changed_ref = dict(ref, sha256="b" * 64)
    with pytest.raises((mail.MailUnderstandingError, artifact_source.ArtifactSourceError)):
        mail.load_mail(changed_ref, base)

    request_path = source_root / "mail-export-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["snapshot"]["body"] = "篡改的正文"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((mail.MailUnderstandingError, mail_export.MailExportError)):
        mail.load_mail(ref, base)


def test_semantic_review_rejection_never_dispatches_office(tmp_path, monkeypatch):
    body = "小王周五不要提交报价。"
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path, body)
    candidate = _ready(body=body, task="提交报价", owner="小王", deadline="周五", schema=4)
    client = FakeClient(candidate, {"approved": False, "reason": "原文是否定表达"})
    dispatched = []
    monkeypatch.setattr(mail.workflow, "run_office_workflow",
                        lambda *args, **kwargs: dispatched.append((args, kwargs)))

    result = mail.run_task("提取需要完成的事项", base,
                           task_root=base / ("task-" + "f" * 32),
                           source_artifact=ref, client=client)

    assert [call["system"] for call in client.calls] == [mail.SYSTEM, mail.REVIEW]
    assert result["ok"] is False and result["error"] == "mail_analysis_review_rejected"
    assert dispatched == []


def test_schema_failure_does_not_call_review_or_dispatch(tmp_path, monkeypatch):
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path)
    client = FakeClient({"unexpected": "schema"})
    dispatched = []
    monkeypatch.setattr(mail.workflow, "run_office_workflow",
                        lambda *args, **kwargs: dispatched.append((args, kwargs)))

    result = mail.run_task("整理邮件", base, task_root=base / ("task-" + "e" * 32),
                           source_artifact=ref, client=client)

    assert len(client.calls) == 1
    assert result["ok"] is False and result["error"] == "mail_analysis_schema_invalid"
    assert dispatched == []


def test_approved_analysis_uses_two_model_calls_then_one_local_office_dispatch(tmp_path, monkeypatch):
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path)
    client = FakeClient(_ready(schema=4), {"approved": True, "reason": "引用、事项和格式均匹配"})
    dispatched = []

    def office(steps, workflow_root, **kwargs):
        dispatched.append((steps, workflow_root, kwargs))
        return {"ok": True, "state": "succeeded", "completed": ["s1", "s2"],
                "receipts": {}}

    monkeypatch.setattr(mail.workflow, "run_office_workflow", office)
    result = mail.run_task("整理邮件为表格和报告", base,
                           task_root=base / ("task-" + "d" * 32),
                           source_artifact=ref, client=client)

    assert [call["system"] for call in client.calls] == [mail.SYSTEM, mail.REVIEW]
    assert len(dispatched) == 1
    assert [step["action"] for step in dispatched[0][0]] == ["spreadsheet", "document"]
    assert result["ok"] is True and result["state"] == "succeeded"


def test_cancel_before_review_prevents_second_call_and_office(tmp_path, monkeypatch):
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path)
    task_root = base / ("task-" + "c" * 32)

    def cancel_after_first(call_number, _call):
        if call_number == 1:
            (task_root / "cancel.flag").write_text("cancel", encoding="utf-8")

    client = FakeClient(_ready(schema=4), on_call=cancel_after_first)
    dispatched = []
    monkeypatch.setattr(mail.workflow, "run_office_workflow",
                        lambda *args, **kwargs: dispatched.append((args, kwargs)))

    result = mail.run_task("整理邮件", base, task_root=task_root,
                           source_artifact=ref, client=client)

    assert len(client.calls) == 1
    assert result["state"] == "cancelled" and result["error"] == "task_cancelled"
    assert dispatched == []


def test_repeated_task_root_is_rejected_before_another_model_call(tmp_path):
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path)
    # The first call exits before source snapshot; a duplicate claim still blocks replay.
    task_root = base / ("task-" + "b" * 32)
    client = FakeClient({"unexpected": "schema"})
    first = mail.run_task("整理邮件", base, task_root=task_root,
                          source_artifact=ref, client=client)
    assert first["ok"] is False
    count = len(client.calls)

    with pytest.raises(mail.MailUnderstandingError, match="mail_analysis_replay_blocked"):
        mail.run_task("整理邮件", base, task_root=task_root,
                      source_artifact=ref, client=client)
    assert len(client.calls) == count


@pytest.mark.parametrize("target", ["original", "plan", "instruction"])
def test_original_request_or_plan_tamper_after_review_blocks_office(tmp_path, monkeypatch, target):
    _snapshot_value, source_root, base, _export, ref = _mail_source(tmp_path)
    task_root = base / ("task-" + ("9" if target == "original" else "8") * 32)

    def tamper_after_review(call_number, _call):
        if call_number != 2:
            return
        if target == "original":
            (source_root / "mail-export-request.json").write_text("{}", encoding="utf-8")
        elif target == "plan":
            plan = task_root / "mail-analysis-plan.json"
            value = json.loads(plan.read_text(encoding="utf-8"))
            value["steps"][0]["data"]["title"] = "篡改计划"
            plan.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        else:
            (task_root / "request.txt").write_text("被篡改的任务原文", encoding="utf-8")

    client = FakeClient(_ready(schema=4), {"approved": True, "reason": "检查通过"},
                        on_call=tamper_after_review)
    dispatched = []
    monkeypatch.setattr(mail.workflow, "run_office_workflow",
                        lambda *args, **kwargs: dispatched.append((args, kwargs)))

    result = mail.run_task("整理邮件", base, task_root=task_root,
                           source_artifact=ref, client=client)

    assert len(client.calls) == 2
    assert result["ok"] is False
    assert result["error"] in {"mail_analysis_source_changed", "mail_analysis_plan_changed",
                               "request_changed", "model_or_research_error"}
    assert dispatched == []


def test_verify_task_rejects_modified_executable_steps(tmp_path, monkeypatch):
    _snapshot_value, _source_root, base, _export, ref = _mail_source(tmp_path)
    client = FakeClient(_ready(schema=4), {"approved": True, "reason": "检查通过"})
    monkeypatch.setattr(mail.workflow, "run_office_workflow",
                        lambda steps, _root, **_kwargs: {
                            "ok": True, "state": "succeeded",
                            "completed": [step["id"] for step in steps], "receipts": {}})
    task_root = base / ("task-" + "7" * 32)

    result = mail.run_task("整理邮件为表格和报告", base, task_root=task_root,
                           source_artifact=ref, client=client)
    assert result["ok"] is True

    altered_steps = mail.compile_steps(_ready())
    altered_steps[0]["data"]["title"] = "改写后的标题"
    with pytest.raises(mail.MailUnderstandingError, match="mail_analysis_plan_changed"):
        mail.verify_task(task_root, base, altered_steps)
