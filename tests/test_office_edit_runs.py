"""Versioned Word run edits: one changed text node, with exact package preservation."""
import json
import zipfile

from lxml import etree as ET
import pytest

from agent_service import office_edit as edit
from test_office_edit import word_fixture
from test_office_complex_table_edit import complex_fixture, write_parts


def _change_document(path, mutate):
    parts = edit._parts(path)
    tree = edit._xml(parts["word/document.xml"])
    mutate(tree)
    parts["word/document.xml"] = ET.tostring(tree)
    write_parts(path, parts)


def _prepared(path, tmp_path, change):
    root = tmp_path / "input"
    root.mkdir()
    source = edit.snapshot_edit_source(path, root)
    spec = {"kind": "document", "source_id": "selected-edit",
            "source_sha256": source["sha256"], "changes": [change]}
    return root, source, spec


def _edit_and_check(path, root, source, spec, tmp_path):
    original = path.read_bytes()
    assert edit._bind(spec, root)[1] == source
    output = tmp_path / "edited.docx"
    edit._make_copy(spec, path, output, source["observation_version"])
    observed = edit.verify_preservation(path, output, spec, source["observation_version"])
    assert path.read_bytes() == original
    assert edit.verify_edit_source(root) == source
    before = edit._parts(path)
    after = edit._parts(output)
    assert before.keys() == after.keys()
    assert all(before[name] == after[name] for name in before if name != "word/document.xml")
    assert edit._masked(before, spec) == edit._masked(after, spec)
    return output, observed


def test_body_mixed_format_changes_only_one_text_node(tmp_path):
    path = tmp_path / "source.docx"
    word_fixture(path, complex_target=True)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": "预算待确认复杂格式", "after": "预算已确认复杂格式"})
    assert source["observation_version"] == 2
    assert source["observed"]["paragraphs"][1]["editable"] is True
    output, observed = _edit_and_check(path, root, source, spec, tmp_path)
    assert observed["paragraphs"][1]["text"] == spec["changes"][0]["after"]
    before_p = edit._xml(edit._parts(path)["word/document.xml"]).findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
    after_p = edit._xml(edit._parts(output)["word/document.xml"]).findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
    before_runs = before_p.findall(f"{{{edit.W}}}r")
    after_runs = after_p.findall(f"{{{edit.W}}}r")
    assert ET.tostring(before_runs[1], method="c14n") == ET.tostring(after_runs[1], method="c14n")
    assert ET.tostring(before_runs[0].find(f"{{{edit.W}}}rPr"), method="c14n") == ET.tostring(
        after_runs[0].find(f"{{{edit.W}}}rPr"), method="c14n")


def test_two_text_nodes_in_one_run(tmp_path):
    path = tmp_path / "source.docx"
    word_fixture(path)
    def split(tree):
        paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
        run = paragraph.find(f"{{{edit.W}}}r")
        run.find(f"{{{edit.W}}}t").text = "预算"
        ET.SubElement(run, f"{{{edit.W}}}t").text = "待确认"
    _change_document(path, split)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": "预算待确认", "after": "预算已确认"})
    output, _ = _edit_and_check(path, root, source, spec, tmp_path)
    texts = edit._xml(edit._parts(output)["word/document.xml"]).findall(
        f".//{{{edit.W}}}body/{{{edit.W}}}p[2]/{{{edit.W}}}r/{{{edit.W}}}t")
    assert [node.text for node in texts] == ["预算", "已确认"]


def test_nested_and_merged_cell_runs_are_addressable(tmp_path):
    path = tmp_path / "source.docx"
    complex_fixture(path, complex_run=True)
    def split_merged(tree):
        table = tree.find(f".//{{{edit.W}}}body/{{{edit.W}}}tbl")
        paragraph = table.find(f"{{{edit.W}}}tr/{{{edit.W}}}tc/{{{edit.W}}}p")
        ET.SubElement(paragraph, f"{{{edit.W}}}r").append(ET.Element(f"{{{edit.W}}}t"))
        paragraph.findall(f"{{{edit.W}}}r")[-1].find(f"{{{edit.W}}}t").text = "尾"
    _change_document(path, split_merged)
    root, source, spec = _prepared(path, tmp_path, {
        "table_index": 1, "row_index": 0, "column_index": 0, "paragraph_index": 0,
        "before": "Nested复杂", "after": "Nest更新ed复杂"})
    assert edit._word_observed_item(source["observed"], spec["changes"][0])["editable"] is True
    output, _ = _edit_and_check(path, root, source, spec, tmp_path)
    assert edit._observe(output, "document", 2)["table_cells"][0]["text"] == "Horizontal尾"
    merged = {**spec["changes"][0], "table_index": 0, "before": "Horizontal尾",
              "after": "Horiz新ontal尾"}
    spec["changes"] = [merged]
    edit._bind(spec, root)


@pytest.mark.parametrize("before,after,code", [
    ("预算待确认复杂格式", "完整重写", "edit_word_complex"),
    ("预算待确认复杂格式", "预算已确认其他格式", "edit_word_complex"),
])
def test_cross_node_rewrite_rejected_before_output(tmp_path, before, after, code):
    path = tmp_path / "source.docx"
    word_fixture(path, complex_target=True)
    root, _, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": before, "after": after})
    with pytest.raises(edit.EditError, match=code):
        edit._bind(spec, root)
    assert not (tmp_path / "edited.docx").exists()


def test_ambiguous_placement_rejected(tmp_path):
    path = tmp_path / "source.docx"
    word_fixture(path)
    def duplicate(tree):
        paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
        first = paragraph.find(f"{{{edit.W}}}r/{{{edit.W}}}t")
        first.text = "A"
        second = ET.SubElement(paragraph, f"{{{edit.W}}}r")
        ET.SubElement(second, f"{{{edit.W}}}t").text = "A"
    _change_document(path, duplicate)
    root, _, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": "AA", "after": "AXA"})
    with pytest.raises(edit.EditError, match="edit_word_ambiguous"):
        edit._bind(spec, root)


def test_legacy_snapshot_retains_original_editable_projection(tmp_path, monkeypatch):
    path = tmp_path / "source.docx"
    word_fixture(path, complex_target=True)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 0, "before": "编辑验收", "after": "编辑已验收"})
    legacy = dict(source)
    legacy.pop("observation_version")
    legacy["observed"] = edit._observe(root / "source.docx", "document")
    record = json.loads((root / "source.json").read_text(encoding="utf-8"))
    record["public"] = legacy
    (root / "source.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert legacy["observed"]["paragraphs"][1]["editable"] is False
    assert edit.verify_edit_source(root) == legacy
    output_root = tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setattr(edit, "_native_readback", lambda *args: {"progid": "test-only", "changes": spec["changes"]})
    result = edit.execute_edit(spec, output_root, root)
    assert result["ok"] is True
    assert edit.verify_edit_result(result, root, spec) == result


def test_unapproved_run_change_fails_preservation(tmp_path):
    path = tmp_path / "source.docx"
    word_fixture(path, complex_target=True)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": "预算待确认复杂格式", "after": "预算已确认复杂格式"})
    output, _ = _edit_and_check(path, root, source, spec, tmp_path)
    parts = edit._parts(output)
    tree = edit._xml(parts["word/document.xml"])
    paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
    paragraph.findall(f"{{{edit.W}}}r")[1].find(f"{{{edit.W}}}t").text = "篡改"
    parts["word/document.xml"] = ET.tostring(tree)
    tampered = tmp_path / "tampered.docx"
    with zipfile.ZipFile(tampered, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    with pytest.raises(edit.EditError):
        edit.verify_preservation(path, tampered, spec, 2)


def test_sentinel_collision_cannot_shift_approved_node(tmp_path):
    marker = "__EDIT_VALUE__"
    path = tmp_path / "source.docx"
    word_fixture(path)
    def collision(tree):
        paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
        paragraph.find(f"{{{edit.W}}}r/{{{edit.W}}}t").text = marker
        second = ET.SubElement(paragraph, f"{{{edit.W}}}r")
        ET.SubElement(second, f"{{{edit.W}}}t").text = "a" + marker
    _change_document(path, collision)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": marker + "a" + marker, "after": marker * 3})
    output, _ = _edit_and_check(path, root, source, spec, tmp_path)
    parts = edit._parts(output)
    tree = edit._xml(parts["word/document.xml"])
    paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
    first, second = paragraph.findall(f"{{{edit.W}}}r/{{{edit.W}}}t")
    first.text, second.text = marker * 2, marker
    parts["word/document.xml"] = ET.tostring(tree)
    forged = tmp_path / "forged.docx"
    write_parts(forged, parts)
    assert edit._observe(forged, "document", 2)["paragraphs"][1]["text"] == spec["changes"][0]["after"]
    with pytest.raises(edit.EditError, match="edit_preservation_mismatch"):
        edit.verify_preservation(path, forged, spec, 2)


@pytest.mark.parametrize("original_space,after,expected_space,tampered_space", [
    (None, "预算已确认", None, "preserve"),
    ("preserve", "预算已确认", "preserve", None),
    (None, " 预算已确认 ", "preserve", None),
])
def test_xml_space_must_follow_deterministic_v2_rule(
        tmp_path, original_space, after, expected_space, tampered_space):
    path = tmp_path / "source.docx"
    word_fixture(path)
    if original_space is not None:
        def mark_space(tree):
            paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
            paragraph.find(f"{{{edit.W}}}r/{{{edit.W}}}t").set(edit._XML_SPACE, original_space)
        _change_document(path, mark_space)
    root, source, spec = _prepared(path, tmp_path, {
        "paragraph_index": 1, "before": "预算待确认", "after": after})
    output, _ = _edit_and_check(path, root, source, spec, tmp_path)
    parts = edit._parts(output)
    tree = edit._xml(parts["word/document.xml"])
    paragraph = tree.findall(f".//{{{edit.W}}}body/{{{edit.W}}}p")[1]
    node = paragraph.find(f"{{{edit.W}}}r/{{{edit.W}}}t")
    assert node.get(edit._XML_SPACE) == expected_space
    if tampered_space is None:
        node.attrib.pop(edit._XML_SPACE)
    else:
        node.set(edit._XML_SPACE, tampered_space)
    parts["word/document.xml"] = ET.tostring(tree)
    forged = tmp_path / "forged.docx"
    write_parts(forged, parts)
    with pytest.raises(edit.EditError, match="edit_preservation_mismatch"):
        edit.verify_preservation(path, forged, spec, 2)
