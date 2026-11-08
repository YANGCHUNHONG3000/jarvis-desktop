"""Local, bounded Windows conditions with immutable evidence and safe restart.

The backend owns application permissions and native effects. This engine only
chooses a unique current candidate and never retries an uncertain effect.
"""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, same_identity, write_new
from .run_lifecycle import _lock, _unlock

MAX_CONDITIONS = 12
_ATTENTION_REASONS = {'owned_window_requires_manual_attention', 'multiple_find_dialogs', 'windows_application_blocked'}
_TRANSIENT = {'screen_changed_reobserve', 'windows_observation_changed', 'agent_observation_changed'}
_IDENTITY = {'hwnd', 'pid', 'created', 'image', 'class_name', 'title', 'profile'}


def _text(value, limit=300, *, empty=False):
    return (type(value) is str and len(value) <= limit and (empty or bool(value.strip()))
            and not any(ord(c) < 32 and c not in '\r\n\t' for c in value))


def _selector(value, *, ancestor=True):
    required = {'type', 'name', 'automation_id'}
    if (type(value) is not dict or set(value) not in (required, required | {'ancestor'})
            or not _text(value.get('type')) or not _text(value.get('name'), empty=True)
            or not _text(value.get('automation_id'), empty=True)):
        raise ComputerError('windows_selector_invalid')
    if not value['name'] and not value['automation_id']:
        raise ComputerError('windows_selector_invalid')
    if 'ancestor' in value:
        if not ancestor:
            raise ComputerError('windows_selector_invalid')
        _selector(value['ancestor'], ancestor=False)
    return value


def validate_goal(goal):
    if (type(goal) is not dict or set(goal) != {'schema', 'instruction', 'conditions', 'max_steps'}
            or type(goal['schema']) is not int or goal['schema'] != 1
            or not _text(goal['instruction'], 2000)
            or type(goal['max_steps']) is not int or not 1 <= goal['max_steps'] <= 12
            or type(goal['conditions']) is not list or not 1 <= len(goal['conditions']) <= MAX_CONDITIONS):
        raise ComputerError('windows_goal_invalid')
    ids = set()
    for condition in goal['conditions']:
        if (type(condition) is not dict or set(condition) != {'id', 'window', 'selector', 'state', 'value', 'retain'}
                or type(condition['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', condition['id'])
                or condition['id'] in ids or type(condition['retain']) is not bool
                or type(condition['window']) is not dict or not _IDENTITY.issubset(condition['window'])
                or type(condition['window']['hwnd']) is not int or condition['window']['hwnd'] <= 0
                or type(condition['window']['pid']) is not int or condition['window']['pid'] <= 0
                or any(not _text(condition['window'][key], 32768) for key in _IDENTITY - {'hwnd', 'pid'})
                or condition['state'] not in {'selected', 'expanded', 'collapsed', 'focused', 'value', 'checked'}):
            raise ComputerError('windows_condition_invalid')
        ids.add(condition['id'])
        _selector(condition['selector'])
        from .windows_winapp_office import is_goal as office_goal, decode as office_decode
        if office_goal(condition['selector']):
            if condition['state'] != 'value' or condition['retain']:
                raise ComputerError('windows_office_goal_invalid')
            candidate, _ = office_decode(condition['selector'], condition['value'])
            if candidate['window'] != condition['window']:
                raise ComputerError('windows_office_window_changed')
        from .windows_reviewed_controls import is_goal as reviewed_goal, decode as reviewed_decode
        if reviewed_goal(condition['selector']):
            if condition['state']!='value' or condition['retain']:
                raise ComputerError('windows_reviewed_goal_invalid')
            _,source=reviewed_decode(condition['selector'],condition['value'])
            if source['window']!=condition['window']:raise ComputerError('windows_reviewed_window_changed')
        from .windows_visual_forms import is_goal as form_goal, decode as form_decode
        if form_goal(condition['selector']):
            if condition['state']!='value' or condition['retain']:
                raise ComputerError('windows_form_goal_invalid')
            _,source=form_decode(condition['selector'],condition['value'])
            if source['window']!=condition['window']:raise ComputerError('windows_form_window_changed')
        from .windows_visual import is_goal as visual_goal, decode as visual_decode
        if visual_goal(condition['selector']):
            if condition['state'] != 'value' or condition['retain']:
                raise ComputerError('windows_visual_goal_invalid')
            visual_decode(condition['selector'], condition['value'])
        state, value = condition['state'], condition['value']
        if state == 'value':
            valid = _text(value, 4000, empty=True)
        elif state == 'collapsed':
            valid = type(value) in (int, bool) and value == 0
        elif state == 'checked':
            valid = type(value) in (int, bool) and value in (0, 1)
        else:
            valid = type(value) in (int, bool) and value == 1
        if not valid or (state == 'focused' and condition['retain']):
            raise ComputerError('windows_condition_invalid')
    from .windows_reviewed_controls import validate_sequence
    validate_sequence(goal['conditions'])
    return deepcopy(goal)


def _observation(value, window):
    if (type(value) is not dict or type(value.get('window')) is not dict
            or not same_identity(window, value['window']) or type(value.get('controls')) is not list
            or len(value['controls']) > 200 or len(json.dumps(value, ensure_ascii=False)) > 250_000
            or type(value.get('truncated', False)) is not bool):
        raise ComputerError('windows_observation_invalid')
    ids = []
    for control in value['controls']:
        if (type(control) is not dict or not _text(control.get('id'), 128)
                or any(not _text(control.get(k), 300, empty=True) for k in ('type', 'name', 'automation_id'))
                or any(type(control.get(k)) is not bool for k in ('enabled', 'visible'))):
            raise ComputerError('windows_observation_invalid')
        ids.append(control['id'])
    if len(set(ids)) != len(ids):
        raise ComputerError('windows_control_ambiguous')
    reason = value.get('blocked_reason')
    if reason:
        raise ComputerError('windows_application_blocked')
    if value.get('truncated'):
        raise ComputerError('windows_observation_truncated')
    return deepcopy(value)


def locate(observation, selector):
    """Resolve a semantic selector uniquely within a complete current view."""
    _selector(selector)
    def matches(control, expected):
        return all(control.get(key) == expected[key] for key in ('type', 'name', 'automation_id'))
    controls = []
    for control in observation['controls']:
        if not matches(control, selector):
            continue
        if 'ancestor' in selector:
            parents = control.get('ancestors', [])
            if 'ancestor' in control:
                parents = [*parents, control['ancestor']]
            if not any(type(parent) is dict and matches(parent, selector['ancestor']) for parent in parents):
                continue
        controls.append(control)
    if len(controls) > 1:
        raise ComputerError('windows_target_ambiguous')
    return controls[0] if controls else None


def satisfied(observation, condition):
    control = locate(observation, condition['selector'])
    if control is None or not control.get('visible') or not control.get('enabled'):
        return False
    return _state_matches(control, condition)


def _reviewed_completion(condition, result, operation):
    """Validate the worker's persisted independent postcondition before completion."""
    from .windows_reviewed_controls import is_goal
    if not is_goal(condition['selector']) or not result.get('ok') or result.get('state') != 'verified' or result.get('effects') != 'verified':
        raise ComputerError('windows_reviewed_completion_invalid')
    observed = _observation(result.get('observation'), condition['window'])
    state = observed.get('reviewed')
    if (observed['window'] != condition['window'] or observed.get('scope') != 'reviewed_control'
            or len(observed['controls']) != 1 or type(state) is not dict
            or state.get('verified') is not True or state.get('selector') != condition['selector']
            or state.get('value') != condition['value'] or type(state.get('facts')) is not dict
            or not satisfied(observed, condition)
            or read_record(operation / 'reviewed-after.json') != {'schema': 1, 'observation': observed}):
        raise ComputerError('windows_reviewed_completion_invalid')
    return observed


def _office_completion(condition, result, operation):
    from .windows_winapp_office import is_goal, decode, ACTION
    if not is_goal(condition['selector']) or not result.get('ok') or result.get('state') != 'verified' or result.get('effects') != 'verified':
        raise ComputerError('windows_office_completion_invalid')
    item, _ = decode(condition['selector'], condition['value'])
    observed = _observation(result.get('observation'), condition['window'])
    if (observed.get('scope') != 'winapp_office' or observed.get('office', {}).get('role') != item['source']['role']
            or observed.get('office', {}).get('verified') is not True or not satisfied(observed, condition)
            or read_record(operation / 'office-after.json') != {'schema': 1, 'observation': observed}):
        raise ComputerError('windows_office_completion_invalid')
    return observed


def _state_matches(control, condition):
    """Read the condition independently of viewport position."""
    state = condition['state']
    if state == 'value':
        actual = control.get('value')
        return type(actual) is str and actual.replace('\r\n', '\n').replace('\r', '\n') == condition['value'].replace('\r\n', '\n').replace('\r', '\n')
    key = 'expanded' if state in {'expanded', 'collapsed'} else state
    actual = control.get(key)
    return type(actual) in (bool, int) and actual == condition['value']


def _save(path, payload):
    record = {'schema': 1, 'payload': payload, 'sha256': digest(payload)}
    write_new(path, record)
    return record['sha256']


def _load(path):
    record = read_record(safe_root(path))
    if (type(record) is not dict or set(record) != {'schema', 'payload', 'sha256'}
            or record['schema'] != 1 or digest(record['payload']) != record['sha256']):
        raise ComputerError('windows_evidence_changed')
    return record['payload'], record['sha256']


def _inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        safe_root(path)
        if path.is_file():
            if path.stat().st_size > 2_000_000:
                raise ComputerError('windows_evidence_limit')
            if path.parent == root and path.name == 'task.lease':
                # Byte zero is the live owner lock. Identity starts at byte one,
                # exactly as _own verifies it; never read the locked byte.
                with path.open('rb') as stream:
                    stream.seek(1)
                    data = b'!' + stream.read()
            else:
                data = path.read_bytes()
            result[path.relative_to(root).as_posix()] = sha256(data).hexdigest()
    if len(result) > 400:
        raise ComputerError('windows_evidence_limit')
    return digest(result)


def create(parent, goal, *, sources=None, visual_proposal=None, stage_binding=None, provider_binding=None):
    goal = validate_goal(goal)
    from .windows_winapp_office import is_goal as office_goal
    if any(office_goal(c['selector']) for c in goal['conditions']):
        from .windows_winapp import OFFICE_PROVIDER_BINDING
        from .windows_auto import PROVIDER_BINDING as AUTO_PROVIDER_BINDING
        if provider_binding not in (OFFICE_PROVIDER_BINDING, AUTO_PROVIDER_BINDING) or stage_binding is None:
            raise ComputerError('windows_office_stage_required')
    from .windows_visual_forms import is_goal as form_goal
    from .windows_reviewed_controls import is_goal as reviewed_goal
    if any(form_goal(c['selector']) or reviewed_goal(c['selector']) for c in goal['conditions']) and visual_proposal is None:
        raise ComputerError('windows_visual_approval_required')
    if sources is not None:
        from .windows_handoff import verify
        sources = deepcopy(sources)
        verify(goal, sources)
    if visual_proposal is not None:
        if sources is not None:raise ComputerError('windows_visual_proposal_mixed_sources')
        from .windows_visual_proposal import verify
        verify(visual_proposal,goal)
    root = safe_root(parent) / ('windows-' + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    (root / 'attempts').mkdir()
    token = ('!' + uuid.uuid4().hex).encode('ascii')
    with (root / 'task.lease').open('xb') as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
        stat = os.fstat(stream.fileno())
    payload = {'schema': 1, 'goal': goal, 'created': time.time(),
               'lease_sha256': sha256(token).hexdigest(),
               'lease_id': [stat.st_dev, stat.st_ino]}
    if sources is not None:
        payload.update(schema=2, sources=sources)
    if visual_proposal is not None:
        payload.update(schema=3,visual_proposal=deepcopy(visual_proposal))
    if stage_binding is not None:
        if (type(stage_binding) is not dict or set(stage_binding) != {'root', 'number'}
                or type(stage_binding['root']) is not str or type(stage_binding['number']) is not int
                or not 1 <= stage_binding['number'] <= 6):
            raise ComputerError('windows_stage_binding_invalid')
        payload['stage_binding'] = deepcopy(stage_binding)
    if provider_binding is not None:
        from .windows_winapp import valid_provider_binding
        if not valid_provider_binding(provider_binding):
            raise ComputerError('windows_provider_binding_invalid')
        payload['provider_binding'] = deepcopy(provider_binding)
    _save(root / 'task.json', payload)
    return root


@contextmanager
def _own(root):
    task, _ = _load(root / 'task.json')
    lease = safe_root(root / 'task.lease')
    with lease.open('r+b', buffering=0) as stream:
        stat = os.fstat(stream.fileno())
        # Byte zero may be locked by another Windows process; identity bytes
        # remain readable so liveness is established by the lock itself.
        stream.seek(1)
        if ([stat.st_dev, stat.st_ino] != task.get('lease_id')
                or [os.stat(lease).st_dev, os.stat(lease).st_ino] != task.get('lease_id')
                or sha256(b'!' + stream.read()).hexdigest() != task.get('lease_sha256')):
            raise ComputerError('windows_lease_changed')
        try:
            _lock(stream)
        except ComputerError as exc:
            raise ComputerError('windows_task_active' if str(exc) == 'run_owner_active' else 'windows_lease_invalid') from None
        try:
            if _load(root / 'task.json')[0] != task:
                raise ComputerError('windows_evidence_changed')
            yield
        finally:
            _unlock(stream)


def _attention(value, window):
    """Only bounded popup metadata; it grants no action or completion authority."""
    if (type(value) is not dict or not same_identity(value.get('window', {}), window)
            or value.get('blocked_reason') not in _ATTENTION_REASONS
            or value.get('controls') != [] or value.get('dialogs') != []):
        raise ComputerError('windows_interruption_invalid')
    info = {'schema': 1, 'reason': value['blocked_reason'], 'windows': deepcopy(value.get('blocked_windows'))}
    return _validate_attention(info)


def _validate_attention(info):
    if (type(info) is not dict or set(info) != {'schema', 'reason', 'windows'} or type(info['schema']) is not int or info['schema'] != 1
            or info['reason'] not in _ATTENTION_REASONS
            or type(info['windows']) is not list or not 1 <= len(info['windows']) <= 16):
        raise ComputerError('windows_interruption_invalid')
    seen = set()
    for item in info['windows']:
        if (type(item) is not dict or set(item) != {'hwnd', 'class_name'}
                or type(item['hwnd']) is not int or not 0 < item['hwnd'] <= 0xffffffffffffffff or item['hwnd'] in seen
                or not _text(item['class_name'], 128)
                or any(ord(c) < 32 or ord(c) == 127 for c in item['class_name'])):
            raise ComputerError('windows_interruption_invalid')
        seen.add(item['hwnd'])
    return deepcopy(info)


def _safe_interruption_result(result, window):
    raw = result.get('observation')
    if type(raw) is not dict or not raw.get('blocked_reason'):
        return result
    if raw.get('blocked_reason') not in _ATTENTION_REASONS:
        raise ComputerError('windows_interruption_invalid')
    info = _attention(raw, window)
    code = result.get('code')
    if type(code) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
        code = 'windows_backend_blocked'
    return {**{k: result[k] for k in ('ok', 'state', 'effects')}, 'code': code,
            'observation': {'window': deepcopy(window), 'controls': [], 'dialogs': [],
                            'blocked_reason': info['reason'], 'blocked_windows': info['windows']}}


def _scan(root):
    task, task_sha = _load(root / 'task.json')
    goal = validate_goal(task['goal'])
    if type(task.get('schema')) is not int or task['schema'] not in (1, 2, 3):
        raise ComputerError('windows_evidence_changed')
    binding = task.get('stage_binding')
    if binding is not None and (type(binding) is not dict or set(binding) != {'root', 'number'}
            or type(binding['root']) is not str or type(binding['number']) is not int
            or not 1 <= binding['number'] <= 6):
        raise ComputerError('windows_stage_binding_invalid')
    if 'provider_binding' in task:
        from .windows_winapp import valid_provider_binding
        if not valid_provider_binding(task['provider_binding']):
            raise ComputerError('windows_provider_binding_invalid')
    if task['schema'] == 2:
        from .windows_handoff import validate
        if type(task.get('sources')) is not list or not task['sources']:
            raise ComputerError('windows_evidence_changed')
        validate(goal, task.get('sources'))
    elif 'sources' in task:
        raise ComputerError('windows_evidence_changed')
    if task['schema']==3:
        if type(task.get('visual_proposal')) is not dict:
            raise ComputerError('windows_evidence_changed')
    elif 'visual_proposal' in task:
        raise ComputerError('windows_evidence_changed')
    completed, previous, count, actions = [], task_sha, 0, 0
    uncertain, last_outcome = False, None
    unsealed, last_attention = set(), None
    attempts = sorted((root / 'attempts').iterdir())
    for number, attempt in enumerate(attempts, 1):
        last_attention = None
        if not attempt.is_dir() or attempt.name != f'{number:04}':
            raise ComputerError('windows_evidence_changed')
        start, _ = _load(attempt / 'attempt.json')
        contract = start.get('interruption_contract')
        if (contract is not None and (type(contract) is not int or contract != 1)):
            raise ComputerError('windows_evidence_changed')
        expected_start = {'schema': 1, 'number': number, 'previous': previous, 'task_sha256': task_sha}
        if contract is not None: expected_start['interruption_contract'] = contract
        if start != expected_start:
            raise ComputerError('windows_evidence_changed')
        tail, pending, dispatched, latest, latest_for = digest(start), None, 0, None, None
        event_files = sorted((attempt / 'events').iterdir())
        for index, path in enumerate(event_files, 1):
            if path.name != f'{index:04}.json':
                raise ComputerError('windows_evidence_changed')
            event, event_sha = _load(path)
            if event.get('previous') != tail or event.get('kind') not in {'observation', 'intent', 'result', 'complete', 'interruption'}:
                raise ComputerError('windows_evidence_changed')
            tail = event_sha
            recheck = event.get('purpose') == 'recheck'
            if 'purpose' in event and not recheck:
                raise ComputerError('windows_evidence_changed')
            condition = (next((c for c in goal['conditions'] if c['id'] == event.get('condition_id')
                               and c['id'] in completed and (c['retain'] or c is goal['conditions'][-1])), None)
                         if recheck else goal['conditions'][len(completed)] if len(completed) < len(goal['conditions']) else None)
            if condition is None or event.get('condition_id') != condition['id']:
                raise ComputerError('windows_evidence_changed')
            kind = event['kind']
            if kind == 'interruption':
                if (contract != 1 or pending is not None or index != len(event_files)
                        or set(event) != {'kind', 'condition_id', 'previous', 'attention'} | ({'purpose'} if recheck else set())):
                    raise ComputerError('windows_interruption_invalid')
                info = _validate_attention(event['attention'])
                uncertain |= condition['id'] in unsealed
                last_attention = {**info, 'condition_id': condition['id'], 'after_action': uncertain}
            if kind == 'intent':
                candidate = event.get('candidate')
                if (pending is not None or latest is None
                        or latest_for != (condition['id'], recheck)
                        or event.get('before_sha256') != digest(latest)
                        or type(candidate) is not dict or set(candidate) != {'control_id', 'action', 'value'}):
                    raise ComputerError('windows_evidence_changed')
                if recheck or candidate.get('action') == 'reveal':
                    target = locate(latest, condition['selector'])
                    if (candidate != {'control_id': target['id'] if target else None, 'action': 'reveal', 'value': ''}
                            or target is None or target['visible'] or not target['enabled']
                            or (recheck and not _state_matches(target, condition))):
                        raise ComputerError('windows_evidence_changed')
                pending = event
                dispatched += 1
                actions += 1
            elif kind == 'result':
                if (pending is None or event.get('intent_sha256') != digest(pending)
                        or event.get('condition_id') != pending['condition_id']
                        or event.get('purpose') != pending.get('purpose')):
                    raise ComputerError('windows_evidence_changed')
                operation = attempt / 'operations' / f'{dispatched:04}'
                if _inventory(operation) != event.get('operation_sha256'):
                    raise ComputerError('windows_evidence_changed')
                result = _result(event.get('result'))
                if 'completes' in event:
                    if event['completes'] is not True or recheck:
                        raise ComputerError('windows_evidence_changed')
                    from .windows_winapp_office import is_goal as office_goal, ACTION as office_actions, decode as office_decode
                    if office_goal(condition['selector']):
                        item, _ = office_decode(condition['selector'], condition['value'])
                        if pending['candidate'].get('action') != office_actions[item['source']['role']]:
                            raise ComputerError('windows_evidence_changed')
                        _office_completion(condition, result, operation)
                    else:
                        if pending['candidate'].get('action') != 'reviewed_control':
                            raise ComputerError('windows_evidence_changed')
                        _reviewed_completion(condition, result, operation)
                    completed.append(condition['id'])
                if result['state'] == 'unknown' or result['effects'] == 'unknown':
                    uncertain = True
                if (result['effects'] != 'none' and condition['id'] not in completed
                        and not (result['ok'] and pending['candidate']['action'] == 'reveal')):
                    unsealed.add(condition['id'])
                if condition['id'] in completed:
                    unsealed.discard(condition['id'])
                pending = None
            elif kind == 'complete':
                from .windows_winapp_office import is_goal as office_goal
                if office_goal(condition['selector']):
                    raise ComputerError('windows_evidence_changed')
                observed = _observation(event.get('observation'), condition['window'])
                if (recheck or pending is not None or latest_for != (condition['id'], False)
                        or latest != observed or not satisfied(observed, condition)):
                    raise ComputerError('windows_evidence_changed')
                completed.append(condition['id'])
                unsealed.discard(condition['id'])
            elif kind == 'observation':
                if pending is not None:
                    raise ComputerError('windows_evidence_changed')
                latest = _observation(event.get('observation'), condition['window'])
                latest_for = (condition['id'], recheck)
        if pending is not None:
            uncertain = True
        # A successful native call is not a sealed condition. A crash or read
        # failure before its complete event must never authorize replay.
        if contract == 1:
            uncertain |= bool(unsealed)
        outcome_path = attempt / 'outcome.json'
        if outcome_path.exists():
            outcome, _ = _load(outcome_path)
            if (outcome.get('events_sha256') != tail or outcome.get('events') != len(event_files)
                    or outcome.get('completed') != completed or outcome.get('actions') != actions
                    or outcome.get('state') not in {'verified', 'paused', 'failed', 'unknown'}
                    or outcome.get('ok') != (outcome.get('state') == 'verified')):
                raise ComputerError('windows_evidence_changed')
            uncertain |= outcome['state'] == 'unknown'
            if last_attention and (outcome.get('code') != 'windows_manual_attention'
                    or outcome['state'] != ('unknown' if uncertain else 'paused')):
                raise ComputerError('windows_interruption_invalid')
            if outcome['state'] == 'verified':
                final = outcome.get('final_observations')
                required = [c for i, c in enumerate(goal['conditions']) if c['retain'] or i == len(goal['conditions']) - 1]
                if (len(completed) != len(goal['conditions']) or type(final) is not dict
                        or set(final) != {c['id'] for c in required}):
                    raise ComputerError('windows_evidence_changed')
                for condition in required:
                    observation, observed_sha = _load(attempt / 'final' / (condition['id'] + '.json'))
                    if (observed_sha != final[condition['id']]
                            or not satisfied(_observation(observation, condition['window']), condition)):
                        raise ComputerError('windows_evidence_changed')
            last_outcome = outcome
        else:
            last_outcome = ({'state': 'unknown' if uncertain else 'paused', 'code': 'windows_manual_attention'}
                            if last_attention else None)
        if uncertain and number != len(attempts):
            raise ComputerError('windows_unknown_replayed')
        previous = _inventory(attempt)
        count += 1
    return {'task': task, 'task_sha256': task_sha, 'goal': goal, 'completed': completed,
            'previous': previous, 'attempts': count, 'actions': actions,
            'uncertain': uncertain, 'last_outcome': last_outcome,
            'unsealed_effects': sorted(unsealed), 'manual_attention': last_attention}


def _summary(root, scan):
    outcome = scan['last_outcome'] or {}
    state = 'unknown' if scan['uncertain'] else outcome.get('state', 'ready')
    result = {'ok': state == 'verified', 'state': state,
            'code': 'windows_unknown_requires_review' if scan['uncertain'] else outcome.get('code', 'windows_task_ready'),
            'root': str(root), 'attempt': scan['attempts'], 'completed': list(scan['completed']),
            'remaining': [c['id'] for c in scan['goal']['conditions'] if c['id'] not in scan['completed']],
            'cause_code': outcome.get('cause_code'),
            'eligible': not scan['uncertain'] and state != 'verified',
            'resumable': not scan['uncertain'] and state != 'verified'}
    if scan['task']['schema']==3:
        from .windows_visual_proposal import approved
        result['approval_required']=not approved(root,scan)
        if result['approval_required']:result['resumable']=False
    if scan.get('manual_attention'):
        result['manual_attention'] = deepcopy(scan['manual_attention'])
    if scan.get('unsealed_effects'):
        result.update(eligible=False, resumable=False, unsealed_effect=True)
    return result


def inspect(root):
    root = safe_root(root)
    with _own(root):
        scan = _scan(root)
        _verify_sources(scan)
        return _summary(root, scan)


def _verify_sources(scan):
    if scan['task']['schema']==3:
        from .windows_visual_proposal import verify
        verify(scan['task']['visual_proposal'],scan['goal'])
    if scan['task']['schema'] == 2:
        from .windows_handoff import verify
        verify(scan['goal'], scan['task']['sources'])


def load(root):
    """One locked, verified snapshot for restoring a task and its handoff."""
    root = safe_root(root)
    with _own(root):
        scan = _scan(root)
        _verify_sources(scan)
        return {'root': root, 'goal': deepcopy(scan['goal']),
                'sources': deepcopy(scan['task'].get('sources', [])),
                'provider_binding': deepcopy(scan['task'].get('provider_binding')),
                'summary': _summary(root, scan)}


def load_goal(root):
    """Return the immutable goal after checking its saved evidence and lease."""
    root = safe_root(root)
    with _own(root):
        scan = _scan(root)
        _verify_sources(scan)
        return deepcopy(scan['goal'])


def _result(result):
    if (type(result) is not dict or type(result.get('ok')) is not bool
            or result.get('state') not in {'verified', 'failed', 'unknown'}
            or result.get('effects') not in {'none', 'verified', 'observed', 'unknown'}
            or result['ok'] != (result['state'] == 'verified')
            or (result['state'] == 'unknown' and result['effects'] != 'unknown')
            or (result['state'] == 'failed' and result['effects'] != 'none')
            or (result['ok'] and result['effects'] not in {'verified', 'observed'})):
        raise ComputerError('windows_result_invalid')
    return result


@contextmanager
def _backend_cleanup(backend):
    try:
        yield
    finally:
        try:
            end = getattr(backend, 'end_run', None)
            if end is not None:
                end()
        except Exception:
            # The original immutable receipt or interruption remains authoritative.
            pass


@contextmanager
def _stage_guard(root):
    payload, _ = _load(root / 'task.json')
    binding = payload.get('stage_binding')
    if binding is None:
        yield
    else:
        from .windows_stages import dispatch_guard
        with dispatch_guard(root, binding):
            yield


def run(root, backend, *, stop=lambda: False, pause_after_step=False):
    root = safe_root(root)
    with _own(root), _stage_guard(root), _backend_cleanup(backend):
        scan = _scan(root)
        if scan['uncertain']:
            raise ComputerError('windows_unknown_requires_review')
        if scan.get('unsealed_effects'):
            # Preserve legacy history, but do not replay its unsealed effects.
            raise ComputerError('windows_unknown_requires_review')
        if scan['task'].get('provider_binding') != getattr(backend, 'provider_binding', None):
            raise ComputerError('windows_provider_binding_mismatch')
        from .windows_winapp_office import is_goal as office_goal
        if any(office_goal(c['selector']) for c in scan['goal']['conditions']):
            from .windows_winapp import OFFICE_PROVIDER_BINDING
            from .windows_auto import PROVIDER_BINDING as AUTO_PROVIDER_BINDING
            if scan['task'].get('provider_binding') not in (OFFICE_PROVIDER_BINDING, AUTO_PROVIDER_BINDING) or not scan['task'].get('stage_binding'):
                raise ComputerError('windows_office_stage_required')
        _verify_sources(scan)
        from .windows_visual_forms import is_goal as form_goal
        from .windows_reviewed_controls import is_goal as reviewed_goal
        if any(form_goal(c['selector']) or reviewed_goal(c['selector']) for c in scan['goal']['conditions']) and scan['task']['schema']!=3:
            raise ComputerError('windows_visual_approval_required')
        if scan['task']['schema']==3:
            from .windows_visual_continuation import existing
            if existing(root) is not None:raise ComputerError('windows_visual_continuation_exists')
            from .windows_visual_proposal import approved
            if not approved(root,scan):raise ComputerError('windows_visual_approval_required')
            from .windows_visual_proposal import verify
            proposal=verify(scan['task']['visual_proposal'],scan['goal'])
            if 'continuation' in proposal:
                from .windows_visual_continuation import assert_registered
                assert_registered(proposal,root)
                from .windows_visual_rebind import preflight
                preflight(proposal,backend,stop=stop)
        goal, completed = scan['goal'], list(scan['completed'])
        attempt = root / 'attempts' / f'{scan["attempts"] + 1:04}'
        attempt.mkdir(exist_ok=False)
        (attempt / 'events').mkdir()
        (attempt / 'operations').mkdir()
        start = {'schema': 1, 'number': scan['attempts'] + 1, 'previous': scan['previous'],
                 'task_sha256': scan['task_sha256'], 'interruption_contract': 1}
        _save(attempt / 'attempt.json', start)
        tail, event_count, action_count = digest(start), 0, 0
        actions = scan['actions']
        uncertain, cause_code = False, None
        unsealed = set(scan.get('unsealed_effects', []))
        last_action = None

        def check():
            if stop():
                raise ComputerError('windows_task_cancelled')

        def event(kind, condition, **values):
            nonlocal tail, event_count
            value = {'kind': kind, 'condition_id': condition['id'], 'previous': tail, **values}
            event_count += 1
            tail = _save(attempt / 'events' / f'{event_count:04}.json', value)
            if kind == 'complete' or values.get('completes'):
                unsealed.discard(condition['id'])
            elif (kind == 'result' and values['result']['effects'] != 'none' and condition['id'] not in completed
                    and not (values['result']['ok'] and last_action == 'reveal')):
                unsealed.add(condition['id'])
            return value

        def interrupt(condition, raw):
            nonlocal uncertain
            uncertain |= condition['id'] in unsealed
            info = _attention(raw, condition['window'])
            event('interruption', condition, attention=info,
                  **({'purpose': 'recheck'} if condition['id'] in completed else {}))
            raise ComputerError('windows_manual_attention')

        def observe(condition):
            check()
            conditional = getattr(backend, 'observe_condition', None)
            targeted = getattr(backend, 'observe_target', None)
            if conditional is not None:
                raw = conditional(deepcopy(condition), stop=stop)
            elif targeted is not None:
                raw = targeted(condition['window'], deepcopy(condition['selector']), stop=stop)
            else:
                raw = backend.observe(condition['window'], stop=stop)
            if type(raw) is dict and raw.get('blocked_reason') in _ATTENTION_REASONS:
                interrupt(condition, raw)
            value = _observation(raw, condition['window'])
            check()
            return value

        def perform(condition, observed, *, recheck=False):
            """Dispatch one durable intent; False means a bounded safe refresh."""
            nonlocal actions, action_count, uncertain, cause_code, last_action
            if actions >= goal['max_steps']:
                raise ComputerError('windows_action_limit')
            candidates = backend.candidates(deepcopy(observed), deepcopy(condition))
            if (type(candidates) is not list or len(candidates) != 1
                    or type(candidates[0]) is not dict
                    or set(candidates[0]) != {'control_id', 'action', 'value'}):
                raise ComputerError('windows_no_safe_action' if candidates == [] else 'windows_action_ambiguous')
            candidate = candidates[0]
            last_action = candidate['action']
            if (candidate['control_id'] is not None and not _text(candidate['control_id'], 128)) or not _text(candidate['action'], 64) or not _text(candidate['value'], 4000, empty=True):
                raise ComputerError('windows_action_invalid')
            controls = [c for c in observed['controls'] if c['id'] == candidate['control_id']]
            if candidate['control_id'] is not None and len(controls) != 1:
                raise ComputerError('windows_action_unobserved')
            target = locate(observed, condition['selector'])
            reveal = candidate['action'] == 'reveal'
            if recheck and not reveal:
                raise ComputerError('windows_completed_action_refused')
            if reveal:
                if (target is None or target['visible'] or not target['enabled']
                        or candidate['control_id'] != target['id'] or candidate['value'] != ''):
                    raise ComputerError('windows_reveal_invalid')
            elif controls and not controls[0]['visible']:
                raise ComputerError('windows_action_unobserved')
            current = observe(condition)
            if digest(current) != digest(observed):
                return False
            check()
            action_count += 1
            actions += 1
            operation = attempt / 'operations' / f'{action_count:04}'
            operation.mkdir(exist_ok=False)
            purpose = {'purpose': 'recheck'} if recheck else {}
            intent = event('intent', condition, candidate=candidate, before_sha256=digest(observed), **purpose)
            uncertain = True
            try:
                result = _result(backend.act(condition['window'], deepcopy(controls[0]) if controls else None,
                    candidate['action'], candidate['value'], digest(observed), operation, stop=stop))
                result = _safe_interruption_result(result, condition['window'])
                from .windows_auto import PROVIDER_BINDING as AUTO_PROVIDER_BINDING, verify_result
                if scan['task'].get('provider_binding') == AUTO_PROVIDER_BINDING:
                    verify_result(operation, condition, candidate, digest(observed), result)
            except Exception:
                result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_backend_interrupted'}
            from .windows_reviewed_controls import is_goal as reviewed_goal
            from .windows_winapp_office import is_goal as office_goal, ACTION as office_actions, decode as office_decode
            completes = False
            if reviewed_goal(condition['selector']) and result['ok']:
                try:
                    if recheck or candidate['action'] != 'reviewed_control':
                        raise ComputerError('windows_reviewed_completion_invalid')
                    _reviewed_completion(condition, result, operation)
                    completes = True
                except Exception:
                    result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_reviewed_completion_invalid'}
            if office_goal(condition['selector']) and result['ok']:
                try:
                    item, _ = office_decode(condition['selector'], condition['value'])
                    if recheck or candidate['action'] != office_actions[item['source']['role']]:
                        raise ComputerError('windows_office_completion_invalid')
                    _office_completion(condition, result, operation)
                    completes = True
                except Exception:
                    result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_office_completion_invalid'}
            event('result', condition, intent_sha256=digest(intent), result=result,
                  operation_sha256=_inventory(operation), **({'completes': True} if completes else {}), **purpose)
            if completes:
                completed.append(condition['id'])
            uncertain = result['state'] == 'unknown' or result['effects'] == 'unknown'
            if not result['ok']:
                raw_code = result.get('code')
                cause_code = raw_code if type(raw_code) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,79}', raw_code) else 'windows_backend_failed'
            else:
                cause_code = None
            if (not result['ok'] and type(result.get('observation')) is dict
                    and result['observation'].get('blocked_reason') in _ATTENTION_REASONS):
                interrupt(condition, result['observation'])
            if uncertain:
                raise ComputerError('windows_effect_unknown')
            if not result['ok']:
                if result['effects'] == 'none' and result.get('code') in _TRANSIENT:
                    return False
                raise ComputerError('windows_action_failed')
            if reveal:
                # ScrollItem succeeding is not proof that its target is visible.
                after = observe(condition)
                event('observation', condition, observation=after, **purpose)
                revealed = locate(after, condition['selector'])
                if revealed is None or not revealed['visible'] or not revealed['enabled']:
                    raise ComputerError('windows_reveal_not_visible')
            return 'completed' if completes else True

        def recheck(condition, failure):
            refreshes = 0
            while True:
                observed = observe(condition)
                event('observation', condition, observation=observed, purpose='recheck')
                if satisfied(observed, condition):
                    return observed
                target = locate(observed, condition['selector'])
                # Visibility may be recovered; changed completed state is never repaired.
                if (target is None or target['visible'] or not target['enabled']
                        or not _state_matches(target, condition)
                        or getattr(backend, 'observe_target', None) is None):
                    raise ComputerError(failure)
                if not perform(condition, observed, recheck=True):
                    refreshes += 1
                    if refreshes > 2:
                        raise ComputerError('windows_reobservation_limit')

        try:
            check()
            begin = getattr(backend, 'begin_run', None)
            if begin is not None:
                begin()
            check()
            # Retained earlier conditions must still hold before another effect.
            for condition in goal['conditions'][:len(completed)]:
                if condition['retain']:
                    recheck(condition, 'windows_completed_state_changed')
            while len(completed) < len(goal['conditions']):
                condition = goal['conditions'][len(completed)]
                refreshes = 0
                while True:
                    observed = observe(condition)
                    event('observation', condition, observation=observed)
                    if satisfied(observed, condition):
                        from .windows_winapp_office import is_goal as office_goal
                        if office_goal(condition['selector']):
                            raise ComputerError('windows_office_target_changed')
                        event('complete', condition, observation=observed)
                        completed.append(condition['id'])
                        if pause_after_step() if callable(pause_after_step) else pause_after_step:
                            raise ComputerError('windows_task_paused')
                        break
                    if condition['id'] in unsealed:
                        raise ComputerError('windows_postcondition_changed')
                    dispatched = perform(condition, observed)
                    if dispatched == 'completed':
                        if pause_after_step() if callable(pause_after_step) else pause_after_step:
                            raise ComputerError('windows_task_paused')
                        break
                    if not dispatched:
                        refreshes += 1
                        if refreshes > 2:
                            raise ComputerError('windows_reobservation_limit')
                    # A successful native call is not proof of the goal. Reobserve.
            final = {}
            (attempt / 'final').mkdir()
            for index, condition in enumerate(goal['conditions']):
                if condition['retain'] or index == len(goal['conditions']) - 1:
                    observation = recheck(condition, 'windows_final_state_changed')
                    final[condition['id']] = _save(attempt / 'final' / (condition['id'] + '.json'), observation)
            outcome = {'ok': True, 'state': 'verified', 'code': 'windows_conditions_verified', 'final_observations': final}
        except Exception as exc:
            code = str(exc) if isinstance(exc, ComputerError) else 'windows_task_failed'
            uncertain |= bool(unsealed)
            if uncertain and cause_code is None:
                cause_code = code
            outcome = {'ok': False, 'state': 'unknown' if uncertain else 'paused' if code in {'windows_task_paused', 'windows_task_cancelled', 'windows_manual_attention'} else 'failed', 'code': code}
        outcome.update(events_sha256=tail, events=event_count, completed=completed, actions=actions)
        if cause_code is not None:
            outcome['cause_code'] = cause_code
        _save(attempt / 'outcome.json', outcome)
        summary = _summary(root, _scan(root))
        return summary
