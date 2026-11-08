"""Passive receipt-to-edit mapping; synthetic task evidence, no native input."""
from copy import deepcopy
import pytest
from lxml import etree as ET
from openpyxl import Workbook

from agent_service import windows_located_edit as located
from agent_service import windows_content_query as query, windows_content_find as find
from agent_service import windows_task as wt, office_edit as edit
from agent_service import windows_workbook_model as model
from agent_service.computer import ComputerError, digest
from test_windows_task import Backend, WINDOW, goal, control
from test_office_edit import word_fixture
from test_office_complex_table_edit import complex_fixture, write_parts


def _word(path, literal, occurrence=1, collapsed=False):
    token = query.encode(path, literal, occurrence)
    raw = edit._word_native_stream(edit._word_body(edit._parts(path)))
    text = raw.replace('\r\x07', '\x07') if collapsed else raw
    hit, total = query.word_match(text, 0, query._utf16_length(text), literal, occurrence)
    return token, {'hit': hit, 'total': total, 'selected': True,
                   'content_sha256': digest({'text': raw, 'start': 0, 'end': query._utf16_length(text)})}


def _excel(path, literal, scope='text', occurrence=1):
    token = query.encode(path, literal, occurrence, scope)
    disk = model.disk_model(path)
    hit, total = query.excel_match(disk, literal, occurrence, scope)
    return token, {'hit': hit, 'total': total, 'selected': True, 'content_sha256': digest(disk)}


def _task(tmp_path, token, match, *, extra=None):
    condition = {'id': 'match', 'window': deepcopy(WINDOW), 'selector': deepcopy(find.SELECTOR),
                 'state': 'value', 'value': token, 'retain': False}
    class Passive(Backend):
        def observe(self, window, **kwargs):
            value = super().observe(window, **kwargs)
            value.update(application='wps', target_selector=deepcopy(find.SELECTOR), dialogs=[],
                         content_token=token, content_match=deepcopy(match))
            value.update(extra or {})
            return value
    root = wt.create(tmp_path / 'tasks', goal(condition))
    backend = Passive(control(**find.SELECTOR, value=token))
    assert wt.run(root, backend)['ok']
    assert backend.calls == []
    return root


def _reference(tmp_path, path, literal, *, occurrence=1, collapsed=False, scope='text'):
    token, match = (_word(path, literal, occurrence, collapsed) if path.suffix == '.docx'
                    else _excel(path, literal, scope, occurrence))
    root = _task(tmp_path, token, match)
    return root, located.capture(root, 'match')


def test_body_substring_maps_without_writing_sources_or_evidence(tmp_path):
    path = tmp_path / 'original.docx'
    word_fixture(path)
    root, ref = _reference(tmp_path, path, '待确认')
    original, evidence = path.read_bytes(), wt._inventory(root)
    assert located.verify(ref) == ref
    assert located.derive(ref, '已确认') == {'path': str(path), 'changes': [
        {'paragraph_index': 1, 'before': '预算待确认', 'after': '预算已确认'}]}
    assert path.read_bytes() == original and wt._inventory(root) == evidence
    assert list(path.parent.glob('**/source.json')) == []
    assert located.choices(root) == [{'condition_id': 'match', 'ref': ref, 'editable': True, 'reason': ''}]


def _mutate(path, operation):
    parts = edit._parts(path)
    tree = edit._xml(parts['word/document.xml'])
    operation(tree)
    parts['word/document.xml'] = ET.tostring(tree)
    write_parts(path, parts)


@pytest.mark.parametrize('collapsed', [False, True])
def test_nested_and_merged_paragraph_coordinates(tmp_path, collapsed):
    path = tmp_path / 'nested.docx'
    complex_fixture(path)
    root, ref = _reference(tmp_path, path, 'Nested', collapsed=collapsed)
    change = located.derive(ref, 'Nested final')['changes'][0]
    assert change == {'table_index': 1, 'row_index': 0, 'column_index': 0,
                      'paragraph_index': 0, 'before': 'Nested', 'after': 'Nested final'}
    # Same merged-grid source, separate independently attested location.
    token, match = _word(path, 'Horizontal', collapsed=collapsed)
    other = _task(tmp_path / 'merged', token, match)
    merged = located.derive(located.capture(other, 'match'), 'Horizontal final')['changes'][0]
    assert merged['table_index'] == 0 and merged['column_index'] == 0
    assert merged['before'] == 'Horizontal'


def test_repeated_literal_and_utf16_select_exact_second_paragraph(tmp_path):
    path = tmp_path / 'repeated.docx'
    word_fixture(path)
    def duplicate(tree):
        paragraphs = tree.findall(f'.//{{{edit.W}}}body/{{{edit.W}}}p')
        for paragraph in paragraphs:
            paragraph.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = '😀same same'
    _mutate(path, duplicate)
    _, ref = _reference(tmp_path, path, 'same', occurrence=4)
    change = located.derive(ref, 'new')['changes'][0]
    assert change == {'paragraph_index': 1, 'before': '😀same same', 'after': '😀same new'}


def test_single_node_guard_is_retained(tmp_path):
    path = tmp_path / 'mixed.docx'
    word_fixture(path, complex_target=True)
    _, ref = _reference(tmp_path, path, '待确认复杂')
    with pytest.raises(edit.EditError, match='edit_word_complex'):
        located.derive(ref, '更新')


@pytest.fixture
def workbook(tmp_path):
    path = tmp_path / 'original.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = '预算'
    sheet['B2'], sheet['B3'], sheet['B4'] = 10, 20, '=SUM(B2:B3)'
    second = book.create_sheet('保留页')
    second['A1'] = '原始说明文字'
    book.save(path)
    book.close()
    return path


def test_excel_replaces_only_attested_whole_cell_and_keeps_scalar_type(tmp_path, workbook):
    _, ref = _reference(tmp_path, workbook, '说明')
    assert located.derive(ref, '更新说明')['changes'] == [
        {'sheet': '保留页', 'cell': 'A1', 'before': '原始说明文字', 'after': '更新说明'}]
    token, match = _excel(workbook, '10')
    root = _task(tmp_path / 'numeric', token, match)
    change = located.derive(located.capture(root, 'match'), 15)['changes'][0]
    assert type(change['before']) is int and change['before'] == 10


def test_formula_location_explains_refusal(tmp_path, workbook):
    root, ref = _reference(tmp_path, workbook, 'SUM', scope='formula')
    assert located.choices(root)[0]['editable'] is False
    assert located.choices(root)[0]['reason'] == 'windows_located_edit_formula_forbidden'
    with pytest.raises(ComputerError, match='formula_forbidden'):
        located.derive(ref, 30)


@pytest.mark.parametrize('after', ['=1+1', '原始说明文字'])
def test_formula_replacement_and_noop_rejected(tmp_path, workbook, after):
    _, ref = _reference(tmp_path, workbook, '说明')
    with pytest.raises(edit.EditError):
        located.derive(ref, after)


@pytest.mark.parametrize('field', sorted(located._FIELDS))
def test_reference_fields_cannot_be_swapped(tmp_path, workbook, field):
    _, ref = _reference(tmp_path, workbook, '说明')
    changed = deepcopy(ref)
    if field == 'match':
        changed[field]['hit']['address'] = 'A2'
    elif field == 'schema':
        changed[field] = True
    else:
        changed[field] = 'swapped'
    with pytest.raises((ComputerError, OSError, ValueError)):
        located.verify(changed)


def test_additional_field_and_new_parent_evidence_rejected(tmp_path, workbook):
    root, ref = _reference(tmp_path, workbook, '说明')
    with pytest.raises(ComputerError):
        located.verify({**ref, 'extra': None})
    (root / 'unexpected.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError, match='source_changed'):
        located.verify(ref)


def test_changed_source_is_not_resnapshotted(tmp_path, workbook):
    _, ref = _reference(tmp_path, workbook, '说明')
    workbook.write_bytes(workbook.read_bytes() + b'changed')
    with pytest.raises(ComputerError, match='windows_file_changed'):
        located.derive(ref, 'new')


@pytest.mark.parametrize('case', ['missing', 'not_selected', 'fake_application', 'fake_hash', 'wrong_position'])
def test_content_receipt_requires_full_native_match_and_exact_disk_mapping(tmp_path, case):
    path = tmp_path / 'original.docx'
    word_fixture(path)
    token, match = _word(path, '预算')
    extra = {}
    if case == 'missing':
        match = None
    elif case == 'not_selected':
        match['selected'] = False
    elif case == 'fake_application':
        extra['application'] = 'explorer'
    elif case == 'fake_hash':
        match['content_sha256'] = '0' * 64
    elif case == 'wrong_position':
        match['hit']['start'] += 1
        match['hit']['end'] += 1
    root = _task(tmp_path, token, match, extra=extra)
    with pytest.raises(ComputerError):
        located.derive(located.capture(root, 'match'), 'new')


def test_unfinished_parent_is_not_a_location_source(tmp_path, workbook):
    token, match = _excel(workbook, '说明')
    condition = {'id': 'match', 'window': deepcopy(WINDOW), 'selector': deepcopy(find.SELECTOR),
                 'state': 'value', 'value': token, 'retain': False}
    root = wt.create(tmp_path / 'tasks', goal(condition))
    with pytest.raises(ComputerError, match='source_incomplete'):
        located.capture(root, 'match')


def test_unknown_parent_never_yields_edit_choices(tmp_path, workbook):
    token, match = _excel(workbook, '说明')
    condition = {'id': 'match', 'window': deepcopy(WINDOW), 'selector': deepcopy(find.SELECTOR),
                 'state': 'value', 'value': token, 'retain': False}
    root = wt.create(tmp_path / 'tasks', goal(condition))
    backend = Backend(control(**find.SELECTOR, value=''))
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    assert wt.run(root, backend)['state'] == 'unknown'
    for operation in (lambda: located.capture(root, 'match'), lambda: located.choices(root)):
        with pytest.raises(ComputerError, match='source_unknown'):
            operation()


def test_another_equal_literal_position_cannot_replace_token_occurrence(tmp_path):
    path = tmp_path / 'repeated.docx'
    word_fixture(path)
    def duplicate(tree):
        for p in tree.findall(f'.//{{{edit.W}}}body/{{{edit.W}}}p'):
            p.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = 'same'
    _mutate(path, duplicate)
    token, match = _word(path, 'same', 2)
    first = _word(path, 'same', 1)[1]
    match['hit'] = first['hit']
    root = _task(tmp_path, token, match)
    with pytest.raises(ComputerError, match='body_mismatch'):
        located.derive(located.capture(root, 'match'), 'new')


@pytest.mark.parametrize('literal,coordinates', [
    ('Vertical', (0, 1, 0, 0)), ('After nested', (0, 1, 1, 1)), ('R0C2', (0, 0, 2, 0))])
def test_vertical_merge_anchor_and_post_nested_paragraph(tmp_path, literal, coordinates):
    path = tmp_path / 'merged.docx'
    complex_fixture(path)
    _, ref = _reference(tmp_path, path, literal, collapsed=True)
    change = located.derive(ref, literal + ' updated')['changes'][0]
    assert tuple(change[k] for k in edit._WORD_COORDINATES) == coordinates


def test_tampered_completed_event_is_rejected_by_scan(tmp_path, workbook):
    root, ref = _reference(tmp_path, workbook, '说明')
    event = root / ref['event']
    event.write_bytes(event.read_bytes().replace(b'content_sha256', b'content_sha256_changed'))
    with pytest.raises(ComputerError, match='windows_evidence_changed'):
        located.verify(ref)


def test_excel_match_cannot_point_to_other_cell(tmp_path, workbook):
    token, match = _excel(workbook, '说明')
    match['hit']['address'] = 'B2'
    root = _task(tmp_path, token, match)
    with pytest.raises(ComputerError, match='body_mismatch'):
        located.derive(located.capture(root, 'match'), 'new')


@pytest.mark.parametrize('representation', ['float', 'formula_spelling', 'both'])
def test_excel_raw_native_digest_is_bound_without_equating_canonical_digest(tmp_path, workbook, representation):
    # Production capture compares canonical models, then persists raw native
    # spelling. Both differ in bytes while representing the complete same file.
    from openpyxl import load_workbook
    book = load_workbook(workbook)
    book['保留页']['B1'] = '=预算!B2'
    book.save(workbook)
    book.close()
    canonical = model.disk_model(workbook)
    raw = deepcopy(canonical)
    if representation in {'float', 'both'}:
        raw['sheets'][0]['values'][1][1] = 10.0
    if representation in {'formula_spelling', 'both'}:
        formula = raw['sheets'][1]['formulas'][0]
        formula['expression'] = formula['expression'].replace("'预算'!", '预算!')
    assert model.serialized_model(model._bytes(raw)) == canonical
    assert digest(raw) != digest(canonical)
    token = query.encode(workbook, '10')
    hit, total = query.excel_match(raw, '10')
    match = {'selected': True, 'hit': hit, 'total': total, 'content_sha256': digest(raw)}
    root = _task(tmp_path, token, match)
    ref = located.capture(root, 'match')
    assert ref['match']['content_sha256'] == digest(raw)
    change = located.derive(ref, 15)['changes'][0]
    assert change == {'sheet': '预算', 'cell': 'B2', 'before': 10, 'after': 15}
    assert type(change['before']) is int
    altered = deepcopy(ref)
    altered['match']['content_sha256'] = digest(canonical)
    with pytest.raises(ComputerError, match='source_changed'):
        located.verify(altered)


@pytest.mark.parametrize('value', ['10', True, 10.5])
def test_native_type_and_value_changes_are_not_model_normalization(workbook, value):
    canonical = model.disk_model(workbook)
    changed = deepcopy(canonical)
    changed['sheets'][0]['values'][1][1] = value
    # Native capture would reject this complete model rather than issue the
    # successful match relied on by the bridge. Only 10.0 normalizes to 10.
    with pytest.raises(ComputerError, match='windows_copy_native_content_invalid'):
        model.serialized_model(model._bytes(changed))


def test_formula_raw_spelling_still_has_an_explicit_edit_refusal(tmp_path, workbook):
    from openpyxl import load_workbook
    book = load_workbook(workbook)
    book['保留页']['B1'] = '=预算!B2'
    book.save(workbook)
    book.close()
    raw = model.disk_model(workbook)
    formula = raw['sheets'][1]['formulas'][0]
    formula['expression'] = formula['expression'].replace("'预算'!", '预算!')
    token = query.encode(workbook, '预算', scope='formula')
    hit, total = query.excel_match(raw, '预算', scope='formula')
    root = _task(tmp_path, token, {'hit': hit, 'total': total,
        'selected': True, 'content_sha256': digest(raw)})
    ref = located.capture(root, 'match')
    assert located.choices(root)[0]['editable'] is False
    with pytest.raises(ComputerError, match='formula_forbidden'):
        located.derive(ref, '=1')


def test_excel_total_count_remains_bound(tmp_path, workbook):
    token, match = _excel(workbook, '说明')
    match['total'] += 1
    root = _task(tmp_path, token, match)
    with pytest.raises(ComputerError, match='body_mismatch'):
        located.derive(located.capture(root, 'match'), 'new')
