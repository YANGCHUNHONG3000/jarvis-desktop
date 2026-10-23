"""Offline complex Word intake against preserved native edit receipts."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import zipfile

from lxml import etree as ET
import pytest

from agent_service import office_edit, office_source, structured_document_source as structured
from agent_service.edited_artifact_source import prepare_reference
from agent_service.verified_edit_document import _derive, snapshot_reference, verify_snapshot


BASE = Path(__file__).resolve().parents[1] / 'data/assistant_tasks'
COMPLEX = BASE / ('task-1d52d21e59a241e693601607106075fc'
                  '/edit/edited-document.docx')


def _ref():
    if not COMPLEX.is_file():
        pytest.skip('preserved complex native Word edit unavailable')
    return prepare_reference(COMPLEX, sha256(COMPLEX.read_bytes()).hexdigest(), BASE)


def test_native_two_edit_multi_merged_nested_word_has_complete_ordered_audit(tmp_path):
    ref = _ref()
    root, edits, observed, coverage = _derive(ref, BASE)
    assert len(edits) == 2 and root['sha256']
    assert coverage == {'scope': 'verified_edited_document_structure', 'records': 27,
                        'tables': 3, 'cells': 10, 'paragraphs': 14,
                        'layout_preserved': False, 'edit_depth': 2}
    records = observed['records']
    body = office_edit._word_body(office_edit._parts(COMPLEX))
    for name, record_type in (('p', 'paragraph'), ('tbl', 'table'), ('tc', 'cell')):
        assert sum(item['type'] == record_type for item in records) == len(
            list(body.iter(f'{{{office_source._W}}}{name}')))
    assert [item['text'] for item in records if item['type'] == 'paragraph'] == [
        office_source._word_text(node) for node in body.iter(f'{{{office_source._W}}}p')]
    assert records[0]['position'] == 'body/0' and records[0]['text'] == '开头'
    assert records[-1]['position'] == 'body/4' and records[-1]['text'] == '结尾'
    assert next(item for item in records if item['text'] == '中间')['position'] == 'body/2'
    horizontal = next(item for item in records if item['position'] == 'body/1/row/0/cell/0')
    assert horizontal['structure']['span'] == 2
    continuation = next(item for item in records if item['position'] == 'body/1/row/2/cell/0')
    assert continuation['structure']['merge'] == 'continue'
    assert records[records.index(continuation) + 1]['text'] == ''
    nested = next(item for item in records if item['text'] == '嵌套已复核')
    assert '/block/1/row/0/cell/0/block/0' in nested['position']
    assert records[records.index(nested) + 1]['text'] == '嵌套之后已确认'
    audit = structured.audit_table(observed)
    assert audit['headers'] == ['位置', '类型', '结构', '原文']
    assert len(audit['rows']) == len(records)
    assert [row[0] for row in audit['rows']] == [item['position'] for item in records]
    assert audit['rows'][0][3] == '开头' and audit['rows'][-1][3] == '结尾'
    task = tmp_path / 'new-task'
    task.mkdir()
    snapshot = snapshot_reference(ref, task, BASE)
    assert snapshot['kind'] == 'document_structure' and snapshot['observed'] == observed
    provenance = json.loads((task / 'sources/provenance.json').read_text(encoding='utf-8'))
    assert verify_snapshot(task, snapshot, provenance, BASE) == (
        'document_structure', observed, coverage)


@pytest.mark.parametrize('damage', ['observation', 'bytes', 'provenance'])
def test_structured_snapshot_rejects_changed_observation_bytes_or_chain(tmp_path, damage):
    ref = _ref()
    task = tmp_path / 'new-task'
    task.mkdir()
    snapshot = snapshot_reference(ref, task, BASE)
    provenance = json.loads((task / 'sources/provenance.json').read_text(encoding='utf-8'))
    if damage == 'observation':
        snapshot = deepcopy(snapshot)
        snapshot['observed']['records'][0]['text'] = 'forged'
    elif damage == 'bytes':
        with (task / 'sources/source.docx').open('ab') as stream:
            stream.write(b'forged')
    else:
        provenance['edits'][0]['receipt_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        verify_snapshot(task, snapshot, provenance, BASE)


@pytest.mark.parametrize('damage', ['hidden_object', 'too_many_records',
                                    'unexpected_body', 'unexpected_cell', 'orphan_merge'])
def test_complex_parser_rejects_unrepresented_or_overflow_content(tmp_path, damage):
    _ref()
    target = tmp_path / 'bad.docx'
    with zipfile.ZipFile(COMPLEX) as source, zipfile.ZipFile(target, 'w') as output:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == 'word/document.xml':
                tree = ET.fromstring(data)
                body = tree.find(f'{{{office_source._W}}}body')
                paragraph = body.find(f'{{{office_source._W}}}p')
                if damage == 'hidden_object':
                    ET.SubElement(paragraph.find(f'{{{office_source._W}}}pPr'),
                                  f'{{{office_source._W}}}drawing')
                elif damage == 'too_many_records':
                    for _ in range(180):
                        body.insert(len(body) - 1, deepcopy(paragraph))
                elif damage == 'unexpected_body':
                    body.insert(1, ET.Element(f'{{{office_source._W}}}customXml'))
                elif damage == 'unexpected_cell':
                    cell = body.find(f'.//{{{office_source._W}}}tc')
                    cell.insert(1, ET.Element(f'{{{office_source._W}}}customXml'))
                else:
                    merge = body.find(f'.//{{{office_source._W}}}vMerge[@{{{office_source._W}}}val="restart"]')
                    assert merge is not None
                    merge.getparent().remove(merge)
                data = ET.tostring(tree, encoding='UTF-8', xml_declaration=True)
            output.writestr(member, data)
    with pytest.raises(ValueError):
        structured.parse(target)
