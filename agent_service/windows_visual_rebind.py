"""Immutable, explicitly reviewed continuation into a uniquely attested window.

Only first-generation schema3 visual navigation is eligible. Parent tasks and
their approval/evidence are read-only; completed steps never enter the child.
"""
from copy import deepcopy
from pathlib import Path

from .computer import ComputerError, digest, safe_root
from . import windows_task as task, windows_visual as visual
from . import windows_visual_binding as binding, windows_visual_memory as memory


def _check(stop):
    if stop():
        raise ComputerError('windows_visual_rebind_cancelled')


def _parent(root):
    from . import windows_visual_proposal as proposal
    root = safe_root(root)
    with task._own(root):
        scan = task._scan(root)
        if scan['task']['schema'] != 3 or scan['uncertain']:
            raise ComputerError('windows_visual_rebind_ineligible')
        goal = scan['goal']
        if (len(scan['completed']) == len(goal['conditions'])
                or any(not visual.is_goal(c['selector']) or c['retain'] for c in goal['conditions'])):
            raise ComputerError('windows_visual_rebind_ineligible')
        reference = scan['task']['visual_proposal']
        header, checksum = task._load(safe_root(reference['root']) / 'proposal.json')
        # Check before recursive source verification: migration may not chain.
        if (checksum != reference['sha256'] or header.get('continuation') is not None
                or any(c.get('source', {}).get('kind') == 'rebind' for c in header.get('candidates', []))):
            raise ComputerError('windows_visual_rebind_generation_limit')
        payload = proposal.verify(reference, goal)
        documents = payload.get('document_bindings')
        if type(documents) is not list or not documents:
            raise ComputerError('windows_visual_rebind_document_required')
        windows = []
        for c in goal['conditions']:
            if c['window'] not in windows:
                windows.append(c['window'])
        if len(windows) > 2 or len(documents) != len(windows):
            raise ComputerError('windows_visual_rebind_document_required')
        for window in windows:
            matches = [d for d in documents if type(d) is dict and set(d) == {'window', 'document'}
                       and d['window'] == window]
            if len(matches) != 1:
                raise ComputerError('windows_visual_rebind_document_required')
            binding.verify(matches[0]['document'], window=window)
        return deepcopy(scan), deepcopy(payload), task._inventory(root)


def _remaining(scan):
    remaining = [c for c in scan['goal']['conditions'] if c['id'] not in scan['completed']]
    if scan['goal']['max_steps'] - scan['actions'] <= 0:
        raise ComputerError('windows_action_limit')
    return remaining


def _goal(scan, mappings):
    conditions = []
    for number, old in enumerate(_remaining(scan), 1):
        matches = [m for m in mappings if m['old'] == old['window']]
        if len(matches) != 1:
            raise ComputerError('windows_visual_rebind_mapping_invalid')
        conditions.append({**deepcopy(old), 'id': f'target_{number}', 'window': deepcopy(matches[0]['new'])})
    return task.validate_goal({**scan['goal'], 'conditions': conditions,
                               'max_steps': scan['goal']['max_steps'] - scan['actions']})


def _continuation(root, scan, inventory, mappings):
    return {'schema': 1, 'parent_root': str(safe_root(root)),
            'parent_task_sha256': scan['task_sha256'], 'parent_inventory_sha256': inventory,
            'parent_goal_sha256': digest(scan['goal']), 'parent_proposal': scan['task']['visual_proposal'],
            'completed': scan['completed'], 'actions': scan['actions'], 'windows': deepcopy(mappings),
            'conditions': [{'old_id': c['id'], 'new_id': f'target_{i}'}
                           for i, c in enumerate(_remaining(scan), 1)]}


def _verify_continuation(continuation):
    if type(continuation) is not dict:
        raise ComputerError('windows_visual_rebind_continuation_invalid')
    scan, parent, inventory = _parent(continuation['parent_root'])
    mappings = continuation.get('windows')
    if type(mappings) is not list or not 1 <= len(mappings) <= 2:
        raise ComputerError('windows_visual_rebind_mapping_invalid')
    used = []
    for mapping in mappings:
        if type(mapping) is not dict or set(mapping) != {'old', 'new', 'document'}:
            raise ComputerError('windows_visual_rebind_mapping_invalid')
        old, new = memory._window(mapping['old']), memory._window(mapping['new'])
        if old != mapping['old'] or new != mapping['new'] or old['hwnd'] == new['hwnd'] or new['hwnd'] in used:
            raise ComputerError('windows_visual_rebind_mapping_invalid')
        used.append(new['hwnd'])
        matches = [d for d in parent['document_bindings'] if d['window'] == old and d['document'] == mapping['document']]
        if len(matches) != 1 or not any(c['window'] == old for c in _remaining(scan)):
            raise ComputerError('windows_visual_rebind_document_changed')
        if (str(safe_root(new['image'])) != mapping['document']['registered_app']
                or new['class_name'] != old['class_name'] or new['profile'] != old['profile']
                or new['title'] != old['title']):
            raise ComputerError('windows_visual_rebind_application_changed')
        binding.verify(mapping['document'], window=old)
    if continuation != _continuation(continuation['parent_root'], scan, inventory, mappings):
        raise ComputerError('windows_visual_rebind_parent_changed')
    return scan, parent, _goal(scan, mappings)


def _candidate(old, window, continuation, snapshot):
    from . import windows_visual_discovery as discovery
    from .windows_visual_proposal import candidate_condition
    discovery._page(snapshot, old['label'])
    if snapshot['window'] != window or snapshot['application'] != 'wps':
        raise ComputerError('windows_visual_rebind_snapshot_changed')
    item = {'window': deepcopy(window), 'label': old['label'], 'expected': deepcopy(old['expected']),
            'source': {'kind': 'rebind', 'parent_candidate': deepcopy(old),
                       'continuation': deepcopy(continuation), 'snapshot': deepcopy(snapshot),
                       'binding': memory._bitmap(snapshot['frame'])}}
    condition = candidate_condition(item)
    if condition['selector']['automation_id'] == visual.SELECTION_ID:
        _, details = visual.presentation(snapshot['frame'], condition['selector'], condition['value'])
        selected = details['selection_readback']
        if len(selected['selected_labels']) != 1 or len(selected['content_labels']) < 2:
            raise ComputerError('windows_visual_selection_unavailable')
    return item


def verify_candidate(item):
    """Read files only. New window liveness is checked separately at execution."""
    from . import windows_visual_proposal as proposal
    try:
        if type(item) is not dict or set(item) != {'window', 'label', 'expected', 'source'}:
            raise ComputerError('windows_visual_rebind_candidate_invalid')
        source = item['source']
        if (type(source) is not dict or set(source) != {'kind', 'parent_candidate', 'continuation', 'snapshot', 'binding'}
                or source['kind'] != 'rebind'):
            raise ComputerError('windows_visual_rebind_candidate_invalid')
        scan, parent, goal = _verify_continuation(source['continuation'])
        old = source['parent_candidate']
        if old not in parent['candidates'] or old.get('source', {}).get('kind') == 'rebind':
            raise ComputerError('windows_visual_rebind_candidate_invalid')
        proposal.verify_candidate(old)
        mappings = [m for m in source['continuation']['windows'] if m['old'] == old['window'] and m['new'] == item['window']]
        proposed = proposal.candidate_condition(item)
        if len(mappings) != 1 or not any({**c, 'id': 'target_1'} == proposed for c in goal['conditions']):
            raise ComputerError('windows_visual_rebind_candidate_invalid')
        if item != _candidate(old, item['window'], source['continuation'], source['snapshot']):
            raise ComputerError('windows_visual_rebind_candidate_changed')
        return item
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ComputerError):
            raise
        raise ComputerError('windows_visual_rebind_candidate_invalid') from None


def verify_continuation(payload, goal):
    try:
        continuation = payload['continuation']
        _, _, expected = _verify_continuation(continuation)
        if goal != expected:
            raise ComputerError('windows_visual_rebind_goal_changed')
        bindings = payload.get('document_bindings')
        if type(bindings) is not list or len(bindings) != len(continuation['windows']):
            raise ComputerError('windows_visual_rebind_document_changed')
        for saved, mapping in zip(bindings, continuation['windows']):
            if (type(saved) is not dict or set(saved) != {'window', 'document'}
                    or saved['window'] != mapping['new']):
                raise ComputerError('windows_visual_rebind_document_changed')
            binding.verify(saved['document'], window=mapping['new'])
            if not binding.same_document(saved['document'], mapping['document']):
                raise ComputerError('windows_visual_rebind_document_changed')
        candidates = payload.get('candidates')
        if (type(candidates) is not list or not candidates
                or any(c.get('source', {}).get('continuation') != continuation for c in candidates)):
            raise ComputerError('windows_visual_rebind_continuation_invalid')
        for item in candidates:
            verify_candidate(item)
        return continuation
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ComputerError):
            raise
        raise ComputerError('windows_visual_rebind_continuation_invalid') from None


def _unique(mapping, backend, stop):
    _check(stop)
    presence = backend.window_presence(mapping['old'], stop=stop)
    if type(presence) is not bool or presence:
        raise ComputerError('windows_visual_rebind_old_window_present')
    document = mapping['document']
    matches = []
    windows = backend.list_windows(stop=stop)
    # The native enumerator stops at 200: reaching that cap cannot prove uniqueness.
    if type(windows) is not list or len(windows) >= 200:
        raise ComputerError('windows_visual_rebind_window_inventory_incomplete')
    for window in windows:
        _check(stop)
        if (str(safe_root(window['image'])) != document['registered_app']
                or window['class_name'] != mapping['old']['class_name']
                or window['profile'] != mapping['old']['profile']
                or window['title'] != mapping['old']['title']):
            continue
        # An unreadable same-application candidate prevents a uniqueness proof.
        current = backend.visual_document_identity(window, stop=stop)
        binding.verify(current, window=window)
        if binding.same_document(current, document):
            matches.append(window)
    if len(matches) != 1:
        raise ComputerError('windows_visual_rebind_window_not_unique')
    return deepcopy(matches[0])


def prepare(parent, backend, stop=lambda: False):
    """Observe an eligible replacement and return an unapproved child proposal."""
    _check(stop)
    scan, payload, inventory = _parent(parent)
    remaining = _remaining(scan)
    windows = []
    for condition in remaining:
        if condition['window'] not in windows:
            windows.append(condition['window'])
    mappings, observations, snapshots = [], [], []
    for old in windows:
        document = next(d['document'] for d in payload['document_bindings'] if d['window'] == old)
        mapping = {'old': deepcopy(old), 'new': None, 'document': deepcopy(document)}
        new = _unique(mapping, backend, stop)
        mapping['new'] = new
        observation = task._observation(backend.observe(new, stop=stop), new)
        if observation.get('dialogs'):
            raise ComputerError('windows_application_blocked')
        snapshot = backend.visual_snapshot(new, stop=stop)
        _check(stop)
        current_document = backend.visual_document_identity(new, stop=stop)
        binding.verify(current_document, window=new)
        if not binding.same_document(current_document, document):
            raise ComputerError('windows_visual_rebind_document_changed')
        # The source mapping preserves old native proof; the new binding below
        # carries an independently verified observation from the replacement.
        mapping['new_document'] = current_document
        mappings.append(mapping)
        observations.append(observation)
        snapshots.append(snapshot)
    documents = [{'window': m['new'], 'document': m.pop('new_document')} for m in mappings]
    continuation = _continuation(parent, scan, inventory, mappings)
    goal = _goal(scan, mappings)
    candidates = []
    from .windows_visual_proposal import candidate_condition
    for old_condition, new_condition in zip(remaining, goal['conditions']):
        choices = [c for c in payload['candidates']
                   if candidate_condition(c) == {**old_condition, 'id': 'target_1'}]
        if len(choices) != 1:
            raise ComputerError('windows_visual_rebind_candidate_invalid')
        snapshot = snapshots[windows.index(old_condition['window'])]
        item = _candidate(choices[0], new_condition['window'], continuation, snapshot)
        if item not in candidates:
            candidates.append(item)
    result = {'goal': goal, 'candidates': candidates, 'observations': observations,
              'document_bindings': documents,
              'continuation': continuation}
    verify_continuation(result, goal)
    _check(stop)
    return result


def preflight(payload, backend, stop=lambda: False):
    """After explicit approval, re-prove liveness and exact native document."""
    verify_continuation(payload, payload['goal'])
    for mapping in payload['continuation']['windows']:
        _check(stop)
        if _unique(mapping, backend, stop) != mapping['new']:
            raise ComputerError('windows_visual_rebind_window_changed')
        observation = task._observation(backend.observe(mapping['new'], stop=stop), mapping['new'])
        if observation.get('dialogs'):
            raise ComputerError('windows_application_blocked')
    _check(stop)
    return True
