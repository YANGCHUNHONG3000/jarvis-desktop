"""Assistant→Office contracts with fake model, browser, and native executor."""

from hashlib import sha256
from pathlib import Path

import pytest

from agent_service import assistant
from agent_service.contracts import Observation
from agent_service.research import run_research


URLS = ["https://example.org/first", "https://example.net/second"]
EXCERPTS = ["Primary source reports three completed tasks.",
            "Secondary source reports two reviewed tasks."]


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return self.answers.pop(0)


class FakeBrowser:
    def read(self, url, should_stop=lambda: False):
        index = URLS.index(url)
        return Observation(url, url, f"Source {index + 1}", EXCERPTS[index],
                           "2026-10-17T00:00:00Z")


def fake_research(topic, root, max_sources=2):
    return run_research(topic, root, max_sources=max_sources,
                        search=lambda query, count: URLS[:count], browser=FakeBrowser())


def office_executor(monkeypatch):
    from agent_service import office_host
    observed_specs = []

    def execute(spec, step_root, should_stop):
        observed_specs.append(spec)
        suffix = ".xlsx" if spec["kind"] == "spreadsheet" else ".docx"
        path = Path(step_root) / ("fake" + suffix)
        content = (str(len(observed_specs)) + spec["title"]).encode("utf-8")
        path.write_bytes(content)
        if spec["kind"] == "spreadsheet":
            observation = {"headers": spec["headers"], "rows": spec["rows"],
                           "totals": {str(c): sum(row[c] for row in spec["rows"])
                                      for c in spec["sum_columns"]}}
        else:
            observation = {"text": "\n".join([spec["title"], *spec["paragraphs"]])}
            if "table" in spec:
                observation["table"] = {"headers": spec["table"]["headers"],
                                        "rows": [["" if cell is None else str(cell) for cell in row]
                                                 for row in spec["table"]["rows"]]}
        return {"ok": True, "state": "succeeded", "artifact": str(path),
                "sha256": sha256(content).hexdigest(), "observed": observation,
                "app": {"progid": "KWPS.Application" if suffix == ".docx" else "KET.Application",
                        "name": "WPS compatibility", "version": "12.0"},
                "application_verified": True, "file_verified": True}

    monkeypatch.setattr(office_host, "execute_office_supervised", execute)
    return observed_specs


def test_valid_office_plan_uses_verified_sheet_values_in_document(tmp_path, monkeypatch):
    observed_specs = office_executor(monkeypatch)
    plan = {"kind": "office", "steps": [
        {"id": "s1", "action": "spreadsheet", "data": {"kind": "spreadsheet",
         "title": "任务", "headers": ["项目", "数量"], "rows": [["甲", 3], ["乙", 2]],
         "sum_columns": [1]}},
        {"id": "s2", "action": "document_from_sheet", "from": "s1",
         "title": "汇总", "paragraphs": ["以核验表格为准"]}]}
    result = assistant.run_task("Create an Office summary", tmp_path, client=FakeClient(plan))
    assert result["ok"] is True and result["kind"] == "office"
    assert result["completed"] == ["s1", "s2"] and len(result["artifacts"]) == 2
    assert observed_specs[1]["table"] == {"headers": ["项目", "数量"],
                                           "rows": [["甲", 3], ["乙", 2], ["合计", 5]]}


@pytest.mark.parametrize("steps", [
    [{"id": "s1", "action": "shell", "command": "echo test"}],
    [{"id": "s1", "action": "document_from_sheet", "from": "s2",
      "title": "bad", "paragraphs": []}],
    [{"id": "s1", "action": "spreadsheet", "data": {"kind": "spreadsheet",
      "title": "bad", "headers": ["a"], "rows": [[1]], "sum_columns": [0]}},
     {"id": "s2", "action": "document_from_sheet", "from": "s2",
      "title": "bad", "paragraphs": []}],
    [{"id": "s1", "action": "document", "data": {"kind": "document",
      "title": "x", "paragraphs": [], "path": "C:/outside.docx"}}],
])
def test_invalid_action_or_reference_never_reaches_executor(tmp_path, monkeypatch, steps):
    observed_specs = office_executor(monkeypatch)
    client = FakeClient({"kind": "office", "steps": steps},
                        {"kind": "office", "steps": steps})
    result = assistant.run_task("Do unsafe Office operation", tmp_path, client=client)
    assert result["ok"] is False and result["state"] == "failed"
    assert observed_specs == [] and client.calls == 2


def test_research_outputs_office_files_from_verified_sources(tmp_path, monkeypatch):
    observed_specs = office_executor(monkeypatch)
    plan = {"kind": "research", "query": "task counts", "max_sources": 2,
            "office_formats": ["document", "spreadsheet"]}
    summary = {"title": "资料概览", "claims": [{"text": "两个来源提供任务数量。",
               "sources": [1, 2], "quotes": [
                   {"source": 1, "quote": "three completed tasks"},
                   {"source": 2, "quote": "two reviewed tasks"}]}]}
    result = assistant.run_task("Research and create Office files", tmp_path,
                                client=FakeClient(plan, summary), research=fake_research)
    assert result["ok"] is True and result["kind"] == "office"
    assert result["sources"] == URLS and result["source_count"] == 2
    assert result["completed"] == ["s1", "s2"]
    document, spreadsheet = observed_specs
    assert all(url in "\n".join(document["paragraphs"]) for url in URLS)
    assert spreadsheet["rows"][0][0] == summary["claims"][0]["text"]
    assert all(url in spreadsheet["rows"][0][2] for url in URLS)


def test_failed_office_workflow_cannot_mark_assistant_success(tmp_path, monkeypatch):
    from agent_service import office_host

    def failed(spec, step_root, should_stop):
        return {"ok": False, "state": "unknown", "code": "office_operation_failed",
                "application_verified": False, "file_verified": False}

    monkeypatch.setattr(office_host, "execute_office_supervised", failed)
    plan = {"kind": "office", "steps": [{"id": "s1", "action": "document",
            "data": {"kind": "document", "title": "Title", "paragraphs": ["Body"]}}]}
    result = assistant.run_task("Create report", tmp_path, client=FakeClient(plan))
    assert result["ok"] is False and result["state"] == "unknown"
    assert result["artifacts"] == [] and result["artifact"] is None
    workflow = Path(result["workflow_root"])
    assert (workflow / "s1" / "intent.json").is_file()
    assert not (workflow / "s1" / "receipt.json").exists()


def test_research_retry_keeps_original_office_output_request(tmp_path, monkeypatch):
    observed_specs = office_executor(monkeypatch)
    attempts = 0

    def research(topic, root, max_sources=2):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"ok": False, "reason": "source_rejected", "sources": [URLS[0]]}
        return fake_research(topic, root, max_sources=max_sources)

    first = {"kind": "research", "query": "original", "max_sources": 1,
             "office_formats": ["document"]}
    revised = {"kind": "research", "query": "revised", "max_sources": 1}
    summary = {"title": "Retry summary", "claims": [{"text": "Task count summarized.",
               "sources": [1], "quotes": [{"source": 1, "quote": "three completed tasks"}]}]}
    result = assistant.run_task("Research and make a Word document", tmp_path,
                                client=FakeClient(first, revised, summary), research=research)
    assert attempts == 2 and result["ok"] is True and result["kind"] == "office"
    assert len(observed_specs) == 1 and observed_specs[0]["kind"] == "document"
