"""Only stopped, provably undispatched mail analyses may prepare a new request."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import mail_followup as followup, mail_understanding as mail
from test_mail_understanding import _mail_source, _ready, FakeClient
from test_mail_scope import nonready


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _stamp(root):
    return {str(p): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def _stopped(tmp_path, rejected=False):
    _, _, base, _, ref = _mail_source(tmp_path)
    client = FakeClient(_ready(schema=4), {'approved': False, 'reason': '请明确需要整理的事项。'}) if rejected else FakeClient(nonready())
    raw = mail.run_task('只整理我负责的事项', base, source_artifact=ref, client=client)
    assert raw['ok'] is False
    return Path(raw['root']), base, ref


@pytest.mark.parametrize('rejected', [False, True])
def test_prepare_is_read_only_repeatable_and_requires_explicit_new_execution(tmp_path, monkeypatch, rejected):
    task, base, ref = _stopped(tmp_path, rejected)
    before = _stamp(base)
    monkeypatch.setattr(mail, 'run_task', lambda *a, **k: pytest.fail('replay'))
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **k: pytest.fail('Office'))
    view = followup.inspect_followup(task, base)
    assert view['eligible'] is True and view['question']
    assert '尚无可续跑' in followup.describe(view)
    a = followup.prepare_followup(task, base, view['token'])
    b = followup.prepare_followup(task, base, view['token'])
    assert a == b and a['source_ref'] == ref and a['instruction'] == '只整理我负责的事项'
    assert _stamp(base) == before


def test_rejected_old_compiler_plan_is_verified_without_recompilation(tmp_path, monkeypatch):
    task, base, _ = _stopped(tmp_path, True)
    # This is an old schema2 history record, not a new schema3 rejection.
    request = _read(task/'mail-analysis-request.json'); request['schema'] = 2
    _write(task/'mail-analysis-request.json', request)
    request_hash = mail.workflow._hash(request)
    candidate = _read(task/'mail-analysis-candidate.json'); candidate['request_sha256'] = request_hash
    candidate['value']['scope']['lines'] = list(request['context']['body_lines'])
    _write(task/'mail-analysis-candidate.json', candidate)
    result = _read(task/'result.json'); result['mail_analysis_schema'] = 2
    _write(task/'result.json', result)
    plan = _read(task/'mail-analysis-plan.json')
    plan['request_sha256'] = request_hash
    plan['steps'][0]['data']['rows'][1][2:4] = ['未说明', '未说明']
    plan['steps'][1]['data']['table']['rows'][1][2:4] = ['未说明', '未说明']
    _write(task/'mail-analysis-plan.json', plan)
    review = _read(task/'mail-analysis-review.json'); review['plan_sha256'] = mail.workflow._hash(plan)
    _write(task/'mail-analysis-review.json', review)
    monkeypatch.setattr(mail, 'compile_steps', lambda *a: pytest.fail('must not recompile rejected plan'))
    assert followup.inspect_followup(task, base)['eligible'] is True


@pytest.mark.parametrize('name', ['workflow-orphan', 'resume', 'cancel.flag', 'office-intent.json', 'output.docx', 'unknown.json'])
def test_unknown_or_effect_records_block_prepare(tmp_path, name):
    task, base, _ = _stopped(tmp_path)
    (task/name).write_text('preserved', encoding='utf-8')
    before = _stamp(base)
    view = followup.inspect_followup(task, base)
    assert view == {'eligible': False, 'code': 'mail_followup_effects_unknown'}
    assert _stamp(base) == before


@pytest.mark.parametrize('state,error', [('succeeded', None), ('failed', 'task_deadline'),
    ('cancelled', 'task_cancelled'), ('failed', 'model_error'), ('unknown', 'mail_analysis_needs_input')])
def test_unapproved_states_never_offer_retry(tmp_path, state, error):
    task, base, _ = _stopped(tmp_path)
    result = _read(task/'result.json'); result.update(state=state, error=error)
    _write(task/'result.json', result)
    assert not followup.inspect_followup(task, base)['eligible']


@pytest.mark.parametrize('name', ['request.txt', 'mail-analysis-request.json', 'mail-analysis-candidate.json',
                                'result.json', 'sources/source.json', 'sources/provenance.json', 'sources/source.docx'])
def test_changed_records_or_source_refuse_prepare(tmp_path, name):
    task, base, ref = _stopped(tmp_path)
    view = followup.inspect_followup(task, base)
    path = task/name
    if path.suffix == '.json':
        value = _read(path); value['tampered'] = True; _write(path, value)
    else:
        path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        followup.prepare_followup(task, base, view['token'])


@pytest.mark.parametrize('field,value', [('ok', True), ('artifacts', [{}]), ('completed', ['s1']),
    ('mail_analysis_schema', 1), ('mail_analysis_schema', True), ('workflow_root', 'workflow-unknown'), ('reason', 'different')])
def test_result_must_match_proven_terminal_analysis(tmp_path, field, value):
    task, base, _ = _stopped(tmp_path)
    result = _read(task/'result.json'); result[field] = value; _write(task/'result.json', result)
    assert not followup.inspect_followup(task, base)['eligible']


@pytest.mark.parametrize('field,value', [('approved', True), ('approved', 0), ('reason', 'changed')])
def test_rejection_review_must_be_explicit_false_and_bound(tmp_path, field, value):
    task, base, _ = _stopped(tmp_path, True)
    review = _read(task/'mail-analysis-review.json'); review['verdict'][field] = value
    _write(task/'mail-analysis-review.json', review)
    assert not followup.inspect_followup(task, base)['eligible']


def test_source_original_and_valid_record_byte_change_invalidate_preview(tmp_path):
    task, base, ref = _stopped(tmp_path)
    view = followup.inspect_followup(task, base)
    result = _read(task/'result.json'); result['budget'] = {'new': 1}; _write(task/'result.json', result)
    assert followup.inspect_followup(task, base)['eligible'] is True
    with pytest.raises(ValueError, match='mail_followup_changed'):
        followup.prepare_followup(task, base, view['token'])
    Path(ref['artifact']).write_bytes(b'changed')
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_source_changed'


def test_inventory_race_is_rejected(tmp_path, monkeypatch):
    task, base, _ = _stopped(tmp_path)
    original = followup._inventory; calls = []
    def inventory(*args):
        value = original(*args); calls.append(1)
        if len(calls) == 2:
            value['result.json'] = 'changed'
        return value
    monkeypatch.setattr(followup, '_inventory', inventory)
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_changed'


def test_links_and_non_mail_are_refused(tmp_path, monkeypatch):
    task, base, ref = _stopped(tmp_path)
    assert followup.inspect_followup(ref['task_root'], base)['code'] == 'mail_followup_not_mail'
    original = followup._linked
    monkeypatch.setattr(followup, '_linked', lambda p: Path(p).name == 'source.docx' or original(p))
    assert not followup.inspect_followup(task, base)['eligible']


def test_result_replaced_between_read_and_initial_inventory_is_rejected(tmp_path, monkeypatch):
    task, base, _ = _stopped(tmp_path)
    original = followup._inventory
    def inventory(*args):
        result = _read(task/'result.json'); result['state'] = 'unknown'
        _write(task/'result.json', result)
        return original(*args)
    monkeypatch.setattr(followup, '_inventory', inventory)
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_changed'


def test_claim_bool_is_not_a_schema_integer(tmp_path):
    task, base, _ = _stopped(tmp_path)
    _write(task/'mail-analysis-claim.json', {'schema': True})
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_record_invalid'


@pytest.mark.parametrize('file', ['mail-analysis-plan.json', 'mail-analysis-review.json'])
def test_rejected_plan_review_binding_cannot_change(tmp_path, file):
    task, base, _ = _stopped(tmp_path, True)
    value = _read(task/file)
    value['request_sha256' if file.endswith('plan.json') else 'plan_sha256'] = 'b'*64
    _write(task/file, value)
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_record_invalid'


def test_missing_request_with_claim_is_invalid_mail_not_non_mail(tmp_path):
    task, base, _ = _stopped(tmp_path)
    (task/'mail-analysis-request.json').rename(task/'lost-request.json')
    assert followup.inspect_followup(task, base)['code'] == 'mail_followup_record_invalid'


@pytest.mark.parametrize('token', [None, '', 'a'*63, True])
def test_invalid_token_cannot_prepare(tmp_path, token):
    task, base, _ = _stopped(tmp_path)
    with pytest.raises(ValueError, match='mail_followup_changed'):
        followup.prepare_followup(task, base, token)
