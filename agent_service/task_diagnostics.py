"""Read-only Chinese explanations for existing task and recovery decisions.

Callers supply already verified records. This module never decides eligibility,
loads records, contacts providers, or changes a task's recorded result.
"""

from collections.abc import Mapping, Sequence


_COMPARISON_CODES = frozenset({
    'comparison_coverage_insufficient',
    'comparison_independence_insufficient',
    'comparison_semantic_insufficient',
})
_REVIEW_FORMAT_CODES = frozenset({'comparison_transport_invalid',
                                'comparison_review_invalid', 'comparison_evidence_invalid'})

_PUBLISHER_STATUS = {
    'unknown': '未提供发布方依据',
    'name_absent': '引句未出现所称发布方',
    'absent': '引句未在捕获正文中找到',
    'ambiguous': '引句无法唯一定位',
    'bare_name': '引句只有名称',
    'navigation': '引句仅为导航链接',
    'download_label': '引句仅为下载标签',
}

from .publisher_evidence import REASONS as _ATTRIBUTION_REASONS
_PUBLISHER_STATUS.update(_ATTRIBUTION_REASONS)
from .publisher_scope import REASONS as _SCOPE_REASONS
_PUBLISHER_STATUS.update(_SCOPE_REASONS)
_PUBLISHER_STATUS['project_scope_missing'] = '无法确认项目边界，需本页归属依据'

_RECOVERY_REASONS = {
    'run_owner_active': '原任务仍在运行或清理，暂不能同时恢复，也不能并行续跑。',
    'recovery_dispatched_action_not_verified': '已有操作发出，但缺少可核验的完成回执；结果未知，不能重放。',
    'recovery_browser_action_evidence_missing': '浏览器操作缺少独立回执；结果未知，不能重放。',
    'recovery_browser_independent_read_missing': '浏览器页面缺少独立读取证据。',
    'recovery_browser_observation_missing': '浏览器页面缺少独立观察记录。',
    'recovery_browser_checkpoint_missing': '浏览器历史捕获检查点缺失。',
    'recovery_evidence_incomplete': '步骤证据或回执不完整。',
    'recovery_evidence_invalid': '原任务证据未通过安全核验。',
    'recovery_terminal_state_not_safe': '原任务结束状态不满足安全续跑条件。',
    'recovery_lineage_depth': '续跑层数已达到上限。',
    'recovery_already_claimed': '原任务已被另一份续跑计划占用。',
    'recovery_nothing_remaining': '没有经核验可续做的步骤。',
    'recovery_goal_not_completed': '原目标尚未被独立核验完成。',
    'recovery_source_owner_invalid': '成果不属于同一受控任务范围。',
    'recovery_duplicate_edit': '检测到重复编辑记录，不能安全续跑。',
}


def _label(value, fallback):
    if not isinstance(value, str):
        return fallback
    clean = ' '.join(value.split())
    return clean[:64] if clean else fallback


def _assessment_details(assessment, dimensions):
    if not isinstance(assessment, Mapping):
        return []
    details = []
    complete = assessment.get('complete_independent_count')
    required = assessment.get('required_independent_count')
    if (type(complete) is int and type(required) is int
            and 0 <= complete < required <= 100):
        details.append(f'完整且独立的来源组只有 {complete}/{required} 个')
    groups = assessment.get('group_details')
    statuses = assessment.get('publisher_evidence')
    if not isinstance(groups, list):
        return details
    statuses = dict(statuses) if isinstance(statuses, Mapping) else {}
    from .publisher_scope import REASONS as scope_reasons
    scope_reasons = {**scope_reasons, 'project_scope_missing': _PUBLISHER_STATUS['project_scope_missing']}
    scopes = assessment.get('publisher_support_reasons')
    if isinstance(scopes, Mapping):
        statuses.update({sid: status for sid, status in scopes.items() if isinstance(status, str) and status in scope_reasons})
    names = dimensions if isinstance(dimensions, Sequence) and not isinstance(dimensions, str) else ()
    for index, group in enumerate(groups[:6], 1):
        if not isinstance(group, Mapping) or group.get('complete') is True:
            continue
        gaps = []
        if group.get('publisher_supported') is False:
            ids = group.get('ids')
            if isinstance(ids, list):
                for sid in ids[:8]:
                    status = statuses.get(sid) if isinstance(sid, str) else None
                    if isinstance(status, str) and status in _PUBLISHER_STATUS:
                        gaps.append(f'{_label(sid, "页面")[:12]}：{_PUBLISHER_STATUS[status]}')
            if not gaps:
                gaps.append('发布方归属或组内来源关系未证实')
        covered = group.get('covered_dimensions')
        if isinstance(covered, list):
            missing = [_label(names[i], f'第{i + 1}维') if i < len(names) else f'第{i + 1}维'
                       for i, valid in enumerate(covered) if valid is False]
            if missing:
                gaps.append('缺维度证据：' + '、'.join(missing))
        if gaps:
            details.append(f'来源组{index}：' + '；'.join(gaps))
    return details


def explain_failure(code, *, assessment=None, dimensions=(), artifacts=()):
    """Explain a recorded failure without changing its result or evidence status."""
    code = code if isinstance(code, str) else ''
    if code in _COMPARISON_CODES or code == 'agent_no_safe_next_action' and assessment is not None:
        lead = {
            'comparison_coverage_insufficient': '来源组未覆盖全部对比维度。',
            'comparison_independence_insufficient': '独立且有据的来源组不足。',
            'comparison_semantic_insufficient': '没有足够的相关维度证据。',
            'agent_no_safe_next_action': '现有证据仍不足，且已没有安全的下一步操作。',
        }[code]
        particulars = _assessment_details(assessment, dimensions)
        reason = lead + (('\n' + '\n'.join(particulars)) if particulars else '')
        next_step = '核对缺失页面的发布方归属与逐字维度引句；需要新资料时，另建获批准的检索或补查任务。'
    elif code in _REVIEW_FORMAT_CODES:
        reason = '模型的选择或复核响应不符合约定格式（例如候选编号、来源编号或引句字段无效），本次复核未被接受。'
        next_step = ('到历史来源选择中核验该未完成对比的已读捕获；受支持且无 Office 派发的任务可另建复核预览，'
                     '批准后使用原捕获，仍需重新验证结论。原失败不会改写；未知作用或回执不完整时不能重试。')
    elif code.startswith('comparison_capture_import_'):
        reason = '保存的补查捕获未通过文件、查询或来源绑定核验，不能用于本轮复核。'
        next_step = '保留原任务和捕获，检查导入记录的原文件与哈希；不要重复抓取或沿用旧结论。'
    elif code == 'model_usage_exceeds_request_bound':
        reason = '接口返回的用量超出本次请求的费用边界；已记录返回费用并停止后续模型调用。'
        next_step = '核对账本中的费用异常与接口用量，不能通过换账本、清空预留或重复调用恢复。'
    elif code == 'model_input_exceeds_context':
        reason = '本次输入和输出上限超过模型可用上下文；尚未发出付费请求。'
        next_step = '缩小本次来源范围或拆分任务，再重新预览。'
    elif code == 'model_budget_exceeded':
        reason = '共享模型预算不足，本轮模型调用已停止；不能根据历史余额推断当前可用额度。'
        next_step = '读取当前持久账本，待预算允许后再预览或执行剩余步骤。'
    elif code == 'agent_office_unknown':
        reason = 'Office 操作已经发出，但结果未获独立回执；不能假定成功或自动重放。'
        next_step = '人工核验原生文件、回执与字节；确认状态后另行决定后续操作。'
    elif code == 'agent_no_safe_next_action':
        reason = '当前没有经核验可安全执行的下一步；任务未完成。'
        next_step = '查看失败步骤及原始证据，再准备明确的新任务。'
    else:
        reason = '任务未完成；需查看原始失败记录。'
        next_step = '核对失败步骤和回执，再决定是否建立新任务。'
    count = len(artifacts) if isinstance(artifacts, (list, tuple)) else 0
    preserved = ('原任务记录与已采集证据保留；记录列出 '
                 f'{count} 份成果，仍须依据原回执核验。' if count else
                 '原任务记录与已采集证据保留；本次诊断未确认新的成果。')
    return f'原因：{reason}\n已保留：{preserved}\n建议：{next_step}'


def explain_outcome(outcome, *, assessment=None, dimensions=()):
    """Format a supplied outcome; never follow its run path or load evidence.

    The optional assessment must already have passed the caller's evidence
    verification. An outcome alone contains failure codes, not that assessment.
    """
    if not isinstance(outcome, Mapping):
        return '原因：任务结果记录不可用；不能确认成功。\n建议：核验原始任务记录。'
    if outcome.get('ok') is True:
        return ''
    if outcome.get('ok') is not False:
        return '原因：任务结果状态未知；不能确认成功。\n建议：核验原始任务记录与回执。'
    code = outcome.get('code')
    failures = outcome.get('failures')
    if code == 'agent_no_safe_next_action' and assessment is None and isinstance(failures, list):
        for failure in reversed(failures):
            if (isinstance(failure, Mapping) and isinstance(failure.get('code'), str)
                    and failure['code'] in _COMPARISON_CODES | _REVIEW_FORMAT_CODES):
                detail = explain_failure(failure['code'], artifacts=outcome.get('artifacts'))
                return detail.replace('原因：', '原因：当前已无安全的下一步；最近一次对比：', 1)
    return explain_failure(code, assessment=assessment, dimensions=dimensions,
                           artifacts=outcome.get('artifacts'))


def explain_recovery(row):
    """Explain the existing inspect() decision for a recovery candidate."""
    if not isinstance(row, Mapping):
        return '选择任务后核验来源和原生回执。'
    if row.get('eligible') is True:
        done = row.get('completed_edits')
        count = len(done) if isinstance(done, list) else 0
        return (f'状态：可生成恢复预览；已核验 {count} 项已完成步骤。\n'
                '已保留：原任务和已完成成果仍在原记录中。\n'
                '建议：预览并批准仅执行尚未开始的步骤；恢复不会改写历史失败。')
    code = row.get('code')
    code = code if isinstance(code, str) else ''
    reason = _RECOVERY_REASONS.get(code)
    if reason is None and isinstance(code, str) and ('changed' in code or 'invalid' in code):
        reason = '计划、来源、回执或成果与原记录不一致，不能安全续跑。'
    if reason is None:
        reason = '安全续跑核验未通过；需查看原始失败记录。'
    next_step = ('待原任务停止并完成清理后重新核验。' if code == 'run_owner_active' else
                 '核对原任务证据和回执；结果未知的操作须人工确认，不能自动重放。')
    return (f'原因：{reason}\n'
            '已保留：原任务记录和既有文件未因本次诊断更改；当前不视为已安全恢复。\n'
            f'建议：{next_step}')
