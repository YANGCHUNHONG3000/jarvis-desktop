"""Output coverage, clarification and retained legacy mail contracts (offline)."""
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest

from agent_service import mail_understanding as mail, capability_router as router
from agent_service.computer import ComputerSession
from test_mail_understanding import _context, _mail_source, _ready, FakeClient

BODY = '小王周一整理采购资料。\n小李周二核对两份成果。\n更正：周一的整理已取消，请勿再执行。'


def subset():
    value = _ready(body=BODY, task='周一整理已取消', owner='小王', deadline='周一')
    value['scope'] = {'kind': 'subset', 'description': '仅整理周一事项及更正', 'lines': ['B1', 'B3']}
    value['items'][0]['kind'] = '决策'
    value['items'][0]['evidence'].append({'line': 'B3', 'quote': BODY.split('\n')[2]})
    value['summary'][0] = {'text': '周一整理事项已取消', 'evidence': deepcopy(value['items'][0]['evidence'])}
    return value


def nonready(state='needs_input'):
    return {'state': state, 'reason': '请说明“我”对应邮件中的哪位人员？',
            'scope': None, 'title': '', 'formats': [], 'summary': [], 'items': []}


@pytest.mark.parametrize('scope', [None, [], {},
    {'kind': 'other', 'description': '范围', 'lines': ['B1']},
    {'kind': 'subset', 'description': '范围', 'lines': []},
    {'kind': 'subset', 'description': '范围', 'lines': ['B1', 'B1']},
    {'kind': 'subset', 'description': '范围', 'lines': ['B3', 'B1']},
    {'kind': 'subset', 'description': '范围', 'lines': ['B9']},
    {'kind': 'subset', 'description': '范围', 'lines': [True]},
    {'kind': 'subset', 'description': '范围', 'lines': 'B1'},
    {'kind': 'full', 'description': '范围', 'lines': ['B1']},
    {'kind': 'subset', 'description': '范围', 'lines': ['B1'], 'extra': 'ignored?'}])
def test_invalid_scope_rejected(scope):
    value = subset(); value['scope'] = scope
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_scope_invalid'):
        mail.validate(value, _context(BODY))


def test_scope_cannot_authorize_citations_outside_selection():
    value = subset(); value['scope']['lines'] = ['B1']
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_evidence_invalid'):
        mail.validate(value, _context(BODY))


def test_compiled_outputs_disclose_subset_and_full_body_context_review():
    value = mail.validate(subset(), _context(BODY))
    steps = mail.compile_steps(value)
    rows = steps[0]['data']['rows']
    assert rows[1][2:4] == ['不适用（摘要）', '不适用（摘要）']
    assert rows[2][2:4] == ['小王', '周一']
    assert rows[-1] == ['范围', '按指定范围整理：仅整理周一事项及更正',
                        '不适用', '不适用', 'B1；B3',
                        '所选邮件完整正文用于上下文审查；范围仅指本次输出，不代表其他事项不存在。']
    assert steps[1]['data']['table']['rows'] == rows
    assert '完整正文用于上下文审查' in steps[1]['data']['paragraphs'][-1]
    assert not any('周二' in str(row) for row in rows)


def test_schema1_remains_explicit_and_does_not_gain_scope_rows():
    legacy = _ready(); legacy.pop('scope')
    assert mail.validate(legacy, _context(), schema=1) == legacy
    steps = mail.compile_steps(legacy)
    assert len(steps[0]['data']['rows']) == 3 and len(steps[1]['data']['paragraphs']) == 3
    assert steps[0]['data']['rows'][1][2:4] == ['未说明', '未说明']
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
        mail.validate(legacy, _context())
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
        mail.validate(_ready(), _context(), schema=1)


def test_summary_only_output_does_not_claim_missing_fields_or_missing_details():
    value = _ready(); value['items'] = []
    rows = mail.compile_steps(mail.validate(value, _context()))[0]['data']['rows']
    assert rows[1][2:4] == ['不适用（摘要）', '不适用（摘要）']
    assert not any('见明细' in str(row) for row in rows)


@pytest.mark.parametrize('field,value', [('scope', {'kind': 'subset'}), ('formats', ['document']),
                                        ('items', [{'task': 'run'}]), ('title', '报告')])
def test_nonready_cannot_carry_executable_content(field, value):
    candidate = nonready(); candidate[field] = value
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
        mail.validate(candidate, _context())


@pytest.mark.parametrize('state', ['needs_input', 'unsupported'])
def test_nonready_stops_before_review_or_office_and_preserves_original(tmp_path, monkeypatch, state):
    _, original, base, _, ref = _mail_source(tmp_path)
    old = {p: p.read_bytes() for p in original.rglob('*') if p.is_file()}
    task = base / ('task-' + uuid.uuid4().hex)
    client = FakeClient(nonready(state))
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('Office dispatched'))
    result = mail.run_task('整理我负责的任务', base, task_root=task, source_artifact=ref, client=client)
    assert result['state'] == state and result['error'] == 'mail_analysis_' + state
    assert len(client.calls) == 1 and result['artifacts'] == []
    assert not list(task.glob('workflow-*')) and not (task / 'mail-analysis-plan.json').exists()
    assert all(p.read_bytes() == raw for p, raw in old.items())
    before = (task / 'result.json').read_bytes()
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_replay_blocked'):
        mail.run_task('小王的任务', base, task_root=task, source_artifact=ref, client=client)
    assert (task / 'result.json').read_bytes() == before and len(client.calls) == 1


def test_scope_review_sees_unselected_correction_and_blocks_semantic_mismatch(tmp_path, monkeypatch):
    _, _, base, _, ref = _mail_source(tmp_path, BODY)
    value = _ready(body=BODY, task='整理采购资料', owner='小王', deadline='周一')
    value['scope'] = {'kind': 'subset', 'description': '周一事项', 'lines': ['B1']}
    client = FakeClient(value, {'approved': False, 'reason': 'B3已取消，不能遗漏更正'})
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('Office dispatched'))
    result = mail.run_task('整理周一事项', base, source_artifact=ref, client=client)
    assert result['error'] == 'mail_analysis_review_rejected'
    assert json.loads(client.calls[1]['user'])['body_lines']['B3'] == BODY.split('\n')[2]


def test_cancel_after_clarification_reply_remains_cancelled(tmp_path):
    _, _, base, _, ref = _mail_source(tmp_path)
    task = base / ('task-' + uuid.uuid4().hex)
    client = FakeClient(nonready(), on_call=lambda *_: (task / 'cancel.flag').touch())
    result = mail.run_task('我的任务', base, task_root=task, source_artifact=ref, client=client)
    assert result['state'] == 'cancelled' and result['error'] == 'task_cancelled'
    assert not result['artifacts'] and len(client.calls) == 1


@pytest.mark.parametrize('deadline', [None, '一', '周二'])
def test_explicit_weekday_heading_cannot_be_dropped_even_with_model_approval(tmp_path, monkeypatch, deadline):
    body = '周一：整理三份项目资料。\n周二：核对资料。'
    _, _, base, _, ref = _mail_source(tmp_path, body)
    value = _ready(body=body, owner=None, deadline=deadline, schema=4)
    client = FakeClient(value, {'approved': True, 'reason': '误放行'})
    monkeypatch.setattr(mail.workflow, 'run_office_workflow', lambda *a, **kw: pytest.fail('Office dispatched'))
    result = mail.run_task('保留星期日程', base, source_artifact=ref, client=client)
    assert not result['ok'] and len(client.calls) == 1 and not result['artifacts']
    assert result['error'] in {'mail_analysis_schedule_missing', 'mail_analysis_field_unquoted'}


@pytest.mark.parametrize('heading', ['周一', '下周一', '星期五', '本星期六', '礼拜天'])
def test_weekday_heading_is_kept_verbatim_without_date_calculation(heading):
    body = heading + '：整理资料。'
    value = _ready(body=body, owner=None, deadline=heading)
    assert mail.validate(value, _context(body)) == value


def test_schema1_weekday_history_not_reinterpreted():
    body = '周一：整理资料。'
    value = _ready(body=body, owner=None, deadline=None); value.pop('scope')
    assert mail.validate(value, _context(body), schema=1) == value


def test_schedule_heading_does_not_replace_a_different_explicit_cutoff():
    body = '周一：请周五前提交报价。'
    value = _ready(body=body, task='周一安排：请周五前提交报价', owner=None, deadline='周五前')
    assert mail.validate(value, _context(body)) == value
    value['items'][0]['task'] = '下周一安排：请周五前提交报价'
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schedule_missing'):
        mail.validate(value, _context(body))


def test_capability_clarification_is_saved_once_and_reason_not_executed(tmp_path):
    _, _, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = router.prepare_mail(session, '我的任务生成Word', source_ref=ref, task_base=base)
    class Client(FakeClient):
        def complete(self, system, user, **kw):
            return super().complete(system=system, user=user, **kw)
    client = Client(nonready())
    result = router.execute(session, plan, approval=plan['sha256'], task_base=base, client=client)
    assert result['state'] == 'needs_input' and result['reason'] == nonready()['reason']
    assert result['artifacts'] == []
    before = len(client.calls)
    assert router.execute(session, plan, approval=plan['sha256'], task_base=base, client=client) == result
    assert len(client.calls) == before == 1
    text = router.describe_result({**result, 'reason': '问题\x00\n' + '长' * 200})
    assert '\x00' not in text and '长' * 161 not in text and '原任务记录保留' in text
