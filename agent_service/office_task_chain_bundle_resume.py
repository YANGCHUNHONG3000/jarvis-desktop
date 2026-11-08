"""Read-only attribution of a bound joint report's dedicated continuation."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os

from . import office_bundle_resume as recovery, office_resume, workspace_state
from . import office_task_chain as chain
from .computer import digest


def _record(path):
    return workspace_state._read(path, 1_000_000)


def _original(manifest, run, plan, original):
    base = Path(manifest['task_base'])
    task = base / ('task-' + plan['request_id'])
    chain._require(run.suffix == '.bundle' and run.stem == plan['sha256']
                   and run.parent.parent.parent == Path(manifest['computer_base']))
    saved, _ = _record(run.parent / (run.stem + '.json'))
    chain._require(saved == plan, 'office_chain_preview_changed')
    request = task / 'request.txt'
    workspace_state._safe_components(request)
    chain._require(request.is_file() and request.stat().st_size <= chain._LIMIT,
                   'office_chain_task_instruction_changed')
    with request.open('rb') as stream:
        authored = stream.read(chain._LIMIT + 1)
    chain._require(authored == plan['instruction'].replace('\n', os.linesep).encode('utf-8'),
                   'office_chain_task_instruction_changed')
    # The owner guard binds comparison, inputs, decision, claim, plan, original
    # native result and every known attempt; plain Office snapshots are insufficient.
    snapshot = office_resume._owned_snapshot(task, base, recovery._guard)
    owner = recovery._guard(task)
    parent, _ = _record(task / 'result.json')
    expected = {'ok': False, 'state': parent['state'], 'code': 'bundle_office_incomplete',
                'artifacts': [], 'run': str(run)}
    chain._require(original == expected, 'office_chain_bundle_original_changed')
    report, _ = _record(task / 'comparison.json')
    return task, snapshot, digest(owner), sha256(authored).hexdigest(), report


def _success(manifest, run, plan, original_raw, task, snapshot, owner, request_hash,
             report, attempt, pinned):
    chain._require(type(attempt) is str and workspace_state._ID.fullmatch(attempt) is not None,
                   'office_chain_resume_attempt_invalid')
    result = recovery.read_resume_result(task, Path(manifest['task_base']), attempt)
    chain._require(result.get('kind') == 'bundle_resume' and result.get('ok') is True
                   and result.get('state') == 'succeeded' and result.get('attempt_id') == attempt
                   and result.get('task_root') == str(task)
                   and result.get('workflow_root') == str(snapshot['workflow_root'])
                   and result.get('remaining') == 0 and result.get('completed') == len(snapshot['steps'])
                   and result.get('original_sha256') == snapshot['original_sha256']
                   and result.get('manifest_sha256') == snapshot['manifest_sha256']
                   and result.get('token_after') == snapshot['token'], 'office_chain_resume_unverified')
    outcome, raw = _record(task / ('resume-' + attempt) / 'outcome.json')
    # kind is dispatch metadata supplied by the dedicated reader, never on disk.
    chain._require(outcome == {k: v for k, v in result.items() if k != 'kind'},
                   'office_chain_resume_changed')
    proof = {'attempt_id': attempt, 'task_root': str(task), 'workflow_root': str(snapshot['workflow_root']),
             'original_sha256': snapshot['original_sha256'], 'manifest_sha256': snapshot['manifest_sha256'],
             'token_before': result['token_before'], 'token_after': result['token_after'],
             'outcome_sha256': sha256(raw).hexdigest(), 'parent_outcome_sha256': sha256(original_raw).hexdigest(),
             'bundle_owner_sha256': owner, 'request_sha256': request_hash}
    if pinned is not None:
        chain._require(pinned == proof, 'office_chain_resume_binding_changed')
    records = [{'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
    chain._require(len(records) == len(snapshot['steps']) == 2)
    normalized = {'ok': True, 'state': 'succeeded', 'code': 'bundle_office_resumed',
                  'task_root': str(task), 'run': str(run), 'approval': plan['sha256'],
                  'positions': report['positions'], 'differences': len(report['differences']),
                  'artifacts': records, 'bundle_resume': proof}
    return 'waiting_result', '联合核对续跑已全部核验，可接续下一步骤。', normalized


def inspect(manifest, run, plan, original, original_raw, *, state, completed=None, attempt=None):
    """Read one exact run; never dispatch, scan other tasks or rewrite failure."""
    task = Path(manifest['task_base']) / ('task-' + plan['request_id'])
    pinned = completed.get('bundle_resume') if type(completed) is dict else None
    parent_path = task / 'result.json'
    workspace_state._safe_components(parent_path)
    if not parent_path.exists():
        chain._require(pinned is None and attempt is None, 'office_chain_resume_unverified')
        return None
    task, snapshot, owner, request_hash, report = _original(manifest, run, plan, original)
    state.update(task_root=str(task), resume_original_state=snapshot['original_state'])
    def success(ident, fixed=None):
        return _success(manifest, run, plan, original_raw, task, snapshot, owner,
                        request_hash, report, ident, fixed)
    if pinned is not None:
        chain._require(type(pinned) is dict and type(pinned.get('attempt_id')) is str,
                       'office_chain_resume_binding_changed')
        return success(pinned['attempt_id'], pinned)
    if attempt is not None:
        return success(attempt)
    pointer = task / 'resume-latest.json'
    workspace_state._safe_components(pointer)
    latest = recovery.latest_continuation(task, Path(manifest['task_base']))
    if latest is not None and latest.get('ok') is True:
        return success(latest['attempt_id'])
    if pointer.exists() and latest is None:
        return 'blocked', '恢复指针或回执未通过核验，原失败保留；不会自动重试。', deepcopy(original)
    preview = recovery.preview_resume(task, Path(manifest['task_base']))
    if preview.get('eligible') is True and preview.get('remaining', 0) > 0:
        state['resume_preview'] = deepcopy(preview)
        return 'resume_available', '已核验完成部分；请明确批准，仅继续剩余联合核对步骤。', deepcopy(original)
    return 'blocked', '当前没有可证明安全的剩余步骤，原失败和恢复记录保留。', deepcopy(original)
