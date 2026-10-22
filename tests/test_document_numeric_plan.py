"""Explicit DOCX text-to-number plan; no native Office or paid model calls."""

from copy import deepcopy

import pytest

from agent_service.office_source_plan import SourcePlanError, describe_source_edits, resolve_source_steps


def snapshot(rows=None):
    return {"source_id": "selected", "kind": "document", "sha256": "a" * 64,
            "display_name": "来源.docx", "observed": {"paragraphs": ["正文保留在原件"],
            "table": {"headers": ["编号", "备注", "金额"],
                      "rows": rows if rows is not None else [["001", "汉字", "3.0"],
                                                               ["=1+1", "原文", "5.0"]]}}}


def numeric(*, columns=None, numeric_columns=None, sum_columns=None):
    return {"id": "s1", "action": "spreadsheet_from_document_table_numeric",
            "source_id": "selected", "title": "数字汇总", "columns": [0, 2, 1] if columns is None else columns,
            "numeric_columns": [2] if numeric_columns is None else numeric_columns,
            "sum_columns": [1] if sum_columns is None else sum_columns}


def report():
    return {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "汇总报告"}


def test_reordered_numeric_conversion_maps_source_to_output_and_audits():
    source = snapshot()
    before = deepcopy(source)
    plan = [numeric(), report()]
    steps = resolve_source_steps(plan, source)
    assert steps[0]["data"] == {"kind": "spreadsheet", "title": "数字汇总",
                                "headers": ["编号", "金额", "备注"],
                                "rows": [["001", 3.0, "汉字"], ["=1+1", 5.0, "原文"]],
                                "sum_columns": [1]}
    assert steps[1]["action"] == "document_from_sheet"
    assert "重新求和" in steps[1]["paragraphs"][-1]
    assert "未重新计算" not in steps[1]["paragraphs"][-1]
    assert describe_source_edits(plan, source) == [
        {"step": "s1", "row": 0, "column": 2, "output_column": 1,
         "before": "3.0", "after": 3.0, "op": "convert_numeric", "sum_columns": [1]},
        {"step": "s1", "row": 1, "column": 2, "output_column": 1,
         "before": "5.0", "after": 5.0, "op": "convert_numeric", "sum_columns": [1]}]
    assert source == before
    steps[0]["data"]["rows"][0][0] = "mutated"
    assert source == before


def test_two_numeric_columns_follow_reordered_source_indices():
    source = snapshot([["001", "2", "3.0"], ["=1+1", "4", "5.0"]])
    step = numeric(columns=[0, 2, 1], numeric_columns=[1, 2], sum_columns=[1, 2])
    data = resolve_source_steps([step], source)[0]["data"]
    assert data["rows"] == [["001", 3.0, 2], ["=1+1", 5.0, 4]]
    assert data["sum_columns"] == [1, 2]
    audit = describe_source_edits([step], source)
    assert [(item["row"], item["column"], item["output_column"]) for item in audit] == [
        (0, 2, 1), (0, 1, 2), (1, 2, 1), (1, 1, 2)]


@pytest.mark.parametrize("bad", [
    {"columns": [0]}, {"columns": [2, 0]}, {"numeric_columns": []},
    {"numeric_columns": [True]}, {"sum_columns": []}, {"sum_columns": [0]},
    {"sum_columns": [2]}, {"numeric_columns": [1]}, {"columns": [0, 2, 2]},
    {"edits": []}, {"append_rows": []}, {"rows": []}, {"path": "C:/other.xlsx"},
])
def test_columns_and_fields_are_exact(bad):
    step = numeric()
    step.update(bad)
    with pytest.raises(SourcePlanError):
        resolve_source_steps([step], snapshot())


@pytest.mark.parametrize("text", ["", " 8 ", "0012", "-0", "-0.00", "=1+1",
                                        "1,234", "¥20", "20%", "1e3", "0.12345678901234567",
                                        "1000000000000000", "NaN", "Infinity"])
def test_ambiguous_numeric_spellings_reject_entire_plan(text):
    with pytest.raises(SourcePlanError):
        resolve_source_steps([numeric(), report()], snapshot([["001", "note", text]]))


@pytest.mark.parametrize("label", ["合计", " 小计 ", "总计：", "汇总", "TOTAL", "SubTotal:", "Grand  Total"])
def test_recognized_existing_total_is_never_summed_again(label):
    with pytest.raises(SourcePlanError, match="source_existing_total_unsupported"):
        resolve_source_steps([numeric()], snapshot([[label, "原值", "8.0"]]))


def test_sum_precision_and_report_row_limit_reject_before_effect():
    with pytest.raises(SourcePlanError, match="source_numeric_values_invalid"):
        resolve_source_steps([numeric()], snapshot([["a", "", "999999999999999"],
                                                    ["b", "", "2"]]))
    rows = [[str(i), "", "1"] for i in range(200)]
    assert len(resolve_source_steps([numeric()], snapshot(rows))[0]["data"]["rows"]) == 200
    assert len(resolve_source_steps([numeric(), report()], snapshot(rows[:199]))[0]["data"]["rows"]) == 199
    with pytest.raises(SourcePlanError, match="source_report_too_large"):
        resolve_source_steps([numeric(), report()], snapshot(rows))
    with pytest.raises(SourcePlanError, match="source_numeric_values_invalid"):
        resolve_source_steps([numeric()], snapshot([]))


def test_literal_action_retains_existing_total_without_new_sum():
    source = snapshot([["合计", "原文", "8.0"]])
    step = {"id": "s1", "action": "spreadsheet_from_document_table", "source_id": "selected",
            "title": "原文副本", "columns": [0, 1, 2]}
    data = resolve_source_steps([step], source)[0]["data"]
    assert data["rows"] == [["合计", "原文", "8.0"]] and data["sum_columns"] == []
