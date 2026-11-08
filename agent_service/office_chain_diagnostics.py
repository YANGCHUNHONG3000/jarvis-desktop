"""Bounded, read-only explanations for task-chain verification failures."""

_BUSY = frozenset({'office_chain_busy', 'workflow_busy'})
_CHANGED = frozenset({
    'office_chain_delivery_selection_invalid',
    'office_chain_source_changed', 'office_chain_request_changed',
    'office_chain_preview_changed', 'office_chain_task_instruction_changed',
    'office_chain_mail_original_changed', 'office_chain_mail_binding_changed',
    'office_chain_bundle_original_changed', 'office_chain_resume_changed',
    'office_chain_resume_binding_changed', 'office_chain_edit_plan_changed',
    'office_chain_edit_inputs_changed', 'office_chain_edit_source_changed',
    'office_chain_edit_context_changed', 'office_chain_edit_goal_changed',
    'office_chain_edit_planning_changed', 'office_chain_edit_refresh_changed',
    'office_handoff_reference_mismatch', 'office_handoff_reference_changed',
    'artifact_source_receipt_mismatch', 'artifact_source_reference_stale',
    'artifact_source_changed', 'edited_artifact_changed', 'edited_artifact_ref_changed',
    'artifact_hash_mismatch', 'receipt_mismatch', 'intent_mismatch',
    'manifest_mismatch', 'resume_parent_mismatch', 'source_changed', 'snapshot_mismatch',
    'mail_analysis_request_changed', 'mail_analysis_source_changed',
    'mail_analysis_plan_changed', 'request_changed',
    'draft_subject_mismatch', 'draft_recipient_mismatch', 'draft_body_mismatch',
    'draft_attachment_mismatch', 'draft_attachment_hash_mismatch', 'draft_attachment_changed',
})
_MISSING = frozenset({'draft_attachment_missing'})
_RECORD = frozenset({
    'office_chain_record_invalid', 'office_chain_oversize',
    'office_chain_mail_outputs_invalid', 'office_chain_resume_unverified',
    'workspace_record_invalid', 'workspace_payload_invalid', 'workspace_reference_invalid',
    'workspace_path_invalid', 'workspace_oversize',
    'resume_record_invalid', 'resume_manifest_invalid', 'resume_parent_invalid',
    'resume_workflow_invalid', 'resume_task_scope_invalid', 'resume_workflow_scope_invalid',
    'resume_workflow_ambiguous', 'resume_artifact_scope_invalid',
    'office_handoff_record_invalid', 'office_handoff_path_invalid', 'office_handoff_history_invalid',
    'record_invalid', 'record_oversize', 'receipt_invalid', 'artifact_invalid',
    'artifact_scope_invalid', 'artifact_type_invalid', 'observation_invalid',
    'artifact_source_digest_invalid', 'artifact_source_path_invalid',
    'artifact_source_reference_invalid', 'artifact_source_owner_invalid',
    'artifact_source_continuation_unverified', 'artifact_source_file_invalid',
    'invalid_office_package', 'office_file_missing_part', 'invalid_word_document', 'package_invalid',
    'draft_mime_invalid', 'draft_attachment_invalid', 'draft_attachment_scope_invalid',
    'draft_attachment_size_invalid', 'mail_analysis_review_invalid',
})

_REPORT_ONLY = '这里只报告核验问题，不会自动修复文件或重放任务。'
_MESSAGES = {
    'busy': '清单或关联任务正在被其他操作占用。请等待当前操作结束后点击“重新核验”。',
    'changed': ('来源、成果、附件或绑定未通过一致性核验，不能沿用旧证明。'
                '请核对原文件与记录；重新核验不保证修复。'),
    'missing': '核验所需文件缺失。请恢复原文件后点击“重新核验”；不能用新文件替代原件。',
    'access': '核验所需文件访问受阻或被占用。请检查权限并解除占用后点击“重新核验”。',
    'record': '清单或关联记录未通过完整性或格式核验，可能损坏或不完整。请保留现场并检查原始记录。',
    'unknown': '清单核验未通过，当前无法安全确认具体原因。请保留现场，核对原始文件与记录后再重新核验。',
}


def verification_failure(exc):
    """Explain at most eight cause nodes using exact codes/types, never exception text.

    A lock's explicit busy code takes precedence over its OS cause. Consistency
    rejections take precedence over retry suggestions; generic record wrappers
    yield to a concrete missing/access error. Implicit context is not inspected.
    """
    found, seen = set(), set()
    current = exc
    for _ in range(8):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        # Read BaseException's stored slots rather than invoking __str__ or an
        # exception subclass's replacement properties.
        args = BaseException.args.__get__(current)
        code = args[0] if len(args) == 1 and type(args[0]) is str and len(args[0]) <= 64 else None
        if code in _BUSY:
            found.add('busy')
        elif code in _CHANGED:
            found.add('changed')
        elif code in _MISSING:
            found.add('missing')
        elif code in _RECORD:
            found.add('record')
        if isinstance(current, FileNotFoundError):
            found.add('missing')
        elif isinstance(current, PermissionError):
            found.add('access')
        elif isinstance(current, OSError):
            try:
                winerror = getattr(current, 'winerror', None)
            except Exception:
                winerror = None
            if type(winerror) is int and winerror in (32, 33):
                found.add('access')
        current = BaseException.__cause__.__get__(current)
    category = next((name for name in ('busy', 'changed', 'missing', 'access', 'record')
                     if name in found), 'unknown')
    return _MESSAGES[category] + _REPORT_ONLY
