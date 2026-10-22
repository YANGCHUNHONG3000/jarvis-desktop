"""Resolve explicit generated Office outputs without a file picker or model."""
from pathlib import Path

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
    if not base.exists():
        return []
    sessions = sorted(base.glob('computer-*'), key=lambda p:p.stat().st_mtime_ns, reverse=True)[:20]
    outcomes = []
    for session in sessions:
        if stop():
            raise ComputerError('office_handoff_cancelled')
        if safe_root(session).parent != base:
            raise ComputerError('office_handoff_history_invalid')
        outcomes.extend((p.stat().st_mtime_ns, p) for p in (session/'goals').glob('*.run/outcome.json'))
    for _, path in sorted(outcomes, key=lambda item:(item[0], str(item[1])), reverse=True)[:100]:
        if stop():
            raise ComputerError('office_handoff_cancelled')
        if safe_root(path).parent.parent.parent.parent != base:
            raise ComputerError('office_handoff_history_invalid')
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
