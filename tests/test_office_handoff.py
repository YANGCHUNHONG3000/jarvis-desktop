"""Generated output references stay bound through natural-language planning."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from agent_service import office_handoff as handoff, office_intent as intent
from agent_service.computer import ComputerError, ComputerSession
from test_office_intent import Client, valid_response
from test_office_edit import word_fixture, sheet_fixture


@pytest.fixture
def selected(tmp_path, monkeypatch):
    files = [tmp_path / 'brief.docx', tmp_path / 'budget.xlsx']
    word_fixture(files[0])
    sheet_fixture(files[1])
    records = [{'path': str(p), 'sha256':sha256(p.read_bytes()).hexdigest()} for p in files]
    receipts = {str(p): {'artifact':str(p), 'sha256':r['sha256'], 'receipt_sha256':'a'*64}
                for p, r in zip(files, records)}
    calls = []
    def prepare(path, expected, base):
        path = str(path)
        calls.append(path)
        if sha256(files[[str(p) for p in files].index(path)].read_bytes()).hexdigest() != expected:
            raise ValueError('artifact_changed')
        return deepcopy(receipts[path])
    monkeypatch.setattr(handoff, 'prepare_artifact_source', prepare)
    return ComputerSession.create(tmp_path / 'computer'), files, records, receipts, calls


def test_selects_entire_explicit_batch_and_revalidates_without_model(selected):
    session, files, records, receipts, calls = selected
    refs = handoff.select_outputs(records)
    handoff.verify_references(files, refs)
    assert calls == [str(p) for p in files] * 2
    assert refs == list(receipts.values())


@pytest.mark.parametrize('records', [[], [{}]*5, [{'path':'x.docx','sha256':'a'*64}],
                                   [{'path':'bad.pdf','sha256':'a'*64}], [{'path':123,'sha256':'a'*64}]])
def test_invalid_batches_rejected_before_receipt_lookup(records, monkeypatch):
    monkeypatch.setattr(handoff, 'prepare_artifact_source', lambda *a: pytest.fail('unexpected lookup'))
    with pytest.raises(ComputerError):
        handoff.select_outputs(records)


def test_duplicates_and_reordered_references_rejected(selected):
    _, files, records, _, _ = selected
    with pytest.raises(ComputerError):
        handoff.select_outputs([records[0], records[0]])
    refs = handoff.select_outputs(records)
    with pytest.raises(ComputerError):
        handoff.verify_references(files, list(reversed(refs)))


def test_proposal_keeps_receipts_local_and_uses_original_sources(selected):
    session, files, records, _, calls = selected
    refs = handoff.select_outputs(records)
    client = Client()
    result = intent.propose(session, '确认预算', files, client=client, artifact_refs=refs)
    assert result['plan'] and len(calls) == 6
    saved = list((session.root / 'office-planning').glob('*/artifact-references.json'))
    assert len(saved) == 1 and json.loads(saved[0].read_text(encoding='utf-8')) == refs
    context = json.dumps(client.contexts)
    assert str(files[0]) not in context and 'receipt_sha256' not in context


@pytest.mark.parametrize('when', ['before', 'during'])
def test_receipt_change_revokes_plan(selected, when):
    session, files, records, receipts, _ = selected
    refs = handoff.select_outputs(records)
    def change():
        receipts[str(files[0])]['receipt_sha256'] = 'b'*64
    if when == 'before':
        change()
    def respond(context):
        change()
        return valid_response(context)
    client = Client(respond)
    with pytest.raises(ComputerError, match='office_handoff_reference_changed'):
        intent.propose(session, '确认预算', files, client=client, artifact_refs=refs)
    assert len(client.contexts) == (0 if when == 'before' else 1)


def test_changed_file_between_selection_and_planning_is_not_uploaded(selected):
    session, files, records, _, _ = selected
    refs = handoff.select_outputs(records)
    files[0].write_bytes(b'changed')
    client = Client()
    with pytest.raises(ComputerError):
        intent.propose(session, '确认预算', files, client=client, artifact_refs=refs)
    assert client.contexts == []


def test_snapshot_race_is_rejected_before_model(selected, monkeypatch):
    session, files, records, _, _ = selected
    refs = handoff.select_outputs(records)
    snapshot = intent.snapshot_edit_source
    def raced(path, root):
        result = snapshot(path, root)
        result['sha256'] = 'f'*64
        return result
    monkeypatch.setattr(intent, 'snapshot_edit_source', raced)
    client = Client()
    with pytest.raises(ComputerError, match='office_handoff_snapshot_changed'):
        intent.propose(session, '确认预算', files, client=client, artifact_refs=refs)
    assert client.contexts == []


def test_restart_discovers_latest_entire_batch_and_verifies(selected,tmp_path):
    import os
    _,files,records,_,calls=selected
    base=tmp_path/'sessions'; session=base/'computer-new'
    def saved(name,artifacts,mtime):
        path=session/'goals'/(name+'.run')/'outcome.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'engine':'observed_agent','state':'verified','ok':True,'run':str(path.parent),'artifacts':artifacts}),encoding='utf-8')
        os.utime(path,ns=(mtime,mtime))
        return path
    saved('old',[records[0]],100)
    latest=saved('new',records,200)
    assert handoff.latest_generated_outputs(base=base)==records
    assert calls==[str(p) for p in files]
    files[1].write_bytes(b'tampered latest')
    with pytest.raises(ValueError,match='artifact_changed'):
        handoff.latest_generated_outputs(base=base)
    # A malformed latest batch also blocks selection of the older valid file.
    latest.write_text(json.dumps({'engine':'observed_agent','state':'verified','artifacts':[{}]}),encoding='utf-8')
    with pytest.raises(ComputerError,match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=base)


def test_history_discovery_caps_outcome_reads_at_one_hundred(tmp_path,monkeypatch):
    base=tmp_path/'sessions'; session=base/'computer-one'
    for i in range(105):
        path=session/'goals'/(str(i)+'.run')/'outcome.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('{}',encoding='utf-8')
    reads=[]
    monkeypatch.setattr(handoff,'read_record',lambda path: reads.append(path) or {})
    assert handoff.latest_generated_outputs(base=base)==[]
    assert len(reads)==100


def test_history_output_batch_must_bind_current_run(selected,tmp_path):
    _,_,records,_,calls=selected
    base=tmp_path/'sessions'
    path=base/'computer-one'/'goals'/'new.run'/'outcome.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'engine':'observed_agent','state':'verified','ok':True,
                               'run':str(path.parent/'wrong'),'artifacts':records}),encoding='utf-8')
    with pytest.raises(ComputerError,match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=base)
    assert calls==[]
