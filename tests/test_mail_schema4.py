"""Versioned full-mail coverage: raw model candidates remain immutable (offline)."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import mail_understanding as mail, mail_followup
from test_mail_understanding import BODY, _context, _mail_source, _ready, FakeClient


def ready4(**kwargs):
    value = _ready(**kwargs)
    value['scope']['lines'] = 'all'
    return value


def test_full_all_expands_every_original_line_including_blanks_without_mutation():
    body = '\n'.join([BODY] + ['' if i % 2 else f'原始行{i}' for i in range(182)] + [''])
    data = _context(body)
    raw = ready4(body=body)
    original_data, original_raw = deepcopy(data), deepcopy(raw)
    normalized = mail.validate(raw, data, schema=4)
    assert len(normalized['scope']['lines']) == 184
    assert normalized['scope']['lines'] == list(data['body_lines'])
    assert normalized['scope']['lines'][-1] == 'B184'
    assert raw == original_raw and data == original_data
    normalized['summary'][0]['evidence'][0]['quote'] = 'changed'
    normalized['scope']['lines'].pop()
    assert raw == original_raw and data == original_data


@pytest.mark.parametrize('lines', [['B1'], [], None, True, 'ALL', ' all', 'all ', {'all': True}])
def test_schema4_full_requires_exact_all_string_even_for_complete_array(lines):
    value = ready4()
    value['scope']['lines'] = lines
    before = deepcopy(value)
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
        mail.validate(value, _context(), schema=4)
    assert value == before


@pytest.mark.parametrize('lines', ['all', [], ['B1', 'B1'], ['B2', 'B1'], ['B9'], [True]])
def test_schema4_subset_remains_explicit_ordered_unique_known_array(lines):
    value = ready4(body=BODY + '\n其他行')
    value['scope'].update(kind='subset', lines=lines)
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
        mail.validate(value, _context(BODY + '\n其他行'), schema=4)


def test_schema4_subset_keeps_selection_and_does_not_authorize_outside_quotes():
    body = BODY + '\n其他行'
    value = ready4(body=body)
    value['scope'].update(kind='subset', lines=['B1'])
    assert mail.validate(value, _context(body), schema=4) == value
    value['summary'][0]['evidence'].append({'line': 'B2', 'quote': '其他行'})
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, _context(body), schema=4)


@pytest.mark.parametrize('count', [4, 5])
def test_schema4_full_all_does_not_expand_evidence_allowance(count):
    value = ready4()
    value['summary'][0]['evidence'] *= count
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, _context(), schema=4)


@pytest.mark.parametrize('count', [300, 301])
def test_schema4_all_keeps_existing_body_line_limit(count):
    body = '\n'.join([BODY] + [''] * (count - 1))
    value, data = ready4(body=body), _context(body)
    if count == 300:
        assert mail.validate(value, data, schema=4)['scope']['lines'] == list(data['body_lines'])
    else:
        with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
            mail.validate(value, data, schema=4)
    assert value['scope']['lines'] == 'all'


@pytest.mark.parametrize('schema', [2, 3])
def test_legacy_full_array_is_unchanged_and_all_string_is_not_reinterpreted(schema):
    value = _ready()
    assert mail.validate(value, _context(), schema=schema) == value
    value['scope']['lines'] = 'all'
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
        mail.validate(value, _context(), schema=schema)


def _run(tmp_path, monkeypatch, *, approved=True):
    _, _, base, _, ref = _mail_source(tmp_path, BODY + '\n\n补充原文\n')
    raw = ready4(body=BODY + '\n\n补充原文\n')
    client = FakeClient(raw, {'approved': approved, 'reason': '离线独立审查'})
    dispatched = []
    def execute(steps, _root, **_kwargs):
        dispatched.append(deepcopy(steps))
        return {'ok': True, 'state': 'succeeded', 'completed': [], 'receipts': {}}
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', execute)
    result = mail.run_task('整理整封邮件为Word和Excel', base, source_artifact=ref, client=client)
    return Path(result['root']), base, result, raw, client, dispatched


def _read(task, name):
    return json.loads((task / name).read_text(encoding='utf-8'))


def test_new_run_saves_raw_all_but_reviews_and_reverifies_normalized_plan(tmp_path, monkeypatch):
    task, base, result, raw, client, dispatched = _run(tmp_path, monkeypatch)
    request = _read(task, 'mail-analysis-request.json')
    candidate = _read(task, 'mail-analysis-candidate.json')
    plan = _read(task, 'mail-analysis-plan.json')
    assert result['ok'] and result['mail_analysis_schema'] == request['schema'] == 4
    assert candidate['value'] == raw and raw['scope']['lines'] == 'all'
    assert plan['value']['scope']['lines'] == ['B1', 'B2', 'B3', 'B4']
    assert candidate['request_sha256'] == plan['request_sha256'] == mail.workflow._hash(request)
    review_payload = json.loads(client.calls[1]['user'])
    assert review_payload['candidate'] == plan['value']
    assert review_payload['body_lines'] == request['context']['body_lines']
    assert dispatched == [plan['steps']]
    assert mail.verify_task(task, base, plan['steps'])
    assert 'lines:"all" exactly' in client.calls[0]['system']


@pytest.mark.parametrize('changed', ['candidate', 'plan', 'context', 'result_schema'])
def test_schema4_reverification_rejects_missing_line_or_changed_bound_context(tmp_path, monkeypatch, changed):
    task, base, _result, _raw, _client, _dispatched = _run(tmp_path, monkeypatch)
    plan = _read(task, 'mail-analysis-plan.json')
    names = {'candidate': 'mail-analysis-candidate.json', 'plan': 'mail-analysis-plan.json',
             'context': 'mail-analysis-request.json', 'result_schema': 'result.json'}
    path = task / names[changed]
    saved = json.loads(path.read_text(encoding='utf-8'))
    if changed == 'candidate':
        saved['value']['scope']['lines'] = ['B1', 'B3']
    elif changed == 'plan':
        saved['value']['scope']['lines'].remove('B2')
    elif changed == 'context':
        saved['context']['body_lines'].pop('B2')
    else:
        saved['mail_analysis_schema'] = 3
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(mail.MailUnderstandingError):
        mail.verify_task(task, base, plan['steps'])


def test_schema4_rejected_normalized_plan_can_prepare_followup_without_effects(tmp_path, monkeypatch):
    task, base, result, _raw, client, dispatched = _run(tmp_path, monkeypatch, approved=False)
    assert result['error'] == 'mail_analysis_review_rejected' and dispatched == []
    before = {p: p.read_bytes() for p in task.rglob('*') if p.is_file()}
    preview = mail_followup.inspect_followup(task, base)
    assert preview['eligible'] and len(client.calls) == 2
    prepared = mail_followup.prepare_followup(task, base, preview['token'])
    assert prepared['instruction'] == '整理整封邮件为Word和Excel'
    assert before == {p: p.read_bytes() for p in task.rglob('*') if p.is_file()}


def test_schema4_rejected_plan_cannot_be_resealed_to_different_steps(tmp_path, monkeypatch):
    task, base, _result, _raw, _client, _dispatched = _run(tmp_path, monkeypatch, approved=False)
    plan = _read(task, 'mail-analysis-plan.json')
    plan['steps'][0]['data']['title'] = '未经批准的新标题'
    review = _read(task, 'mail-analysis-review.json')
    review['plan_sha256'] = mail.workflow._hash(plan)
    for name, value in [('mail-analysis-plan.json', plan), ('mail-analysis-review.json', review)]:
        (task / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    assert mail_followup.inspect_followup(task, base)['eligible'] is False


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_legacy_request_candidate_plan_and_review_still_verify_with_original_schema(tmp_path, monkeypatch, schema):
    task, base, result, _raw, _client, _dispatched = _run(tmp_path, monkeypatch)
    request = _read(task, 'mail-analysis-request.json')
    request['schema'] = schema
    candidate = _read(task, 'mail-analysis-candidate.json')
    candidate['request_sha256'] = mail.workflow._hash(request)
    candidate['value']['scope']['lines'] = list(request['context']['body_lines'])
    if schema == 1:
        candidate['value'].pop('scope')
    value = mail.validate(candidate['value'], request['context'], schema=schema)
    steps = mail.compile_steps(value)
    plan = {'request_sha256': mail.workflow._hash(request), 'value': value, 'steps': steps}
    review = _read(task, 'mail-analysis-review.json')
    review['plan_sha256'] = mail.workflow._hash(plan)
    result.update(mail_analysis_schema=schema, mail_understanding={
        'request_sha256': mail.workflow._hash(request), 'plan_sha256': mail.workflow._hash(plan)})
    for name, record in [('mail-analysis-request.json', request),
                         ('mail-analysis-candidate.json', candidate), ('mail-analysis-plan.json', plan),
                         ('mail-analysis-review.json', review), ('result.json', result)]:
        (task / name).write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    assert mail.verify_task(task, base, steps)
