"""DOCX numeric plans must receive a bounded independent intent review."""

from copy import deepcopy
import json

import pytest

from agent_service import assistant, local_skills, office_source, office_workflow, skill_matching
from agent_service.office_source_plan import resolve_source_steps


def _snapshot(rows=None):
    return {"source_id": "selected", "kind": "document", "sha256": "a" * 64,
            "bytes": 123, "display_name": "source.docx", "coverage": {"scope": "main_body_only"},
            "observed": {
                "paragraphs": ["正文不进入表格"],
                "table": {"headers": ["项目", "数量", "备注"],
                          "rows": rows if rows is not None else [
                              ["甲", "3", "原样"], ["乙", "2", "=literal"]]}}}


def _plan():
    return {"kind": "source_office", "steps": [
        {"id": "s1", "action": "spreadsheet_from_document_table_numeric",
         "source_id": "selected", "title": "新表格", "columns": [0, 1, 2],
         "numeric_columns": [1], "sum_columns": [1]},
        {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "新报告"}]}


class Client:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.ledger = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.answers.pop(0)


def _run(tmp_path, monkeypatch, client, snapshot=None):
    source = tmp_path / "source.docx"
    source.write_bytes(b"synthetic-selected-source")
    value = _snapshot() if snapshot is None else snapshot
    monkeypatch.setattr(office_source, "snapshot_source", lambda *_args: deepcopy(value))
    calls = []

    def office(steps, root, **_kwargs):
        calls.append(steps)
        return {"ok": False, "state": "unknown", "receipts": {}, "completed": [],
                "error": "synthetic_no_effect"}

    monkeypatch.setattr(office_workflow, "run_office_workflow", office)
    result = assistant.run_task("把 Word 唯一表格第二列数字转换并对全部明细求和，生成 Excel 和 Word 报告",
                                tmp_path / "tasks", source_file=source, client=client)
    return result, calls


def test_numeric_source_prompt_keeps_literal_action_distinct_and_describes_exact_coordinates():
    prompt = assistant._source_plan_prompt(_snapshot())
    assert '"action":"spreadsheet_from_document_table"' in prompt
    assert '"action":"spreadsheet_from_document_table_numeric"' in prompt
    assert '"numeric_columns":[1],"sum_columns":[1]' in prompt
    for rule in ("zero-based SOURCE", "zero-based OUTPUT", "Preserve ALL rows",
                 "existing aggregate rows are unsupported", "Never supply cell values"):
        assert rule in prompt
    one_column = _snapshot()
    one_column["observed"]["table"] = {"headers": ["数字"], "rows": [["3"]]}
    assert '"action":"spreadsheet_from_document_table_numeric"' not in assistant._source_plan_prompt(one_column)


def test_numeric_document_review_shows_original_rows_conversion_and_all_row_sum(tmp_path, monkeypatch):
    client = Client(_plan(), {"approved": True, "reason": "明确要求第二列求和"})
    result, calls = _run(tmp_path, monkeypatch, client)
    assert result["kind"] == "office" and len(calls) == 1
    assert calls[0][0]["data"]["rows"] == [["甲", 3, "原样"], ["乙", 2, "=literal"]]
    assert len(client.calls) == 2
    review = json.loads(client.calls[1]["user"])
    assert review["source_headers"] == ["项目", "数量", "备注"]
    assert review["edited_source_rows"] == {
        "0": ["甲", "3", "原样"], "1": ["乙", "2", "=literal"]}
    assert review["numeric_conversion"] == {
        "source_kind": "document", "source_columns": [1], "output_sum_columns": [1],
        "source_row_count": 2, "converted_cells": 2, "all_source_rows_retained": True}
    assert [(e["row"], e["column"], e["output_column"], e["before"], e["after"])
            for e in review["proposed_source_edits"]] == [
                (0, 1, 1, "3", 3), (1, 1, 1, "2", 2)]
    assert "EVERY retained source detail row" in client.calls[1]["system"]


@pytest.mark.parametrize("verdict", [
    {"approved": False, "reason": "列目标不明确"},
    {"approved": "true", "reason": "错误类型"},
    {},
])
def test_numeric_review_rejection_blocks_native_effect(tmp_path, monkeypatch, verdict):
    client = Client(_plan(), verdict)
    result, calls = _run(tmp_path, monkeypatch, client)
    assert not result["ok"] and calls == []
    assert len(client.calls) == 2


def test_numeric_audit_oversize_fails_closed_without_truncation_or_effect(tmp_path, monkeypatch):
    headers = [f"列{i}" for i in range(12)]
    snapshot = _snapshot([[f"行{i}"] + ["1"] * 11 for i in range(199)])
    snapshot["observed"]["table"]["headers"] = headers
    plan = _plan()
    plan["steps"][0].update(columns=list(range(12)), numeric_columns=list(range(1, 12)),
                            sum_columns=list(range(1, 12)))
    client = Client(plan)
    result, calls = _run(tmp_path, monkeypatch, client, snapshot)
    assert not result["ok"] and result["error"] == "intent_review_oversize"
    assert calls == [] and len(client.calls) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_fixed_numeric_skill_review_includes_original_cells_and_blocks_rejected_effect(
        tmp_path, monkeypatch, approved):
    source = tmp_path / "selected.docx"
    source.write_bytes(b"synthetic-selected-source")
    snapshot = _snapshot()
    monkeypatch.setattr(office_source, "snapshot_source", lambda *_args: deepcopy(snapshot))
    candidate = {"id": "document-numeric-report", "sha256": "b" * 64,
                 "status": "available", "recipe": "document_numeric_report",
                 "name": "Word 数字报告", "effects": [{"output": ".xlsx", "sum_columns": [1],
                                                "numeric_source_columns": [1]}]}
    monkeypatch.setattr(skill_matching, "discover_candidates", lambda *_a, **_kw: {
        "schema": 1, "candidates": [candidate], "hints": {"matched_ids": []}})
    monkeypatch.setattr(skill_matching, "candidate_plan", lambda *_args:
                        resolve_source_steps(_plan()["steps"], snapshot))
    effects = []

    def run_skill(*_args, **kwargs):
        effects.append(True)
        return kwargs["publish"]({"ok": True, "state": "succeeded", "kind": "office",
                                   "skill": {"id": candidate["id"]}})

    monkeypatch.setattr(local_skills, "run_prepared_skill", run_skill)
    choice = {"kind": "local_skill", "id": candidate["id"], "sha256": candidate["sha256"]}
    verdict = {"approved": approved, "reason": "按请求" if approved else "要求不明确"}
    answers = [choice, verdict] if approved else [choice, verdict, {
        "kind": "unsupported", "reason": "列目标不明确"}]
    client = Client(*answers)
    result = assistant.run_task("把 Word 表格第二列转换为数字并对所有明细求和，生成 Excel 和 Word 报告",
                                tmp_path / "tasks", source_file=source, client=client)

    review = json.loads(client.calls[1]["user"])
    numeric = review["numeric_conversion"]
    assert numeric["source_rows"] == [["甲", "3", "原样"], ["乙", "2", "=literal"]]
    assert numeric["source_columns"] == numeric["output_sum_columns"] == [1]
    assert [(entry["before"], entry["after"]) for entry in numeric["conversion_audit"]] == [
        ("3", 3), ("2", 2)]
    assert "For numeric_conversion" in client.calls[1]["system"]
    assert effects == ([True] if approved else [])
    assert result["ok"] is approved
    assert result["state"] == ("succeeded" if approved else "unsupported")
