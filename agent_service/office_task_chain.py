"""Append-only, bounded Office handoff; never plans, approves or executes work."""
from copy import deepcopy
from functools import wraps
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
import os
import re
import uuid

from . import daily_task_routing as routing, workspace_state as workspace
from .computer import digest
from .office_workflow import _owner
from .office_chain_inputs import InputVerifications
from . import office_chain_payload

_LIMIT = 64 * 1024
MAX_STEPS = 4


class OfficeTaskChainError(ValueError):
    """Stable local failure code without document contents or paths."""


def _require(condition, code='office_chain_record_invalid'):
    if not condition:
        raise OfficeTaskChainError(code)


def _public(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OfficeTaskChainError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            code = 'office_chain_busy' if str(exc) == 'workflow_busy' else 'office_chain_record_invalid'
            raise OfficeTaskChainError(code) from exc
    return call


def _root(value, *, create=False):
    return workspace._store_root(value, create=create)


def _sealed(value):
    return {**value, 'sha256': digest(value)}


def _read(path, keys):
    value, _ = workspace._read(path, _LIMIT)
    _require(set(value) == set(keys) | {'sha256'})
    _require(value['sha256'] == digest({k: v for k, v in value.items() if k != 'sha256'}))
    return value


def _append(path, value):
    raw = workspace._encode(_sealed(value))
    _require(len(raw) <= _LIMIT, 'office_chain_oversize')
    workspace._write_new(path, raw)


def _folder(store, ident):
    _require(type(ident) is str and workspace._ID.fullmatch(ident) is not None)
    return _root(_root(store) / ('chain-' + ident))


def _instructions(value):
    _require(type(value) is list and 2 <= len(value) <= MAX_STEPS, 'office_chain_requires_two_to_four_steps')
    _require(all(type(text) is str and 1 <= len(text.strip()) <= 4000
                 and not any(ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127 for c in text)
                 for text in value), 'office_chain_instruction_invalid')
    return [text.strip() for text in value]


def _verified(payload, base, *, inputs=None):
    if inputs is None:
        return workspace.verify(payload, base)
    _require(type(inputs) is InputVerifications)
    return inputs.verify(payload, base)


def _refs(payload, base, *, inputs=None):
    result = _verified(payload, base, inputs=inputs)
    return [result['source_ref']] if payload['source'] is not None else result['refs']


def _bound(manifest, index, binding, payload, *, inputs=None):
    _require(type(binding) is dict and set(binding) == {'session_root', 'routed', 'refs'})
    session_root = _root(binding['session_root'])
    _require(session_root.parent == Path(manifest['computer_base'])
             and re.fullmatch(r'computer-[0-9a-f]{32}', session_root.name) is not None)
    session, _ = workspace._read(session_root / 'session.json', _LIMIT)
    _require(session.get('schema') == 1 and type(session.get('created')) in (int, float))
    refs = _refs(payload, manifest['task_base'], inputs=inputs)
    _require(binding['refs'] == refs, 'office_chain_source_changed')
    routed = binding['routed']
    _require(type(routed) is dict and (
        set(routed) == {'state', 'reason', 'plan', 'routing'} and routed['state'] == 'ready'
        or set(routed) == {'state', 'reason', 'plan', 'routing', 'edit'} and routed['state'] == 'needs_scope'),
        'office_chain_route_unsupported')
    route = _root(routed['routing'])
    _require(route.parent == session_root / 'task_routes'
             and workspace._SHA.fullmatch(route.name) is not None)
    req, _ = workspace._read(route / 'request.json', _LIMIT)
    keys = {'schema', 'routing_rules_sha256', 'instruction', 'source_refs', 'task_base'}
    _require(set(req) in (keys, keys | {'mail_export'}) and type(req['schema']) is int
             and req['schema'] >= 1 and type(req['routing_rules_sha256']) is str
             and workspace._SHA.fullmatch(req['routing_rules_sha256']) is not None
             and digest(req) == route.name)
    _require(req['instruction'] == manifest['instructions'][index]
             and req['source_refs'] == refs and req['task_base'] == manifest['task_base'],
             'office_chain_request_changed')
    # Read with duplicate-key and size checks before using the existing verifier.
    for name in ('decision.json', 'outcome.json'):
        workspace._read(route / name, _LIMIT)
    saved = routing._cached(SimpleNamespace(root=session_root), route, req)
    _require(saved == routed, 'office_chain_preview_changed')
    decision = routing._checked_decision(route, req)
    if routed['state'] == 'needs_scope':
        _require(decision['route'] == 'office_edit' and routed['plan'] is None)
        return None
    plan = routed['plan']
    _require(type(plan) is dict and type(plan.get('sha256')) is str
             and workspace._SHA.fullmatch(plan['sha256']) is not None)
    workspace._read(session_root / 'goals' / (plan['sha256'] + '.json'), _LIMIT)
    _require((decision['route'] == 'single_source' and len(refs) == 1
              and plan.get('mode') == 'capability' and type(plan.get('schema')) is int
              and plan['schema'] == 1)
             or (decision['route'] == 'compare_tables' and 2 <= len(refs) <= 4
                 and plan.get('mode') == 'office_bundle' and type(plan.get('schema')) is int
                 and plan['schema'] == 1)
             or (decision['route'] == 'mail_understanding' and len(refs) == 1
                 and plan.get('mode') == 'capability' and type(plan.get('schema')) is int
                 and plan['schema'] == 4 and plan.get('capability') == 'mail_understanding'),
             'office_chain_route_unsupported')
    keys = {'schema', 'mode', 'instruction', 'task_base', 'request_id', 'sha256'}
    keys |= {'source_refs'} if plan['mode'] == 'office_bundle' else {'source_ref', 'candidates'}
    if plan.get('schema') == 4:
        keys.add('capability')
    _require(set(plan) == keys and type(plan['request_id']) is str
             and workspace._ID.fullmatch(plan['request_id']) is not None
             and (plan['mode'] == 'office_bundle' or type(plan['candidates']) is list))
    routing._checked_child(SimpleNamespace(root=session_root), req, decision, plan)
    if plan.get('schema') == 4:
        from .capability_router import _mail_plan
        _mail_plan(plan, Path(manifest['task_base']))
    suffix = '.bundle' if plan['mode'] == 'office_bundle' else '.capability'
    return _root(session_root / 'goals' / (plan['sha256'] + suffix))


def _outcome(manifest, run, *, state=None, completed=None, resume_attempt=None):
    if not run.exists():
        _require(resume_attempt is None, 'office_chain_resume_unverified')
        return 'preview', '请在现有预览中明确批准本步骤。', None
    _require(run.is_dir())
    outcome_path = run / 'outcome.json'
    workspace._safe_components(outcome_path)
    if not outcome_path.exists():
        _require(resume_attempt is None, 'office_chain_resume_unverified')
        return 'waiting_result', '运行已开始但结局未知；保留现场，不自动重试。', None
    original, original_raw = workspace._read(outcome_path, _LIMIT)
    from . import capability_router, office_bundle
    plan, _ = workspace._read(run.parent / (run.stem + '.json'), _LIMIT)
    if (plan.get('schema') == 4 or run.suffix == '.bundle') and original.get('ok') is False and not original.get('before_start'):
        from . import office_task_chain_resume, office_task_chain_bundle_resume
        recovery = office_task_chain_bundle_resume if run.suffix == '.bundle' else office_task_chain_resume
        result = recovery.inspect(manifest, run, plan, original, original_raw,
                 state=state if state is not None else {}, completed=completed, attempt=resume_attempt)
        if result is not None:
            return result
    _require(resume_attempt is None, 'office_chain_resume_unverified')
    inspector = office_bundle.inspect_saved if run.suffix == '.bundle' else capability_router.inspect_saved
    outcome = inspector(run, task_base=manifest['task_base'], computer_base=manifest['computer_base'])
    _require(type(outcome) is dict and type(outcome.get('ok')) is bool)
    if outcome['ok'] is not True:
        return 'blocked', '本步骤未成功，已保留原结局与输入；不会重放。', outcome
    _require(outcome.get('state') == 'succeeded')
    request = Path(outcome['task_root']) / 'request.txt'
    workspace._safe_components(request)
    _require(request.is_file() and request.stat().st_size <= _LIMIT,
             'office_chain_task_instruction_changed')
    with request.open('rb') as stream:
        actual = stream.read(_LIMIT + 1)
    # Both existing executors use UTF-8 text write(), without a final newline.
    expected = plan['instruction'].replace('\n', os.linesep).encode('utf-8')
    _require(actual == expected, 'office_chain_task_instruction_changed')
    if run.suffix == '.capability':
        from .capability_source_choice import verify_task_source
        verify_task_source(plan['source_ref'], Path(outcome['task_root']), outcome)
    return 'waiting_result', '成果已核验，可接续下一步骤。', outcome


def _outputs(outcome, base, *, inputs=None):
    _require(type(outcome.get('artifacts')) is list and 1 <= len(outcome['artifacts']) <= 4)
    records = deepcopy(outcome['artifacts'])
    proof = None
    task = Path(outcome['task_root']) if type(outcome.get('task_root')) is str else None
    mail_claim = task / 'mail-analysis-claim.json' if task is not None else None
    if mail_claim is not None:
        workspace._safe_components(mail_claim)
    if (mail_claim is not None and mail_claim.exists()
            or any(type(item) is dict and Path(item.get('path', '')).suffix.lower() == '.eml'
                   for item in records)):
        from . import office_resume
        _require(task is not None and outcome.get('ok') is True and outcome.get('state') == 'succeeded',
                 'office_chain_mail_outputs_invalid')
        office_resume._assert_plain_office(task)
        parent, _ = workspace._read(task / 'result.json', 1_000_000)
        _require('mail_understanding' in parent,
                 'office_chain_mail_outputs_invalid')
        # The snapshot rechecks every receipt, draft MIME/attachment byte and
        # the exact source/candidate/review/workflow binding, including resumes.
        snapshot = office_resume._snapshot(task, Path(base))
        _require(len(snapshot['receipts']) == len(snapshot['steps']),
                 'office_chain_mail_outputs_invalid')
        expected = [{'path': snapshot['receipts'][step['id']]['artifact'],
                     'sha256': snapshot['receipts'][step['id']]['sha256']}
                    for step in snapshot['steps']]
        _require(records == expected, 'office_chain_mail_outputs_invalid')
        _require(all(Path(item['path']).suffix.lower() in {'.docx', '.xlsx', '.eml'}
                     for item in records), 'office_chain_mail_outputs_invalid')
        if any(step['action'] == 'email_draft' for step in snapshot['steps']):
            _require(parent.get('mail_analysis_schema') in (3, 4),
                     'office_chain_mail_outputs_invalid')
            proof = {name: snapshot[name] for name in ('original_sha256', 'manifest_sha256', 'token')}
            records = [item for item in records if Path(item['path']).suffix.lower() != '.eml']
    _verified({'draft': '', 'source': None, 'records': records}, base, inputs=inputs)
    return records, proof


def _completion(path, *, with_bytes=False):
    value, raw = workspace._read(path, _LIMIT)
    keys = {'schema', 'preview_sha256', 'outcome', 'records', 'sha256'}
    _require(type(value.get('schema')) is int and value['schema'] in (1, 2))
    _require(set(value) == keys | ({'mail_proof'} if value['schema'] == 2 else set()))
    _require(value['sha256'] == digest({k: v for k, v in value.items() if k != 'sha256'}))
    return (value, raw) if with_bytes else value


def _manifest(folder, ident):
    manifest = _read(folder / 'manifest.json',
                     {'schema', 'id', 'instructions', 'payload', 'task_base', 'computer_base'})
    _require(type(manifest['schema']) is int and manifest['schema'] in (1, 2) and manifest['id'] == ident)
    _require(_instructions(manifest['instructions']) == manifest['instructions'])
    _require((manifest['schema'] == 1 and len(manifest['instructions']) == 2)
             or (manifest['schema'] == 2 and 3 <= len(manifest['instructions']) <= MAX_STEPS))
    for name in ('task_base', 'computer_base'):
        _require(str(_root(manifest[name])) == manifest[name])
    workspace._payload(manifest['payload'])
    return manifest


def _load(store, ident, *, resume_attempt=None):
    office_chain_payload.invalidate()
    inputs = InputVerifications()
    result = _load_checked(store, ident, resume_attempt=resume_attempt, inputs=inputs)
    _require(inputs.recheck(), 'office_chain_source_changed')
    if office_chain_payload.collecting():
        state, base = result[2], result[1]['task_base']
        office_chain_payload.publish(state, base, inputs.completed_proof(state['payload'], base))
    return result


def _load_checked(store, ident, *, resume_attempt=None, inputs):
    folder = _folder(store, ident)
    manifest = _manifest(folder, ident)
    total = len(manifest['instructions'])
    saved_steps = set()
    for path in folder.glob('step-*.json'):
        match = re.fullmatch(r'step-(0|[1-9][0-9]*)-.*\.json', path.name)
        _require(match is not None and int(match[1]) < total, 'office_chain_step_changed')
        saved_steps.add(int(match[1]))
    initial = _verified(manifest['payload'], manifest['task_base'], inputs=inputs)['payload']
    _require(initial['source'] is not None or initial['records'], 'office_chain_source_required')
    payload = {'draft': manifest['instructions'][0],
               'records': [initial['source']] if initial['source'] is not None else initial['records'],
               'source': initial['source']}
    state = {'id': ident, 'index': 0, 'total': total, 'status': 'ready', 'payload': payload,
             'scope': 'selected' if initial['source'] is not None else 'batch',
             'binding': None, 'run': None, 'edit_payload': None, 'edit_plan': None, 'edit_revision': None,
             'task_root': None, 'resume_preview': None, 'resume_original_state': None,
             'deliveries': [], 'completed_office': [],
             'reason': '核对本步骤输入后规划。'}
    for index in range(total):
        preview_path = folder / f'step-{index}-preview.json'
        complete_path = folder / f'step-{index}-complete.json'
        for path in (preview_path, complete_path):
            workspace._safe_components(path)
        if not preview_path.exists():
            _require(resume_attempt is None, 'office_chain_resume_unverified')
            _require(not complete_path.exists())
            _require(not list(folder.glob(f'step-{index}-edit*.json')))
            _require(not any(saved > index for saved in saved_steps))
            break
        preview = _read(preview_path, {'schema', 'chain_sha256', 'index', 'binding'})
        complete = _completion(complete_path) if complete_path.exists() else None
        _require(type(preview['schema']) is int and preview['schema'] == 1
                 and type(preview['index']) is int and preview['index'] == index
                 and preview['chain_sha256'] == manifest['sha256'])
        run = _bound(manifest, index, preview['binding'], state['payload'], inputs=inputs)
        state.update(binding=preview['binding'])
        anchor = preview['sha256']
        if run is None:
            _require(complete is not None or resume_attempt is None, 'office_chain_resume_unverified')
            from . import office_task_chain_edit as editing
            outcome, anchor = editing.load(folder, manifest, index, preview, state, inputs=inputs)
        else:
            _require(not list(folder.glob(f'step-{index}-edit*.json')))
            plan = preview['binding']['routed']['plan']
            if plan.get('schema') == 4 or plan.get('mode') == 'office_bundle':
                state['task_root'] = str(Path(manifest['task_base']) / ('task-' + plan['request_id']))
            if complete is None and resume_attempt is not None:
                _require(plan.get('schema') == 4 or plan.get('mode') == 'office_bundle',
                         'office_chain_resume_unverified')
            status, reason, outcome = _outcome(manifest, run, state=state,
                completed=complete['outcome'] if complete is not None else None,
                resume_attempt=resume_attempt if complete is None else None)
            state.update(status=status, reason=reason, run=str(run))
        if not complete_path.exists():
            _require(not any(saved > index for saved in saved_steps))
            if (outcome is not None and outcome.get('ok') is True
                    and any(Path(item['path']).suffix.lower() == '.eml'
                            for item in outcome.get('artifacts', []))):
                records, _ = _outputs(outcome, manifest['task_base'], inputs=inputs)
                if not records:
                    state.update(status='blocked', blocked_code='office_chain_mail_reply_only',
                                 reason='本步骤仅生成邮件草稿，不能接续办公清单；草稿保留在原任务中。')
            return folder, manifest, state, outcome, anchor
        _require(complete['preview_sha256'] == anchor
                 and outcome is not None and outcome.get('ok') is True
                 and complete['outcome'] == outcome)
        records, proof = _outputs(outcome, manifest['task_base'], inputs=inputs)
        _require((complete['schema'] == 1 and proof is None)
                 or (complete['schema'] == 2 and proof is not None
                     and complete['mail_proof'] == proof), 'office_chain_mail_binding_changed')
        _require(records, 'office_chain_mail_reply_only')
        _require(complete['records'] == records)
        state['completed_office'].append({'index': index, 'records': deepcopy(records)})
        if proof is not None:
            state['deliveries'].append({'index': index, 'task_root': outcome['task_root'],
                                        'records': deepcopy(outcome['artifacts'])})
        remaining = index + 1 < total
        state.update(index=index + 1, status='ready' if remaining else 'complete',
                     scope='batch', binding=None, run=None, edit_payload=None, edit_plan=None, edit_revision=None,
                     task_root=None, resume_preview=None, resume_original_state=None,
                     reason='核对全部上一步成果后规划。' if remaining else '全部步骤均已核验完成。',
                     payload={'draft': manifest['instructions'][index + 1] if remaining else '',
                              'records': records, 'source': None})
    _require(resume_attempt is None, 'office_chain_resume_unverified')
    return folder, manifest, state, None, None


@_public
def create(store, instructions, payload, task_base, computer_base):
    """Persist 2..4 explicit instructions and verified input; return the chain id."""
    instructions = _instructions(instructions)
    base, computer = _root(task_base), _root(computer_base)
    checked = _verified(payload, base)['payload']
    _require(checked['source'] is not None or checked['records'], 'office_chain_source_required')
    root = _root(store, create=True)
    with _owner(root):
        ident = uuid.uuid4().hex
        folder = _root(root / ('chain-' + ident), create=True)
        with _owner(folder):
            _append(folder / 'manifest.json', {'schema': 1 if len(instructions) == 2 else 2,
                     'id': ident, 'instructions': instructions,
                     'payload': checked, 'task_base': str(base), 'computer_base': str(computer)})
    return ident


@_public
def inspect(store, ident):
    """Revalidate saved proofs without advancing, executing or creating files."""
    folder = _folder(store, ident)
    with _owner(folder):
        return deepcopy(_load(store, ident)[2])


@_public
def list_chains(store, limit=12):
    """List metadata from at most 100 entries; restore always revalidates proofs."""
    _require(type(limit) is int and 1 <= limit <= 12)
    root = _root(store)
    if not root.exists():
        return []
    values = []
    for entry in islice(root.iterdir(), 100):
        if re.fullmatch(r'chain-[0-9a-f]{32}', entry.name) is None:
            continue
        try:
            folder = _root(entry)
            manifest = _manifest(folder, entry.name[6:])
            values.append((folder.stat().st_mtime_ns,
                           {'id': manifest['id'], 'instructions': manifest['instructions']}))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return [deepcopy(value) for _, value in sorted(values, key=lambda item: item[0], reverse=True)[:limit]]


@_public
def bind_preview(store, ident, index, session, routed, refs):
    """Bind only this step's exact persisted routing preview, once."""
    _require(type(index) is int and 0 <= index < MAX_STEPS, 'office_chain_step_changed')
    folder = _folder(store, ident)
    with _owner(folder):
        _, manifest, state, _, _ = _load(store, ident)
        office_chain_payload.invalidate()  # Further binding reads need a fresh final proof.
        _require(index < len(manifest['instructions']), 'office_chain_step_changed')
        binding = {'session_root': str(_root(session.root)), 'routed': deepcopy(routed), 'refs': deepcopy(refs)}
        path = folder / f'step-{index}-preview.json'
        if path.exists():
            previous = _read(path, {'schema', 'chain_sha256', 'index', 'binding'})
            _require(previous['binding'] == binding, 'office_chain_preview_conflict')
            return deepcopy(state)
        _require(state['index'] == index and state['status'] == 'ready', 'office_chain_step_changed')
        _bound(manifest, index, binding, state['payload'])
        _append(path, {'schema': 1, 'chain_sha256': manifest['sha256'], 'index': index, 'binding': binding})
        return deepcopy(_load(store, ident)[2])


@_public
def advance(store, ident, *, resume_attempt=None, resume_index=None):
    """Append at most one verified successful stage; never retry any work."""
    folder = _folder(store, ident)
    _require(resume_attempt is None or type(resume_attempt) is str and workspace._ID.fullmatch(resume_attempt),
             'office_chain_resume_attempt_invalid')
    _require(resume_index is None or (resume_attempt is not None
             and type(resume_index) is int and 0 <= resume_index < MAX_STEPS),
             'office_chain_resume_index_invalid')
    with _owner(folder):
        if resume_index is not None:
            path = folder / f'step-{resume_index}-complete.json'
            workspace._safe_components(path)
            if path.exists():
                pinned = _completion(path, with_bytes=True)
                outcome = pinned[0]['outcome']
                _require(type(outcome) is dict, 'office_chain_resume_unverified')
                proofs = [outcome[name] for name in ('mail_resume', 'bundle_resume') if name in outcome]
                _require(len(proofs) == 1 and type(proofs[0]) is dict
                         and proofs[0].get('attempt_id') == resume_attempt,
                         'office_chain_resume_unverified')
                state = _load(store, ident)[2]
                _require(_completion(path, with_bytes=True) == pinned
                         and state['index'] > resume_index, 'office_chain_resume_binding_changed')
                return deepcopy(state)
        _, manifest, state, outcome, anchor = _load(store, ident, resume_attempt=resume_attempt)
        _require(resume_index is None or state['index'] == resume_index,
                 'office_chain_resume_index_invalid')
        if outcome is None or outcome.get('ok') is not True or state['status'] == 'blocked':
            return deepcopy(state)
        office_chain_payload.invalidate()
        index = state['index']
        records, proof = _outputs(outcome, manifest['task_base'])
        _require(records, 'office_chain_mail_reply_only')
        completion = {'schema': 1 if proof is None else 2, 'preview_sha256': anchor,
                      'outcome': outcome, 'records': records}
        if proof is not None:
            completion['mail_proof'] = proof
        _append(folder / f'step-{index}-complete.json', completion)
        return deepcopy(_load(store, ident)[2])


@_public
def bind_edit_preview(store, ident, index, session, plan, planning_result, office_payload):
    """Append one exact existing Office intent result to a bound edit route."""
    from . import office_task_chain_edit as editing
    _require(type(index) is int and 0 <= index < MAX_STEPS, 'office_chain_step_changed')
    folder = _folder(store, ident)
    with _owner(folder):
        _, manifest, state, _, _ = _load(store, ident)
        office_chain_payload.invalidate()
        _require(index < len(manifest['instructions']), 'office_chain_step_changed')
        preview = _read(folder / f'step-{index}-preview.json', {'schema', 'chain_sha256', 'index', 'binding'})
        _require(preview['binding']['routed']['state'] == 'needs_scope', 'office_chain_route_unsupported')
        record = editing.capture(manifest, index, preview['binding'], preview['sha256'],
                                 session, plan, planning_result, office_payload)
        path = folder / f'step-{index}-edit.json'
        workspace._safe_components(path)
        if path.exists():
            _require(_read(path, editing._EDIT_KEYS) == _sealed(record), 'office_chain_edit_preview_conflict')
            return deepcopy(state)
        _require(state['index'] == index and state['status'] == 'needs_scope', 'office_chain_step_changed')
        run = _root(session.root / 'goals' / (plan['sha256'] + '.run'))
        _require(not run.exists(), 'office_chain_edit_already_started')
        _append(path, record)
        return deepcopy(_load(store, ident)[2])


@_public
def bind_edit_refresh(store, ident, index, session, plan):
    """Append a local same-goal preview revision only before any dispatch."""
    from . import office_task_chain_edit as editing
    _require(type(index) is int and 0 <= index < MAX_STEPS, 'office_chain_step_changed')
    folder = _folder(store, ident)
    with _owner(folder):
        _, manifest, state, _, _ = _load(store, ident)
        office_chain_payload.invalidate()
        _require(index < len(manifest['instructions']), 'office_chain_step_changed')
        _require(state['index'] == index and state['edit_plan'] is not None, 'office_chain_step_changed')
        preview = _read(folder / f'step-{index}-preview.json', {'schema', 'chain_sha256', 'index', 'binding'})
        editing.refresh(folder, manifest, index, preview, state, session, plan)
        return deepcopy(_load(store, ident)[2])
