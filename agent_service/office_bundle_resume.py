"""Zero-model continuation of a verified, explicitly interrupted joint report."""
from functools import partial
from hashlib import sha256
from pathlib import Path

from . import office_resume as core, office_workflow as workflow
from .computer import read_record


def _hash_file(path):
    return sha256(core._small_bytes(path, workflow._MAX_JSON)).hexdigest()


def _guard(root, *, active_attempt=None):
    """Bind immutable origin and reject orphan/unknown attempts before dispatch."""
    if (root / 'skill-request.json').exists():
        raise core.ResumeError('resume_skill_owned')
    parent, parent_hash = core._parent(root)
    workspace, steps, manifest_hash = core._manifest(root, parent)
    from .office_bundle import verify_task
    verify_task(root, root.parent, steps)
    request = read_record(root / 'bundle-request.json')
    run = Path(request['run'])
    origin = read_record(run / 'outcome.json')
    original = read_record(workspace / 'result.json')
    state = parent['state']
    if state == 'succeeded':
        raise core.ResumeError('resume_already_complete')
    if (state not in {'cancelled', 'timed_out', 'failed'}
            or origin != {'ok': False, 'state': state, 'code': 'bundle_office_incomplete',
                          'artifacts': [], 'run': str(run)}
            or original.get('state') != state or original.get('ok') is not False
            or original.get('root') != str(workspace)
            or original.get('completed') != parent['completed']
            or original.get('error') != parent.get('error')):
        raise core.ResumeError('bundle_resume_origin_invalid')
    if state == 'failed' and (parent.get('error') not in core._PREFLIGHT_BLOCKS
            or len(parent['completed']) >= len(steps)
            or original.get('step') != steps[len(parent['completed'])]['id']):
        raise core.ResumeError('bundle_resume_origin_invalid')
    receipts, unknown = workflow._resume_preflight(steps, workspace)
    if unknown is not None:
        raise core.ResumeError('resume_unknown_effect')
    core._original_prefix(parent, steps, receipts)
    prefix = dict(list(receipts.items())[:len(parent['completed'])])
    if original.get('receipts') != prefix:
        raise core.ResumeError('bundle_resume_origin_invalid')
    # verify_task, invoked by the shared snapshot, enforces managed run/approval scope.
    paths = [root/'bundle-request.json', root/'inputs.json', root/'comparison.json',
             run/'claim.json', run/'decision.json', run/'outcome.json',
             run.parent/(request['approval']+'.json'), workspace/'result.json']
    proof = [_hash_file(p) for p in paths]
    hashes = [[key, _hash_file(workspace/key/'receipt.json'), receipt['sha256']]
              for key, receipt in receipts.items()]

    def snapshot(count):
        plain = workflow._hash({'original': parent_hash, 'manifest': manifest_hash,
                                'receipts': hashes[:count]})
        return {'root': root, 'workflow_root': workspace, 'steps': steps,
                'receipts': dict(list(receipts.items())[:count]),
                'original_sha256': parent_hash, 'manifest_sha256': manifest_hash,
                'token': workflow._hash({'snapshot': plain, 'owner': proof})}

    attempts = sorted(root.glob('resume-*'))
    attempts = [p for p in attempts if p.name != 'resume-latest.json']
    if len(attempts) > 32 or (active_attempt and len(attempts) >= 32
                            and not (root/('resume-'+active_attempt)).exists()):
        raise core.ResumeError('bundle_resume_limit')
    for attempt in attempts:
        name = attempt.name.removeprefix('resume-')
        if not core._ATTEMPT.fullmatch(name) or core._linked(attempt) or not attempt.is_dir():
            raise core.ResumeError('bundle_resume_attempt_invalid')
        if name == active_attempt and not (attempt/'outcome.json').exists():
            continue  # The engine owns this exact new attempt under its task lock.
        try:
            req = workflow._read_json(attempt/'request.json')
            out = workflow._read_json(attempt/'outcome.json')
            result = workflow._read_json(attempt/'result.json')
            count = out.get('completed')
            if (type(count) is not int or not len(prefix) <= count <= len(receipts)
                    or not core._durable_progress(out)
                    or not any(req == {'version': 1, 'attempt_id': name, 'task_root': str(root),
                                       'expected_token': snapshot(n)['token']}
                               for n in range(len(prefix), count+1))):
                raise ValueError('invalid')
            snap = snapshot(count)
            expected = core._outcome(snap, name, req['expected_token'], out['state'],
                                     error=out.get('error'))
            blocked = out['state'] == 'blocked'
            if (out != expected or result.get('ok') is not out['ok']
                    or result.get('state') != ('failed' if blocked else out['state'])
                    or result.get('root') != str(workspace)
                    or result.get('completed') != list(snap['receipts'])
                    or result.get('receipts') != snap['receipts']
                    or (blocked and (count >= len(steps) or result.get('error') != out['error']
                                     or result.get('step') != steps[count]['id']))):
                raise ValueError('invalid')
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise core.ResumeError('bundle_resume_attempt_unknown') from exc
    return proof


def preview_resume(task_root, base):
    result = core._preview_resume(task_root, base, owner_guard=_guard)
    if result['eligible']:
        root = core._task(task_root, base)
        if len([p for p in root.glob('resume-*') if p.name != 'resume-latest.json']) >= 32:
            result.update(eligible=False, reason='bundle_resume_limit')
    return {**result, 'kind': 'bundle_resume'}


def resume_task(task_root, base, expected_token, attempt_id, *, execute=None, deadline=None):
    guard = partial(_guard, active_attempt=attempt_id)
    # A merely stale UI preview must not leave an orphaned new attempt.
    current = core._owned_snapshot(task_root, base, guard)
    if current['token'] != expected_token:
        raise core.ResumeError('resume_preview_stale')
    result = core._resume_task(task_root, base, expected_token, attempt_id,
                              owner_guard=guard,
                              execute=execute, deadline=deadline)
    return {**result, 'kind': 'bundle_resume'}


def read_resume_result(task_root, base, attempt_id):
    result = core._read_resume_result(task_root, base, attempt_id, owner_guard=_guard)
    return {**result, 'kind': 'bundle_resume'}


def latest_continuation(task_root, base):
    result = core._latest_continuation(task_root, base, owner_guard=_guard)
    return {**result, 'kind': 'bundle_resume'} if result is not None else None
