"""Precision and literal preservation at the Office/source boundaries."""

import json
from openpyxl import Workbook
import pytest

from agent_service import assistant
from agent_service.office import _equal_scalar, _expected, validate_spec
from agent_service.office_source import SourceError, _csv_value, snapshot_source


def _task_root(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    return root


def test_csv_keeps_leading_zero_ids_and_values_over_excel_precision_as_text(tmp_path):
    source = tmp_path / "precise.csv"
    source.write_text(
        "编号,长整数,高精度小数,普通数\n"
        "001234,1234567890123456,0.12345678901234567,0.1\n",
        encoding="utf-8",
    )

    result = snapshot_source(source, _task_root(tmp_path))

    assert result["observed"]["rows"] == [[
        "001234", "1234567890123456", "0.12345678901234567", 0.1,
    ]]
    assert _csv_value("001234") == "001234"
    assert _csv_value("1234567890123456") == "1234567890123456"
    assert _csv_value("0.12345678901234567") == "0.12345678901234567"


@pytest.mark.parametrize("value", [1234567890123456, 1234567890123450])
def test_xlsx_ambiguous_large_numeric_cell_is_rejected_before_model_call(tmp_path, value):
    source = tmp_path / "ambiguous.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["编号", "值"])
    sheet.append(["A", value])
    workbook.save(source)

    with pytest.raises(SourceError, match="workbook_numeric_precision_unsupported"):
        snapshot_source(source, _task_root(tmp_path))

    calls = []

    class NoCallClient:
        def complete(self, **kwargs):
            calls.append(kwargs)
            return {"kind": "unsupported", "reason": "should not be reached"}

    result = assistant.run_task("Use this workbook", tmp_path / "assistant-tasks",
                                source_file=source, client=NoCallClient())
    assert result["ok"] is False and result["error"] == "workbook_numeric_precision_unsupported"
    assert calls == []


@pytest.mark.parametrize("value", [1234567890123456, 1.234567890123456])
def test_office_spec_rejects_numeric_inputs_beyond_fifteen_significant_digits(value):
    spec = {"kind": "spreadsheet", "title": "数值精度", "headers": ["值"],
            "rows": [[value]], "sum_columns": []}

    with pytest.raises(ValueError, match="precision_unsupported"):
        validate_spec(spec)


def test_decimal_sum_is_stable_and_com_float_roundtrip_uses_fifteen_digits():
    spec = validate_spec({"kind": "spreadsheet", "title": "小数求和", "headers": ["金额"],
                          "rows": [[0.1], [0.2]], "sum_columns": [0]})

    assert _expected(spec)["totals"] == {"0": 0.3}
    assert _equal_scalar(0.30000000000000004, 0.3)
    assert not _equal_scalar(0.300000000000001, 0.3)


def test_integer_readback_comparison_rejects_large_precision_loss():
    expected = 9007199254740992
    rounded_actual = 9007199255640992.0

    assert rounded_actual - expected == 900000
    assert not _equal_scalar(rounded_actual, expected)


def test_csv_snapshot_preserves_source_bytes_and_textual_precision(tmp_path):
    source = tmp_path / "source.csv"
    raw = ("编号,金额\r\n00123,0.10000000000000001\r\n"
           "大值,1234567890123456\r\n").encode("utf-8-sig")
    source.write_bytes(raw)

    result = snapshot_source(source, _task_root(tmp_path))
    snapshot = tmp_path / "task" / "sources" / "source.csv"

    assert snapshot.read_bytes() == raw
    assert source.read_bytes() == raw
    assert result["observed"]["rows"] == [
        ["00123", "0.10000000000000001"],
        ["大值", "1234567890123456"],
    ]


def test_csv_precise_values_are_sent_to_planner_as_text(tmp_path):
    source = tmp_path / "identifiers.csv"
    source.write_text("编号,金额\n00123,0.10000000000000001\n大值,1234567890123456\n",
                      encoding="utf-8")

    class UnsupportedClient:
        def __init__(self):
            self.requests = []

        def complete(self, **kwargs):
            self.requests.append(kwargs)
            return {"kind": "unsupported", "reason": "此处仅验证来源数值类型"}

    client = UnsupportedClient()
    result = assistant.run_task("读取这份 CSV", tmp_path / "assistant-tasks",
                                source_file=source, client=client)

    assert result["state"] == "unsupported"
    payload = json.loads(client.requests[0]["user"])
    assert payload["selected_source"]["observed"]["rows"] == [
        ["00123", "0.10000000000000001"],
        ["大值", "1234567890123456"],
    ]
