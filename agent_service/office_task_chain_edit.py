"""Exact Office planning proofs for one already-bound task-chain stage."""
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from . import office_task_chain as chain, office_workspace, office_intent, office_edit
from . import office_edit_adapter as adapter, interaction_recovery
from .computer import digest

_MAX_REFRESH = 32
_EDIT_KEYS = {'schema', 'preview_sha256', 'session_root', 'plan', 'planning_result', 'office_payload', 'proofs'}
_REFRESH_KEYS = {'schema', 'previous_sha256', 'revision', 'plan'}


def _json(path):
    chain.workspace._safe_components(path)
    chain._require(path.is_file() and path.stat().st_size <= 200_000)
    with path.open('rb') as stream:
        raw = stream.read(200_001)
    chain._require(len(raw) <= 200_000)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=chain.workspace._unique_pairs), sha256(raw).hexdigest()


def _session(manifest, root):
    root = chain._root(root)
    chain._require(root.parent == Path(manifest['computer_base'])
                   and re.fullmatch(r'computer-[0-9a-f]{32}', root.name) is not None)
    metadata, _ = _json(root / 'session.json')
    chain._require(type(metadata) is dict and type(metadata.get('schema')) is int
                   and metadata['schema'] == 1 and type(metadata.get('created')) in (int, float))
    return chain.SimpleNamespace(root=root)


def _plan(session, plan, instruction):
    keys = {'schema', 'mode', 'instruction', 'goal', 'max_actions', 'max_seconds', 'prepared_at', 'sha256'}
    chain._require(type(plan) is dict and set(plan) == keys
                   and type(plan['schema']) is int and plan['schema'] == 3
                   and type(plan['sha256']) is str and chain.workspace._SHA.fullmatch(plan['sha256']) is not None
                   and plan['instruction'] == instruction and plan['max_actions'] == 12
                   and plan['max_seconds'] == 360 and type(plan['prepared_at']) in (int, float)
                   and math.isfinite(plan['prepared_at']), 'office_chain_edit_plan_changed')
    chain.workspace._read(session.root / 'goals' / (plan['sha256'] + '.json'), 200_000)
    adapter._plan(session, plan)
    return chain._root(session.root / 'goals' / (plan['sha256'] + '.run'))


def _inputs(manifest, index, binding, payload, *, inputs=None, clarification=None):
    checked = office_workspace._payload(payload)
    selected = binding['routed']['edit']['source_refs']
    records = [{'path': ref['artifact'], 'sha256': ref['sha256']} for ref in selected]
    instruction = clarification['instruction'] if clarification is not None else manifest['instructions'][index]
    if clarification is not None:
        records = [{'path': p, 'sha256': s['sha256']} for p, s in zip(
            clarification['files'], clarification['scope'])]
    chain._require(checked['draft'] == instruction
                   and checked['records'] == records and checked['scope'] is not None,
                   'office_chain_edit_inputs_changed')
    refs = chain._refs({'draft': checked['draft'], 'records': records, 'source': None},
                       manifest['task_base'], inputs=inputs)
    chain._require(refs == (clarification['artifact_refs'] if clarification is not None else selected),
                   'office_chain_source_changed')
    return checked, refs


def _clarification(session, manifest, index, binding, result, payload):
    from .office_clarification import load as load_clarification
    item = load_clarification(session, result)
    selected = binding['routed']['edit']['source_refs']
    chain._require(item['state'] == 'resolved' and item['answers']
                   and item['original_instruction'] == manifest['instructions'][index]
                   and item['all_refs'] == selected
                   and [{'path': s['path'], 'sha256': s['public']['sha256']} for s in item['all_sources']]
                       == [{'path': s['artifact'], 'sha256': s['sha256']} for s in selected]
                   and item['instruction'] == payload['draft'] == result['plan']['instruction']
                   and item['files'] == [r['path'] for r in payload['records']]
                   and item['scope'] == payload['scope'], 'office_chain_edit_clarification_changed')
    return item


def validate(manifest, index, binding, record, *, inputs=None):
    """Reconstruct the exact context and model-produced goal without a model call."""
    chain._require(type(record['schema']) is int and record['schema'] == 1)
    session = _session(manifest, record['session_root'])
    chain._require(str(session.root) == binding['session_root'], 'office_chain_edit_session_changed')
    plan, result = record['plan'], record['planning_result']
    clarification = (_clarification(session, manifest, index, binding, result, record['office_payload'])
                     if 'continuation.json' in record['proofs'] else None)
    payload, refs = _inputs(manifest, index, binding, record['office_payload'], inputs=inputs,
                           clarification=clarification)
    run = _plan(session, plan, payload['draft'])
    chain._require(type(result) is dict and set(result) == {'plan', 'reason', 'office_result'}
                   and result['plan'] == plan and type(result['office_result']) is dict
                   and set(result['office_result']) == {'kind', 'record'}
                   and result['office_result']['kind'] == 'edits', 'office_chain_edit_planning_changed')
    folder = chain._root(result['office_result']['record'])
    chain._require(folder.parent == session.root / 'office-planning'
                   and chain.workspace._ID.fullmatch(folder.name) is not None)
    files = ['request.json', 'context.json', 'response.json', 'outcome.json',
             'artifact-references.json', 'selection.json']
    if clarification is not None:
        files.append('continuation.json')
    values, proofs = {}, {}
    for name in files:
        values[name], proofs[name] = _json(folder / name)
    chain._require(proofs == record['proofs'], 'office_chain_edit_planning_changed')
    chain.workspace._safe_components(folder / 'failure.json')
    chain._require(not (folder / 'failure.json').exists())
    request = {'instruction': payload['draft'], 'files': [r['path'] for r in payload['records']],
               'artifact_refs': refs, 'scope': payload['scope']}
    chain._require(values['request.json'] == request and values['outcome.json'] == result
                   and values['artifact-references.json'] == refs and values['selection.json'] == payload['scope'],
                   'office_chain_edit_planning_changed')
    roots = {edit['id']: edit['source_root'] for edit in plan['goal']['edits']}
    sources = []
    for n, (item, selection) in enumerate(zip(payload['records'], payload['scope']), 1):
        path = Path(item['path'])
        kind = 'document' if path.suffix.lower() == '.docx' else 'spreadsheet'
        public = {'source_id': 'selected-edit', 'kind': kind, 'sha256': item['sha256'],
                  'bytes': path.stat().st_size, 'display_name': path.name,
                  'observed': office_edit._observe(path, kind, 2 if kind == 'document' else 1)}
        if kind == 'document':
            public['observation_version'] = 2
        root = roots.get(f'e{n}')
        if root is not None:
            source_root = chain._root(root)
            chain._require(source_root.parent == session.root / 'edit-inputs'
                           and chain.workspace._ID.fullmatch(source_root.name) is not None)
            native, _ = _json(source_root / 'source.json')
            chain._require(native == {'public': public, 'original': str(path)}
                           and office_edit.verify_edit_source(source_root) == public,
                           'office_chain_edit_source_changed')
        sources.append({'id': f'e{n}', 'root': root, 'public': public,
                        'path': str(path), 'scope': selection})
    context, targets = office_intent._context(payload['draft'], sources)
    chain._require(context == values['context.json'], 'office_chain_edit_context_changed')
    goal = office_intent._goal(values['response.json'], targets)
    chain._require(goal == plan['goal'], 'office_chain_edit_goal_changed')
    chain._refs({'draft': '', 'records': payload['records'], 'source': None},
                manifest['task_base'], inputs=inputs)
    return session, payload, run


def capture(manifest, index, binding, preview_sha256, session, plan, result, payload):
    record = {'schema': 1, 'preview_sha256': preview_sha256, 'session_root': str(chain._root(session.root)),
              'plan': deepcopy(plan), 'planning_result': deepcopy(result), 'office_payload': deepcopy(payload)}
    chain._require(type(result) is dict and type(result.get('office_result')) is dict
                   and result['office_result'].get('kind') == 'edits', 'office_chain_edit_planning_changed')
    folder = chain._root(result['office_result']['record'])
    checked_session = _session(manifest, record['session_root'])
    chain._require(str(checked_session.root) == binding['session_root'], 'office_chain_edit_session_changed')
    chain._require(folder.parent == checked_session.root / 'office-planning'
                   and chain.workspace._ID.fullmatch(folder.name) is not None)
    record['proofs'] = {name: _json(folder / name)[1] for name in (
        'request.json', 'context.json', 'response.json', 'outcome.json', 'artifact-references.json', 'selection.json')}
    if plan['instruction'] != manifest['instructions'][index]:
        record['proofs']['continuation.json'] = _json(folder / 'continuation.json')[1]
    validate(manifest, index, binding, record)
    return record


def _refreshes(folder, index):
    paths = []
    for path in folder.glob(f'step-{index}-edit-refresh-*.json'):
        paths.append(path)
        chain._require(len(paths) <= _MAX_REFRESH, 'office_chain_edit_refresh_limit')
    expected = [folder / f'step-{index}-edit-refresh-{n}.json' for n in range(1, len(paths) + 1)]
    chain._require(set(paths) == set(expected))
    return expected


def load(folder, manifest, index, preview, state, *, inputs=None):
    path = folder / f'step-{index}-edit.json'
    chain.workspace._safe_components(path)
    if not path.exists():
        chain._require(not _refreshes(folder, index))
        state.update(status='needs_scope', reason='请选择明确编辑范围后规划差异。')
        return None, preview['sha256']
    record = chain._read(path, _EDIT_KEYS)
    chain._require(record['preview_sha256'] == preview['sha256'])
    session, payload, run = validate(manifest, index, preview['binding'], record, inputs=inputs)
    plan, anchor, revision = record['plan'], record['sha256'], 0
    for revision, path in enumerate(_refreshes(folder, index), 1):
        refresh = chain._read(path, _REFRESH_KEYS)
        chain._require(type(refresh['schema']) is int and refresh['schema'] == 1
                       and type(refresh['revision']) is int and refresh['revision'] == revision
                       and refresh['previous_sha256'] == anchor and not run.exists(),
                       'office_chain_edit_refresh_changed')
        fresh = refresh['plan']
        next_run = _plan(session, fresh, plan['instruction'])
        chain._require({k: v for k, v in fresh.items() if k not in {'prepared_at', 'sha256'}}
                       == {k: v for k, v in plan.items() if k not in {'prepared_at', 'sha256'}}
                       and fresh['sha256'] != plan['sha256'] and fresh['prepared_at'] > plan['prepared_at'],
                       'office_chain_edit_refresh_changed')
        plan, run, anchor = fresh, next_run, refresh['sha256']
    state.update(status='edit_preview', edit_payload=payload, edit_plan=plan,
                 edit_revision=revision, run=str(run), reason='请核对差异，明确批准后才执行编辑。')
    if not run.exists():
        return None, anchor
    chain._require(run.is_dir())
    chain.workspace._safe_components(run / 'outcome.json')
    if not (run / 'outcome.json').exists():
        state.update(status='waiting_result', reason='编辑已经开始但结局未知；不会自动重试。')
        return None, anchor
    outcome, _ = _json(run / 'outcome.json')
    chain._require(type(outcome) is dict and type(outcome.get('ok')) is bool)
    if outcome['ok'] is not True:
        state.update(status='blocked', reason='本次编辑未成功，原输入与结局保留；不会重放。')
        return outcome, anchor
    native = adapter._verified(session, plan, outcome)
    proof = interaction_recovery._inspect(session, run, successful=True)
    chain._require(proof['instruction'] == plan['instruction'] and proof['goal'] == plan['goal']
                   and native['run'] == str(run) and native['plan_sha256'] == plan['sha256']
                   and native['artifacts'] == proof['artifacts'] == outcome['artifacts'])
    records = [{'path': a['path'], 'sha256': a['sha256']} for a in proof['artifacts']]
    chain._verified({'draft': '', 'records': records, 'source': None}, manifest['task_base'], inputs=inputs)
    normalized = {'ok': True, 'state': 'succeeded', 'run': str(run), 'approval': plan['sha256'],
                  'artifacts': records, 'native_outcome_sha256': digest(outcome), 'proof_sha256': digest(native)}
    state.update(status='waiting_result', reason='编辑成果已核验，可接续下一步骤。')
    return normalized, anchor


def refresh(folder, manifest, index, preview, state, session, plan):
    record = chain._read(folder / f'step-{index}-edit.json', _EDIT_KEYS)
    chain._require(str(chain._root(session.root)) == record['session_root'], 'office_chain_edit_session_changed')
    session = _session(manifest, record['session_root'])
    new_run = _plan(session, plan, state['edit_plan']['instruction'])
    chain._require(state['status'] == 'edit_preview' and not Path(state['run']).exists(),
                   'office_chain_edit_already_started')
    if plan == state['edit_plan']:
        return
    chain._require(not new_run.exists(), 'office_chain_edit_already_started')
    chain._require({k: v for k, v in plan.items() if k not in {'prepared_at', 'sha256'}}
                   == {k: v for k, v in state['edit_plan'].items() if k not in {'prepared_at', 'sha256'}},
                   'office_chain_edit_refresh_changed')
    chain._require(plan['prepared_at'] > state['edit_plan']['prepared_at'], 'office_chain_edit_refresh_changed')
    paths = _refreshes(folder, index)
    chain._require(len(paths) < _MAX_REFRESH, 'office_chain_edit_refresh_limit')
    previous = chain._read(paths[-1], _REFRESH_KEYS) if paths else record
    revision = len(paths) + 1
    chain._append(folder / f'step-{index}-edit-refresh-{revision}.json', {
        'schema': 1, 'previous_sha256': previous['sha256'], 'revision': revision, 'plan': deepcopy(plan)})
