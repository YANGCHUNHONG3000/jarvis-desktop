"""Offline contract tests for the static desktop capability catalog."""

import builtins

import pytest

from agent_service.capabilities import capability_gap, get_capability, list_capabilities


def test_catalog_has_unique_ids_and_explicit_scope_evidence_and_paths():
    entries = list_capabilities()
    ids = [item["id"] for item in entries]
    assert entries
    assert len(ids) == len(set(ids))
    for item in entries:
        assert item["scope"].strip()
        assert item["evidence_status"] in {
            "offline_tests_only", "offline_and_live_target",
            "offline_and_synthetic_live_target",
            "offline_with_user_reported_live",
            "offline_pending_live",
        }
        assert item["offline_tests"]
        assert item["implementation"]
        assert all(path.endswith(".py") for path in item["implementation"])
        assert "enabled" not in item
        assert "execute" not in item


def test_first_source_skill_actions_have_matching_adapters_and_live_evidence():
    table = get_capability("office.source.table_to_sheet")
    report = get_capability("office.source.sheet_report")
    assert table["availability"] == "implemented"
    assert report["availability"] == "implemented"
    assert "source_office.spreadsheet_from_source schema" in table["inputs"]
    assert "source_office.report_from_sheet schema" in report["inputs"]
    assert "原来源只读" in table["scope"]
    assert "不从模型计划接受编造的值" in report["description"]
    assert table["evidence_status"] == report["evidence_status"] == "offline_and_live_target"


def test_mailbox_and_synthetic_mail_export_evidence_are_distinguished():
    mailbox = get_capability("mail.qq.read_only")
    export = get_capability("mail.qq.export_selected_to_word")
    assert mailbox["availability"] == "implemented_auth_required"
    assert mailbox["evidence_status"] == "offline_with_user_reported_live"
    assert "confirmed by user" in mailbox["live_evidence"][0]
    assert "附件编码字节" in mailbox["scope"]
    assert get_capability("mail.gmail.read_only")["evidence_status"] == "offline_tests_only"
    assert export["evidence_status"] == "offline_and_synthetic_live_target"
    assert any("mail-office-live-20261017" in ref for ref in export["live_evidence"])
    assert "不接收邮箱凭据" in export["scope"]


def test_verified_artifact_handoff_and_local_recipe_catalog_match_live_evidence():
    artifact = get_capability("office.artifact.use_as_source")
    recipe = get_capability("skills.local_office_recipes")
    evidence_ref = "validation/artifacts/artifact-skills-live-20261017/skills-live.json"

    assert artifact["availability"] == recipe["availability"] == "implemented"
    assert artifact["kind"] == recipe["kind"] == "service"
    assert "receipt SHA-256" in artifact["inputs"][0]
    assert "不接受任意路径" in artifact["scope"]
    assert "agent_service/artifact_source.py" in artifact["implementation"]
    assert artifact["live_evidence"] == [evidence_ref]

    assert all(name in recipe["scope"] for name in
               ("table_report", "document_copy", "document_table_report"))
    assert "最多运行 10 次" in recipe["scope"]
    assert "动态查询" in recipe["scope"]
    assert "历史实现哈希" in recipe["scope"]
    assert "agent_service/local_skills.py" in recipe["implementation"]
    assert "run_jarvis_skills.py" in recipe["implementation"]
    assert recipe["evidence_status"] == "offline_and_live_target"
    live_ref = "validation/artifacts/document-numeric-live-20261017/gates.json"
    assert recipe["live_evidence"] == [live_ref]
    assert recipe["historical_live_evidence"] == [evidence_ref,
        "validation/artifacts/document-table-live-20261017/recipes-live.json",
        "validation/artifacts/skill-matching-live-20261017/recipes-final.json",
        "validation/artifacts/skill-resume-live-20261017/gates.json"]
    assert "table_copy" in recipe["scope"]
    assert "enabled" not in recipe  # Activation state comes from local status(), not this static catalog.
    assert any("不支持选列、改值或追加行" in limit for limit in recipe["limits"])
    assert any("保留所有原列" in item and "不支持选列" in item for item in recipe["inputs"])
    assert any("保留全部原行列" in limit and "不支持选列" in limit for limit in recipe["limits"])


def test_docx_table_to_sheet_is_live_catalogued_and_preserves_literal_table_contract():
    capability = get_capability("office.document_table.extract_to_sheet")

    assert capability["kind"] == "adapter"
    assert capability["availability"] == "implemented"
    assert capability["evidence_status"] == "offline_and_live_target"
    assert capability["live_evidence"] == [
        "validation/artifacts/document-table-live-20261017/recipes-live.json"]
    assert any("用户选择的 Word 文档" in value for value in capability["inputs"])
    assert any("选择并调整列顺序" in value for value in capability["inputs"])
    assert not any("schema" in value or "source_office." in value for value in capability["inputs"])
    assert any("Excel 工作簿" in value for value in capability["outputs"])
    assert any("Word 报告" in value for value in capability["outputs"])
    assert "正文段落和原版式不导出" in capability["scope"]
    assert "禁止改值、删行、数值转换、公式及重新求和" in capability["scope"]
    assert any("最多 12 列、200 行" in limit for limit in capability["limits"])
    assert any("空白内容和现有合计行" in limit for limit in capability["limits"])
    assert any("不转换数值、不重新计算合计" in limit for limit in capability["limits"])
    assert not any("sum_columns" in limit for limit in capability["limits"])
    assert "agent_service/office_source_plan.py" in capability["implementation"]


def test_docx_numeric_adapter_is_distinct_from_literal_handoff_and_has_real_live_evidence():
    numeric = get_capability("office.document_table.numeric_sum_to_report")
    literal = get_capability("office.document_table.extract_to_sheet")
    assert numeric["availability"] == "implemented"
    assert numeric["evidence_status"] == "offline_and_live_target"
    assert numeric["live_evidence"] == ["validation/artifacts/document-numeric-live-20261017/gates.json"]
    assert "严格数字文本" in numeric["description"]
    assert "已有合计行" in numeric["scope"]
    assert "禁止改值、删行、数值转换" in literal["scope"]
    assert [item["id"] for item in list_capabilities("请帮我Word表格第二列求和")] == [numeric["id"]]
    assert capability_gap("请帮我Word表格数值求和")["matched_ids"] == [numeric["id"]]
    assert "document_numeric_report" in get_capability("skills.local_office_recipes")["scope"]


def test_search_is_bounded_copy_safe_and_does_not_import_adapters(monkeypatch):
    def forbidden_import(*_args, **_kwargs):
        raise AssertionError("catalog queries must not import or execute adapters")

    monkeypatch.setattr(builtins, "__import__", forbidden_import)
    results = list_capabilities("source")
    assert results
    results[0]["scope"] = "mutated by caller"
    assert list_capabilities("source")[0]["scope"] != "mutated by caller"
    assert get_capability("office.spreadsheet.create")["id"] in {
        item["id"] for item in list_capabilities("200 data rows")
    }
    assert [item["id"] for item in list_capabilities("请帮我Word表格转Excel，再保存")] == [
        "office.document_table.extract_to_sheet"]


@pytest.mark.parametrize("query", [
    "请帮我Word表格转Excel，再保存",
    "请帮我 ＷＯＲＤ 表格转 ＥＸＣＥＬ 再保存",
    "请帮我把 Word 里的表格转到 Excel，然后生成报告",
    "docx 转 xlsx",
])
def test_specific_office_phrase_aliases_find_only_document_table_capability(query):
    results = list_capabilities(query)
    assert [item["id"] for item in results] == ["office.document_table.extract_to_sheet"]
    gap = capability_gap(query)
    assert gap["matched_ids"] == ["office.document_table.extract_to_sheet"]
    assert gap["gap"] is False
    assert "ok" not in gap and "execute" not in gap


@pytest.mark.parametrize("query", [
    "请帮我Word表格转Excell，再保存",
    "请帮我把照片转成Excel",
])
def test_misspelled_or_unrelated_full_queries_do_not_alias_match(query):
    assert list_capabilities(query) == []


@pytest.mark.parametrize(("query", "expected_id"), [
    ("Word", "office.document.create"),
    ("Excel", "office.spreadsheet.create"),
    ("Office", "office.workflow.resume_verified_prefix"),
])
def test_generic_office_searches_keep_existing_substring_behavior(query, expected_id):
    results = list_capabilities(query)
    assert results
    assert expected_id in {item["id"] for item in results}


@pytest.mark.parametrize("query", ["", "  ", "a\nb", "x" * 201, None])
def test_gap_query_rejects_empty_or_unbounded_values(query):
    with pytest.raises(ValueError, match="capability_query_invalid"):
        capability_gap(query)


def test_gap_is_metadata_only_and_reports_proposed_order():
    known = capability_gap("table_to_sheet")
    assert known["gap"] is False
    assert known["matched_ids"] == ["office.source.table_to_sheet"]
    assert known["acquisition_steps"] == []

    missing = capability_gap("control arbitrary smart appliance")
    assert missing["gap"] is True
    assert missing["matched_ids"] == []
    assert missing["acquisition_steps"] == [
        "check_applicable_verified_local_skill",
        "check_supported_official_adapter",
        "review_mature_library_and_license",
        "research_and_propose_new_adapter_or_REA_path",
    ]


@pytest.mark.parametrize("limit", [True, 0, -1, 51, 1.0])
def test_limit_is_strict_integer_in_range(limit):
    with pytest.raises(ValueError, match="capability_limit_invalid"):
        list_capabilities(limit=limit)


def test_get_returns_none_for_unknown_id_and_rejects_malformed_id():
    assert get_capability("office.not.real") is None
    with pytest.raises(ValueError, match="capability_id_invalid"):
        get_capability("../office.document.create")

