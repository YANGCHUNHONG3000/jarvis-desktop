"""Receipt-bound Word handoff uses preserved native outputs without model calls."""

from hashlib import sha256
from copy import deepcopy
import json
from pathlib import Path
import zipfile

from lxml import etree as ET

import pytest

from agent_service import edited_artifact_source, office_source, verified_edit_document
from agent_service.edited_artifact_source import prepare_reference
from agent_service.local_skills import _prepared_snapshot
from agent_service.verified_edit_document import _derive, snapshot_reference


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'data/assistant_tasks'
TWO_EDITS = BASE / ('task-6a971c1e5a1543c9a87e7e9e5e2bebb2'
                    '/edit/edited-document.docx')
UNREPRESENTED_TABLE = BASE / ('task-1d52d21e59a241e693601607106075fc'
                              '/edit/edited-document.docx')
SINGLE_TABLE = BASE / ('task-284176722f68436785f83e0f7786ae64'
                       '/edit/edited-document.docx')


def _ref(path):
    if not path.is_file():
        pytest.skip('preserved native Office edit unavailable')
    return prepare_reference(path, sha256(path.read_bytes()).hexdigest(), BASE)


def test_two_word_edits_transfer_complete_paragraphs_and_recheck_at_skill_gate(tmp_path):
    ref = _ref(TWO_EDITS)
    root, edits, observed, coverage = _derive(ref, BASE)
    assert len(edits) == 2 and coverage['edit_depth'] == 2
    assert len(observed['paragraphs']) == 4
    task = tmp_path / 'task'
    task.mkdir()
    snapshot = snapshot_reference(ref, task, BASE)
    assert snapshot['observed'] == observed
    assert sha256((task / 'sources/source.docx').read_bytes()).hexdigest() == ref['sha256']
    assert _prepared_snapshot(task, snapshot, BASE) == snapshot
    provenance = json.loads((task / 'sources/provenance.json').read_text(encoding='utf-8'))
    assert provenance['edits'] == edits and provenance['root'] == root
    provenance['edits'][0]['receipt_sha256'] = '0' * 64
    (task / 'sources/provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
    with pytest.raises(ValueError, match='edited_document_snapshot_changed'):
        _prepared_snapshot(task, snapshot, BASE)


def test_intervening_paragraph_and_multiple_tables_are_structured_without_omission(tmp_path):
    ref = _ref(UNREPRESENTED_TABLE)
    _, _, observed, coverage = _derive(ref, BASE)
    assert coverage['scope'] == 'verified_edited_document_structure'
    records = observed['records']
    assert coverage['records'] == len(records)
    assert coverage['tables'] == 3  # Two body tables and one nested table.
    assert [(item['position'], item['text']) for item in records
            if item['type'] == 'paragraph' and item['structure']['parent'] == 'body'] == [
                ('body/0', '开头'), ('body/2', '中间'), ('body/4', '结尾')]
    task = tmp_path / 'task'
    task.mkdir()
    snapshot = snapshot_reference(ref, task, BASE)
    assert snapshot['kind'] == 'document_structure'
    assert _prepared_snapshot(task, snapshot, BASE) == snapshot


def test_native_single_table_edit_with_one_empty_tail_reaches_skill_gate(tmp_path):
    ref = _ref(SINGLE_TABLE)
    task = tmp_path / 'new-task'
    task.mkdir()
    snapshot = snapshot_reference(ref, task, BASE)
    assert snapshot['coverage']['tables'] == 1
    assert snapshot['coverage']['omitted_terminal_empty_paragraphs'] == 1
    assert snapshot['coverage']['layout_preserved'] is False
    assert len(snapshot['observed']['table']['rows']) == 4
    assert _prepared_snapshot(task, snapshot, BASE) == snapshot


@pytest.mark.parametrize('mutation', ['object_in_properties', 'two_empty_tails'])
def test_pseudo_empty_or_multiple_table_tails_are_rejected(tmp_path, mutation):
    if not SINGLE_TABLE.is_file():
        pytest.skip('preserved native one-table document unavailable')
    target = tmp_path / 'mutated.docx'
    with zipfile.ZipFile(SINGLE_TABLE) as source, zipfile.ZipFile(target, 'w') as output:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == 'word/document.xml':
                tree = ET.fromstring(data)
                body = tree.find(f'{{{office_source._W}}}body')
                table = body.find(f'{{{office_source._W}}}tbl')
                tail = list(body)[list(body).index(table) + 1]
                assert tail.tag == f'{{{office_source._W}}}p'
                if mutation == 'object_in_properties':
                    properties = tail.find(f'{{{office_source._W}}}pPr')
                    ET.SubElement(properties, f'{{{office_source._W}}}hyperlink')
                else:
                    body.insert(list(body).index(tail) + 1, deepcopy(tail))
                data = ET.tostring(tree, encoding='UTF-8', xml_declaration=True)
            output.writestr(member, data)
    with pytest.raises(ValueError, match='edited_document_order_unsupported'):
        verified_edit_document._document(target)


def _regular_table_copy(target, changed):
    if not SINGLE_TABLE.is_file():
        pytest.skip('preserved native one-table document unavailable')
    with zipfile.ZipFile(SINGLE_TABLE) as source, zipfile.ZipFile(target, 'w') as output:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == 'word/document.xml':
                tree = ET.fromstring(data)
                body = tree.find(f'{{{office_source._W}}}body')
                table = body.find(f'{{{office_source._W}}}tbl')
                row = table.findall(f'{{{office_source._W}}}tr')[1]
                cell = row.findall(f'{{{office_source._W}}}tc')[0]
                texts = list(cell.iter(f'{{{office_source._W}}}t'))
                texts[0].text = changed
                for text in texts[1:]:
                    text.text = ''
                # The native source has one empty paragraph after its table;
                # remove it in this bounded fixture to exercise a fully
                # representable document without silently dropping that node.
                for child in list(body):
                    if child.tag == f'{{{office_source._W}}}p' and list(body).index(child) > list(body).index(table):
                        assert office_source._word_text(child) == ''
                        body.remove(child)
                data = ET.tostring(tree, encoding='UTF-8', xml_declaration=True)
            output.writestr(member, data)


def test_two_word_edits_with_one_regular_table_snapshot(tmp_path, monkeypatch):
    root = tmp_path / 'root.docx'
    first = tmp_path / 'task-first/edit/edited-document.docx'
    second = tmp_path / 'task-second/edit/edited-document.docx'
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    for path, text in ((root, 'original'), (first, 'first edit'), (second, 'second edit')):
        _regular_table_copy(path, text)
    source1, source2 = tmp_path / 'source-one', tmp_path / 'source-two'
    source1.mkdir()
    source2.mkdir()
    refs = {path: {'version': 1, 'kind': 'office_edit', 'artifact': str(path),
                   'sha256': sha256(path.read_bytes()).hexdigest(),
                   'manifest_sha256': 'a' * 64, 'receipt_sha256': 'b' * 64}
            for path in (first, second)}
    original_prepare = edited_artifact_source.prepare_reference
    monkeypatch.setattr(edited_artifact_source, 'prepare_reference',
                        lambda path, digest, base: refs[Path(path)] if Path(path) in refs
                        else original_prepare(path, digest, base))
    monkeypatch.setattr(verified_edit_document, '_load',
                        lambda path: ({'spec': {'kind': 'document'},
                                       'source_root': str(source1 if Path(path).parent == first.parent else source2)}, ''))
    monkeypatch.setattr(verified_edit_document, 'verify_edit_source',
                        lambda path: {'sha256': sha256((root if Path(path) == source1 else first)
                                                  .read_bytes()).hexdigest()})
    monkeypatch.setattr(verified_edit_document, '_read',
                        lambda path: {'original': str(root if Path(path).parent == source1 else first)})
    task = tmp_path / 'new-task'
    task.mkdir()
    snapshot = verified_edit_document.snapshot_reference(refs[second], task, BASE)
    assert snapshot['coverage']['edit_depth'] == 2
    assert snapshot['coverage']['tables'] == 1
    assert snapshot['observed']['table']['rows'][0][0] == 'second edit'
    assert len(snapshot['observed']['paragraphs']) == 2
    assert _prepared_snapshot(task, snapshot, BASE) == snapshot
