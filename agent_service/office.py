"""Bounded, owned WPS Office document and spreadsheet creation.

This module imports pywin32 only inside ``execute_office``. A caller must run it in
the task's bounded desktop worker; an interrupted COM call has unknown effects.
"""

from __future__ import annotations

from hashlib import sha256
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CELL = re.compile(r"^([A-Z]{1,2})([1-9][0-9]*)$")


class OfficeError(RuntimeError):
    """An internal, stable error code suitable for a task receipt."""


def _string(value, maximum: int, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value.strip()):
        raise ValueError(f"invalid_{label}")
    if _CONTROL.search(value) or any(0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF)
                                      for c in value):
        raise ValueError(f"invalid_{label}")
    return value


def _significant_digits(value: Decimal) -> int:
    if not value.is_finite():
        return 10**9
    if value.is_zero():
        return 1
    digits = value.as_tuple().digits
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
    return len(digits)


def _decimal_total(values):
    """Add validated spreadsheet values as decimal text, within Excel precision."""
    values = list(values)
    try:
        with localcontext() as context:
            context.prec = 1000
            total = sum((Decimal(str(value)) for value in values), Decimal(0))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("sum_out_of_range") from exc
    if not total.is_finite():
        raise ValueError("sum_out_of_range")
    if _significant_digits(total) > 15:
        raise ValueError("sum_precision_unsupported")
    if all(type(value) is int for value in values):
        return int(total)
    try:
        result = float(total)
    except (OverflowError, ValueError) as exc:
        raise ValueError("sum_out_of_range") from exc
    if not math.isfinite(result):
        raise ValueError("sum_out_of_range")
    return result


def _scalar(value):
    if isinstance(value, str):
        return _string(value, 2000, "cell")
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        if len(str(abs(value))) > 15:
            raise ValueError("cell_integer_precision_unsupported")
        return value
    if type(value) is float and math.isfinite(value):
        try:
            significant = _significant_digits(Decimal(str(value)))
        except InvalidOperation as exc:
            raise ValueError("invalid_cell") from exc
        if significant > 15:
            raise ValueError("cell_float_precision_unsupported")
        return value
    raise ValueError("invalid_cell")


def _table(value, *, max_headers=12):
    if not isinstance(value, dict) or set(value) != {"headers", "rows"}:
        raise ValueError("invalid_table")
    headers = value["headers"]
    rows = value["rows"]
    if not isinstance(headers, list) or not 1 <= len(headers) <= max_headers:
        raise ValueError("invalid_headers")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError("invalid_rows")
    canonical_headers = [_string(h, 2000, "header", nonempty=True) for h in headers]
    canonical_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(headers):
            raise ValueError("invalid_row_width")
        canonical_rows.append([_scalar(cell) for cell in row])
    return {"headers": canonical_headers, "rows": canonical_rows}


def validate_spec(spec) -> dict:
    """Return an owned, canonical copy of a fixed Office action payload."""
    if not isinstance(spec, dict):
        raise ValueError("invalid_spec")
    kind = spec.get("kind")
    if kind == "document":
        if set(spec) not in ({"kind", "title", "paragraphs"},
                             {"kind", "title", "paragraphs", "table"}):
            raise ValueError("invalid_document_fields")
        title = _string(spec["title"], 200, "title", nonempty=True)
        paragraphs = spec["paragraphs"]
        if not isinstance(paragraphs, list) or len(paragraphs) > 40:
            raise ValueError("invalid_paragraphs")
        paragraphs = [_string(p, 3000, "paragraph") for p in paragraphs]
        if len(title) + sum(map(len, paragraphs)) > 60000:
            raise ValueError("document_too_large")
        result = {"kind": kind, "title": title, "paragraphs": paragraphs}
        if "table" in spec:
            result["table"] = _table(spec["table"])
        return result
    if kind == "spreadsheet":
        if set(spec) not in ({"kind", "title", "headers", "rows"},
                             {"kind", "title", "headers", "rows", "sum_columns"}):
            raise ValueError("invalid_spreadsheet_fields")
        title = _string(spec["title"], 200, "title", nonempty=True)
        table = _table({"headers": spec["headers"], "rows": spec["rows"]})
        columns = spec.get("sum_columns", [])
        if not isinstance(columns, list) or any(type(c) is not int or c < 0
                                                 or c >= len(table["headers"]) for c in columns):
            raise ValueError("invalid_sum_columns")
        if len(set(columns)) != len(columns):
            raise ValueError("duplicate_sum_column")
        for column in columns:
            if not table["rows"] or any(type(row[column]) not in (int, float)
                                         for row in table["rows"]):
                raise ValueError("sum_column_not_numeric")
            _decimal_total(row[column] for row in table["rows"])
        return {"kind": kind, "title": title, **table, "sum_columns": columns[:]}
    raise ValueError("invalid_kind")


def _expected(spec):
    if spec["kind"] == "document":
        result = {"title": spec["title"], "paragraphs": spec["paragraphs"][:]}
        if "table" in spec:
            result["table"] = spec["table"]
        return result
    totals = {str(c): _decimal_total(row[c] for row in spec["rows"])
              for c in spec["sum_columns"]}
    result = {"title": spec["title"], "headers": spec["headers"],
              "rows": spec["rows"], "totals": totals}
    label_col = _total_label_col(spec)
    if label_col is not None:
        result["total_label"] = "合计"
    return result


def _total_label_col(spec):
    if not spec["sum_columns"]:
        return None
    return next((c for c in range(len(spec["headers"])) if c not in spec["sum_columns"]), None)


def _display_width(value):
    text = "" if value is None else str(value)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _column_widths(spec):
    """Give every column a readable, bounded width in Excel character units."""
    widths = []
    for column, header in enumerate(spec["headers"]):
        values = [header, *(row[column] for row in spec["rows"])]
        numeric = all(type(value) in (int, float, bool, type(None)) for value in values[1:])
        if numeric:
            widths.append(12)
        else:
            widths.append(min(60, max(12, max(_display_width(value) for value in values) + 2)))
    return widths


def _public_observation(spec, internal):
    if spec["kind"] == "document":
        result = {"text": "\n".join([internal["title"], *internal["paragraphs"]])}
        if "table" in internal:
            result["table"] = internal["table"]
        return result
    return {"headers": internal["headers"], "rows": internal["rows"],
            "totals": internal["totals"]}


def _equal_scalar(actual, expected):
    if expected is None:
        return actual is None or actual == ""
    if type(expected) is bool:
        return type(actual) is bool and actual == expected
    if type(expected) is int:
        if type(actual) is int:
            return actual == expected
        return type(actual) is float and math.isfinite(actual) and actual == expected
    if type(expected) is float:
        if type(actual) not in (int, float) or (type(actual) is float and not math.isfinite(actual)):
            return False
        try:
            return _round_significant(Decimal(str(actual))) == _round_significant(Decimal(str(expected)))
        except InvalidOperation:
            return False
    return actual == expected


def _round_significant(value: Decimal, digits=15) -> Decimal:
    if not value.is_finite() or value.is_zero():
        return value
    quantum = Decimal(1).scaleb(value.adjusted() - digits + 1)
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def _compare(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(
            _compare(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _compare(a, e) for a, e in zip(actual, expected))
    return _equal_scalar(actual, expected)


def _safe_zip(path: Path, names: set[str]):
    with zipfile.ZipFile(path) as z:
        if len(z.infolist()) > 4000 or sum(i.file_size for i in z.infolist()) > 30_000_000:
            raise OfficeError("office_file_too_large")
        for item in z.infolist():
            p = PurePosixPath(item.filename)
            if item.file_size > 10_000_000 or p.is_absolute() or ".." in p.parts:
                raise OfficeError("invalid_office_package")
        for name in names:
            if name not in z.namelist():
                raise OfficeError("office_file_missing_part")
        return {name: z.read(name) for name in names}


def _word_file(path: Path, spec: dict):
    parts = _safe_zip(path, {"word/document.xml"})
    root = ET.fromstring(parts["word/document.xml"])
    body = root.find(f"{{{_W}}}body")
    if body is None:
        raise OfficeError("invalid_word_document")
    paragraphs = []
    tables = []
    for child in body:
        if child.tag == f"{{{_W}}}p":
            paragraphs.append("".join(t.text or "" for t in child.iter(f"{{{_W}}}t")))
        elif child.tag == f"{{{_W}}}tbl":
            table = []
            for row in child.findall(f"{{{_W}}}tr"):
                table.append(["".join(t.text or "" for t in cell.iter(f"{{{_W}}}t"))
                              for cell in row.findall(f"{{{_W}}}tc")])
            tables.append(table)
    expected_text = [spec["title"], *spec["paragraphs"]]
    # Word keeps a final empty body paragraph after a table in some WPS builds.
    if paragraphs[:len(expected_text)] != expected_text or any(paragraphs[len(expected_text):]):
        raise OfficeError("word_file_text_mismatch")
    expected_tables = []
    if "table" in spec:
        t = spec["table"]
        expected_tables = [[t["headers"], *[["" if v is None else str(v) for v in row]
                                           for row in t["rows"]]]]
    if tables != expected_tables:
        raise OfficeError("word_file_table_mismatch")
    return _expected(spec)


def _xlsx_file(path: Path, spec: dict):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
    required = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
    parts = _safe_zip(path, required)
    workbook = ET.fromstring(parts["xl/workbook.xml"])
    sheets = workbook.find(f"{{{_S}}}sheets")
    if sheets is None or not list(sheets):
        raise OfficeError("missing_worksheet")
    sheet = list(sheets)[0]
    rel_id = sheet.get(f"{{{_R}}}id")
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    target = next((r.get("Target") for r in rels if r.get("Id") == rel_id), None)
    if not target or target.startswith("/") and not target.startswith("/xl/"):
        raise OfficeError("invalid_worksheet_target")
    sheet_name = target.lstrip("/") if target.startswith("/") else str(PurePosixPath("xl") / target)
    sheet_name = str(PurePosixPath(sheet_name))
    if ".." in PurePosixPath(sheet_name).parts or sheet_name not in names:
        raise OfficeError("invalid_worksheet_target")
    read_names = {sheet_name}
    if "xl/sharedStrings.xml" in names:
        read_names.add("xl/sharedStrings.xml")
    parts = _safe_zip(path, read_names)
    shared = []
    if "xl/sharedStrings.xml" in parts:
        strings = ET.fromstring(parts["xl/sharedStrings.xml"])
        shared = ["".join(t.text or "" for t in si.iter(f"{{{_S}}}t"))
                  for si in strings.findall(f"{{{_S}}}si")]
    root = ET.fromstring(parts[sheet_name])
    cells = {}
    for cell in root.iter(f"{{{_S}}}c"):
        addr = cell.get("r", "")
        if not _CELL.fullmatch(addr):
            continue
        kind = cell.get("t")
        v = cell.find(f"{{{_S}}}v")
        inline = cell.find(f"{{{_S}}}is")
        if kind == "s":
            value = shared[int(v.text)] if v is not None and v.text is not None else ""
        elif kind == "inlineStr":
            value = "".join(t.text or "" for t in inline.iter(f"{{{_S}}}t")) if inline is not None else ""
        elif kind == "b":
            value = v is not None and v.text == "1"
        elif kind in ("str", "e"):
            value = v.text if v is not None else ""
        elif v is None or v.text is None:
            value = None
        else:
            value = float(v.text)
        cells[addr] = (value, cell.find(f"{{{_S}}}f"))
    observed = _sheet_observation(spec, lambda row, col: cells.get(f"{_col(col)}{row}", (None, None))[0])
    if not _compare(observed, _expected(spec)):
        raise OfficeError("spreadsheet_file_value_mismatch")
    total_row = len(spec["rows"]) + 3
    allowed_formula_cells = {f"{_col(col)}{total_row}" for col in spec["sum_columns"]}
    if any(formula is not None and address not in allowed_formula_cells
           for address, (_, formula) in cells.items()):
        raise OfficeError("unexpected_spreadsheet_formula")
    for col in spec["sum_columns"]:
        formula = cells.get(f"{_col(col)}{total_row}", (None, None))[1]
        expected_formula = f"SUM({_col(col)}3:{_col(col)}{total_row - 1})"
        if formula is None or (formula.text or "").upper().replace("$", "") != expected_formula:
            raise OfficeError("spreadsheet_formula_mismatch")
    return observed


def _col(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_observation(spec, read):
    width = len(spec["headers"])
    title = read(1, 0)
    headers = [read(2, c) for c in range(width)]
    rows = []
    for r, expected_row in enumerate(spec["rows"]):
        row = []
        for c, expected in enumerate(expected_row):
            actual = read(r + 3, c)
            # WPS persists an empty string as a blank cell. Apply this storage
            # equivalence only to data cells explicitly requested as empty text.
            if actual is None and type(expected) is str and expected == "":
                actual = ""
            row.append(actual)
        rows.append(row)
    totals = {str(c): read(len(spec["rows"]) + 3, c) for c in spec["sum_columns"]}
    result = {"title": title, "headers": headers, "rows": rows, "totals": totals}
    label_col = _total_label_col(spec)
    if label_col is not None:
        result["total_label"] = read(len(spec["rows"]) + 3, label_col)
    return result


def verify_office_file(path: Path, spec: dict):
    """Independently inspect OOXML produced by the native application."""
    spec = validate_spec(spec)
    path = Path(path)
    return _word_file(path, spec) if spec["kind"] == "document" else _xlsx_file(path, spec)


def _word_paragraphs(text: str, count: int):
    """Parse Word paragraph marks without trimming any source characters."""
    if not text.endswith("\r"):
        raise OfficeError("application_readback_mismatch")
    paragraphs = text[:-1].split("\r")
    if len(paragraphs) != count:
        raise OfficeError("application_readback_mismatch")
    return paragraphs


def _word_table_cells(text: str, rows: int, columns: int):
    """Parse WPS's cell and extra row CR/BEL terminators exactly."""
    marker = "\r\x07"
    if not text.endswith(marker):
        raise OfficeError("application_readback_mismatch")
    parts = text.split(marker)
    if len(parts) != rows * (columns + 1) + 1 or parts[-1] != "":
        raise OfficeError("application_readback_mismatch")
    table = []
    for row in range(rows):
        offset = row * (columns + 1)
        if parts[offset + columns] != "":
            raise OfficeError("application_readback_mismatch")
        cells = parts[offset:offset + columns]
        if any("\r" in cell or "\x07" in cell for cell in cells):
            raise OfficeError("application_readback_mismatch")
        table.append(cells)
    return table


def _word_app_observation(doc, spec):
    count = 1 + len(spec["paragraphs"])
    has_table = "table" in spec
    if doc.Tables.Count != int(has_table):
        raise OfficeError("application_readback_mismatch")
    if not has_table:
        body = str(doc.Content.Text)
        max_body = sum(len(value) for value in [spec["title"], *spec["paragraphs"]]) + count
        if len(body) > max_body + 1:
            raise OfficeError("application_readback_mismatch")
        if not body.endswith("\r"):
            raise OfficeError("application_readback_mismatch")
        paragraphs = body[:-1].split("\r")
        if len(paragraphs) != count + 1 or paragraphs[-1] != "":
            raise OfficeError("application_readback_mismatch")
        # Provisional strict WPS tail rule: one read-only probe observed this
        # shape; nonempty and title-only controls must validate it for release.
        paragraphs.pop()
        return {"title": paragraphs[0], "paragraphs": paragraphs[1:]}

    source = spec["table"]
    row_count = len(source["rows"]) + 1
    column_count = len(source["headers"])
    table = doc.Tables.Item(1)
    if table.Rows.Count != row_count or table.Columns.Count != column_count:
        raise OfficeError("application_readback_mismatch")
    table_range = table.Range
    body = str(doc.Range(0, table_range.Start).Text)
    table_text = str(table_range.Text)
    full_text = str(doc.Content.Text)
    max_body = sum(len(value) for value in [spec["title"], *spec["paragraphs"]]) + count
    values = [source["headers"], *source["rows"]]
    max_table = (sum(len("" if value is None else str(value)) for row in values for value in row)
                 + 2 * row_count * (column_count + 1))
    if (len(body) > max_body or len(table_text) > max_table
            or len(full_text) > max_body + max_table + 1
            or full_text != body + table_text + "\r"):
        raise OfficeError("application_readback_mismatch")
    paragraphs = _word_paragraphs(body, count)
    cells = _word_table_cells(table_text, row_count, column_count)
    return {"title": paragraphs[0], "paragraphs": paragraphs[1:],
            "table": {"headers": cells[0], "rows": cells[1:]}}


def _write_word(doc, spec, check):
    check()
    doc.Content.Text = "\r".join([spec["title"], *spec["paragraphs"]]) + "\r"
    check()
    doc.Content.Font.Size = 12
    check()
    doc.Content.ParagraphFormat.SpaceAfter = 8
    check()
    title = doc.Paragraphs.Item(1).Range
    title.Font.Size = 18
    check()
    title.Font.Bold = True
    check()
    title.ParagraphFormat.SpaceAfter = 14
    if "table" in spec:
        t = spec["table"]
        values = [t["headers"], *t["rows"]]
        delimited = "\r".join("\t".join("" if value is None else str(value)
                                            for value in row) for row in values) + "\r"
        check()
        start = doc.Content.End - 1
        insertion = doc.Range(start, start)
        check()
        insertion.Text = delimited
        check()
        utf16_units = len(delimited.encode("utf-16-le")) // 2
        table = doc.Range(start, start + utf16_units).ConvertToTable(
            Separator=1, NumRows=len(values), NumColumns=len(t["headers"]))
        check()
        table.Rows.Item(1).Range.Font.Bold = True
        check()
        table.Borders.Enable = 1
        check()
        table.AutoFitBehavior(2)  # wdAutoFitWindow: keep the table within the page.


def _write_sheet(book, spec, check):
    check()
    sheet = book.Worksheets.Item(1)
    check()
    sheet.Name = "Data"
    check()
    sheet.Cells(1, 1).NumberFormat = "@"
    check()
    sheet.Cells(1, 1).Value2 = spec["title"]
    for r, row in enumerate([spec["headers"], *spec["rows"]], 2):
        for c, value in enumerate(row, 1):
            check()
            cell = sheet.Cells(r, c)
            if isinstance(value, str):
                check()
                cell.NumberFormat = "@"
            check()
            cell.Value2 = value
    total_row = len(spec["rows"]) + 3
    label_col = _total_label_col(spec)
    if label_col is not None:
        check()
        cell = sheet.Cells(total_row, label_col + 1)
        cell.NumberFormat = "@"
        check()
        cell.Value2 = "合计"
    for c in spec["sum_columns"]:
        check()
        sheet.Cells(total_row, c + 1).Formula = f"=SUM({_col(c)}3:{_col(c)}{total_row - 1})"
    if spec["sum_columns"]:
        check()
        sheet.Calculate()
    width = len(spec["headers"])
    last_row = total_row if spec["sum_columns"] else total_row - 1
    check()
    used = sheet.Range(sheet.Cells(1, 1), sheet.Cells(last_row, width))
    used.Font.Size = 11
    check()
    used.WrapText = True
    check()
    used.Borders.LineStyle = 1  # xlContinuous
    check()
    title = sheet.Range(sheet.Cells(1, 1), sheet.Cells(1, width))
    if width > 1:
        check()
        title.Merge()
    check()
    title.Font.Size = 16
    check()
    title.Font.Bold = True
    check()
    title.Interior.Color = 0xF7EBDF  # light blue using COM's BGR color value
    check()
    header = sheet.Range(sheet.Cells(2, 1), sheet.Cells(2, width))
    header.Font.Bold = True
    check()
    header.Interior.Color = 0xEBD7BC
    if spec["sum_columns"]:
        check()
        total = sheet.Range(sheet.Cells(total_row, 1), sheet.Cells(total_row, width))
        total.Font.Bold = True
        check()
        total.Interior.Color = 0xE6F2E6
    for column, column_width in enumerate(_column_widths(spec), 1):
        check()
        sheet.Columns(column).ColumnWidth = column_width
    check()
    sheet.Rows(1).RowHeight = 36
    check()
    sheet.Range(sheet.Cells(2, 1), sheet.Cells(last_row, width)).Rows.AutoFit()
    for row in range(2, last_row + 1):
        check()
        height = sheet.Rows(row).RowHeight
        if height > 180:
            check()
            sheet.Rows(row).RowHeight = 180
    return sheet


def execute_office(spec, run_root: Path, should_stop=lambda: False) -> dict:
    """Create, save, independently verify, and reopen one owned Office artifact.

    The run root must be a fresh directory allocated by the task runner. This
    function never takes a file path from the payload and never quits the app.
    """
    stage = "validate"
    native_started = False
    final = None
    result = {"ok": False, "state": "failed", "application_verified": False,
              "file_verified": False}

    def check():
        if should_stop():
            raise OfficeError("cancelled")

    try:
        spec = validate_spec(spec)
        root = Path(run_root).resolve(strict=True)
        if not root.is_dir():
            raise OfficeError("run_root_missing")
        filename = "document.docx" if spec["kind"] == "document" else "spreadsheet.xlsx"
        final = root / filename
        staging = root / ("staging-" + filename)
        if final.exists() or staging.exists():
            raise OfficeError("artifact_path_conflict")
        result["artifact"] = str(final)
        check()
        stage = "initialize_com"
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            progid = "KWPS.Application" if spec["kind"] == "document" else "KET.Application"
            check()
            stage = "start_application"
            native_started = True
            app = win32com.client.DispatchEx(progid)
            check()
            app.Visible = True
            result["app"] = {"progid": progid, "name": str(app.Name), "version": str(app.Version)}
            check()
            stage = "create_object"
            owned = app.Documents.Add() if spec["kind"] == "document" else app.Workbooks.Add()
            stage = "write_content"
            if spec["kind"] == "document":
                _write_word(owned, spec, check)
            else:
                _write_sheet(owned, spec, check)
            check()
            stage = "save_staging"
            # Explicit OOXML format IDs from Word/Excel COM compatibility APIs.
            owned.SaveAs(str(staging), FileFormat=16 if spec["kind"] == "document" else 51)
            if not staging.is_file():
                raise OfficeError("staging_file_missing")
            stage = "verify_file"
            observed_file = verify_office_file(staging, spec)
            result["file_verified"] = True
            check()
            stage = "publish_file"
            # Exclusive creation preserves staging as immutable evidence even if a
            # user later edits the visible reopened final document.
            with staging.open("rb") as source, final.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            digest = sha256(final.read_bytes()).hexdigest()
            if digest != sha256(staging.read_bytes()).hexdigest():
                raise OfficeError("published_file_mismatch")
            result["sha256"] = digest
            check()
            stage = "close_staging"
            owned.Close(SaveChanges=False)
            check()
            stage = "reopen_final"
            reopened = app.Documents.Open(str(final)) if spec["kind"] == "document" else app.Workbooks.Open(str(final))
            check()
            stage = "verify_application"
            if spec["kind"] == "document":
                observed_app = _word_app_observation(reopened, spec)
                # Table values in Word are displayed as text.
                expected_app = _expected(spec)
                if "table" in expected_app:
                    t = expected_app["table"]
                    expected_app["table"] = {"headers": t["headers"],
                                             "rows": [["" if v is None else str(v) for v in row]
                                                      for row in t["rows"]]}
            else:
                sheet = reopened.Worksheets.Item(1)
                observed_app = _sheet_observation(spec, lambda r, c: sheet.Cells(r, c + 1).Value2)
                expected_app = _expected(spec)
            if not _compare(observed_app, expected_app) or not _compare(observed_file, _expected(spec)):
                raise OfficeError("application_readback_mismatch")
            result.update(ok=True, state="succeeded", artifact=str(final),
                          observed=_public_observation(spec, observed_app),
                          application_verified=True, file_verified=True)
        finally:
            pythoncom.CoUninitialize()
    except Exception as error:
        code = str(error) if isinstance(error, OfficeError) else (
            str(error) if isinstance(error, ValueError) else "office_operation_failed")
        result.update(ok=False, state="cancelled" if code == "cancelled" and not native_started
                      else "unknown" if native_started else "failed", code=code, stage=stage)
        # A trustworthy file readback remains true even if later app verification fails.
    return result
