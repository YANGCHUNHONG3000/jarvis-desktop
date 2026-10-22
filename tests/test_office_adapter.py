"""Offline contracts for the bounded Office adapter; no COM launch."""

from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest

from agent_service.office import (
    OfficeError, _column_widths, _compare, _expected, _sheet_observation, _word_app_observation,
    _write_word, validate_spec, verify_office_file,
)


def test_document_spec_is_copied_and_bounded():
    raw = {"kind": "document", "title": "中文报告", "paragraphs": ["第一段"],
           "table": {"headers": ["项目", "数值"], "rows": [["收入", 12]]}}
    spec = validate_spec(raw)
    raw["table"]["rows"][0][0] = "changed"
    assert spec["table"]["rows"][0][0] == "收入"
    assert spec["title"] == "中文报告"


@pytest.mark.parametrize("spec", [
    {"kind": "document", "title": "x", "paragraphs": [], "path": "C:/outside.docx"},
    {"kind": "document", "title": "x\nrun", "paragraphs": []},
    {"kind": "document", "title": "x", "paragraphs": ["a\x00b"]},
    {"kind": "document", "title": "x", "paragraphs": ["a"] * 41},
    {"kind": "document", "title": "x", "paragraphs": [],
     "table": {"headers": ["a", "b"], "rows": [[1]]}},
    {"kind": "spreadsheet", "title": "x", "headers": ["n"], "rows": [["=1+1"]],
     "sum_columns": [0]},
    {"kind": "spreadsheet", "title": "x", "headers": ["n"], "rows": [[True]],
     "sum_columns": [0]},
    {"kind": "spreadsheet", "title": "x", "headers": ["n"], "rows": [[float("inf")]]},
    {"kind": "spreadsheet", "title": "x", "headers": ["n"], "rows": [[2]],
     "sum_columns": [True]},
    {"kind": "spreadsheet", "title": "x", "headers": ["n"], "rows": [[2]],
     "sum_columns": [0, 0]},
])
def test_rejects_unsafe_or_ambiguous_specs(spec):
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_preserves_formula_like_input_as_string():
    spec = validate_spec({"kind": "spreadsheet", "title": "Sheet",
                          "headers": ["value"], "rows": [["=HYPERLINK(\"x\")"]]})
    assert spec["rows"][0][0].startswith("=")


def test_column_widths_are_bounded_and_total_label_avoids_formula_cell():
    spec = validate_spec({"kind": "spreadsheet", "title": "月度", "headers": ["来源", "金额"],
                          "rows": [["https://example.org/" + "x" * 100, 12]],
                          "sum_columns": [1]})
    assert _column_widths(spec) == [60, 12]
    assert _expected(spec)["total_label"] == "合计"
    all_summed = validate_spec({"kind": "spreadsheet", "title": "x", "headers": ["n"],
                                "rows": [[2]], "sum_columns": [0]})
    assert "total_label" not in _expected(all_summed)


def _package(path: Path, parts: dict[str, str]):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in parts.items():
            z.writestr(name, content)


def test_word_ooxml_readback_checks_text_and_table(tmp_path):
    spec = validate_spec({"kind": "document", "title": "季度报告", "paragraphs": ["收入增长"],
                          "table": {"headers": ["名称", "金额"], "rows": [["甲", 10]]}})
    path = tmp_path / "report.docx"
    _package(path, {"word/document.xml": """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>季度报告</w:t></w:r></w:p><w:p><w:r><w:t>收入增长</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>名称</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"""})
    assert verify_office_file(path, spec)["paragraphs"] == ["收入增长"]
    wrong = dict(spec, paragraphs=["错误"])
    with pytest.raises(OfficeError, match="word_file_text_mismatch"):
        verify_office_file(path, wrong)


def test_xlsx_ooxml_checks_cached_total_and_formula(tmp_path):
    spec = validate_spec({"kind": "spreadsheet", "title": "月度", "headers": ["项", "金额"],
                          "rows": [["销售", 12], ["成本", 3]], "sum_columns": [1]})
    path = tmp_path / "ledger.xlsx"
    workbook = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>'
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    sheet = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>月度</t></is></c></row>
    <row r="2"><c r="A2" t="inlineStr"><is><t>项</t></is></c><c r="B2" t="inlineStr"><is><t>金额</t></is></c></row>
    <row r="3"><c r="A3" t="inlineStr"><is><t>销售</t></is></c><c r="B3"><v>12</v></c></row>
    <row r="4"><c r="A4" t="inlineStr"><is><t>成本</t></is></c><c r="B4"><v>3</v></c></row>
    <row r="5"><c r="A5" t="inlineStr"><is><t>合计</t></is></c><c r="B5"><f>SUM(B3:B4)</f><v>15</v></c></row>
    </sheetData></worksheet>'''
    _package(path, {"xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": rels,
                    "xl/worksheets/sheet1.xml": sheet})
    assert verify_office_file(path, spec)["totals"] == {"1": 15.0}
    assert verify_office_file(path, spec)["total_label"] == "合计"
    wrong = dict(spec, rows=[["销售", 12], ["成本", 4]])
    with pytest.raises(OfficeError, match="spreadsheet_file_value_mismatch"):
        verify_office_file(path, wrong)
    injected_sheet = sheet.replace('<c r="A3" t="inlineStr"><is><t>销售</t></is></c>',
                                   '<c r="A3" t="str"><f>HYPERLINK("x")</f><v>销售</v></c>')
    _package(path, {"xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": rels,
                    "xl/worksheets/sheet1.xml": injected_sheet})
    with pytest.raises(OfficeError, match="unexpected_spreadsheet_formula"):
        verify_office_file(path, spec)


def test_zip_traversal_is_rejected(tmp_path):
    path = tmp_path / "bad.docx"
    _package(path, {"word/document.xml": "<empty/>", "../other.txt": "bad"})
    with pytest.raises(OfficeError, match="invalid_office_package"):
        verify_office_file(path, {"kind": "document", "title": "x", "paragraphs": []})


def _spreadsheet_observation(tmp_path, backend, spec, cells):
    if backend == "native":
        return _sheet_observation(spec, lambda row, col: cells.get((row, col)))
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = ET.Element("worksheet", xmlns=namespace)
    data = ET.SubElement(sheet, "sheetData")
    total_row = len(spec["rows"]) + 3
    for (row, col), value in cells.items():
        cell = ET.SubElement(data, "c", r=f"{chr(65 + col)}{row}")
        if row == total_row and col in spec["sum_columns"]:
            ET.SubElement(cell, "f").text = f"SUM({chr(65 + col)}3:{chr(65 + col)}{total_row - 1})"
        if type(value) is str:
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, "is"), "t").text = value
        elif type(value) is bool:
            cell.set("t", "b")
            ET.SubElement(cell, "v").text = str(int(value))
        elif value is not None:
            ET.SubElement(cell, "v").text = str(value)
    path = tmp_path / "storage.xlsx"
    _package(path, {
        "xl/workbook.xml": f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": ET.tostring(sheet, encoding="unicode"),
    })
    return verify_office_file(path, spec)


@pytest.mark.parametrize("backend", ["native", "ooxml"])
@pytest.mark.parametrize("expected,actual", [
    ("", None), ("", ""), (None, None), (None, ""),
    (" ", " "), (0, 0), (False, False), ("0012", "0012"), ("=1+1", "=1+1"),
])
def test_spreadsheet_data_empty_storage_compatibility(tmp_path, backend, expected, actual):
    spec = validate_spec({"kind": "spreadsheet", "title": "T", "headers": ["H"],
                          "rows": [[expected]]})
    observed = _spreadsheet_observation(tmp_path, backend, spec,
                                        {(1, 0): "T", (2, 0): "H", (3, 0): actual})
    assert _compare(observed, _expected(spec))
    value = observed["rows"][0][0]
    if actual is None and expected == "":
        assert type(value) is str and value == ""
    elif type(actual) is bool:
        assert type(value) is bool and value is actual
    elif actual is None:
        assert value is None
    else:
        assert value == actual and type(value) is not bool
    assert spec["rows"][0][0] is expected
    # Storage normalization does not change the general scalar contract.
    assert not _compare(None, "")


@pytest.mark.parametrize("backend", ["native", "ooxml"])
@pytest.mark.parametrize("expected,actual", [
    ("", " "), ("", 0), ("", False), (" ", None), (" ", ""),
    ("value", None), ("0012", None), (0, None), (False, None), (False, 0),
])
def test_spreadsheet_data_storage_rejects_nonempty_or_typed_changes(tmp_path, backend, expected, actual):
    spec = validate_spec({"kind": "spreadsheet", "title": "T", "headers": ["H"],
                          "rows": [[expected]]})
    cells = {(1, 0): "T", (2, 0): "H", (3, 0): actual}
    if backend == "ooxml":
        with pytest.raises(OfficeError, match="spreadsheet_file_value_mismatch"):
            _spreadsheet_observation(tmp_path, backend, spec, cells)
    else:
        observed = _spreadsheet_observation(tmp_path, backend, spec, cells)
        assert observed["rows"][0][0] is actual
        assert not _compare(observed, _expected(spec))


@pytest.mark.parametrize("backend", ["native", "ooxml"])
@pytest.mark.parametrize("address", [(1, 0), (2, 0), (4, 0), (4, 1)])
def test_spreadsheet_empty_storage_does_not_change_metadata(tmp_path, backend, address):
    spec = validate_spec({"kind": "spreadsheet", "title": "T", "headers": ["H", "N"],
                          "rows": [["", 0]], "sum_columns": [1]})
    cells = {(1, 0): "T", (2, 0): "H", (2, 1): "N", (3, 0): None,
             (3, 1): 0, (4, 0): "合计", (4, 1): 0}
    cells[address] = None
    if backend == "ooxml":
        with pytest.raises(OfficeError, match="spreadsheet_file_value_mismatch"):
            _spreadsheet_observation(tmp_path, backend, spec, cells)
    else:
        observed = _spreadsheet_observation(tmp_path, backend, spec, cells)
        assert observed["rows"] == [["", 0]]
        assert not _compare(observed, _expected(spec))


def _native_table_text(rows):
    return "".join("".join(cell + "\r\x07" for cell in row) + "\r\x07" for row in rows)


def _fake_word_readback(body, rows=None, *, full=None, tables=None, row_count=None,
                        column_count=None):
    if rows is None:
        return SimpleNamespace(
            Content=SimpleNamespace(Text=body),
            Tables=SimpleNamespace(Count=0 if tables is None else tables),
        )
    table_text = _native_table_text(rows)
    table = SimpleNamespace(
        Range=SimpleNamespace(Start=42, Text=table_text),
        Rows=SimpleNamespace(Count=len(rows) if row_count is None else row_count),
        Columns=SimpleNamespace(Count=len(rows[0]) if column_count is None else column_count),
    )
    return SimpleNamespace(
        Content=SimpleNamespace(Text=full if full is not None else body + table_text + "\r"),
        Range=lambda start, end: SimpleNamespace(Text=body),
        Tables=SimpleNamespace(Count=1 if tables is None else tables,
                               Item=lambda index: table),
    )


def test_word_native_batch_readback_parses_actual_literal_cells():
    spec = validate_spec({"kind": "document", "title": "报告😀", "paragraphs": ["完整正文", ""],
                          "table": {"headers": ["编号", "值"],
                                    "rows": [["001", None], [True, 2.5], ["🧪", False]]}})
    body = "报告😀\r完整正文\r\r"
    rows = [["编号", "值"], ["001", ""], ["True", "2.5"], ["🧪", "False"]]
    observed = _word_app_observation(_fake_word_readback(body, rows), spec)
    assert observed == {"title": "报告😀", "paragraphs": ["完整正文", ""],
                        "table": {"headers": ["编号", "值"], "rows": rows[1:]}}
    changed = [row[:] for row in rows]
    changed[1][0] = "002"
    actual = _word_app_observation(_fake_word_readback(body, changed), spec)
    assert actual["table"]["rows"][0][0] == "002"
    expected = _expected(spec)
    expected["table"]["rows"] = [["" if value is None else str(value) for value in row]
                                   for row in expected["table"]["rows"]]
    assert not _compare(actual, expected)


def test_word_native_batch_readback_without_table_rejects_extra_text():
    spec = validate_spec({"kind": "document", "title": "标题", "paragraphs": ["正文"]})
    assert _word_app_observation(_fake_word_readback("标题\r正文\r\r"), spec) == {
        "title": "标题", "paragraphs": ["正文"]}
    for text in ("标题\r正文\r", "标题\r正文\r\r\r", "标题\r正文\r额外\r",
                 "标题\r正文", "标题\r正文\rX"):
        with pytest.raises(OfficeError, match="application_readback_mismatch"):
            _word_app_observation(_fake_word_readback(text), spec)
    with pytest.raises(OfficeError, match="application_readback_mismatch"):
        _word_app_observation(_fake_word_readback("标题\r正文\r\r", tables=1), spec)


def test_word_native_batch_readback_preserves_user_final_blank_paragraph():
    spec = validate_spec({"kind": "document", "title": "标题🙂",
                          "paragraphs": ["", "保留 空格", "多语言 cafe\u0301 中文🙂", ""]})
    expected = "\r".join([spec["title"], *spec["paragraphs"]]) + "\r"
    observed = {"title": spec["title"], "paragraphs": spec["paragraphs"]}
    # The preserved real WPS plain-case readback added exactly one empty tail.
    assert _word_app_observation(_fake_word_readback(expected + "\r"), spec) == observed
    for wrong in (expected, expected[:-1], expected + "\r\r", expected + "X\r",
                  expected.replace("中文🙂\r\r", "中文🙂\r", 1)):
        with pytest.raises(OfficeError, match="application_readback_mismatch"):
            _word_app_observation(_fake_word_readback(wrong), spec)


def test_word_native_batch_readback_title_only_needs_one_empty_tail():
    spec = validate_spec({"kind": "document", "title": "标题🙂", "paragraphs": []})
    assert _word_app_observation(_fake_word_readback("标题🙂\r\r"), spec) == {
        "title": "标题🙂", "paragraphs": []}
    with pytest.raises(OfficeError, match="application_readback_mismatch"):
        _word_app_observation(_fake_word_readback("标题🙂\r"), spec)


@pytest.mark.parametrize("mutation", [
    "body_extra", "missing_cell", "extra_cell", "missing_row_marker",
    "extra_row_marker", "wrong_row_marker", "extra_tail", "missing_tail",
    "table_count", "table_missing", "row_count", "column_count",
])
def test_word_native_batch_readback_rejects_structure_changes(mutation):
    spec = validate_spec({"kind": "document", "title": "T", "paragraphs": ["P"],
                          "table": {"headers": ["A", "B"], "rows": [["x", "y"]]}})
    body = "T\rP\r"
    rows = [["A", "B"], ["x", "y"]]
    kwargs = {}
    if mutation == "body_extra":
        body = "T\rP\rQ\r"
    elif mutation == "missing_cell":
        rows[1].pop()
    elif mutation == "extra_cell":
        rows[1].append("z")
    elif mutation == "missing_row_marker":
        doc = _fake_word_readback(body, rows)
        doc.Tables.Item(1).Range.Text = doc.Tables.Item(1).Range.Text.replace(
            "B\r\x07\r\x07", "B\r\x07", 1)
        doc.Content.Text = body + doc.Tables.Item(1).Range.Text + "\r"
    elif mutation == "extra_row_marker":
        doc = _fake_word_readback(body, rows)
        doc.Tables.Item(1).Range.Text += "\r\x07"
        doc.Content.Text = body + doc.Tables.Item(1).Range.Text + "\r"
    elif mutation == "wrong_row_marker":
        doc = _fake_word_readback(body, rows)
        doc.Tables.Item(1).Range.Text = doc.Tables.Item(1).Range.Text.replace(
            "B\r\x07\r\x07", "B\r\x07\r", 1)
        doc.Content.Text = body + doc.Tables.Item(1).Range.Text + "\r"
    elif mutation == "extra_tail":
        kwargs["full"] = body + _native_table_text(rows) + "\rX\r"
    elif mutation == "missing_tail":
        kwargs["full"] = body + _native_table_text(rows)
    elif mutation == "table_count":
        kwargs["tables"] = 2
    elif mutation == "table_missing":
        kwargs["tables"] = 0
    elif mutation == "row_count":
        kwargs["row_count"] = 3
    elif mutation == "column_count":
        kwargs["column_count"] = 3
    if mutation not in {"missing_row_marker", "extra_row_marker", "wrong_row_marker"}:
        doc = _fake_word_readback(body, rows, **kwargs)
    with pytest.raises(OfficeError, match="application_readback_mismatch"):
        _word_app_observation(doc, spec)


class _FakeWordWriter:
    def __init__(self):
        self.body = ""
        self.inserted = ""
        self.ranges = []
        self.conversion = None
        self.body_font = SimpleNamespace(Size=None)
        self.body_para = SimpleNamespace(SpaceAfter=None)
        self.title_range = SimpleNamespace(Font=SimpleNamespace(Size=None, Bold=None),
                                           ParagraphFormat=SimpleNamespace(SpaceAfter=None))
        self.header_range = SimpleNamespace(Font=SimpleNamespace(Bold=None))
        self.table = SimpleNamespace(
            Rows=SimpleNamespace(Item=lambda index: SimpleNamespace(Range=self.header_range)),
            Borders=SimpleNamespace(Enable=None),
            AutoFitBehavior=lambda behavior: setattr(self, "autofit", behavior),
        )
        self.Content = self._Content(self)
        self.Paragraphs = SimpleNamespace(Item=lambda index: SimpleNamespace(Range=self.title_range))

    class _Content:
        def __init__(self, writer):
            self.writer = writer
            self.Font = writer.body_font
            self.ParagraphFormat = writer.body_para

        @property
        def Text(self):
            return self.writer.body

        @Text.setter
        def Text(self, value):
            self.writer.body = value

        @property
        def End(self):
            return len(self.writer.body.encode("utf-16-le")) // 2 + 1

    class _Range:
        def __init__(self, writer):
            self.writer = writer

        @property
        def Text(self):
            return self.writer.inserted

        @Text.setter
        def Text(self, value):
            self.writer.inserted = value

        def ConvertToTable(self, **kwargs):
            self.writer.conversion = kwargs
            return self.writer.table

    def Range(self, start, end):
        self.ranges.append((start, end))
        return self._Range(self)


def test_word_native_batch_write_uses_utf16_ranges_and_owned_formatting():
    spec = validate_spec({"kind": "document", "title": "标题😀", "paragraphs": ["正文"],
                          "table": {"headers": ["编号", "值"],
                                    "rows": [["001", "🧪"], ["bool", True], ["空", None],
                                             ["浮点", 2.5]]}})
    doc = _FakeWordWriter()
    checks = []
    _write_word(doc, spec, lambda: checks.append(True))
    assert len(checks) >= 9
    assert doc.body == "标题😀\r正文\r"
    assert doc.body_font.Size == 12
    assert doc.body_para.SpaceAfter == 8
    assert (doc.title_range.Font.Size, doc.title_range.Font.Bold,
            doc.title_range.ParagraphFormat.SpaceAfter) == (18, True, 14)
    assert doc.inserted == "编号\t值\r001\t🧪\rbool\tTrue\r空\t\r浮点\t2.5\r"
    start = len(doc.body.encode("utf-16-le")) // 2
    assert doc.ranges == [(start, start),
                          (start, start + len(doc.inserted.encode("utf-16-le")) // 2)]
    assert doc.conversion == {"Separator": 1, "NumRows": 5, "NumColumns": 2}
    assert doc.header_range.Font.Bold is True
    assert doc.table.Borders.Enable == 1
    assert doc.autofit == 2


def test_word_native_batch_write_without_table():
    doc = _FakeWordWriter()
    _write_word(doc, validate_spec({"kind": "document", "title": "T", "paragraphs": []}),
                lambda: None)
    assert doc.ranges == []
    assert doc.body_para.SpaceAfter == 8
