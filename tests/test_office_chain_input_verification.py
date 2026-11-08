"""Chain-local input reuse, with real routing/package/continuation proofs."""
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import uuid

import pytest

from agent_service import (office_task_chain as chain, office_task_chain_edit as editing,
    office_bundle, office_bundle_resume, office_workflow, office_scope, office_intent,
    office_edit_adapter, daily_task_routing, verification_progress, workspace_state)
from agent_service.computer import ComputerError
from test_office_task_chain_edit import local, rich_fake
from test_office_bundle import Client


def prepare(s):
    instruction = '将两份报告表头“核对结果”改为“复核结论”，其他不变。'
    ident = chain.create(s.store, ['逐格核对两份表格并生成Excel和Word', instruction],
        {'draft': '', 'records': s.records, 'source': None}, s.base, s.session.root.parent)
    class Route:
        def __init__(self, route): self.route = route
        def complete(self, *args, **kwargs):
            return {'route': self.route, 'source_ids': ['S1', 'S2'], 'reason': '两份全部'}
    routed = daily_task_routing.propose(s.session, '逐格核对两份表格并生成Excel和Word',
        s.refs, task_base=s.base, client=Route('compare_tables'))
    chain.bind_preview(s.store, ident, 0, s.session, routed, s.refs)
    def runner(steps, root, **kwargs):
        return office_workflow.run_office_workflow(steps, root, execute=rich_fake,
                                                  pause_after_step=True, **kwargs)
    original = office_bundle.execute(s.session, routed['plan'], approval=routed['plan']['sha256'],
        task_base=s.base, client=Client(), workflow_runner=runner)
    assert original['state'] == 'cancelled'
    task = s.base / ('task-' + routed['plan']['request_id'])
    ready = office_bundle_resume.preview_resume(task, s.base)
    attempt = uuid.uuid4().hex
    resumed = office_bundle_resume.resume_task(task, s.base, ready['token'], attempt, execute=rich_fake)
    assert resumed['ok']
    state = chain.advance(s.store, ident, resume_attempt=attempt)
    refs = chain._refs(state['payload'], s.base)
    edit_route = daily_task_routing.propose(s.session, instruction, refs,
        task_base=s.base, client=Route('office_edit'))
    chain.bind_preview(s.store, ident, 1, s.session, edit_route, refs)
    records = state['payload']['records']
    catalog = office_scope.inspect_files(s.session, [r['path'] for r in records], artifact_refs=refs)
    chosen = office_scope.selection_from_indices(catalog, [(i, j)
        for i, f in enumerate(catalog['files']) for j, row in enumerate(f['entries'])
        if row['value'] == '核对结果'])
    class Planner:
        last_call = {'state': 'offline_only'}
        def complete(self, system, user, **kwargs):
            context = json.loads(user)
            return {'kind': 'edits', 'changes': [{'target': row['target'], 'after': '复核结论'}
                for f in context['files'] for row in f['content']
                if row.get('target') and row['value'] == '核对结果']}
    payload = {'draft': instruction, 'records': records, 'scope': chosen['scope']}
    planning = office_intent.propose(s.session, instruction, [r['path'] for r in records],
        client=Planner(), artifact_refs=refs, scope=payload['scope'])
    state = chain.bind_edit_preview(s.store, ident, 1, s.session, planning['plan'], planning, payload)
    assert state['status'] == 'edit_preview'
    return SimpleNamespace(**vars(s), chain_id=ident, state=state, task=task,
                           attempt=attempt, report_records=records)


@pytest.fixture(scope='module')
def prepared():
    patch = pytest.MonkeyPatch()
    source = local.__wrapped__(patch)
    try:
        yield prepare(next(source))
    finally:
        source.close()
        patch.undo()


@pytest.fixture
def preview(prepared):
    # Reuse the expensive production fixture. Each case restores only this
    # fixture's owned TemporaryDirectory, including its original timestamps.
    root = prepared.root.resolve()
    before = {p: (p.read_bytes(), p.stat()) for p in root.rglob('*') if p.is_file()}
    yield prepared
    for path in root.rglob('*'):
        assert path.resolve().is_relative_to(root)
        if path.is_file() and path not in before:
            path.unlink()
    for path, (raw, stat) in before.items():
        if path.read_bytes() != raw:
            path.write_bytes(raw)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def track(monkeypatch):
    calls = []
    original = workspace_state.verify
    def verify(payload, base):
        calls.append(deepcopy(payload))
        return original(payload, base)
    monkeypatch.setattr(workspace_state, 'verify', verify)
    return calls


def test_preview_uses_two_exact_inputs_twice_and_requests_are_independent(preview, monkeypatch):
    s = preview; calls = track(monkeypatch)
    fixed = s.store / ('chain-' + s.chain_id) / 'step-0-complete.json'
    old = fixed.read_bytes()
    first = chain.inspect(s.store, s.chain_id)
    assert first == s.state
    assert len(calls) == 4
    second = chain.inspect(s.store, s.chain_id)
    assert second == first and len(calls) == 8
    assert fixed.read_bytes() == old
    print('workspace.verify measured: inspect=4, two independent requests=8')


def test_refresh_keeps_two_independent_load_boundaries(preview, monkeypatch):
    s = preview
    fresh = office_edit_adapter.refresh_plan(s.session, s.state['edit_plan'])
    calls = track(monkeypatch)
    result = chain.bind_edit_refresh(s.store, s.chain_id, 1, s.session, fresh)
    assert result['edit_plan'] == fresh and result['edit_revision'] == 1
    assert len(calls) == 8
    print('workspace.verify measured: bind_edit_refresh two loads=8')


def alter_same_mtime(path, raw=b'{}'):
    before = path.stat()
    path.write_bytes(raw)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize('target', ['artifact', 'receipt', 'latest', 'attempt'])
def test_final_production_verification_rejects_late_proof_changes(preview, monkeypatch, target):
    s = preview
    workflow = Path(s.report_records[0]['path']).parent.parent
    paths = {'artifact': Path(s.report_records[0]['path']),
             'receipt': workflow / 's1/receipt.json',
             'latest': s.task / 'resume-latest.json',
             'attempt': s.task / ('resume-' + s.attempt) / 'outcome.json'}
    original = editing.validate
    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        alter_same_mtime(paths[target])
        return result
    monkeypatch.setattr(editing, 'validate', validate)
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.chain_id)


def test_exact_key_validates_draft_and_never_infers_subsets(preview, monkeypatch):
    from agent_service.office_chain_inputs import InputVerifications
    s = preview; calls = track(monkeypatch); checks = InputVerifications()
    payload = {'draft': 'first', 'records': s.records, 'source': None}
    first = checks.verify(payload, s.base)
    first['refs'].clear()
    again = checks.verify({**payload, 'draft': 'second'}, s.base)
    assert again['payload']['draft'] == 'second' and len(again['refs']) == 2 and len(calls) == 1
    with pytest.raises(workspace_state.WorkspaceError):
        checks.verify({**payload, 'draft': None}, s.base)
    checks.verify({**payload, 'source': s.records[0]}, s.base)
    checks.verify({**payload, 'records': s.records[:1]}, s.base)
    checks.verify({**payload, 'records': list(reversed(s.records))}, s.base)
    assert len(calls) == 4
    assert checks.recheck() and len(calls) == 8
    other = s.root / 'other-base'; other.mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        checks.verify(payload, other)
    assert len(calls) == 9
    assert checks.verify(payload, s.base)['payload'] == payload and len(calls) == 9


def test_failed_verification_is_not_retained(preview, monkeypatch):
    from agent_service.office_chain_inputs import InputVerifications
    s = preview; checks = InputVerifications(); calls = []
    original = workspace_state.verify
    def verify(payload, base):
        result = original(payload, base)
        calls.append(result)
        if len(calls) == 1:
            raise ValueError('first verification failed')
        return result
    monkeypatch.setattr(workspace_state, 'verify', verify)
    payload = {'draft': '', 'records': s.records, 'source': None}
    with pytest.raises(ValueError, match='first verification failed'):
        checks.verify(payload, s.base)
    assert checks.verify(payload, s.base)['refs'] and len(calls) == 2
    assert checks.recheck() and len(calls) == 3


@pytest.mark.parametrize('field', ['refs', 'source_ref', 'payload'])
def test_end_recheck_compares_complete_production_return(preview, monkeypatch, field):
    from agent_service.office_chain_inputs import InputVerifications
    s = preview; checks = InputVerifications()
    payload = {'draft': '', 'records': s.records, 'source': None}
    checks.verify(payload, s.base)
    original = workspace_state.verify
    def changed(payload, base):
        result = original(payload, base)
        if field == 'refs': result['refs'][0]['token'] = '0' * 64
        elif field == 'source_ref': result['source_ref'] = result['refs'][0]
        else: result['payload']['draft'] = 'changed'
        return result
    monkeypatch.setattr(workspace_state, 'verify', changed)
    assert checks.recheck() is False


def test_standalone_edit_validate_and_capture_keep_original_double_verification(preview, monkeypatch):
    s = preview; folder = s.store / ('chain-' + s.chain_id)
    manifest = chain._manifest(folder, s.chain_id)
    preview_record = chain._read(folder / 'step-1-preview.json',
                                 {'schema', 'chain_sha256', 'index', 'binding'})
    record = chain._read(folder / 'step-1-edit.json', editing._EDIT_KEYS)
    calls = track(monkeypatch)
    editing.validate(manifest, 1, preview_record['binding'], record)
    assert len(calls) == 2
    editing.capture(manifest, 1, preview_record['binding'], preview_record['sha256'], s.session,
                    record['plan'], record['planning_result'], record['office_payload'])
    assert len(calls) == 4


@pytest.mark.parametrize('cause', ['cancel', 'changed_source'])
def test_first_load_recheck_refuses_changes_before_refresh_append(preview, monkeypatch, cause):
    s = preview; stopped = Event()
    fresh = office_edit_adapter.refresh_plan(s.session, s.state['edit_plan'])
    folder = s.store / ('chain-' + s.chain_id)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    original = editing.validate
    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        if cause == 'cancel': stopped.set()
        else: alter_same_mtime(s.task / 'resume-latest.json')
        return result
    monkeypatch.setattr(editing, 'validate', validate)
    if cause == 'cancel':
        with pytest.raises(ComputerError, match='^comparison_selection_cancelled$'):
            with verification_progress.verification_scope(stopped.is_set, lambda _: None, '核验'):
                chain.bind_edit_refresh(s.store, s.chain_id, 1, s.session, fresh)
    else:
        with pytest.raises(chain.OfficeTaskChainError):
            chain.bind_edit_refresh(s.store, s.chain_id, 1, s.session, fresh)
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before
    assert verification_progress._CURRENT.get() is None
