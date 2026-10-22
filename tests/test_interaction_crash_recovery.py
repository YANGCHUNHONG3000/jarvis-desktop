"""Offline abrupt exits use production persistence, receipts and OS leases."""
from pathlib import Path

import pytest

from agent_service import interaction_recovery as recovery
from agent_service.computer import ComputerError, read_record, write_new
from agent_service.computer_interaction import prepare as prepare_interaction
from test_computer_interaction import ctx, prepare, run
from test_browser_partial_recovery import CONFIG, PartialBrowser


class AbruptExit(BaseException):
    """Bypass the engine's normal Exception-to-outcome boundary."""


def crash(ctx, plan, *, after=0, active_rows=None, **options):
    root = ctx['session'].root/'goals'/(plan['sha256']+'.run')
    def stop():
        if len(list(root.glob('step-*/verification.json'))) >= after:
            if active_rows is not None:
                active_rows.append(recovery.inspect(ctx['session'], root))
            raise AbruptExit()
        return False
    with pytest.raises(AbruptExit):
        run(ctx, plan, stop=stop, **options)
    assert not (root/'outcome.json').exists()
    return root


def contents(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_crash_after_acknowledged_office_recovers_without_replay_or_rewriting_parent(ctx):
    root = crash(ctx, prepare(ctx, browser=None), after=1)
    original = contents(root)
    models = contents(ctx['session'].root/'model_calls')
    row = recovery.inspect(ctx['session'], root)
    assert row['eligible'] and row['completed_edits'] == ['e1'] and row['remaining_edits'] == ['e2']
    assert row['interrupted_without_outcome'] is True
    assert recovery.list_candidates(ctx['session'])[0]['run'] == str(root)
    preview = recovery.prepare(ctx['session'], root)
    assert '无活动执行且未写入结束记录' in preview['reason']
    plan = preview['plan']
    assert contents(root) == original
    assert contents(ctx['session'].root/'model_calls') == models
    result = run(ctx, plan)
    assert result['ok'] and result['actions'] == 1 and len(result['artifacts']) == 2
    assert [s['kind'] for s in ctx['calls']] == ['document','spreadsheet']
    assert {k:v for k,v in contents(root).items() if k != 'continuation-claim.json'} == original
    assert not (root/'outcome.json').exists()


@pytest.mark.parametrize('phase',[2,3])
def test_crash_browser_verified_cursor_uses_fresh_session_and_remaining_actions(ctx,phase):
    plan = prepare_interaction(ctx['session'], '恢复已批准GitHub阶段', browser=CONFIG,
                               edits=[], repository_document=True)
    root = crash(ctx, plan, after=phase, browser_factory=PartialBrowser)
    first = recovery._inspect(ctx['session'],root)
    assert first['browser_resume']['phase'] == ('search_results' if phase == 2 else 'repository_opened')
    original = contents(root)
    child = recovery.prepare(ctx['session'],root)['plan']
    result = run(ctx,child,browser_factory=PartialBrowser)
    assert result['ok']
    assert [a['result']['code'] for a in result['attempts']] == (
        ['browser_github_repository_opened','browser_wait_observed'] if phase == 2 else
        ['browser_wait_observed'])
    assert {k:v for k,v in contents(root).items() if k != 'continuation-claim.json'} == original


def test_active_no_outcome_parent_is_blocked_then_becomes_eligible(ctx):
    rows = []
    root = crash(ctx, prepare(ctx,browser=None), active_rows=rows)
    assert rows and rows[0]['code'] == 'run_owner_active' and not rows[0]['eligible']
    assert recovery.inspect(ctx['session'],root)['eligible']


def test_terminal_run_still_owned_during_cleanup_is_blocked(ctx,monkeypatch):
    from agent_service.computer_interaction import InteractionEnvironment
    rows = []
    original = InteractionEnvironment.close
    def close(environment):
        assert (environment.run/'outcome.json').exists()
        rows.append(recovery.inspect(ctx['session'],environment.run))
        original(environment)
    monkeypatch.setattr(InteractionEnvironment,'close',close)
    result = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    assert rows[0]['code'] == 'run_owner_active'
    assert recovery.inspect(ctx['session'],result['run'])['eligible']


def test_terminal_new_claim_cannot_fall_back_to_legacy_when_owner_files_removed(ctx):
    result = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    root = Path(result['run'])
    assert 'lifecycle' in read_record(root/'claim.json')
    (root/'owner.json').unlink()
    (root/'owner.lease').unlink()
    assert recovery.inspect(ctx['session'],root)['code'] == 'run_owner_missing'


def test_legacy_terminal_without_new_ownership_fields_still_recovers(ctx):
    import json
    result = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    root = Path(result['run'])
    claim = read_record(root/'claim.json')
    claim.pop('lifecycle')
    (root/'claim.json').write_text(json.dumps(claim),encoding='utf-8')
    (root/'owner.json').unlink()
    (root/'owner.lease').unlink()
    before = contents(root)
    assert recovery.inspect(ctx['session'],root)['eligible']
    recovery.prepare(ctx['session'],root)
    assert contents(root) == before


@pytest.mark.parametrize('name',['owner.json','owner.lease'])
def test_no_outcome_without_durable_owner_proof_is_refused_and_still_listed(ctx,name):
    root = crash(ctx,prepare(ctx,browser=None))
    (root/name).unlink()  # Only temporary evidence owned by this test.
    before = contents(root)
    row = recovery.inspect(ctx['session'],root)
    assert not row['eligible'] and row['artifacts'] == []
    assert row['code'] == ('run_owner_missing' if name == 'owner.json' else 'run_owner_invalid')
    assert recovery.list_candidates(ctx['session'])[0]['eligible'] is False
    assert contents(root) == before


@pytest.mark.parametrize('name',['intent.json','environment-intent.json',
                                 'environment-result.json','verification.json'])
def test_crash_dispatched_action_missing_acknowledgement_is_never_recovered(ctx,name):
    root = crash(ctx,prepare(ctx,browser=None),after=1)
    (root/'step-01'/name).unlink()
    row = recovery.inspect(ctx['session'],root)
    assert not row['eligible'] and row['artifacts'] == []
    with pytest.raises((ValueError,OSError)):
        recovery.prepare(ctx['session'],root)
    assert len(ctx['calls']) == 1


@pytest.mark.parametrize('name',['owner.json','step-01/environment-result.json',
                                 'step-01/verification.json','observation-01.json'])
def test_crash_truncated_persistence_cannot_be_a_recovery_checkpoint(ctx,name):
    root = crash(ctx,prepare(ctx,browser=None),after=1)
    (root/name).write_text('{',encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']


@pytest.mark.parametrize('change',['outcome','owner','lease','receipt','ancestor'])
def test_crash_snapshot_change_after_preview_blocks_before_remaining_effect(ctx,change):
    root = crash(ctx,prepare(ctx,browser=None),after=1)
    child = recovery.prepare(ctx['session'],root)['plan']
    if change == 'outcome':
        write_new(root/'outcome.json', {'ok':False,'state':'partial','engine':'observed_agent','run':str(root)})
    elif change == 'owner':
        (root/'owner.json').write_bytes((root/'owner.json').read_bytes()+b' ')
    elif change == 'lease':
        (root/'owner.lease').write_bytes(b'0'*32)
    elif change == 'receipt':
        artifact = recovery._inspect(ctx['session'],root)['artifacts'][0]['path']
        path = Path(artifact).parent/'native-edit-receipt.json'
        path.write_bytes(path.read_bytes()+b' ')
    else:
        path = root/'step-01'/'environment-result.json'
        path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises((ValueError,OSError)):
        run(ctx,child)
    assert len(ctx['calls']) == 1


def test_crash_competing_previews_get_one_child_and_four_hop_limit(ctx):
    root = crash(ctx,prepare(ctx,browser=None))
    first = recovery.prepare(ctx['session'],root)['plan']
    competing = recovery.prepare(ctx['session'],root)['plan']
    next_root = crash(ctx,first)
    with pytest.raises(ComputerError,match='recovery_already_claimed'):
        run(ctx,competing)
    for depth in range(2,recovery.MAX_DEPTH+1):
        child = recovery.prepare(ctx['session'],next_root)['plan']
        assert child['recovery']['depth'] == depth
        next_root = crash(ctx,child)
    assert recovery.inspect(ctx['session'],next_root)['code'] == 'recovery_lineage_depth'
    assert not ctx['calls']


def test_crash_ancestral_receipts_remain_bound_across_new_crashed_children(ctx):
    root = crash(ctx,prepare(ctx,browser=None),after=1)
    child = recovery.prepare(ctx['session'],root)['plan']
    child_root = crash(ctx,child)
    preview = recovery.prepare(ctx['session'],child_root)['plan']
    path = root/'step-01'/'environment-result.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ComputerError,match='recovery_lineage_changed'):
        run(ctx,preview)
    assert len(ctx['calls']) == 1


def test_crash_same_session_model_budget_is_not_reset(ctx):
    root = crash(ctx,prepare(ctx,browser=None))
    for n in range(6):
        ctx['session'].claim_model({'prior':n})
    child = recovery.prepare(ctx['session'],root)['plan']
    result = run(ctx,child)
    assert result['code'] == 'session_model_call_limit' and result['actions'] == 0
    assert not ctx['calls']


@pytest.mark.parametrize('phase',['choices','decision','intent'])
def test_crash_valid_undispatched_selection_is_safe_but_corruption_is_refused(ctx,phase):
    plan = prepare(ctx,browser=None)
    root = ctx['session'].root/'goals'/(plan['sha256']+'.run')
    # Intercept only persistence; production selection and pre-dispatch checks run.
    from agent_service import computer_agent as engine
    original = engine.write_new
    def save(path, value):
        original(path,value)
        if Path(path).name == phase+'.json':
            raise AbruptExit()
    from unittest.mock import patch
    with patch.object(engine,'write_new',save), pytest.raises(AbruptExit):
        run(ctx,plan)
    assert not (root/'outcome.json').exists() and not ctx['calls']
    assert recovery.inspect(ctx['session'],root)['eligible']
    selected = root/'step-01'/(phase+'.json')
    selected.write_text('{}',encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']


@pytest.mark.parametrize('kinds',[('document',),('document','spreadsheet')])
def test_crash_all_office_work_done_gets_new_verified_child_without_replay(ctx,kinds):
    root = crash(ctx,prepare(ctx,browser=None,kinds=kinds),after=len(kinds))
    original = contents(root)
    row = recovery.inspect(ctx['session'],root)
    assert row['eligible'] and row['remaining_edits'] == []
    preview = recovery.prepare(ctx['session'],root)
    assert '新任务只重新核验并记录结果' in preview['reason']
    result = run(ctx,preview['plan'])
    assert result['ok'] and result['actions'] == 0 and result['attempts'] == []
    assert len(result['artifacts']) == len(kinds) and len(ctx['calls']) == len(kinds)
    assert {k:v for k,v in contents(root).items() if k != 'continuation-claim.json'} == original
    assert not (root/'outcome.json').exists()


def test_crash_complete_browser_and_office_seals_without_actions_then_delivers_repository_word(ctx):
    from agent_service import repository_document as repo
    from test_artifact_source import fake as native_fake
    from test_computer_interaction import edits
    plan = prepare_interaction(ctx['session'],'恢复已完成但未封口的GitHub与Office任务',
        browser=CONFIG,edits=edits(ctx),repository_document=True)
    root = crash(ctx,plan,after=6,browser_factory=PartialBrowser)
    original = contents(root)
    preview = recovery.prepare(ctx['session'],root)
    assert preview['plan']['recovery']['kind'] == 'interactive_browser'
    def no_replay(*args,**kwargs):
        pytest.fail('A completed browser or Office action was replayed')
    result = run(ctx,preview['plan'],browser_factory=no_replay,office_execute=no_replay)
    assert result['ok'] and result['actions'] == 0 and len(result['artifacts']) == 2
    assert len(ctx['calls']) == 2
    base = ctx['session'].root.parent
    document = repo.prepare_repository_document(result['run'],computer_base=base)
    delivered = repo.execute_repository_document(document,approval=document['sha256'],
        computer_base=base,task_base=ctx['task_base'],office_execute=native_fake)
    assert delivered['ok']
    assert {k:v for k,v in contents(root).items() if k != 'continuation-claim.json'} == original
    assert not (root/'outcome.json').exists()


def test_normally_stopped_terminal_with_all_actions_done_still_has_no_work(ctx):
    def edit(spec,output,source_root,**options):
        value = ctx['execute'](spec,output,source_root,**options)
        ctx['stopped'][0] = True
        return value
    result = run(ctx,prepare(ctx,browser=None,kinds=('document',)),office_execute=edit)
    assert not result['ok'] and len(result['artifacts']) == 1
    assert recovery.inspect(ctx['session'],result['run'])['code'] == 'recovery_nothing_remaining'


@pytest.mark.parametrize('state',['unknown','failed'])
def test_crash_last_office_step_not_verified_cannot_be_sealed(ctx,state):
    from agent_service import computer_agent as engine
    original = engine.write_new
    def save(path,value):
        original(path,value)
        if Path(path).name == 'verification.json' and Path(path).parent.name == 'step-02':
            raise AbruptExit()
    def edit(spec,output,source_root,**options):
        if len(ctx['calls']) == 1:
            return {'ok':False,'state':state,'code':'offline_last_native_unverified'}
        return ctx['execute'](spec,output,source_root,**options)
    from unittest.mock import patch
    plan = prepare(ctx,browser=None)
    root = ctx['session'].root/'goals'/(plan['sha256']+'.run')
    with patch.object(engine,'write_new',save), pytest.raises(AbruptExit):
        run(ctx,plan,office_execute=edit)
    assert not (root/'outcome.json').exists()
    assert not recovery.inspect(ctx['session'],root)['eligible']
    with pytest.raises(ComputerError,match='recovery_dispatched_action_not_verified'):
        recovery.prepare(ctx['session'],root)
