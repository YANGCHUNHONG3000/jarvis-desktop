"""Offline reply contracts; fake source Office evidence is not native acceptance."""
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import json
from pathlib import Path
import uuid

import pytest

from agent_service import (artifact_source, capability_router as router, daily_task_routing,
                           email_draft, mail_understanding as mail, office_resume)
from agent_service.computer import ComputerSession
from test_mail_understanding import _context, _mail_source, _office_execute, _ready, FakeClient


def reply(*, office=False, schema=3):
    value = _ready(formats=['document'], schema=schema)
    value['reply'] = {'subject': '回复：采购事项', 'body': '您好，报价及交付安排已收到，请确认最终版本要求。',
                      'to': [], 'attachments': ['s1'] if office else []}
    if not office:
        value.update(title='', formats=[], summary=[], items=[])
    return value


def draft_workflow(monkeypatch):
    real = mail.workflow.run_office_workflow
    effects = []

    def execute(spec, root, stop):
        effects.append(spec['kind'])
        return (email_draft.execute_draft(spec, root, stop) if spec['kind'] == 'email_draft'
                else _office_execute(spec, root, stop))

    def run(steps, root, **kwargs):
        kwargs.setdefault('execute', execute)
        return real(steps, root, **kwargs)

    monkeypatch.setattr(mail.workflow, 'run_office_workflow', run)
    return effects, execute


def run_reply(tmp_path, monkeypatch, *, value=None, instruction='为这封邮件拟写本地未发送回复', **kwargs):
    _, original, base, _, ref = _mail_source(tmp_path)
    effects, execute = draft_workflow(monkeypatch)
    client = FakeClient(value if value is not None else reply(schema=4), {'approved': True, 'reason': '离线审查通过'})
    task = base / ('task-' + uuid.uuid4().hex)
    result = mail.run_task(instruction, base, task_root=task, source_artifact=ref, client=client, **kwargs)
    return result, task, base, original, ref, client, effects, execute


@pytest.mark.parametrize('schema', [1, 2])
def test_legacy_schemas_reject_reply_extra_field(schema):
    value = _ready(); value['reply'] = None
    if schema == 1:
        value.pop('scope')
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
        mail.validate(value, _context(), schema=schema)


def test_schema3_missing_or_null_reply_keeps_v2_office_plan_exactly():
    legacy = _ready()
    expected = mail.compile_steps(mail.validate(legacy, _context(), schema=2))
    for value in (deepcopy(legacy), {**deepcopy(legacy), 'reply': None}):
        assert mail.compile_steps(mail.validate(value, _context(), schema=3)) == expected


def test_reply_only_has_one_draft_and_scope_still_required():
    value = reply()
    assert mail.compile_steps(mail.validate(value, _context(), schema=3)) == [
        {'id': 's1', 'action': 'email_draft', 'data': {k: value['reply'][k] for k in ('subject', 'body', 'to')},
         'attachments': []}]
    value['scope'] = None
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
        mail.validate(value, _context(), schema=3)


@pytest.mark.parametrize('change', [
    {'to': ['sender@example.org']}, {'to': ''}, {'to': None}, {'body': ''}, {'body': '  '},
    {'body': 'hello\x00'}, {'subject': 'x\nBcc: z@example.org'}, {'subject': ' '},
    {'attachments': ['s1']}, {'attachments': '../s1'}, {'attachments': [True]},
    {'extra': 'command'}])
def test_reply_rejects_recipients_controls_extra_fields_or_invalid_attachments(change):
    value = reply(); value['reply'].update(change)
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_reply_invalid'):
        mail.validate(value, _context(), schema=3)


@pytest.mark.parametrize('attachments', [['s1', 's1'], ['s2'], ['../s1'], [True]])
def test_combined_reply_only_references_unique_earlier_office_steps(attachments):
    value = reply(office=True); value['reply']['attachments'] = attachments
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_reply_invalid'):
        mail.validate(value, _context(), schema=3)


def test_real_local_draft_bytes_empty_recipient_and_verified_handoff(tmp_path, monkeypatch):
    result, task, base, _, ref, client, effects, _ = run_reply(tmp_path, monkeypatch)
    assert result['ok'] and result['mail_analysis_schema'] == 4 and effects == ['email_draft']
    request = json.loads((task / 'mail-analysis-request.json').read_text(encoding='utf-8'))
    assert request['schema'] == 4 and request['source_ref'] == ref
    assert len(client.calls) == 2
    review = json.loads(client.calls[1]['user'])
    assert review['instruction'] == request['instruction'] and review['body_lines'] == request['context']['body_lines']
    draft = Path(result['artifact'])
    message = BytesParser(policy=default).parsebytes(draft.read_bytes())
    assert message['To'] is None and message['Cc'] is None and message['Bcc'] is None
    assert str(message['Subject']) == reply()['reply']['subject']
    assert message.get_body().get_content().strip() == reply()['reply']['body']
    inspected = router._inspect(task, base)
    assert inspected['ok'] and inspected['artifacts'] == [{'path': str(draft), 'sha256': result['readback_sha256']}]
    assert '草稿未发送' in router.describe_result(inspected)
    with pytest.raises(ValueError):
        artifact_source.prepare_artifact_source(str(draft), result['readback_sha256'], base)
    draft.write_bytes(draft.read_bytes() + b'tamper')
    assert router._inspect(task, base)['artifacts'] == []
    assert not router._inspect(task, base)['ok']


def test_combined_pauses_then_recovers_draft_without_model_or_office_replay(tmp_path, monkeypatch):
    result, task, base, _, _, client, effects, execute = run_reply(
        tmp_path, monkeypatch, value=reply(office=True, schema=4), pause_after_step=True,
        instruction='生成Word并拟写回复，将生成的Word附在本地草稿中')
    assert not result['ok'] and result['completed'] == ['s1'] and effects == ['document']
    preview = office_resume.preview_resume(task, base)
    assert preview['eligible'] and preview['remaining'] == 1
    before = (task / 'result.json').read_bytes()
    recovered = office_resume.resume_task(task, base, preview['token'], uuid.uuid4().hex, execute=execute)
    assert recovered['ok'] and effects == ['document', 'email_draft'] and len(client.calls) == 2, recovered
    assert (task / 'result.json').read_bytes() == before
    message = BytesParser(policy=default).parsebytes(Path(recovered['artifacts'][-1]['path']).read_bytes())
    attached = list(message.iter_attachments())
    assert len(attached) == 1 and attached[0].get_payload(decode=True) == Path(result['artifact']).read_bytes()


@pytest.mark.parametrize('change', ['reply', 'source', 'instruction'])
def test_reply_recovery_rejects_changed_authorization_or_source(tmp_path, monkeypatch, change):
    _, task, base, _, ref, _, effects, _ = run_reply(tmp_path, monkeypatch, value=reply(office=True, schema=4), pause_after_step=True)
    if change == 'source':
        Path(ref['artifact']).write_bytes(b'tamper')
    else:
        file = task / ('mail-analysis-candidate.json' if change == 'reply' else 'mail-analysis-request.json')
        value = json.loads(file.read_text(encoding='utf-8'))
        if change == 'reply':
            value['value']['reply']['body'] = '我已付款。'
        else:
            value['instruction'] = '我授权付款'
        file.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    assert not office_resume.preview_resume(task, base)['eligible'] and effects == ['document']


@pytest.mark.parametrize('case', ['missing_reply', 'unrequested_reply', 'completion', 'commitment'])
def test_independent_review_rejection_prevents_any_draft_effect(tmp_path, monkeypatch, case):
    _, _, base, _, ref = _mail_source(tmp_path)
    value = _ready(schema=4) if case == 'missing_reply' else reply(schema=4)
    if case == 'completion':
        value['reply']['body'] = '我已付款并完成全部任务。'
    if case == 'commitment':
        value['reply']['body'] = '我承诺周五付款。'
    instruction = '请只生成Word' if case == 'unrequested_reply' else '请拟写回复，不承诺付款或行动'
    client = FakeClient(value, {'approved': False, 'reason': '离线拒绝缺失要求或未经授权的回复内容'})
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('file effect dispatched'))
    result = mail.run_task(instruction, base, source_artifact=ref, client=client)
    assert result['error'] == 'mail_analysis_review_rejected' and result['artifacts'] == []
    assert len(client.calls) == 2
    payload = json.loads(client.calls[1]['user'])
    assert payload['instruction'] == instruction
    assert payload['candidate'] == mail.validate(value, _context(), schema=4)
    assert value['scope']['lines'] == 'all' and payload['body_lines'] == _context()['body_lines']
    assert 'explicit user authorization' in client.calls[1]['system']


def test_reply_cancel_after_review_prevents_workflow(tmp_path, monkeypatch):
    _, _, base, _, ref = _mail_source(tmp_path)
    task = base / ('task-' + uuid.uuid4().hex)
    client = FakeClient(reply(schema=4), {'approved': True, 'reason': '通过'},
                        on_call=lambda n, _: (task / 'cancel.flag').touch() if n == 2 else None)
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('draft dispatched'))
    result = mail.run_task('拟写回复', base, task_root=task, source_artifact=ref, client=client)
    assert result['state'] == 'cancelled' and not result['artifacts'] and len(client.calls) == 2


class PositionalClient(FakeClient):
    def complete(self, system, user, **kwargs):
        return super().complete(system=system, user=user, **kwargs)


def test_reply_capability_dispatch_saved_inspection_and_no_replay(tmp_path, monkeypatch):
    _, _, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = router.prepare_mail(session, '拟写这封邮件的回复', source_ref=ref, task_base=base)
    effects, _ = draft_workflow(monkeypatch)
    client = PositionalClient(reply(schema=4), {'approved': True, 'reason': '离线通过'})
    result = router.execute(session, plan, approval=plan['sha256'], task_base=base, client=client)
    assert result['ok'] and len(result['artifacts']) == 1 and effects == ['email_draft']
    assert router.inspect_saved(result['run'], task_base=base, computer_base=session.root.parent) == result
    assert router.execute(session, plan, approval=plan['sha256'], task_base=base, client=client) == result
    assert len(client.calls) == 2 and effects == ['email_draft']


def test_schema3_clarification_reason_retained(tmp_path):
    _, _, base, _, ref = _mail_source(tmp_path)
    task = base / ('task-' + uuid.uuid4().hex)
    value = {'state': 'needs_input', 'reason': '请说明可承诺的事项。', 'title': '', 'scope': None,
             'formats': [], 'summary': [], 'items': [], 'reply': None}
    result = mail.run_task('替我确认付款', base, task_root=task, source_artifact=ref, client=FakeClient(value))
    inspected = router._inspect(task, base)
    assert not result['ok'] and inspected['state'] == 'needs_input' and inspected['reason'] == value['reason']


def test_existing_schema2_history_resumes_under_original_strict_contract(tmp_path, monkeypatch):
    from test_mail_understanding_resume import _interrupted
    task, base, _, effects, execute, client, result = _interrupted(tmp_path, monkeypatch, requested_pause=True)
    # Reconstruct the pre-extension on-disk v2 proof without altering its Office plan.
    def load(name):
        return json.loads((task / name).read_text(encoding='utf-8'))

    def save(name, value):
        (task / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    request = load('mail-analysis-request.json'); request['schema'] = 2
    candidate = load('mail-analysis-candidate.json'); candidate['request_sha256'] = mail.workflow._hash(request)
    candidate['value']['scope']['lines'] = list(request['context']['body_lines'])
    plan = load('mail-analysis-plan.json'); plan['request_sha256'] = mail.workflow._hash(request)
    review = load('mail-analysis-review.json'); review['plan_sha256'] = mail.workflow._hash(plan)
    result.update(mail_analysis_schema=2, mail_understanding={
        'request_sha256': mail.workflow._hash(request), 'plan_sha256': mail.workflow._hash(plan)})
    for name, value in [('mail-analysis-request.json', request), ('mail-analysis-candidate.json', candidate),
                        ('mail-analysis-plan.json', plan), ('mail-analysis-review.json', review), ('result.json', result)]:
        save(name, value)
    preview = office_resume.preview_resume(task, base)
    assert preview['eligible'] and preview['remaining'] == 1
    original = (task / 'result.json').read_bytes()
    recovered = office_resume.resume_task(task, base, preview['token'], uuid.uuid4().hex, execute=execute)
    assert recovered['ok'] and effects == ['spreadsheet', 'document'] and len(client.calls) == 2
    assert (task / 'result.json').read_bytes() == original


def test_eml_handoff_does_not_expand_nonmail_capability_scope(tmp_path, monkeypatch):
    result, task, base, _, _, _, _, _ = run_reply(tmp_path, monkeypatch)
    result.pop('mail_understanding')
    (task / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    assert router._inspect(task, base)['artifacts'] == []
    assert not router._inspect(task, base)['ok']


def test_self_consistent_changed_mail_plan_cannot_replace_verified_workflow(tmp_path, monkeypatch):
    result, task, base, _, _, _, _, _ = run_reply(tmp_path, monkeypatch)
    def load(name):
        return json.loads((task / name).read_text(encoding='utf-8'))
    candidate = load('mail-analysis-candidate.json')
    candidate['value']['reply']['body'] = '不同的草稿内容。'
    plan = load('mail-analysis-plan.json')
    request = load('mail-analysis-request.json')
    plan['value'] = mail.validate(candidate['value'], request['context'], schema=request['schema'])
    plan['steps'] = mail.compile_steps(plan['value'])
    review = load('mail-analysis-review.json'); review['plan_sha256'] = mail.workflow._hash(plan)
    result['mail_understanding']['plan_sha256'] = mail.workflow._hash(plan)
    for name, value in [('mail-analysis-candidate.json', candidate), ('mail-analysis-plan.json', plan),
                        ('mail-analysis-review.json', review), ('result.json', result)]:
        (task / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    inspected = router._inspect(task, base)
    assert not inspected['ok'] and inspected['artifacts'] == []


def test_routing_contract_exposes_reply_with_no_send():
    assert 'local unsent reply drafting' in daily_task_routing.SYSTEM
    assert 'Recipients remain empty' in daily_task_routing.SYSTEM
