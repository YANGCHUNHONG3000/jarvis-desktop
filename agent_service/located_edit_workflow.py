"""Bridge verified Windows hits to the existing gated Office editing engine."""
from copy import deepcopy
from pathlib import Path

from .computer import ComputerError, ComputerSession, digest, read_record, safe_root, write_new
from . import computer_interaction as interaction
from . import office_edit_adapter as adapter
from . import windows_located_edit as located


def prepare(parent_task, selections, instruction):
    if type(selections) is not list or not 1 <= len(selections) <= 20:
        raise ComputerError('located_edit_selection_required')
    parent = str(safe_root(parent_task))
    for selection in selections:
        if type(selection) is not dict or set(selection) != {'ref', 'after'}:
            raise ComputerError('located_edit_selection_invalid')
        ref = located.verify(selection['ref'])
        if ref['parent_root'] != parent:
            raise ComputerError('located_edit_parent_changed')
    edits = located.derive_many(selections)
    # Preserve the legacy shape for single hits; grouped edits bind every hit.
    for item in edits:
        if len(item['locations']) == 1:
            item['location'] = item.pop('locations')[0]
    # Reject all invalid scopes before creating a persistent preview.
    session = ComputerSession.create()
    plan = interaction.prepare(session, instruction, edits=edits)
    candidate = adapter.ensure_candidate(adapter.DEFAULT_STORE, session, plan, proposer='Jarvis located edit')['candidate']
    write_new(session.root / 'located-edit.json', {'schema': 1, 'initial_plan': plan['sha256'],
              'candidate': candidate, 'parent_task': parent})
    return {'session_root': str(session.root), 'plan': plan, 'candidate': candidate, 'outcome': None}


def _base(root, *, readback_only=False):
    session = ComputerSession(root)
    manifest = read_record(session.root / 'located-edit.json')
    if (type(manifest) is not dict or set(manifest) != {'schema', 'initial_plan', 'candidate', 'parent_task'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1
            or not readback_only and manifest['candidate'] != adapter.current_candidate()):
        raise ComputerError('located_edit_implementation_changed')
    key = manifest['initial_plan']
    if type(key) is not str or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ComputerError('located_edit_plan_changed')
    plan = read_record(session.root / 'goals' / (key + '.json'))
    _check_plan(session, manifest, plan, plan)
    return session, manifest, plan


def _check_plan(session, manifest, plan, initial):
    adapter._plan(session, plan, recovery='recovery' in plan)
    if plan['goal'] != initial['goal'] or plan['instruction'] != initial['instruction']:
        raise ComputerError('located_edit_plan_changed')
    for item in plan['goal']['edits']:
        locations = ([item['location']] if 'location' in item else item.get('locations', []))
        if not locations or any(part['ref']['parent_root'] != manifest['parent_task'] for part in locations):
            raise ComputerError('located_edit_binding_required')


def _latest(session, manifest, initial):
    runs, parents = {}, set()
    for run in (session.root / 'goals').glob('*.run'):
        plan = read_record(run.with_suffix('.json'))
        _check_plan(session, manifest, plan, initial)
        runs[str(run)] = plan
        if 'recovery' in plan:
            parents.add(plan['recovery']['parent_run'])
    if not runs:
        return initial, None
    leaves = set(runs) - parents
    if len(leaves) != 1 or not parents <= runs.keys():
        raise ComputerError('located_edit_recovery_ambiguous')
    leaf = next(iter(leaves))
    path = Path(leaf) / 'outcome.json'
    outcome = read_record(path) if path.exists() else None
    plan = runs[leaf]
    if outcome and outcome.get('ok'):
        adapter._verified(session, plan, outcome)
    return plan, outcome


def load(session_root):
    session, manifest, initial = _base(session_root, readback_only=True)
    plan, outcome = _latest(session, manifest, initial)
    recheck = None
    if outcome and outcome.get('state') == 'unknown':
        recheck = _readback_scope(session, plan, outcome)
        pointer = Path(recheck['failed']['path']).parent / 'native-recheck.json'
        if pointer.exists():
            from .office_edit_recheck import verify
            proof = verify(recheck['failed']['path'], recheck['failed']['sha256'])
            outcome = {'ok': True, 'state': 'verified', 'code': 'copies_reverified_original_failure_retained',
                       'original_run': recheck['run'], 'artifacts': recheck['records'],
                       'recheck_proof_sha256': proof['proof_sha256']}
    elif manifest['candidate'] != adapter.current_candidate():
        raise ComputerError('located_edit_implementation_changed')
    return {'session_root': str(session.root), 'plan': plan,
            'candidate': manifest['candidate'], 'outcome': outcome, **({'readback':recheck} if recheck else {})}


def preview(session_root):
    state = load(session_root)
    if state.get('readback') and state['outcome'].get('ok') is not True:
        from .office_edit_recheck import inspect
        failed = state['readback']['failed']
        state['readback_preview'] = inspect(failed['path'], failed['sha256'])
        return state
    session, manifest, initial = _base(session_root)
    plan, outcome = _latest(session, manifest, initial)
    if outcome and outcome.get('ok'):
        raise ComputerError('located_edit_already_complete')
    if list((session.root / 'goals').glob('*.run')):
        plan = adapter.prepare_resume(adapter.DEFAULT_STORE, manifest['candidate'], session)['plan']
    else:
        plan = adapter.refresh_plan(session, initial)
    _check_plan(session, manifest, plan, initial)
    return {'session_root': str(session.root), 'plan': plan,
            'candidate': manifest['candidate'], 'outcome': outcome}


def execute(session_root, plan, *, stop=lambda: False, progress=lambda value: None, pause_after_step=False):
    session, manifest, initial = _base(session_root)
    _check_plan(session, manifest, plan, initial)
    if type(pause_after_step) is not bool:
        raise ComputerError('located_edit_pause_invalid')
    options = dict(approval=plan['sha256'], stop=stop, progress=progress,
                   pause_after_step=lambda: pause_after_step)
    candidate = manifest['candidate']
    if 'recovery' in plan:
        adapter.resume(adapter.DEFAULT_STORE, candidate, session, plan, **options)
    else:
        state = adapter.status(adapter.DEFAULT_STORE, candidate)
        operation = adapter.execute if state['trial_passed'] else adapter.trial
        operation(adapter.DEFAULT_STORE, candidate, session, plan, **options)
    return load(session.root)


def outputs(session_root):
    state = load(session_root)
    if not state['outcome'] or state['outcome'].get('ok') is not True:
        raise ComputerError('located_edit_outputs_incomplete')
    records = [{'path': item['path'], 'sha256': item['sha256']} for item in state['outcome']['artifacts']]
    from .office_handoff import select_outputs
    select_outputs(records)
    return records


def _readback_scope(session, plan, outcome):
    """Only the final dispatched file's native-readback failure can be reassessed.

    No business step is replayed and no unknown run is rewritten as success.
    Unstarted files or other unknown effects require a different recovery path.
    """
    from . import interaction_recovery as recovery
    from .computer_agent_tools import _load
    from .office_handoff import select_outputs
    from .run_lifecycle import assert_inactive
    run = recovery._run_root(session, outcome['run'])
    assert_inactive(run)
    checked = recovery._plan(session, run)
    if (checked != plan or outcome.get('ok') is not False or outcome.get('state') != 'unknown'
            or outcome.get('code') != 'agent_office_unknown' or outcome.get('engine') != 'observed_agent'
            or outcome.get('recovery') != plan.get('recovery') or plan['goal']['browser'] is not None):
        raise ComputerError('located_edit_readback_not_eligible')
    seed = recovery._lineage(session, plan, claimed=True)
    steps = sorted(run.glob('step-*'))
    if len(steps) != 1 or steps[0].name != 'step-01':
        raise ComputerError('located_edit_readback_not_eligible')
    record, _ = _load(steps[0]/'environment-result.json')
    failed_id = record['action'].get('edit_id')
    expected = {'step':1,'action':record['action'],'result':record['result']}
    if (record['action'].get('kind') != 'office_edit'
            or outcome.get('attempts') != [expected] or outcome.get('completed') != []
            or outcome.get('artifacts') != seed['artifacts']
            or len(plan['goal']['edits']) != len(seed['done']) + 1
            or set(seed['done']) | {failed_id} != {e['id'] for e in plan['goal']['edits']}
            or failed_id in seed['done']):
        raise ComputerError('located_edit_readback_not_eligible')
    inherited = [{'path':a['path'],'sha256':a['sha256']} for a in seed['artifacts']]
    if inherited:
        select_outputs(inherited)
    failed = {'path':record['native']['artifact'],'sha256':record['native']['sha256']}
    failed_item = next(item for item in plan['goal']['edits'] if item['id'] == failed_id)
    manifest, _ = _load(safe_root(failed['path']).parent/'edit-manifest.json')
    if (manifest['run'] != str(run) or manifest['step'] != str(steps[0])
            or manifest['source_root'] != failed_item['source_root'] or manifest['spec'] != failed_item['spec']):
        raise ComputerError('located_edit_readback_source_changed')
    # Exact failure/source/manifest and native result eligibility is checked by
    # the dedicated inspector before preview and again inside its worker flow.
    return {'run':str(run),'failed':failed,'records':[*inherited,failed]}


def recheck(session_root, *, approval, stop=lambda:False):
    state = preview(session_root)
    request = state.get('readback_preview')
    if request is None or request['sha256'] != approval:
        raise ComputerError('located_edit_readback_approval_required')
    from .office_edit_recheck import execute
    failed = state['readback']['failed']
    result = execute(failed['path'], failed['sha256'], approval=approval, stop=stop)
    if result.get('ok') is not True:
        raise ComputerError('located_edit_readback_failed')
    return load(session_root)
