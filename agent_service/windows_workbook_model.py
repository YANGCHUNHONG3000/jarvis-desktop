"""Bounded XLSX formula-preservation evidence for an existing workbook.

No Office instances or methods are created/invoked. Native reads use only
IDispatch PROPERTYGET; formula cached values are deliberately never evidence.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import re
import sys

from .computer import ComputerError

_LIMIT = 300_000


def ensure_dependencies():
    """Reuse only exact packages in this interpreter's base installation."""
    from .windows_document_copy import _ensure_lxml, _path
    _ensure_lxml()
    for name in ('et_xmlfile', 'openpyxl'):
        if importlib.util.find_spec(name) is not None:
            continue
        site = _path(Path(sys.base_prefix) / 'Lib' / 'site-packages')
        package = _path(site / name)
        spec = importlib.machinery.PathFinder.find_spec(name, [str(site)])
        if spec is None:
            raise ComputerError('windows_copy_dependency_unavailable')
        try:
            if (spec.name != name or spec.loader is None
                    or _path(spec.origin) != package / '__init__.py'
                    or list(spec.submodule_search_locations or []) != [str(package)]):
                raise ValueError()
            previous = {key: value for key, value in sys.modules.items()
                        if key == name or key.startswith(name + '.')}
            try:
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                if list(module.__path__) != [str(package)] or _path(module.__file__) != package / '__init__.py':
                    raise ValueError()
            except Exception:
                for key in list(sys.modules):
                    if (key == name or key.startswith(name + '.')) and key not in previous:
                        sys.modules.pop(key, None)
                sys.modules.update(previous)
                raise
        except Exception as exc:
            raise ComputerError('windows_copy_dependency_invalid') from exc


def _source():
    ensure_dependencies()
    from . import workbook_source
    return workbook_source


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _formula(expression, sheet, bounds, source):
    """Use the established native-formula identity, preserving anchors/order."""
    parser = source._Parser(expression, sheet, bounds)
    parser.parse()
    tokens = []
    for token in parser.tokens:
        value = token.value
        if token.type == 'OPERAND' and token.subtype == 'RANGE':
            quoted, plain, first, last = source._REF.fullmatch(value).groups()
            name = quoted.replace("''", "'") if quoted is not None else plain
            prefix = "'" + name.casefold().replace("'", "''") + "'!" if name is not None else ''
            value = prefix + first.upper() + (':' + last.upper() if last is not None else '')
        tokens.append(value)
    result = '=' + ''.join(tokens)
    if not source._same_native_formula(result, expression, sheet, bounds):
        raise ValueError('formula_identity_invalid')
    return result


def _model(sheets, source):
    # _analyze supplies the existing bounds, grammar, cycles and scalar policy.
    # Its independently computed results are discarded, never cached evidence.
    analyzed, _ = source._analyze(sheets)
    bounds = {sheet['name']: (len(sheet['values']), len(sheet['values'][0])) for sheet in analyzed}
    from openpyxl.utils.cell import coordinate_to_tuple
    for sheet in analyzed:
        for row in sheet['values']:
            for i, value in enumerate(row):
                if type(value) is float and value.is_integer():
                    row[i] = int(value)
        for formula in sheet['formulas']:
            formula['expression'] = _formula(formula['expression'], sheet['name'], bounds, source)
            formula['result'] = None
            row, column = coordinate_to_tuple(formula['cell'])
            sheet['values'][row - 1][column - 1] = None
        sheet['formulas'].sort(key=lambda entry: coordinate_to_tuple(entry['cell']))
    body = {'version': 1, 'sheets': analyzed}
    result = dict(body, semantic_sha256=sha256(_bytes(body)).hexdigest())
    if len(_bytes(result)) > _LIMIT:
        raise ValueError('workbook_observation_limit')
    return result


def disk_model(path):
    """Validate a supported bounded XLSX; retain formulas, never stored caches."""
    try:
        source = _source()
        observed, _ = source._parse(path)
        return _model(observed['sheets'], source)
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_copy_content_unsupported') from exc


def serialized_model(data):
    """Reconstruct and validate complete persisted native evidence offline."""
    try:
        if type(data) is not bytes or not 0 < len(data) <= _LIMIT:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(data, object_pairs_hook=unique)
        if (type(value) is not dict or set(value) != {'version', 'sheets', 'semantic_sha256'}
                or type(value['version']) is not int or value['version'] != 1):
            raise ValueError()
        model = _model(value['sheets'], _source())
        from openpyxl.utils.cell import coordinate_to_tuple
        for sheet in value['sheets']:
            for formula in sheet['formulas']:
                row, column = coordinate_to_tuple(formula['cell'])
                if formula['result'] is not None or sheet['values'][row - 1][column - 1] is not None:
                    raise ValueError()
        if model['semantic_sha256'] != value['semantic_sha256']:
            raise ValueError()
        return model
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_copy_native_content_invalid') from exc


def _get(pointer, name, *args):
    member = pointer.GetIDsOfNames(0, name)
    return pointer.Invoke(member, 0, 2, 1, *args)


class _CaptureValidation(ValueError):
    """Only fixed local validation reasons may reach capture diagnostics."""


def _numeric_text_equivalent(expression, value):
    """WPS may expose literal numeric Formula as text and Value2 as float."""
    if (type(expression) is not str or not 0 < len(expression) <= 100
            or type(value) not in (int, float)
            or re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', expression) is None):
        return False
    try:
        number, actual = Decimal(expression), Decimal(str(value))
        return (number.is_finite() and actual.is_finite()
                and (number.is_zero() or abs(number.adjusted()) <= 100) and number == actual)
    except (InvalidOperation, ValueError):
        return False


def _value_type(value):
    kind = type(value)
    return kind.__name__ if kind in (str, int, float, bool, type(None)) else 'Other'


def _count(pointer, maximum, read=_get):
    value = read(pointer, 'Count')
    if type(value) is not int or not 0 <= value <= maximum:
        raise _CaptureValidation('workbook_collection_limit')
    return value


def capture(book_pointer, expected_model_or_path, check):
    """Read every bounded native cell and compare formula identity and literals.

    The caller supplies a bound existing IDispatch pointer and owns COM lifetime,
    input guards and deadlines. ``check`` runs between bounded property reads.
    """
    check()
    expected = (serialized_model(_bytes(expected_model_or_path)) if type(expected_model_or_path) is dict
                else disk_model(expected_model_or_path))
    source = _source()
    from openpyxl.utils.cell import get_column_letter
    progress = {'last_property': None}
    try:
        def get(pointer, name, *args):
            check()
            progress['last_property'] = name  # All callers supply fixed literals.
            return _get(pointer, name, *args)
        def count_of(pointer, maximum):
            return _count(pointer, maximum, get)
        # Sheets includes chart sheets; equal worksheet count excludes them.
        sheets = get(book_pointer, 'Sheets')
        worksheets = get(book_pointer, 'Worksheets')
        count = count_of(sheets, 8)
        if not count or count != count_of(worksheets, 8) or count != len(expected['sheets']):
            raise _CaptureValidation('workbook_sheet_mismatch')
        if count_of(get(book_pointer, 'Names'), 0):
            raise _CaptureValidation('workbook_names_unsupported')
        observed, cells = [], 0
        for index, target in enumerate(expected['sheets'], 1):
            sheet = get(sheets, 'Item', index)
            name = get(sheet, 'Name')
            state = get(sheet, 'Visible')
            if type(state) is not int or state not in {-1, 0, 2}:
                raise _CaptureValidation('workbook_visibility_invalid')
            state = {-1: 'visible', 0: 'hidden', 2: 'veryHidden'}[state]
            if name != target['name'] or state != target['state']:
                raise _CaptureValidation('workbook_sheet_mismatch')
            if count_of(get(sheet, 'Names'), 0):
                raise _CaptureValidation('workbook_names_unsupported')
            used = get(sheet, 'UsedRange')
            row, column = get(used, 'Row'), get(used, 'Column')
            rows, columns = count_of(get(used, 'Rows'), 201), count_of(get(used, 'Columns'), 12)
            if (type(row) is not int or type(column) is not int or min(row, column, rows, columns) < 1
                    or row + rows - 1 > 201 or column + columns - 1 > 12):
                raise _CaptureValidation('workbook_grid_limit')
            rows, columns = row + rows - 1, column + columns - 1
            cells += rows * columns
            if cells > source.MAX_CELLS:
                raise _CaptureValidation('workbook_cell_limit')
            area = f'A1:{get_column_letter(columns)}{rows}'
            if area != target['range']:
                raise _CaptureValidation('workbook_range_mismatch')
            grid, formulas = [], []
            for r in range(1, rows + 1):
                values = []
                for c in range(1, columns + 1):
                    address = f'{get_column_letter(c)}{r}'
                    cell = get(sheet, 'Range', address)
                    if get(cell, 'MergeCells') is not False:
                        raise _CaptureValidation('workbook_merged_cells_unsupported')
                    has_formula = get(cell, 'HasFormula')
                    expression = get(cell, 'Formula')
                    if type(has_formula) is not bool:
                        raise _CaptureValidation('workbook_formula_flag_invalid')
                    if has_formula:
                        if type(expression) is not str or not expression.startswith('='):
                            raise _CaptureValidation('workbook_formula_invalid')
                        formulas.append({'cell': address, 'expression': expression, 'result': None})
                        values.append(None)
                    else:
                        value = get(cell, 'Value2')
                        # Formula for a literal must agree with Value2, including
                        # strings beginning '=' that remain literal text.
                        if value is None and expression in (None, ''):
                            pass  # Excel may expose an empty Formula for a blank cell.
                        elif type(expression) is not type(value) or expression != value:
                            if not (type(expression) in (int, float) and type(value) in (int, float)
                                    and expression == value) and not _numeric_text_equivalent(expression, value):
                                error = _CaptureValidation('workbook_formula_flag_mismatch')
                                error.diagnostic = {'workbook_formula_type': _value_type(expression),
                                    'workbook_value_type': _value_type(value),
                                    'workbook_numeric_text_equivalent': _numeric_text_equivalent(expression, value)}
                                raise error
                        values.append(value)
                grid.append(values)
            observed.append({'name': name, 'range': area, 'state': state, 'values': grid, 'formulas': formulas})
        actual = _model(observed, source)
        if actual != expected:
            raise ComputerError('windows_copy_native_content_mismatch')
    except ComputerError:
        raise
    except Exception as exc:
        error = ComputerError('windows_copy_native_content_unavailable')
        kind = type(exc).__name__
        diagnostic = {'workbook_last_property': progress['last_property'],
                      'workbook_exception_type': kind if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', kind)
                      else 'Exception'}
        if type(exc) is _CaptureValidation:
            diagnostic['workbook_validation_reason'] = exc.args[0]
            diagnostic.update(getattr(exc, 'diagnostic', {}))
        error.copy_diagnostic = diagnostic
        raise error from exc
    check()
    # Preserve the actual native expression spelling as raw evidence. Rebuilding
    # this record normalizes only the established native-formula identity.
    evidence = {'version': 1, 'sheets': observed, 'semantic_sha256': actual['semantic_sha256']}
    return _bytes(evidence), deepcopy(actual)
