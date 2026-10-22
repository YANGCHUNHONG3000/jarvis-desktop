"""Offline recovery uses production evidence/OOXML with only native I/O faked."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import interaction_recovery as recovery
from agent_service import computer_agent as engine
from agent_service.computer import ComputerError, ComputerSession, read_record
from test_computer_interaction import ctx, prepare, run, CurrentChoiceClient


def partial(ctx):
    plan = prepare(ctx, browser=None)
    def edit(spec, output, source_root, **options):
        value = ctx['execute'](spec, output, source_root, **options)
        ctx['stopped'][0] = True
        return value
    result = run(ctx, plan, office_execute=edit)
    ctx['stopped'][0] = False
    assert result['code'] == 'goal_cancelled' and len(result['artifacts']) == 1
    return plan, result


def test_fresh_approved_same_session_continuation_never_replays_completed_edit(ctx):
    original, prior = partial(ctx)
    parent = Path(prior['run'])
    preserved = {str(p):p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    models_before = len(list((ctx['session'].root/'model_calls').glob('[1-6].json')))
    row = recovery.inspect(ctx['session'], parent)
    assert row['eligible'] and row['completed_edits'] == ['e1'] and row['remaining_edits'] == ['e2']
    assert recovery.list_candidates(ctx['session'])[0]['run'] == str(parent)
    resumed = recovery.prepare(ctx['session'], parent)['plan']
    assert resumed['goal'] == original['goal']
    preview = engine.describe(resumed)
    assert '仅带入成果：e1' in preview and '本次允许执行：e2' in preview
    assert models_before == len(list((ctx['session'].root/'model_calls').glob('[1-6].json')))
    result = run(ctx, resumed)
    assert result['ok'] and len(result['artifacts']) == 2 and result['actions'] == 1
    assert [item['kind'] for item in ctx['calls']] == ['document','spreadsheet']
    assert all(Path(path).read_bytes() == value for path,value in preserved.items())
    assert read_record(parent/'outcome.json') == prior
    assert not recovery.inspect(ctx['session'],parent)['eligible']
    with pytest.raises(ComputerError):
        run(ctx, resumed)
    with pytest.raises(ComputerError,match='goal_already_claimed'):
        run(ctx, original)


def test_competing_previews_share_one_durable_child_claim(ctx):
    _, prior = partial(ctx)
    first = recovery.prepare(ctx['session'],prior['run'])['plan']
    second = recovery.prepare(ctx['session'],prior['run'])['plan']
    assert first['sha256'] != second['sha256']
    assert run(ctx,first)['ok']
    with pytest.raises(ComputerError,match='recovery_already_claimed'):
        run(ctx,second)
    assert len(ctx['calls']) == 2


@pytest.mark.parametrize('target',['source','snapshot','artifact','receipt','manifest','outcome','intent','result'])
def test_tampering_after_preview_blocks_all_remaining_effects(ctx,target):
    original, prior = partial(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    artifact = Path(prior['artifacts'][0]['path'])
    parent = Path(prior['run'])
    paths = {'source':ctx['word'],
             'snapshot':Path(original['goal']['edits'][0]['source_root'])/'source.docx',
             'artifact':artifact, 'receipt':artifact.parent/'native-edit-receipt.json',
             'manifest':artifact.parent/'edit-manifest.json', 'outcome':parent/'outcome.json',
             'intent':parent/'step-01'/'environment-intent.json',
             'result':parent/'step-01'/'environment-result.json'}
    paths[target].write_bytes(paths[target].read_bytes()+b' ')
    with pytest.raises(ValueError):
        run(ctx,plan)
    assert len(ctx['calls']) == 1


def test_source_change_before_inspection_exposes_no_completed_artifacts(ctx):
    _, prior = partial(ctx)
    ctx['word'].write_bytes(ctx['word'].read_bytes()+b'changed')
    row = recovery.inspect(ctx['session'],prior['run'])
    assert not row['eligible'] and row['artifacts'] == []
    with pytest.raises(ValueError):
        recovery.prepare(ctx['session'],prior['run'])


@pytest.mark.parametrize('state',['unknown','failed','cancelled'])
def test_failed_or_uncertain_native_operations_cannot_recover(ctx,state):
    def bad(*args,**options):
        return {'ok':False,'state':state,'code':'offline_failed_native'}
    prior = run(ctx,prepare(ctx,browser=None),office_execute=bad)
    assert not recovery.inspect(ctx['session'],prior['run'])['eligible']
    with pytest.raises(ValueError):
        recovery.prepare(ctx['session'],prior['run'])
    assert not ctx['calls']


def test_browser_without_completed_read_checkpoint_is_refused(ctx):
    plan = prepare(ctx)
    prior = run(ctx,plan,stop=lambda:True)
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_browser_read_not_completed'
    assert recovery.list_candidates(ctx['session'])[0]['eligible'] is False


def test_no_effect_cancel_can_resume_untouched_original_edits(ctx):
    prior = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    row = recovery.inspect(ctx['session'],prior['run'])
    assert row['eligible'] and row['completed_edits'] == [] and row['remaining_edits'] == ['e1','e2']
    assert run(ctx,recovery.prepare(ctx['session'],prior['run'])['plan'])['ok']


def test_continuation_cannot_reset_existing_session_model_call_cap(ctx):
    prior = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    for n in range(6):
        ctx['session'].claim_model({'prior':n})
    reopened = ComputerSession(ctx['session'].root)
    plan = recovery.prepare(reopened,prior['run'])['plan']
    client = CurrentChoiceClient()
    result = run(ctx,plan,client=client)
    assert result['code'] == 'session_model_call_limit' and result['actions'] == 0
    assert not client.calls and not ctx['calls']
    assert len(list((reopened.root/'model_calls').glob('[1-6].json'))) == 6


def test_bounded_nested_lineage_and_stale_parent_refusal(ctx):
    prior = run(ctx,prepare(ctx,browser=None),stop=lambda:True)
    for depth in range(1, recovery.MAX_DEPTH+1):
        plan = recovery.prepare(ctx['session'],prior['run'])['plan']
        assert plan['recovery']['depth'] == depth
        previous = prior
        prior = run(ctx,plan,stop=lambda:True)
        assert not recovery.inspect(ctx['session'],previous['run'])['eligible']
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_lineage_depth'
    with pytest.raises(ComputerError,match='recovery_lineage_depth'):
        recovery.prepare(ctx['session'],prior['run'])
    assert not ctx['calls']


def test_lineage_claim_tamper_during_execution_blocks_before_dispatch_and_final_read(ctx):
    _, prior = partial(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def progress(value):
        if value.get('op') == 'agent_cycle':
            path = Path(prior['run'])/'continuation-claim.json'
            path.write_text(json.dumps({'child':'0'*64}),encoding='utf-8')
    result = run(ctx,plan,progress=progress)
    assert not result['ok'] and result['actions'] == 0 and result['artifacts'] == []
    assert len(ctx['calls']) == 1


def test_ancestor_evidence_tamper_during_continuation_suppresses_all_handoff(ctx):
    _, prior = partial(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def edit(spec, output, source_root, **options):
        value = ctx['execute'](spec,output,source_root,**options)
        path = Path(prior['run'])/'outcome.json'
        path.write_bytes(path.read_bytes()+b' ')
        return value
    result = run(ctx,plan,office_execute=edit)
    assert not result['ok'] and result['actions'] == 1 and result['artifacts'] == []
    assert len(ctx['calls']) == 2


def test_cross_session_parent_and_forged_goal_are_rejected(ctx):
    _, prior = partial(ctx)
    other = ComputerSession.create(ctx['root']/'other')
    assert recovery.inspect(other,prior['run'])['code'] == 'recovery_run_owner_invalid'
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    changed = deepcopy(plan)
    changed['goal']['edits'].reverse()
    from agent_service.computer_goals import _save_goal
    changed.pop('sha256')
    changed = _save_goal(ctx['session'],changed)
    with pytest.raises(ComputerError,match='recovery_lineage_changed'):
        run(ctx,changed)
    assert len(ctx['calls']) == 1


def test_dangling_dispatched_intent_refuses_every_recovery(ctx):
    _, prior = partial(ctx)
    # A terminal envelope cannot turn missing dispatched-action evidence into
    # proof of no effect, even when its own attempts still describe success.
    path = Path(prior['run'])/'step-01'/'environment-result.json'
    path.unlink()  # Only this test's temporary evidence is removed.
    row = recovery.inspect(ctx['session'],prior['run'])
    assert not row['eligible'] and not row['artifacts']
    with pytest.raises(ValueError):
        recovery.prepare(ctx['session'],prior['run'])
    assert len(ctx['calls']) == 1


def test_expired_continuation_preview_does_not_consume_parent_claim(ctx,monkeypatch):
    _, prior = partial(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    monkeypatch.setattr(engine.time,'time',lambda:plan['prepared_at']+121)
    with pytest.raises(ComputerError,match='goal_preview_expired'):
        run(ctx,plan)
    assert recovery.inspect(ctx['session'],prior['run'])['eligible']
    assert not (Path(prior['run'])/'continuation-claim.json').exists()


def test_null_recovery_metadata_is_rejected_without_effect_or_claim(ctx):
    from agent_service.computer_goals import _save_goal
    plan = prepare(ctx,browser=None)
    plan.pop('sha256')
    plan['recovery'] = None
    plan = _save_goal(ctx['session'],plan)
    with pytest.raises(ComputerError,match='recovery_metadata_invalid'):
        run(ctx,plan)
    assert not ctx['calls']


def test_verified_office_pause_then_resume_only_remaining_edit(ctx):
    plan=prepare(ctx,browser=None)
    prior=run(ctx,plan,pause_after_step=lambda: True)
    assert prior['code']=='agent_paused' and prior['state']=='partial'
    assert len(prior['completed'])==1 and len(prior['artifacts'])==1
    parent=Path(prior['run'])
    original={p:p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    resumed=recovery.prepare(ctx['session'],parent)['plan']
    final=run(ctx,resumed,pause_after_step=lambda: True)
    assert final['ok'] and final['actions']==1 and len(final['artifacts'])==2
    assert [item['kind'] for item in ctx['calls']]==['document','spreadsheet']
    assert all(p.read_bytes()==value for p,value in original.items())
