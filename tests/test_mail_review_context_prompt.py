"""Prompt dispatch contracts; fixed verdicts do not prove model semantics."""
import json
import uuid

import pytest

from agent_service import mail_understanding as mail
from test_mail_reply_draft import draft_workflow, reply
from test_mail_understanding import BODY, FakeClient, _mail_source


@pytest.mark.parametrize('approved', [True, False])
def test_subset_and_fixed_annotations_are_reviewed_with_full_context_without_bypass(tmp_path, monkeypatch, approved):
    _snapshot, _original, base, _result, ref = _mail_source(tmp_path, BODY + '\n内部来源说明。')
    value = reply(office=True)
    value['scope'] = {'kind': 'subset', 'description': '仅采购事项', 'lines': ['B1']}
    client = FakeClient(value, {'approved': approved, 'reason': '固定测试结论'})
    effects, _execute = draft_workflow(monkeypatch)
    task = base / ('task-' + uuid.uuid4().hex)
    result = mail.run_task('只整理采购事项为Word并拟本地回复，附本次Word，收件人留空。',
                          base, task_root=task, source_artifact=ref, client=client)
    assert len(client.calls) == 2
    request = json.loads(client.calls[1]['user'])
    assert request['candidate']['scope']['lines'] == ['B1']
    assert '内部来源说明' in client.calls[1]['user']
    rows = request['steps'][0]['data']['table']['rows']
    assert rows[0][0] == '说明' and rows[-1][0] == '范围'
    prompt = client.calls[1]['system']
    assert 'not extra outputs' in prompt and 'not the amount of context uploaded' in prompt
    assert 'do not override explicit user constraints' in prompt
    assert 'cancellation/correction/context that changes meaning' in prompt
    if approved:
        assert result['ok'] and effects == ['document', 'email_draft']
    else:
        assert not result['ok'] and result['error'] == 'mail_analysis_review_rejected'
        assert not effects and not list(task.glob('workflow-*'))
