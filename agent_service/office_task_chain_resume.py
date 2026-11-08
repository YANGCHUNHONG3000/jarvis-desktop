"""Read-only attribution of a fixed mail task's existing Office continuation."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from . import capability_router, mail_understanding, office_resume, workspace_state
from . import office_task_chain as chain
from .computer import digest


def _record(path):
    return workspace_state._read(path, 1_000_000)


def _original(manifest, run, plan, original):
    """Rebuild the failed capability receipt from its immutable original prefix."""
    base = Path(manifest['task_base'])
    task = base / ('task-' + plan['request_id'])
    chain._require(run.stem == plan['sha256'] and run.parent.parent.parent == Path(manifest['computer_base']))
    claim, _ = _record(run / 'claim.json')
    chain._require(claim == {'approval': plan['sha256'], 'task_root': str(task)})
    capability_router._mail_plan(plan, base)
    parent, _ = _record(task / 'result.json')
    chain._require(parent.get('ok') is False and parent.get('state') in {'cancelled', 'timed_out', 'failed'})
    office_resume._assert_plain_office(task)
    snapshot = office_resume._snapshot(task, base)
    records = [{'path': a['path'], 'sha256': a['sha256']} for a in parent['artifacts']]
    expected = {'ok': False, 'state': parent['state'],
                'code': parent.get('error', parent.get('reason', 'capability_not_completed')),
                'task_root': str(task), 'artifacts': records, 'skill': parent.get('skill'), 'gap': None,
                'result_sha256': digest(parent), 'run': str(run), 'approval': plan['sha256']}
    chain._require(original == expected, 'office_chain_mail_original_changed')
    capability_router._bind_mail_result(plan, task, original, base)
    binding = mail_understanding.verify_task(task, base, snapshot['steps'], parent)
    return task, snapshot, binding


def _success(manifest, run, plan, original_raw, task, snapshot, binding, attempt, pinned):
    chain._require(type(attempt) is str and workspace_state._ID.fullmatch(attempt) is not None,
                   'office_chain_resume_attempt_invalid')
    result = office_resume.read_resume_result(task, Path(manifest['task_base']), attempt)
    chain._require(result.get('ok') is True and result.get('state') == 'succeeded'
                   and result.get('attempt_id') == attempt and result.get('task_root') == str(task)
                   and result.get('workflow_root') == str(snapshot['workflow_root'])
                   and result.get('remaining') == 0 and result.get('completed') == len(snapshot['steps'])
                   and result.get('original_sha256') == snapshot['original_sha256']
                   and result.get('manifest_sha256') == snapshot['manifest_sha256']
                   and result.get('token_after') == snapshot['token'], 'office_chain_resume_unverified')
    outcome, raw = _record(task / ('resume-' + attempt) / 'outcome.json')
    chain._require(outcome == result, 'office_chain_resume_changed')
    proof = {'attempt_id': attempt, 'task_root': str(task), 'workflow_root': str(snapshot['workflow_root']),
             'original_sha256': snapshot['original_sha256'], 'manifest_sha256': snapshot['manifest_sha256'],
             'token_before': result['token_before'], 'token_after': result['token_after'],
             'outcome_sha256': sha256(raw).hexdigest(), 'parent_outcome_sha256': sha256(original_raw).hexdigest(),
             'mail_binding_sha256': binding}
    if pinned is not None:
        chain._require(pinned == proof, 'office_chain_resume_binding_changed')
    records = [{'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
    chain._require(len(records) == len(snapshot['steps']) and 1 <= len(records) <= 4)
    normalized = {'ok': True, 'state': 'succeeded', 'code': 'mail_office_resumed',
                  'task_root': str(task), 'run': str(run), 'approval': plan['sha256'],
                  'artifacts': records, 'mail_resume': proof}
    return 'waiting_result', '邮件办公续跑已全部核验，可接续下一步骤。', normalized


def inspect(manifest, run, plan, original, original_raw, *, state, completed=None, attempt=None):
    """Return None for non-Office failures; never executes or rewrites a result."""
    task = Path(manifest['task_base']) / ('task-' + plan['request_id'])
    pinned = completed.get('mail_resume') if type(completed) is dict else None
    parent_path = task / 'result.json'
    workspace_state._safe_components(parent_path)
    if not parent_path.exists():
        chain._require(pinned is None and attempt is None, 'office_chain_resume_unverified')
        return None
    parent, _ = _record(parent_path)
    if parent.get('ok') is not False or not parent.get('workflow_root') or 'mail_understanding' not in parent:
        chain._require(pinned is None and attempt is None, 'office_chain_resume_unverified')
        return None
    task, snapshot, binding = _original(manifest, run, plan, original)
    state.update(task_root=str(task), resume_original_state=parent['state'])
    if pinned is not None:
        chain._require(type(pinned) is dict and type(pinned.get('attempt_id')) is str,
                       'office_chain_resume_binding_changed')
        return _success(manifest, run, plan, original_raw, task, snapshot, binding, pinned['attempt_id'], pinned)
    if attempt is not None:
        return _success(manifest, run, plan, original_raw, task, snapshot, binding, attempt, None)
    pointer = task / 'resume-latest.json'
    workspace_state._safe_components(pointer)
    latest = office_resume.latest_continuation(task, Path(manifest['task_base']))
    if latest is not None and latest.get('ok') is True:
        return _success(manifest, run, plan, original_raw, task, snapshot, binding, latest['attempt_id'], None)
    if pointer.exists() and latest is None:
        return 'blocked', '恢复指针或回执未通过核验，原失败保留；不会自动重试。', deepcopy(original)
    preview = office_resume.preview_resume(task, Path(manifest['task_base']))
    if preview.get('eligible') is True and preview.get('remaining', 0) > 0:
        # Dispatch metadata belongs to the chain view, not the persisted Office
        # recovery contract (whose public preview deliberately has no kind).
        state['resume_preview'] = {**deepcopy(preview), 'kind': 'office_resume'}
        return 'resume_available', '已核验完成部分；请明确批准，仅继续剩余办公步骤。', deepcopy(original)
    return 'blocked', '当前没有可证明安全的剩余步骤，原失败和恢复记录保留。', deepcopy(original)
