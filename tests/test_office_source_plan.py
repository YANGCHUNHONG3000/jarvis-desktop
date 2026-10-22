"""Pure selected-source Office planning; no model, filesystem, or COM effects."""

from copy import deepcopy

import pytest

from agent_service.office_source_plan import SourcePlanError, resolve_source_steps
from agent_service.office_workflow import validate_steps


HASH = "a" * 64


def table_snapshot():
    return {"source_id": "selected", "kind": "table", "sha256": HASH,
            "bytes": 123, "display_name": "数据.csv", "coverage": {"scope": "all_rows"},
            "observed": {"headers": ["项目", "数量", "原文"],
                         "rows": [["甲", 3, "=1+1"], ["乙", 2, "中文说明"]]}}


def document_snapshot():
    return {"source_id": "selected", "kind": "document", "sha256": HASH,
            "bytes": 456, "display_name": "原始材料.docx", "coverage": {"scope": "main_body_only"},
            "observed": {"paragraphs": ["第一段", "第二段"],
                         "table": {"headers": ["事项", "状态"],
                                   "rows": [["核对", "完成"], ["归档", "待办"]]}}}


def derived_sheet():
    return {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
            "title": "工作量", "columns": [1, 0, 2], "sum_columns": [0]}


def test_reorders_columns_without_changing_values_or_row_count():
    snapshot = table_snapshot()
    original = deepcopy(snapshot)
    steps = resolve_source_steps([derived_sheet()], snapshot)
    assert steps == validate_steps(steps)
    data = steps[0]["data"]
    assert steps[0]["action"] == "spreadsheet"
    assert data == {"kind": "spreadsheet", "title": "工作量",
                    "headers": ["数量", "项目", "原文"],
                    "rows": [[3, "甲", "=1+1"], [2, "乙", "中文说明"]],
                    "sum_columns": [0]}
    assert snapshot == original
    data["rows"][0][1] = "new"
    assert snapshot == original


def test_document_source_copies_every_observed_paragraph_and_table_cell():
    snapshot = document_snapshot()
    original = deepcopy(snapshot)
    steps = resolve_source_steps([{"id": "s1", "action": "document_from_source",
                                   "source_id": "selected", "title": "副本"}], snapshot)
    assert steps == validate_steps(steps)
    assert steps[0]["action"] == "document"
    assert steps[0]["data"] == {"kind": "document", "title": "副本",
                                 "paragraphs": ["第一段", "第二段"],
                                 "table": snapshot["observed"]["table"]}
    assert snapshot == original


def test_report_uses_only_prior_derived_sheet_and_fixed_provenance():
    steps = resolve_source_steps([derived_sheet(),
                                  {"id": "s2", "action": "report_from_sheet",
                                   "from": "s1", "title": "汇总报告"}], table_snapshot())
    assert steps == validate_steps(steps)
    assert steps[1] == {"id": "s2", "action": "document_from_sheet", "from": "s1",
                        "title": "汇总报告",
                        "paragraphs": ["来源文件：数据.csv", "来源 SHA-256：" + HASH[:16]]}


@pytest.mark.parametrize("bad", [
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [0, 0], "sum_columns": []},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [True], "sum_columns": []},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [3], "sum_columns": []},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [2], "sum_columns": [0]},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [0], "sum_columns": [1]},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "other",
     "title": "x", "columns": [0], "sum_columns": []},
    {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
     "title": "x", "columns": [0], "sum_columns": [], "path": "C:/overwrite.xlsx"},
    {"id": "s1", "action": "document", "data": {"kind": "document", "title": "invented",
                                             "paragraphs": ["invented"]}},
])
def test_rejects_invalid_columns_sums_paths_or_invented_actions(bad):
    with pytest.raises(SourcePlanError):
        resolve_source_steps([bad], table_snapshot())


@pytest.mark.parametrize("steps", [
    [{"id": "s1", "action": "report_from_sheet", "from": "s2", "title": "x"}],
    [{"id": "s1", "action": "report_from_sheet", "from": "s1", "title": "x"}],
    [derived_sheet(), {"id": "s2", "action": "report_from_sheet", "from": "other", "title": "x"}],
    [derived_sheet(), {"id": "s2", "action": "report_from_sheet", "from": "s1",
                       "title": "x", "paragraphs": ["invented"]}],
    [derived_sheet(), {"id": "s3", "action": "report_from_sheet", "from": "s1", "title": "x"}],
])
def test_rejects_forward_unknown_self_or_edited_report_references(steps):
    with pytest.raises(SourcePlanError):
        resolve_source_steps(steps, table_snapshot())


@pytest.mark.parametrize("change", [
    {"source_id": "other"},
    {"sha256": "xyz"},
    {"display_name": "../private.csv"},
    {"display_name": "C:\\private.csv"},
    {"bytes": True},
    {"coverage": "not evidence"},
    {"path": "C:/external.csv"},
])
def test_rejects_untrusted_or_malformed_snapshot_metadata(change):
    snapshot = table_snapshot()
    snapshot.update(change)
    with pytest.raises(SourcePlanError, match="source_snapshot_invalid"):
        resolve_source_steps([derived_sheet()], snapshot)


def test_wrong_source_kind_and_step_limit_rejected():
    with pytest.raises(SourcePlanError):
        resolve_source_steps([derived_sheet()], document_snapshot())
    with pytest.raises(SourcePlanError):
        resolve_source_steps([{"id": "s1", "action": "document_from_source",
                               "source_id": "selected", "title": "x"}], table_snapshot())
    with pytest.raises(SourcePlanError, match="source_steps_invalid"):
        resolve_source_steps([], table_snapshot())
    with pytest.raises(SourcePlanError, match="source_steps_invalid"):
        resolve_source_steps([derived_sheet()] * 9, table_snapshot())


def test_report_rejects_200_source_rows_plus_generated_total_before_effects():
    snapshot = table_snapshot()
    snapshot["observed"]["rows"] = [["项", 1, "原文"] for _ in range(200)]
    plan = [derived_sheet(), {"id": "s2", "action": "report_from_sheet",
                              "from": "s1", "title": "汇总"}]
    with pytest.raises(SourcePlanError, match="source_report_too_large"):
        resolve_source_steps(plan, snapshot)
