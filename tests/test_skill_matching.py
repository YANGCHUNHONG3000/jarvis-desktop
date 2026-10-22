"""Contracts for read-only installed-skill discovery and public effect previews."""

from copy import deepcopy
import json

import pytest

from agent_service import local_skills
from agent_service.skill_matching import (
    candidate_plan,
    discover_candidates,
    list_available_for_prompt,
)


def _manifest(skill_id="table-copy-demo", recipe="table_copy", *, draft=False, sums=None):
    return {"schema": 1, "id": skill_id, "version": 1, "name": "Copy rows",
            "recipe": recipe, "sum_columns": sums or [], "draft": draft}


def _snapshot(kind="table"):
    if kind == "document":
        observed = {"paragraphs": ["PRIVATE DOCUMENT TEXT"],
                    "table": {"headers": ["ID", "Value"],
                              "rows": [["001", "42"], ["Total", "42"]]}}
    else:
        observed = {"headers": ["ID", "Value"],
                    "rows": [["PRIVATE CELL", 42], ["001", 42]]}
    return {"source_id": "selected", "kind": kind, "sha256": "a" * 64,
            "display_name": "private-source.csv" if kind == "table" else "private-source.docx",
            "observed": observed}


def _activate_status(monkeypatch, active_digest, *, enabled=True, remaining=4):
    def status(_store, skill_id, _base=None):
        return {"active": active_digest, "previous": None, "remaining_runs": remaining,
                "inflight": None, "blocked": [], "enabled": enabled}
    monkeypatch.setattr(local_skills, "status", status)


def test_discovery_lists_source_compatible_active_skill_and_safe_full_effect_preview(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(store, _manifest())
    _activate_status(monkeypatch, saved["sha256"])

    result = discover_candidates(store, tmp_path / "tasks", _snapshot(), "please create a spreadsheet")

    assert result["schema"] == 1
    assert result["hints"]["match_semantics"] == "static_alias_or_substring_hint_only"
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert {key: candidate[key] for key in ("id", "sha256", "version", "recipe")} == {
        "id": "table-copy-demo", "sha256": saved["sha256"], "version": 1, "recipe": "table_copy"}
    assert candidate["implementation_sha256"] == local_skills.implementation_sha256()
    assert candidate["remaining_runs"] == 4
    assert candidate["status"] == "available"
    assert candidate["source_kind"] == "table"
    assert candidate["effects"] == [{"id": "s1", "action": "spreadsheet", "output": ".xlsx",
                                      "title": "Copy rows", "columns": 2, "rows": 2,
                                      "sum_columns": []}]
    rendered = json.dumps(result, ensure_ascii=False)
    for private in ("PRIVATE CELL", "private-source.csv", "PRIVATE DOCUMENT TEXT"):
        assert private not in rendered
    assert list_available_for_prompt(store, tmp_path / "tasks", _snapshot()) == [
        {key: candidate[key] for key in ("id", "sha256", "recipe", "name", "effects")}
    ]


def test_discovery_reports_source_requirement_and_distinguishes_incompatibility(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(store, _manifest())
    _activate_status(monkeypatch, saved["sha256"])

    no_source = discover_candidates(store, tmp_path / "tasks")["candidates"][0]
    wrong_source = discover_candidates(store, tmp_path / "tasks", _snapshot("document"))["candidates"][0]

    assert no_source["status"] == "incompatible"
    assert no_source["reason"] == "source_required"
    assert no_source["effects"] == []
    assert wrong_source["status"] == "incompatible"
    assert wrong_source["reason"] == "skill_source_kind_mismatch"
    assert list_available_for_prompt(store, tmp_path / "tasks", _snapshot("document")) == []


def test_disabled_active_candidate_never_enters_prompt_candidates(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(store, _manifest())
    _activate_status(monkeypatch, saved["sha256"], enabled=False, remaining=3)

    candidate = discover_candidates(store, tmp_path / "tasks", _snapshot())["candidates"][0]

    assert candidate["status"] == "disabled"
    assert candidate["reason"] == "verification_stale_or_invalid"
    assert candidate["effects"][0]["rows"] == 2
    assert list_available_for_prompt(store, tmp_path / "tasks", _snapshot()) == []


def test_discovery_is_deterministic_bounded_and_skips_invalid_families(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    a = local_skills.register_candidate(store, _manifest("table-copy-zed"))
    b = local_skills.register_candidate(store, _manifest("table-copy-ace"))
    (store / "Bad Skill").mkdir(parents=True)
    (store / "table-copy-broken").mkdir()
    _activate_status(monkeypatch, a["sha256"])

    first = discover_candidates(store, tmp_path / "tasks", _snapshot())
    second = discover_candidates(store, tmp_path / "tasks", _snapshot())

    assert first == second
    assert [item["id"] for item in first["candidates"]] == ["table-copy-ace", "table-copy-zed"]
    assert all(item["status"] == "disabled" for item in first["candidates"] if item["id"] == "table-copy-ace")


def test_active_version_precedes_newer_inactive_version_and_global_limit_is_50(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    active = local_skills.register_candidate(store, _manifest("table-copy-order", "table_copy"))
    newer = local_skills.register_candidate(store, {
        **_manifest("table-copy-order", "table_copy"), "version": 2, "name": "Newer but inactive"})
    def status(path, skill_id, base=None):
        if skill_id == "table-copy-order":
            return {"active": active["sha256"], "previous": None, "remaining_runs": 2,
                    "inflight": None, "blocked": [], "enabled": True}
        return {"active": "f" * 64, "previous": None, "remaining_runs": 1,
                "inflight": None, "blocked": [], "enabled": True}

    monkeypatch.setattr(local_skills, "status", status)
    for index in range(55):
        local_skills.register_candidate(store, _manifest(f"zz-copy-{index:02d}", "table_copy"))

    result = discover_candidates(store, tmp_path / "tasks", _snapshot())
    ordered = [item for item in result["candidates"] if item["id"] == "table-copy-order"]

    assert len(result["candidates"]) == 50
    assert ordered[0]["sha256"] == active["sha256"]
    assert ordered[0]["status"] == "available"
    assert ordered[1]["sha256"] == newer["sha256"]
    assert ordered[1]["status"] == "disabled"


def test_implementation_fingerprint_is_computed_once_per_discovery(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    first = local_skills.register_candidate(store, _manifest("table-copy-one"))
    local_skills.register_candidate(store, _manifest("table-copy-two"))
    _activate_status(monkeypatch, first["sha256"])
    original = local_skills.implementation_sha256
    calls = []

    def counted_fingerprint():
        calls.append(True)
        return original()

    monkeypatch.setattr(local_skills, "implementation_sha256", counted_fingerprint)

    discover_candidates(store, tmp_path / "tasks", _snapshot())

    assert len(calls) == 1


def test_candidate_plan_requires_valid_pinned_identity_and_returns_resolved_values(tmp_path):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(store, _manifest())

    plan = candidate_plan(store, "table-copy-demo", saved["sha256"], _snapshot())

    assert plan[0]["action"] == "spreadsheet"
    assert plan[0]["data"]["rows"] == [["PRIVATE CELL", 42], ["001", 42]]
    assert plan[0]["data"]["sum_columns"] == []
    with pytest.raises(ValueError, match="skill_identity_invalid"):
        candidate_plan(store, "../bad", saved["sha256"], _snapshot())
    with pytest.raises(ValueError, match="skill_candidate_missing"):
        candidate_plan(store, "table-copy-demo", "f" * 64, _snapshot())


def test_document_table_recipe_preview_exposes_report_without_source_cells(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(
        store, _manifest("doc-table-demo", "document_table_report"))
    _activate_status(monkeypatch, saved["sha256"])

    candidate = discover_candidates(store, tmp_path / "tasks", _snapshot("document"))["candidates"][0]

    assert candidate["status"] == "available"
    assert [(effect["id"], effect["output"]) for effect in candidate["effects"]] == [
        ("s1", ".xlsx"), ("s2", ".docx")]
    assert candidate["effects"][1]["from_step"] == "s1"
    assert candidate["effects"][1]["title"] == "Copy rows · 报告"
    assert candidate["effects"][1]["table_rows"] == 0
    encoded = json.dumps(candidate, ensure_ascii=False)
    assert "PRIVATE DOCUMENT TEXT" not in encoded
    assert '"001"' not in encoded


def test_document_numeric_preview_names_converted_source_and_summed_output_columns_without_cells(
        tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(
        store, _manifest("document-numeric-demo", "document_numeric_report", sums=[1]))
    _activate_status(monkeypatch, saved["sha256"])
    source = _snapshot("document")
    source["observed"]["table"]["rows"] = [["001", "42"], ["002", "3"]]

    candidate = discover_candidates(store, tmp_path / "tasks", source)["candidates"][0]

    assert candidate["status"] == "available"
    assert candidate["effects"][0]["numeric_source_columns"] == [1]
    assert candidate["effects"][0]["sum_columns"] == [1]
    assert candidate["effects"][0]["rows"] == 2
    assert [(effect["id"], effect["output"]) for effect in candidate["effects"]] == [
        ("s1", ".xlsx"), ("s2", ".docx")]
    encoded = json.dumps(candidate, ensure_ascii=False)
    assert '"001"' not in encoded and '"42"' not in encoded


def test_document_copy_preview_uses_real_title_and_table_row_count(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(
        store, _manifest("document-copy-demo", "document_copy"))
    _activate_status(monkeypatch, saved["sha256"])

    candidate = discover_candidates(store, tmp_path / "tasks", _snapshot("document"))["candidates"][0]

    assert candidate["status"] == "available"
    assert candidate["effects"] == [{"id": "s1", "action": "document", "output": ".docx",
                                      "title": "Copy rows", "from_step": None,
                                      "paragraphs": 1, "table_rows": 2}]


def test_discovery_has_no_side_effect_on_candidate_store(tmp_path, monkeypatch):
    store = tmp_path / "skills"
    saved = local_skills.register_candidate(store, _manifest())
    _activate_status(monkeypatch, saved["sha256"])
    before = {path.relative_to(store).as_posix(): path.read_bytes()
              for path in store.rglob("*") if path.is_file()}

    discover_candidates(store, tmp_path / "tasks", _snapshot(), "table")

    after = {path.relative_to(store).as_posix(): path.read_bytes()
             for path in store.rglob("*") if path.is_file()}
    assert after == before
