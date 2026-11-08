"""Offline proof of single REVIEW continuation; no real messages or APIs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service import mail_review_resume as resume, mail_understanding as mail
from agent_service import office, office_resume, office_workflow
from agent_service.model import ModelError
from test_mail_understanding import _mail_source, _ready, FakeClient
from test_mail_understanding_resume import _interrupted


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _stamp(root):
    return {str(p): (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def _stopped(tmp_path):
    _, _, base, _, ref = _mail_source(tmp_path)
    def fail_review(count, call):
        if count == 2:
            raise ModelError('model_input_invalid')
    client = FakeClient(_ready(schema=4), on_call=fail_review)
    result = mail.run_task('将所选邮件整理成Word和Excel', base, source_artifact=ref, client=client)
    assert result['error'] == 'model_input_invalid'
    task = Path(result['root'])
    preview = resume.inspect_resume(task, base)
    assert preview['eligible'], preview
    return task, base, ref, preview


def _run(base, ref, view, client, **kwargs):
    return mail.run_task(view['instruction'], base, source_artifact=ref, client=client,
                         review_resume={k: view[k] for k in ('task_root', 'token')}, **kwargs)


def test_preview_and_prepare_are_readonly(tmp_path):
    task, base, ref, view = _stopped(tmp_path)
    before = _stamp(base)
    a = resume.prepare_resume(task, base, view['token'])
    assert a == resume.prepare_resume(task, base, view['token'])
    assert a['source_ref'] == ref and a['candidate']['value']['scope']['lines'] == 'all'
    assert _stamp(base) == before


@pytest.mark.parametrize('approved', [True, False])
def test_only_review_then_office_after_approval_and_old_parent_untouched(tmp_path, monkeypatch, approved):
    task, base, ref, view = _stopped(tmp_path)
    before = _stamp(task)
    effects = []
    client = FakeClient({'approved': approved, 'reason': '离线复核结论'})
    def execute(steps, root, **kwargs):
        assert len(client.calls) == 1 and approved
        effects.append(steps)
        return {'ok': True, 'state': 'succeeded', 'completed': [], 'receipts': {}}
    monkeypatch.setattr(office_workflow, 'run_office_workflow', execute)
    result = _run(base, ref, view, client)
    assert len(client.calls) == 1
    assert client.calls[0]['system'] == mail.REVIEW and client.calls[0]['max_tokens'] == 600
    assert result['ok'] is approved and bool(effects) is approved
    assert result['mail_review_resume']['parent_root'] == str(task)
    assert _stamp(task) == before
    assert resume.inspect_resume(task, base)['code'] == 'mail_review_resume_already_claimed'
    with pytest.raises(ValueError, match='already_claimed'):
        _run(base, ref, view, FakeClient())


@pytest.mark.parametrize('name', ['workflow-orphan', 'resume', 'cancel.flag', 'office-intent.json',
                                 'output.docx', 'unknown.json', 'mail-analysis-review.json'])
def test_unknown_effect_or_review_file_blocks(tmp_path, name):
    task, base, _, _ = _stopped(tmp_path)
    (task / name).write_text('{}', encoding='utf-8')
    assert resume.inspect_resume(task, base)['code'] == 'mail_review_resume_effects_unknown'


@pytest.mark.parametrize('name', ['request.txt', 'mail-analysis-request.json', 'mail-analysis-candidate.json',
                                'mail-analysis-plan.json', 'result.json', 'sources/source.json',
                                'sources/provenance.json', 'sources/source.docx'])
def test_changed_parent_records_refuse_preview_token(tmp_path, name):
    task, base, _, view = _stopped(tmp_path)
    path = task / name
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError):
        resume.prepare_resume(task, base, view['token'])


@pytest.mark.parametrize('error', ['mail_analysis_review_rejected', 'task_cancelled', 'task_deadline',
                                 'model_json_invalid', 'model_usage_invalid', 'model_unavailable'])
def test_other_failures_are_conservatively_rejected(tmp_path, error):
    task, base, _, _ = _stopped(tmp_path)
    result = _read(task / 'result.json'); result['error'] = error
    _write(task / 'result.json', result)
    assert not resume.inspect_resume(task, base)['eligible']


@pytest.mark.parametrize('change', ['plan', 'raw', 'source_ref', 'source_bytes', 'schema_bool', 'completed'])
def test_structural_tamper_cannot_obtain_fresh_preview(tmp_path, change):
    task, base, ref, _ = _stopped(tmp_path)
    if change == 'source_bytes':
        Path(ref['artifact']).write_bytes(b'changed')
    elif change in ('schema_bool', 'completed'):
        path = task / 'result.json'; value = _read(path)
        value['mail_analysis_schema' if change == 'schema_bool' else 'completed'] = True if change == 'schema_bool' else ['s1']
        _write(path, value)
    else:
        name = {'plan': 'plan', 'raw': 'candidate', 'source_ref': 'request'}[change]
        path = task / ('mail-analysis-' + name + '.json'); value = _read(path)
        if change == 'plan':
            value['steps'][0]['data']['title'] = 'changed'
        elif change == 'raw':
            value['value']['items'][0]['evidence'][0]['quote'] = 'unknown'
        else:
            value['source_ref'] = None
        _write(path, value)
    assert not resume.inspect_resume(task, base)['eligible']


def test_input_substitution_never_claims_or_calls(tmp_path):
    task, base, ref, view = _stopped(tmp_path)
    client = FakeClient()
    with pytest.raises(ValueError, match='inputs_changed'):
        mail.run_task('changed', base, source_artifact=ref, client=client,
                      review_resume={k: view[k] for k in ('task_root', 'token')})
    assert client.calls == [] and not (base / '.mail-review-resumes').exists()


def test_source_substitution_never_claims_or_calls(tmp_path):
    _, base, ref, view = _stopped(tmp_path)
    changed = deepcopy(ref); changed['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='inputs_changed'):
        _run(base, changed, view, FakeClient())
    assert not (base / '.mail-review-resumes').exists()


def test_parent_change_during_review_prevents_office(tmp_path, monkeypatch):
    task, base, ref, view = _stopped(tmp_path)
    def change(*_):
        path = task / 'result.json'; path.write_bytes(path.read_bytes() + b' ')
    client = FakeClient({'approved': True, 'reason': '离线通过'}, on_call=change)
    monkeypatch.setattr(office_workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('Office'))
    result = _run(base, ref, view, client)
    assert not result['ok'] and result['error'] == 'mail_review_resume_binding_changed'
    assert len(client.calls) == 1


def test_failed_review_keeps_claim_and_cannot_nest(tmp_path):
    _, base, ref, view = _stopped(tmp_path)
    def fail(*_):
        raise ModelError('model_network_failed')
    client = FakeClient(on_call=fail)
    result = _run(base, ref, view, client)
    assert result['error'] == 'model_network_failed' and len(client.calls) == 1
    assert resume.inspect_resume(result['root'], base)['code'] == 'mail_review_resume_nested'
    assert resume.inspect_resume(view['task_root'], base)['code'] == 'mail_review_resume_already_claimed'


def test_inventory_race_refuses_prepare(tmp_path, monkeypatch):
    task, base, _, _ = _stopped(tmp_path)
    real = resume._inventory; calls = []
    def inventory(root):
        value = real(root); calls.append(1)
        if len(calls) == 2:
            value['result.json']['sha256'] = '0' * 64
        return value
    monkeypatch.setattr(resume, '_inventory', inventory)
    assert resume.inspect_resume(task, base)['code'] == 'mail_review_resume_changed'


def test_linked_parent_file_refuses_prepare(tmp_path, monkeypatch):
    task, base, _, _ = _stopped(tmp_path)
    real = resume._linked
    monkeypatch.setattr(resume, '_linked', lambda p: Path(p).name == 'source.docx' or real(p))
    assert not resume.inspect_resume(task, base)['eligible']


def test_concurrent_claims_allow_only_one_and_crash_claim_is_never_released(tmp_path):
    task, base, _, view = _stopped(tmp_path)
    payload = resume.prepare_resume(task, base, view['token'])
    children = [base / ('task-' + uuid.uuid4().hex) for _ in range(2)]
    for child in children:
        child.mkdir()
    def claim(child):
        try:
            return resume.claim_resume(payload, child, base)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, children))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 'mail_review_resume_already_claimed' in results
    assert resume.inspect_resume(task, base)['code'] == 'mail_review_resume_already_claimed'


def _paused_child(tmp_path, monkeypatch):
    old, base, ref, view = _stopped(tmp_path)
    effects = []
    def execute(spec, step_root, stop):
        effects.append(spec['kind'])
        path = step_root / ('document.docx' if spec['kind'] == 'document' else 'spreadsheet.xlsx')
        raw = json.dumps(spec, ensure_ascii=False).encode()
        path.write_bytes(raw)
        return {'ok': True, 'state': 'succeeded', 'artifact': str(path), 'sha256': sha256(raw).hexdigest(),
                'observed': office._public_observation(spec, office._expected(spec)),
                'app': {'progid': 'offline-test', 'name': 'offline', 'version': '1'},
                'application_verified': True, 'file_verified': True}
    real = office_workflow.run_office_workflow
    def run(steps, root, **kw):
        kw.setdefault('execute', execute)
        return real(steps, root, **kw)
    monkeypatch.setattr(office_workflow, 'run_office_workflow', run)
    client = FakeClient({'approved': True, 'reason': '离线通过'})
    result = _run(base, ref, view, client, pause_after_step=True)
    assert result['state'] == 'cancelled' and result['completed'] == ['s1'], result
    return old, Path(result['root']), base, effects, execute, result, client


def test_office_step_resume_keeps_review_binding_without_model_replay(tmp_path, monkeypatch):
    old, child, base, effects, execute, result, client = _paused_child(tmp_path, monkeypatch)
    protected = _stamp(old); child_protected = _stamp(child)
    preview = office_resume.preview_resume(child, base)
    assert preview['eligible'], preview
    final = office_resume.resume_task(child, base, preview['token'], uuid.uuid4().hex, execute=execute)
    assert final['ok'], final
    assert effects == ['spreadsheet', 'document'] and len(client.calls) == 1
    assert _stamp(old) == protected
    assert all(_stamp(child)[k] == v for k, v in child_protected.items())
    assert resume.inspect_resume(child, base)['code'] == 'mail_review_resume_nested'


@pytest.mark.parametrize('change', ['marker', 'result', 'claim', 'delete_marker', 'delete_both', 'parent',
                                    'registry', 'child_index', 'delete_child_index', 'delete_parent_claim'])
def test_child_binding_cannot_be_tampered_or_removed_to_downgrade(tmp_path, monkeypatch, change):
    old, child, base, _, _, result, _ = _paused_child(tmp_path, monkeypatch)
    if change in ('delete_child_index', 'delete_parent_claim'):
        path = (base / '.mail-review-resumes' / 'children' / (child.name + '.json') if change == 'delete_child_index'
                else base / '.mail-review-resumes' / (old.name + '.json'))
        path.unlink()
    elif change.startswith('delete'):
        (child / 'mail-review-resume.json').unlink()
        if change == 'delete_both':
            result.pop('mail_review_resume'); _write(child / 'result.json', result)
    elif change == 'result':
        result['mail_review_resume']['token'] = 'a' * 64; _write(child / 'result.json', result)
    elif change == 'parent':
        path = old / 'result.json'; path.write_bytes(path.read_bytes() + b' ')
    else:
        path = (child / 'mail-review-resume.json' if change == 'marker' else
                child / 'mail-analysis-claim.json' if change == 'claim' else
                base / '.mail-review-resumes' / 'children' / (child.name + '.json') if change == 'child_index' else
                base / '.mail-review-resumes' / (old.name + '.json'))
        value = _read(path); value['tampered'] = True; _write(path, value)
    assert not office_resume.preview_resume(child, base)['eligible']


def test_unrelated_broken_claims_do_not_affect_current_child(tmp_path, monkeypatch):
    _, child, base, _, _, _, _ = _paused_child(tmp_path, monkeypatch)
    directory = base / '.mail-review-resumes'
    (directory / 'unknown-extra.txt').write_text('unrelated', encoding='utf-8')
    (directory / ('task-' + 'f' * 32 + '.json')).write_text('not json', encoding='utf-8')
    (directory / 'children' / ('task-' + 'e' * 32 + '.json')).write_text('not json', encoding='utf-8')
    assert office_resume.preview_resume(child, base)['eligible']


def test_plain_task_keeps_original_verification_hash_and_ignores_other_claims(tmp_path, monkeypatch):
    task, base, _, _, _, _, result = _interrupted(tmp_path, monkeypatch, requested_pause=True)
    req = _read(task / 'mail-analysis-request.json')
    candidate = _read(task / 'mail-analysis-candidate.json')
    plan = _read(task / 'mail-analysis-plan.json')
    review = _read(task / 'mail-analysis-review.json')
    snapshot = _read(task / 'sources' / 'source.json')
    expected = mail.workflow._hash({'request': req, 'candidate': candidate, 'plan': plan, 'review': review,
        'snapshot': snapshot, 'provenance': {'kind': 'verified_generated_office_artifact', **req['source_ref']}})
    assert mail.verify_task(task, base, plan['steps'], result) == expected
    directory = base / '.mail-review-resumes'; directory.mkdir()
    (directory / 'children').mkdir()
    (directory / 'unknown.json').write_text('not json', encoding='utf-8')
    (directory / 'children' / ('task-' + 'e' * 32 + '.json')).write_text('not json', encoding='utf-8')
    assert mail.verify_task(task, base, plan['steps'], result) == expected
