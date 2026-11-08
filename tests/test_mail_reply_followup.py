"""Schema3 no-effect clarification/rejection preparation and legacy boundary."""
import json
from pathlib import Path

import pytest

from agent_service import mail_followup as followup, mail_understanding as mail
from test_mail_understanding import _mail_source, FakeClient
from test_mail_reply_draft import reply


def load(task, name):
    return json.loads((task / name).read_text(encoding='utf-8'))


def save(task, name, value):
    (task / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def stopped(tmp_path, rejected=False, *, schema=3):
    _, _, base, _, ref = _mail_source(tmp_path)
    value = reply(schema=4) if rejected else {'state': 'needs_input', 'reason': '请明确可承诺的行动。',
        'scope': None, 'title': '', 'formats': [], 'summary': [], 'items': [], 'reply': None}
    client = FakeClient(value, {'approved': False, 'reason': '未授权付款，不能承诺。'}) if rejected else FakeClient(value)
    result = mail.run_task('根据所选邮件拟写回复', base, source_artifact=ref, client=client)
    task = Path(result['root'])
    if schema == 3:
        # Retain actual schema3 on-disk coverage after new executions switch to v4.
        request = load(task, 'mail-analysis-request.json'); request['schema'] = 3
        candidate = load(task, 'mail-analysis-candidate.json')
        candidate['request_sha256'] = mail.workflow._hash(request)
        if rejected:
            candidate['value']['scope']['lines'] = list(request['context']['body_lines'])
            plan = load(task, 'mail-analysis-plan.json')
            plan['request_sha256'] = mail.workflow._hash(request)
            review = load(task, 'mail-analysis-review.json')
            review['plan_sha256'] = mail.workflow._hash(plan)
            save(task, 'mail-analysis-plan.json', plan)
            save(task, 'mail-analysis-review.json', review)
        result['mail_analysis_schema'] = 3
        for name, record in [('mail-analysis-request.json', request),
                             ('mail-analysis-candidate.json', candidate), ('result.json', result)]:
            save(task, name, record)
    return task, base, ref, client


@pytest.mark.parametrize('rejected', [False, True])
@pytest.mark.parametrize('schema', [3, 4])
def test_schema3_stopped_reply_prepares_exact_instruction_source_without_effect(tmp_path, monkeypatch, rejected, schema):
    task, base, ref, client = stopped(tmp_path, rejected, schema=schema)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}
    monkeypatch.setattr(mail, 'run_task', lambda *a, **kw: pytest.fail('new execution'))
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('file effect'))
    preview = followup.inspect_followup(task, base)
    assert preview['eligible']
    result = followup.prepare_followup(task, base, preview['token'])
    assert result['source_ref'] == ref and result['instruction'] == '根据所选邮件拟写回复'
    assert result['code'] == ('mail_followup_review_rejected' if rejected else 'mail_followup_needs_input')
    assert len(client.calls) == (2 if rejected else 1)
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}


@pytest.mark.parametrize('result_schema,request_schema', [(1, 1), (5, 5), (True, True), (3, 2), (2, 3)])
def test_unknown_legacy1_bool_or_mismatched_schema_never_prepares(tmp_path, result_schema, request_schema):
    task, base, _, _ = stopped(tmp_path)
    result = load(task, 'result.json'); result['mail_analysis_schema'] = result_schema
    request = load(task, 'mail-analysis-request.json'); request['schema'] = request_schema
    candidate = load(task, 'mail-analysis-candidate.json'); candidate['request_sha256'] = mail.workflow._hash(request)
    for name, value in [('result.json', result), ('mail-analysis-request.json', request), ('mail-analysis-candidate.json', candidate)]:
        save(task, name, value)
    assert not followup.inspect_followup(task, base)['eligible']


def test_schema2_needs_input_retains_old_strict_contract(tmp_path):
    task, base, _, _ = stopped(tmp_path)
    result = load(task, 'result.json'); result['mail_analysis_schema'] = 2
    request = load(task, 'mail-analysis-request.json'); request['schema'] = 2
    candidate = load(task, 'mail-analysis-candidate.json')
    candidate['value'].pop('reply'); candidate['request_sha256'] = mail.workflow._hash(request)
    for name, value in [('result.json', result), ('mail-analysis-request.json', request), ('mail-analysis-candidate.json', candidate)]:
        save(task, name, value)
    assert followup.inspect_followup(task, base)['eligible']
    candidate['value']['reply'] = None
    save(task, 'mail-analysis-candidate.json', candidate)
    assert not followup.inspect_followup(task, base)['eligible']


def test_schema3_rejected_reply_plan_must_equal_compiled_candidate_even_if_rehashed(tmp_path):
    task, base, _, _ = stopped(tmp_path, True)
    plan = load(task, 'mail-analysis-plan.json')
    plan['steps'][0]['data']['body'] = '我已付款。'
    review = load(task, 'mail-analysis-review.json'); review['plan_sha256'] = mail.workflow._hash(plan)
    save(task, 'mail-analysis-plan.json', plan); save(task, 'mail-analysis-review.json', review)
    assert not followup.inspect_followup(task, base)['eligible']


def test_schema3_inventory_still_refuses_any_workflow_effect(tmp_path):
    task, base, _, _ = stopped(tmp_path, True)
    (task / 'workflow-unknown').mkdir()
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_effects_unknown'
