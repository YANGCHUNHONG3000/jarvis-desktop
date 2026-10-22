"""Offline OOXML preservation and simulated COM protocol checks; no native Office proof."""
from copy import deepcopy
from types import SimpleNamespace
import sys
import zipfile

from lxml import etree as ET
import pytest

from agent_service import office_edit as edit


def paragraph(text, *, complex_run=False):
    extra = '<w:r><w:t>复杂</w:t></w:r>' if complex_run else ''
    return ('<w:p><w:pPr><w:spacing w:after="120"/></w:pPr><w:r><w:rPr><w:b/>'
            f'<w:color w:val="006699"/></w:rPr><w:t>{text}</w:t></w:r>{extra}</w:p>')


def table_fixture(path, *, complex_cell=False, unsupported=None):
    cells = [[paragraph('相同'), paragraph('第一段') + paragraph('第二段', complex_run=complex_cell)],
             [paragraph('相同'), paragraph('保持')]]
    rows = ''.join('<w:tr><w:trPr><w:trHeight w:val="360"/></w:trPr>' + ''.join(
        '<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/><w:shd w:fill="FFFF00"/>'
        '</w:tcPr>' + cell + '</w:tc>' for cell in row) + '</w:tr>' for row in cells)
    table = ('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="4800" w:type="dxa"/>'
             '<w:tblBorders><w:top w:val="single" w:sz="8"/></w:tblBorders></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>' + rows + '</w:tbl>')
    if unsupported == 'merged':
        table = table.replace('<w:tcPr>', '<w:tcPr><w:gridSpan w:val="2"/>', 1)
    if unsupported == 'nested':
        table = table.replace('</w:tc>', '<w:tbl><w:tr><w:tc>' + paragraph('嵌套') + '</w:tc></w:tr></w:tbl></w:tc>', 1)
    if unsupported == 'ragged':
        table = table.replace('<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/><w:shd w:fill="FFFF00"/>'
                              '</w:tcPr>' + paragraph('保持') + '</w:tc>', '')
    second = '<w:tbl><w:tr><w:tc>' + paragraph('相同') + '</w:tc></w:tr></w:tbl>'
    body = paragraph('开头') + table + paragraph('中间') + second + paragraph('结尾') + '<w:sectPr/>'
    types = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '</Types>')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b'preserve archive comment'
        archive.writestr('[Content_Types].xml', types)
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{edit.W}"><w:body>{body}</w:body></w:document>')
        archive.writestr('word/styles.xml', f'<w:styles xmlns:w="{edit.W}"/>')


def prepared(tmp_path, **kwargs):
    path = tmp_path / 'original.docx'
    table_fixture(path, **kwargs)
    root = tmp_path / 'input'
    root.mkdir()
    source = edit.snapshot_edit_source(path, root)
    change = {'table_index': 0, 'row_index': 0, 'column_index': 1, 'paragraph_index': 1,
              'before': '第二段', 'after': ' 已确认 '}
    spec = {'kind': 'document', 'source_id': 'selected-edit', 'source_sha256': source['sha256'], 'changes': [change]}
    return path, root, source, spec


def test_table_observation_precise_copy_and_all_other_xml_preserved(tmp_path):
    path, root, source, spec = prepared(tmp_path)
    original = path.read_bytes()
    assert [p['text'] for p in source['observed']['paragraphs']] == ['开头', '中间', '结尾']
    assert len(source['observed']['table_cells']) == 6
    assert source['observed']['table_cells'][2] == {**{k: spec['changes'][0][k] for k in edit._WORD_COORDINATES},
                                                   'text': '第二段', 'editable': True}
    spec['changes'] += [{'paragraph_index': 1, 'before': '中间', 'after': '正文已确认'},
                        {'table_index': 1, 'row_index': 0, 'column_index': 0, 'paragraph_index': 0,
                         'before': '相同', 'after': '另一个表'}]
    edit._bind(spec, root)
    output = tmp_path / 'edited.docx'
    edit._make_copy(spec, path, output)
    observed = edit.verify_preservation(path, output, spec)
    assert path.read_bytes() == original
    assert observed['table_cells'][0]['text'] == observed['table_cells'][3]['text'] == '相同'
    assert observed['table_cells'][1]['text'] == '第一段'
    assert observed['table_cells'][2]['text'] == ' 已确认 '
    assert observed['table_cells'][-1]['text'] == '另一个表'
    assert observed['paragraphs'][1]['text'] == '正文已确认'
    assert edit.verify_edit_source(root) == source
    before, after = edit._parts(path), edit._parts(output)
    assert before['word/styles.xml'] == after['word/styles.xml']
    assert edit._masked(before, spec) == edit._masked(after, spec)
    with zipfile.ZipFile(output) as archive:
        assert archive.comment == b'preserve archive comment'


@pytest.mark.parametrize('coordinate,value', [('table_index', 2), ('row_index', 2), ('column_index', 2),
                                             ('paragraph_index', 2), ('row_index', -1), ('column_index', True),
                                             ('table_index', 8), ('row_index', 100), ('column_index', 52)])
def test_table_out_of_bounds_or_noninteger_target_rejected(tmp_path, coordinate, value):
    _, root, _, spec = prepared(tmp_path)
    spec['changes'][0][coordinate] = value
    with pytest.raises(edit.EditError, match='edit_change_invalid|edit_before_mismatch'):
        edit._bind(spec, root)


@pytest.mark.parametrize('missing', edit._WORD_COORDINATES)
def test_table_partial_coordinates_rejected(tmp_path, missing):
    _, _, _, spec = prepared(tmp_path)
    del spec['changes'][0][missing]
    with pytest.raises(edit.EditError, match='edit_change_invalid'):
        edit.validate_edit_spec(spec)


def test_same_table_paragraph_duplicate_rejected_but_distinct_targets_allowed(tmp_path):
    _, _, _, spec = prepared(tmp_path)
    spec['changes'].append(deepcopy(spec['changes'][0]))
    with pytest.raises(edit.EditError, match='edit_duplicate_or_noop'):
        edit.validate_edit_spec(spec)


def test_preservation_independently_checks_exact_original_table_text(tmp_path):
    path, _, _, spec = prepared(tmp_path)
    output = tmp_path / 'copy.docx'
    edit._make_copy(spec, path, output)
    spec['changes'][0]['before'] = '错误原值'
    with pytest.raises(edit.EditError, match='edit_before_mismatch'):
        edit.verify_preservation(path, output, spec)


def test_complex_table_paragraph_readonly_and_other_cell_still_editable(tmp_path):
    path, root, source, spec = prepared(tmp_path, complex_cell=True)
    assert source['observed']['table_cells'][2]['editable'] is False
    spec['changes'][0]['before'] = '第二段复杂'
    with pytest.raises(edit.EditError, match='edit_before_mismatch'):
        edit._bind(spec, root)
    spec['changes'][0].update(column_index=0, paragraph_index=0, before='相同')
    edit._bind(spec, root)
    output = tmp_path / 'copy.docx'
    edit._make_copy(spec, path, output)
    assert edit.verify_preservation(path, output, spec)['table_cells'][2]['text'] == '第二段复杂'


@pytest.mark.parametrize('unsupported,code', [('merged', 'edit_word_table_invalid'),
                                            ('nested', 'edit_word_table_invalid'),
                                            ('ragged', 'edit_word_table_invalid')])
def test_malformed_span_nested_tail_or_nonrectangular_table_rejected(tmp_path, unsupported, code):
    path = tmp_path / 'original.docx'
    table_fixture(path, unsupported=unsupported)
    with pytest.raises(edit.EditError, match=code):
        edit._observe(path, 'document')


@pytest.mark.parametrize('damage', ['style', 'other_text', 'move_paragraph'])
def test_table_unapproved_xml_change_rejected(tmp_path, damage):
    path, _, _, spec = prepared(tmp_path)
    output = tmp_path / 'copy.docx'
    edit._make_copy(spec, path, output)
    parts = edit._parts(output)
    tree = edit._xml(parts['word/document.xml'])
    if damage == 'style':
        tree.find(f'.//{{{edit.W}}}shd').set(f'{{{edit.W}}}fill', 'FF0000')
    elif damage == 'other_text':
        tree.find(f'.//{{{edit.W}}}t').text = '未经批准'
    else:
        cell = tree.find(f'.//{{{edit.W}}}tr').findall(f'{{{edit.W}}}tc')[1]
        cell.append(cell.findall(f'{{{edit.W}}}p')[0])
    parts['word/document.xml'] = ET.tostring(tree)
    damaged = tmp_path / 'damaged.docx'
    with zipfile.ZipFile(damaged, 'w') as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    with pytest.raises(edit.EditError, match='edit_preservation_mismatch'):
        edit.verify_preservation(path, damaged, spec)


class Collection:
    def __init__(self, items):
        self.items = items
        self.Count = len(items)

    def Item(self, index):
        return self.items[index - 1]


def native_protocol_document(observed):
    """Only test doubles. Production _native_readback runs unchanged against these COM surfaces."""
    def native_paragraph(text, in_table):
        return SimpleNamespace(Range=SimpleNamespace(Text=text, Information=lambda constant: in_table if constant == 12 else None))
    table_cells = observed['table_cells']
    tables, all_paragraphs = [], []
    for table_index in range(2):
        cells = {}
        items = [item for item in table_cells if item['table_index'] == table_index]
        rows, columns = max(i['row_index'] for i in items) + 1, max(i['column_index'] for i in items) + 1
        for row in range(rows):
            for column in range(columns):
                paragraphs = [item for item in items if item['row_index'] == row and item['column_index'] == column]
                native = [native_paragraph(item['text'] + ('\r\x07' if i == len(paragraphs) - 1 else '\r'), True)
                          for i, item in enumerate(paragraphs)]
                cells[(row + 1, column + 1)] = SimpleNamespace(Range=SimpleNamespace(Paragraphs=Collection(native)))
                all_paragraphs.extend(native)
        tables.append(SimpleNamespace(Rows=SimpleNamespace(Count=rows), Columns=SimpleNamespace(Count=columns),
                                      Cell=lambda row, column, cells=cells: cells[(row, column)]))
        all_paragraphs.append(native_paragraph(observed['paragraphs'][table_index + 1]['text'] + '\r', False))
    all_paragraphs.insert(0, native_paragraph(observed['paragraphs'][0]['text'] + '\r', False))
    return SimpleNamespace(Paragraphs=Collection(all_paragraphs), Tables=Collection(tables))


def install_simulated_com(monkeypatch, owned):
    app = SimpleNamespace(Name='simulated COM protocol', Version='test-only',
                          Documents=SimpleNamespace(Open=lambda *args, **kwargs: owned))
    client = SimpleNamespace(DispatchEx=lambda progid: app)
    monkeypatch.setitem(sys.modules, 'pythoncom', SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None))
    monkeypatch.setitem(sys.modules, 'win32com', SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, 'win32com.client', client)


def test_native_readback_protocol_excludes_table_from_body_and_keeps_coordinates(tmp_path, monkeypatch):
    path, _, source, spec = prepared(tmp_path)
    output = tmp_path / 'copy.docx'
    edit._make_copy(spec, path, output)
    observed = edit.verify_preservation(path, output, spec)
    owned = native_protocol_document(observed)
    install_simulated_com(monkeypatch, owned)
    result = edit._native_readback(output, spec, observed, lambda: None)
    assert result['name'] == 'simulated COM protocol'
    assert result['changes'] == [{k: v for k, v in spec['changes'][0].items() if k != 'before'}]


@pytest.mark.parametrize('damage', ['cell_text', 'cell_paragraph_count', 'body_text', 'table_count', 'row_count', 'column_count'])
def test_native_readback_protocol_rejects_mismatch(tmp_path, monkeypatch, damage):
    path, _, source, spec = prepared(tmp_path)
    observed = source['observed']
    owned = native_protocol_document(observed)
    cell = owned.Tables.Item(1).Cell(1, 2)
    if damage == 'cell_text':
        cell.Range.Paragraphs.Item(2).Range.Text = '错误\r\x07'
    elif damage == 'cell_paragraph_count':
        cell.Range.Paragraphs.Count = 1
    elif damage == 'body_text':
        owned.Paragraphs.Item(1).Range.Text = '错误\r'
    elif damage == 'table_count':
        owned.Tables.Count = 1
    elif damage == 'row_count':
        owned.Tables.Item(1).Rows.Count = 1
    else:
        owned.Tables.Item(1).Columns.Count = 1
    install_simulated_com(monkeypatch, owned)
    with pytest.raises(edit.EditError, match='edit_application_mismatch'):
        edit._native_readback(path, spec, observed, lambda: None)
