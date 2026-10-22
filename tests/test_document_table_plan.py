"""Document-table handoff is literal, bounded, and remains offline in these tests."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import assistant, office_source, office_workflow
from agent_service.office_source_plan import SourcePlanError, resolve_source_steps


HASH = "a" * 64


def document_snapshot():
    return {"source_id": "selected", "kind": "document", "sha256": HASH,
            "bytes": 456, "display_name": "原始材料.docx",
            "coverage": {"scope": "main_body_only"},
            "observed": {"paragraphs": ["正文中的数字 42 不属于表格"],
                         "table": {"headers": ["编号", "表达式", "金额"],
                                   "rows": [["0012", "=SUM(A1:A2)", ""],
                                            ["", "=1+1", " 8 "],
                                            ["合计", "已有结果", "8"]]}}}


def sheet(*, columns=None):
    return {"id": "s1", "action": "spreadsheet_from_document_table",
            "source_id": "selected", "title": "提取表格",
            "columns": [2, 0, 1] if columns is None else columns}


def report():
    return {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "提取报告"}


def test_literal_selection_preserves_identity_formula_blank_total_and_source():
    original = document_snapshot()
    before = deepcopy(original)
    steps = resolve_source_steps([sheet(), report()], original)
    assert steps == office_workflow.validate_steps(steps)
    assert steps[0] == {"id": "s1", "action": "spreadsheet",
                        "data": {"kind": "spreadsheet", "title": "提取表格",
                                 "headers": ["金额", "编号", "表达式"],
                                 "rows": [["", "0012", "=SUM(A1:A2)"],
                                          [" 8 ", "", "=1+1"],
                                          ["8", "合计", "已有结果"]],
                                 "sum_columns": []}}
    assert all(type(cell) is str for row in steps[0]["data"]["rows"] for cell in row)
    assert steps[1]["action"] == "document_from_sheet"
    assert "仅提取来源文档的表格" in steps[1]["paragraphs"][-1]
    assert "已有合计行" in steps[1]["paragraphs"][-1]
    assert "未重新计算" in steps[1]["paragraphs"][-1]
    assert original == before
    steps[0]["data"]["rows"][0][0] = "mutated"
    assert original == before


def test_missing_table_is_explicit_input_failure():
    snapshot = document_snapshot()
    del snapshot["observed"]["table"]
    with pytest.raises(SourcePlanError, match="source_document_table_missing"):
        resolve_source_steps([sheet()], snapshot)


@pytest.mark.parametrize("bad", [
    {"columns": [0, 0]}, {"columns": [True]}, {"columns": [-1]},
    {"columns": [3]}, {"columns": []}, {"source_id": "other"},
    {"sum_columns": []}, {"edits": []}, {"append_rows": []},
    {"rows": []}, {"path": "C:/other.xlsx"},
])
def test_rejects_invalid_columns_extra_fields_and_model_supplied_values(bad):
    action = sheet()
    action.update(bad)
    with pytest.raises(SourcePlanError):
        resolve_source_steps([action], document_snapshot())


def test_wrong_source_kind_and_nontext_cells_or_headers_rejected():
    table = document_snapshot()
    table["kind"] = "table"
    table["observed"] = table["observed"]["table"]
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet()], table)
    for location in ("header", "cell"):
        snapshot = document_snapshot()
        if location == "header":
            snapshot["observed"]["table"]["headers"][0] = 12
        else:
            snapshot["observed"]["table"]["rows"][0][0] = 12
        with pytest.raises(SourcePlanError):
            resolve_source_steps([sheet()], snapshot)


def test_existing_spreadsheet_header_contract_rejects_empty_header():
    snapshot = document_snapshot()
    snapshot["observed"]["table"]["headers"][0] = ""
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet()], snapshot)


@pytest.mark.parametrize("change", [
    lambda table: table["rows"].append(["x", "y"]),
    lambda table: table["rows"].extend([["x", "y", "z"]] * 201),
    lambda table: table["rows"][0].__setitem__(0, "x" * 2001),
])
def test_malformed_or_oversize_table_rejected_before_workflow(change):
    snapshot = document_snapshot()
    change(snapshot["observed"]["table"])
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet()], snapshot)


def test_report_reference_must_point_to_prior_extracted_sheet():
    with pytest.raises(SourcePlanError):
        resolve_source_steps([report()], document_snapshot())
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(), {**report(), "from": "s3"}], document_snapshot())


class Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.ledger = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def test_assistant_schema_and_prompt_route_without_native_office(tmp_path, monkeypatch):
    selected = tmp_path / "selected.docx"
    selected.write_bytes(b"fixture")
    snapshot = document_snapshot()
    monkeypatch.setattr(office_source, "snapshot_source", lambda path, root: deepcopy(snapshot))
    captured = []
    def fake_workflow(steps, root, **kwargs):
        captured.append(steps)
        return {"ok": False, "state": "unknown", "receipts": {}, "completed": [],
                "error": "synthetic_no_effect"}
    monkeypatch.setattr(office_workflow, "run_office_workflow", fake_workflow)
    client = Client({"kind": "source_office", "steps": [sheet(), report()]})
    result = assistant.run_task("只把文档表格原样转成 Excel，再生成 Word 报告；不要重新计算合计，也不要转换数字",
                                tmp_path / "tasks", source_file=selected, client=client)
    assert result["kind"] == "office" and result["state"] == "unknown"
    assert len(client.calls) == 1 and len(captured) == 1
    prompt = client.calls[0]["system"]
    assert "spreadsheet_from_document_table" in prompt
    assert "report_from_sheet" in prompt and "SAVED and verified sheet values" in prompt
    assert "original selected file is DOCX" in prompt
    assert "spreadsheet_from_document_table_numeric" in prompt
    assert "existing aggregate rows are unsupported" in prompt
    assert "Do not add edits, sum_columns, numeric coercion" in prompt
    assert captured[0][0]["data"]["rows"][0] == ["", "0012", "=SUM(A1:A2)"]
    assert snapshot == document_snapshot()


def test_model_unsupported_numeric_request_causes_no_office_effect(tmp_path, monkeypatch):
    selected = tmp_path / "selected.docx"
    selected.write_bytes(b"fixture")
    monkeypatch.setattr(office_source, "snapshot_source",
                        lambda path, root: deepcopy(document_snapshot()))
    monkeypatch.setattr(office_workflow, "run_office_workflow",
                        lambda *args, **kwargs: pytest.fail("numeric downgrade reached Office"))
    client = Client({"kind": "unsupported", "reason": "文档表格数值重算尚不支持"})
    result = assistant.run_task("把 Word 表格转 Excel 并重新计算合计", tmp_path / "tasks",
                                source_file=selected, client=client)
    assert result["kind"] == "unsupported" and result["state"] == "unsupported"
    assert len(client.calls) == 1
    assert json.loads((Path(result["root"]) / "result.json").read_text(encoding="utf-8")) == result


def test_source_prompt_shows_only_applicable_examples_and_explicit_handoff():
    document = document_snapshot()
    doc_prompt = assistant._source_plan_prompt(document)
    assert '"action":"document_from_source"' in doc_prompt
    assert '"action":"spreadsheet_from_document_table"' in doc_prompt
    assert '"action":"report_from_sheet","from":"s1"' in doc_prompt
    assert '"action":"spreadsheet_from_source"' not in doc_prompt
    assert "newly saved Excel" in doc_prompt
    assert "initial selected file type does not restrict" in doc_prompt
    assert "A request to preserve an existing total literally" in doc_prompt

    no_table = deepcopy(document)
    del no_table["observed"]["table"]
    no_table_prompt = assistant._source_plan_prompt(no_table)
    assert '"action":"document_from_source"' in no_table_prompt
    assert '"action":"spreadsheet_from_document_table"' not in no_table_prompt
    assert "no extracted table" in no_table_prompt
    assert "must be unsupported" in no_table_prompt

    tabular = {"source_id": "selected", "kind": "table", "sha256": HASH,
               "display_name": "数据.csv", "observed": {"headers": ["列"], "rows": [["值"]]}}
    table_prompt = assistant._source_plan_prompt(tabular)
    assert '"action":"spreadsheet_from_source"' in table_prompt
    assert '"action":"report_from_sheet","from":"s1"' in table_prompt
    assert '"columns":[0]' in table_prompt
    assert '"action":"document_from_source"' not in table_prompt
    assert '"action":"spreadsheet_from_document_table"' not in table_prompt


@pytest.mark.parametrize("width", [3, 12])
def test_source_prompt_defaults_to_every_column_for_both_source_kinds(width):
    expected = '"columns":[' + ','.join(str(index) for index in range(width)) + ']'
    document = document_snapshot()
    document["observed"]["table"]["headers"] = [f"列{index}" for index in range(width)]
    document["observed"]["table"]["rows"] = [[f"值{index}" for index in range(width)]]
    document_prompt = assistant._source_plan_prompt(document)
    assert expected in document_prompt
    assert "Unless the USER explicitly requests selecting or reordering columns, keep ALL columns" in document_prompt

    table = {"source_id": "selected", "kind": "table", "sha256": HASH,
             "display_name": "数据.csv",
             "observed": {"headers": [f"列{index}" for index in range(width)],
                          "rows": [[f"值{index}" for index in range(width)]]}}
    table_prompt = assistant._source_plan_prompt(table)
    assert expected in table_prompt
    assert "Unless the USER explicitly requests selecting or reordering columns, keep ALL columns" in table_prompt
