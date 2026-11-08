"""Offline schema4 citation positioning; exact text never replaces semantic review."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import mail_understanding as mail
from test_mail_understanding import BODY, FakeClient, _context, _mail_source, _ready


def _candidate(body=BODY + '\n备用行', schema=4):
    value = _ready(body=body, schema=schema)
    if schema == 1:
        value.pop('scope')
    for group in ('summary', 'items'):
        value[group][0]['evidence'][0]['line'] = 'B2'
    return value, _context(body)


def test_unique_exact_quote_corrects_summary_and_item_in_copy_only():
    raw, data = _candidate()
    before, context_before = deepcopy(raw), deepcopy(data)
    value = mail.validate(raw, data, schema=4)
    for group in ('summary', 'items'):
        assert value[group][0]['evidence'] == [{'line': 'B1', 'quote': BODY}]
    assert raw == before and data == context_before
    value['summary'][0]['evidence'][0]['quote'] = 'changed'
    assert raw == before


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_legacy_schemas_reject_mistyped_known_line_even_for_unique_quote(schema):
    value, data = _candidate(schema=schema)
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, data, schema=schema)


@pytest.mark.parametrize('change', [
    'unknown_id', 'nonstring_id', 'missing_quote', 'extra_key', 'nonlist', 'four_quotes',
    'empty_quote', 'long_quote', 'edited_quote', 'no_match', 'duplicate_after_anchor',
])
def test_invalid_or_unsupported_evidence_is_never_repaired(change):
    raw, data = _candidate()
    evidence = raw['summary'][0]['evidence']
    if change == 'unknown_id':
        evidence[0]['line'] = 'B999'
    elif change == 'nonstring_id':
        evidence[0]['line'] = True
    elif change == 'missing_quote':
        evidence[0].pop('quote')
    elif change == 'extra_key':
        evidence[0]['extra'] = True
    elif change == 'nonlist':
        raw['summary'][0]['evidence'] = tuple(evidence)
    elif change == 'four_quotes':
        evidence *= 4
    elif change == 'empty_quote':
        evidence[0]['quote'] = '   '
    elif change == 'long_quote':
        evidence[0]['quote'] = '字' * 501
    elif change == 'edited_quote':
        evidence[0]['quote'] = BODY.replace('提交', '上交')
    elif change == 'no_match':
        evidence[0]['quote'] = '正文不存在的文字'
    else:
        evidence.append(deepcopy(evidence[0]))
    before = deepcopy(raw)
    with pytest.raises(mail.MailUnderstandingError):
        mail.validate(raw, data, schema=4)
    assert raw == before


def test_ambiguity_is_checked_in_whole_body_even_if_only_one_match_is_in_scope():
    value, data = _candidate(BODY + '\n备用行\n' + BODY)
    value['scope'].update(kind='subset', lines=['B1'])
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, data, schema=4)


def test_unique_matching_line_outside_scope_is_rejected_without_scope_expansion():
    value, data = _candidate()
    value['scope'].update(kind='subset', lines=['B2'])
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, data, schema=4)
    assert value['scope']['lines'] == ['B2']


def test_known_original_id_can_be_outside_scope_if_only_matching_target_is_inside():
    value, data = _candidate()
    value['scope'].update(kind='subset', lines=['B1'])
    normalized = mail.validate(value, data, schema=4)
    assert normalized['scope']['lines'] == ['B1']
    assert normalized['items'][0]['evidence'][0]['line'] == 'B1'


def test_already_correct_reference_remains_valid_when_quote_is_repeated_elsewhere():
    value, data = _candidate(BODY + '\n备用行\n' + BODY)
    for group in ('summary', 'items'):
        value[group][0]['evidence'][0]['line'] = 'B3'
    normalized = mail.validate(value, data, schema=4)
    assert normalized['summary'][0]['evidence'][0]['line'] == 'B3'
    assert normalized['items'][0]['evidence'][0]['line'] == 'B3'


def _run(tmp_path, monkeypatch, *, approved=True):
    raw, _data = _candidate()
    _, _, base, _, ref = _mail_source(tmp_path, BODY + '\n备用行')
    client = FakeClient(raw, {'approved': approved, 'reason': '离线语义复核'})
    dispatched = []
    def execute(steps, _root, **_kwargs):
        dispatched.append(deepcopy(steps))
        return {'ok': True, 'state': 'succeeded', 'completed': [], 'receipts': {}}
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', execute)
    result = mail.run_task('整理全文为Word和Excel', base, source_artifact=ref, client=client)
    return Path(result['root']), base, result, raw, client, dispatched


def _read(task, name):
    return json.loads((task / name).read_text(encoding='utf-8'))


def test_run_saves_raw_citations_reviews_corrected_plan_and_verify_reconstructs(tmp_path, monkeypatch):
    task, base, result, raw, client, dispatched = _run(tmp_path, monkeypatch)
    assert result['ok'] and len(client.calls) == 2
    candidate = _read(task, 'mail-analysis-candidate.json')
    plan = _read(task, 'mail-analysis-plan.json')
    assert candidate['value'] == raw
    for group in ('summary', 'items'):
        assert candidate['value'][group][0]['evidence'][0]['line'] == 'B2'
        assert plan['value'][group][0]['evidence'][0]['line'] == 'B1'
        assert plan['value'][group][0]['evidence'][0]['quote'] == BODY
    review = json.loads(client.calls[1]['user'])
    assert review['candidate'] == plan['value'] and review['body_lines'] == _context(BODY + '\n备用行')['body_lines']
    assert dispatched == [plan['steps']]
    assert mail.verify_task(task, base, plan['steps'])
    assert 'text location only' in client.calls[1]['system']


def test_semantic_review_rejection_still_prevents_any_office_dispatch(tmp_path, monkeypatch):
    task, _base, result, raw, client, dispatched = _run(tmp_path, monkeypatch, approved=False)
    assert result['error'] == 'mail_analysis_review_rejected' and not dispatched
    assert len(client.calls) == 2 and not list(task.glob('workflow-*'))
    assert _read(task, 'mail-analysis-candidate.json')['value'] == raw
    assert _read(task, 'mail-analysis-plan.json')['value']['summary'][0]['evidence'][0]['line'] == 'B1'


@pytest.mark.parametrize('change', ['raw_quote', 'unknown_line', 'plan_line', 'context', 'review', 'steps'])
def test_reverification_rejects_changed_source_citation_plan_review_or_steps(tmp_path, monkeypatch, change):
    task, base, _result, _raw, _client, _dispatched = _run(tmp_path, monkeypatch)
    plan = _read(task, 'mail-analysis-plan.json')
    steps = deepcopy(plan['steps'])
    if change in ('raw_quote', 'unknown_line'):
        name = 'mail-analysis-candidate.json'
        record = _read(task, name)
        cite = record['value']['summary'][0]['evidence'][0]
        cite['quote' if change == 'raw_quote' else 'line'] = '不支持的引句' if change == 'raw_quote' else 'B999'
    elif change == 'plan_line':
        name, record = 'mail-analysis-plan.json', plan
        record['value']['summary'][0]['evidence'][0]['line'] = 'B2'
    elif change == 'context':
        name = 'mail-analysis-request.json'
        record = _read(task, name)
        record['context']['body_lines']['B1'] = '改变的来源'
    elif change == 'review':
        name = 'mail-analysis-review.json'
        record = _read(task, name)
        record['verdict']['approved'] = False
    else:
        name, record = None, None
        steps[0]['data']['title'] = '未经批准的标题'
    if name is not None:
        (task / name).write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(mail.MailUnderstandingError):
        mail.verify_task(task, base, steps)
