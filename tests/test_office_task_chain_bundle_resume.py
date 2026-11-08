"""Bound joint-report continuation with real offline receipts and no native apps."""
from pathlib import Path
from types import SimpleNamespace
import importlib
import uuid

import pytest

from agent_service import (office_bundle, office_bundle_resume as recovery, office_resume,
                           office_workflow, office_task_chain as chain, daily_task_routing)
from agent_service import office_task_chain_bundle_resume as continuation
from agent_service.computer import read_record
from test_artifact_source import fake
from test_office_bundle import setup as bundle_setup, Client


INSTRUCTION = '逐格核对这两份表格原文并生成Excel和Word核对报告'


@pytest.fixture
def local(bundle_setup, tmp_path):
    session, base, refs, _ = bundle_setup
    records = [{'path': r['artifact'], 'sha256': r['sha256']} for r in refs]
    store = tmp_path / 'chains'
    ident = chain.create(store, [INSTRUCTION, '核对上一步全部成果并生成Excel和Word报告'],
                         {'draft': '', 'records': records, 'source': None}, base, session.root.parent)
    class Routing:
        def complete(self, *a, **kw):
            return {'route': 'compare_tables', 'source_ids': ['S1', 'S2'], 'reason': '全部两份表格'}
    routed = daily_task_routing.propose(session, INSTRUCTION, refs, task_base=base, client=Routing())
    chain.bind_preview(store, ident, 0, session, routed, refs)
    return SimpleNamespace(session=session, base=base, refs=refs, store=store,
                           ident=ident, plan=routed['plan'])


def execute(s, *, pause=True, plan=None, client=None, native=fake):
    plan = plan or s.plan
    client = client or Client()
    def runner(steps, root, **kw):
        return office_workflow.run_office_workflow(steps, root, execute=native,
                                                  pause_after_step=pause, **kw)
    result = office_bundle.execute(s.session, plan, approval=plan['sha256'], task_base=s.base,
                                  client=client, workflow_runner=runner)
    return result, client


def paused(s):
    original, client = execute(s)
    state = chain.inspect(s.store, s.ident)
    assert original['state'] == 'cancelled' and original['code'] == 'bundle_office_incomplete'
    assert state['status'] == 'resume_available'
    return original, client, state


def resume(s, *, task=None, native=fake):
    task = Path(task or s.base / ('task-' + s.plan['request_id']))
    preview = recovery.preview_resume(task, s.base)
    attempt = uuid.uuid4().hex
    result = recovery.resume_task(task, s.base, preview['token'], attempt, execute=native)
    assert result['ok'] is True
    return attempt, result


def complete_path(s):
    return s.store / ('chain-' + s.ident) / 'step-0-complete.json'


def test_bundle_normal_success_and_missing_run_remain_unchanged(local):
    s = local
    assert chain.inspect(s.store, s.ident)['status'] == 'preview'
    result, _ = execute(s, pause=False)
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 1 and state['payload']['records'] == result['artifacts']
    assert 'bundle_resume' not in read_record(complete_path(s))['outcome']


def test_pause_exposes_dedicated_owner_token_without_execution(local):
    s = local; original, client, state = paused(s)
    preview = state['resume_preview']
    assert preview['kind'] == 'bundle_resume' and preview['completed'] == 1 and preview['remaining'] == 1
    task = Path(state['task_root'])
    assert preview['token'] != office_resume._snapshot(task, s.base)['token']
    assert office_resume.preview_resume(task, s.base)['reason'] == 'resume_bundle_owned'
    assert chain.advance(s.store, s.ident) == state and len(client.calls) == 1
    assert read_record(task / 'result.json')['ok'] is False and original['artifacts'] == []


def test_exact_success_keeps_failure_bytes_and_only_runs_remaining_step(local):
    s = local; original, client, state = paused(s)
    task, run = Path(state['task_root']), Path(original['run'])
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for root in (task, run) for p in root.rglob('*') if p.is_file()}
    calls = []
    def rest(spec, root, stop):
        calls.append(spec['kind']); return fake(spec, root, stop)
    attempt, result = resume(s, native=rest)
    restored = importlib.reload(chain).advance(s.store, s.ident, resume_attempt=attempt)
    assert restored['index'] == 1 and restored['scope'] == 'batch' and restored['payload']['source'] is None
    assert restored['payload']['records'] == [{'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
    assert calls == ['document'] and len(client.calls) == 1
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == v for p, v in before.items())
    outcome = read_record(complete_path(s))['outcome']
    assert outcome['bundle_resume']['attempt_id'] == attempt and outcome['differences'] == 0
    assert 'kind' not in read_record(task / ('resume-' + attempt) / 'outcome.json')
    assert office_bundle.inspect_saved(run, task_base=s.base, computer_base=s.session.root.parent) == original
    assert chain.advance(s.store, s.ident) == restored


def test_both_stages_can_pause_resume_and_finish_exact_full_batch(local):
    s = local; paused(s); attempt, _ = resume(s)
    first = chain.advance(s.store, s.ident, resume_attempt=attempt)
    refs = chain._refs(first['payload'], s.base)
    class Routing:
        def complete(self, *a, **kw):
            return {'route': 'compare_tables', 'source_ids': ['S1', 'S2'], 'reason': '全部两份成果'}
    routed = daily_task_routing.propose(s.session, first['payload']['draft'], refs,
                                       task_base=s.base, client=Routing())
    chain.bind_preview(s.store, s.ident, 1, s.session, routed, refs)
    original, _ = execute(s, plan=routed['plan'])
    state = chain.advance(s.store, s.ident)
    assert state['index'] == 1 and state['status'] == 'resume_available'
    assert state['task_root'] == str(s.base / ('task-' + routed['plan']['request_id']))
    attempt, result = resume(s, task=state['task_root'])
    final = chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert final['status'] == 'complete' and final['index'] == 2
    assert final['payload']['records'] == [{'path': a['path'], 'sha256': a['sha256']} for a in result['artifacts']]
    assert read_record(Path(original['run']) / 'outcome.json')['ok'] is False
    assert chain.inspect(s.store, s.ident) == final


def test_recovery_can_adopt_only_trusted_bound_task_latest(local):
    s = local; paused(s); attempt, _ = resume(s)
    assert chain.advance(s.store, s.ident)['index'] == 1
    assert read_record(complete_path(s))['outcome']['bundle_resume']['attempt_id'] == attempt


def test_same_sources_and_instruction_other_task_cannot_claim_success(local):
    s = local; paused(s)
    other = office_bundle.prepare(s.session, INSTRUCTION, s.refs, task_base=s.base)
    execute(s, plan=other)
    attempt, _ = resume(s, task=s.base / ('task-' + other['request_id']))
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert chain.inspect(s.store, s.ident)['status'] == 'resume_available'


def test_missing_exact_attempt_never_falls_back_to_successful_latest(local):
    s = local; paused(s); attempt, _ = resume(s)
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=uuid.uuid4().hex)
    assert not complete_path(s).exists()
    assert chain.advance(s.store, s.ident, resume_attempt=attempt)['index'] == 1


def test_fixed_completion_reader_never_selects_latest(local, monkeypatch):
    s = local; original, _, _ = paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    fixed = read_record(complete_path(s))['outcome']
    manifest = chain._manifest(complete_path(s).parent, s.ident)
    monkeypatch.setattr(recovery, 'latest_continuation', lambda *a, **kw: pytest.fail('read latest'))
    result = continuation.inspect(manifest, Path(original['run']), s.plan, original,
                                  (Path(original['run']) / 'outcome.json').read_bytes(),
                                  state={}, completed=fixed)
    assert result[2] == fixed


def test_broken_latest_fails_closed_without_rewriting_fixed_claim(local):
    s = local; _, _, state = paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    before = complete_path(s).read_bytes()
    (Path(state['task_root']) / 'resume-latest.json').write_text('{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.inspect(s.store, s.ident)
    assert complete_path(s).read_bytes() == before


def test_resealed_completion_cannot_switch_attempt(local):
    s = local; paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    value = read_record(complete_path(s)); value.pop('sha256')
    value['outcome']['bundle_resume']['attempt_id'] = uuid.uuid4().hex
    complete_path(s).write_bytes(chain.workspace._encode(chain._sealed(value)))
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.inspect(s.store, s.ident)


@pytest.mark.parametrize('location', ['origin', 'parent', 'instruction', 'source', 'snapshot', 'comparison',
                                    'decision', 'claim', 'plan', 'manifest', 'receipt', 'first_artifact',
                                    'resume_request', 'resume_result', 'resume_outcome'])
def test_changed_original_or_resume_proof_never_advances(local, location):
    s = local; original, _, state = paused(s); attempt, result = resume(s)
    task, run, workflow = Path(state['task_root']), Path(original['run']), Path(result['workflow_root'])
    paths = {'origin': run / 'outcome.json', 'parent': task / 'result.json', 'instruction': task / 'request.txt',
             'source': Path(s.refs[0]['artifact']), 'snapshot': next((task / 'inputs').glob('S1*')),
             'comparison': task / 'comparison.json', 'decision': run / 'decision.json', 'claim': run / 'claim.json',
             'plan': run.with_suffix('.json'), 'manifest': workflow / 'manifest.json',
             'receipt': workflow / 's1' / 'receipt.json', 'first_artifact': workflow / 's1' / 'spreadsheet.xlsx',
             'resume_request': task / ('resume-' + attempt) / 'request.json',
             'resume_result': task / ('resume-' + attempt) / 'result.json',
             'resume_outcome': task / ('resume-' + attempt) / 'outcome.json'}
    paths[location].write_bytes(b'{}')
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert not complete_path(s).exists()


def test_missing_authored_instruction_blocks_resume(local):
    s = local; _, _, state = paused(s)
    (Path(state['task_root']) / 'request.txt').unlink()
    with pytest.raises(chain.OfficeTaskChainError, match='task_instruction_changed'):
        chain.inspect(s.store, s.ident)


def test_orphan_attempt_and_unknown_effect_never_offer_resume(local):
    s = local; _, _, state = paused(s)
    task = Path(state['task_root'])
    (task / ('resume-' + uuid.uuid4().hex)).mkdir()
    with pytest.raises(chain.OfficeTaskChainError):
        chain.advance(s.store, s.ident)
    assert not complete_path(s).exists()


def test_unknown_native_effect_stays_blocked(local):
    s = local
    def unknown(*args):
        return {'ok': False, 'state': 'unknown', 'code': 'unknown_native_effect'}
    result, _ = execute(s, pause=False, native=unknown)
    assert result['ok'] is False
    try:
        state = chain.advance(s.store, s.ident)
    except chain.OfficeTaskChainError:
        state = None
    assert state is None or state['status'] == 'blocked'
    assert not complete_path(s).exists()


def test_unsupported_before_start_is_not_resumable(local):
    s = local; execute(s, client=Client(False))
    state = chain.advance(s.store, s.ident)
    assert state['status'] == 'blocked' and state['resume_preview'] is None


def test_stale_preview_creates_no_new_attempt(local):
    s = local; _, _, state = paused(s); resume(s)
    attempt = uuid.uuid4().hex
    with pytest.raises(ValueError, match='resume_preview_stale'):
        recovery.resume_task(state['task_root'], s.base, state['resume_preview']['token'], attempt,
                             execute=lambda *a: pytest.fail('effect'))
    assert not (Path(state['task_root']) / ('resume-' + attempt)).exists()


def test_old_success_event_cannot_advance_unplanned_next_stage(local):
    s = local; paused(s); attempt, _ = resume(s)
    chain.advance(s.store, s.ident, resume_attempt=attempt)
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    assert chain.inspect(s.store, s.ident)['index'] == 1


def test_repeated_preflight_block_retains_prefix_then_finishes(local, monkeypatch):
    from agent_service import office_readiness
    s = local; _, _, state = paused(s)
    monkeypatch.setattr(office_readiness, 'preflight_office_window', lambda: 'office_ui_blocked')
    preview = state['resume_preview']; attempt = uuid.uuid4().hex
    result = recovery.resume_task(state['task_root'], s.base, preview['token'], attempt)
    assert result['state'] == 'blocked'
    state = chain.advance(s.store, s.ident)
    assert state['status'] == 'resume_available' and state['resume_preview']['completed'] == 1
    with pytest.raises(chain.OfficeTaskChainError, match='resume_unverified'):
        chain.advance(s.store, s.ident, resume_attempt=attempt)
    attempt, _ = resume(s)
    assert chain.advance(s.store, s.ident, resume_attempt=attempt)['index'] == 1
