"""Local document-table recipe contracts; no model or native Office invocation."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import local_skills as skills


def candidate():
    return {"schema": 1, "id": "document-table-office-package", "version": 1,
            "name": "Word表格转Excel与报告", "recipe": "document_table_report",
            "sum_columns": [], "draft": False}


def snapshot():
    return {"source_id": "selected", "kind": "document", "sha256": "a" * 64,
            "display_name": "原始表格.docx", "observed": {"paragraphs": ["此段不是表格"],
            "table": {"headers": ["编号", "数量"],
                      "rows": [["001", "3.0"], ["=1+1", "5.0"], ["合计", "8.0"], [" 原文 ", ""]]}}}


def test_document_table_recipe_preserves_text_and_never_adds_a_sum_or_draft():
    source = snapshot()
    original = deepcopy(source)
    steps = skills.build_steps(candidate(), source)
    assert [step["action"] for step in steps] == ["spreadsheet", "document_from_sheet"]
    assert steps[0]["data"]["rows"] == source["observed"]["table"]["rows"]
    assert steps[0]["data"]["headers"] == source["observed"]["table"]["headers"]
    assert steps[0]["data"]["sum_columns"] == []
    assert steps[1]["from"] == "s1"
    assert "此段不是表格" not in steps[1]["paragraphs"]
    steps[0]["data"]["rows"][0][0] = "CHANGED"
    assert source == original


@pytest.mark.parametrize("columns", [[0], [1], [True], "", None])
def test_document_table_manifest_cannot_request_totals(columns):
    value = candidate()
    value["sum_columns"] = columns
    with pytest.raises(skills.SkillError):
        skills.validate_manifest(value)


@pytest.mark.parametrize("change", ["no_table", "table_kind", "invalid_table"])
def test_missing_or_malformed_table_cannot_be_replaced_with_invented_data(change):
    source = snapshot()
    if change == "no_table":
        del source["observed"]["table"]
    elif change == "table_kind":
        source["kind"] = "table"
        source["observed"] = source["observed"]["table"]
    else:
        source["observed"]["table"]["rows"] = [[1, "3.0"]]
    with pytest.raises(ValueError):
        skills.build_steps(candidate(), source)


def test_new_candidate_is_gated_after_offline_checks_and_review(tmp_path):
    value = candidate()
    store, base = tmp_path / "skills", tmp_path / "tasks"
    registered = skills.register_candidate(store, value)
    digest = registered["sha256"]
    assert registered["enabled"] is False
    checked = skills.verify_offline(store, value["id"], digest)
    assert checked["passed"] and checked["kind"] == "offline_contract"
    skills.record_review(store, value["id"], digest, "offline test", "Fixed text-only recipe reviewed")
    assert not skills.status(store, value["id"], base)["enabled"]
    with pytest.raises((ValueError, OSError)):
        skills.activate(store, value["id"], digest, base)


def test_example_manifest_matches_fixed_recipe_contract():
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root / "docs/examples/skills/document-table-office-package.json").read_text(encoding="utf-8"))
    assert value == candidate()
    assert skills.validate_manifest(value) == value
