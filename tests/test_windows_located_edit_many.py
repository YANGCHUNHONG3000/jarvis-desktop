"""Multiple immutable content receipts coalesce into existing copied edits."""
from copy import deepcopy

import pytest

from agent_service import windows_located_edit as located
from agent_service import windows_task as wt, windows_content_find as find, office_edit as edit
from agent_service.computer import ComputerError
from test_windows_task import Backend, WINDOW, goal, control
from test_windows_located_edit import _word, _excel, _reference, _mutate, workbook  # noqa: F401
from test_office_edit import word_fixture
from test_office_complex_table_edit import complex_fixture


def references(tmp_path, requests):
    """One completed Windows task with several separately attested locations."""
    conditions, observations = [], {}
    for index, (token, match) in enumerate(requests):
        identifier = f'match_{index}'
        conditions.append({'id': identifier, 'window': deepcopy(WINDOW),
            'selector': deepcopy(find.SELECTOR), 'state': 'value', 'value': token, 'retain': False})
        observations[identifier] = (token, match)

    class Passive(Backend):
        def observe_condition(self, condition, **kwargs):
            token, match = observations[condition['id']]
            return {'window': deepcopy(WINDOW), 'controls': [control(**find.SELECTOR, value=token)],
                'application': 'wps', 'target_selector': deepcopy(find.SELECTOR), 'dialogs': [],
                'truncated': False, 'content_token': token, 'content_match': deepcopy(match)}
    root = wt.create(tmp_path / 'tasks', goal(*conditions))
    backend = Passive()
    assert wt.run(root, backend)['ok'] and backend.calls == []
    return root, [located.capture(root, c['id']) for c in conditions]


def selections(refs, afters):
    return [{'ref': ref, 'after': after} for ref, after in zip(refs, afters)]


def test_excel_two_cells_one_file_one_group_and_full_location_bindings(tmp_path, workbook):
    root, refs = references(tmp_path, [_excel(workbook, '10'), _excel(workbook, '说明')])
    selected = selections(refs, [15, '更新说明'])
    before, evidence = workbook.read_bytes(), wt._inventory(root)
    result = located.derive_many(selected)
    assert result == [{'path': str(workbook), 'changes': [
        {'sheet': '预算', 'cell': 'B2', 'before': 10, 'after': 15},
        {'sheet': '保留页', 'cell': 'A1', 'before': '原始说明文字', 'after': '更新说明'}],
        'locations': selected}]
    assert result[0]['locations'] is not selected
    assert workbook.read_bytes() == before and wt._inventory(root) == evidence


@pytest.mark.parametrize('second_after', ['更新说明', 'another'])
def test_excel_same_cell_repeated_or_conflicting_rejected(tmp_path, workbook, second_after):
    _, refs = references(tmp_path, [_excel(workbook, '说明'), _excel(workbook, '文字')])
    with pytest.raises(ComputerError, match='duplicate_range'):
        located.derive_many(selections(refs, ['更新说明', second_after]))


def test_word_same_paragraph_two_disjoint_literals_coalesce_and_preserve_format(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    _, refs = references(tmp_path, [_word(path, '预算'), _word(path, '待确认')])
    selected = selections(refs, ['费用', '已确认'])
    result = located.derive_many(selected)
    assert result == [{'path': str(path), 'changes': [
        {'paragraph_index': 1, 'before': '预算待确认', 'after': '费用已确认'}], 'locations': selected}]
    spec = {'kind': 'document', 'source_id': 'selected-edit', 'source_sha256':
        located.query.decode(refs[0]['token'])['file']['sha256'], 'changes': result[0]['changes']}
    output = tmp_path / 'edited.docx'
    original = path.read_bytes()
    edit._make_copy(spec, path, output, 2)
    edit.verify_preservation(path, output, spec, 2)
    assert path.read_bytes() == original


def test_original_utf16_ranges_survive_earlier_length_changes_and_repeated_literals(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    def replace(tree):
        paragraph = tree.findall(f'.//{{{edit.W}}}body/{{{edit.W}}}p')[1]
        paragraph.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = '😀alpha beta alpha omega'
    _mutate(path, replace)
    _, refs = references(tmp_path, [_word(path, 'alpha', 2), _word(path, 'beta'), _word(path, '😀')])
    change = located.derive_many(selections(refs, ['longer replacement', '', 'Start']))[0]['changes'][0]
    assert change == {'paragraph_index': 1, 'before': '😀alpha beta alpha omega',
                      'after': 'Startalpha  longer replacement omega'}


@pytest.mark.parametrize('literal', ['预算', '预算待'])
def test_same_or_overlapping_original_word_ranges_rejected(tmp_path, literal):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    _, refs = references(tmp_path, [_word(path, '预算'), _word(path, literal)])
    with pytest.raises(ComputerError, match='overlapping_ranges'):
        located.derive_many(selections(refs, ['费用', 'cost']))


def test_disjoint_changes_across_two_word_runs_still_rejected(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path, complex_target=True)
    _, refs = references(tmp_path, [_word(path, '待确认'), _word(path, '复杂格式')])
    for ref, after in zip(refs, ['已确认', '特殊格式']):
        assert located.derive(ref, after)['changes']
    with pytest.raises(edit.EditError, match='edit_word_complex'):
        located.derive_many(selections(refs, ['已确认', '特殊格式']))


def test_nested_same_paragraph_mixed_native_marker_representations(tmp_path):
    path = tmp_path / 'nested.docx'
    complex_fixture(path)
    _, refs = references(tmp_path, [_word(path, 'Nest'), _word(path, 'ed', collapsed=True)])
    result = located.derive_many(selections(refs, ['Best', 'ing']))
    assert result[0]['changes'] == [{'table_index': 1, 'row_index': 0, 'column_index': 0,
        'paragraph_index': 0, 'before': 'Nested', 'after': 'Besting'}]


def test_different_word_paragraphs_remain_separate_changes(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    _, refs = references(tmp_path, [_word(path, '编辑验收'), _word(path, '待确认')])
    changes = located.derive_many(selections(refs, ['编辑完成', '已确认']))[0]['changes']
    assert [c['paragraph_index'] for c in changes] == [0, 1]


def test_absolute_file_groups_preserve_first_appearance_order(tmp_path):
    paths = [tmp_path / 'z.docx', tmp_path / 'a.docx']
    for path in paths:
        word_fixture(path)
    _, refs = references(tmp_path, [_word(p, '待确认') for p in paths])
    result = located.derive_many(selections(refs, ['Z', 'A']))
    # Input is deliberately reverse dictionary order: task order is authoritative.
    assert [item['path'] for item in result] == [str(paths[0]), str(paths[1])]
    assert [item['locations'][0]['after'] for item in result] == ['Z', 'A']


def test_five_distinct_files_refused(tmp_path):
    paths = [tmp_path / f'{i}.docx' for i in range(5)]
    for path in paths:
        word_fixture(path)
    _, refs = references(tmp_path, [_word(p, '待确认') for p in paths])
    with pytest.raises(ComputerError, match='file_limit'):
        located.derive_many(selections(refs, ['new'] * 5))


@pytest.mark.parametrize('value', [None, (), [], [{}], [{'ref': None}],
    [{'ref': None, 'after': 'x', 'extra': 1}], [{'ref': None, 'after': 'x'}] * 21])
def test_many_input_schema_and_bounds(value):
    with pytest.raises(ComputerError, match='selections_invalid'):
        located.derive_many(value)


def test_one_selection_retains_existing_derive_contract(tmp_path, workbook):
    _, ref = _reference(tmp_path, workbook, '说明')
    selected = [{'ref': ref, 'after': 'new'}]
    assert located.derive_many(selected) == [{**located.derive(ref, 'new'), 'locations': selected}]


def test_formula_selection_remains_forbidden_in_batch(tmp_path, workbook):
    _, refs = references(tmp_path, [_excel(workbook, '说明'), _excel(workbook, 'SUM', scope='formula')])
    with pytest.raises(ComputerError, match='formula_forbidden'):
        located.derive_many(selections(refs, ['new', 'formula']))


def test_stale_source_cannot_be_combined_with_new_source_receipt(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    _, old = _reference(tmp_path / 'old', path, '预算')
    def change(tree):
        paragraph = tree.findall(f'.//{{{edit.W}}}body/{{{edit.W}}}p')[1]
        paragraph.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = '新版待确认'
    _mutate(path, change)
    _, new = _reference(tmp_path / 'new', path, '待确认')
    with pytest.raises(ComputerError, match='windows_file_changed'):
        located.derive_many(selections([old, new], ['old', 'new']))


def test_changed_receipt_is_not_accepted_in_batch(tmp_path, workbook):
    _, refs = references(tmp_path, [_excel(workbook, '10'), _excel(workbook, '说明')])
    refs[1]['match']['hit']['address'] = 'B1'
    with pytest.raises(ComputerError, match='source_changed'):
        located.derive_many(selections(refs, [15, 'new']))


def test_nested_business_paragraph_and_merged_anchor_form_two_changes(tmp_path):
    path = tmp_path / 'nested.docx'
    complex_fixture(path)
    def update(tree):
        paragraph = edit._word_paragraph(tree, {'table_index': 1, 'row_index': 0,
            'column_index': 0, 'paragraph_index': 0})
        paragraph.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = 'Nested scope verified'
    _mutate(path, update)
    _, refs = references(tmp_path, [_word(path, 'Nested', collapsed=True),
        _word(path, 'scope', collapsed=True), _word(path, 'Horizontal', collapsed=True)])
    result = located.derive_many(selections(refs, ['Nested bundled', 'range checked', 'Horizontal reviewed']))
    assert result[0]['changes'] == [
        {'table_index': 1, 'row_index': 0, 'column_index': 0, 'paragraph_index': 0,
         'before': 'Nested scope verified', 'after': 'Nested bundled range checked verified'},
        {'table_index': 0, 'row_index': 0, 'column_index': 0, 'paragraph_index': 0,
         'before': 'Horizontal', 'after': 'Horizontal reviewed'}]
    assert len(result[0]['locations']) == 3


def test_twenty_selections_from_two_completed_tasks_are_allowed(tmp_path):
    path = tmp_path / 'word.docx'
    word_fixture(path)
    literals = '0123456789abcdefghij'
    def update(tree):
        paragraph = tree.findall(f'.//{{{edit.W}}}body/{{{edit.W}}}p')[1]
        paragraph.find(f'{{{edit.W}}}r/{{{edit.W}}}t').text = literals
    _mutate(path, update)
    requests = [_word(path, literal) for literal in literals]
    _, first = references(tmp_path / 'first', requests[:12])
    _, second = references(tmp_path / 'second', requests[12:])
    result = located.derive_many(selections(first + second, [f'[{literal}]' for literal in literals]))
    assert len(result) == 1 and len(result[0]['locations']) == 20
    assert result[0]['changes'] == [{'paragraph_index': 1, 'before': literals,
                                   'after': ''.join(f'[{literal}]' for literal in literals)}]
