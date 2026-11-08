"""Consume one explicitly approved Office recovery draft after its exact receipt.

This UI helper neither approves nor executes work. The caller verifies artifacts
before passing its on_artifacts records; execution and recovery gates stay in the
existing engine. Bindings belong to one panel/tracker and are consumed once.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import re

from .computer import digest, read_record
from .computer_interaction import validate_goal

_SHA = re.compile(r'[0-9a-f]{64}')
_PLAN_KEYS = {'schema', 'mode', 'instruction', 'goal', 'max_actions',
              'max_seconds', 'prepared_at', 'sha256', 'recovery'}
_RECOVERY_KEYS = {'version', 'parent_run', 'parent_evidence_sha256', 'depth',
                  'completed_edits', 'remaining_edits'}


@dataclass
class _Binding:
    owner: object
    tracker: object
    session_root: str
    ticket: dict
    plan: dict
    run: str
    consumed: bool = False


def _idle(parent):
    return (not parent.closed and not parent.running and not parent.stopped.is_set()
            and not parent.external_busy())


def _checked_plan(session_root, plan):
    if (type(plan) is not dict or set(plan) != _PLAN_KEYS or plan['schema'] != 3
            or plan['mode'] != 'agent' or plan['max_actions'] != 12 or plan['max_seconds'] != 360
            or type(plan['sha256']) is not str or not _SHA.fullmatch(plan['sha256'])
            or type(plan['instruction']) is not str or not plan['instruction'].strip()
            or type(plan['prepared_at']) not in (int, float) or not math.isfinite(plan['prepared_at'])
            or digest({k:v for k,v in plan.items() if k != 'sha256'}) != plan['sha256']):
        raise ValueError('recovery_instruction_plan_invalid')
    goal = validate_goal(plan['goal'])
    if set(goal) != {'kind', 'browser', 'edits'} or goal['browser'] is not None or not goal['edits']:
        raise ValueError('recovery_instruction_office_only')
    recovery = plan['recovery']
    identifiers = {item['id'] for item in goal['edits']}
    if (type(recovery) is not dict or set(recovery) != _RECOVERY_KEYS
            or type(recovery['version']) is not int or recovery['version'] != 1
            or type(recovery['depth']) is not int or not 1 <= recovery['depth'] <= 4
            or type(recovery['parent_evidence_sha256']) is not str
            or not _SHA.fullmatch(recovery['parent_evidence_sha256'])
            or any(type(recovery[key]) is not list or any(type(item) is not str for item in recovery[key])
                   for key in ('completed_edits', 'remaining_edits'))):
        raise ValueError('recovery_instruction_recovery_invalid')
    done, remaining = recovery['completed_edits'], recovery['remaining_edits']
    if (len(set(done)) != len(done) or len(set(remaining)) != len(remaining)
            or set(done) & set(remaining) or set(done) | set(remaining) != identifiers):
        raise ValueError('recovery_instruction_recovery_invalid')
    goals = Path(session_root) / 'goals'
    prior = Path(recovery['parent_run'])
    if (not prior.is_absolute() or prior.parent != goals or prior.suffix != '.run'
            or not _SHA.fullmatch(prior.stem)
            or any(Path(item['source_root']).parent != Path(session_root) / 'edit-inputs'
                   for item in goal['edits'])
            or read_record(goals / (plan['sha256'] + '.json')) != plan):
        raise ValueError('recovery_instruction_plan_changed')
    return str(goals / (plan['sha256'] + '.run'))


def capture(parent):
    """Capture only the current unchanged, persisted, pure Office recovery plan."""
    try:
        if not _idle(parent) or parent.session is None:
            return None
        tracker = parent.instruction_draft
        ticket = tracker.snapshot()
        plan = deepcopy(parent.plan)
        session_root = str(parent.session.root)
        if (not Path(session_root).is_absolute() or type(ticket) is not dict
                or set(ticket) != {'revision', 'text'} or type(ticket['revision']) is not int
                or ticket['revision'] < 0 or not tracker.unchanged(ticket)
                or type(plan) is not dict or ticket['text'] != plan.get('instruction')):
            return None
        run = _checked_plan(session_root, plan)
        return _Binding(parent, tracker, session_root, deepcopy(ticket), plan, run)
    except Exception:
        return None


def _output_pairs(records):
    if type(records) not in (list, tuple) or not 1 <= len(records) <= 4:
        raise ValueError('recovery_instruction_outputs_invalid')
    pairs = []
    for item in records:
        if (type(item) is not dict or type(item.get('path')) is not str
                or not Path(item['path']).is_absolute() or type(item.get('sha256')) is not str
                or not _SHA.fullmatch(item['sha256'])):
            raise ValueError('recovery_instruction_outputs_invalid')
        pairs.append((item['path'], item['sha256']))
    if len({path for path, _ in pairs}) != len(pairs):
        raise ValueError('recovery_instruction_outputs_invalid')
    return set(pairs)


def complete(parent, binding, records):
    """Clear only this exact successful recovery's still-current instruction."""
    try:
        if (type(binding) is not _Binding or binding.consumed or binding.owner is not parent
                or not _idle(parent) or parent.session is None
                or str(parent.session.root) != binding.session_root
                or parent.instruction_draft is not binding.tracker
                or not binding.tracker.unchanged(binding.ticket)
                or parent.plan is not None and parent.plan != binding.plan
                or _checked_plan(binding.session_root, binding.plan) != binding.run):
            return False
        run = Path(binding.run)
        claim, outcome = read_record(run / 'claim.json'), read_record(run / 'outcome.json')
        if (claim.get('approval') != binding.plan['sha256'] or claim.get('mode') != 'agent'
                or outcome.get('ok') is not True
                or outcome.get('state') != 'verified' or outcome.get('code') != 'objective_verified'
                or outcome.get('engine') != 'observed_agent' or outcome.get('run') != binding.run
                or outcome.get('recovery') != binding.plan['recovery']
                or _output_pairs(outcome.get('artifacts')) != _output_pairs(records)
                or len(records) != len(binding.plan['goal']['edits'])):
            return False
        binding.consumed = True
        parent.complete_instruction(deepcopy(binding.ticket), 'execute', deepcopy(outcome))
        return binding.tracker.snapshot()['text'] == ''
    except Exception:
        return False
