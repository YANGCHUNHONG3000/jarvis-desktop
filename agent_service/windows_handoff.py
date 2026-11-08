"""Bind an explicitly reviewed next Windows goal to a verified saved copy.

Only immutable local evidence is read here. Capturing a source never resumes its
task, opens Office, or treats the next goal as approved for execution.
"""
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

from .computer import ComputerError, digest, safe_root
from . import windows_document_copy as copies
from . import windows_file_dialog as files
from . import windows_task as wt
from .windows_document_lifecycle import SAVE_SELECTOR
from .windows_file_actions import READONLY_SELECTOR

_STACK = ContextVar('windows_handoff_stack', default=())
_SHA = re.compile(r'[a-f0-9]{64}')
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_FIELDS = {'parent_root', 'task_sha256', 'evidence_sha256', 'save_id',
           'token_sha256', 'receipt_sha256', 'final', 'target_id'}
_CARRY_FIELDS = {'kind', 'parent_root', 'task_sha256', 'evidence_sha256',
                 'source_id', 'source_sha256', 'token_sha256', 'final', 'target_id'}


def _identifier(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _local_absolute(value):
    return (type(value) is str and bool(value) and len(value) <= 3000
            and Path(value).is_absolute() and '..' not in Path(value).parts
            and not value.startswith(('\\\\', '//')) and ':' not in value[2:]
            and not any(ord(c) < 32 for c in value))


def validate(goal, sources):
    """Validate shape and target mapping without reading files or evidence."""
    wt.validate_goal(goal)
    if sources is None:
        return
    if type(sources) is not list or not 1 <= len(sources) <= wt.MAX_CONDITIONS:
        raise ComputerError('windows_handoff_sources_invalid')
    targets, seen = {c['id']: c for c in goal['conditions']}, set()
    for ref in sources:
        carried = type(ref) is dict and set(ref) == _CARRY_FIELDS and ref.get('kind') == 'carried'
        if (type(ref) is not dict or (set(ref) != _FIELDS and not carried)):
            raise ComputerError('windows_handoff_reference_invalid')
        if (not _identifier(ref['source_id' if carried else 'save_id']) or not _identifier(ref['target_id'])
                or ref['target_id'] in seen
                or not _local_absolute(ref['parent_root'])
                or any(type(ref[key]) is not str or _SHA.fullmatch(ref[key]) is None
                       for key in ('task_sha256', 'evidence_sha256', 'token_sha256',
                                   'source_sha256' if carried else 'receipt_sha256'))):
            raise ComputerError('windows_handoff_reference_invalid')
        final = ref['final']
        if (type(final) is not dict or set(final) != {'path', 'sha256', 'size'}
                or not _local_absolute(final['path'])
                or Path(final['path']).suffix.casefold() not in {'.docx', '.xlsx'}
                or type(final['sha256']) is not str or _SHA.fullmatch(final['sha256']) is None
                or type(final['size']) is not int or not 0 < final['size'] <= files.MAX_FILE):
            raise ComputerError('windows_handoff_reference_invalid')
        target = targets.get(ref['target_id'])
        if (target is None or target['selector'] not in (files.OPEN_DOCUMENT_SELECTOR, READONLY_SELECTOR)
                or target['state'] != 'value'):
            raise ComputerError('windows_handoff_target_invalid')
        seen.add(ref['target_id'])


def capture(parent_root, save_id, *, target_id):
    """Capture a save or fully completed bound open under its task read lease."""
    if (not _identifier(save_id) or not _identifier(target_id)
            or not isinstance(parent_root, (str, Path)) or not _local_absolute(str(parent_root))):
        raise ComputerError('windows_handoff_reference_invalid')
    root = safe_root(parent_root)
    stack = _STACK.get()
    if root in stack:
        raise ComputerError('windows_handoff_cycle')
    if len(stack) >= 4:
        raise ComputerError('windows_handoff_depth_limit')
    context = _STACK.set((*stack, root))
    try:
        with wt._own(root):
            scan = wt._scan(root)
            if scan['uncertain'] or (scan['last_outcome'] or {}).get('state') == 'unknown':
                raise ComputerError('windows_handoff_source_unknown')
            task, goal = scan['task'], scan['goal']
            if task.get('schema') == 2:
                if task.get('sources') is None:
                    raise ComputerError('windows_handoff_sources_invalid')
                verify(goal, task['sources'])
            source = next((c for c in goal['conditions'] if c['id'] == save_id), None)
            if (source is not None and source['selector'] in (files.OPEN_DOCUMENT_SELECTOR, READONLY_SELECTOR)
                    and source['state'] == 'value' and task.get('schema') == 2):
                if (save_id not in scan['completed']
                        or (scan['last_outcome'] or {}).get('state') != 'verified'):
                    raise ComputerError('windows_handoff_source_incomplete')
                matches = [ref for ref in task['sources'] if ref['target_id'] == save_id]
                if len(matches) != 1:
                    raise ComputerError('windows_handoff_source_required')
                # verify() above checks the exact stored binding. Do not replace it
                # with a fresh ancestor capture that could renew changed evidence.
                original = matches[0]
                if files.decode_token(source['value']) != original['final']:
                    raise ComputerError('windows_handoff_target_changed')
                return {'kind': 'carried', 'parent_root': str(root),
                        'task_sha256': scan['task_sha256'], 'evidence_sha256': wt._inventory(root),
                        'source_id': save_id, 'source_sha256': digest(original),
                        'token_sha256': sha256(source['value'].encode()).hexdigest(),
                        'final': deepcopy(original['final']), 'target_id': target_id}
            if (source is None or source['selector'] != SAVE_SELECTOR
                    or source['state'] != 'value' or save_id not in scan['completed']):
                raise ComputerError('windows_handoff_save_required')
            token = source['value']
            receipt = copies.saved_receipt(token)
            if receipt is None:
                raise ComputerError('windows_handoff_receipt_required')
            return {'parent_root': str(root), 'task_sha256': scan['task_sha256'],
                    'evidence_sha256': wt._inventory(root), 'save_id': save_id,
                    'token_sha256': sha256(token.encode()).hexdigest(),
                    'receipt_sha256': digest(receipt), 'final': deepcopy(receipt['final']),
                    'target_id': target_id}
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ComputerError('windows_handoff_source_invalid') from exc
    finally:
        _STACK.reset(context)


def verify(goal, sources):
    """Rebuild every binding, then revalidate the exact child open token."""
    validate(goal, sources)
    for ref in sources or []:
        source_id = ref['source_id'] if ref.get('kind') == 'carried' else ref['save_id']
        rebuilt = capture(ref['parent_root'], source_id, target_id=ref['target_id'])
        if rebuilt != ref:
            raise ComputerError('windows_handoff_source_changed')
        target = next(c for c in goal['conditions'] if c['id'] == ref['target_id'])
        if files.decode_token(target['value']) != ref['final']:
            raise ComputerError('windows_handoff_target_changed')
