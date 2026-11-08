"""Approve a bounded native readback of an existing, preserved failed copy.

Original edit runs remain unknown. This module never creates or edits an Office
copy, replays an intent, or changes adapter qualification or quota records.
"""
from pathlib import Path
import re
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new
from .computer_goal_adapters import TASK_BASE
from .computer_agent_tools import _load
from . import office_edit as edit


def _fail(code):
    raise ComputerError('edit_recheck_' + code)


def _files(root, *, output=False):
    files = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if output and (relative.parts[0] == 'native-rechecks' or relative.as_posix() in {
                'native-edit-receipt.json', 'native-recheck.json'}):
            continue
        safe_root(path)
        if path.is_file():
            if len(files) >= 500:
                _fail('evidence_limit')
            files[relative.as_posix()] = edit._hash(path)
    return files


def _original(artifact, expected_sha256, base):
    """Rebuild the old failure binding; new recheck files are never old facts."""
    from .run_lifecycle import assert_inactive
    from .computer_interaction import validate_goal, verify_location
    artifact, base = safe_root(artifact), safe_root(base)
    output, task = artifact.parent, artifact.parent.parent
    if (output.name != 'edit' or task.parent != base
            or not re.fullmatch(r'task-[0-9a-f]{32}', task.name)
            or artifact.name not in {'edited-document.docx', 'edited-spreadsheet.xlsx'}
            or type(expected_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or edit._hash(artifact) != expected_sha256):
        _fail('artifact_invalid')
    manifest, _ = _load(output / 'edit-manifest.json')
    run, step = safe_root(manifest['run']), safe_root(manifest['step'])
    if (step.parent != run or not re.fullmatch(r'step-(0[1-9]|1[0-2])', step.name)
            or run.parent.name != 'goals' or not re.fullmatch(r'[0-9a-f]{64}\.run', run.name)):
        _fail('owner_invalid')
    owner = assert_inactive(run)
    plan = read_record(run.with_suffix('.json'))
    if (plan.get('sha256') != run.stem or digest({k: v for k, v in plan.items() if k != 'sha256'}) != run.stem
            or plan.get('mode') != 'agent'):
        _fail('plan_invalid')
    claim = read_record(run / 'claim.json')
    if claim.get('approval') != plan['sha256'] or claim.get('mode') != 'agent':
        _fail('approval_invalid')
    goal, _ = _load(run / 'environment-goal.json')
    if goal != plan['goal'] or goal.get('kind') != 'interactive':
        _fail('goal_invalid')
    validate_goal(goal)
    record, _ = _load(step / 'environment-result.json')
    intent, intent_hash = _load(step / 'environment-intent.json')
    action = record['action']
    wanted = {'ok': False, 'state': 'unknown', 'code': 'edit_application_mismatch'}
    if (set(action) != {'kind', 'edit_id'} or action['kind'] != 'office_edit'
            or record['intent_sha256'] != intent_hash or action != intent['action']
            or read_record(step / 'intent.json').get('action') != action
            or record['result'] != wanted or read_record(step / 'verification.json') != wanted):
        _fail('failure_invalid')
    outcome = read_record(run / 'outcome.json')
    number = int(step.name[5:])
    attempt = {'step': number, 'action': action, 'result': wanted}
    if (outcome.get('ok') is not False or outcome.get('state') != 'unknown'
            or outcome.get('engine') != 'observed_agent' or outcome.get('code') != 'agent_office_unknown'
            or outcome.get('recovery') != plan.get('recovery') or outcome.get('run') != str(run)
            or outcome.get('attempts', []).count(attempt) != 1):
        _fail('outcome_invalid')
    items = [item for item in goal['edits'] if item['id'] == action['edit_id']]
    if len(items) != 1:
        _fail('spec_invalid')
    item = items[0]
    source_root = safe_root(manifest['source_root'])
    if (item['source_root'] != str(source_root) or item['spec'] != manifest['spec']
            or source_root.parent != run.parent.parent / 'edit-inputs'):
        _fail('source_invalid')
    verify_location(item)
    spec, source = edit._bind(item['spec'], source_root)
    old = record['native']
    if (old.get('ok') is not False or old.get('state') != 'unknown'
            or old.get('stage') != 'native_readback' or old.get('code') != 'edit_application_mismatch'
            or old.get('file_verified') is not True or old.get('preservation_verified') is not True
            or old.get('application_verified') is not False or old.get('artifact') != str(artifact)
            or old.get('sha256') != expected_sha256 or old.get('source_sha256') != source['sha256']
            or read_record(output / 'worker-outcome.json') != old
            or read_record(output / 'worker-request.json') != {'spec': spec, 'source_root': str(source_root)}):
        _fail('native_failure_invalid')
    expected_name = 'edited-document.docx' if spec['kind'] == 'document' else 'edited-spreadsheet.xlsx'
    if artifact.name != expected_name:
        _fail('artifact_invalid')
    source_path = source_root / ('source.docx' if spec['kind'] == 'document' else 'source.xlsx')
    observed = edit.verify_preservation(source_path, artifact, spec, source.get('observation_version', 1))
    value = {'schema': 1, 'artifact': str(artifact), 'expected_sha256': expected_sha256,
        'base': str(base), 'source_root': str(source_root), 'spec': spec, 'observed': observed,
        'source': source, 'run': str(run), 'step': str(step), 'owner_sha256': digest(owner),
        'plan_sha256': edit._hash(run.with_suffix('.json')), 'run_files': _files(run),
        'source_files': _files(source_root), 'old_output_files': _files(output, output=True)}
    assert_inactive(run)
    return dict(value, sha256=digest(value))


def inspect(artifact, expected_sha256, base=TASK_BASE):
    """Return a reviewable binding; no application or worker is started."""
    preview = _original(artifact, expected_sha256, base)
    output = Path(preview['artifact']).parent
    if (output / 'native-recheck.json').exists():
        verify(artifact, expected_sha256, base=base)
        _fail('already_verified')
    if (output / 'native-edit-receipt.json').exists():
        _fail('receipt_conflict')
    return preview


def _same(preview):
    if (_original(preview['artifact'], preview['expected_sha256'], preview['base']) != preview):
        _fail('binding_changed')


def _native(preview, native):
    _same(preview)
    result = edit.verify_edit_result(native, preview['source_root'], preview['spec'])
    if result['artifact'] != preview['artifact'] or result['sha256'] != preview['expected_sha256']:
        _fail('artifact_changed')
    return result


def _worker_readback(payload, root, stop):
    """Only the supervised child calls this; it never calls a copy writer."""
    preview = payload['preview']
    root = safe_root(root)
    if (root.parent != Path(preview['artifact']).parent / 'native-rechecks'
            or not re.fullmatch(r'[0-9a-f]{32}', root.name)
            or read_record(root / 'request.json') != payload):
        _fail('worker_owner_invalid')
    if payload != {'preview': preview, 'approval': preview['sha256']}:
        _fail('approval_invalid')
    if inspect(preview['artifact'], preview['expected_sha256'], preview['base']) != preview:
        _fail('binding_changed')
    def check():
        if stop():
            _fail('cancelled')
    check()
    app = edit._native_readback(Path(preview['artifact']), preview['spec'], preview['observed'], check)
    # Cancellation after a complete successful read cannot erase its evidence.
    _same(preview)
    artifact = Path(preview['artifact'])
    receipt = {'spec': preview['spec'], 'source_sha256': preview['source']['sha256'],
        'artifact': str(artifact), 'sha256': preview['expected_sha256'],
        'app': app, 'observed': preview['observed']}
    receipt_path = artifact.parent / 'native-edit-receipt.json'
    write_new(receipt_path, receipt)
    native = {'ok': True, 'state': 'succeeded', 'artifact': str(artifact), 'path': str(artifact),
        'sha256': preview['expected_sha256'], 'source_sha256': preview['source']['sha256'],
        'file_verified': True, 'preservation_verified': True, 'application_verified': True,
        'original_unchanged': True, 'app': app, 'observed': preview['observed'],
        'native_receipt': str(receipt_path), 'native_receipt_sha256': edit._hash(receipt_path)}
    return _native(preview, native)


def _proof(artifact, expected_sha256, base, pointer):
    output = safe_root(artifact).parent
    if (type(pointer) is not dict or set(pointer) != {'schema', 'recheck', 'proof_sha256'}
            or type(pointer['schema']) is not int or pointer['schema'] != 1):
        _fail('pointer_invalid')
    root = safe_root(pointer['recheck'])
    if root.parent != output / 'native-rechecks' or not re.fullmatch(r'[0-9a-f]{32}', root.name):
        _fail('pointer_invalid')
    proof = read_record(root / 'proof.json')
    if (type(proof) is not dict or set(proof) != {'schema', 'preview', 'native', 'worker_files'}
            or proof['schema'] != 1 or digest(proof) != pointer['proof_sha256']):
        _fail('proof_invalid')
    preview = _original(artifact, expected_sha256, base)
    if proof['preview'] != preview:
        _fail('binding_changed')
    request = {'preview': preview, 'approval': preview['sha256']}
    if (read_record(root / 'request.json') != request or read_record(root / 'worker-request.json') != request
            or read_record(root / 'worker-outcome.json') != proof['native']
            or read_record(root / 'worker-ready.json') != {'ready': True}
            or read_record(root / 'worker-gated.json') != {'gated': True}
            or read_record(root / 'supervision.json') != {'exit_code': 0, 'read_only': True}
            or proof['worker_files'] != {name: edit._hash(root / name) for name in (
                'request.json', 'worker-request.json', 'worker-outcome.json', 'worker-ready.json',
                'worker-gated.json', 'supervision.json')}):
        _fail('worker_invalid')
    native = _native(preview, proof['native'])
    return {'native': native, 'proof_sha256': pointer['proof_sha256']}


def verify(artifact, expected_sha256, *, base=TASK_BASE):
    """Verify old failure, new readback and all bound bytes; never start Office."""
    pointer = read_record(safe_root(artifact).parent / 'native-recheck.json')
    result = _proof(artifact, expected_sha256, base, pointer)
    if (safe_root(pointer['recheck']) / 'failure.json').exists():
        _fail('result_invalid')
    if read_record(safe_root(pointer['recheck']) / 'result.json') != {'ok': True, 'state': 'verified', **result}:
        _fail('result_invalid')
    return result


def execute(artifact, expected_sha256, *, approval, base=TASK_BASE, stop=lambda: False):
    preview = inspect(artifact, expected_sha256, base)
    if approval != preview['sha256']:
        _fail('approval_invalid')
    output = Path(preview['artifact']).parent
    root = safe_root(output / 'native-rechecks' / uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    payload = {'preview': preview, 'approval': approval}
    write_new(root / 'request.json', payload)
    try:
        from .office_edit_host import execute_recheck_supervised
        native = execute_recheck_supervised(payload, root, stop=stop)
        if native.get('ok') is not True:
            result = {'ok': False, 'state': native.get('state', 'failed'),
                'code': native.get('code', 'edit_recheck_failed'), 'recheck': str(root)}
            write_new(root / 'result.json', result)
            return result
        native = _native(preview, native)
        names = ('request.json', 'worker-request.json', 'worker-outcome.json', 'worker-ready.json',
                 'worker-gated.json', 'supervision.json')
        proof = {'schema': 1, 'preview': preview, 'native': native,
                 'worker_files': {name: edit._hash(root / name) for name in names}}
        write_new(root / 'proof.json', proof)
        pointer = {'schema': 1, 'recheck': str(root), 'proof_sha256': digest(proof)}
        verified = _proof(artifact, expected_sha256, base, pointer)
        write_new(root / 'result.json', {'ok': True, 'state': 'verified', **verified})
        write_new(output / 'native-recheck.json', pointer)
        return {'ok': True, 'state': 'verified', 'recheck': str(root), **verified}
    except Exception as exc:
        failure = {'ok': False, 'state': 'unknown', 'code': str(exc) if isinstance(exc, ValueError)
            else 'edit_recheck_failed', 'recheck': str(root)}
        if not (root / 'result.json').exists():
            write_new(root / 'result.json', failure)
        write_new(root / 'failure.json', failure)
        return failure
