"""Resolve explicit generated Office outputs without a file picker or model."""
from pathlib import Path
import re

from .artifact_source import prepare_artifact_source
from .computer import BASE, ComputerError, read_record, safe_root
from .computer_goal_adapters import TASK_BASE
from .office_scope import MAX_EDIT_FILES


def select_outputs(records, *, base=TASK_BASE):
    if type(records) not in (list, tuple) or not 1 <= len(records) <= MAX_EDIT_FILES:
        raise ComputerError('office_handoff_requires_one_to_four_outputs')
    refs, seen = [], set()
    for item in records:
        if type(item) is not dict or set(item) != {'path', 'sha256'}:
            raise ComputerError('office_handoff_record_invalid')
        if type(item['path']) is not str:
            raise ComputerError('office_handoff_path_invalid')
        path = Path(item['path'])
        if not path.is_absolute() or path.suffix.lower() not in {'.docx', '.xlsx'} or path.resolve() in seen:
            raise ComputerError('office_handoff_path_invalid')
        seen.add(path.resolve())
        refs.append(prepare_artifact_source(path, item['sha256'], base))
    return refs


def verify_references(files, refs, *, base=TASK_BASE):
    if type(refs) not in (list, tuple) or len(refs) != len(files):
        raise ComputerError('office_handoff_reference_mismatch')
    records = []
    for path, ref in zip(files, refs):
        if type(ref) is not dict or ref.get('artifact') != str(Path(path).absolute()):
            raise ComputerError('office_handoff_reference_mismatch')
        records.append({'path': ref['artifact'], 'sha256': ref.get('sha256')})
    if select_outputs(records, base=base) != list(refs):
        raise ComputerError('office_handoff_reference_changed')


def latest_generated_outputs(*, base=BASE, task_base=TASK_BASE, stop=lambda: False):
    """Find one recent batch, then verify it; never substitute an older invalid batch."""
    base = safe_root(base)
    task_base = safe_root(task_base)
    outcomes = []
    if base.exists():
        sessions = sorted(base.glob('computer-*'),
                          key=lambda p: p.stat().st_mtime_ns, reverse=True)[:20]
        for session in sessions:
            if stop():
                raise ComputerError('office_handoff_cancelled')
            if safe_root(session).parent != base:
                raise ComputerError('office_handoff_history_invalid')
            goals = session / 'goals'
            if goals.exists():
                outcomes.extend((p.stat().st_mtime_ns, 'computer', p)
                                for p in goals.glob('*.run/outcome.json'))
                outcomes.extend((p.stat().st_mtime_ns, 'computer', p)
                                for p in goals.glob('*.capability/outcome.json'))

    # Also inspect the normal assistant-task store. It is deliberately scoped to
    # direct task-<hex> children; this is not a search of arbitrary user files.
    if task_base.exists():
        tasks = sorted((p for p in task_base.iterdir()
                        if re.fullmatch(r'task-[0-9a-f]{32}', p.name)),
                       key=lambda p: p.lstat().st_mtime_ns, reverse=True)[:100]
        for task in tasks:
            if stop():
                raise ComputerError('office_handoff_cancelled')
            if safe_root(task).parent != task_base:
                raise ComputerError('office_handoff_history_invalid')
            result_path = task / 'result.json'
            if not result_path.exists():
                continue
            try:
                if safe_root(result_path).parent != task:
                    raise ComputerError('office_handoff_history_invalid')
                raw = read_record(result_path)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if type(raw) is not dict:
                continue
            original_claim = raw.get('state') == 'succeeded'
            timestamp = result_path.stat().st_mtime_ns
            kind = 'assistant'
            # First gather cheap metadata only. Full continuation validation can
            # traverse bounded historical chains, so defer it to the winner.
            pointer_times = []
            for pointer_name in ('resume-latest.json', 'skill-resume-latest.json'):
                pointer = task / pointer_name
                if (pointer.exists() or pointer.is_symlink()
                        or (hasattr(pointer, 'is_junction') and pointer.is_junction())):
                    pointer_times.append((pointer.lstat().st_mtime_ns, pointer_name))
            if pointer_times:
                pointer_time, pointer_name = max(pointer_times)
                try:
                    pointer_path = task / pointer_name
                    if safe_root(pointer_path).parent != task:
                        raise ValueError('office_handoff_history_invalid')
                    pointer_record = read_record(pointer_path)
                    if type(pointer_record) is not dict:
                        raise ValueError('office_handoff_history_invalid')
                    attempt_id = pointer_record.get('attempt_id')
                    if type(attempt_id) is not str or re.fullmatch(r'[0-9a-f]{32}', attempt_id) is None:
                        raise ValueError('office_handoff_history_invalid')
                    prefix = 'skill-resume-' if pointer_name.startswith('skill-') else 'resume-'
                    attempt_path = task / (prefix + attempt_id)
                    if safe_root(attempt_path).parent != task:
                        raise ValueError('office_handoff_history_invalid')
                    outcome_path = attempt_path / 'outcome.json'
                    if safe_root(outcome_path).parent != attempt_path:
                        raise ValueError('office_handoff_history_invalid')
                    attempt_record = read_record(outcome_path)
                    if type(attempt_record) is not dict:
                        raise ValueError('office_handoff_history_invalid')
                    state = attempt_record.get('state')
                    if state == 'succeeded':
                        kind = 'assistant_continuation'
                        timestamp = pointer_time
                    elif state in {'cancelled', 'timed_out', 'blocked', 'failed', 'partial', 'unknown'}:
                        if not original_claim:
                            continue
                    else:
                        raise ValueError('office_handoff_history_invalid')
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    # Preserve the timestamp as an invalid candidate. If it is
                    # newest, the caller must see the integrity problem rather
                    # than silently using an older task's output.
                    kind = 'assistant_invalid_continuation'
                    timestamp = pointer_time
                if kind == 'assistant' and not original_claim:
                    continue
            elif not original_claim:
                continue
            outcomes.append((timestamp, kind, task))

    for _, kind, path in sorted(outcomes, key=lambda item:(item[0], str(item[2])), reverse=True)[:100]:
        if stop():
            raise ComputerError('office_handoff_cancelled')
        if kind == 'assistant_invalid_continuation':
            raise ComputerError('office_handoff_history_invalid')
        if kind in {'assistant', 'assistant_continuation'}:
            from .task_history import inspect_task
            try:
                inspection = inspect_task(path, task_base)
            except Exception as exc:
                raise ComputerError('office_handoff_history_invalid') from exc
            if kind == 'assistant_continuation':
                continuation = inspection.get('continuation')
                if type(continuation) is not dict or continuation.get('state') != 'succeeded':
                    raise ComputerError('office_handoff_history_invalid')
            elif inspection.get('state') != 'succeeded':
                raise ComputerError('office_handoff_history_invalid')
            artifacts = inspection.get('artifacts')
            if type(artifacts) is not list:
                raise ComputerError('office_handoff_history_invalid')
            office = [item for item in artifacts if type(item) is dict
                      and type(item.get('path')) is str
                      and Path(item['path']).suffix.lower() in {'.docx', '.xlsx'}]
            if not office:
                continue
            if len(office) > MAX_EDIT_FILES:
                raise ComputerError('office_handoff_history_invalid')
            records = [{'path': item['path'], 'sha256': item['sha256']}
                       for item in office]
            try:
                select_outputs(records, base=task_base)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ComputerError('office_handoff_history_invalid') from exc
            return records
        if safe_root(path).parent.parent.parent.parent != base:
            raise ComputerError('office_handoff_history_invalid')
        if path.parent.name.endswith('.capability'):
            from .capability_router import inspect_saved
            outcome = inspect_saved(path.parent, task_base=task_base, computer_base=base)
            artifacts = outcome['artifacts']
            if not artifacts:
                continue
            if outcome['ok'] is not True or not 1 <= len(artifacts) <= MAX_EDIT_FILES:
                raise ComputerError('office_handoff_history_invalid')
            records = [{'path': item['path'], 'sha256': item['sha256']} for item in artifacts]
            select_outputs(records, base=task_base)
            return records
        outcome = read_record(path)
        if type(outcome) is not dict:
            raise ComputerError('office_handoff_history_invalid')
        if outcome.get('engine') != 'observed_agent':
            continue
        artifacts = outcome.get('artifacts')
        if type(artifacts) is not list:
            raise ComputerError('office_handoff_history_invalid')
        if not artifacts:
            continue
        if (outcome.get('run') != str(path.parent) or type(outcome.get('ok')) is not bool
                or outcome['ok'] != (outcome.get('state') == 'verified')
                or outcome.get('state') not in {'verified','partial','stopped'}
                or type(artifacts) is not list or not 1 <= len(artifacts) <= MAX_EDIT_FILES
                or any(type(a) is not dict or 'path' not in a or 'sha256' not in a for a in artifacts)):
            raise ComputerError('office_handoff_history_invalid')
        records = [{'path':a['path'], 'sha256':a['sha256']} for a in artifacts]
        select_outputs(records, base=task_base)
        return records
    return []
