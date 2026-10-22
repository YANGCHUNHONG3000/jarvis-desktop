"""Procurement output has explicit market/source and editable review status."""
from agent_service.page_office_delivery import _steps


def source(site="ikea_us"):
    body = "MICKE Desk, white 41 3/8x19 5/8 $109.99 Article Number 802.130.74"
    return {"metadata":{"config":{"site":site,"query":"desk"},
        "url":"https://www.ikea.com/us/en/p/micke-desk-white-80213074/",
        "title":"MICKE desk, white - IKEA","body_chars":len(body)}, "text":body}


def test_procurement_outputs_keep_exact_source_and_review_status():
    data = source()
    sheet, word = [step["data"] for step in _steps(data)]
    assert sheet["title"] == "办公采购候选台账"
    assert word["title"] == "办公采购候选资料"
    assert ["采购核对状态","","","待人工核对"] in sheet["rows"]
    assert "采购核对状态：待人工核对" in word["paragraphs"]
    assert any("IKEA 美国站" in p and "本任务未下单" in p for p in word["paragraphs"])
    extracts = [r for r in sheet["rows"] if r[0] == "原文摘录"]
    assert extracts and all(data["text"][r[1]:r[2]] == r[3] for r in extracts)
    assert data["text"] in word["paragraphs"]


def test_other_profiles_retain_original_generic_output():
    sheet, word = [step["data"] for step in _steps(source("django_docs"))]
    assert sheet["title"] == "网页来源与分段摘要" and word["title"] == "网页阅读资料"
    assert all(r[0] != "采购核对状态" for r in sheet["rows"])
    assert not any("采购核对状态" in p for p in word["paragraphs"])


def test_labels_do_not_invent_a_price_or_available_stock():
    data = source()
    data["text"] = "MICKE Desk. Price unavailable. Delivery requires a postal code."
    data["metadata"]["body_chars"] = len(data["text"])
    sheet, word = [step["data"] for step in _steps(data)]
    assert data["text"] in word["paragraphs"]
    assert not any("$" in str(row) for row in sheet["rows"])
