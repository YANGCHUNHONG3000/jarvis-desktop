"""Exact mail-chain continuation ownership using real offline Office receipts."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from agent_service import (office_task_chain as chain, office_task_chain_resume as continuation,
                           office_resume, office_workflow, daily_task_routing, capability_router,
                           office_bundle)
from agent_service.computer import ComputerSession, read_record, write_new
from test_artifact_source import fake
from test_mail_understanding import _mail_source, _ready
from test_mail_capability_router import FakeClient


INSTRUCTION = '将这封邮件的安排、负责人和期限整理成Excel和Word，保留原文依据。'


@pytest.fixture
def local(tmp_path, monkeypatch):
    _, source, base, _, ref = _mail_source(tmp_path)
    session = ComputerSession.create(tmp_path / 'sessions')
    monkeypatch.setattr(office_bundle, 'BASE', session.root.parent)
    actual_workflow = office_workflow.run_office_workflow
    def offline(steps, root, **kwargs):
        kwargs.setdefault('execute', fake)
        return actual_workflow(steps, root, **kwargs)
    monkeypatch.setattr(office_workflow, 'run_office_workflow', offline)
    records = [{'path': ref['artifact'], 'sha256': ref['sha256']}]
    store = tmp_path / 'chains'
    ident = chain.create(store, [INSTRUCTION, '逐格核对上一步全部表格并生成Excel和Word'],
                         {'draft': '', 'records': records, 'source': records[0]}, base, session.root.parent)
    class RoutingClient:
        def complete(self, *args, **kwargs):
            return {'route': 'mail_understanding', 'source_ids': ['S1'], 'reason': '仅所选原始邮件'}
    routed = daily_task_routing.propose(session, INSTRUCTION, [ref], task_base=base, client=RoutingClient())
    bound = chain.bind_preview(store, ident, 0, session, routed, [ref])
    assert bound['status'] == 'preview'
    yield SimpleNamespace(base=base, session=session, source=source, ref=ref, store=store,
                          ident=ident, routed=routed, plan=routed['plan'])


def execute(s, *, pause=True, client=None, plan=None):
    selected = plan or s.plan
    client = client or FakeClient(_ready(schema=4), {'approved': True, 'reason': 'offline independent review'})
    result = capability_router.execute(s.session, selected, approval=selected['sha256'],
                                       task_base=s.base, client=client, pause_after_step=pause)
    return result, client


def paused(s):
    result, client = execute(s)
    assert result['ok'] is False and result['code'] == 'office_paused_after_verified_step'
    state = chain.inspect(s.store, s.ident)
    assert state['status'] == 'resume_available'
    return result, client, state


def resume(s, *, task=None, attempt=None):
    task = Path(task or s.base / ('task-' + s.plan['request_id']))
    preview = office_resume.preview_resume(task, s.base)
    attempt = attempt or uuid.uuid4().hex
    result = office_resume.resume_task(task, s.base, preview['token'], attempt, execute=fake)
    assert result['ok'] is True
    return attempt, result


def test_mail_route_is_bound_to_original_export_and_exact_task(local):
    s = local; state = chain.inspect(s.store, s.ident)
    assert s.plan['schema'] == 4 and s.plan['capability'] == 'mail_understanding'
    assert state['task_root'] == str(s.base / ('task-' + s.plan['request_id']))
    assert state['resume_preview'] is None and state['binding']['refs'] == [s.ref]


def test_mail_normal_success_still_uses_saved_inspector(local):
    s = local; result, _ = execute(s, pause=False)
    assert result['ok'] is True
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 1 and state['payload']['records'] == result['artifacts']
    assert 'mail_resume' not in read_record(s.store / ('chain-' + s.ident) / 'step-0-complete.json')['outcome']


def test_paused_chain_exposes_only_safe_remaining_steps(local):
    s = local; result, client, state = paused(s)
    assert state['resume_preview']['completed'] == 1 and state['resume_preview']['remaining'] == 1
    assert state['resume_original_state'] == 'cancelled'
    assert chain.advance(s.store, s.ident) == state
    assert len(client.calls) == 2
    assert not (Path(result['task_root']) / 'workflow-never').exists()


def test_exact_resume_success_preserves_original_failure_and_all_completed_bytes(local):
    s = local; original, client, _ = paused(s)
    task = Path(original['task_root']); run = Path(original['run'])
    protected = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in task.rglob('*') if p.is_file()}
    old_capability = (run / 'outcome.json').read_bytes()
    attempt, result = resume(s)
    state = chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert state['index'] == 1 and state['status'] == 'ready'
    assert state['payload']['source'] is None
    assert state['payload']['records'] == [{'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == v for p, v in protected.items())
    assert (run / 'outcome.json').read_bytes() == old_capability and read_record(task / 'result.json')['ok'] is False
    complete = read_record(s.store / ('chain-' + s.ident) / 'step-0-complete.json')
    assert complete['outcome']['mail_resume']['attempt_id'] == attempt
    assert len(client.calls) == 2
    assert chain.advance(s.store, s.ident) == state


def test_resume_success_then_existing_bundle_finishes_second_stage(local):
    s = local; paused(s); attempt, _ = resume(s)
    first = chain.advance(s.store, s.ident, resume_attempt=attempt)
    refs = chain._refs(first['payload'], s.base)
    class CompareRouting:
        def complete(self, *args, **kwargs):
            return {'route': 'compare_tables', 'source_ids': ['S1', 'S2'], 'reason': '全部两成果逐格核对'}
    routed = daily_task_routing.propose(s.session, first['payload']['draft'], refs, task_base=s.base, client=CompareRouting())
    chain.bind_preview(s.store, s.ident, 1, s.session, routed, refs)
    class CompareClient:
        def complete(self, *args, **kwargs):
            return {'supported': True, 'reason': '仅按位置核对全部表格'}
    result = office_bundle.execute(s.session, routed['plan'], approval=routed['plan']['sha256'],
                                  task_base=s.base, client=CompareClient())
    assert result['ok'] is True and result['differences'] == 0
    final = chain.advance(s.store, s.ident)
    assert final['index'] == 2 and final['status'] == 'complete'
    assert final['payload']['records'] == result['artifacts']


def test_recover_without_event_may_use_only_trusted_exact_task_latest(local):
    s = local; paused(s); attempt, _ = resume(s)
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 1
    complete = read_record(s.store / ('chain-' + s.ident) / 'step-0-complete.json')
    assert complete['outcome']['mail_resume']['attempt_id'] == attempt


def test_foreign_task_same_source_and_instruction_cannot_claim_resume(local):
    s = local; paused(s)
    foreign = capability_router.prepare_mail(s.session, INSTRUCTION, source_ref=s.ref, task_base=s.base)
    other, _ = execute(s, plan=foreign)
    attempt, _ = resume(s, task=other['task_root'])
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert chain.inspect(s.store, s.ident)['status'] == 'resume_available'


@pytest.mark.parametrize('attempt', ['../other', '', True, 1, 'a' * 31, 'a' * 33])
def test_resume_attempt_is_bounded_and_never_used_for_path_injection(local, attempt):
    s = local; paused(s)
    with pytest.raises(chain.OfficeTaskChainError, match='attempt_invalid'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)


def test_missing_exact_attempt_does_not_fall_back_to_valid_latest(local):
    s = local; paused(s); actual, _ = resume(s)
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=uuid.uuid4().hex)
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()
    assert chain.advance(s.store, s.ident, resume_attempt=actual)['index'] == 1


def test_fixed_completion_proof_reader_does_not_select_mutable_latest(local, monkeypatch):
    s = local; original, _, _ = paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    folder = s.store / ('chain-' + s.ident)
    complete = read_record(folder / 'step-0-complete.json')['outcome']
    manifest = chain._manifest(folder, s.ident)
    monkeypatch.setattr(office_resume, 'latest_continuation', lambda *a, **k: pytest.fail('pinned proof read latest'))
    raw = (Path(original['run']) / 'outcome.json').read_bytes()
    status, _, result = continuation.inspect(manifest, Path(original['run']), s.plan, original, raw,
                                              state={}, completed=complete)
    assert status == 'waiting_result' and result == complete


def test_mutated_latest_may_fail_closed_but_never_retarget_completed_claim(local):
    s = local; original, _, _ = paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    complete_path = s.store / ('chain-' + s.ident) / 'step-0-complete.json'
    before = complete_path.read_bytes()
    (Path(original['task_root']) / 'resume-latest.json').write_text('{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)  # Existing downstream source contract conservatively rejects.
    assert complete_path.read_bytes() == before


def test_valid_new_latest_cannot_replace_the_attempt_already_claimed(local):
    s = local; original, _, _ = paused(s); first_attempt, result = resume(s)
    first = chain.advance(s.store, s.ident, resume_attempt=first_attempt)
    complete_path = s.store / ('chain-' + s.ident) / 'step-0-complete.json'
    preserved = complete_path.read_bytes()
    task = Path(original['task_root'])
    # Controlled test-only removal: existing recovery can close a fully verified
    # workflow with no effects when its latest pointer is absent.
    (task / 'resume-latest.json').unlink()
    second_attempt = uuid.uuid4().hex
    second = office_resume.resume_task(task, s.base, result['token_after'], second_attempt,
                                      execute=lambda *a: pytest.fail('completed step replayed'))
    assert second['ok'] is True and second_attempt != first_attempt
    assert office_resume.latest_continuation(task, s.base)['attempt_id'] == second_attempt
    assert chain.inspect(s.store, s.ident) == first
    assert complete_path.read_bytes() == preserved
    assert read_record(complete_path)['outcome']['mail_resume']['attempt_id'] == first_attempt


def test_resealed_completion_cannot_claim_a_different_attempt(local):
    s = local; paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    path = s.store / ('chain-' + s.ident) / 'step-0-complete.json'
    value = read_record(path)
    value['outcome']['mail_resume']['attempt_id'] = uuid.uuid4().hex
    value.pop('sha256')
    path.write_bytes(chain.workspace._encode(chain._sealed(value)))
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.inspect(s.store, s.ident)


def test_old_resume_event_cannot_advance_the_next_unplanned_stage(local):
    s = local; paused(s); attempt, _ = resume(s)
    assert chain.advance(s.store, s.ident, resume_attempt=attempt)['index'] == 1
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert chain.inspect(s.store, s.ident)['index'] == 1


@pytest.mark.parametrize('where', ['capability_outcome', 'parent_result', 'request', 'review', 'manifest',
                                  'first_artifact', 'resume_outcome'])
def test_tampered_mail_or_resume_proof_cannot_advance(local, where):
    s = local; original, _, _ = paused(s); attempt, result = resume(s)
    task = Path(original['task_root']); workflow = Path(result['workflow_root'])
    path = {'capability_outcome': Path(original['run']) / 'outcome.json', 'parent_result': task / 'result.json',
            'request': task / 'mail-analysis-request.json', 'review': task / 'mail-analysis-review.json',
            'manifest': workflow / 'manifest.json', 'first_artifact': workflow / 's1' / 'spreadsheet.xlsx',
            'resume_outcome': task / ('resume-' + attempt) / 'outcome.json'}[where]
    path.write_bytes(b'{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()


def test_unknown_office_effect_is_not_offered_as_resumable(local):
    s = local
    actual = office_workflow.run_office_workflow
    # A malformed native dispatch result stays unknown; no fabricated receipt.
    def bad(spec, root, stop):
        return {'ok': False, 'state': 'unknown', 'code': 'unknown_native_effect'}
    from unittest.mock import patch
    with patch.object(office_workflow, 'run_office_workflow',
                      lambda steps, root, **kw: actual(steps, root, execute=bad, **kw)):
        original, _ = execute(s, pause=False)
    try:
        state = chain.advance(s.store, s.ident)
    except chain.OfficeTaskChainError:
        state = None  # Native proof is rejected closed, never retried.
    assert state is None or state['status'] == 'blocked'
    assert not (s.store / ('chain-' + s.ident) / 'step-0-complete.json').exists()


def test_failed_semantic_analysis_has_no_resume_authority(local):
    s = local; result, _ = execute(s, client=FakeClient({'invalid': 'candidate'}))
    assert result['ok'] is False
    state = chain.advance(s.store, s.ident)
    assert state['status'] == 'blocked' and state['resume_preview'] is None


def test_stale_resume_preview_token_cannot_replay_completed_step(local):
    s = local; _, _, state = paused(s)
    token = state['resume_preview']['token']
    _, result = resume(s)
    stale = office_resume.resume_task(state['task_root'], s.base, token, uuid.uuid4().hex, execute=fake)
    assert stale['ok'] is False
    assert result['artifacts'][0]['sha256'] == read_record(Path(result['workflow_root']) / 's1' / 'receipt.json')['sha256']
