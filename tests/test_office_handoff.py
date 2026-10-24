"""Generated output references stay bound through natural-language planning."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

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
    task_base=tmp_path/'assistant_tasks'
    assert handoff.latest_generated_outputs(base=base,task_base=task_base)==records
    assert calls==[str(p) for p in files]
    files[1].write_bytes(b'tampered latest')
    with pytest.raises(ValueError,match='artifact_changed'):
        handoff.latest_generated_outputs(base=base,task_base=task_base)
    # A malformed latest batch also blocks selection of the older valid file.
    latest.write_text(json.dumps({'engine':'observed_agent','state':'verified','artifacts':[{}]}),encoding='utf-8')
    with pytest.raises(ComputerError,match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=base,task_base=task_base)


def test_history_discovery_caps_outcome_reads_at_one_hundred(tmp_path,monkeypatch):
    base=tmp_path/'sessions'; session=base/'computer-one'
    for i in range(105):
        path=session/'goals'/(str(i)+'.run')/'outcome.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('{}',encoding='utf-8')
    reads=[]
    monkeypatch.setattr(handoff,'read_record',lambda path: reads.append(path) or {})
    assert handoff.latest_generated_outputs(base=base,task_base=tmp_path/'assistant_tasks')==[]
    assert len(reads)==100


def test_history_output_batch_must_bind_current_run(selected,tmp_path):
    _,_,records,_,calls=selected
    base=tmp_path/'sessions'
    path=base/'computer-one'/'goals'/'new.run'/'outcome.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'engine':'observed_agent','state':'verified','ok':True,
                               'run':str(path.parent/'wrong'),'artifacts':records}),encoding='utf-8')
    with pytest.raises(ComputerError,match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=base,task_base=tmp_path/'assistant_tasks')
    assert calls==[]


def test_restart_picks_new_capability_batch_and_rejects_latest_tampering(selected, tmp_path, monkeypatch):
    from agent_service import capability_router as router

    session, _, records, _, calls = selected
    task_base = tmp_path / 'tasks'
    plan = router.prepare(session, '复制表格', task_base=task_base)
    monkeypatch.setattr(router, '_inspect', lambda task, base: {
        'ok': True, 'state': 'succeeded', 'code': 'capability_verified',
        'task_root': str(task), 'artifacts': records, 'skill': None, 'gap': None,
        'result_sha256': 'a' * 64,
    })
    router.execute(session, plan, approval=plan['sha256'], task_base=task_base,
                   client=object(), runner=lambda *args, **kwargs: None)
    latest = session.root / 'goals' / (plan['sha256'] + '.capability') / 'outcome.json'
    old = session.root / 'goals' / 'old.run' / 'outcome.json'
    old.parent.mkdir()
    old.write_text(json.dumps({'engine': 'observed_agent', 'state': 'verified',
                               'ok': True, 'run': str(old.parent), 'artifacts': [records[0]]}),
                   encoding='utf-8')
    os.utime(old, ns=(100, 100))
    os.utime(latest, ns=(200, 200))

    assert handoff.latest_generated_outputs(base=session.root.parent, task_base=task_base) == records
    assert calls == [item['path'] for item in records]

    saved = json.loads(latest.read_text(encoding='utf-8'))
    latest.write_text(json.dumps({**saved, 'artifacts': [records[0]]}), encoding='utf-8')
    with pytest.raises(ComputerError, match='capability_result_changed'):
        handoff.latest_generated_outputs(base=session.root.parent, task_base=task_base)


def _assistant_task(base, name, state, records, mtime):
    task = base / name
    task.mkdir(parents=True)
    result = task / 'result.json'
    result.write_text(json.dumps({'root': str(task), 'kind': 'office', 'state': state}),
                      encoding='utf-8')
    os.utime(result, ns=(mtime, mtime))
    return task


def test_assistant_task_ordinary_success_is_discovered_when_computer_base_empty(selected, tmp_path, monkeypatch):
    from agent_service import task_history
    _, _, records, _, calls = selected
    task_base = tmp_path / 'assistant_tasks'
    task = _assistant_task(task_base, 'task-' + 'a' * 32, 'succeeded', records, 200)
    monkeypatch.setattr(task_history, 'inspect_task', lambda root, base: {
        'root': str(root), 'state': 'succeeded', 'artifacts': [
            {'path': r['path'], 'sha256': r['sha256']} for r in records]})

    assert handoff.latest_generated_outputs(base=tmp_path / 'missing-computer',
                                           task_base=task_base) == records
    assert calls == [r['path'] for r in records]


def test_recovered_failed_task_uses_verified_successful_continuation(selected, tmp_path, monkeypatch):
    from agent_service import task_history, office_resume
    _, _, records, _, _ = selected
    task_base = tmp_path / 'assistant_tasks'
    task = _assistant_task(task_base, 'task-' + 'b' * 32, 'failed', records, 100)
    attempt_id = '1' * 32
    pointer = task / 'resume-latest.json'
    attempt = task / ('resume-' + attempt_id)
    attempt.mkdir()
    (attempt / 'outcome.json').write_text(json.dumps({'state': 'succeeded'}), encoding='utf-8')
    pointer.write_text(json.dumps({'attempt_id': attempt_id}), encoding='utf-8')
    os.utime(pointer, ns=(300, 300))
    monkeypatch.setattr(office_resume, 'latest_continuation', lambda root, base: {
        'state': 'succeeded', 'artifacts': records})
    monkeypatch.setattr(task_history, 'inspect_task', lambda root, base: {
        'root': str(root), 'state': 'failed', 'continuation': {'state': 'succeeded'},
        'artifacts': [{'path': r['path'], 'sha256': r['sha256']} for r in records]})

    assert handoff.latest_generated_outputs(base=tmp_path / 'missing-computer',
                                           task_base=task_base) == records


def test_newer_invalid_completed_assistant_task_blocks_older_success(selected, tmp_path, monkeypatch):
    from agent_service import task_history
    _, _, records, _, calls = selected
    task_base = tmp_path / 'assistant_tasks'
    older = _assistant_task(task_base, 'task-' + 'c' * 32, 'succeeded', records, 100)
    newer = _assistant_task(task_base, 'task-' + 'd' * 32, 'succeeded', records, 300)
    monkeypatch.setattr(task_history, 'inspect_task', lambda root, base: {
        'root': str(root), 'state': 'succeeded' if root == older else 'needs_review',
        'artifacts': [{'path': r['path'], 'sha256': r['sha256']} for r in records]})

    with pytest.raises(ComputerError, match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=tmp_path / 'missing-computer', task_base=task_base)
    assert calls == []


def test_unknown_assistant_task_is_not_auto_selected(selected, tmp_path, monkeypatch):
    from agent_service import task_history
    _, _, _, _, calls = selected
    task_base = tmp_path / 'assistant_tasks'
    _assistant_task(task_base, 'task-' + 'e' * 32, 'unknown', [], 300)
    monkeypatch.setattr(task_history, 'inspect_task', lambda *_: pytest.fail('unknown must not inspect'))

    assert handoff.latest_generated_outputs(base=tmp_path / 'missing-computer',
                                           task_base=task_base) == []
    assert calls == []


def test_office_batch_skips_email_artifact_without_losing_documents(selected, tmp_path, monkeypatch):
    from agent_service import task_history
    _, _, records, _, calls = selected
    task_base = tmp_path / 'assistant_tasks'
    task = _assistant_task(task_base, 'task-' + 'f' * 32, 'succeeded', records, 200)
    email = {'path': str(task / 'message.eml'), 'sha256': 'c' * 64}
    artifacts = [{'path': r['path'], 'sha256': r['sha256']} for r in records] + [email]
    monkeypatch.setattr(task_history, 'inspect_task', lambda root, base: {
        'root': str(root), 'state': 'succeeded', 'artifacts': artifacts})

    assert handoff.latest_generated_outputs(base=tmp_path / 'missing-computer',
                                           task_base=task_base) == records
    assert calls == [r['path'] for r in records]


def test_newest_task_pointer_attempt_traversal_is_rejected_before_outside_read(
        selected, tmp_path, monkeypatch):
    _, _, _, _, _ = selected
    task_base = tmp_path / 'assistant_tasks'
    task = _assistant_task(task_base, 'task-' + '1' * 32, 'failed', [], 100)
    pointer = task / 'resume-latest.json'
    pointer.write_text(json.dumps({'attempt_id': '../outside'}), encoding='utf-8')
    os.utime(pointer, ns=(300, 300))
    reads = []
    original = handoff.read_record
    def track(path):
        reads.append(Path(path).absolute())
        return original(path)
    monkeypatch.setattr(handoff, 'read_record', track)

    with pytest.raises(ComputerError, match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=tmp_path / 'missing-computer', task_base=task_base)
    assert all('outside' not in str(path) for path in reads)


def test_newest_task_pointer_attempt_junction_is_rejected_before_outside_read(
        selected, tmp_path, monkeypatch):
    _, _, _, _, _ = selected
    task_base = tmp_path / 'assistant_tasks'
    task = _assistant_task(task_base, 'task-' + '2' * 32, 'failed', [], 100)
    outside = tmp_path / 'outside-attempt'
    outside.mkdir()
    (outside / 'outcome.json').write_text(json.dumps({'state': 'succeeded'}), encoding='utf-8')
    attempt_id = '2' * 32
    attempt = task / ('resume-' + attempt_id)
    try:
        attempt.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlink creation unavailable')
    pointer = task / 'resume-latest.json'
    pointer.write_text(json.dumps({'attempt_id': attempt_id}), encoding='utf-8')
    os.utime(pointer, ns=(300, 300))
    reads = []
    original = handoff.read_record
    def track(path):
        value = Path(path).absolute()
        reads.append(value)
        if value == (outside / 'outcome.json').absolute():
            pytest.fail('read followed the attempt junction')
        return original(path)
    monkeypatch.setattr(handoff, 'read_record', track)

    with pytest.raises(ComputerError, match='office_handoff_history_invalid'):
        handoff.latest_generated_outputs(base=tmp_path / 'missing-computer', task_base=task_base)
    assert (outside / 'outcome.json').absolute() not in reads


def test_only_newest_continuation_gets_full_validation(selected, tmp_path, monkeypatch):
    from agent_service import task_history, office_resume
    _, _, records, _, _ = selected
    task_base = tmp_path / 'assistant_tasks'
    older = _assistant_task(task_base, 'task-' + '3' * 32, 'failed', records, 100)
    attempt_id = '3' * 32
    attempt = older / ('resume-' + attempt_id)
    attempt.mkdir()
    (attempt / 'outcome.json').write_text(json.dumps({'state': 'succeeded'}), encoding='utf-8')
    pointer = older / 'resume-latest.json'
    pointer.write_text(json.dumps({'attempt_id': attempt_id}), encoding='utf-8')
    os.utime(pointer, ns=(200, 200))
    newest = _assistant_task(task_base, 'task-' + '4' * 32, 'succeeded', records, 300)
    monkeypatch.setattr(office_resume, 'latest_continuation',
                        lambda *_: pytest.fail('older candidate was fully verified'))
    monkeypatch.setattr(task_history, 'inspect_task', lambda root, base: {
        'root': str(root), 'state': 'succeeded',
        'artifacts': [{'path': r['path'], 'sha256': r['sha256']} for r in records]})

    assert handoff.latest_generated_outputs(base=tmp_path / 'missing-computer',
                                           task_base=task_base) == records
    assert newest.exists()
