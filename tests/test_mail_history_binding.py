"""A valid Office receipt alone cannot authenticate a mail continuation."""
import json
from pathlib import Path
import uuid

import pytest

from agent_service.task_history import inspect_task
from agent_service.office_workflow import run_office_workflow
from test_task_history import _task, _steps, _fake_office, _files


@pytest.mark.parametrize('marker,final_result', [
    ('claim', True), ('resume_index', True), ('result', True),
    ('claim', False), ('resume_index', False)])
def test_unbound_mail_cannot_expose_unrelated_office_receipts(tmp_path, marker, final_result):
    task = _task(tmp_path)
    workflow = task / ('workflow-' + uuid.uuid4().hex)
    outcome = run_office_workflow(_steps(), workflow, execute=_fake_office)
    result = {'root': str(task), 'kind': 'office', 'state': 'succeeded',
              'ok': True, 'workflow_root': str(workflow), 'completed': ['s1','s2'],
              'artifacts': [{'step': step, 'path': r['artifact'], 'sha256': r['sha256']}
                            for step, r in outcome['receipts'].items()]}
    if final_result:
        (task / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    # These are otherwise valid ordinary Office receipts.
    assert len(inspect_task(task, tmp_path)['artifacts']) == 2
    if marker == 'claim':
        (task / 'mail-analysis-claim.json').write_text('{"schema":1}', encoding='utf-8')
    elif marker == 'resume_index':
        directory = tmp_path / '.mail-review-resumes' / 'children'
        directory.mkdir(parents=True)
        (directory / (task.name + '.json')).write_text('{}', encoding='utf-8')
    else:
        result['mail_review_resume'] = {'parent_root': str(task)}
        (task / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    before = _files(tmp_path)
    inspection = inspect_task(task, tmp_path)
    assert inspection['state'] == 'needs_review' and inspection['artifacts'] == []
    assert inspection['resume']['reason'] == 'mail_analysis_binding_invalid'
    assert _files(tmp_path) == before


def test_mail_binding_is_checked_against_actual_workflow_not_standalone_plan(tmp_path, monkeypatch):
    from agent_service import mail_understanding
    task = _task(tmp_path)
    workflow = task / ('workflow-' + uuid.uuid4().hex)
    outcome = run_office_workflow(_steps(), workflow, execute=_fake_office)
    result = {'root': str(task), 'kind': 'office', 'state': 'succeeded',
              'ok': True, 'workflow_root': str(workflow), 'completed': ['s1','s2'],
              'artifacts': [{'step': step, 'path': r['artifact'], 'sha256': r['sha256']}
                            for step, r in outcome['receipts'].items()]}
    (task / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    (task / 'mail-analysis-plan.json').write_text('{"steps":[]}', encoding='utf-8')
    (task / 'mail-analysis-claim.json').write_text('{"schema":1}', encoding='utf-8')
    calls = []
    def check(_root, _base, steps, parent=None):
        calls.append(steps)
        if steps != []:
            raise ValueError('actual_workflow_not_approved')
        return 'standalone_plan_is_valid'
    monkeypatch.setattr(mail_understanding, 'verify_task', check)
    inspection = inspect_task(task, tmp_path)
    assert calls[-1] == _steps()
    assert inspection['state'] == 'needs_review' and inspection['artifacts'] == []
