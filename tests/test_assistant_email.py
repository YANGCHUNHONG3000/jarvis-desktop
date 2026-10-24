"""Natural language→verified Office artifacts→local unsent draft, offline only."""

from hashlib import sha256
from pathlib import Path

import pytest

from agent_service import assistant, office_readiness


@pytest.fixture(autouse=True)
def offline_office_readiness(monkeypatch):
    monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: None)
from agent_service.contracts import Observation
from agent_service.research import run_research


URL = "https://example.org/task-count"
EXCERPT = "The verified page reports three completed tasks."


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0
        self.users = []

    def complete(self, *, system, user, max_tokens):
        self.calls += 1
        self.users.append(user)
        return self.answers.pop(0)


class FakeBrowser:
    def read(self, url, should_stop=lambda: False):
        return Observation(url, url, "Task count", EXCERPT, "2026-10-17T00:00:00Z")


def fake_research(topic, root, max_sources=2):
    return run_research(topic, root, max_sources=max_sources,
                        search=lambda query, count: [URL], browser=FakeBrowser())


def fake_office(monkeypatch):
    from agent_service import office_host
    seen = []

    def execute(spec, step_root, should_stop):
        seen.append(spec)
        filename = "spreadsheet.xlsx" if spec["kind"] == "spreadsheet" else "document.docx"
        path = Path(step_root) / filename
        content = (spec["title"] + "-verified").encode("utf-8")
        path.write_bytes(content)
        if spec["kind"] == "spreadsheet":
            observation = {"headers": spec["headers"], "rows": spec["rows"],
                           "totals": {str(c): sum(row[c] for row in spec["rows"])
                                      for c in spec["sum_columns"]}}
        else:
            observation = {"text": "\n".join([spec["title"], *spec["paragraphs"]])}
            if "table" in spec:
                observation["table"] = {"headers": spec["table"]["headers"],
                                        "rows": [["" if value is None else str(value) for value in row]
                                                 for row in spec["table"]["rows"]]}
        return {"ok": True, "state": "succeeded", "artifact": str(path),
                "sha256": sha256(content).hexdigest(), "observed": observation,
                "app": {"progid": "KET.Application" if spec["kind"] == "spreadsheet" else "KWPS.Application",
                        "name": "WPS compatibility", "version": "12.0"},
                "application_verified": True, "file_verified": True}

    monkeypatch.setattr(office_host, "execute_office_supervised", execute)
    return seen


def _summary():
    return {"title": "任务资料", "claims": [{"text": "已完成三项任务。", "sources": [1],
            "quotes": [{"source": 1, "quote": "three completed tasks"}]}]}


def test_office_document_and_spreadsheet_become_unsent_draft_attachments(tmp_path, monkeypatch):
    seen = fake_office(monkeypatch)
    plan = {"kind": "office", "steps": [
        {"id": "s1", "action": "spreadsheet", "data": {"kind": "spreadsheet", "title": "任务",
         "headers": ["项目", "数量"], "rows": [["甲", 3]], "sum_columns": [1]}},
        {"id": "s2", "action": "document_from_sheet", "from": "s1", "title": "报告",
         "paragraphs": ["见表格"]},
        {"id": "s3", "action": "email_draft", "data": {"subject": "工作汇总",
         "body": "您好，请查收附件。", "to": ["review@example.org"]},
         "attachments": ["s1", "s2"]}]}
    result = assistant.run_task("为 review@example.org 准备工作汇总邮件草稿和表格报告", tmp_path,
                                client=FakeClient(plan, {"approved": True, "reason": "Explicit recipient"}))
    assert result["ok"] is True and result["state"] == "succeeded"
    assert result["completed"] == ["s1", "s2", "s3"] and len(seen) == 2
    assert result["artifact"].endswith("draft.eml")
    draft = Path(result["artifact"])
    assert draft.is_file() and sha256(draft.read_bytes()).hexdigest() == result["readback_sha256"]
    assert len(result["artifacts"]) == 3


def test_selected_csv_to_sheet_report_and_draft_keeps_real_values(tmp_path, monkeypatch):
    seen = fake_office(monkeypatch)
    source = tmp_path / "source.csv"
    source.write_text("项目,数量\n甲,3\n乙,2\n", encoding="utf-8")
    plan = {"kind": "source_office", "steps": [
        {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
         "title": "任务表", "columns": [0, 1], "sum_columns": [1]},
        {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "任务报告"},
        {"id": "s3", "action": "email_draft", "data": {"subject": "任务材料",
         "body": "请看附件。", "to": []}, "attachments": ["s1", "s2"]}]}
    client = FakeClient(plan)
    result = assistant.run_task("从所选资料制作表格、报告与邮件草稿", tmp_path / "tasks",
                                client=client, source_file=source)
    assert result["ok"] is True and result["completed"] == ["s1", "s2", "s3"]
    assert seen[0]["rows"] == [["甲", 3], ["乙", 2]]
    assert seen[1]["table"]["rows"] == [["甲", 3], ["乙", 2], ["合计", 5]]
    assert source.read_text(encoding="utf-8") == "项目,数量\n甲,3\n乙,2\n"
    assert str(source.resolve()) not in client.users[0]


def test_research_retry_preserves_email_request_and_attaches_verified_document(tmp_path, monkeypatch):
    seen = fake_office(monkeypatch)
    attempts = 0

    def research(topic, root, max_sources=2):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"ok": False, "reason": "source_rejected", "sources": [URL]}
        return fake_research(topic, root, max_sources=max_sources)

    first = {"kind": "research", "query": "first", "max_sources": 1,
             "office_formats": ["document"],
             "email_draft": {"subject": "研究材料", "body": "请看附件。", "to": []}}
    revised = {"kind": "research", "query": "second", "max_sources": 1}
    result = assistant.run_task("研究后准备带文档的邮件草稿", tmp_path,
                                client=FakeClient(first, revised, _summary()), research=research)
    assert attempts == 2 and result["ok"] is True
    assert result["completed"] == ["s1", "s2"] and result["artifact"].endswith("draft.eml")
    assert len(seen) == 1 and seen[0]["kind"] == "document"
    assert URL in "\n".join(seen[0]["paragraphs"])


def test_guessed_recipient_is_rejected_before_any_office_effect(tmp_path, monkeypatch):
    seen = fake_office(monkeypatch)
    plan = {"kind": "office", "steps": [
        {"id": "s1", "action": "document", "data": {"kind": "document",
         "title": "报告", "paragraphs": ["内容"]}},
        {"id": "s2", "action": "email_draft", "data": {"subject": "报告",
         "body": "请查收。", "to": ["guessed@example.org"]}, "attachments": ["s1"]}]}
    result = assistant.run_task("准备一份报告和邮件草稿", tmp_path, client=FakeClient(plan))
    assert result["ok"] is False and result["error"] == "recipient_not_requested"
    assert seen == [] and list(Path(result["root"]).glob("workflow-*")) == []


def test_standalone_empty_recipient_draft_succeeds_without_office_app(tmp_path, monkeypatch):
    seen = fake_office(monkeypatch)
    plan = {"kind": "office", "steps": [{"id": "s1", "action": "email_draft",
            "data": {"subject": "待发送草稿", "body": "您好。", "to": []}, "attachments": []}]}
    result = assistant.run_task("准备一个未发送的邮件草稿", tmp_path, client=FakeClient(plan))
    assert result["ok"] is True and result["completed"] == ["s1"]
    assert seen == [] and result["artifact"].endswith("draft.eml")
