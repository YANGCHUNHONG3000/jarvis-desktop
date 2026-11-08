"""Offline output sizing through the real mail and shared budget request paths."""

import json
from pathlib import Path

import pytest

from agent_service import mail_understanding as mail, model
from agent_service.model import ModelError
from test_budgeted_model import (FakeSession, completion, local_client, model_metadata,
                                 posted_reserve)
from test_mail_understanding import _mail_source, _ready, _snapshot


def _body(length):
    if length == 14425:
        return ('x' * 77 + '\n') * 183 + 'x' * 151  # 184 synthetic lines.
    full, tail = divmod(length, 100)
    return ('x' * 99 + '\n') * full + 'x' * tail


@pytest.mark.parametrize('length,tokens', [
    (1, 2048), (3999, 2048), (4000, 2048), (4001, 4096),
    (9999, 4096), (10000, 4096), (10001, 8192), (14425, 8192),
])
def test_body_length_capacity_boundaries(length, tokens):
    body = _body(length)
    assert len(body) == length
    assert mail._generation_max_tokens(body) == tokens
    assert mail._generation_max_tokens(body.replace('\n', '\r\n')) == tokens


def _no_office(monkeypatch):
    dispatched = []
    monkeypatch.setattr(mail.workflow, 'run_office_workflow',
                        lambda *args, **kwargs: dispatched.append((args, kwargs)))
    return dispatched


@pytest.mark.parametrize('length,tokens', [
    (4000, 2048), (4001, 4096), (10000, 4096), (10001, 8192), (14425, 8192),
])
def test_generation_band_uses_real_budget_request_and_review_stays_600(
        tmp_path, monkeypatch, local_client, length, tokens):
    body = _body(length)
    _, _, base, _, ref = _mail_source(tmp_path, body)
    answers = [_ready(body=body, task='x', owner='x', deadline='x', schema=4),
               {'approved': False, 'reason': 'offline review stops before Office'}]
    make, ledger, _ = local_client
    held_at_post = []
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192))

    def on_post():
        held_at_post.append(ledger.snapshot()['held_micro'])
        session.result = completion(content=json.dumps(answers[len(session.posts) - 1]))

    session.on_post = on_post
    dispatched = _no_office(monkeypatch)
    client = make(session)
    result = mail.run_task('整理邮件为表格和报告', base, source_artifact=ref, client=client)
    requests = [json.loads(options['data']) for _, options in session.posts]
    assert [request['max_tokens'] for request in requests] == [tokens, 600]
    assert held_at_post == [posted_reserve(session, i) for i in range(2)]
    assert result['error'] == 'mail_analysis_review_rejected'
    assert client.calls == 2 and dispatched == []
    assert ledger.snapshot() == {'limit_micro': 5_000_000, 'charged_micro': 120,
                                 'held_micro': 0, 'remaining_micro': 4_999_880}


@pytest.mark.parametrize('tokens,available', [(4096, 2048), (8192, 4096)])
def test_metadata_insufficient_for_capacity_blocks_paid_request(local_client, tokens, available):
    make, ledger, _ = local_client
    session = FakeSession(metadata=model_metadata(max_output_tokens=available))
    client = make(session)
    with pytest.raises(ModelError, match='model_limits_changed'):
        client.complete('JSON', 'bounded mail generation', max_tokens=tokens)
    assert session.posts == [] and client.calls == 0
    assert ledger.snapshot()['held_micro'] == ledger.snapshot()['charged_micro'] == 0


def test_above_capacity_blocks_all_http_before_reservation(local_client):
    make, ledger, _ = local_client
    session = FakeSession(metadata=model_metadata(max_output_tokens=16384))
    with pytest.raises(ModelError, match='model_input_invalid'):
        make(session).complete('JSON', 'bounded mail generation', max_tokens=8193)
    assert session.gets == session.posts == []
    assert ledger.snapshot()['held_micro'] == ledger.snapshot()['charged_micro'] == 0


def test_default_output_capacity_remains_1024(local_client):
    make, _, _ = local_client
    session = FakeSession()
    make(session).complete('JSON', 'default capacity')
    assert json.loads(session.posts[0][1]['data'])['max_tokens'] == 1024
    assert model.MAX_OUTPUT == 8192


def test_large_output_still_requires_prompt_plus_output_context(local_client):
    make, ledger, _ = local_client
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192, context_window=8192))
    with pytest.raises(ModelError, match='model_input_exceeds_context'):
        make(session).complete('JSON', 'bounded mail generation', max_tokens=8192)
    assert session.posts == []
    assert ledger.snapshot()['held_micro'] == ledger.snapshot()['charged_micro'] == 0


def test_long_generation_budget_rejection_keeps_old_hold_without_downgrade_or_office(
        tmp_path, monkeypatch, local_client):
    _, _, base, _, ref = _mail_source(tmp_path, _body(14425))
    make, ledger, _ = local_client
    ledger.reserve(4_950_000, 'unresolved-old-request')
    before = ledger.snapshot()
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192))
    dispatched = _no_office(monkeypatch)
    client = make(session)
    result = mail.run_task('整理邮件', base, source_artifact=ref, client=client)
    assert result['error'] == 'budget_rejected'
    assert session.posts == [] and client.calls == 0 and dispatched == []
    assert ledger.snapshot() == before


@pytest.mark.parametrize('body', [_body(24000), '\n'.join(['x'] * 301)])
def test_oversize_context_never_requests_model(tmp_path, monkeypatch, local_client, body):
    _, _, base, _, ref = _mail_source(tmp_path, body)
    make, ledger, _ = local_client
    session = FakeSession()
    dispatched = _no_office(monkeypatch)
    result = mail.run_task('整理邮件', base, source_artifact=ref, client=make(session))
    assert result['error'] == 'mail_analysis_content_limit'
    assert session.gets == session.posts == [] and dispatched == []
    assert ledger.snapshot()['held_micro'] == ledger.snapshot()['charged_micro'] == 0


def test_context_existing_boundaries_remain_exact():
    assert len(mail.context(_snapshot('\n'.join(['x'] * 300)))['body_lines']) == 300
    overhead = len(json.dumps(mail.context(_snapshot('x')), ensure_ascii=False)) - 1
    accepted = _snapshot('x' * (24000 - overhead))
    assert len(json.dumps(mail.context(accepted), ensure_ascii=False)) == 24000
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_content_limit'):
        mail.context(dict(accepted, body=accepted['body'] + 'x'))


def test_truncated_long_generation_is_settled_failure_without_retry_or_office(
        tmp_path, monkeypatch, local_client):
    _, _, base, _, ref = _mail_source(tmp_path, _body(14425))
    make, ledger, _ = local_client
    response = completion(content='{"state":"ready",', usage={
        'prompt_tokens': 100, 'completion_tokens': 8192, 'total_tokens': 8292})
    response['choices'][0]['finish_reason'] = 'length'
    session = FakeSession(metadata=model_metadata(max_output_tokens=8192), result=response)
    dispatched = _no_office(monkeypatch)
    client = make(session)
    result = mail.run_task('整理邮件', base, source_artifact=ref, client=client)
    assert result['ok'] is False and result['state'] == 'failed'
    assert result['error'] == 'model_response_incomplete'
    assert len(session.posts) == client.calls == 1 and dispatched == []
    assert json.loads(session.posts[0][1]['data'])['max_tokens'] == 8192
    assert client.last_call['state'] == 'settled'
    assert ledger.snapshot()['charged_micro'] == 100 * model.INPUT_RATE + 8192 * model.OUTPUT_RATE
    assert ledger.snapshot()['held_micro'] == 0
    task = Path(result['root'])
    assert not (task / 'mail-analysis-candidate.json').exists()
    assert not (task / 'mail-analysis-review.json').exists()
    assert not list(task.glob('workflow-*'))
