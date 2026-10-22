"""Fixed declarative numeric recipe is inert until publisher gates are met."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import local_skills as skills


def manifest():
    return {"schema": 1, "id": "document-numeric-report", "version": 1,
            "name": "Word表格第二列转数值求和并生成报告",
            "recipe": "document_numeric_report", "sum_columns": [1], "draft": False}


def snapshot():
    return {"source_id": "selected", "kind": "document", "sha256": "b" * 64,
            "display_name": "来源.docx", "observed": {"paragraphs": ["正文"],
            "table": {"headers": ["编号", "金额"], "rows": [["001", "3.0"], ["=1+1", "5.0"]]}}}


def test_fixed_recipe_uses_all_columns_and_numeric_sum_only():
    source = snapshot()
    original = deepcopy(source)
    steps = skills.build_steps(manifest(), source)
    assert [step["action"] for step in steps] == ["spreadsheet", "document_from_sheet"]
    assert steps[0]["data"]["headers"] == ["编号", "金额"]
    assert steps[0]["data"]["rows"] == [["001", 3.0], ["=1+1", 5.0]]
    assert steps[0]["data"]["sum_columns"] == [1]
    assert "重新求和" in steps[1]["paragraphs"][-1]
    assert source == original


@pytest.mark.parametrize("bad", [[], [0], [True], [1, 1], [12], "1", None])
def test_manifest_rejects_unsafe_sum_columns(bad):
    value = manifest()
    value["sum_columns"] = bad
    with pytest.raises(skills.SkillError):
        skills.validate_manifest(value)


def test_wrong_source_and_existing_total_rejected():
    candidate = manifest()
    no_table = snapshot()
    del no_table["observed"]["table"]
    with pytest.raises(ValueError):
        skills.build_steps(candidate, no_table)
    old_total = snapshot()
    old_total["observed"]["table"]["rows"].append(["合计", "8.0"])
    with pytest.raises(ValueError):
        skills.build_steps(candidate, old_total)


def test_offline_contract_does_not_enable_without_review_and_live(tmp_path):
    store = tmp_path / "skills"
    base = tmp_path / "tasks"
    candidate = manifest()
    digest = skills.register_candidate(store, candidate)["sha256"]
    checked = skills.verify_offline(store, candidate["id"], digest)
    assert checked["kind"] == "offline_contract" and checked["passed"]
    assert not skills.status(store, candidate["id"], base)["enabled"]
    skills.record_review(store, candidate["id"], digest, "Offline reviewer", "Exact numeric action")
    assert not skills.status(store, candidate["id"], base)["enabled"]
    with pytest.raises((ValueError, OSError)):
        skills.activate(store, candidate["id"], digest, base)


def test_example_manifest_matches_bounded_recipe():
    path = Path(__file__).resolve().parents[1] / "docs" / "examples" / "skills" / "document-numeric-report.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == manifest()
    assert skills.validate_manifest(saved) == saved
