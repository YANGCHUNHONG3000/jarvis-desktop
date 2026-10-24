"""Offline, receipt-bound intake for the preserved §56 edited workbook."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import artifact_source, generated_edit_table, local_skills
from agent_service.artifact_source import ArtifactSourceError
from agent_service.computer_agent_tools import _load
from agent_service.edited_artifact_source import prepare_reference


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'data/assistant_tasks'
EDITED = BASE / ('task-c9299da4c2a04b8e9dac9009c6be4a26'
                 '/edit/edited-spreadsheet.xlsx')
LEGACY_TASK = BASE / 'task-4b3f9305714647abb87c72771ced23ff'


@pytest.fixture
def prepared(tmp_path):
    if not EDITED.is_file():
        pytest.skip('§56 preserved native edit and its receipts are unavailable')
    manifest, _ = _load(EDITED.parent / 'edit-manifest.json')
    original = Path(json.loads((Path(manifest['source_root']) / 'source.json').read_text(
        encoding='utf-8'))['original'])
    hashes = {path: sha256(path.read_bytes()).hexdigest() for path in (EDITED, original)}
    ref = prepare_reference(EDITED, hashes[EDITED], BASE)
    task = tmp_path / 'new-task'
    task.mkdir()
    snapshot = generated_edit_table.snapshot_if_generated(ref, task, BASE)
    assert {path: sha256(path.read_bytes()).hexdigest() for path in hashes} == hashes
    return task, snapshot, manifest, ref


def test_real_edited_source_preserves_every_row_and_reaches_table_copy(prepared):
    task, snapshot, manifest, ref = prepared
    assert {'generated_edit_table.py', 'computer.py', 'computer_agent_tools.py',
            'verification_progress.py'} <= set(local_skills._CODE)
    assert snapshot['kind'] == 'table'
    assert snapshot['coverage']['scope'] == 'verified_generated_edited_workbook_data_rows'
    assert len(snapshot['observed']['headers']) == 6
    assert len(snapshot['observed']['rows']) == 6
    assert snapshot['observed']['rows'][3][5] == manifest['spec']['changes'][0]['after']
    assert sha256((task / 'sources/source.xlsx').read_bytes()).hexdigest() == ref['sha256']
    provenance = json.loads((task / 'sources/provenance.json').read_text(encoding='utf-8'))
    assert provenance['edited'] == ref and provenance['origin']['receipt_sha256']
    checked = local_skills._prepared_snapshot(task, snapshot, BASE)
    candidate = {'schema': 1, 'id': 'copy-table', 'version': 1, 'name': '完整表格副本',
                 'recipe': 'table_copy', 'sum_columns': [], 'draft': False}
    steps = local_skills.build_steps(candidate, checked)
    assert steps[0]['data']['rows'] == snapshot['observed']['rows']
    assert steps[0]['data']['headers'] == snapshot['observed']['headers']


@pytest.mark.parametrize('change', [
    {'sheet': 'Data', 'cell': 'A1', 'before': 1, 'after': 2},
    {'sheet': 'Data', 'cell': 'C2', 'before': 1, 'after': 2},
    {'sheet': 'Data', 'cell': 'A4', 'before': 1, 'after': 2},
    {'sheet': 'Data', 'cell': 'C3', 'before': 1, 'after': 2},
    {'sheet': 'Other', 'cell': 'A3', 'before': 1, 'after': 2},
])
def test_only_exact_header_and_data_cells_can_be_transferred(change):
    original = {'headers': ['one', 'two'], 'rows': [[1, 2]]}
    before = deepcopy(original)
    with pytest.raises(ValueError, match='edited_table_scope_invalid'):
        generated_edit_table._apply_changes(original, [change], 'Data')
    assert original == before


def test_approved_header_and_data_edits_preserve_full_table():
    original = {'headers': ['one', 'two'], 'rows': [[1, 2]]}
    table = generated_edit_table._apply_changes(original, [
        {'sheet': 'Data', 'cell': 'B2', 'before': 'two', 'after': '证据来源页面'},
        {'sheet': 'Data', 'cell': 'A3', 'before': 1, 'after': 3}], 'Data')
    assert table == {'headers': ['one', '证据来源页面'], 'rows': [[3, 2]]}
    assert original == {'headers': ['one', 'two'], 'rows': [[1, 2]]}
    with pytest.raises(ValueError, match='edited_table_before_mismatch'):
        generated_edit_table._apply_changes(original, [
            {'sheet': 'Data', 'cell': 'B2', 'before': 'forged', 'after': 'new'}], 'Data')


def test_changed_before_value_is_rejected_without_mutating_table():
    table = {'headers': ['one', 'two'], 'rows': [[1, 2]]}
    with pytest.raises(ValueError, match='edited_table_before_mismatch'):
        generated_edit_table._apply_changes(table, [{'sheet': 'Data', 'cell': 'B3',
            'before': 99, 'after': 3}], 'Data')
    assert table['rows'] == [[1, 2]]


@pytest.fixture
def derived_header_chain(tmp_path, monkeypatch):
    from agent_service import edited_artifact_source
    origin = tmp_path / ('task-' + '0' * 32) / ('workflow-' + '1' * 32) / 's1/spreadsheet.xlsx'
    paths = [tmp_path / ('task-' + str(n) * 32) / 'edit/edited-spreadsheet.xlsx'
             for n in (2, 3)]
    refs = [{'artifact': str(path), 'sha256': str(n) * 64} for n, path in enumerate(paths, 2)]
    specs = [{'kind': 'spreadsheet', 'changes': [
        {'sheet': 'Data', 'cell': 'B2', 'before': 'two', 'after': '证据来源页面'}]},
        {'kind': 'spreadsheet', 'changes': [
            {'sheet': 'Data', 'cell': 'B2', 'before': '证据来源页面', 'after': '来源页面'},
            {'sheet': 'Data', 'cell': 'A3', 'before': 1, 'after': 9}]}]
    roots = [tmp_path / 'source-1', tmp_path / 'source-2']
    monkeypatch.setattr(edited_artifact_source, 'prepare_reference',
                        lambda path, digest, base: refs[paths.index(Path(path))])
    monkeypatch.setattr(generated_edit_table, '_load', lambda path: (
        {'spec': specs[paths.index(path.parent / 'edited-spreadsheet.xlsx')],
         'source_root': str(roots[paths.index(path.parent / 'edited-spreadsheet.xlsx')])}, ''))
    monkeypatch.setattr(generated_edit_table, 'verify_edit_source', lambda path: {
        'sha256': refs[0]['sha256'], 'observed': {'sheets': [{'name': 'Data'}]}})
    monkeypatch.setattr(generated_edit_table, '_read', lambda path: {
        'original': str(origin if path.parent == roots[0] else paths[0])})
    monkeypatch.setattr(artifact_source, '_verified', lambda *a: ({'artifact': str(origin)}, {}, {}, 0))
    monkeypatch.setattr(artifact_source, '_extract', lambda *a: (
        'table', {'headers': ['one', 'two'], 'rows': [[1, 2]]}, {}))
    return refs, specs, tmp_path


def test_header_chain_coverage_and_order(derived_header_chain):
    refs, specs, base = derived_header_chain
    _, edits, table, coverage = generated_edit_table._derive(refs[-1], base)
    assert edits == refs and table == {'headers': ['one', '来源页面'], 'rows': [[9, 2]]}
    assert coverage == {'scope': 'verified_generated_edited_workbook_headers_and_data',
        'rows': 1, 'columns': 2, 'title_row_excluded': True, 'totals_row_excluded': True,
        'edited_data_cells': 1, 'edited_header_cells': 2, 'edit_depth': 2}
    specs[-1]['changes'][0]['before'] = 'two'
    with pytest.raises(ValueError, match='edited_table_before_mismatch'):
        generated_edit_table._derive(refs[-1], base)


@pytest.mark.parametrize('after', [None, 7, '', '  ', 'line\nbreak', 'x' * 2001])
def test_invalid_header_rejected_by_existing_writer_contract(derived_header_chain, after):
    refs, specs, base = derived_header_chain
    specs[0]['changes'][0]['after'] = after
    with pytest.raises(ValueError, match='source_writer_unsupported'):
        generated_edit_table._derive(refs[0], base)


def test_real_header_edit_reaches_report_without_losing_evidence(tmp_path):
    edited = BASE / 'task-b5eff3cee4b3488d9c8a411a52cfecd0/edit/edited-spreadsheet.xlsx'
    if not edited.is_file():
        pytest.skip('§77 preserved native header edit unavailable')
    digest = sha256(edited.read_bytes()).hexdigest()
    ref = prepare_reference(edited, digest, BASE)
    task = tmp_path / 'task'
    task.mkdir()
    snapshot = generated_edit_table.snapshot_if_generated(ref, task, BASE)
    checked = local_skills._prepared_snapshot(task, snapshot, BASE)
    assert checked['observed']['headers'][0] == '证据来源页面'
    assert checked['coverage']['edited_header_cells'] == 1
    assert checked['coverage']['edited_data_cells'] == 0
    assert len(checked['observed']['rows']) == 5
    candidate = {'schema': 1, 'id': 'evidence-table-report', 'version': 1,
        'name': '证据来源表原文转存报告', 'recipe': 'table_report', 'sum_columns': [], 'draft': False}
    steps = local_skills.build_steps(candidate, checked)
    assert len(steps) == 2 and steps[1]['action'] == 'document_from_sheet'
    assert steps[0]['data']['headers'] == checked['observed']['headers']
    assert steps[0]['data']['rows'] == checked['observed']['rows']
    assert steps[0]['data']['sum_columns'] == []
    assert sha256(edited.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('target', ['provenance', 'source_record', 'copied_bytes'])
def test_skill_gate_rejects_snapshot_tamper(prepared, target):
    task, snapshot, _, _ = prepared
    sources = task / 'sources'
    supplied = deepcopy(snapshot)
    if target == 'provenance':
        path = sources / 'provenance.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        record['origin']['receipt_sha256'] = '0' * 64
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    elif target == 'source_record':
        path = sources / 'source.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        record['observed']['rows'][3][5] = 'forged'
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        supplied = record
    else:
        with (sources / 'source.xlsx').open('ab') as stream:
            stream.write(b'tampered')
    with pytest.raises(ValueError):
        local_skills._prepared_snapshot(task, supplied, BASE)


def test_skill_gate_fails_closed_if_creation_receipt_no_longer_valid(prepared, monkeypatch):
    task, snapshot, _, _ = prepared

    def rejected(*args, **kwargs):
        raise ArtifactSourceError('artifact_source_receipt_mismatch')

    monkeypatch.setattr(artifact_source, '_verified', rejected)
    with pytest.raises(ArtifactSourceError, match='artifact_source_receipt_mismatch'):
        local_skills._prepared_snapshot(task, snapshot, BASE)


def test_origin_derived_totals_are_explicitly_rejected(prepared, monkeypatch):
    _, _, _, ref = prepared
    extract = artifact_source._extract

    def with_totals(*args, **kwargs):
        kind, observed, coverage = extract(*args, **kwargs)
        return kind, observed, {**coverage, 'derived_totals': ['F9']}

    monkeypatch.setattr(artifact_source, '_extract', with_totals)
    with pytest.raises(ValueError, match='^edited_table_totals_unsupported$'):
        generated_edit_table._derive(ref, BASE)


def test_general_excel_stays_outside_generated_edit_route(tmp_path):
    arbitrary = tmp_path / 'merged.xlsx'
    assert not generated_edit_table._generated_path(arbitrary, tmp_path)


def test_preserved_single_edit_snapshot_remains_usable_at_skill_gate():
    if not (LEGACY_TASK / 'sources/provenance.json').is_file():
        pytest.skip('preserved single-edit source unavailable')
    sources = LEGACY_TASK / 'sources'
    snapshot = json.loads((sources / 'source.json').read_text(encoding='utf-8'))
    provenance = json.loads((sources / 'provenance.json').read_text(encoding='utf-8'))
    assert set(provenance) == {'kind', 'edited', 'origin'}
    assert 'edit_depth' not in snapshot['coverage']
    assert generated_edit_table.verify_snapshot(LEGACY_TASK, snapshot, provenance, BASE) == (
        'table', snapshot['observed'], snapshot['coverage'])
    assert local_skills._prepared_snapshot(LEGACY_TASK, snapshot, BASE) == snapshot


def test_legacy_three_field_provenance_cannot_claim_a_deep_chain(monkeypatch):
    if not (LEGACY_TASK / 'sources/provenance.json').is_file():
        pytest.skip('preserved single-edit source unavailable')
    sources = LEGACY_TASK / 'sources'
    snapshot = json.loads((sources / 'source.json').read_text(encoding='utf-8'))
    provenance = json.loads((sources / 'provenance.json').read_text(encoding='utf-8'))
    derive = generated_edit_table._derive

    def deeper(ref, base):
        origin, edits, observed, coverage = derive(ref, base)
        return origin, edits + [edits[-1]], observed, {**coverage, 'edit_depth': 2}

    monkeypatch.setattr(generated_edit_table, '_derive', deeper)
    with pytest.raises(ValueError, match='edited_table_provenance_invalid'):
        generated_edit_table.verify_snapshot(LEGACY_TASK, snapshot, provenance, BASE)


def test_two_approved_excel_edits_apply_in_order(prepared, tmp_path, monkeypatch):
    _, first, _, first_ref = prepared
    latest = tmp_path / 'task-00000000000000000000000000000000' / 'edit' / 'edited-spreadsheet.xlsx'
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b'future-native-output')
    source_root = tmp_path / 'source-root'
    source_root.mkdir()
    latest_ref = {**first_ref, 'artifact': str(latest), 'sha256': sha256(latest.read_bytes()).hexdigest()}
    first_cell = first['observed']['rows'][0][0]
    next_cell = 'second approved edit'
    spec = {'kind': 'spreadsheet', 'changes': [{'sheet': 'Data', 'cell': 'A3',
            'before': first_cell, 'after': next_cell}]}
    load = generated_edit_table._load
    read = generated_edit_table._read
    verify = generated_edit_table.verify_edit_source
    from agent_service import edited_artifact_source
    prepare = edited_artifact_source.prepare_reference
    monkeypatch.setattr(edited_artifact_source, 'prepare_reference',
                        lambda path, digest, base: latest_ref if Path(path) == latest
                        else prepare(path, digest, base))
    monkeypatch.setattr(generated_edit_table, '_load',
                        lambda path: ({'spec': spec, 'source_root': str(source_root)}, '')
                        if Path(path) == latest.parent / 'edit-manifest.json' else load(path))
    monkeypatch.setattr(generated_edit_table, '_read',
                        lambda path: {'original': first_ref['artifact']}
                        if Path(path) == source_root / 'source.json' else read(path))
    monkeypatch.setattr(generated_edit_table, 'verify_edit_source',
                        lambda path: {'sha256': first_ref['sha256'],
                                      'observed': {'sheets': [{'name': 'Data'}]}}
                        if Path(path) == source_root else verify(path))
    origin, edits, table, coverage = generated_edit_table._derive(latest_ref, BASE)
    assert edits == [first_ref, latest_ref] and origin['receipt_sha256']
    assert coverage['edit_depth'] == 2 and coverage['edited_data_cells'] == 2
    assert table['rows'][0][0] == next_cell
    assert table['rows'][3][5] == first['observed']['rows'][3][5]
    spec['changes'][0]['before'] = 'unapproved value'
    with pytest.raises(ValueError, match='edited_table_before_mismatch'):
        generated_edit_table._derive(latest_ref, BASE)
