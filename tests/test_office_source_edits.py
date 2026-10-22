"""Bounded source-table patches resolve before the ordinary Office workflow."""

from copy import deepcopy

import pytest

from agent_service.office_source_plan import (SourcePlanError, describe_source_edits,
                                              resolve_source_steps)


def source(rows=None):
    return {"source_id": "selected", "kind": "table", "sha256": "b" * 64,
            "display_name": "任务.csv", "bytes": 100, "coverage": {"scope": "all_rows"},
            "observed": {"headers": ["项目", "数量", "备注"],
                         "rows": rows if rows is not None else [["甲", 3, "原文"], ["乙", 2, "=KEEP()"]]}}


def sheet(**changes):
    plan = {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
            "title": "副本", "columns": [1, 0, 2], "sum_columns": [0]}
    plan.update(changes)
    return plan


def test_set_add_append_apply_before_reorder_without_mutating_source_or_plan():
    snapshot = source()
    plan = [sheet(edits=[{"row": 0, "column": 0, "op": "set", "value": "新甲"},
                         {"row": 1, "column": 1, "op": "add", "value": 4}],
                  append_rows=[["丙", 5, "新增"]])]
    original_snapshot = deepcopy(snapshot)
    original_plan = deepcopy(plan)
    steps = resolve_source_steps(plan, snapshot)
    assert steps[0]["data"]["headers"] == ["数量", "项目", "备注"]
    assert steps[0]["data"]["rows"] == [[3, "新甲", "原文"],
                                         [6, "乙", "=KEEP()"], [5, "丙", "新增"]]
    assert describe_source_edits(plan, snapshot) == [
        {"step": "s1", "row": 0, "column": 0, "before": "甲", "after": "新甲", "op": "set"},
        {"step": "s1", "row": 1, "column": 1, "before": 2, "after": 6, "op": "add"},
        {"step": "s1", "row": 2, "op": "append", "values": ["丙", 5, "新增"]},
    ]
    assert snapshot == original_snapshot and plan == original_plan


def test_missing_patch_fields_retains_original_behavior_and_empty_audit():
    snapshot = source()
    steps = resolve_source_steps([sheet()], snapshot)
    assert steps[0]["data"]["rows"] == [[3, "甲", "原文"], [2, "乙", "=KEEP()"]]
    assert describe_source_edits([sheet()], snapshot) == []


def test_add_uses_decimal_arithmetic_without_binary_float_drift():
    snapshot = source(rows=[["甲", 0.1, "原文"]])
    plan = [sheet(edits=[{"row": 0, "column": 1, "op": "add", "value": 0.2}])]
    assert resolve_source_steps(plan, snapshot)[0]["data"]["rows"][0][0] == 0.3
    assert describe_source_edits(plan, snapshot)[0]["after"] == 0.3


def test_set_can_replace_literal_text_and_then_validate_numeric_sum():
    snapshot = source(rows=[["甲", "待核", "=TEXT()"], ["乙", 2, "原文"]])
    plan = [sheet(edits=[{"row": 0, "column": 1, "op": "set", "value": 4},
                         {"row": 0, "column": 2, "op": "set", "value": "=1+1"}])]
    output = resolve_source_steps(plan, snapshot)[0]["data"]
    assert output["rows"] == [[4, "甲", "=1+1"], [2, "乙", "原文"]]
    assert snapshot["observed"]["rows"][0] == ["甲", "待核", "=TEXT()"]


@pytest.mark.parametrize("edits", [
    [{"row": 0, "column": 1, "op": "set", "value": 4},
     {"row": 0, "column": 1, "op": "add", "value": 1}],
    [{"row": -1, "column": 1, "op": "set", "value": 4}],
    [{"row": 2, "column": 1, "op": "set", "value": 4}],
    [{"row": True, "column": 1, "op": "set", "value": 4}],
    [{"row": 0, "column": 3, "op": "set", "value": 4}],
    [{"row": 0, "column": True, "op": "set", "value": 4}],
    [{"row": 0, "column": 1, "op": "multiply", "value": 4}],
    [{"row": 0, "column": 1, "op": ["set"], "value": 4}],
    [{"row": 0, "column": 1, "op": "set", "value": 4, "path": "C:/bad"}],
])
def test_rejects_duplicate_or_invalid_source_coordinates(edits):
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(edits=edits)], source())


@pytest.mark.parametrize("rows,edit", [
    ([["甲", True, "x"]], {"row": 0, "column": 1, "op": "add", "value": 1}),
    ([["甲", 3, "x"]], {"row": 0, "column": 1, "op": "add", "value": True}),
    ([["甲", "3", "x"]], {"row": 0, "column": 1, "op": "add", "value": 1}),
    ([["甲", 999_999_999_999_999, "x"]], {"row": 0, "column": 1, "op": "add", "value": 1}),
    ([["甲", 3, "x"]], {"row": 0, "column": 1, "op": "add", "value": float("inf")}),
])
def test_add_rejects_boolean_text_nonfinite_and_precision_overflow(rows, edit):
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(edits=[edit])], source(rows=rows))


@pytest.mark.parametrize("append_rows", [
    [["missing", 1]],
    [["too", 1, "many", "cells"]],
    [["bad", float("nan"), "cell"]],
    [["bad", 1, "\x00"]],
    [["x", 1, "okay"]] * 51,
])
def test_append_requires_complete_bounded_rows(append_rows):
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(append_rows=append_rows)], source())


def test_combined_row_limit_and_report_limit_reject_before_effects():
    snapshot = source(rows=[["项", 1, "原文"] for _ in range(200)])
    with pytest.raises(SourcePlanError, match="source_rows_too_many"):
        resolve_source_steps([sheet(append_rows=[["新", 2, "原文"]])], snapshot)
    snapshot = source(rows=[["项", 1, "原文"] for _ in range(199)])
    plan = [sheet(append_rows=[["新", 2, "原文"]]),
            {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "报告"}]
    with pytest.raises(SourcePlanError, match="source_report_too_large"):
        resolve_source_steps(plan, snapshot)


def test_edit_count_limit_and_unknown_step_field_rejected():
    too_many = [{"row": 0, "column": 1, "op": "set", "value": i} for i in range(51)]
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(edits=too_many)], source())
    with pytest.raises(SourcePlanError):
        resolve_source_steps([sheet(remove_rows=[0])], source())
