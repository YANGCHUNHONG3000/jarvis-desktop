"""Offline display checks using the preserved §56 failed review when available."""

import json
from pathlib import Path

import pytest

from agent_service.task_diagnostics import explain_failure, explain_outcome, explain_recovery


ROOT = Path(__file__).resolve().parents[1]
FAILED_RUN = (ROOT / 'data/computer_sessions/computer-c1239aacc9904a54a5a809491e17f376'
              / 'goals/fce82312503e8490502b149b5533d2a1423c559e7d184f661c84541f6ed2178c.run')


@pytest.mark.parametrize('code', ['comparison_transport_invalid',
                                 'comparison_review_invalid', 'comparison_evidence_invalid'])
def test_review_format_failure_points_to_conditional_history_import(code):
    outcome = {'ok': False, 'code': 'agent_no_safe_next_action', 'artifacts': [],
               'failures': [{'code': code}]}
    before = json.dumps(outcome, sort_keys=True)
    direct, terminal = explain_failure(code), explain_outcome(outcome)
    for message in (direct, terminal):
        assert '不符合约定格式' in message and '历史来源' in message
        assert '无 Office 派发' in message and '批准后' in message
        assert '原失败不会改写' in message and '未知作用' in message
    assert '当前已无安全的下一步' in terminal
    assert json.dumps(outcome, sort_keys=True) == before


def test_preserved_gui_failure_explains_actual_missing_publisher_evidence():
    outcome_file = FAILED_RUN / 'outcome.json'
    record_file = FAILED_RUN / 'step-02/environment-result.json'
    if not outcome_file.is_file() or not record_file.is_file():
        pytest.skip('§56 original failed GUI records are unavailable; live verification was not rerun')
    outcome = json.loads(outcome_file.read_text(encoding='utf-8'))
    record = json.loads(record_file.read_text(encoding='utf-8'))['data']
    assessment = record['comparison']['assessment']
    assert outcome['ok'] is False and outcome['code'] == 'agent_no_safe_next_action'
    assert outcome['failures'][-1]['code'] == 'comparison_independence_insufficient'
    assert assessment['complete_independent_count'] == 0
    assert assessment['required_independent_count'] == 2
    message = explain_failure(outcome['code'], assessment=assessment,
                              dimensions=['文件格式'], artifacts=outcome['artifacts'])
    assert '0/2' in message
    assert '未提供发布方依据' in message
    assert '来源组1' in message and '来源组2' in message
    assert '本次诊断未确认新的成果' in message
    assert '建议：' in message
    from_outcome = explain_outcome(outcome)
    assert '当前已无安全的下一步' in from_outcome
    assert '独立且有据的来源组不足' in from_outcome
    assert '0/2' not in from_outcome  # The outcome alone does not carry the assessment.
    assert '0/2' in explain_outcome(outcome, assessment=assessment, dimensions=['文件格式'])


def test_dimension_and_publisher_gaps_are_named_without_changing_assessment():
    assessment = {'complete_independent_count': 1, 'required_independent_count': 2,
                  'group_details': [{'ids': ['source-a'], 'publisher_supported': False,
                                     'covered_dimensions': [True, False], 'complete': False}],
                  'publisher_evidence': {'source-a': 'navigation'}}
    original = json.dumps(assessment, sort_keys=True)
    message = explain_failure('comparison_coverage_insufficient', assessment=assessment,
                              dimensions=['格式', '许可证'])
    assert '1/2' in message and '许可证' in message
    assert '仅为导航链接' in message
    assert json.dumps(assessment, sort_keys=True) == original


def test_budget_and_unknown_office_result_do_not_claim_success():
    budget = explain_failure('model_budget_exceeded')
    unknown = explain_failure('agent_office_unknown', artifacts=[{'path': 'saved.docx'}])
    assert '当前持久账本' in budget and '历史余额' in budget
    assert '结果未获独立回执' in unknown and '不能假定成功' in unknown
    assert '仍须依据原回执核验' in unknown
    assert explain_outcome({'ok': True, 'code': 'objective_verified'}) == ''
    assert '不能确认成功' in explain_outcome({'state': 'unknown'})


@pytest.mark.parametrize('code', [
    'comparison_capture_import_invalid', 'comparison_capture_import_changed',
    'comparison_capture_import_duplicate'])
def test_capture_import_failure_preserves_original_evidence_without_retry(code):
    message = explain_failure(code)
    assert '查询或来源绑定核验' in message
    assert '不要重复抓取或沿用旧结论' in message
    assert '原任务记录与已采集证据保留' in message


def test_request_budget_failures_distinguish_preflight_and_accounting_fault():
    before = explain_failure('model_input_exceeds_context')
    after = explain_failure('model_usage_exceeds_request_bound')
    assert '尚未发出付费请求' in before
    assert '已记录返回费用并停止后续模型调用' in after
    assert '不能通过换账本' in after


def test_recovery_refusal_identifies_unknown_effect_and_preserves_record():
    row = {'eligible': False, 'code': 'recovery_dispatched_action_not_verified',
           'completed_edits': [], 'artifacts': []}
    message = explain_recovery(row)
    assert '缺少可核验的完成回执' in message
    assert '不能重放' in message
    assert '不视为已安全恢复' in message


def test_recovery_active_and_eligible_have_distinct_next_steps():
    active = explain_recovery({'eligible': False, 'code': 'run_owner_active'})
    ready = explain_recovery({'eligible': True, 'code': 'recovery_ready',
                              'completed_edits': ['search:0', 'read:source-a']})
    assert '不能并行续跑' in active and '待原任务停止' in active
    assert '已核验 2 项' in ready and '恢复预览' in ready
    assert '恢复不会改写历史失败' in ready
