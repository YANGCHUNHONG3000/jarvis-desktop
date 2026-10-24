"""Explicit source clarification for a proven, undispatched capability request."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

from .computer import BASE, ComputerError, read_record, safe_root, write_new
from .computer_goal_adapters import TASK_BASE

_CLAIM = 'source-clarification-claim.json'


def _hash(path):
    read_record(path)  # Same size, link and JSON constraints as the normal reader.
    return sha256(Path(path).read_bytes()).hexdigest()


def _parent(run, base, computer):
    from .capability_router import inspect_saved
    path = safe_root(run)
    if (path.parent.name != 'goals' or path.parent.parent.parent != computer
            or re.fullmatch(r'computer-[0-9a-f]{32}', path.parent.parent.name) is None
            or not path.is_dir()):
        raise ComputerError('capability_clarification_invalid')
    # Reject other schemas before inspect_saved, preventing recursive bindings.
    approval = path.name.removesuffix('.capability')
    if re.fullmatch(r'[0-9a-f]{64}', approval) is None:
        raise ComputerError('capability_clarification_invalid')
    plan = read_record(path.parent / (approval + '.json'))
    if type(plan) is not dict or plan.get('schema') != 2:
        raise ComputerError('capability_clarification_invalid')
    outcome = inspect_saved(path, task_base=base, computer_base=computer)
    if (outcome.get('state') != 'needs_input' or outcome.get('code') != 'capability_source_ambiguous'
            or outcome.get('before_start') is not True):
        raise ComputerError('capability_clarification_unavailable')
    allowed = {'claim.json', 'source-selection.json', 'outcome.json', _CLAIM, 'owner.lock'}
    if any(p.name not in allowed or not p.is_file() or safe_root(p) != p for p in path.iterdir()):
        raise ComputerError('capability_clarification_invalid')
    binding = {'run': str(path), 'parent_plan_sha256': _hash(path.parent / (approval + '.json')),
               'parent_outcome_sha256': _hash(path / 'outcome.json'),
               'parent_selection_sha256': _hash(path / 'source-selection.json')}
    return path, plan, binding


def _selected(plan, source_id):
    ids = [f'S{i}' for i in range(1, len(plan['source_refs']) + 1)]
    if type(source_id) is not str or source_id not in ids:
        raise ComputerError('capability_clarification_source_invalid')
    return plan['source_refs'][ids.index(source_id)]


def prepare(session, run, source_id, *, task_base=TASK_BASE):
    """No model or effects: narrow the original request to one approved source."""
    from .capability_router import prepare as prepare_capability
    base = safe_root(task_base)
    path, original, binding = _parent(run, base, session.root.parent)
    if (path / _CLAIM).exists():
        raise ComputerError('capability_clarification_claimed')
    source = _selected(original, source_id)
    binding['source_id'] = source_id
    return prepare_capability(session, original['instruction'], source_ref=deepcopy(source),
                              task_base=base, _clarification=binding)


def _claim_value(plan, run, base):
    return {'schema': 1, 'approval': plan['sha256'], 'run': str(run),
            'task_root': str(base / ('task-' + plan['request_id']))}


def verify(plan, run, base, computer, *, require_claim=False):
    binding = plan.get('clarification')
    keys = {'run', 'parent_plan_sha256', 'parent_outcome_sha256', 'parent_selection_sha256', 'source_id'}
    if type(binding) is not dict or set(binding) != keys or type(binding['run']) is not str:
        raise ComputerError('capability_clarification_invalid')
    path, original, expected = _parent(binding['run'], base, computer)
    expected['source_id'] = binding['source_id']
    if (binding != expected or plan.get('schema') != 3
            or plan.get('instruction') != original['instruction']
            or plan.get('source_ref') != _selected(original, binding['source_id'])
            or plan.get('task_base') != original['task_base']):
        raise ComputerError('capability_clarification_changed')
    claim = path / _CLAIM
    if claim.exists():
        if read_record(claim) != _claim_value(plan, run, base):
            raise ComputerError('capability_clarification_claimed')
    elif require_claim:
        raise ComputerError('capability_clarification_claim_missing')
    return path


def claim(plan, run, base, computer):
    """Single durable claim precedes all new model/Office work."""
    from .office_workflow import _owner
    path = verify(plan, run, base, computer)
    with _owner(path):
        verify(plan, run, base, computer)
        record = path / _CLAIM
        if record.exists():
            raise ComputerError('capability_clarification_claimed')
        write_new(record, _claim_value(plan, run, base))


def pending(*, computer_base=BASE, task_base=TASK_BASE, limit=20):
    """Bounded, read-only discovery; partial or unknown runs are never offered."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ComputerError('capability_clarification_limit_invalid')
    computer, base = safe_root(computer_base), safe_root(task_base)
    if not computer.is_dir():
        return []
    sessions = sorted((p for p in computer.iterdir() if re.fullmatch(r'computer-[0-9a-f]{32}', p.name)),
                      key=lambda p: p.stat().st_mtime_ns, reverse=True)[:200]
    result, examined = [], 0
    for session in sessions:
        try:
            goals = safe_root(session / 'goals')
            candidates = sorted(goals.glob('*.capability'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        except (ValueError, OSError):
            continue
        for run in candidates:
            examined += 1
            if examined > 400:
                return result
            try:
                safe_root(run)
                raw = read_record(run / 'outcome.json')
                if (type(raw) is not dict or raw.get('code') != 'capability_source_ambiguous'
                        or (run / _CLAIM).exists()):
                    continue
                path, plan, _ = _parent(run, base, computer)
                reason = read_record(path / 'source-selection.json')['reason']
                result.append({'run': str(path), 'instruction': plan['instruction'], 'reason': reason,
                    'sources': [{'id': f'S{i}', 'filename': Path(r['artifact']).name,
                                 'format': Path(r['artifact']).suffix.lower()}
                                for i, r in enumerate(plan['source_refs'], 1)]})
                if len(result) >= limit:
                    return result
            except (ValueError, OSError, KeyError, TypeError):
                continue
    return result
