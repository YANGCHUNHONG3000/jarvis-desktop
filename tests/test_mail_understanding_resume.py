"""New mail boundary recovery; offline executors are never native evidence."""
from hashlib import sha256
from pathlib import Path
import json
import uuid

import pytest

from agent_service import mail_understanding as mail, office, office_resume, office_workflow
from test_mail_understanding import _mail_source, _ready, FakeClient


def _interrupted(tmp_path, monkeypatch, *, requested_pause=False):
    _, source, base, _, ref = _mail_source(tmp_path)
    task = base / ('task-' + uuid.uuid4().hex)
    effects = []

    def execute(spec, step_root, stop):
        effects.append(spec['kind'])
        suffix = '.docx' if spec['kind'] == 'document' else '.xlsx'
        path = step_root / ('document.docx' if suffix == '.docx' else 'spreadsheet.xlsx')
        raw = json.dumps(spec, ensure_ascii=False).encode()
        path.write_bytes(raw)
        result = {'ok': True, 'state': 'succeeded', 'artifact': str(path), 'sha256': sha256(raw).hexdigest(),
                  'observed': office._public_observation(spec, office._expected(spec)),
                  'app': {'progid': 'offline-test', 'name': 'offline', 'version': '1'},
                  'application_verified': True, 'file_verified': True}
        if spec['kind'] == 'spreadsheet' and not requested_pause:
            (task / 'cancel.flag').write_text('stop at known receipt boundary')
        return result

    real = office_workflow.run_office_workflow
    def offline_run(steps, root, **kwargs):
        kwargs.setdefault('execute', execute)
        return real(steps, root, **kwargs)
    monkeypatch.setattr(office_workflow, 'run_office_workflow', offline_run)
    client = FakeClient(_ready(schema=4), {'approved': True, 'reason': 'offline review accepted'})
    result = mail.run_task('将所选邮件整理成Word和Excel', base, task_root=task,
                           source_artifact=ref, client=client, pause_after_step=requested_pause)
    assert result['state'] == 'cancelled' and result['completed'] == ['s1'], result
    return task, base, source, effects, execute, client, result


def test_explicit_boundary_pause_resumes_mail_without_reanalysis(tmp_path, monkeypatch):
    task, base, _, effects, execute, client, result = _interrupted(tmp_path, monkeypatch, requested_pause=True)
    assert result['error'] == 'office_paused_after_verified_step'
    assert not (task / 'cancel.flag').exists()
    workflow = Path(result['workflow_root'])
    assert not (workflow / 's2').exists()
    protected = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in task.rglob('*') if p.is_file()}
    preview = office_resume.preview_resume(task, base)
    assert preview['eligible'] and preview['remaining'] == 1
    final = office_resume.resume_task(task, base, preview['token'], uuid.uuid4().hex, execute=execute)
    assert final['ok'] and effects == ['spreadsheet', 'document'] and len(client.calls) == 2
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in protected.items())


def test_recovery_reuses_review_and_completed_step(tmp_path, monkeypatch):
    task, base, _, effects, execute, client, result = _interrupted(tmp_path, monkeypatch)
    before = (task / 'result.json').read_bytes()
    parent_files = {str(p): p.read_bytes() for p in task.glob('mail-analysis-*.json')}
    preview = office_resume.preview_resume(task, base)
    assert preview['eligible'], preview
    attempt = uuid.uuid4().hex
    recovered = office_resume.resume_task(task, base, preview['token'], attempt, execute=execute)
    assert recovered['state'] == 'succeeded', recovered
    assert effects == ['spreadsheet', 'document']
    assert len(client.calls) == 2
    from agent_service.model import MAX_OUTPUT
    assert all(1 <= call['max_tokens'] <= MAX_OUTPUT for call in client.calls)
    assert (task / 'result.json').read_bytes() == before
    assert all(Path(p).read_bytes() == value for p, value in parent_files.items())
    with pytest.raises(ValueError, match='resume_attempt_exists'):
        office_resume.resume_task(task, base, preview['token'], attempt, execute=execute)
    assert effects == ['spreadsheet', 'document']


@pytest.mark.parametrize('change', ['manifest', 'review', 'source_ref', 'instruction'])
def test_recovery_blocks_changed_mail_proof(tmp_path, monkeypatch, change):
    task, base, _, effects, _, _, result = _interrupted(tmp_path, monkeypatch)
    if change == 'manifest':
        path = Path(result['workflow_root']) / 'manifest.json'
        data = json.loads(path.read_text())
        data['steps'][1]['data']['title'] = 'unreviewed title'
        data['steps_sha256'] = office_workflow._hash(data['steps'])
        path.write_text(json.dumps(data))
    elif change == 'instruction':
        (task / 'request.txt').write_text('changed intent', encoding='utf8')
    else:
        path = task / ('mail-analysis-review.json' if change == 'review' else 'mail-analysis-request.json')
        data = json.loads(path.read_text())
        if change == 'review':
            data['verdict']['approved'] = False
        else:
            data['source_ref'] = None
        path.write_text(json.dumps(data))
    preview = office_resume.preview_resume(task, base)
    assert not preview['eligible'], preview
    assert preview['reason'] in {'mail_analysis_plan_changed', 'mail_analysis_review_invalid',
                                  'artifact_source_reference_invalid', 'request_changed'}
    assert effects == ['spreadsheet']


def test_budget_rejection_does_not_dispatch_or_retry(tmp_path, monkeypatch):
    from agent_service.budget import BudgetError
    _, _, base, _, ref = _mail_source(tmp_path)
    calls = []
    class Client:
        ledger = None
        def complete(self, **kwargs):
            calls.append(kwargs)
            raise BudgetError('not enough budget')
    monkeypatch.setattr(office_workflow, 'run_office_workflow', lambda *a, **k: pytest.fail('unexpected Office'))
    result = mail.run_task('理解邮件生成Word', base, source_artifact=ref, client=Client())
    assert result['error'] == 'budget_rejected' and len(calls) == 1
