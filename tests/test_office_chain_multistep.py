"""Real persisted 2..4-step provenance; all Office execution uses offline packages."""
from copy import deepcopy
import importlib
from pathlib import Path

import pytest

from agent_service import office_task_chain as chain, daily_task_routing
from agent_service.computer import digest, read_record, write_new
from test_office_task_chain import local, route, succeed_capability


def create_steps(s, total):
    instructions = [f'第{i + 1}步：依据当前成果生成一份明确的新表格' for i in range(total)]
    s.ident = chain.create(s.store, instructions, s.payload, s.base, s.session.root.parent)
    return instructions


def folder(s):
    return s.store / ('chain-' + s.ident)


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def complete_step(s, index):
    view, refs = route(s, index)
    preview = chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
    assert preview['index'] == index and preview['status'] == 'preview'
    assert not Path(preview['run']).exists()
    succeed_capability(s, view, count=1)
    return chain.advance(s.store, s.ident)


def seal_manifest(s, **changes):
    path = folder(s) / 'manifest.json'
    value = read_record(path)
    value.pop('sha256')
    value.update(changes)
    value['sha256'] = digest(value)
    path.write_bytes(chain.workspace._encode(value))


def test_original_two_step_schema_and_advance_remain_compatible(local):
    s = local
    manifest = read_record(folder(s) / 'manifest.json')
    assert manifest['schema'] == 1 and len(manifest['instructions']) == 2
    initial = chain.inspect(s.store, s.ident)
    assert initial['total'] == 2 and initial['index'] == 0
    before = snapshot(folder(s))
    assert importlib.reload(chain).inspect(s.store, s.ident) == initial
    assert chain.advance(s.store, s.ident) == initial and snapshot(folder(s)) == before
    complete_step(s, 0)
    final = complete_step(s, 1)
    assert final['total'] == final['index'] == 2 and final['status'] == 'complete'


@pytest.mark.parametrize('total', [3, 4])
def test_multistep_completes_in_order_after_each_restart_without_redoing_completed_steps(local, total):
    s = local
    instructions = create_steps(s, total)
    manifest = read_record(folder(s) / 'manifest.json')
    assert manifest['schema'] == 2 and manifest['instructions'] == instructions
    for index in range(total):
        before = chain.inspect(s.store, s.ident)
        assert before['index'] == index and before['total'] == total
        assert before['payload']['draft'] == instructions[index]
        state = complete_step(s, index)
        assert state['index'] == index + 1 and state['total'] == total
        assert state['status'] == ('complete' if index + 1 == total else 'ready')
        durable = snapshot(s.base)
        records = snapshot(folder(s))
        # Reload uses the real saved parent proofs rather than an in-memory cursor.
        assert importlib.reload(chain).inspect(s.store, s.ident) == state
        assert chain.advance(s.store, s.ident) == state
        assert snapshot(s.base) == durable and snapshot(folder(s)) == records
        assert len(list(folder(s).glob('step-*-complete.json'))) == index + 1
    assert state['payload']['draft'] == '' and state['payload']['source'] is None


def test_third_step_failure_preserves_both_completed_steps_and_never_replays(local):
    s = local
    create_steps(s, 4)
    complete_step(s, 0)
    previous = complete_step(s, 1)
    completed_before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in folder(s).glob('*complete.json')}
    view, refs = route(s, 2)
    preview = chain.bind_preview(s.store, s.ident, 2, s.session, view, refs)
    run = Path(preview['run'])
    run.mkdir()
    write_new(run / 'claim.json', {'approval': view['plan']['sha256'],
                                  'task_root': str(s.base / ('task-' + view['plan']['request_id']))})
    write_new(run / 'outcome.json', {'ok': False, 'state': 'cancelled', 'code': 'capability_cancelled',
                                   'artifacts': [], 'run': str(run), 'before_start': True})
    failed = chain.advance(s.store, s.ident)
    assert failed['status'] == 'blocked' and failed['index'] == 2 and failed['total'] == 4
    assert failed['payload'] == previous['payload']
    saved = snapshot(folder(s))
    assert importlib.reload(chain).advance(s.store, s.ident) == failed
    assert snapshot(folder(s)) == saved and not (folder(s) / 'step-2-complete.json').exists()
    assert completed_before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                for p in folder(s).glob('*complete.json')}


@pytest.mark.parametrize('total', [3, 4])
def test_missing_middle_completion_rejects_future_bound_step(local, total):
    s = local
    create_steps(s, total)
    complete_step(s, 0)
    complete_step(s, 1)
    view, refs = route(s, 2)
    chain.bind_preview(s.store, s.ident, 2, s.session, view, refs)
    missing = folder(s) / 'step-1-complete.json'
    missing.rename(folder(s) / 'held-middle-completion.json')
    before = snapshot(folder(s))
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)
    assert snapshot(folder(s)) == before


@pytest.mark.parametrize('record', ['preview', 'complete', 'edit-refresh-1'])
def test_future_third_step_record_without_earlier_completion_is_rejected(local, record):
    s = local
    create_steps(s, 4)
    write_new(folder(s) / ('step-2-' + record + '.json'), {})
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)


@pytest.mark.parametrize('index', [-1, 3, True])
def test_binding_outside_actual_three_step_manifest_is_rejected(local, index):
    s = local
    create_steps(s, 3)
    view, refs = route(s)
    before = snapshot(folder(s))
    with pytest.raises(chain.OfficeTaskChainError, match='step_changed'):
        chain.bind_preview(s.store, s.ident, index, s.session, view, refs)
    assert snapshot(folder(s)) == before


@pytest.mark.parametrize('total', [3, 4])
def test_schema_two_cannot_be_downgraded_to_legacy_schema_one(local, total):
    s = local
    create_steps(s, total)
    seal_manifest(s, schema=1)
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)


def third_edit_route(s):
    create_steps(s, 3)
    complete_step(s, 0)
    state = complete_step(s, 1)
    refs = chain._refs(state['payload'], s.base)
    class Client:
        def complete(self, *_args, **_kwargs):
            return {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '第三步明确字面编辑'}
    routed = daily_task_routing.propose(s.session, state['payload']['draft'], refs,
                                       task_base=s.base, client=Client())
    bound = chain.bind_preview(s.store, s.ident, 2, s.session, routed, refs)
    assert bound['status'] == 'needs_scope' and bound['index'] == 2
    return bound


@pytest.mark.parametrize('endpoint', ['preview', 'refresh'])
def test_third_step_edit_binding_reaches_existing_edit_validator(local, monkeypatch, endpoint):
    from agent_service import office_task_chain_edit as editing
    s = local
    third_edit_route(s)
    reached = []
    class ReachedEditValidator(RuntimeError):
        pass
    def capture(manifest, index, *_args):
        reached.append((len(manifest['instructions']), index))
        raise ReachedEditValidator('existing edit validator reached')
    if endpoint == 'preview':
        monkeypatch.setattr(editing, 'capture', capture)
        call = lambda: chain.bind_edit_preview(s.store, s.ident, 2, s.session, {}, {}, {})
    else:
        original_load = chain._load
        def loaded(*args, **kwargs):
            result = list(original_load(*args, **kwargs))
            result[2] = {**deepcopy(result[2]), 'edit_plan': {'existing': 'validated separately'}}
            return tuple(result)
        monkeypatch.setattr(chain, '_load', loaded)
        monkeypatch.setattr(editing, 'refresh', lambda _folder, manifest, index, *_args:
                            capture(manifest, index))
        call = lambda: chain.bind_edit_refresh(s.store, s.ident, 2, s.session, {})
    with pytest.raises(ReachedEditValidator):
        call()
    assert reached == [(3, 2)]
