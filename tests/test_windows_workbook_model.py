"""Offline preservation counterexamples; no Office instance is ever created."""
from copy import deepcopy
import json

from openpyxl import Workbook, load_workbook
from openpyxl.workbook.defined_name import DefinedName
import pytest

from agent_service import windows_workbook_model as model
from agent_service.computer import ComputerError


def fixture(path):
    book = Workbook()
    sheet = book.active
    sheet.title = '明细'
    sheet.append(['项目', '金额'])
    sheet.append(['甲', 10])
    sheet.append(['乙', 20])
    sheet.append(['合计', '=SUM(B2:B3)'])
    other = book.create_sheet('汇总')
    other.append(['公式', "='明细'!B4*2+5"])
    hidden = book.create_sheet('隐藏')
    hidden['A1'] = True
    hidden.sheet_state = 'hidden'
    book.create_sheet('空白')
    book.save(path)
    book.close()


class Pointer:
    def __init__(self, **properties):
        self.properties = properties
        self.calls = []

    def GetIDsOfNames(self, lcid, name):
        assert lcid == 0
        return name

    def Invoke(self, member, lcid, flags, result, *args):
        assert (lcid, flags, result) == (0, 2, 1), 'PROPERTYGET only'
        self.calls.append((member, args))
        value = self.properties[member]
        return value(*args) if callable(value) else value


def native(expected):
    from openpyxl.utils.cell import get_column_letter
    sheets = []
    for data in expected['sheets']:
        formulas = {entry['cell']: entry['expression'] for entry in data['formulas']}
        cells = {}
        for r, values in enumerate(data['values'], 1):
            for c, value in enumerate(values, 1):
                address = f'{get_column_letter(c)}{r}'
                cells[address] = Pointer(MergeCells=False, HasFormula=address in formulas,
                                         Formula=formulas.get(address, value), Value2=value)
        used = Pointer(Row=1, Column=1, Rows=Pointer(Count=len(data['values'])),
                       Columns=Pointer(Count=len(data['values'][0])))
        sheet = Pointer(Name=data['name'], Visible={'visible': -1, 'hidden': 0, 'veryHidden': 2}[data['state']],
                        Names=Pointer(Count=0), UsedRange=used, Range=lambda address, cells=cells: cells[address])
        sheet.cells = cells
        sheets.append(sheet)
    collection = Pointer(Count=len(sheets), Item=lambda index: sheets[index - 1])
    book = Pointer(Sheets=collection, Worksheets=Pointer(Count=len(sheets)), Names=Pointer(Count=0))
    book.sheets = sheets
    return book


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / 'input.xlsx'
    fixture(path)
    expected = model.disk_model(path)
    return path, expected, native(expected)


def test_complete_disk_and_native_round_trip_without_cached_results(prepared):
    path, expected, book = prepared
    before = path.read_bytes()
    for sheet in book.sheets:
        for cell in sheet.cells.values():
            if cell.properties['HasFormula']:
                cell.properties['Value2'] = object()  # impossible cached value is not accessed
    checks = []
    raw, actual = model.capture(book, path, lambda: checks.append(1))
    assert actual == expected == model.serialized_model(raw)
    assert len(checks) > 30 and path.read_bytes() == before
    assert len(actual['sheets']) == 4
    assert actual['sheets'][2]['state'] == 'hidden'
    assert actual['sheets'][0]['values'][3][1] is None
    assert actual['sheets'][0]['formulas'][0]['expression'] == '=SUM(B2:B3)'
    assert all('Value2' not in [call[0] for call in cell.calls]
               for sheet in book.sheets for cell in sheet.cells.values() if cell.properties['HasFormula'])


def test_native_quoting_normalized_but_raw_expression_retained(prepared):
    _, expected, book = prepared
    book.sheets[1].cells['B1'].properties['Formula'] = '=明细!b4*2+5'
    raw, actual = model.capture(book, expected, lambda: None)
    assert actual == expected == model.serialized_model(raw)
    assert json.loads(raw)['sheets'][1]['formulas'][0]['expression'] == '=明细!b4*2+5'
    assert actual['sheets'][1]['formulas'][0]['expression'] == "='明细'!B4*2+5"


def test_empty_native_formula_is_a_blank_literal(prepared):
    _, expected, book = prepared
    book.sheets[-1].cells['A1'].properties['Formula'] = ''
    raw, actual = model.capture(book, expected, lambda: None)
    assert actual == expected == model.serialized_model(raw)


@pytest.mark.parametrize('text', ['10', '10.0', '+10', '1E1', '0.10e2'])
def test_nonformula_numeric_text_precisely_matches_native_number(prepared, text):
    _, expected, book = prepared
    book.sheets[0].cells['B2'].properties.update(Formula=text, Value2=10.0)
    raw, actual = model.capture(book, expected, lambda: None)
    assert actual == expected == model.serialized_model(raw)


@pytest.mark.parametrize('text', ['10.000000000000001', '11', '=10', 'NaN', 'Infinity', '1e999999',
                                 ' 10', '10 ', '1,0', '0' * 101 + '10'])
def test_nonformula_numeric_text_mismatch_stays_rejected_and_diagnostics_are_safe(prepared, text):
    _, expected, book = prepared
    book.sheets[0].cells['B2'].properties.update(Formula=text, Value2=10.0)
    with pytest.raises(ComputerError) as raised:
        model.capture(book, expected, lambda: None)
    diagnostic = raised.value.copy_diagnostic
    assert diagnostic['workbook_formula_type'] == 'str' and diagnostic['workbook_value_type'] == 'float'
    assert diagnostic['workbook_numeric_text_equivalent'] is False
    assert '10' not in json.dumps(diagnostic)


def test_numeric_text_is_not_formula_preservation_evidence(prepared):
    _, expected, book = prepared
    book.sheets[0].cells['B4'].properties.update(HasFormula=False, Formula='30', Value2=30.0)
    with pytest.raises(ComputerError, match='windows_copy_native_content_mismatch'):
        model.capture(book, expected, lambda: None)


def test_numeric_text_cannot_replace_a_true_formula_expression(prepared):
    _, expected, book = prepared
    book.sheets[0].cells['B4'].properties.update(Formula='30', Value2=30.0)
    with pytest.raises(ComputerError) as raised:
        model.capture(book, expected, lambda: None)
    assert raised.value.copy_diagnostic['workbook_validation_reason'] == 'workbook_formula_invalid'


@pytest.mark.parametrize('value', [True, float('nan'), float('inf')])
def test_numeric_text_never_coerces_boolean_or_nonfinite_number(value):
    assert model._numeric_text_equivalent('1', value) is False


@pytest.mark.parametrize('change', ['lost', 'static_equal', 'same_value_formula', 'anchors', 'cross_sheet',
                                  'visibility', 'order', 'missing_sheet', 'added_sheet', 'outside',
                                  'literal', 'merged', 'names', 'sheet_names', 'chart', 'hasformula'])
def test_native_preservation_counterexamples(prepared, change):
    _, expected, book = prepared
    cell = book.sheets[0].cells['B4'].properties
    if change in {'lost', 'static_equal'}:
        cell.update(HasFormula=False, Formula=None if change == 'lost' else 30,
                    Value2=None if change == 'lost' else 30)
    elif change == 'same_value_formula':
        cell['Formula'] = '=B2+B3'
    elif change == 'anchors':
        cell['Formula'] = '=SUM($B$2:$B$3)'
    elif change == 'cross_sheet':
        book.sheets[1].cells['B1'].properties['Formula'] = "='明细'!B3*3+5"
    elif change == 'visibility':
        book.sheets[2].properties['Visible'] = 2
    elif change == 'order':
        book.sheets[0], book.sheets[1] = book.sheets[1], book.sheets[0]
    elif change in {'missing_sheet', 'added_sheet'}:
        book.properties['Sheets'].properties['Count'] += -1 if change == 'missing_sheet' else 1
    elif change == 'outside':
        book.sheets[0].properties['UsedRange'].properties['Rows'].properties['Count'] = 5
    elif change == 'literal':
        book.sheets[0].cells['B2'].properties.update(Formula=11, Value2=11)
    elif change == 'merged':
        cell['MergeCells'] = True
    elif change == 'names':
        book.properties['Names'].properties['Count'] = 1
    elif change == 'sheet_names':
        book.sheets[0].properties['Names'].properties['Count'] = 1
    elif change == 'chart':
        book.properties['Worksheets'].properties['Count'] -= 1
    elif change == 'hasformula':
        cell['HasFormula'] = None
    with pytest.raises(ComputerError):
        model.capture(book, expected, lambda: None)


@pytest.mark.parametrize('expression', ['=AVERAGE(B2:B3)', "='[external.xlsx]S'!A1", '=NamedValue', '=B2/0'])
def test_disk_rejects_unsupported_formula_before_native(prepared, expression):
    path, _, _ = prepared
    book = load_workbook(path)
    book['明细']['B4'] = expression
    book.save(path)
    book.close()
    with pytest.raises(ComputerError, match='windows_copy_content_unsupported'):
        model.disk_model(path)


@pytest.mark.parametrize('change', ['defined_name', 'merged', 'outside'])
def test_disk_rejects_unsupported_structure(prepared, change):
    path, _, _ = prepared
    book = load_workbook(path)
    if change == 'defined_name':
        book.defined_names.add(DefinedName('named', attr_text="'明细'!B2"))
    elif change == 'merged':
        book['明细'].merge_cells('A2:A3')
    else:
        book['明细']['M1'] = 1
    book.save(path)
    book.close()
    with pytest.raises(ComputerError):
        model.disk_model(path)


@pytest.mark.parametrize('change', ['hash', 'literal', 'formula', 'cache', 'slot', 'extra', 'version', 'duplicate'])
def test_persisted_model_reconstruction_rejects_tampering(prepared, change):
    _, expected, book = prepared
    raw, _ = model.capture(book, expected, lambda: None)
    value = json.loads(raw)
    if change == 'hash':
        value['semantic_sha256'] = '0' * 64
    elif change == 'literal':
        value['sheets'][0]['values'][1][1] = 99
    elif change == 'formula':
        value['sheets'][0]['formulas'][0]['expression'] = '=B2+B3'
    elif change == 'cache':
        value['sheets'][0]['formulas'][0]['result'] = 30
    elif change == 'slot':
        value['sheets'][0]['values'][3][1] = 30
    elif change == 'extra':
        value['ignored'] = True
    elif change == 'version':
        value['version'] = True
    if change == 'duplicate':
        raw = raw.replace(b'"version":1', b'"version":1,"version":1')
    else:
        raw = json.dumps(value, ensure_ascii=False).encode()
    with pytest.raises(ComputerError):
        model.serialized_model(raw)


def test_check_interrupt_is_propagated(prepared):
    _, expected, book = prepared
    calls = []
    def check():
        calls.append(1)
        if len(calls) == 15:
            raise ComputerError('cancelled')
    with pytest.raises(ComputerError, match='cancelled'):
        model.capture(book, expected, check)
    assert len(calls) == 15


def test_blank_leading_cells_capture_used_range_offset(prepared):
    _, expected, book = prepared
    # Explicit leading blanks remain part of the canonical A1-based snapshot.
    data = deepcopy(expected)
    data['sheets'] = [{'name': 'Sparse', 'range': 'A1:B2', 'state': 'visible',
                       'values': [[None, None], [None, 123]], 'formulas': []}]
    data = model._model(data['sheets'], model._source())
    book = native(data)
    used = book.sheets[0].properties['UsedRange']
    used.properties.update(Row=2, Column=2, Rows=Pointer(Count=1), Columns=Pointer(Count=1))
    raw, actual = model.capture(book, data, lambda: None)
    assert actual == data == model.serialized_model(raw)


@pytest.mark.parametrize('property_name', ['Sheets', 'Count', 'Value2'])
def test_provider_failure_diagnostics_contain_only_property_and_exception_type(prepared, property_name):
    _, expected, book = prepared
    def fail():
        raise RuntimeError('private provider path C:/secret.xlsx and cell content')
    target = (book if property_name == 'Sheets' else book.properties['Sheets'] if property_name == 'Count'
              else book.sheets[0].cells['A1'])
    target.properties[property_name] = fail
    with pytest.raises(ComputerError, match='windows_copy_native_content_unavailable') as raised:
        model.capture(book, expected, lambda: None)
    assert raised.value.copy_diagnostic == {'workbook_last_property': property_name,
                                            'workbook_exception_type': 'RuntimeError'}
    assert 'secret' not in json.dumps(raised.value.copy_diagnostic)


def test_local_validation_reason_is_fixed_and_provider_message_is_never_trusted(prepared):
    _, expected, book = prepared
    book.sheets[0].properties['Visible'] = 'private unsupported visibility'
    with pytest.raises(ComputerError) as raised:
        model.capture(book, expected, lambda: None)
    assert raised.value.copy_diagnostic == {'workbook_last_property': 'Visible',
        'workbook_exception_type': '_CaptureValidation', 'workbook_validation_reason': 'workbook_visibility_invalid'}
    def provider_value_error():
        raise ValueError('workbook_private_secret')
    book.sheets[0].properties['Visible'] = provider_value_error
    with pytest.raises(ComputerError) as raised:
        model.capture(book, expected, lambda: None)
    assert raised.value.copy_diagnostic == {'workbook_last_property': 'Visible', 'workbook_exception_type': 'ValueError'}


def test_provider_exception_class_name_is_bounded_and_sanitized(prepared):
    _, expected, book = prepared
    exception = type('private class name/path', (RuntimeError,), {})
    def fail():
        raise exception('private body')
    book.properties['Sheets'] = fail
    with pytest.raises(ComputerError) as raised:
        model.capture(book, expected, lambda: None)
    assert raised.value.copy_diagnostic == {'workbook_last_property': 'Sheets', 'workbook_exception_type': 'Exception'}
