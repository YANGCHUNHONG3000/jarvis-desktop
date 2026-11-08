"""Independent current-page readback; never repairs or resumes unknown tasks."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root
from . import windows_task as task
from . import windows_visual as visual

BASE = Path(__file__).resolve().parents[1] / 'data' / 'windows_visual_rechecks'
_FAILURE = 'windows_visual_postcondition_failed'


def _source(root):
    """Called only under the original task's existing exclusive lease."""
    scan = task._scan(root)
    task._verify_sources(scan)
    goal, outcome = scan['goal'], scan['last_outcome']
    if (not scan['uncertain'] or not outcome or outcome.get('state') != 'unknown'
            or outcome.get('cause_code') != _FAILURE
            or len(scan['completed']) != len(goal['conditions']) - 1 or not scan['attempts']):
        raise ComputerError('windows_visual_recheck_ineligible')
    condition = goal['conditions'][-1]
    if not visual.is_goal(condition['selector']) or condition['state'] != 'value' or condition['retain']:
        raise ComputerError('windows_visual_recheck_ineligible')
    visual.decode(condition['selector'], condition['value'])
    attempt = root / 'attempts' / f'{scan["attempts"]:04}'
    events = [task._load(path)[0] for path in sorted((attempt / 'events').iterdir())]
    if len(events) < 3:
        raise ComputerError('windows_visual_recheck_ineligible')
    observed, intent, result = events[-3:]
    candidate = intent.get('candidate', {})
    if (observed.get('kind') != 'observation' or intent.get('kind') != 'intent'
            or result.get('kind') != 'result' or any(e.get('purpose') for e in (intent, result))
            or any(e.get('condition_id') != condition['id'] for e in (observed, intent, result))
            or candidate.get('action') != visual.ACTION or candidate.get('value') != condition['value']
            or result.get('result', {}).get('code') != _FAILURE
            or result['result'].get('state') != 'unknown' or result['result'].get('effects') != 'unknown'):
        raise ComputerError('windows_visual_recheck_ineligible')
    number = sum(e['kind'] == 'intent' for e in events)
    operation = attempt / 'operations' / f'{number:04}'
    try:
        request = read_record(operation / 'native-request.json')
        native_intent = read_record(operation / 'native-intent.json')
        native = read_record(operation / 'native-outcome.json')
    except (OSError, ValueError):
        raise ComputerError('windows_visual_recheck_ineligible') from None
    if any(type(value) is not dict for value in (request, native_intent, native)):
        raise ComputerError('windows_visual_recheck_ineligible')
    native_result = {k: v for k, v in native.items() if k != 'input_epoch'}
    if (request.get('op') != 'act' or request.get('action') != visual.ACTION
            or request.get('window') != condition['window'] or request.get('selector') != condition['selector']
            or request.get('value') != condition['value']
            or request.get('before_sha') != intent.get('before_sha256')
            or request.get('control', {}).get('id') != candidate.get('control_id')
            or native_intent.get('request_sha') != digest(request)
            or native_result != result['result'] or native.get('code') != _FAILURE
            or native.get('ok') is not False):
        raise ComputerError('windows_visual_recheck_ineligible')
    binding = {'task_root': str(root), 'task_sha256': scan['task_sha256'],
               'task_inventory_sha256': task._inventory(root), 'attempt': scan['attempts'],
               'attempt_inventory_sha256': task._inventory(attempt),
               'operation': operation.relative_to(root).as_posix(),
               'native_outcome_sha256': sha256((operation / 'native-outcome.json').read_bytes()).hexdigest(),
               'condition': deepcopy(condition), 'completed': list(scan['completed']),
               'original_state': 'unknown'}
    return binding


def inspect(root):
    root = safe_root(root)
    with task._own(root):
        return _source(root)


def _current(raw, condition):
    observation = task._observation(raw, condition['window'])
    if (observation.get('scope') != 'visual_navigation' or observation.get('dialogs')
            or observation.get('application') not in {'explorer', 'wps'}):
        raise ComputerError('windows_visual_recheck_observation_invalid')
    details = observation.get('visual', {})
    control, proof = visual.presentation(details.get('frame'), condition['selector'], condition['value'])
    if (details.get('selector') != condition['selector'] or details.get('value') != condition['value']
            or observation['controls'] != [control]
            or details.get('verified') != proof['verified']
            or details.get('label_boxes') != proof['label_boxes']
            or details.get('expected_matches') != proof['expected_matches']):
        raise ComputerError('windows_visual_recheck_observation_invalid')
    return observation, proof['verified'] and task.satisfied(observation, condition)


def _native_read(folder, condition, observation):
    """Bind production readback to its new worker's request/outcome bytes."""
    native_root = safe_root(folder / 'native')
    operations = list(native_root.iterdir())
    if len(operations) != 1 or not operations[0].is_dir() or not operations[0].name.startswith('observe-'):
        raise ComputerError('windows_visual_recheck_native_evidence_invalid')
    operation = safe_root(operations[0])
    request = read_record(operation / 'native-request.json')
    outcome = read_record(operation / 'native-outcome.json')
    if (type(request) is not dict or type(outcome) is not dict
            or request.get('op') != 'observe' or 'action' in request
            or request.get('window') != condition['window']
            or request.get('selector') != condition['selector']
            or request.get('document_token') != condition['value']
            or outcome.get('ok') is not True or outcome.get('observation') != observation
            or (operation / 'native-intent.json').exists()):
        raise ComputerError('windows_visual_recheck_native_evidence_invalid')
    return {'operation': operation.relative_to(folder).as_posix(),
            'inventory_sha256': task._inventory(native_root)}


def run(root, backend=None, *, parent=BASE, stop=lambda: False):
    """Save a separate success/failure receipt from exactly one read operation.

    Success means only the selected current page now has the requested evidence.
    The original task stays unknown and cannot be resumed or used as success.
    """
    root, parent = safe_root(root), safe_root(parent)
    if root == parent or root in parent.parents:
        raise ComputerError('windows_visual_recheck_destination_invalid')
    with task._own(root):
        source = _source(root)
        if stop():
            raise ComputerError('windows_visual_recheck_cancelled')
        folder = parent / ('recheck-' + uuid.uuid4().hex)
        folder.mkdir(parents=True, exist_ok=False)
        task._save(folder / 'request.json', {'schema': 1, 'kind': 'current_visual_readback',
                   'source': source, 'created': time.time(), 'actions': []})
        observation_sha = None
        native_required, native_proof = backend is None, None
        success, code = False, 'windows_visual_recheck_failed'
        try:
            if backend is None:
                from .windows_access import WindowsBackend
                backend = WindowsBackend(parent=folder / 'native')
            # Never call act, candidates, run, or an inherited result reader.
            raw = backend.observe_condition(deepcopy(source['condition']), stop=stop)
            observation, success = _current(raw, source['condition'])
            observation_sha = task._save(folder / 'observation.json', observation)
            if native_required:
                native_proof = _native_read(folder, source['condition'], observation)
            if stop():
                raise ComputerError('windows_visual_recheck_cancelled')
            if _source(root) != source:
                raise ComputerError('windows_visual_recheck_source_changed')
            code = 'windows_visual_current_page_verified' if success else 'windows_visual_current_page_unverified'
        except Exception as exc:
            success = False
            code = str(exc) if isinstance(exc, ComputerError) else 'windows_visual_recheck_failed'
        # Source drift invalidates both a positive and a negative observation.
        try:
            if _source(root) != source:
                raise ComputerError('windows_visual_recheck_source_changed')
        except Exception:
            success, code = False, 'windows_visual_recheck_source_changed'
        receipt = {'schema': 1, 'kind': 'current_visual_readback', 'ok': success,
                   'state': 'verified' if success else 'failed', 'code': code,
                   'source': source, 'observation_sha256': observation_sha,
                   'native_required': native_required, 'native_proof': native_proof,
                   'evidence_sha256': task._inventory(folder), 'actions': [],
                   'original_state': 'unknown', 'original_task_modified': False,
                   'resumable': False, 'finished': time.time()}
        task._save(folder / 'receipt.json', receipt)
        return {**receipt, 'root': str(folder)}


def load(folder):
    """Verify a receipt and its original source; never convert it into a task."""
    folder = safe_root(folder)
    receipt, _ = task._load(folder / 'receipt.json')
    if (receipt.get('schema') != 1 or type(receipt.get('ok')) is not bool
            or receipt.get('state') != ('verified' if receipt['ok'] else 'failed')
            or receipt.get('kind') != 'current_visual_readback' or receipt.get('actions') != []
            or receipt.get('original_state') != 'unknown' or receipt.get('resumable') is not False):
        raise ComputerError('windows_visual_recheck_evidence_changed')
    root = safe_root(receipt['source']['task_root'])
    with task._own(root):
        if _source(root) != receipt['source']:
            raise ComputerError('windows_visual_recheck_source_changed')
    # The receipt itself was appended last; verify exactly its preceding files.
    saved = (folder / 'receipt.json').read_bytes()
    inventory = {}
    for path in sorted(folder.rglob('*')):
        safe_root(path)
        if path.is_file() and path != folder / 'receipt.json':
            if path.stat().st_size > 2_000_000 or len(inventory) >= 400:
                raise ComputerError('windows_visual_recheck_evidence_changed')
            inventory[path.relative_to(folder).as_posix()] = sha256(path.read_bytes()).hexdigest()
    if digest(inventory) != receipt.get('evidence_sha256'):
        raise ComputerError('windows_visual_recheck_evidence_changed')
    request, _ = task._load(folder / 'request.json')
    if request.get('source') != receipt['source'] or request.get('actions') != []:
        raise ComputerError('windows_visual_recheck_evidence_changed')
    if receipt['ok']:
        observation, observation_sha = task._load(folder / 'observation.json')
        _, success = _current(observation, receipt['source']['condition'])
        if not success or observation_sha != receipt.get('observation_sha256'):
            raise ComputerError('windows_visual_recheck_evidence_changed')
        if receipt.get('native_required'):
            if _native_read(folder, receipt['source']['condition'], observation) != receipt.get('native_proof'):
                raise ComputerError('windows_visual_recheck_evidence_changed')
    if (folder / 'receipt.json').read_bytes() != saved:
        raise ComputerError('windows_visual_recheck_evidence_changed')
    return deepcopy(receipt)
