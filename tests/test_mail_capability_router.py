"""Mail capability approval, provenance and replay contracts (offline)."""
import json
from pathlib import Path

import pytest

from agent_service import capability_router as router, mail_understanding as mail
from agent_service.computer import ComputerError, ComputerSession, digest, write_new
from test_mail_understanding import _mail_source, FakeClient as KeywordClient


class FakeClient(KeywordClient):
    def complete(self, system, user, **kwargs):
        return super().complete(system=system, user=user, **kwargs)


@pytest.fixture
def selected(tmp_path):
    _, original, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = router.prepare_mail(session, '提取这封邮件的待办并生成Word', source_ref=ref, task_base=base)
    return session, base, original, ref, plan


def run(selected, **kw):
    session, base, _, _, plan = selected
    return router.execute(session, plan, approval=plan['sha256'], task_base=base, **kw)


def test_preview_has_explicit_upload_scope_and_no_model_claim(selected):
    session, base, _, ref, plan = selected
    assert plan['schema'] == 4 and plan['source_ref'] == ref
    assert plan['capability'] == 'mail_understanding' and plan['candidates'] == []
    assert '批准后' in router.describe(plan) and '主题和正文' in router.describe(plan)
    assert len(list(base.glob('task-*'))) == 1
    assert not list((session.root / 'model_calls').iterdir())


def test_invalid_source_cannot_prepare(tmp_path):
    session = ComputerSession.create(tmp_path / 'sessions')
    with pytest.raises(ValueError):
        router.prepare_mail(session, '整理邮件', source_ref=None, task_base=tmp_path / 'tasks')


def test_changed_source_stops_before_claim_or_runner(selected):
    session, base, original, ref, plan = selected
    Path(ref['artifact']).write_bytes(b'changed')
    with pytest.raises(ValueError):
        run(selected, runner=lambda *a, **kw: pytest.fail('dispatched'), client=object())
    assert not (session.root / 'goals' / (plan['sha256'] + '.capability')).exists()


@pytest.mark.parametrize('change', [{'capability': 'arbitrary_code'}, {'candidates': ['extra']}, {'request_id': '../bad'}])
def test_rehashed_invalid_mail_plan_refused(selected, change):
    session, base, _, _, plan = selected
    changed = {**plan, **change}
    changed['sha256'] = digest({k: v for k, v in changed.items() if k != 'sha256'})
    write_new(session.root / 'goals' / (changed['sha256'] + '.json'), changed)
    with pytest.raises(ComputerError, match='capability_mail_plan_invalid'):
        router.execute(session, changed, approval=changed['sha256'], task_base=base, client=object())


def test_cancelled_before_start_reuses_result_without_task(selected):
    first = run(selected, stop=lambda: True, client=object())
    second = run(selected, client=object(), runner=lambda *a, **kw: pytest.fail('replayed'))
    assert first == second and first['before_start'] is True
    assert len(list(selected[1].glob('task-*'))) == 1


def test_actual_mail_dispatch_and_failure_replay_no_extra_calls(selected):
    client = FakeClient({'invalid': 'candidate'})
    first = run(selected, client=client)
    assert not first['ok'] and first['code'] == 'mail_analysis_schema_invalid'
    assert len(client.calls) == 1 and client.calls[0]['system'] == mail.SYSTEM
    second = run(selected, client=client, runner=lambda *a, **kw: pytest.fail('replayed'))
    assert first == second and len(client.calls) == 1
    session, base, _, _, _ = selected
    assert router.inspect_saved(first['run'], task_base=base, computer_base=session.root.parent) == first


def test_cancel_after_model_response_is_cancelled_not_provider_failure(selected):
    stopped = [False]
    client = FakeClient({'invalid': 'candidate'}, on_call=lambda *_: stopped.__setitem__(0, True))
    result = run(selected, client=client, stop=lambda: stopped[0])
    raw = json.loads((Path(result['task_root']) / 'result.json').read_text(encoding='utf-8'))
    assert raw['state'] == 'cancelled' and result['code'] == 'task_cancelled'
    # No Office manifest exists yet; history conservatively keeps needs_review.
    assert result['state'] == 'needs_review'
    assert len(client.calls) == 1 and result['artifacts'] == []


@pytest.mark.parametrize('field', ['instruction', 'source_ref'])
def test_saved_mail_binding_rejects_changed_request(selected, field):
    result = run(selected, client=FakeClient({'invalid': 'candidate'}))
    reqpath = Path(result['task_root']) / 'mail-analysis-request.json'
    req = json.loads(reqpath.read_text(encoding='utf-8'))
    req[field] = 'different' if field == 'instruction' else None
    reqpath.write_text(json.dumps(req), encoding='utf-8')
    session, base, _, _, _ = selected
    with pytest.raises(ComputerError, match='capability_mail_result_changed'):
        router.inspect_saved(result['run'], task_base=base, computer_base=session.root.parent)


def test_unknown_run_never_replayed(selected):
    session, base, _, _, plan = selected
    (session.root / 'goals' / (plan['sha256'] + '.capability')).mkdir()
    result = run(selected, client=object(), runner=lambda *a, **kw: pytest.fail('replayed'))
    assert result['state'] == 'unknown'
    assert len(list(base.glob('task-*'))) == 1


def test_completed_binding_requires_mail_semantic_proof(selected, tmp_path, monkeypatch):
    _, base, _, ref, plan = selected
    task = tmp_path / 'binding'
    task.mkdir()
    (task / 'request.txt').write_text(plan['instruction'], encoding='utf-8')
    with pytest.raises(ComputerError, match='capability_mail_result_changed'):
        router._bind_mail_result(plan, task, {'ok': True}, base)
    write_new(task / 'mail-analysis-claim.json', {'schema': 1})
    write_new(task / 'mail-analysis-request.json', {'instruction': plan['instruction'], 'source_ref': ref})
    write_new(task / 'mail-analysis-plan.json', {'steps': ['fixed steps']})
    calls = []
    monkeypatch.setattr(mail, 'verify_task', lambda *args: calls.append(args))
    router._bind_mail_result(plan, task, {'ok': True}, base)
    assert calls == [(task, base, ['fixed steps'])]


@pytest.mark.parametrize('code,hint', [('mail_analysis_review_rejected', '独立语义复核'),
                                     ('mail_analysis_unsupported', '多封联合、附件和发送'),
                                     ('task_cancelled', '已有成果和模型调用记录保留')])
def test_mail_failure_has_actionable_local_explanation(code, hint):
    text = router.describe_result({'ok':False,'code':code})
    assert hint in text and code in text


def test_mail_pause_option_recorded_and_forwarded(selected):
    result = run(selected, client=FakeClient({'invalid': 'candidate'}), pause_after_step=True)
    options = json.loads((Path(result['run']) / 'pause.json').read_text(encoding='utf-8'))
    assert options == {'schema':1,'pause_after_step':True}
    assert result['code'] == 'mail_analysis_schema_invalid'
    again = run(selected, client=object(), pause_after_step=False)
    assert result == again


def test_other_capability_cannot_silently_ignore_pause(selected):
    session, base, _, ref, _ = selected
    plan = router.prepare(session, '整理资料', source_ref=ref, task_base=base)
    with pytest.raises(ComputerError, match='capability_pause_unsupported'):
        router.execute(session, plan, approval=plan['sha256'], task_base=base, pause_after_step=True, client=object())
    assert not (session.root / 'goals' / (plan['sha256']+'.capability')).exists()
