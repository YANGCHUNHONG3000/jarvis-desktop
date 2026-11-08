"""Independent bounded Cancel readback; never repairs an unknown task."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import math
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root
from . import windows_task as task, windows_reviewed_controls as reviewed

BASE = Path(__file__).resolve().parents[1] / 'data/windows_control_rechecks'
KIND = 'current_cancel_readback'
_FAILURES = {'window_unavailable', 'windows_reviewed_postcondition_failed'}


def _error(suffix):
    return ComputerError('windows_control_recheck_' + suffix)


def _source(root):
    """Pure disk verification, called while holding the original task lease."""
    from .windows_visual_proposal import approved
    scan = task._scan(root)
    task._verify_sources(scan)
    goal, outcome = scan['goal'], scan['last_outcome']
    if (scan['task']['schema'] != 3 or not approved(root, scan)
            or not scan['uncertain'] or not outcome or outcome.get('state') != 'unknown'
            or outcome.get('cause_code') not in _FAILURES or not scan['attempts']
            or scan['completed'] != [c['id'] for c in goal['conditions'][:-1]]):
        raise _error('ineligible')
    condition = goal['conditions'][-1]
    if (not reviewed.is_goal(condition['selector']) or condition['state'] != 'value'
            or condition['retain'] is not False):
        raise _error('ineligible')
    data, source = reviewed.decode(condition['selector'], condition['value'])
    if (data['role'] != 'cancel' or source['kind'] != 'open_dialog'
            or source['binding'].get('scope') != 'dialog_controls'
            or set(source['binding']) != {'scope', 'dialog'} or source['window'] != condition['window']):
        raise _error('ineligible')
    attempt = root / 'attempts' / f'{scan["attempts"]:04}'
    events = [task._load(p)[0] for p in sorted((attempt / 'events').iterdir())]
    if len(events) < 3:
        raise _error('ineligible')
    observed, intent, result = events[-3:]
    if (observed.get('kind') != 'observation' or intent.get('kind') != 'intent'
            or result.get('kind') != 'result' or any('purpose' in e for e in (observed, intent, result))
            or any(e.get('condition_id') != condition['id'] for e in (observed, intent, result))
            or result.get('completes') or result['result'].get('state') != 'unknown'
            or result['result'].get('effects') != 'unknown' or result['result'].get('ok') is not False
            or result['result'].get('code') != outcome.get('cause_code')):
        raise _error('ineligible')
    before, _ = _current(observed['observation'], condition)
    if before['reviewed']['verified'] or before['reviewed']['facts'].get('dialog_presence') != 'visible':
        raise _error('ineligible')
    control = before['controls'][0]
    if intent.get('candidate') != {'control_id': control['id'], 'action': reviewed.ACTION, 'value': condition['value']}:
        raise _error('ineligible')
    operation = attempt / 'operations' / f'{sum(e["kind"] == "intent" for e in events):04}'
    try:
        request = read_record(operation / 'native-request.json')
        native_intent = read_record(operation / 'native-intent.json')
        native = read_record(operation / 'native-outcome.json')
    except (OSError, ValueError):
        raise _error('ineligible') from None
    if (any(type(v) is not dict for v in (request, native_intent, native))
            or request.get('op') != 'act' or request.get('action') != reviewed.ACTION
            or request.get('window') != condition['window'] or request.get('selector') != condition['selector']
            or request.get('value') != condition['value'] or request.get('document_token') != condition['value']
            or request.get('before_sha') != intent.get('before_sha256') or request.get('control') != control
            or native_intent.get('request_sha') != digest(request)
            or native_intent.get('source') != data['source'] or native_intent.get('role') != 'cancel'
            or {k: v for k, v in native.items() if k != 'input_epoch'} != result['result']):
        raise _error('ineligible')
    return {'task_root': str(root), 'task_sha256': scan['task_sha256'],
            'task_inventory_sha256': task._inventory(root), 'attempt': scan['attempts'],
            'attempt_inventory_sha256': task._inventory(attempt),
            'operation': operation.relative_to(root).as_posix(),
            'native_outcome_sha256': sha256((operation / 'native-outcome.json').read_bytes()).hexdigest(),
            'condition': deepcopy(condition), 'completed': list(scan['completed']), 'original_state': 'unknown'}


def inspect(root):
    root = safe_root(root)
    with task._own(root):
        return _source(root)


def _current(raw, condition):
    observation = task._observation(raw, condition['window'])
    state = observation.get('reviewed')
    control = {'id': digest({'selector': condition['selector'],
                            'source': reviewed.decode(condition['selector'], condition['value'])[0]['source']})[:24],
               **condition['selector'], 'visible': True, 'enabled': True,
               'value': condition['value'] if type(state) is dict and state.get('verified') is True else '',
               'patterns': [reviewed.ACTION]}
    if (observation['window'] != condition['window'] or observation.get('application') != 'wps'
            or observation.get('scope') != 'reviewed_control' or observation.get('dialogs') != []
            or observation.get('truncated') is not False or observation['controls'] != [control]
            or type(state) is not dict or set(state) != {'selector', 'value', 'verified', 'facts'}
            or type(state.get('verified')) is not bool or state.get('selector') != condition['selector']
            or state.get('value') != condition['value'] or type(state.get('facts')) is not dict):
        raise _error('observation_invalid')
    facts = state['facts']
    if (set(facts) != {'dialog_presence', 'binding_scope', 'document_state', 'control', 'parent', 'field_value'}
            or facts.get('binding_scope') != 'dialog_controls' or facts.get('document_state') != 'not_claimed'
            or facts.get('dialog_presence') not in {'visible', 'hidden', 'destroyed'}):
        raise _error('observation_invalid')
    destroyed = facts['dialog_presence'] == 'destroyed'
    if (state['verified'] != destroyed
            or (destroyed and any(facts[k] is not None for k in ('control', 'parent', 'field_value')))):
        raise _error('observation_invalid')
    return observation, destroyed and task.satisfied(observation, condition)


def _native_read(folder, condition, observation):
    native_root = safe_root(folder / 'native')
    operations = list(native_root.iterdir())
    if len(operations) != 1 or not operations[0].is_dir() or not operations[0].name.startswith('observe-'):
        raise _error('native_evidence_invalid')
    operation = safe_root(operations[0])
    names = {'native-request.json', 'native-outcome.json', 'reviewed-observe.json'}
    if (any(p.name == 'native-intent.json' for p in folder.rglob('*'))
            or not names.issubset({p.name for p in operation.iterdir()})):
        raise _error('native_evidence_invalid')
    request = read_record(operation / 'native-request.json')
    outcome = read_record(operation / 'native-outcome.json')
    receipt = read_record(operation / 'reviewed-observe.json')
    if (type(request) is not dict or type(outcome) is not dict
            or request.get('op') != 'observe' or 'action' in request
            or request.get('window') != condition['window'] or request.get('selector') != condition['selector']
            or request.get('document_token') != condition['value'] or outcome.get('ok') is not True
            or outcome.get('observation') != observation or receipt != {'schema': 1, 'observation': observation}):
        raise _error('native_evidence_invalid')
    return {'operation': operation.relative_to(folder).as_posix(),
            'inventory_sha256': task._inventory(native_root),
            'files': {name: sha256((operation / name).read_bytes()).hexdigest() for name in sorted(names)}}


def run(root, backend=None, *, parent=BASE, stop=lambda: False):
    """One independent observation; no task resume, approval, activation or action."""
    root, parent = safe_root(root), safe_root(parent)
    if root == parent or root in parent.parents:
        raise _error('destination_invalid')
    with task._own(root):
        source = _source(root)
        if stop():
            raise _error('cancelled')
        folder = parent / ('recheck-' + uuid.uuid4().hex)
        folder.mkdir(parents=True, exist_ok=False)
        task._save(folder / 'request.json', {'schema': 1, 'kind': KIND, 'source': source,
                                           'created': time.time(), 'actions': []})
        native_required, native_proof, observation_sha = backend is None, None, None
        success, code = False, 'windows_control_recheck_failed'
        try:
            if backend is None:
                from .windows_access import WindowsBackend
                backend = WindowsBackend(parent=folder / 'native')
            raw = backend.observe_condition(deepcopy(source['condition']), stop=stop)
            observation, success = _current(raw, source['condition'])
            observation_sha = task._save(folder / 'observation.json', observation)
            if native_required:
                native_proof = _native_read(folder, source['condition'], observation)
            if stop():
                raise _error('cancelled')
            code = 'windows_control_current_cancel_verified' if success else 'windows_control_current_cancel_unverified'
        except Exception as exc:
            success = False
            code = str(exc) if isinstance(exc, ComputerError) else 'windows_control_recheck_failed'
        try:
            if _source(root) != source:
                raise _error('source_changed')
        except Exception:
            success, code = False, 'windows_control_recheck_source_changed'
        receipt = {'schema': 1, 'kind': KIND, 'ok': success, 'state': 'verified' if success else 'failed',
                   'code': code, 'source': source, 'observation_sha256': observation_sha,
                   'native_required': native_required, 'native_proof': native_proof,
                   'evidence_sha256': task._inventory(folder), 'actions': [],
                   'original_state': 'unknown', 'original_task_modified': False, 'resumable': False,
                   'fresh_permission': False, 'finished': time.time()}
        task._save(folder / 'receipt.json', receipt)
        return {**receipt, 'root': str(folder), 'historical': False}


def _load_receipt(folder, root):
    """Called under the original lease, including the final source reread."""
    folder = safe_root(folder)
    receipt, _ = task._load(folder / 'receipt.json')
    saved = (folder / 'receipt.json').read_bytes()
    if (type(receipt.get('schema')) is not int or receipt['schema'] != 1
            or receipt.get('kind') != KIND or type(receipt.get('ok')) is not bool
            or receipt.get('state') != ('verified' if receipt['ok'] else 'failed')
            or receipt.get('actions') != [] or receipt.get('original_state') != 'unknown'
            or receipt.get('original_task_modified') is not False or receipt.get('resumable') is not False
            or receipt.get('fresh_permission') is not False or type(receipt.get('native_required')) is not bool
            or type(receipt.get('source')) is not dict
            or type(receipt.get('finished')) not in (int, float)
            or not math.isfinite(receipt['finished']) or receipt['finished'] <= 0):
        raise _error('evidence_changed')
    if safe_root(receipt['source']['task_root']) != root or _source(root) != receipt['source']:
        raise _error('source_changed')
    inventory = {}
    for path in sorted(folder.rglob('*')):
        safe_root(path)
        if path.is_file() and path != folder / 'receipt.json':
            if path.stat().st_size > 2_000_000 or len(inventory) >= 400:
                raise _error('evidence_changed')
            inventory[path.relative_to(folder).as_posix()] = sha256(path.read_bytes()).hexdigest()
    if digest(inventory) != receipt.get('evidence_sha256'):
        raise _error('evidence_changed')
    request, _ = task._load(folder / 'request.json')
    if (request.get('schema') != 1 or request.get('kind') != KIND
            or request.get('source') != receipt['source'] or request.get('actions') != []):
        raise _error('evidence_changed')
    if receipt['ok']:
        observation, observation_sha = task._load(folder / 'observation.json')
        if (not _current(observation, receipt['source']['condition'])[1]
                or observation_sha != receipt.get('observation_sha256')
                or receipt.get('code') != 'windows_control_current_cancel_verified'):
            raise _error('evidence_changed')
        if receipt['native_required']:
            if _native_read(folder, receipt['source']['condition'], observation) != receipt.get('native_proof'):
                raise _error('evidence_changed')
        elif receipt.get('native_proof') is not None:
            raise _error('evidence_changed')
    if (folder / 'receipt.json').read_bytes() != saved:
        raise _error('evidence_changed')
    if _source(root) != receipt['source']:
        raise _error('source_changed')
    return {**deepcopy(receipt), 'root': str(folder), 'historical': True}


def load(folder):
    """Verify saved history without observing UI or changing the parent."""
    folder = safe_root(folder)
    receipt, _ = task._load(folder / 'receipt.json')
    try:
        root = safe_root(receipt['source']['task_root'])
    except (KeyError, TypeError):
        raise _error('evidence_changed') from None
    with task._own(root):
        return _load_receipt(folder, root)


def latest(root, *, parent=BASE):
    """Return the latest valid production success; invalid/failed history is retained."""
    root, parent = safe_root(root), safe_root(parent)
    inspect(root)
    if not parent.exists():
        return None
    found = []
    for folder in parent.glob('recheck-*'):
        try:
            value = load(folder)
            if value['ok'] and value['native_required'] and safe_root(value['source']['task_root']) == root:
                found.append(value)
        except (ComputerError, OSError, ValueError, KeyError, TypeError):
            continue
    return max(found, key=lambda r: (r['finished'], r['root'])) if found else None


def verified_window(folder):
    """Historical source identity for a new observation, never permission to act."""
    receipt = load(folder)
    if not receipt['ok'] or not receipt['native_required']:
        raise _error('production_proof_required')
    return deepcopy(receipt['source']['condition']['window'])
