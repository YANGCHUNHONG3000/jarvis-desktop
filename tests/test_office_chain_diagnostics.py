"""Cause-aware diagnosis is fixed text, bounded, and has no recovery effects."""
import errno
from pathlib import Path

import pytest

from agent_service.office_chain_diagnostics import verification_failure


SECRET = r'C:\private\客户邮件-秘密.docx token=secret-123'


def caused(outer, inner):
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize(('error', 'expected'), [
    (FileNotFoundError(errno.ENOENT, SECRET), '文件缺失'),
    (PermissionError(errno.EACCES, SECRET), '访问受阻或被占用'),
    (ValueError('draft_attachment_missing'), '文件缺失'),
    (ValueError('artifact_hash_mismatch'), '不能沿用旧证明'),
    (ValueError('draft_attachment_mismatch'), '不能沿用旧证明'),
    (ValueError('office_chain_mail_binding_changed'), '不能沿用旧证明'),
    (ValueError('mail_analysis_source_changed'), '不能沿用旧证明'),
    (ValueError('office_handoff_reference_changed'), '不能沿用旧证明'),
    (ValueError('office_chain_record_invalid'), '可能损坏或不完整'),
    (ValueError('resume_record_invalid'), '可能损坏或不完整'),
    (ValueError('office_file_missing_part'), '可能损坏或不完整'),
    (ValueError('workflow_busy'), '正在被其他操作占用'),
    (ValueError('office_chain_busy'), '正在被其他操作占用'),
])
def test_only_known_types_and_exact_codes_select_fixed_guidance(error, expected):
    message = verification_failure(error)
    assert expected in message
    assert '不会自动修复文件或重放任务' in message
    assert SECRET not in message
    if expected == '不能沿用旧证明':
        assert '重新核验不保证修复' in message
        assert '哈希' not in message


@pytest.mark.parametrize('winerror', [32, 33])
def test_windows_sharing_conflicts_are_access_failures(winerror):
    error = OSError(SECRET); error.winerror = winerror
    assert '访问受阻或被占用' in verification_failure(error)


@pytest.mark.parametrize('winerror', [5, 2, True, '32', None])
def test_other_os_metadata_does_not_guess_a_category(winerror):
    error = OSError(SECRET); error.winerror = winerror
    assert '无法安全确认具体原因' in verification_failure(error)


@pytest.mark.parametrize('error_type', [FileNotFoundError, PermissionError])
def test_real_workspace_artifact_resume_wrappers_preserve_os_cause(tmp_path, monkeypatch, error_type):
    from agent_service import office_task_chain as chain, workspace_state as workspace
    base = tmp_path / 'tasks'; task = base / ('task-' + '1' * 32)
    artifact = task / ('workflow-' + '2' * 32) / 's1' / 'document.docx'
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'offline-placeholder')
    parent = task / 'result.json'; parent.write_bytes(b'{}')
    read_bytes = Path.read_bytes

    def unavailable(path):
        if path == parent:
            raise error_type(SECRET)
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', unavailable)
    payload = {'draft': '', 'records': [{'path': str(artifact), 'sha256': 'a' * 64}], 'source': None}
    with pytest.raises(chain.OfficeTaskChainError) as caught:
        chain._public(lambda: workspace.verify(payload, base))()
    assert caught.value.args == ('office_chain_record_invalid',)
    message = verification_failure(caught.value)
    assert ('文件缺失' if error_type is FileNotFoundError else '访问受阻或被占用') in message
    assert SECRET not in message and str(tmp_path) not in message


@pytest.mark.parametrize(('codes', 'expected'), [
    (['office_chain_record_invalid', 'workspace_reference_invalid', PermissionError(SECRET)], '访问受阻或被占用'),
    (['resume_record_invalid', FileNotFoundError(SECRET)], '文件缺失'),
    (['office_chain_busy', PermissionError(SECRET)], '正在被其他操作占用'),
    (['office_chain_record_invalid', 'workflow_busy', PermissionError(SECRET)], '正在被其他操作占用'),
    (['artifact_hash_mismatch', FileNotFoundError(SECRET)], '不能沿用旧证明'),
    (['draft_attachment_changed', PermissionError(SECRET)], '不能沿用旧证明'),
    (['office_chain_busy', 'artifact_hash_mismatch'], '正在被其他操作占用'),
    ([PermissionError(SECRET), FileNotFoundError(SECRET)], '文件缺失'),
])
def test_specific_rejections_and_os_evidence_override_generic_wrappers(codes, expected):
    errors = [ValueError(value) if type(value) is str else value for value in codes]
    for outer, inner in zip(errors, errors[1:]):
        caused(outer, inner)
    assert expected in verification_failure(errors[0])


@pytest.mark.parametrize('error', [
    ValueError(SECRET), ValueError('artifact_hash_mismatch ' + SECRET),
    ValueError('artifact_hash_mismatch', SECRET),
    ValueError('new_source_changed'), RuntimeError(SECRET * 5000), None, SECRET,
])
def test_unknowns_never_echo_sensitive_text_or_infer_code_prefixes(error):
    message = verification_failure(error)
    assert '无法安全确认具体原因' in message
    assert SECRET not in message and 'secret-123' not in message
    assert 'artifact_hash_mismatch' not in message


def test_implicit_context_is_not_a_diagnosis_source():
    error = ValueError(SECRET); error.__context__ = FileNotFoundError(SECRET)
    assert '无法安全确认具体原因' in verification_failure(error)


def test_cause_cycle_is_bounded_and_preserves_known_diagnosis():
    outer = ValueError(SECRET); inner = PermissionError(SECRET)
    caused(outer, inner); caused(inner, outer)
    assert '访问受阻或被占用' in verification_failure(outer)
    caused(outer, outer)
    assert '无法安全确认具体原因' in verification_failure(outer)


@pytest.mark.parametrize(('nodes', 'expected'), [(8, '文件缺失'), (9, '无法安全确认具体原因')])
def test_at_most_eight_cause_nodes_are_examined(nodes, expected):
    error = FileNotFoundError(SECRET)
    for _ in range(nodes - 1):
        error = caused(ValueError(SECRET), error)
    assert expected in verification_failure(error)


def test_does_not_call_exception_string_or_subclass_slot_properties():
    class Hostile(ValueError):
        def __str__(self):
            raise AssertionError('exception text must not be evaluated')

        @property
        def args(self):
            raise AssertionError('use the stored BaseException args')

        @property
        def __cause__(self):
            raise AssertionError('use the stored BaseException cause')

    assert '不能沿用旧证明' in verification_failure(Hostile('artifact_hash_mismatch'))


def test_broken_custom_os_metadata_is_conservative():
    class HostileOS(OSError):
        @property
        def winerror(self):
            raise RuntimeError(SECRET)

    assert '无法安全确认具体原因' in verification_failure(HostileOS(SECRET))
