"""Bound native content contracts using read-only fake COM, no desktop input."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_content_find as find
from agent_service import windows_document_copy as copy
from agent_service import windows_workbook_model as model


class Pointer:
    def __init__(self, trace, label, **properties):
        self.trace, self.label, self.properties = trace, label, properties

    def GetIDsOfNames(self, locale, name):
        return name

    def Invoke(self, name, locale, flag, result, *args):
        self.trace.append((self.label, name, flag, args))
        value = self.properties[name]
        return value(*args) if callable(value) else value


def effects(trace):
    return [(label, name) for label, name, flag, _ in trace
            if flag == 1 and name != 'Range']


@pytest.fixture
def native(tmp_path, monkeypatch):
    trace = []
    source, other = tmp_path / 'existing.docx', tmp_path / 'other.docx'
    source.write_bytes(b'bound native fixture')
    other.write_bytes(b'other native fixture')
    request = {'file': {'path': str(source)}, 'query': 'target', 'occurrence': 2, 'scope': 'text'}
    text = 'prefix target suffix target\r'
    content = Pointer(trace, 'content', Text=text, Start=0, End=len(text))
    pointer = Pointer(trace, 'document', FullName=str(source), Content=content)
    otherdoc = Pointer(trace, 'other-document', FullName=str(other))
    hit = find.query.word_match(text, 0, len(text), 'target', 2)[0]
    selection = Pointer(trace, 'selection', Start=hit['start'], End=hit['end'], Text=hit['text'],
                        StoryType=1, Document=pointer)
    app = Pointer(trace, 'app', Selection=selection)
    def create_range(start, end):
        return Pointer(trace, 'found', Start=start, End=end,
            Text=content.properties['Text'].encode('utf-16-le')[2 * start:2 * end].decode('utf-16-le'),
            StoryType=1, Document=pointer, Select=lambda: None)
    pointer.properties.update(Application=app, Range=create_range)
    document = NS(pointer=pointer)
    flags = {'active': True, 'read_only': True, 'saved': True}
    monkeypatch.setattr(find.query, 'decode', lambda token: deepcopy(request))
    monkeypatch.setattr(find.files, 'bound_document', lambda *args: (document, dict(flags)))
    monkeypatch.setattr(copy, '_same_document', lambda *args: ((), (), (flags['read_only'], flags['saved'])))
    return NS(trace=trace, request=request, document=document, pointer=pointer,
              selection=selection, app=app, content=content, otherdoc=otherdoc, flags=flags,
              window={}, token='approved-literal-query')


def test_word_actual_selection_verified_without_mutation(native):
    state, (found, sheet) = find._word_location(native.document, native.request, lambda: None)
    assert state['selected'] is True and state['total'] == 2 and sheet is None
    assert state['hit'] == {'start': 21, 'end': 27, 'text': 'target'}
    assert len(state['content_sha256']) == 64 and effects(native.trace) == []


def word_representation(native, raw, native_text, *, native_end=None):
    """Text may contain WPS's extra CR; Range uses independently supplied positions."""
    units = len(native_text.encode('utf-16-le')) // 2 if native_end is None else native_end
    native.content.properties.update(Text=raw, Start=0, End=units)
    def native_range(start, end):
        return Pointer(native.trace, 'found', Start=start, End=end,
            Text=native_text.encode('utf-16-le')[2 * start:2 * end].decode('utf-16-le'),
            StoryType=1, Document=native.pointer, Select=lambda: None)
    native.pointer.properties['Range'] = native_range
    return units


def test_wps_cr_cell_pairs_only_when_they_explain_entire_native_span(native):
    raw = 'cell\r\x07' * 13 + 'first target second target ' + 'x' * 16
    collapsed = raw.replace('\r\x07', '\x07')
    assert len(raw) == 121 and len(collapsed) == 108
    units = word_representation(native, raw, collapsed)
    expected, _ = find.query.word_match(collapsed, 0, units, 'target', 2)
    native.selection.properties.update(Start=expected['start'], End=expected['end'], Text='target')
    state, _ = find._word_location(native.document, native.request, lambda: None)
    assert state['selected'] is True and state['total'] == 2 and state['hit'] == expected
    assert state['content_sha256'] == find.digest({'text': raw, 'start': 0, 'end': 108})
    assert effects(native.trace) == []


def test_cr_cell_pairs_preserved_when_raw_span_already_matches(native):
    raw = 'cell\r\x07target\r\x07target\r'
    units = word_representation(native, raw, raw)
    expected, _ = find.query.word_match(raw, 0, units, 'target', 2)
    native.selection.properties.update(Start=expected['start'], End=expected['end'], Text='target')
    state, _ = find._word_location(native.document, native.request, lambda: None)
    assert state['hit'] == expected and state['selected'] is True and effects(native.trace) == []


@pytest.mark.parametrize('raw,end', [('one\rX\x07target target', 18),
    ('one\rtarget target', 16), ('one\x07target target', 16),
    ('one\r\x07two\r\x07target target', 22)])
def test_marker_compatibility_does_not_remove_isolated_or_partial_markers(native, raw, end):
    # These spans match neither raw UTF-16 nor removal of every exact CR+CELL.
    assert end != len(raw) and end != len(raw.replace('\r\x07', '\x07'))
    word_representation(native, raw, raw, native_end=end)
    with pytest.raises(ComputerError, match='windows_content_span_invalid'):
        find._word_location(native.document, native.request, lambda: None)
    assert effects(native.trace) == []
    assert not any(name == 'Range' for _, name, _, _ in native.trace)


def test_collapsed_body_still_requires_exact_native_literal_range(native):
    raw = 'cell\r\x07target and target\r'
    collapsed = raw.replace('\r\x07', '\x07')
    word_representation(native, raw, collapsed)
    original = native.pointer.properties['Range']
    def wrong_target(start, end):
        value = original(start, end)
        if start:
            value.properties['Text'] = 'wrong!'
        return value
    native.pointer.properties['Range'] = wrong_target
    with pytest.raises(ComputerError, match='windows_content_span_invalid') as error:
        find._word_location(native.document, native.request, lambda: None)
    assert error.value.content_diagnostic['stage'] == 'found_range'
    assert effects(native.trace) == []


@pytest.mark.parametrize('wrong', ['Text', 'Start', 'End', 'StoryType', 'Document'])
def test_collapsed_mapping_must_attest_exact_native_prefix(native, wrong):
    raw = 'cell\r\x07target and target\r'
    collapsed = raw.replace('\r\x07', '\x07')
    word_representation(native, raw, collapsed)
    original = native.pointer.properties['Range']
    def wrong_prefix(start, end):
        value = original(start, end)
        if start == 0:
            replacements = {'Text': 'wrong-prefix', 'Start': 1, 'End': 1,
                            'StoryType': 2, 'Document': native.otherdoc}
            value.properties[wrong] = replacements[wrong]
        return value
    native.pointer.properties['Range'] = wrong_prefix
    with pytest.raises(ComputerError, match='windows_content_span_invalid') as error:
        find._word_location(native.document, native.request, lambda: None)
    assert error.value.content_diagnostic['stage'] == 'prefix_range'
    assert effects(native.trace) == []


def test_native_prefix_may_include_raw_cr_cell_pairs_and_preserves_ordinal(native):
    raw = 'cell\r\x07target and target\r'
    collapsed = raw.replace('\r\x07', '\x07')
    units = word_representation(native, raw, collapsed)
    expected, _ = find.query.word_match(collapsed, 0, units, 'target', 2)
    original = native.pointer.properties['Range']
    def raw_prefix(start, end):
        value = original(start, end)
        if start == 0:
            value.properties['Text'] = raw[:raw.rindex('target')]
        return value
    native.pointer.properties['Range'] = raw_prefix
    native.selection.properties.update(Start=expected['start'], End=expected['end'], Text='target')
    state, _ = find._word_location(native.document, native.request, lambda: None)
    assert state['hit'] == expected and state['selected'] is True and state['total'] == 2
    assert effects(native.trace) == []


@pytest.mark.parametrize('invalid', ['surrogate', 'oversize'])
def test_word_native_body_guard_refuses_invalid_unicode_and_bounds_before_encoding(native, monkeypatch, invalid):
    text = '\ud800' if invalid == 'surrogate' else 'x' * 100_001
    native.content.properties.update(Text=text, Start=0, End=len(text))
    original = find.query._utf16_length
    def bounded_length(value):
        assert len(value) <= 100_000
        return original(value)
    monkeypatch.setattr(find.query, '_utf16_length', bounded_length)
    with pytest.raises(ComputerError, match='windows_content_span_invalid'):
        find._word_location(native.document, native.request, lambda: None)
    assert effects(native.trace) == []
    assert not any(name == 'Range' for _, name, _, _ in native.trace)


@pytest.mark.parametrize('field,value', [('Start', 0), ('End', 1), ('Text', 'wrong'), ('StoryType', 2)])
def test_word_wrong_actual_selection_is_not_verified(native, field, value):
    native.selection.properties[field] = value
    state, _ = find._word_location(native.document, native.request, lambda: None)
    assert state['selected'] is False and effects(native.trace) == []


def test_word_selection_from_another_document_is_not_verified(native):
    native.selection.properties['Document'] = native.otherdoc
    assert find._word_location(native.document, native.request, lambda: None)[0]['selected'] is False
    assert effects(native.trace) == []


@pytest.mark.parametrize('field,value,error', [('Start', 0, 'windows_content_span_invalid'),
    ('Text', 'wrong', 'windows_content_span_invalid'), ('StoryType', 2, 'windows_content_span_invalid'),
    ('Document', None, 'windows_content_selection_wrong_file')])
def test_word_target_must_exist_as_exact_native_main_story_range(native, field, value, error):
    original = native.pointer.properties['Range']
    def wrong_range(start, end):
        found = original(start, end)
        found.properties[field] = native.otherdoc if field == 'Document' else value
        return found
    native.pointer.properties['Range'] = wrong_range
    with pytest.raises(ComputerError, match=error):
        find._word_location(native.document, native.request, lambda: None)
    assert effects(native.trace) == []


def test_word_no_hit_fails_before_native_selection(native):
    native.request['query'] = 'absent'
    with pytest.raises(ComputerError, match='windows_content_match_missing'):
        find._once(native.window, native.token, lambda: None)
    assert effects(native.trace) == []
    assert not any(name == 'Range' for _, name, _, _ in native.trace)


def test_readonly_saved_document_is_permitted(native):
    assert find._once(native.window, native.token, lambda: None)[0]['selected'] is True
    assert native.flags['read_only'] is True and effects(native.trace) == []


def test_unsaved_document_rejected_before_content_read(native):
    native.flags['saved'] = False
    with pytest.raises(ComputerError, match='windows_document_unsaved_changes'):
        find._once(native.window, native.token, lambda: None)
    assert native.trace == []


def test_unsaved_after_content_read_is_rejected(native, monkeypatch):
    monkeypatch.setattr(copy, '_same_document', lambda *args: ((), (), (True, False)))
    with pytest.raises(ComputerError, match='windows_document_unsaved_changes'):
        find._once(native.window, native.token, lambda: None)
    assert effects(native.trace) == []


def test_content_change_between_inspections_is_rejected_without_mutation(native):
    original = native.pointer.properties['Range']
    calls = []
    def change_after_range(start, end):
        found = original(start, end)
        calls.append(1)
        if len(calls) == 1:
            native.content.properties['Text'] = 'PREFIX target suffix target\r'
        return found
    native.pointer.properties['Range'] = change_after_range
    with pytest.raises(ComputerError, match='windows_content_changed'):
        find.inspect(native.window, native.token, lambda: None)
    assert effects(native.trace) == []


def test_selection_change_between_inspections_is_rejected(native, monkeypatch):
    original = find._once
    calls = []
    def once(*args):
        state = original(*args)
        calls.append(1)
        native.selection.properties['Text'] = 'changed'
        return state
    monkeypatch.setattr(find, '_once', once)
    with pytest.raises(ComputerError, match='windows_content_changed'):
        find.inspect(native.window, native.token, lambda: None)
    assert effects(native.trace) == []


def test_preflight_is_readonly_and_rejects_changed_expected(native):
    state = find.inspect(native.window, native.token, lambda: None)
    find.preflight(native.window, native.token, state, lambda: None)
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        find.preflight(native.window, native.token, dict(state, total=99), lambda: None)
    assert effects(native.trace) == []


def test_word_select_only_invokes_exact_found_range(native):
    native.selection.properties['Text'] = ''
    state = find.inspect(native.window, native.token, lambda: None)
    native.trace.clear()
    find.select_match(native.window, native.token, state, lambda: None)
    assert effects(native.trace) == [('found', 'Select')]


def test_stale_selection_request_has_no_mutation(native):
    state = find.inspect(native.window, native.token, lambda: None)
    with pytest.raises(ComputerError, match='screen_changed_reobserve'):
        find.select_match(native.window, native.token, dict(state, total=99), lambda: None)
    assert effects(native.trace) == []


@pytest.fixture
def excel(native, monkeypatch):
    path = native.request['file']['path'].replace('.docx', '.xlsx')
    from pathlib import Path
    Path(path).write_bytes(b'bound workbook fixture')
    native.request.update(file={'path': path}, query='target', occurrence=1, scope='text')
    native.pointer.properties['FullName'] = path
    raw = {'version': 1, 'semantic_sha256': 'a' * 64, 'sheets': [
        {'name': 'Data', 'state': 'visible', 'range': 'A1:B2',
         'values': [['target', 1], [None, 'other']],
         'formulas': [{'cell': 'A2', 'expression': '=sum(B1:B1)', 'result': None}]}]}
    normalized = deepcopy(raw)
    normalized['sheets'][0]['formulas'][0]['expression'] = '=SUM(B1:B1)'
    monkeypatch.setattr(model, 'capture', lambda *args: (json.dumps(raw).encode(), deepcopy(normalized)))
    sheet = Pointer(native.trace, 'sheet', Name='Data', Visible=-1, Parent=native.pointer,
                    Activate=lambda: None)
    def cell(label, address, formula=False, value='target'):
        return Pointer(native.trace, label, Worksheet=sheet, Address=address, Count=1,
            Areas=Pointer(native.trace, label + '-areas', Count=1), HasFormula=formula,
            Value2=value, Formula='=sum(B1:B1)' if formula else value, Select=lambda: None)
    found_value = cell('found-value', '$A$1')
    found_formula = cell('found-formula', '$A$2', True, None)
    sheet.properties['Range'] = lambda address: {'A1': found_value, 'A2': found_formula}[address]
    selection, active = cell('selection', '$A$1'), cell('active', '$A$1')
    native.app.properties.update(Selection=selection, ActiveCell=active, ActiveSheet=sheet)
    native.pointer.properties['Worksheets'] = Pointer(native.trace, 'sheets', Item=lambda name: sheet)
    native.excel_sheet, native.excel_selection, native.excel_active = sheet, selection, active
    native.excel_found, native.excel_formula, native.raw_model = found_value, found_formula, raw
    return native


def test_excel_actual_single_cell_and_active_cell_verified_without_mutation(excel):
    state, (found, sheet) = find._excel_location(excel.document, excel.request, lambda: None)
    assert state['selected'] is True and state['total'] == 1
    assert state['hit'] == {'sheet': 'Data', 'address': 'A1', 'text': 'target', 'field': 'value'}
    assert found is excel.excel_found and sheet is excel.excel_sheet and effects(excel.trace) == []


@pytest.mark.parametrize('which,field,value', [('selection', 'Address', '$B$1'),
    ('active', 'Address', '$B$1'), ('selection', 'Count', 2), ('active', 'Count', 2),
    ('selection', 'areas', 2), ('selection', 'Value2', 'wrong'), ('active', 'Value2', 'wrong'),
    ('selection', 'HasFormula', True), ('active', 'HasFormula', True)])
def test_excel_wrong_actual_cell_facts_never_verify(excel, which, field, value):
    target = excel.excel_selection if which == 'selection' else excel.excel_active
    if field == 'areas':
        target.properties['Areas'].properties['Count'] = value
    else:
        target.properties[field] = value
    assert find._excel_location(excel.document, excel.request, lambda: None)[0]['selected'] is False
    assert effects(excel.trace) == []


@pytest.mark.parametrize('wrong', ['document', 'sheet', 'active-sheet'])
def test_excel_selection_must_belong_to_exact_workbook_and_sheet(excel, wrong):
    if wrong == 'active-sheet':
        excel.app.properties['ActiveSheet'] = Pointer(excel.trace, 'other-sheet', Name='Other')
    else:
        props = dict(excel.excel_sheet.properties)
        props['Parent' if wrong == 'document' else 'Name'] = excel.otherdoc if wrong == 'document' else 'Other'
        excel.excel_selection.properties['Worksheet'] = Pointer(excel.trace, 'wrong-sheet', **props)
    assert find._excel_location(excel.document, excel.request, lambda: None)[0]['selected'] is False
    assert effects(excel.trace) == []


def test_excel_formula_query_uses_actual_native_expression_spelling(excel):
    excel.request.update(query='sum', scope='formula')
    for target in (excel.excel_selection, excel.excel_active):
        target.properties.update(Address='$A$2', HasFormula=True, Formula='=sum(B1:B1)', Value2=1)
    state, _ = find._excel_location(excel.document, excel.request, lambda: None)
    assert state['selected'] is True
    assert state['hit']['text'] == '=sum(B1:B1)' and state['hit']['field'] == 'formula'
    assert effects(excel.trace) == []


@pytest.mark.parametrize('which', ['selection', 'active'])
def test_excel_actual_formula_expression_must_match(excel, which):
    excel.request.update(query='sum', scope='formula')
    for target in (excel.excel_selection, excel.excel_active):
        target.properties.update(Address='$A$2', HasFormula=True, Formula='=sum(B1:B1)')
    target = excel.excel_selection if which == 'selection' else excel.excel_active
    target.properties['Formula'] = '=SUM(B1:B1)'
    assert find._excel_location(excel.document, excel.request, lambda: None)[0]['selected'] is False
    assert effects(excel.trace) == []


@pytest.mark.parametrize('field,value', [('HasFormula', True), ('HasFormula', 0), ('Value2', 'wrong')])
def test_excel_found_cell_must_agree_with_whole_native_capture(excel, field, value):
    excel.excel_found.properties[field] = value
    with pytest.raises(ComputerError, match='windows_content_changed'):
        find._excel_location(excel.document, excel.request, lambda: None)
    assert effects(excel.trace) == []


def test_excel_no_hit_does_not_select(excel):
    excel.request['query'] = 'missing'
    with pytest.raises(ComputerError, match='windows_content_match_missing'):
        find._excel_location(excel.document, excel.request, lambda: None)
    assert effects(excel.trace) == []


def test_excel_select_only_activates_target_sheet_and_selects_exact_range(excel):
    excel.excel_selection.properties['Value2'] = ''
    state = find.inspect(excel.window, excel.token, lambda: None)
    excel.trace.clear()
    find.select_match(excel.window, excel.token, state, lambda: None)
    assert effects(excel.trace) == [('sheet', 'Activate'), ('found-value', 'Select')]


def test_excel_sheet_activation_must_be_verified_before_range_selection(excel):
    state = find.inspect(excel.window, excel.token, lambda: None)
    def activate_wrong():
        excel.app.properties['ActiveSheet'] = Pointer(excel.trace, 'other-sheet', Name='Other')
    excel.excel_sheet.properties['Activate'] = activate_wrong
    with pytest.raises(ComputerError, match='windows_content_selection_wrong_sheet'):
        find.select_match(excel.window, excel.token, state, lambda: None)
    assert effects(excel.trace) == [('sheet', 'Activate')]


def test_native_failure_retains_fixed_property_diagnostic(native):
    with pytest.raises(ComputerError, match='windows_content_native_unavailable') as error:
        find._get(native.pointer, 'Missing')
    assert error.value.content_diagnostic == {'property': 'Missing', 'method': False}
