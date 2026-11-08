"""Offline literal-query contracts; no Office startup, input or model calls."""
from copy import deepcopy
import json
import zipfile

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_content_query as query


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'existing.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="'
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr('word/document.xml',
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body/></w:document>')
    return path


def test_token_roundtrip_pins_query_and_original_snapshot(source):
    token = query.encode(source, '中文 ^p * ?', 2)
    record = query.decode(token)
    assert record['query'] == '中文 ^p * ?' and record['occurrence'] == 2
    assert record['file'] == query.files.snapshot_file(source)
    assert token == json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert query.encode(record['file'], record['query'], 2) == token


def test_token_stale_file_is_not_refreshed(source):
    token = query.encode(source, 'value')
    record = query.decode(token)
    source.write_bytes(source.read_bytes() + b'changed')
    for operation in (lambda: query.decode(token), lambda: query.encode(record['file'], 'value')):
        with pytest.raises(ComputerError, match='windows_file_changed'):
            operation()


@pytest.mark.parametrize('change', [dict(query=''), dict(query='x' * 301), dict(query='x\n'),
    dict(query='\ud800'), dict(occurrence=True), dict(occurrence=0), dict(occurrence=101),
    dict(scope='display'), dict(scope='formula')])
def test_invalid_query_before_selection(source, change):
    args = dict(file=source, query='word', occurrence=1, scope='text')
    args.update(change)
    with pytest.raises(ComputerError, match='windows_content_query_invalid'):
        query.encode(**args)


@pytest.mark.parametrize('change', [dict(schema=True), dict(schema=2), dict(extra=1),
    dict(query='\t'), dict(occurrence=1.0), dict(scope=[]), dict(file={})])
def test_decode_strict_shape(source, change):
    record = query.decode(query.encode(source, 'word'))
    record.update(change)
    with pytest.raises(ComputerError):
        query.decode(json.dumps(record))


def test_decode_rejects_duplicate_keys(source):
    token = query.encode(source, 'word')
    with pytest.raises(ComputerError, match='windows_content_query_invalid'):
        query.decode('{"schema":1,' + token[1:])
    with pytest.raises(ComputerError, match='windows_content_query_invalid'):
        query.decode(token.replace('"size":', '"size":1,"size":'))


def test_word_literal_case_sensitive_nonoverlapping_and_complete_count():
    text = 'AA aaa aa AA ^p * ?\r'
    hit, count = query.word_match(text, 5, 5 + len(text), 'AA', 2)
    assert hit == {'start': 15, 'end': 17, 'text': 'AA'} and count == 2
    assert query.word_match('aaaaa', 0, 5, 'aa', 2) == ({'start': 2, 'end': 4, 'text': 'aa'}, 2)
    assert query.word_match(text, 5, 5 + len(text), '^p')[0]['text'] == '^p'


def test_word_utf16_offsets_include_emoji_and_table_markers():
    text = '😀中\r\x07目标 😀目标\r'
    units = len(text.encode('utf-16-le')) // 2
    hit, count = query.word_match(text, 10, 10 + units, '目标', 2)
    prefix = len('😀中\r\x07目标 😀'.encode('utf-16-le')) // 2
    assert hit == {'start': 10 + prefix, 'end': 12 + prefix, 'text': '目标'}
    assert count == 2


@pytest.mark.parametrize('text,start,end', [('😀target', 0, 7), ('word', True, 4),
    ('word', 1, 4), ('word', -1, 3), ('\ud800', 0, 1), ('x' * 100001, 0, 100001)],
    ids=['emoji-span', 'bool-start', 'span-length', 'negative-start', 'surrogate', 'size-limit'])
def test_word_rejects_unattestable_native_span(text, start, end):
    with pytest.raises(ComputerError, match='windows_content_span_invalid'):
        query.word_match(text, start, end, 'x')


@pytest.mark.parametrize('text,needle,occurrence', [('Word', 'word', 1), ('aaaa', 'aa', 3), ('', 'x', 1)])
def test_word_missing_occurrence_is_explicit(text, needle, occurrence):
    with pytest.raises(ComputerError, match='windows_content_match_missing'):
        query.word_match(text, 0, len(text), needle, occurrence)


def model():
    return {'version': 1, 'semantic_sha256': '0' * 64, 'sheets': [
        {'name': 'Hidden', 'state': 'hidden', 'range': 'A1:A1', 'values': [['target']], 'formulas': []},
        {'name': 'Second', 'state': 'visible', 'range': 'A1:C2',
         'values': [['target target', 2.0, True], ['target', 'cached target', False]],
         'formulas': [{'cell': 'B2', 'expression': '=A1', 'result': 'cached target'}]},
        {'name': 'Third', 'state': 'visible', 'range': 'A1:A1', 'values': [['target']], 'formulas': []}]}


def test_excel_counts_matching_cells_in_visible_sheet_row_order_and_skips_cache():
    hit, count = query.excel_match(model(), 'target', 2)
    assert hit == {'sheet': 'Second', 'address': 'A2', 'text': 'target', 'field': 'value'}
    assert count == 3
    assert query.excel_match(model(), 'target', 3)[0]['sheet'] == 'Third'
    assert query.excel_match(model(), 'target', 1)[0]['text'] == 'target target'


def test_excel_formula_scope_is_separate_from_values_and_cache():
    assert query.excel_match(model(), 'A1', scope='formula') == (
        {'sheet': 'Second', 'address': 'B2', 'text': '=A1', 'field': 'formula'}, 1)
    for needle, scope in [('A1', 'text'), ('target', 'formula'), ('cached', 'text')]:
        with pytest.raises(ComputerError, match='windows_content_match_missing'):
            query.excel_match(model(), needle, scope=scope)


@pytest.mark.parametrize('value,expected', [(True, 'TRUE'), (False, 'FALSE'), (2.0, '2'),
    (-0.0, '0'), (1.25, '1.25'), ('001', '001'), (None, None)])
def test_scalars_are_stable_literal_values(value, expected):
    assert query.scalar_text(value) == expected


@pytest.mark.parametrize('value', [float('nan'), float('inf'), object(), ['x']])
def test_unsupported_scalar_is_not_stringified(value):
    with pytest.raises(ComputerError, match='windows_content_model_invalid'):
        query.scalar_text(value)


@pytest.mark.parametrize('change', ['state', 'range', 'duplicate', 'outside', 'ragged', 'formula'])
def test_malformed_model_never_returns_selection(change):
    value = deepcopy(model())
    sheet = value['sheets'][1]
    if change == 'state':
        sheet['state'] = []
    elif change == 'range':
        sheet['range'] = 'A1:D2'
    elif change == 'duplicate':
        value['sheets'][0]['name'] = 'second'
    elif change == 'outside':
        sheet['formulas'][0]['cell'] = 'C201'
    elif change == 'ragged':
        sheet['values'][1].append('extra')
    else:
        sheet['formulas'][0]['expression'] = 'A1'
    with pytest.raises(ComputerError, match='windows_content_model_invalid'):
        query.excel_match(value, 'target')
