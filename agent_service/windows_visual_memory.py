"""Bounded, read-only navigation vocabulary from verified native history.

History supplies labels and proof words, never coordinates, current-page claims,
or authority to replay an unknown operation. Schema3 plans are not source data.
"""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import heapq
from pathlib import Path
import re
import struct

from .computer import ComputerError, digest, read_record, safe_root, same_identity
from . import windows_task as task
from . import windows_visual as visual
from . import windows_visual_recovery as recovery

_ROOT = Path(__file__).resolve().parents[1]
TASK_BASE = getattr(task, 'BASE', _ROOT / 'data' / 'windows_tasks')
OBSERVATION_BASE = _ROOT / 'data' / 'windows_observations'
_IDENTITY = ('hwnd', 'pid', 'created', 'image', 'class_name', 'profile', 'title')


def _window(value):
    if (type(value) is not dict or any(k not in value for k in _IDENTITY)
            or any(type(value[k]) is not int or value[k] <= 0 for k in ('hwnd', 'pid'))
            or any(type(value[k]) is not str or not value[k] or len(value[k]) > 32768
                   for k in _IDENTITY if k not in ('hwnd', 'pid'))):
        raise ComputerError('windows_visual_memory_window_invalid')
    return {k: value[k] for k in _IDENTITY}


def _same(window, other):
    return same_identity(window, other) and window['title'] == other.get('title')


def _bitmap(frame):
    visual.validate_frame(frame)
    recipe = frame.get('recipe')
    if recipe is not None and (type(recipe) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', recipe)
                               or recipe in {'.', '..'}):
        raise ComputerError('windows_visual_memory_bitmap_invalid')
    cache = OBSERVATION_BASE / 'ocr-cache'
    path = safe_root((cache / recipe if recipe is not None else cache) / (frame['sha256'] + '.bmp'))
    if path.stat().st_size > 5_000_000:
        raise ComputerError('windows_visual_memory_bitmap_invalid')
    raw = path.read_bytes()
    if len(raw) < 54 or sha256(raw).hexdigest() != frame['sha256'] or raw[:2] != b'BM':
        raise ComputerError('windows_visual_memory_bitmap_invalid')
    size, offset = struct.unpack_from('<I', raw, 2)[0], struct.unpack_from('<I', raw, 10)[0]
    header, width, height, planes, bits, compression = struct.unpack_from('<IiiHHI', raw, 14)
    if (header != 40 or offset != 54 or size != len(raw) or width != frame['width']
            or abs(height) != frame['height'] or planes != 1 or bits != 32 or compression != 0
            or len(raw) != 54 + width * abs(height) * 4):
        raise ComputerError('windows_visual_memory_bitmap_invalid')
    return {'bitmap': str(path), 'bitmap_sha256': frame['sha256'],
            'frame_sha256': digest(frame), 'recipe': recipe}


def _candidate(condition, observation, source):
    if (condition.get('state') != 'value' or not visual.is_goal(condition.get('selector'))
            or observation.get('application') not in {'explorer', 'wps'}
            or observation.get('scope') != 'visual_navigation' or observation.get('dialogs')):
        raise ComputerError('windows_visual_memory_source_invalid')
    observation = task._observation(observation, condition['window'])
    data = visual.decode(condition['selector'], condition['value'])
    details = observation.get('visual', {})
    frame = details.get('frame')
    control, proof = visual.presentation(frame, condition['selector'], condition['value'])
    if (not proof['verified'] or observation['controls'] != [control]
            or not task.satisfied(observation, condition)
            or details.get('selector') != condition['selector'] or details.get('value') != condition['value']):
        raise ComputerError('windows_visual_memory_source_invalid')
    return {'window': _window(condition['window']), 'label': condition['selector']['name'],
            'expected': deepcopy(data['all']), 'source': {**source, **_bitmap(frame)}}


def _native_result(root, operation, condition, intent, result):
    request = read_record(operation / 'native-request.json')
    native_intent = read_record(operation / 'native-intent.json')
    native = read_record(operation / 'native-outcome.json')
    if any(type(item) is not dict for item in (request, native_intent, native)):
        raise ComputerError('windows_visual_memory_native_invalid')
    if (request.get('op') != 'act' or request.get('action') != visual.ACTION
            or intent['candidate'].get('action') != visual.ACTION
            or intent['candidate'].get('value') != condition['value']
            or request.get('selector') != condition['selector'] or request.get('window') != condition['window']
            or request.get('value') != condition['value'] or request.get('before_sha') != intent.get('before_sha256')
            or request.get('control', {}).get('id') != intent['candidate'].get('control_id')
            or native_intent.get('request_sha') != digest(request)
            or {k: v for k, v in native.items() if k != 'input_epoch'} != result
            or native.get('ok') is not True or native.get('state') != 'verified'
            or native.get('effects') != 'verified'):
        raise ComputerError('windows_visual_memory_native_invalid')
    return {'native_operation': operation.relative_to(root).as_posix(),
            'native_outcome_sha256': sha256((operation / 'native-outcome.json').read_bytes()).hexdigest()}


def _task_candidates(root, windows):
    # Cheap manifest filtering happens before the complete evidence walk.
    payload, _ = task._load(root / 'task.json')
    if payload.get('schema') not in (1, 2):
        return [], 'windows_visual_memory_schema_skipped'
    goals = payload.get('goal', {}).get('conditions', [])
    if not any(visual.is_goal(c.get('selector')) and any(_same(w, c.get('window', {})) for w in windows)
               for c in goals if type(c) is dict):
        return [], 'windows_visual_memory_no_matching_step'
    found = []
    with task._own(root):
        # Recheck inside the lease too: schema3 verification may depend on this
        # module, so it must never enter _scan after a changed cheap manifest.
        current_payload, _ = task._load(root / 'task.json')
        if current_payload.get('schema') not in (1, 2):
            return [], 'windows_visual_memory_schema_skipped'
        scan = task._scan(root)
        task._verify_sources(scan)
        inventory = task._inventory(root)
        conditions = {c['id']: c for c in scan['goal']['conditions']}
        verified = {}
        for attempt in sorted((root / 'attempts').iterdir()):
            intent, operation_number = None, 0
            for path in sorted((attempt / 'events').iterdir()):
                event, event_sha = task._load(path)
                condition = conditions.get(event.get('condition_id'))
                if event['kind'] == 'intent':
                    intent = event
                    operation_number += 1
                elif event['kind'] == 'result' and condition is not None and visual.is_goal(condition['selector']):
                    result = event['result']
                    if (intent and intent['candidate'].get('action') == visual.ACTION
                            and result.get('ok') is True and result.get('state') == 'verified'
                            and result.get('effects') == 'verified' and not event.get('purpose')):
                        verified[condition['id']] = (intent, result, path, event_sha,
                            attempt / 'operations' / f'{operation_number:04}')
                elif (event['kind'] == 'complete' and condition is not None
                      and condition['id'] in scan['completed'] and visual.is_goal(condition['selector'])
                      and any(_same(w, condition['window']) for w in windows)):
                    evidence = verified.get(condition['id'])
                    if evidence is None:
                        continue
                    prior_intent, result, result_path, result_sha, operation = evidence
                    if result.get('observation', {}).get('visual', {}).get('frame') != event['observation'].get('visual', {}).get('frame'):
                        continue
                    native = _native_result(root, operation, condition, prior_intent, result)
                    source = {'kind': 'completed_visual_step', 'root': str(root),
                              'task_sha256': scan['task_sha256'], 'inventory_sha256': inventory,
                              'condition_id': condition['id'], 'complete_event': path.relative_to(root).as_posix(),
                              'complete_sha256': event_sha, 'result_event': result_path.relative_to(root).as_posix(),
                              'result_sha256': result_sha, **native}
                    # Both records must independently support this exact page contract.
                    _candidate(condition, result['observation'], source)
                    found.append(_candidate(condition, event['observation'], source))
        if task._inventory(root) != inventory:
            raise ComputerError('windows_visual_memory_source_changed')
    return found, 'windows_visual_memory_no_verified_step'


def _recheck_candidates(root, windows):
    # A recheck of a schema3 task can indirectly verify proposals through memory
    # again. This first corpus deliberately admits only original schema1/2 tasks.
    header, _ = task._load(root / 'receipt.json')
    origin = safe_root(header['source']['task_root'])
    manifest, _ = task._load(origin / 'task.json')
    if manifest.get('schema') not in (1, 2):
        return [], 'windows_visual_memory_schema_skipped'
    receipt = recovery.load(root)
    if receipt.get('ok') is not True or receipt.get('native_required') is not True:
        return [], 'windows_visual_memory_recheck_not_native_verified'
    condition = receipt['source']['condition']
    if not any(_same(w, condition['window']) for w in windows):
        return [], 'windows_visual_memory_no_matching_step'
    observation, observation_sha = task._load(root / 'observation.json')
    if observation_sha != receipt.get('observation_sha256'):
        raise ComputerError('windows_visual_memory_source_changed')
    _, receipt_sha = task._load(root / 'receipt.json')
    inventory = task._inventory(root)
    result = _candidate(condition, observation, {'kind': 'native_visual_recheck', 'root': str(root),
              'receipt_sha256': receipt_sha, 'inventory_sha256': inventory,
              'observation_sha256': observation_sha})
    # Detect a concurrent append/replacement after loading, including source drift.
    if recovery.load(root) != receipt or task._inventory(root) != inventory:
        raise ComputerError('windows_visual_memory_source_changed')
    return [result], 'windows_visual_memory_no_verified_step'


def discover_report(windows, *, task_base=TASK_BASE, recheck_base=recovery.BASE, limit=32):
    if type(windows) not in (list, tuple) or len(windows) > 2 or type(limit) is not int or not 1 <= limit <= 128:
        raise ComputerError('windows_visual_memory_input_invalid')
    windows = [_window(w) for w in windows]
    report = {'candidates': [], 'diagnostics': [], 'empty_reason': None}
    if not windows:
        report['empty_reason'] = 'no_selected_windows'
        return report
    roots = []
    for kind, base in [('task', task_base), ('recheck', recheck_base)]:
        base = safe_root(base)
        if base.exists():
            for path in base.iterdir():
                try:
                    if path.is_dir():
                        safe_root(path)
                        roots.append((path.stat().st_mtime_ns, str(path), kind))
                except (OSError, ValueError):
                    report['diagnostics'].append({'kind': kind, 'root': str(path),
                                                 'code': 'windows_visual_memory_directory_unavailable'})
    found = []
    for _, path, kind in heapq.nlargest(limit, roots):
        try:
            root = safe_root(path)
            candidates, reason = (_task_candidates(root, windows) if kind == 'task'
                                  else _recheck_candidates(root, windows))
            found.extend(candidates)
            if not candidates:
                report['diagnostics'].append({'kind': kind, 'root': path, 'code': reason})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            report['diagnostics'].append({'kind': kind, 'root': path,
                'code': str(exc) if isinstance(exc, ComputerError) else 'windows_visual_memory_source_invalid'})
    groups = defaultdict(dict)
    for candidate in found:
        key = (digest(candidate['window']), visual.normalize(candidate['label']))
        contract = tuple(sorted(visual.normalize(x) for x in candidate['expected']))
        groups[key].setdefault(contract, candidate)
    for (_, label), contracts in groups.items():
        if len(contracts) != 1:
            report['diagnostics'].append({'kind': 'ambiguity', 'label': label,
                                         'code': 'windows_visual_memory_contract_ambiguous'})
        elif len(report['candidates']) < 8:
            report['candidates'].append(next(iter(contracts.values())))
    if not report['candidates']:
        report['empty_reason'] = ('ambiguous_visual_history' if any(d['kind'] == 'ambiguity' for d in report['diagnostics'])
                                  else 'no_verified_same_window_history')
    return report


def discover(windows, *, task_base=TASK_BASE, recheck_base=recovery.BASE, limit=32):
    return discover_report(windows, task_base=task_base, recheck_base=recheck_base, limit=limit)['candidates']


def verify(candidate):
    """Selected evidence is mandatory: invalid/changed mappings fail closed."""
    try:
        if type(candidate) is not dict or set(candidate) != {'window', 'label', 'expected', 'source'}:
            raise ComputerError('windows_visual_memory_candidate_invalid')
        window = _window(candidate['window'])
        source = candidate['source']
        root = safe_root(source['root'])
        if source['kind'] == 'completed_visual_step':
            found, _ = _task_candidates(root, [window])
        elif source['kind'] == 'native_visual_recheck':
            found, _ = _recheck_candidates(root, [window])
        else:
            found = []
        if candidate not in found:
            raise ComputerError('windows_visual_memory_source_changed')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ComputerError('windows_visual_memory_source_changed') from None
