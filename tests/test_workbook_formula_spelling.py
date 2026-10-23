"""Native sheet-name quote normalization must not mask changed formulas."""
import pytest
from agent_service.workbook_source import _same_native_formula

BOUNDS = {'采购': (4, 4), '参数': (2, 2), '汇总': (3, 2), "O'Brien": (2, 2)}

@pytest.mark.parametrize('actual,expected', [
    ('=采购!D4-参数!B2', "='采购'!D4-'参数'!B2"),
    ('=B2*(1+参数!B1)', "=B2*(1+'参数'!B1)"),
    ('=SUM(采购!$D$2:$D$3)', "=SUM('采购'!$D$2:$D$3)"),
    ("='O''Brien'!A1", "='O''Brien'!A1"),
])
def test_native_quote_normalization_is_equal(actual, expected):
    assert _same_native_formula(actual, expected, '汇总', BOUNDS)

@pytest.mark.parametrize('actual', [
    '=采购!D3-参数!B2', '=采购!D4+参数!B2', '=采购!$D$4-参数!B2',
    '=166-参数!B2', '=161', '=SUM(采购!D4,参数!B2)',
    '=采购!D4-参数!B1', '=未知!D4-参数!B2',
    "='[remote.xlsx]采购'!D4-参数!B2", '=采购!D4-INDIRECT("B2")', None,
])
def test_native_changed_formula_stays_rejected(actual):
    assert not _same_native_formula(actual, "='采购'!D4-'参数'!B2", '汇总', BOUNDS)
