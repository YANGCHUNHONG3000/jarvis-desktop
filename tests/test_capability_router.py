"""Offline authorization and replay boundaries for capability routing."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import capability_router as router
from agent_service.artifact_source import ArtifactSourceError
from agent_service.computer import ComputerError, ComputerSession, write_new
from test_artifact_source import owned, sheet_spec


@pytest.fixture
def local(tmp_path):
    session = ComputerSession.create(tmp_path / 'sessions')
    return session, tmp_path / 'tasks'


def _plan(local, *, source_ref=None):
    session, base = local
    return router.prepare(session, '整理已有资料并生成表格', source_ref=source_ref,
                          task_base=base)


def _run(local, plan, **kwargs):
    session, base = local
    return router.execute(session, plan, approval=plan['sha256'], task_base=base,
                          client=object(), **kwargs)


def test_preview_creates_real_plan_without_model_or_task_effect(local, monkeypatch):
    from agent_service import model

    monkeypatch.setattr(model, 'CloudClient', lambda **kw: pytest.fail('preview used model'))
    plan = _plan(local)
    session, base = local
    assert plan['mode'] == 'capability' and plan['instruction'] == '整理已有资料并生成表格'
    assert router.describe(plan).count('批准后') >= 1
    assert not list((session.root / 'model_calls').iterdir())
    assert not base.exists() or not list(base.glob('task-*'))
    assert list((session.root / 'goals').glob('*.json'))


def test_changed_plan_or_saved_preview_refused_before_runner(local):
    plan = _plan(local)
    calls = []
    changed = deepcopy(plan)
    changed['instruction'] = '另一个任务'
    with pytest.raises(ComputerError, match='capability_approval_invalid'):
        _run(local, changed, runner=lambda *a, **kw: calls.append(1))
    saved = local[0].root / 'goals' / (plan['sha256'] + '.json')
    saved.write_text(json.dumps({**plan, 'instruction': '记录被改动'}, ensure_ascii=False),
                     encoding='utf-8')
    with pytest.raises(ComputerError, match='capability_approval_invalid'):
        _run(local, plan, runner=lambda *a, **kw: calls.append(1))
    assert calls == []


def test_source_is_rechecked_after_preview_before_task_start(local, monkeypatch):
    from agent_service import office_handoff

    state = {'valid': True, 'checks': 0}

    def verify(artifacts, refs, *, base):
        state['checks'] += 1
        assert artifacts == ['selected.docx'] and refs[0]['artifact'] == 'selected.docx'
        if not state['valid']:
            raise ComputerError('capability_source_invalid')

    monkeypatch.setattr(office_handoff, 'verify_references', verify)
    plan = _plan(local, source_ref={'artifact': 'selected.docx'})
    assert state['checks'] == 1
    state['valid'] = False
    calls = []
    with pytest.raises(ComputerError, match='capability_source_invalid'):
        _run(local, plan, runner=lambda *a, **kw: calls.append(1))
    assert state['checks'] == 2 and calls == []
    assert not (local[0].root / 'goals' / (plan['sha256'] + '.capability')).exists()


def test_unverified_source_cannot_enter_preview(local):
    session, base = local
    with pytest.raises(ArtifactSourceError, match='artifact_source_digest_invalid'):
        _plan(local, source_ref={'artifact': str(base / 'unverified.docx')})
    assert not (session.root / 'goals').exists()


def test_same_plan_returns_checked_previous_result_without_second_run(local):
    plan = _plan(local)
    calls = []

    def runner(instruction, base, *, task_root, client, source_artifact):
        calls.append(task_root)
        (task_root / 'result.json').write_text(json.dumps({
            'ok': False, 'state': 'unsupported', 'kind': 'unsupported',
            'root': str(task_root), 'reason': 'no_matching_adapter'},
            ensure_ascii=False), encoding='utf-8')

    first = _run(local, plan, runner=runner)
    second = _run(local, plan, runner=lambda *a, **kw: pytest.fail('replayed task'))
    assert len(calls) == 1 and first == second
    assert first['ok'] is False and first['task_root'] == str(calls[0])


def test_saved_inspection_rechecks_plan_claim_result_and_session(local):
    plan = _plan(local)

    def runner(instruction, base, *, task_root, client, source_artifact):
        (task_root / 'result.json').write_text(json.dumps({
            'ok': False, 'state': 'unsupported', 'kind': 'unsupported',
            'root': str(task_root), 'reason': 'no_matching_adapter'}), encoding='utf-8')

    saved = _run(local, plan, runner=runner)
    run = local[0].root / 'goals' / (plan['sha256'] + '.capability')
    assert router.inspect_saved(run, task_base=local[1],
                                computer_base=local[0].root.parent) == saved
    outcome = run / 'outcome.json'
    outcome.write_text(json.dumps({**saved, 'unchecked': True}), encoding='utf-8')
    with pytest.raises(ComputerError, match='capability_result_changed'):
        router.inspect_saved(run, task_base=local[1], computer_base=local[0].root.parent)


def test_saved_office_receipt_chain_is_read_without_execution(tmp_path):
    from agent_service.office_handoff import latest_generated_outputs

    base, task, _, _ = owned(tmp_path, sheet_spec())
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = router._save_goal(session, {
        'schema': 1, 'mode': 'capability', 'instruction': '复制已核验表格',
        'source_ref': None, 'task_base': str(base),
        'request_id': task.name.removeprefix('task-'), 'candidates': [],
    })
    run = session.root / 'goals' / (plan['sha256'] + '.capability')
    run.mkdir()
    write_new(run / 'claim.json', {'approval': plan['sha256'], 'task_root': str(task)})
    verified = router._inspect(task, base)
    assert verified['ok'] and len(verified['artifacts']) == 1
    saved = {**verified, 'run': str(run), 'approval': plan['sha256']}
    write_new(run / 'outcome.json', saved)

    assert router.inspect_saved(run, task_base=base, computer_base=session.root.parent) == saved
    assert latest_generated_outputs(base=session.root.parent, task_base=base) == verified['artifacts']


def test_crash_after_claim_is_unknown_and_never_replayed(local):
    plan = _plan(local)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('worker exited after dispatch')

    with pytest.raises(RuntimeError, match='worker exited'):
        _run(local, plan, runner=interrupted)
    outcome = _run(local, plan, runner=lambda *a, **kw: pytest.fail('replayed task'))
    assert calls == [1]
    assert outcome['ok'] is False and outcome['state'] == 'unknown'
    assert outcome['code'] == 'capability_previous_run_unknown'


def test_six_model_claims_share_real_session_quota_and_stop_prevents_call(local):
    session, _ = local
    calls = []

    class Client:
        def complete(self, system, user, **kwargs):
            calls.append((system, user))
            return {'answer': 'ok'}

    wrapped = router._SessionClient(Client(), session, lambda: False)
    for _ in range(6):
        assert wrapped.complete('system', 'context') == {'answer': 'ok'}
    with pytest.raises(ComputerError, match='session_model_call_limit'):
        wrapped.complete('system', 'context')
    assert len(calls) == 6
    assert len(list((session.root / 'model_calls').glob('*.json'))) == 12
    stopped = router._SessionClient(Client(), session, lambda: True)
    with pytest.raises(ComputerError, match='capability_cancelled'):
        stopped.complete('system', 'context')
    assert len(calls) == 6


def test_stop_before_runner_blocks_work_and_replay(local):
    plan = _plan(local)
    stopped = _run(local, plan, stop=lambda: True,
                   runner=lambda *a, **kw: pytest.fail('runner called after stop'))
    assert stopped['ok'] is False and stopped['state'] == 'cancelled'
    repeated = _run(local, plan, runner=lambda *a, **kw: pytest.fail('replayed stopped plan'))
    assert repeated == stopped and repeated['state'] == 'cancelled'


def test_forged_success_and_office_path_fail_real_history_inspection(local):
    plan = _plan(local)

    def forged(instruction, base, *, task_root, client, source_artifact):
        pretend = task_root / 'document.docx'
        pretend.write_bytes(b'not a verified Office result')
        (task_root / 'result.json').write_text(json.dumps({
            'ok': True, 'state': 'succeeded', 'kind': 'office',
            'root': str(task_root), 'workflow_root': str(task_root / 'workflow-fake'),
            'artifacts': [{'step': 's1', 'path': str(pretend), 'sha256': '0' * 64}]},
            ensure_ascii=False), encoding='utf-8')

    result = _run(local, plan, runner=forged)
    assert result['ok'] is False and result['state'] == 'needs_review'
    assert result['artifacts'] == [] and result['code'] != 'capability_verified'


@pytest.mark.parametrize('kind,state,expected', [('research','succeeded',True), ('notepad','prepared',False)])
def test_verified_research_is_complete_but_notepad_payload_is_not(local, monkeypatch, kind, state, expected):
    from agent_service import task_history
    session, base = local
    task = base / ('task-' + '9' * 32)
    task.mkdir(parents=True)
    (task / 'result.json').write_text(json.dumps({'ok': True, 'kind': kind, 'state': state}), encoding='utf-8')
    monkeypatch.setattr(task_history, 'inspect_task', lambda *_: {
        'state': state, 'kind': kind, 'artifacts': [{'path': str(task/'report.md'), 'sha256':'0'*64}]})
    assert router._inspect(task, base)['ok'] is expected


def test_capability_failure_identifies_layout_and_unknown_effects():
    message = router.describe_result({'ok':False,'code':'workbook_merged_unsupported'})
    assert '合并结构' in message and '不能完整读取' in message
    message = router.describe_result({'ok':False,'code':'capability_previous_run_unknown'})
    assert '没有完整结果回执' in message and '不能重新发出' in message
