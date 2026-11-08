"""Real durable routing/provenance checks with offline Office package receipts."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from threading import Barrier

import pytest

from agent_service import office_task_chain as chain, office_bundle, office_workflow
from agent_service import capability_router, workspace_state
from agent_service.computer import ComputerSession, digest, write_new, read_record
from test_artifact_source import owned, fake


TABLE = {'headers': ['项目', '值'], 'rows': [['甲', '三']]}
SHEET = {'kind': 'spreadsheet', 'title': '表', **TABLE, 'sum_columns': []}
DOC = {'kind': 'document', 'title': '报告', 'paragraphs': ['范围'], 'table': TABLE}


@pytest.fixture
def local(tmp_path, monkeypatch):
    base, _, _, receipt = owned(tmp_path, SHEET, second=DOC)
    records = [{'path': r['artifact'], 'sha256': r['sha256']} for r in receipt['receipts'].values()]
    payload = {'draft': '原草稿', 'records': records, 'source': records[1]}
    session = ComputerSession.create(tmp_path / 'computer')
    (session.root / 'goals').mkdir()
    (session.root / 'task_routes').mkdir()
    monkeypatch.setattr(office_bundle, 'BASE', session.root.parent)
    store = tmp_path / 'chains'
    ident = chain.create(store, ['先从选中文档生成Excel和Word', '逐格核对上一步全部成果并生成Excel和Word'],
                         payload, base, session.root.parent)
    return SimpleNamespace(base=base, session=session, payload=payload, store=store, ident=ident)


def route(s, index=0, *, instruction=None, refs=None, decision=None, schema=1):
    state = chain.inspect(s.store, s.ident)
    payload = state['payload']
    refs = chain._refs(payload, s.base) if refs is None else refs
    instruction = payload['draft'] if instruction is None else instruction
    selected = decision or ('single_source' if len(refs) == 1 else 'compare_tables')
    req = {'schema': 3, 'routing_rules_sha256': 'a' * 64, 'instruction': instruction,
           'source_refs': refs, 'task_base': str(s.base)}
    plan = {'schema': schema, 'mode': 'office_bundle' if selected == 'compare_tables' else 'capability',
            'instruction': instruction, 'task_base': str(s.base), 'request_id': uuid.uuid4().hex}
    if selected == 'compare_tables':
        plan['source_refs'] = refs
    else:
        plan.update(source_ref=refs[0], candidates=[])
    plan['sha256'] = digest(plan)
    write_new(s.session.root / 'goals' / (plan['sha256'] + '.json'), plan)
    root = s.session.root / 'task_routes' / digest(req)
    root.mkdir()
    write_new(root / 'request.json', req)
    ids = [f'S{i}' for i in range(1, len(refs) + 1)] if selected == 'compare_tables' else ['S1']
    write_new(root / 'decision.json', {'request_sha256': digest(req), 'decision': {
        'route': selected, 'source_ids': ids, 'reason': '仅处理本步骤全部输入'}})
    write_new(root / 'outcome.json', {'schema': 1, 'request_sha256': digest(req),
                                    'state': 'ready', 'code': 'planned', 'plan': plan})
    view = {'state': 'ready', 'reason': '仅处理本步骤全部输入', 'plan': plan, 'routing': str(root)}
    return view, refs


def bind(s, index=0, **kwargs):
    view, refs = route(s, index, **kwargs)
    state = chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
    return state, view, refs


def succeed_capability(s, view, *, count=2, source=None):
    plan = view['plan']
    task = s.base / ('task-' + plan['request_id'])
    task.mkdir()
    with (task / 'request.txt').open('x', encoding='utf-8') as stream:
        stream.write(plan['instruction'])
    steps = [{'id': 's1', 'action': 'spreadsheet', 'data': SHEET}]
    if count == 2:
        steps.append({'id': 's2', 'action': 'document', 'data': DOC})
    workflow = task / ('workflow-' + uuid.uuid4().hex)
    receipt = office_workflow.run_office_workflow(steps, workflow, execute=fake)
    selected = plan['source_ref'] if source is None else source
    parent = {'ok': True, 'state': 'succeeded', 'kind': 'office', 'root': str(task),
              'workflow_root': str(workflow), 'completed': receipt['completed'],
              'source': {'sha256': selected['sha256']},
              'source_provenance': {'kind': 'verified_generated_office_artifact', **selected},
              'artifacts': [{'step': key, 'path': r['artifact'], 'sha256': r['sha256']}
                            for key, r in receipt['receipts'].items()]}
    write_new(task / 'result.json', parent)
    run = s.session.root / 'goals' / (plan['sha256'] + '.capability')
    run.mkdir()
    write_new(run / 'claim.json', {'approval': plan['sha256'], 'task_root': str(task)})
    outcome = {**capability_router._inspect(task, s.base), 'run': str(run), 'approval': plan['sha256']}
    write_new(run / 'outcome.json', outcome)
    return run, outcome


def test_create_retains_original_batch_but_first_step_only_selected(local):
    s = local
    state = chain.inspect(s.store, s.ident)
    assert state['scope'] == 'selected' and state['index'] == 0 and state['status'] == 'ready'
    assert state['payload']['records'] == [s.payload['source']]
    manifest = read_record(s.store / ('chain-' + s.ident) / 'manifest.json')
    assert manifest['payload'] == s.payload
    assert chain.advance(s.store, s.ident) == state
    assert chain.list_chains(s.store) == [{'id': s.ident, 'instructions': manifest['instructions']}]


def test_full_chain_restart_all_outputs_and_explicit_approval(local):
    s = local
    preview, view, refs = bind(s)
    assert preview['status'] == 'preview'
    assert chain.advance(s.store, s.ident) == preview
    run, outcome = succeed_capability(s, view)
    waiting = chain.inspect(s.store, s.ident)
    assert waiting['status'] == 'waiting_result' and waiting['index'] == 0
    # Reload the module, retaining only durable files and the chain id.
    restored = importlib.reload(chain).advance(s.store, s.ident)
    assert restored['index'] == 1 and restored['scope'] == 'batch'
    assert restored['payload']['source'] is None
    assert restored['payload']['records'] == outcome['artifacts']
    assert restored['completed_office'] == [{'index': 0, 'records': outcome['artifacts']}]
    assert chain.advance(s.store, s.ident) == restored
    assert chain.bind_preview(s.store, s.ident, 0, s.session, view, refs) == restored
    second, view2, _ = bind(s, 1)
    assert second['status'] == 'preview'
    class Client:
        def complete(self, *args, **kwargs):
            return {'supported': True, 'reason': '逐格核对'}
    def offline(steps, root, **kwargs):
        return office_workflow.run_office_workflow(steps, root, execute=fake, **kwargs)
    result = office_bundle.execute(s.session, view2['plan'], approval=view2['plan']['sha256'],
                                  task_base=s.base, client=Client(), workflow_runner=offline)
    assert result['ok'] is True
    final = chain.advance(s.store, s.ident)
    assert final['index'] == 2 and final['status'] == 'complete' and final['payload']['draft'] == ''
    assert final['payload']['records'] == result['artifacts']
    assert final['completed_office'] == [{'index': 0, 'records': outcome['artifacts']},
                                        {'index': 1, 'records': result['artifacts']}]
    before = {p: p.read_bytes() for p in (s.store / ('chain-' + s.ident)).glob('*.json')}
    assert chain.advance(s.store, s.ident) == final
    assert chain.inspect(s.store, s.ident) == final
    assert before == {p: p.read_bytes() for p in before}
    (Path(result['task_root']) / 'request.txt').write_text('changed bundle task', encoding='utf-8')
    with pytest.raises(chain.OfficeTaskChainError, match='task_instruction_changed'):
        chain.inspect(s.store, s.ident)


def test_second_single_artifact_is_supported(local):
    s = local
    _, view, _ = bind(s)
    succeed_capability(s, view, count=1)
    assert chain.advance(s.store, s.ident)['index'] == 1
    _, second, _ = bind(s, 1)
    succeed_capability(s, second, count=1)
    assert chain.advance(s.store, s.ident)['status'] == 'complete'


@pytest.mark.parametrize('mutation', ['instruction', 'source', 'refs', 'base', 'session', 'plan', 'routing'])
def test_binding_rejects_wrong_request_and_structural_tamper(local, mutation):
    s = local
    view, refs = route(s, instruction='另一条指令' if mutation == 'instruction' else None)
    session = s.session
    if mutation == 'source':
        view['plan']['source_ref'] = deepcopy(refs[0]); view['plan']['source_ref']['sha256'] = 'b' * 64
    elif mutation == 'refs':
        refs = refs * 2
    elif mutation == 'base':
        view['plan']['task_base'] = str(s.base.parent)
    elif mutation == 'session':
        session = ComputerSession.create(s.store / 'other-computers')
    elif mutation == 'plan':
        (session.root / 'goals' / (view['plan']['sha256'] + '.json')).write_text('{}')
    elif mutation == 'routing':
        view['routing'] = str(Path(view['routing']).parent)
    with pytest.raises(chain.OfficeTaskChainError):
        chain.bind_preview(s.store, s.ident, 0, session, view, refs)
    assert chain.inspect(s.store, s.ident)['status'] == 'ready'


def test_bound_preview_replay_and_conflict(local):
    s = local
    first, view, refs = bind(s)
    assert chain.bind_preview(s.store, s.ident, 0, s.session, view, refs) == first
    changed = deepcopy(view); changed['reason'] = '另一个预览'
    with pytest.raises(chain.OfficeTaskChainError, match='preview_conflict'):
        chain.bind_preview(s.store, s.ident, 0, s.session, changed, refs)
    with pytest.raises(chain.OfficeTaskChainError, match='step_changed'):
        chain.bind_preview(s.store, s.ident, 1, s.session, view, refs)


def test_multiple_outputs_cannot_be_dropped_by_second_route(local):
    s = local
    _, view, _ = bind(s); succeed_capability(s, view)
    chain.advance(s.store, s.ident)
    view2, refs = route(s, 1, decision='single_source')
    with pytest.raises(chain.OfficeTaskChainError, match='route_unsupported'):
        chain.bind_preview(s.store, s.ident, 1, s.session, view2, refs)
    assert chain.inspect(s.store, s.ident)['index'] == 1


@pytest.mark.parametrize('routed', [
    {'state': 'needs_input', 'reason': '澄清', 'plan': None, 'routing': 'unused'},
    {'state': 'needs_scope', 'reason': '编辑', 'plan': None, 'routing': 'unused'},
    {'state': 'blocked', 'reason': '不支持', 'plan': None, 'routing': 'unused'},
])
def test_unsupported_route_preserves_two_instructions(local, routed):
    s = local
    refs = chain._refs(chain.inspect(s.store, s.ident)['payload'], s.base)
    with pytest.raises(chain.OfficeTaskChainError, match='route_unsupported'):
        chain.bind_preview(s.store, s.ident, 0, s.session, routed, refs)
    assert len(chain.list_chains(s.store)[0]['instructions']) == 2
    assert chain.advance(s.store, s.ident)['index'] == 0


def test_started_unknown_is_never_replayed_or_completed(local):
    s = local
    _, view, _ = bind(s)
    run = s.session.root / 'goals' / (view['plan']['sha256'] + '.capability')
    run.mkdir()
    for _ in range(2):
        state = chain.advance(s.store, s.ident)
        assert state['index'] == 0 and state['status'] == 'waiting_result'
    assert not list(run.iterdir())


def test_cancelled_first_step_is_blocked_and_retains_sources(local):
    s = local
    _, view, _ = bind(s)
    plan = view['plan']
    run = s.session.root / 'goals' / (plan['sha256'] + '.capability'); run.mkdir()
    write_new(run / 'claim.json', {'approval': plan['sha256'],
                                  'task_root': str(s.base / ('task-' + plan['request_id']))})
    write_new(run / 'outcome.json', {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                                   'artifacts': [], 'run': str(run), 'before_start': True})
    state = chain.advance(s.store, s.ident)
    assert state['status'] == 'blocked' and state['index'] == 0
    assert state['payload']['records'] == [s.payload['source']]
    assert chain.advance(s.store, s.ident) == state


def test_failed_second_step_preserves_first_completion(local):
    s = local
    _, view, _ = bind(s); succeed_capability(s, view)
    first = chain.advance(s.store, s.ident)
    _, second, _ = bind(s, 1)
    plan = second['plan']
    run = s.session.root / 'goals' / (plan['sha256'] + '.bundle'); run.mkdir()
    write_new(run / 'claim.json', {'approval': plan['sha256'],
                                  'task_root': str(s.base / ('task-' + plan['request_id']))})
    write_new(run / 'outcome.json', {'ok': False, 'state': 'unknown', 'code': 'interrupted',
                                   'artifacts': [], 'run': str(run)})
    state = chain.advance(s.store, s.ident)
    assert state['status'] == 'blocked' and state['index'] == 1
    assert state['payload'] == first['payload']
    assert chain.advance(s.store, s.ident) == state


@pytest.mark.parametrize('location', ['manifest', 'preview', 'completion', 'outcome', 'artifact', 'source'])
def test_completed_history_revalidated_and_never_reset(local, location):
    s = local
    _, view, _ = bind(s)
    run, outcome = succeed_capability(s, view)
    chain.advance(s.store, s.ident)
    folder = s.store / ('chain-' + s.ident)
    paths = {'manifest': folder / 'manifest.json', 'preview': folder / 'step-0-preview.json',
             'completion': folder / 'step-0-complete.json', 'outcome': run / 'outcome.json',
             'artifact': Path(outcome['artifacts'][0]['path']), 'source': Path(s.payload['source']['path'])}
    paths[location].write_bytes(b'{}')
    before = {p.name: p.read_bytes() for p in folder.glob('*.json')}
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident)
    assert before == {p.name: p.read_bytes() for p in folder.glob('*.json')}


def test_success_with_wrong_task_source_cannot_advance(local):
    s = local
    _, view, _ = bind(s)
    other = workspace_state.verify(s.payload, s.base)['refs'][0]
    succeed_capability(s, view, source=other)
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident)
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()


@pytest.mark.parametrize('mutation', ['missing', 'changed', 'extra_newline', 'oversize'])
def test_success_task_instruction_must_match_exact_preview(local, mutation):
    s = local
    _, view, _ = bind(s)
    _, outcome = succeed_capability(s, view)
    request = Path(outcome['task_root']) / 'request.txt'
    if mutation == 'missing':
        request.unlink()
    else:
        request.write_bytes({'changed': b'other task', 'extra_newline': request.read_bytes() + b'\n',
                             'oversize': b'x' * (64 * 1024 + 1)}[mutation])
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident)
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()


def test_task_instruction_uses_production_multiline_text_writer(local):
    s = local
    s.ident = chain.create(s.store, ['从文档生成Excel\n并生成Word', '逐格核对全部成果'],
                           s.payload, s.base, s.session.root.parent)
    _, view, _ = bind(s)
    succeed_capability(s, view)
    assert chain.advance(s.store, s.ident)['index'] == 1


def test_completed_instruction_is_rechecked_before_continuing(local):
    s = local
    _, view, _ = bind(s)
    _, outcome = succeed_capability(s, view)
    assert chain.advance(s.store, s.ident)['index'] == 1
    request = Path(outcome['task_root']) / 'request.txt'
    request.write_text('changed task', encoding='utf-8')
    with pytest.raises(chain.OfficeTaskChainError, match='task_instruction_changed'):
        chain.advance(s.store, s.ident)


@pytest.mark.parametrize('instructions', [[], ['one'], ['one', 'two', 'three', 'four', 'five'], ['', 'two'], ['x' * 4001, 'two']])
def test_requires_two_to_four_bounded_instructions(local, instructions):
    s = local
    with pytest.raises(chain.OfficeTaskChainError):
        chain.create(s.store, instructions, s.payload, s.base, s.session.root.parent)


def test_trusted_absolute_roots_no_default_and_oversize_duplicate_records(local):
    s = local
    with pytest.raises(chain.OfficeTaskChainError):
        chain.create(s.store, ['a', 'b'], s.payload, Path('relative'), s.session.root.parent)
    folder = s.store / ('chain-' + s.ident)
    path = folder / 'manifest.json'
    original = path.read_bytes()
    path.write_bytes(b'x' * (64 * 1024 + 1))
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)
    assert chain.list_chains(s.store) == []
    path.write_bytes(original[:-1] + b',"schema":1}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)


def test_inspection_is_read_only_and_busy_lock_does_not_change_progress(local):
    s = local
    folder = s.store / ('chain-' + s.ident)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
    state = chain.inspect(s.store, s.ident)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
    with office_workflow._owner(folder):
        with pytest.raises(chain.OfficeTaskChainError, match='busy'):
            chain.advance(s.store, s.ident)
    assert chain.inspect(s.store, s.ident) == state


def test_concurrent_advance_publishes_one_completion(local):
    s = local
    _, view, _ = bind(s); succeed_capability(s, view)
    gate = Barrier(2)
    def attempt():
        gate.wait(timeout=5)
        try:
            return chain.advance(s.store, s.ident)['index']
        except chain.OfficeTaskChainError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = [f.result() for f in futures]
    assert 1 in results and set(results) <= {1, 'office_chain_busy'}
    assert len(list((s.store / ('chain-' + s.ident)).glob('*complete.json'))) == 1


def test_future_stage_without_parent_completion_is_rejected(local):
    s = local
    folder = s.store / ('chain-' + s.ident)
    write_new(folder / 'step-1-complete.json', {})
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)


def test_empty_source_and_batch_cannot_create_chain(local):
    s = local
    payload = {**s.payload, 'source': None, 'records': []}
    with pytest.raises(chain.OfficeTaskChainError, match='source_required'):
        chain.create(s.store, ['a', 'b'], payload, s.base, s.session.root.parent)


def test_explicit_initial_batch_keeps_all_inputs(local):
    s = local
    payload = {**s.payload, 'source': None}
    ident = chain.create(s.store, ['a', 'b'], payload, s.base, s.session.root.parent)
    state = chain.inspect(s.store, ident)
    assert state['scope'] == 'batch' and state['payload'] == {**payload, 'draft': 'a'}
