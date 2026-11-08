"""Offline edit receipts and complete managed projections; no native application."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import uuid
import zipfile
import xml.etree.ElementTree as ET

import pytest

from agent_service import artifact_source, edited_artifact_source, office_bundle as bundle
from agent_service import office_edit, office_workflow
from agent_service.computer import ComputerSession, digest, read_record
from agent_service.computer_agent_tools import _save
from agent_service.computer_interaction import prepare
import test_artifact_source
from test_artifact_source import owned, fake
from test_office_bundle import Client


def _package_fake(spec, root, stop):
    result = fake(spec, root, stop)
    path = Path(result['artifact'])
    if spec['kind'] == 'spreadsheet':
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        book = Workbook()
        sheet = book.active
        sheet.title = 'Data'
        sheet.append([spec['title']])
        sheet.append(spec['headers'])
        for row in spec['rows']:
            sheet.append(row)
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(spec['headers']))
        if spec['sum_columns']:
            from agent_service.office import _total_label_col
            label = _total_label_col(spec)
            if label is not None:
                sheet.cell(len(spec['rows'])+3, label+1, '合计')
        for column in spec['sum_columns']:
            letter = get_column_letter(column+1)
            sheet.cell(len(spec['rows'])+3, column+1, f'=SUM({letter}3:{letter}{len(spec["rows"])+2})')
        book.save(path)
        book.close()
        if spec['sum_columns']:
            # Offline cached totals match the existing creation receipt contract.
            with zipfile.ZipFile(path) as archive:
                parts = {name: archive.read(name) for name in archive.namelist()}
            xml = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            for column in spec['sum_columns']:
                cell = f'{get_column_letter(column+1)}{len(spec["rows"])+3}'
                xml.find(f'.//s:c[@r="{cell}"]/s:v', ns).text = str(sum(row[column] for row in spec['rows']))
            parts['xl/worksheets/sheet1.xml'] = ET.tostring(xml)
            with zipfile.ZipFile(path, 'w') as archive:
                for name, value in parts.items():
                    archive.writestr(name, value)
    else:
        with zipfile.ZipFile(path, 'a') as archive:
            archive.writestr('[Content_Types].xml',
                             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    result['sha256'] = sha256(path.read_bytes()).hexdigest()
    return result


def _edit(base, session, sources, monkeypatch, *, changes=None):
    selections = []
    for source in sources:
        change = ({'sheet': 'Data', 'cell': 'B2', 'before': '值', 'after': '任务事项'}
                  if Path(source).suffix == '.xlsx' else
                  {'table_index': 0, 'row_index': 0, 'column_index': 1,
                   'paragraph_index': 0, 'before': '值', 'after': '任务事项'})
        selections.append({'path': str(source), 'changes': [changes or change]})
    plan = prepare(session, '把两表的值表头改为任务事项', edits=selections)
    run = session.root/'goals'/(plan['sha256']+'.run')
    run.mkdir()
    _save(run/'environment-goal.json', plan['goal'])
    monkeypatch.setattr(office_edit, '_native_readback',
                        lambda *args: {'progid': 'offline-fixture', 'version': '1'})
    refs = []
    for n, item in enumerate(plan['goal']['edits'], 1):
        task = base/('task-'+uuid.uuid4().hex)
        output = task/'edit'
        output.mkdir(parents=True)
        step = run/f'step-{n:02}'
        step.mkdir()
        action = {'kind': 'office_edit', 'edit_id': item['id']}
        intent = {'action': action, 'observation_sha256': '0'*64}
        _save(step/'environment-intent.json', intent)
        native = office_edit.execute_edit(item['spec'], output, item['source_root'])
        assert native['ok'], native
        _save(output/'edit-manifest.json', {'source_root': item['source_root'], 'spec': item['spec'],
                                           'run': str(run), 'step': str(step)})
        _save(step/'environment-result.json', {'action': action, 'intent_sha256': digest(intent),
              'result': {'ok': True}, 'native': native})
        refs.append(edited_artifact_source.prepare_reference(native['artifact'], native['sha256'], base))
    return refs


@pytest.fixture
def prepared(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp('be')
    monkeypatch.setattr(test_artifact_source, 'fake', _package_fake)
    table = {'headers': ['项目', '值'], 'rows': [['甲', ''], ['乙', '0']]}
    base, _, _, workflow = owned(tmp_path,
        {'kind': 'spreadsheet', 'title': '表', **table, 'sum_columns': []},
        second={'kind': 'document', 'title': '报告', 'paragraphs': ['范围'], 'table': table})
    session = ComputerSession.create(tmp_path/'computer')
    monkeypatch.setattr(bundle, 'BASE', session.root.parent)
    sources = [r['artifact'] for r in workflow['receipts'].values()]
    refs = _edit(base, session, sources, monkeypatch)
    return session, base, refs, sources


def _run(prepared):
    session, base, refs, _ = prepared
    plan = bundle.prepare(session, '逐格核对两份表格并生成Excel和Word核对报告', refs, task_base=base)
    def runner(steps, root, **kwargs):
        return office_workflow.run_office_workflow(steps, root, execute=fake, **kwargs)
    return bundle.execute(session, plan, approval=plan['sha256'], task_base=base,
                          client=Client(), workflow_runner=runner)


def test_real_edit_proofs_reach_complete_comparison_and_saved_inspection(prepared):
    session, base, refs, sources = prepared
    originals = {p: sha256(Path(p).read_bytes()).hexdigest() for p in sources}
    result = _run(prepared)
    assert result['ok'] and result['positions'] == 6 and result['differences'] == 0, result
    assert bundle.inspect_saved(result['run'], task_base=base, computer_base=session.root.parent) == result
    task = Path(result['task_root'])
    inputs = read_record(task/'inputs.json')['inputs']
    assert [v['ref'] for v in inputs] == refs
    assert inputs[0]['observed']['headers'] == ['项目', '任务事项']
    assert inputs[1]['observed']['table']['headers'] == ['项目', '任务事项']
    for item in inputs:
        assert sha256(Path(item['snapshot']).read_bytes()).hexdigest() == item['ref']['sha256']
    assert originals == {p: sha256(Path(p).read_bytes()).hexdigest() for p in sources}
    for output in result['artifacts']:
        artifact_source.prepare_artifact_source(output['path'], output['sha256'], base)


@pytest.mark.parametrize('mutation', ['copy', 'provenance', 'observation', 'native_receipt', 'ancestor'])
def test_saved_edited_comparison_rejects_tampering(prepared, mutation):
    session, base, refs, sources = prepared
    result = _run(prepared)
    assert result['ok'], result
    task = Path(result['task_root'])
    if mutation == 'copy':
        path = task/'inputs/S1/sources/source.xlsx'
    elif mutation in {'provenance', 'observation'}:
        path = task/'inputs/S1/sources'/('provenance.json' if mutation == 'provenance' else 'source.json')
    elif mutation == 'ancestor':
        path = Path(sources[0])
    else:
        path = Path(refs[0]['artifact']).parent/'native-edit-receipt.json'
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises((ValueError, KeyError, TypeError, OSError)):
        bundle.inspect_saved(result['run'], task_base=base, computer_base=session.root.parent)


def test_mixed_edited_and_original_refs_preserve_old_projection(prepared):
    _, base, refs, sources = prepared
    refs[1] = artifact_source.prepare_artifact_source(sources[1], sha256(Path(sources[1]).read_bytes()).hexdigest(), base)
    result = _run(prepared)
    assert result['ok'] and result['differences'] == 1, result


def test_ref_receipt_mismatch_rejected_before_preview(prepared):
    session, base, refs, _ = prepared
    refs = deepcopy(refs)
    refs[0]['receipt_sha256'] = '0'*64
    with pytest.raises(ValueError):
        bundle.prepare(session, '逐格核对', refs, task_base=base)


def test_second_edit_inherits_complete_creation_projection(prepared, monkeypatch):
    session, base, refs, _ = prepared
    # A second real spreadsheet edit keeps the original creation proof and depth.
    again = _edit(base, session, [refs[0]['artifact']], monkeypatch,
                  changes={'sheet': 'Data', 'cell': 'B2', 'before': '任务事项', 'after': '新事项'})
    refs[0] = again[0]
    result = _run(prepared)
    assert result['ok'] and result['differences'] == 1, result
    inputs = read_record(Path(result['task_root'])/'inputs.json')['inputs']
    assert inputs[0]['coverage']['edit_depth'] == 2


def test_formula_totals_projection_is_refused(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp('bt')
    monkeypatch.setattr(test_artifact_source, 'fake', _package_fake)
    table = {'headers': ['项目', '值'], 'rows': [['甲', 3]]}
    base, _, _, workflow = owned(tmp_path,
        {'kind': 'spreadsheet', 'title': '表', **table, 'sum_columns': [1]},
        second={'kind': 'document', 'title': '报告', 'paragraphs': ['范围'],
                'table': {'headers': table['headers'], 'rows': [['甲', '3']]}})
    session = ComputerSession.create(tmp_path/'computer')
    sources = [r['artifact'] for r in workflow['receipts'].values()]
    refs = _edit(base, session, sources, monkeypatch)
    task = base/('task-'+uuid.uuid4().hex)
    task.mkdir()
    with pytest.raises(ValueError, match='edited_table_totals_unsupported'):
        bundle._inputs(task, refs, base, create=True)


@pytest.mark.parametrize('index', [0, 1])
def test_external_edited_layout_does_not_gain_managed_projection(prepared, monkeypatch, tmp_path, index):
    session, base, refs, sources = prepared
    original = Path(sources[index])
    external = tmp_path/('external'+original.suffix)
    external.write_bytes(original.read_bytes())
    refs[index] = _edit(base, session, [external], monkeypatch)[0]
    task = base/('task-'+uuid.uuid4().hex)
    task.mkdir()
    with pytest.raises(ValueError, match='bundle_edited_projection_unsupported'):
        bundle._inputs(task, refs, base, create=True)


def test_edit_chain_depth_above_four_is_refused(prepared, monkeypatch):
    session, base, refs, _ = prepared
    before = '任务事项'
    for n in range(4):
        after = f'事项{n}'
        refs[0] = _edit(base, session, [refs[0]['artifact']], monkeypatch,
                        changes={'sheet': 'Data', 'cell': 'B2', 'before': before, 'after': after})[0]
        before = after
    task = base/('task-'+uuid.uuid4().hex)
    task.mkdir()
    with pytest.raises(ValueError, match='edited_table_chain_invalid'):
        bundle._inputs(task, refs, base, create=True)


def test_edit_ancestry_is_checked_inside_prospective_bundle_depth(prepared, monkeypatch):
    from agent_service import generated_edit_table
    from agent_service.computer import ComputerError
    session, base, refs, _ = prepared
    seen = []
    def full_ancestry(ref, task_base):
        seen.append(bundle._checking.get())
        raise ComputerError('bundle_source_chain_invalid')
    monkeypatch.setattr(generated_edit_table, '_derive', full_ancestry)
    with pytest.raises(ComputerError, match='bundle_source_chain_invalid'):
        bundle.prepare(session, '逐格核对', refs, task_base=base)
    assert len(seen) == 1 and len(seen[0]) == 1
    assert bundle._checking.get() == ()
