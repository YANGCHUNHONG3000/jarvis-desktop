"""Lossless independent-review input encoding; synthetic source, fake HTTP only."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import mail_understanding as mail
from agent_service.model import ModelError
from test_budgeted_model import FakeSession, completion, local_client, model_metadata
from test_mail_understanding import _context, _mail_source, _ready


def _restore(payload):
    steps = deepcopy(payload['steps'])
    if payload.get('plan_encoding') == 'shared_tables_v1':
        for step in steps:
            if 'table_ref' not in step['data']:
                continue
            table = deepcopy(payload['tables'][step['data'].pop('table_ref')])
            if step['action'] == 'spreadsheet':
                step['data'].update(table)
            else:
                step['data']['table'] = table
    return steps


def _long_case():
    body = ('x' * 77 + '\n') * 183 + 'x' * 151
    data = _context(body)
    raw = _ready(body=body, schema=4)
    raw['summary'] = [
        {'text': '合成摘要' + str(i), 'evidence': [{'line': f'B{i}', 'quote': 'x' * 77}]}
        for i in (1, 2)]
    raw['items'] = [
        {'kind': '要点', 'task': f'合成项目{i:02}：' + 'x' * 250,
         'owner': None, 'deadline': None,
         'evidence': [{'line': f'B{j}', 'quote': 'x' * 77} for j in (i, i + 1, i + 2)]}
        for i in range(1, 22)]
    value = mail.validate(raw, data, schema=4)
    return body, data, raw, value, mail.compile_steps(value)


def test_identical_tables_roundtrip_all_metadata_and_inputs_unchanged():
    _, data, _, value, steps = _long_case()
    steps[0]['data']['metadata'] = {'nested': ['sheet metadata']}
    steps[1]['data']['metadata'] = {'nested': ['document metadata']}
    steps[0]['extra_step_metadata'] = {'preserve': True}
    before = deepcopy((data, value, steps))
    payload = mail._review_payload('完整指令', data, value, steps)
    assert payload['plan_encoding'] == 'shared_tables_v1'
    assert list(payload['tables']) == ['t1']
    assert all(step['data']['table_ref'] == 't1' for step in payload['steps'])
    assert 'headers' not in payload['steps'][0]['data']
    assert 'rows' not in payload['steps'][0]['data']
    assert 'table' not in payload['steps'][1]['data']
    assert _restore(payload) == steps
    assert payload['candidate'] == value
    assert {key: payload[key] for key in data} == data
    assert (data, value, steps) == before
    payload['tables']['t1']['rows'][1][1] = 'mutation'
    assert (data, value, steps) == before


@pytest.mark.parametrize('formats', [['document'], ['spreadsheet'], []])
def test_single_office_and_reply_only_keep_original_payload(formats):
    raw = _ready(schema=4, formats=formats or ['document'])
    if not formats:
        raw.update(formats=[], title='', summary=[], items=[],
                   reply={'subject': '答复', 'body': '请核实。', 'to': [], 'attachments': []})
    data = _context()
    value = mail.validate(raw, data, schema=4)
    steps = mail.compile_steps(value)
    payload = mail._review_payload('指令', data, value, steps)
    assert payload == {'instruction': '指令', **data, 'candidate': value, 'steps': steps}


@pytest.mark.parametrize('field', ['headers', 'rows'])
def test_equal_size_different_table_values_are_not_shared(field):
    raw = _ready(schema=4)
    data = _context()
    value = mail.validate(raw, data, schema=4)
    steps = mail.compile_steps(value)
    steps[1]['data']['table'] = deepcopy(steps[1]['data']['table'])
    table = steps[1]['data']['table']
    if field == 'headers':
        table['headers'][0] = '区别'
    else:
        table['rows'][0][0] = '区别'
    before = deepcopy(steps)
    payload = mail._review_payload('指令', data, value, steps)
    assert 'plan_encoding' not in payload and payload['steps'] == before
    assert steps == before


def test_additional_table_metadata_is_never_dropped():
    _, data, _, value, steps = _long_case()
    steps[1]['data']['table'] = {**steps[1]['data']['table'], 'metadata': 'preserved'}
    payload = mail._review_payload('指令', data, value, steps)
    assert 'plan_encoding' not in payload
    assert payload['steps'] == steps


def test_shared_encoding_only_used_when_serialized_input_shrinks():
    steps = [{'id': 's1', 'action': 'spreadsheet', 'data': {'headers': [], 'rows': []}},
             {'id': 's2', 'action': 'document', 'data': {'table': {'headers': [], 'rows': []}}}]
    payload = mail._review_payload('指令', {}, {}, steps)
    assert payload == {'instruction': '指令', 'candidate': {}, 'steps': steps}


def test_synthetic_184_lines_21_items_fit_without_losing_any_input():
    body, data, _, value, steps = _long_case()
    original = {'instruction': '完整整理', **data, 'candidate': value, 'steps': steps}
    payload = mail._review_payload('完整整理', data, value, steps)
    assert len(body) == 14425 and len(data['body_lines']) == 184
    assert len(value['items']) == 21 and len(value['summary']) == 2
    sizes = (len(json.dumps(original, ensure_ascii=False)), len(json.dumps(payload, ensure_ascii=False)))
    assert sizes[0] > 48000 > sizes[1], sizes
    assert payload['candidate'] == value and payload['body_lines'] == data['body_lines']
    assert _restore(payload) == steps


@pytest.mark.parametrize('approved', [False, True])
def test_real_run_task_reviews_once_and_dispatches_only_after_approval(
        tmp_path, monkeypatch, local_client, approved):
    body, data, raw, value, steps = _long_case()
    _, _, base, _, ref = _mail_source(tmp_path, body)
    make, _, _ = local_client
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192))
    answers = [raw, {'approved': approved, 'reason': 'offline semantic decision'}]
    session.on_post = lambda: setattr(session, 'result', completion(
        content=json.dumps(answers[len(session.posts) - 1], ensure_ascii=False)))
    dispatched = []

    def office(executable, workflow_root, **kwargs):
        assert len(session.posts) == 2 and approved
        assert executable == steps  # Full executable plan, never table references.
        dispatched.append(deepcopy(executable))
        return {'ok': True, 'state': 'succeeded', 'completed': ['s1', 's2'], 'receipts': {}}

    monkeypatch.setattr(mail.workflow, 'run_office_workflow', office)
    client = make(session)
    result = mail.run_task('完整整理', base, source_artifact=ref, client=client)
    assert client.calls == len(session.posts) == 2
    requests = [json.loads(options['data']) for _, options in session.posts]
    assert [request['max_tokens'] for request in requests] == [8192, 600]
    review = json.loads(requests[1]['messages'][1]['content'])
    assert len(requests[1]['messages'][1]['content']) < 48000
    assert review['plan_encoding'] == 'shared_tables_v1' and _restore(review) == steps
    assert review['candidate'] == value and review['body_lines'] == data['body_lines']
    assert 'COMPLETE shared' in requests[1]['messages'][0]['content']
    plan = json.loads((Path(result['root']) / 'mail-analysis-plan.json').read_text(encoding='utf-8'))
    assert plan['steps'] == steps
    assert result['ok'] is approved and len(dispatched) == int(approved)
    if not approved:
        assert result['error'] == 'mail_analysis_review_rejected'


def test_shared_client_still_rejects_above_48000_before_any_http(local_client):
    make, ledger, _ = local_client
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192))
    with pytest.raises(ModelError, match='model_input_invalid'):
        make(session).complete(mail.REVIEW, 'x' * 48001, max_tokens=600)
    assert session.gets == session.posts == []
    assert ledger.snapshot()['held_micro'] == ledger.snapshot()['charged_micro'] == 0
