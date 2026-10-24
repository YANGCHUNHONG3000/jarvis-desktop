"""Offline v2 selection, source binding and no-replay checks; no native effects."""
import json
from pathlib import Path

import pytest

from agent_service import capability_router as router, capability_source_choice as choice
from agent_service.computer import ComputerError, ComputerSession, read_record, write_new


class Client:
    def __init__(self, source_id='S2'):
        self.answer = {'source_id': source_id, 'reason': '输入为已生成的Excel'}
        self.calls = []

    def complete(self, system, user, **kwargs):
        kwargs.update(system=system, user=user)
        self.calls.append(kwargs)
        return self.answer


def test_cancel_message_does_not_claim_selection_call_was_free():
    message = router.describe_result({'ok': False, 'code': 'capability_cancelled'})
    assert '已发生的模型费用' in message


@pytest.fixture
def setup(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp('choice')
    refs = [{'artifact': str(tmp_path / name), 'sha256': str(i) * 64, 'version': 1}
            for i, name in enumerate(('document.docx', 'spreadsheet.xlsx'), 1)]
    from agent_service import office_handoff
    def verified(files, selected, **kwargs):
        assert files == [r['artifact'] for r in selected]
        if any(r not in refs for r in selected) or len(files) != len(set(files)):
            raise ComputerError('capability_source_invalid')
    monkeypatch.setattr(office_handoff, 'verify_references', verified)
    session = ComputerSession.create(tmp_path / 'sessions')
    base = tmp_path / 'tasks'
    plan = router.prepare(session, '根据上一任务Excel生成Word报告', source_refs=refs, task_base=base)
    return session, base, refs, plan


def run_task(instruction, base, *, task_root, client, source_artifact):
    # Source provenance uses the actual creation wrapper shape, not the bare ref.
    write_new(task_root / 'result.json', {'ok': False, 'state': 'unsupported', 'kind': 'unsupported',
        'root': str(task_root), 'reason': 'offline executor fixture',
        'source': {'sha256': source_artifact['sha256']},
        'source_provenance': {'kind': 'verified_generated_office_artifact', **source_artifact}})


def execute(setup, client, **kwargs):
    s, base, _, plan = setup
    return router.execute(s, plan, approval=plan['sha256'], task_base=base, client=client,
                          runner=kwargs.pop('runner', run_task), **kwargs)


def test_preview_is_local_and_binds_all_candidates(setup):
    s, base, refs, plan = setup
    assert plan['schema'] == 2 and plan['source_refs'] == refs and plan['source_ref'] is None
    assert not base.exists() and not list((s.root / 'model_calls').iterdir())
    assert '未选正文不上传' in router.describe(plan)


def test_model_chooses_input_then_same_approval_only_checks_saved_result(setup):
    client = Client()
    result = execute(setup, client)
    assert result['source_ref'] == setup[2][1] and len(client.calls) == 1
    sent = json.loads(client.calls[0]['user'])
    assert sent['sources'][0]['filename'] == 'document.docx'
    assert str(setup[1].parent) not in client.calls[0]['user']
    assert 'INPUT' in client.calls[0]['system'] and 'OUTPUT' in client.calls[0]['system']
    assert execute(setup, client, runner=lambda *a, **kw: pytest.fail('replayed')) == result
    assert len(client.calls) == 1


def test_single_candidate_needs_no_selection_model(setup):
    s, base, refs, _ = setup
    plan = router.prepare(s, '处理这份文档', source_refs=refs[:1], task_base=base)
    client = Client()
    result = execute((s, base, refs, plan), client)
    assert result['source_ref'] == refs[0] and not client.calls


def test_ambiguous_or_multiple_inputs_do_not_create_task_and_do_not_reselect(setup):
    client = Client(None)
    result = execute(setup, client, runner=lambda *a, **kw: pytest.fail('ambiguous dispatch'))
    assert result['state'] == 'needs_input' and result['before_start']
    assert not setup[1].exists()
    assert execute(setup, client) == result and len(client.calls) == 1


@pytest.mark.parametrize('answer', [
    {'source_id': 'S3', 'reason': 'outside'}, {'source_id': 1, 'reason': 'wrong type'},
    {'source_id': 'S1', 'reason': ''}, {'source_id': 'S1', 'reason': 'ok', 'extra': True},
])
def test_bad_model_selection_stops_and_never_replays(setup, answer):
    client = Client()
    client.answer = answer
    with pytest.raises(ComputerError, match='capability_source_choice_invalid'):
        execute(setup, client)
    assert not setup[1].exists()
    assert execute(setup, client)['state'] == 'unknown' and len(client.calls) == 1


@pytest.mark.parametrize('field,value', [('source_id', 'S1'), ('reason', 'rewritten'), ('approval', '0' * 64)])
def test_saved_choice_change_is_rejected(setup, field, value):
    result = execute(setup, Client())
    path = Path(result['run']) / 'source-selection.json'
    record = read_record(path)
    record[field] = value
    path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ComputerError):
        router.inspect_saved(result['run'], task_base=setup[1], computer_base=setup[0].root.parent)


def test_wrong_task_source_cannot_be_published(setup):
    def wrong(*args, **kwargs):
        kwargs['source_artifact'] = setup[2][0]
        run_task(*args, **kwargs)
    with pytest.raises(ComputerError, match='capability_task_source_mismatch'):
        execute(setup, Client(), runner=wrong)
    assert execute(setup, Client())['state'] == 'unknown'


def test_cancel_before_selection_does_not_call_model_or_make_task(setup):
    client = Client()
    result = execute(setup, client, stop=lambda: True)
    assert result['state'] == 'cancelled' and not client.calls and not setup[1].exists()
    assert execute(setup, client) == result and not client.calls


def test_source_mutated_during_selection_rejected_before_task(setup):
    client = Client()
    complete = client.complete
    def mutate(*args, **kwargs):
        setup[2][1]['sha256'] = 'f' * 64
        return complete(*args, **kwargs)
    client.complete = mutate
    with pytest.raises(ComputerError, match='capability_source_invalid'):
        execute(setup, client)
    assert not setup[1].exists()


@pytest.mark.parametrize('refs', [[], [1], [{'artifact': 'x'}] * 5])
def test_invalid_candidate_batch_rejected_before_model(tmp_path, refs):
    with pytest.raises(ComputerError, match='capability_source_candidates_invalid'):
        choice.verify_refs(refs, tmp_path)


@pytest.mark.parametrize('kind', ['verified_generated_edited_table', 'verified_edited_document',
                                'verified_edited_workbook', 'office_edit'])
def test_edited_source_provenance_binding_keeps_full_ref(tmp_path, kind):
    ref = {'kind': 'office_edit', 'artifact': str(tmp_path / 'edited.xlsx'), 'sha256': 'a' * 64}
    plan = {'sha256': 'b' * 64, 'source_refs': [ref]}
    run = tmp_path / 'run'
    run.mkdir()
    choice.choose(plan, object(), run)
    task = tmp_path / 'task'
    task.mkdir()
    provenance = ref if kind == 'office_edit' else {'kind': kind, 'edited': ref}
    write_new(task / 'result.json', {'source_provenance': provenance, 'source': {'sha256': ref['sha256']}})
    assert choice.bind_result(plan, run, task, {'ok': True})['source_ref'] == ref
