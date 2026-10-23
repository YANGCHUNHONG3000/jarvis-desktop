"""Complex table identity/preservation and native protocol; live evidence is separate."""
from copy import deepcopy
from types import SimpleNamespace
import zipfile

from lxml import etree as ET
import pytest

from agent_service import office_edit as edit
from test_office_table_edit import paragraph, table_fixture, install_simulated_com


def write_parts(path, parts):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def complex_fixture(path, *, complex_run=False):
    """Synthetic horizontal/vertical merges, nested table and second top-level table."""
    table_fixture(path)
    parts = edit._parts(path)
    tree = edit._xml(parts['word/document.xml'])
    body = tree.find(f'{{{edit.W}}}body')
    def cell(text, props='', extra=''):
        return ('<w:tc><w:tcPr>' + props + '</w:tcPr>' +
                paragraph(text, complex_run=complex_run and text == 'Nested') + extra + '</w:tc>')
    nested = ('<w:tbl><w:tblGrid><w:gridCol w:w="2400"/></w:tblGrid><w:tr>' +
              cell('Nested') + '</w:tr></w:tbl>')
    xml = ('<w:tbl xmlns:w="' + edit.W + '"><w:tblGrid>' + '<w:gridCol w:w="2400"/>'*3 +
           '</w:tblGrid><w:tr>' + cell('Horizontal', '<w:gridSpan w:val="2"/>') + cell('R0C2') +
           '</w:tr><w:tr>' + cell('Vertical', '<w:vMerge w:val="restart"/>') +
           cell('R1C1', extra=nested + paragraph('After nested')) + cell('R1C2') +
           '</w:tr><w:tr>' + cell('', '<w:vMerge/>') + cell('R2C1') + cell('R2C2') + '</w:tr></w:tbl>')
    body.replace(body.find(f'{{{edit.W}}}tbl'), ET.fromstring(xml.encode()))
    parts['word/document.xml'] = ET.tostring(tree)
    write_parts(path, parts)


def prepared_complex(tmp_path, **kwargs):
    path = tmp_path / 'original.docx'
    complex_fixture(path, **kwargs)
    root = tmp_path / 'input'
    root.mkdir()
    source = edit.snapshot_edit_source(path, root)
    changes = [dict(zip(edit._WORD_COORDINATES, coords), before=before, after=after)
               for coords, before, after in [((0, 0, 0, 0), 'Horizontal', 'Horizontal updated'),
                                             ((0, 1, 0, 0), 'Vertical', 'Vertical updated'),
                                             ((0, 1, 1, 1), 'After nested', 'After updated'),
                                             ((1, 0, 0, 0), 'Nested', 'Nested updated'),
                                             ((2, 0, 0, 0), '相同', 'Other table updated')]]
    spec = {'kind': 'document', 'source_id': 'selected-edit', 'source_sha256': source['sha256'], 'changes': changes}
    return path, root, source, spec


def test_complex_preorder_anchor_identity_and_exact_preservation(tmp_path):
    path, root, source, spec = prepared_complex(tmp_path)
    original = path.read_bytes()
    cells = source['observed']['table_cells']
    assert len(cells) == 10
    assert len({tuple(c[k] for k in edit._WORD_COORDINATES) for c in cells}) == len(cells)
    assert next(c for c in cells if c['text'] == 'R0C2')['column_index'] == 2
    assert next(c for c in cells if c['text'] == 'After nested')['paragraph_index'] == 1
    assert next(c for c in cells if c['text'] == 'Nested')['table_index'] == 1
    assert not any(c['table_index'] == 0 and c['row_index'] == 2 and c['column_index'] == 0 for c in cells)
    edit._bind(spec, root)
    output = tmp_path / 'edited.docx'
    edit._make_copy(spec, path, output)
    observed = edit.verify_preservation(path, output, spec)
    assert path.read_bytes() == original
    assert edit.verify_edit_source(root) == source
    for change in spec['changes']:
        assert edit._word_observed_item(observed, change)['text'] == change['after']
    assert edit._masked(edit._parts(path), spec) == edit._masked(edit._parts(output), spec)


@pytest.mark.parametrize('coordinates', [(0, 0, 1, 0), (0, 2, 0, 0), (1, 1, 0, 0), (0, 1, 1, 2)])
def test_merged_continuation_and_nested_aliases_are_not_targets(tmp_path, coordinates):
    path, root, _, spec = prepared_complex(tmp_path)
    spec['changes'] = [dict(zip(edit._WORD_COORDINATES, coordinates), before='', after='Rejected')]
    with pytest.raises(edit.EditError, match='edit_before_mismatch'):
        edit._bind(spec, root)
    tree = edit._xml(edit._parts(path)['word/document.xml'])
    with pytest.raises(edit.EditError, match='edit_word_target_missing'):
        edit._word_paragraph(tree, spec['changes'][0])


def test_cross_run_nested_rewrite_refused(tmp_path):
    _, root, source, spec = prepared_complex(tmp_path, complex_run=True)
    assert edit._word_observed_item(source['observed'], spec['changes'][3])['editable'] is True
    spec['changes'] = [dict(spec['changes'][3], before='Nested复杂', after='全段改写')]
    with pytest.raises(edit.EditError, match='edit_word_complex'):
        edit._bind(spec, root)


def test_combined_rectangular_merge_has_single_anchor(tmp_path):
    path = tmp_path / 'rectangle.docx'
    table_fixture(path)
    parts = edit._parts(path)
    tree = edit._xml(parts['word/document.xml'])
    w = '{' + edit.W + '}'
    table = tree.find('.//' + w + 'tbl')
    for index, row in enumerate(table.findall(w + 'tr')):
        first, second = row.findall(w + 'tc')
        row.remove(second)
        props = first.find(w + 'tcPr')
        ET.SubElement(props, w + 'gridSpan').set(w + 'val', '2')
        ET.SubElement(props, w + 'vMerge').set(w + 'val', 'restart' if index == 0 else 'continue')
        if index:
            first.find('.//' + w + 't').text = ''
    parts['word/document.xml'] = ET.tostring(tree)
    write_parts(path, parts)
    observed = edit._observe(path, 'document')
    assert [c for c in observed['table_cells'] if c['table_index'] == 0] == [
        dict(zip(edit._WORD_COORDINATES, (0, 0, 0, 0)), text='相同', editable=True)]


@pytest.mark.parametrize('damage', ['orphan', 'span_mismatch', 'continuation_text', 'continuation_nested',
                                   'bad_span', 'duplicate_span', 'bad_merge', 'hmerge', 'offset', 'ragged',
                                   'missing_tail', 'depth', 'table_count', 'misplaced_merge', 'misplaced_table'])
def test_malformed_or_unbounded_complex_table_rejected(tmp_path, damage):
    path = tmp_path / 'bad.docx'
    complex_fixture(path)
    parts = edit._parts(path)
    tree = edit._xml(parts['word/document.xml'])
    w = '{' + edit.W + '}'
    table = tree.find('.//' + w + 'tbl')
    rows = table.findall(w + 'tr')
    anchor = rows[1].find(w + 'tc')
    continuation = rows[2].find(w + 'tc')
    if damage == 'orphan':
        anchor.find(w + 'tcPr').remove(anchor.find(w + 'tcPr/' + w + 'vMerge'))
    elif damage == 'span_mismatch':
        ET.SubElement(continuation.find(w + 'tcPr'), w + 'gridSpan').set(w + 'val', '2')
    elif damage == 'continuation_text':
        continuation.find('.//' + w + 't').text = 'hidden'
    elif damage == 'continuation_nested':
        continuation.insert(1, deepcopy(table.find('.//' + w + 'tbl')))
    elif damage in {'bad_span', 'duplicate_span'}:
        span = table.find('.//' + w + 'gridSpan')
        if damage == 'bad_span':
            span.set(w + 'val', '0')
        else:
            span.getparent().append(deepcopy(span))
    elif damage == 'bad_merge':
        continuation.find(w + 'tcPr/' + w + 'vMerge').set(w + 'val', 'unknown')
    elif damage in {'hmerge', 'offset'}:
        ET.SubElement(anchor.find(w + 'tcPr'), w + ('hMerge' if damage == 'hmerge' else 'gridBefore'))
    elif damage == 'ragged':
        rows[0].remove(rows[0].findall(w + 'tc')[-1])
    elif damage == 'missing_tail':
        cell = rows[1].findall(w + 'tc')[1]
        cell.remove(cell.findall(w + 'p')[-1])
    elif damage == 'depth':
        nested = table.find('.//' + w + 'tbl')
        for _ in range(4):
            cell = nested.find(w + 'tr/' + w + 'tc')
            child = deepcopy(nested)
            cell.insert(0, child)
            nested = child
    elif damage == 'table_count':
        for _ in range(7):
            table.getparent().insert(0, deepcopy(table.find('.//' + w + 'tbl')))
    elif damage == 'misplaced_merge':
        ET.SubElement(anchor.find(w + 'p/' + w + 'pPr'), w + 'vMerge')
    elif damage == 'misplaced_table':
        anchor.find(w + 'p/' + w + 'pPr').append(deepcopy(table.find('.//' + w + 'tbl')))
    parts['word/document.xml'] = ET.tostring(tree)
    write_parts(path, parts)
    with pytest.raises(edit.EditError, match='edit_word_table_invalid|edit_word_merged_table_unsupported|edit_word_limit'):
        edit._observe(path, 'document')


def native_complex_document(path):
    xml = edit._parts(path)['word/document.xml']
    body = edit._xml(xml).find(f'{{{edit.W}}}body')
    pkg = '{http://schemas.microsoft.com/office/2006/xmlPackage}'
    package = ET.Element(pkg + 'package')
    part = ET.SubElement(package, pkg + 'part', {pkg + 'name': '/word/document.xml'})
    ET.SubElement(part, pkg + 'xmlData').append(edit._xml(xml))
    return SimpleNamespace(Content=SimpleNamespace(WordOpenXML=ET.tostring(package, encoding='unicode'),
                                                   Text=edit._word_native_stream(body)))


def test_complex_native_protocol_uses_xml_and_text_without_com_cell_apis(tmp_path, monkeypatch):
    path, _, source, spec = prepared_complex(tmp_path)
    owned = native_complex_document(path)
    assert 'Horizontal\r\x07R0C2\r\x07\r\x07' in owned.Content.Text
    assert 'R1C1\rNested\r\x07\r\x07After nested\r\x07' in owned.Content.Text
    install_simulated_com(monkeypatch, owned)
    result = edit._native_readback(path, spec, source['observed'], lambda: None)
    assert len(result['changes']) == 5


def test_native_complex_readback_accepts_run_normalization(tmp_path, monkeypatch):
    path, _, source, spec = prepared_complex(tmp_path)
    owned = native_complex_document(path)
    tree = edit._xml(owned.Content.WordOpenXML.encode())
    text = next(t for t in tree.iter(f'{{{edit.W}}}t') if t.text == 'Horizontal')
    text.text = 'Hori'
    run = text.getparent()
    another = deepcopy(run)
    another.find(f'{{{edit.W}}}t').text = 'zontal'
    run.addnext(another)
    owned.Content.WordOpenXML = ET.tostring(tree, encoding='unicode')
    install_simulated_com(monkeypatch, owned)
    assert edit._native_readback(path, spec, source['observed'], lambda: None)['changes']


def test_native_semantics_accepts_only_unused_grid_division_normalization(tmp_path):
    test_combined_rectangular_merge_has_single_anchor(tmp_path)
    path = tmp_path / 'rectangle.docx'
    body = edit._word_body(edit._parts(path))
    normalized = deepcopy(body)
    w = '{' + edit.W + '}'
    table = normalized.find(w + 'tbl')
    grid = table.find(w + 'tblGrid')
    grid.remove(grid[-1])
    for span in list(table.iter(w + 'gridSpan')):
        span.getparent().remove(span)
    assert edit._word_native_semantics(body) == edit._word_native_semantics(normalized)
    # A cell edge used by another row must remain meaningful.
    complex_fixture(tmp_path / 'complex.docx')
    body = edit._word_body(edit._parts(tmp_path / 'complex.docx'))
    changed = deepcopy(body)
    span = changed.find('.//' + w + 'gridSpan')
    span.set(w + 'val', '1')
    first = changed.find(w + 'tbl/' + w + 'tr')
    extra = deepcopy(first.findall(w + 'tc')[-1])
    first.append(extra)
    assert edit._word_native_semantics(body) != edit._word_native_semantics(changed)


@pytest.mark.parametrize('damage', ['text', 'xml_text', 'xml_merge', 'xml_identity', 'xml_limit', 'xml_dtd'])
def test_complex_native_protocol_rejects_either_surface_mismatch(tmp_path, monkeypatch, damage):
    path, _, source, spec = prepared_complex(tmp_path)
    owned = native_complex_document(path)
    if damage == 'text':
        owned.Content.Text = owned.Content.Text.replace('Nested', 'Wrong')
    elif damage == 'xml_text':
        owned.Content.WordOpenXML = owned.Content.WordOpenXML.replace('Nested', 'Wrong')
    elif damage == 'xml_merge':
        owned.Content.WordOpenXML = owned.Content.WordOpenXML.replace('restart', 'continue')
    elif damage == 'xml_identity':
        owned.Content.WordOpenXML = owned.Content.WordOpenXML.replace('/word/document.xml', '/word/other.xml')
    elif damage == 'xml_limit':
        owned.Content.WordOpenXML = ' ' * 4_000_001
    else:
        owned.Content.WordOpenXML = '<!DOCTYPE package>' + owned.Content.WordOpenXML
    install_simulated_com(monkeypatch, owned)
    with pytest.raises(edit.EditError, match='edit_application_mismatch|edit_word_table_invalid'):
        edit._native_readback(path, spec, source['observed'], lambda: None)
