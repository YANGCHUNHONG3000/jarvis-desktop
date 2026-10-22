"""Resolve selected-source plans into the fixed, ordinary Office workflow.

The model chooses only a bounded action and column indices. Values always come
from the verified source snapshot; this module never opens source/output files.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
import unicodedata

from .office import _decimal_total, _scalar, validate_spec
from .office_source import _csv_value
from .office_workflow import validate_steps


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DISPLAY_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f\x7f-\x9f]{1,200}\Z")
_AGGREGATE_LABELS = frozenset({"合计", "小计", "总计", "汇总", "total", "subtotal", "grand total"})


class SourcePlanError(ValueError):
    """Stable rejection code for a source-derived Office plan."""


def _snapshot(snapshot):
    required = {"source_id", "kind", "sha256", "display_name", "observed"}
    if (type(snapshot) is not dict or not required <= set(snapshot)
            or set(snapshot) - required - {"bytes", "coverage"}):
        raise SourcePlanError("source_snapshot_invalid")
    if snapshot["source_id"] != "selected" or snapshot["kind"] not in {"table", "document"}:
        raise SourcePlanError("source_snapshot_invalid")
    digest = snapshot["sha256"]
    name = snapshot["display_name"]
    if type(digest) is not str or not _SHA256.fullmatch(digest):
        raise SourcePlanError("source_snapshot_invalid")
    if type(name) is not str or not name.strip() or not _DISPLAY_NAME.fullmatch(name):
        raise SourcePlanError("source_snapshot_invalid")
    if "bytes" in snapshot and (type(snapshot["bytes"]) is not int
                                or not 0 < snapshot["bytes"] <= 20_000_000):
        raise SourcePlanError("source_snapshot_invalid")
    if "coverage" in snapshot and type(snapshot["coverage"]) is not dict:
        raise SourcePlanError("source_snapshot_invalid")
    observed = snapshot["observed"]
    if type(observed) is not dict:
        raise SourcePlanError("source_snapshot_invalid")
    try:
        if snapshot["kind"] == "table":
            if set(observed) != {"headers", "rows"}:
                raise ValueError("table_observation_fields")
            normalized = validate_spec({"kind": "spreadsheet", "title": name,
                                        "headers": observed["headers"], "rows": observed["rows"]})
            return {"kind": "table", "sha256": digest, "display_name": name,
                    "observed": {"headers": normalized["headers"], "rows": normalized["rows"]}}
        if set(observed) not in ({"paragraphs"}, {"paragraphs", "table"}):
            raise ValueError("document_observation_fields")
        candidate = {"kind": "document", "title": name, "paragraphs": observed["paragraphs"]}
        if "table" in observed:
            candidate["table"] = observed["table"]
        normalized = validate_spec(candidate)
        document = {"paragraphs": normalized["paragraphs"]}
        if "table" in normalized:
            document["table"] = normalized["table"]
        return {"kind": "document", "sha256": digest, "display_name": name,
                "observed": document}
    except (KeyError, TypeError, ValueError) as exc:
        raise SourcePlanError("source_snapshot_invalid") from exc


def _step_shape(step, index):
    if type(step) is not dict or step.get("id") != f"s{index}" or type(step.get("action")) is not str:
        raise SourcePlanError("source_step_invalid")
    return step["action"]


def _columns(columns, width):
    if type(columns) is not list or not 1 <= len(columns) <= width:
        raise SourcePlanError("source_columns_invalid")
    if any(type(c) is not int or not 0 <= c < width for c in columns):
        raise SourcePlanError("source_columns_invalid")
    if len(set(columns)) != len(columns):
        raise SourcePlanError("source_columns_invalid")
    return columns[:]


def _sums(columns, width):
    if type(columns) is not list or len(columns) > width:
        raise SourcePlanError("source_sums_invalid")
    if any(type(c) is not int or not 0 <= c < width for c in columns):
        raise SourcePlanError("source_sums_invalid")
    if len(set(columns)) != len(columns):
        raise SourcePlanError("source_sums_invalid")
    return columns[:]


def _patched_rows(step, observed):
    """Return a fully copied table patch and its exact source-coordinate audit."""
    width = len(observed["headers"])
    original_count = len(observed["rows"])
    rows = [row[:] for row in observed["rows"]]
    edits = step.get("edits", [])
    appended = step.get("append_rows", [])
    if type(edits) is not list or len(edits) > 50:
        raise SourcePlanError("source_edits_invalid")
    if type(appended) is not list or len(appended) > 50:
        raise SourcePlanError("source_append_invalid")
    if original_count + len(appended) > 200:
        raise SourcePlanError("source_rows_too_many")
    seen = set()
    audit = []
    for edit in edits:
        if type(edit) is not dict or set(edit) != {"row", "column", "op", "value"}:
            raise SourcePlanError("source_edit_invalid")
        row, column, op = edit["row"], edit["column"], edit["op"]
        if (type(row) is not int or not 0 <= row < original_count
                or type(column) is not int or not 0 <= column < width
                or type(op) is not str or op not in {"set", "add"}):
            raise SourcePlanError("source_edit_invalid")
        coordinate = (row, column)
        if coordinate in seen:
            raise SourcePlanError("source_edit_duplicate")
        seen.add(coordinate)
        before = rows[row][column]
        try:
            value = _scalar(edit["value"])
            if op == "add":
                if type(before) not in (int, float) or type(value) not in (int, float):
                    raise ValueError("source_add_not_numeric")
                after = _scalar(_decimal_total([before, value]))
            else:
                after = value
        except (TypeError, ValueError) as exc:
            raise SourcePlanError("source_edit_invalid") from exc
        rows[row][column] = after
        audit.append({"step": step["id"], "row": row, "column": column,
                      "before": before, "after": after, "op": op})
    for input_row in appended:
        if type(input_row) is not list or len(input_row) != width:
            raise SourcePlanError("source_append_invalid")
        try:
            new_row = [_scalar(value) for value in input_row]
        except (TypeError, ValueError) as exc:
            raise SourcePlanError("source_append_invalid") from exc
        audit.append({"step": step["id"], "row": len(rows), "op": "append", "values": new_row[:]})
        rows.append(new_row)
    return rows, audit


def _document_numeric_rows(step, table, columns, numeric_columns, sums):
    """Convert only explicitly mapped DOCX text cells; preserve all source rows."""
    for row in table["rows"]:
        for cell in row:
            label = " ".join(unicodedata.normalize("NFKC", cell).split()).casefold().rstrip(" :")
            if label in _AGGREGATE_LABELS:
                raise SourcePlanError("source_existing_total_unsupported")
    converted = []
    audit = []
    numeric = set(numeric_columns)
    for row_index, row in enumerate(table["rows"]):
        output = []
        for output_column, source_column in enumerate(columns):
            before = row[source_column]
            if source_column not in numeric:
                output.append(before)
                continue
            try:
                after = _csv_value(before)
                if type(after) not in (int, float):
                    raise ValueError("source_numeric_text_invalid")
                precise = Decimal(before)
                if not precise.is_finite() or precise.adjusted() >= 15:
                    raise ValueError("source_numeric_magnitude_unsupported")
                after = _scalar(after)
            except (ValueError, InvalidOperation, OverflowError, TypeError) as exc:
                raise SourcePlanError("source_numeric_cell_invalid") from exc
            output.append(after)
            audit.append({"step": step["id"], "row": row_index, "column": source_column,
                          "output_column": output_column, "before": before, "after": after,
                          "op": "convert_numeric", "sum_columns": sums[:]})
        converted.append(output)
    return converted, audit


def _resolve_source_steps(steps, snapshot):
    source = _snapshot(snapshot)
    if type(steps) is not list or not 1 <= len(steps) <= 8:
        raise SourcePlanError("source_steps_invalid")
    ordinary = []
    audit = []
    derived_sheets = set()
    document_table_sheets = set()
    document_numeric_sheets = set()
    sheet_specs = {}
    for index, step in enumerate(steps, 1):
        action = _step_shape(step, index)
        if action == "spreadsheet_from_source":
            required = {"id", "action", "source_id", "title", "columns", "sum_columns"}
            if (not required <= set(step) or set(step) - required - {"edits", "append_rows"}
                    or step["source_id"] != "selected" or source["kind"] != "table"):
                raise SourcePlanError("source_step_invalid")
            observed = source["observed"]
            columns = _columns(step["columns"], len(observed["headers"]))
            sums = _sums(step["sum_columns"], len(columns))
            rows, changes = _patched_rows(step, observed)
            audit.extend(changes)
            data = {"kind": "spreadsheet", "title": step["title"],
                    "headers": [observed["headers"][c] for c in columns],
                    "rows": [[row[c] for c in columns] for row in rows],
                    "sum_columns": sums}
            try:
                data = validate_spec(data)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePlanError("source_step_invalid") from exc
            ordinary.append({"id": step["id"], "action": "spreadsheet", "data": data})
            derived_sheets.add(step["id"])
            sheet_specs[step["id"]] = data
        elif action == "spreadsheet_from_document_table":
            if (set(step) != {"id", "action", "source_id", "title", "columns"}
                    or step["source_id"] != "selected" or source["kind"] != "document"):
                raise SourcePlanError("source_step_invalid")
            table = source["observed"].get("table")
            if table is None:
                raise SourcePlanError("source_document_table_missing")
            if (any(type(header) is not str for header in table["headers"])
                    or any(type(cell) is not str for row in table["rows"] for cell in row)):
                raise SourcePlanError("source_document_table_nontext")
            columns = _columns(step["columns"], len(table["headers"]))
            data = {"kind": "spreadsheet", "title": step["title"],
                    "headers": [table["headers"][column] for column in columns],
                    "rows": [[row[column] for column in columns] for row in table["rows"]],
                    "sum_columns": []}
            try:
                data = validate_spec(data)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePlanError("source_step_invalid") from exc
            ordinary.append({"id": step["id"], "action": "spreadsheet", "data": data})
            derived_sheets.add(step["id"])
            document_table_sheets.add(step["id"])
            sheet_specs[step["id"]] = data
        elif action == "spreadsheet_from_document_table_numeric":
            if (set(step) != {"id", "action", "source_id", "title", "columns",
                              "numeric_columns", "sum_columns"}
                    or step["source_id"] != "selected" or source["kind"] != "document"):
                raise SourcePlanError("source_step_invalid")
            table = source["observed"].get("table")
            if table is None:
                raise SourcePlanError("source_document_table_missing")
            if (any(type(header) is not str for header in table["headers"])
                    or any(type(cell) is not str for row in table["rows"] for cell in row)):
                raise SourcePlanError("source_document_table_nontext")
            columns = _columns(step["columns"], len(table["headers"]))
            numeric_columns = _columns(step["numeric_columns"], len(table["headers"]))
            sums = _sums(step["sum_columns"], len(columns))
            if (len(columns) < 2 or not sums or 0 in sums
                    or columns[0] in numeric_columns
                    or set(numeric_columns) != {columns[index] for index in sums}):
                raise SourcePlanError("source_numeric_columns_invalid")
            rows, changes = _document_numeric_rows(step, table, columns, numeric_columns, sums)
            data = {"kind": "spreadsheet", "title": step["title"],
                    "headers": [table["headers"][column] for column in columns],
                    "rows": rows, "sum_columns": sums}
            try:
                data = validate_spec(data)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePlanError("source_numeric_values_invalid") from exc
            ordinary.append({"id": step["id"], "action": "spreadsheet", "data": data})
            audit.extend(changes)
            derived_sheets.add(step["id"])
            document_numeric_sheets.add(step["id"])
            sheet_specs[step["id"]] = data
        elif action == "document_from_source":
            if (set(step) != {"id", "action", "source_id", "title"}
                    or step["source_id"] != "selected" or source["kind"] != "document"):
                raise SourcePlanError("source_step_invalid")
            observed = source["observed"]
            data = {"kind": "document", "title": step["title"],
                    "paragraphs": observed["paragraphs"][:]}
            if "table" in observed:
                data["table"] = observed["table"]
            try:
                data = validate_spec(data)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePlanError("source_step_invalid") from exc
            ordinary.append({"id": step["id"], "action": "document", "data": data})
        elif action == "report_from_sheet":
            if (set(step) != {"id", "action", "from", "title"}
                    or type(step["from"]) is not str or step["from"] not in derived_sheets):
                raise SourcePlanError("source_reference_invalid")
            referenced = sheet_specs[step["from"]]
            if len(referenced["rows"]) + bool(referenced["sum_columns"]) > 200:
                raise SourcePlanError("source_report_too_large")
            paragraphs = [f"来源文件：{source['display_name']}",
                          f"来源 SHA-256：{source['sha256'][:16]}"]
            if step["from"] in document_table_sheets:
                paragraphs.append("仅提取来源文档的表格，未复制正文或原版式；保留原表文字，已有合计行按原文保留（如存在），未重新计算。")
            elif step["from"] in document_numeric_sheets:
                paragraphs.append("仅提取来源文档的表格，未复制正文或原版式；指定数字列已由原文显式转换，并对全部保留的明细行重新求和。原文档保持不变。")
            try:
                validate_spec({"kind": "document", "title": step["title"],
                               "paragraphs": paragraphs})
            except (KeyError, TypeError, ValueError) as exc:
                raise SourcePlanError("source_step_invalid") from exc
            ordinary.append({"id": step["id"], "action": "document_from_sheet",
                             "from": step["from"], "title": step["title"],
                             "paragraphs": paragraphs})
        elif action == "email_draft":
            if set(step) != {"id", "action", "data", "attachments"}:
                raise SourcePlanError("source_step_invalid")
            from .email_draft import validate_draft
            data = validate_draft(step["data"])
            ordinary.append({"id": step["id"], "action": "email_draft",
                             "data": data, "attachments": step["attachments"]})
        else:
            raise SourcePlanError("source_action_unsupported")
    try:
        return validate_steps(ordinary), audit
    except (KeyError, TypeError, ValueError) as exc:
        raise SourcePlanError("source_step_invalid") from exc


def resolve_source_steps(steps, snapshot) -> list[dict]:
    """Return canonical Office steps with source edits applied before selection."""
    return _resolve_source_steps(steps, snapshot)[0]


def describe_source_edits(steps, snapshot) -> list[dict]:
    """Return exact before/after changes for a fully valid selected-source plan."""
    return _resolve_source_steps(steps, snapshot)[1]
