"""Offline clarification authorization and no-replay contracts; no Office effects."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import capability_clarification as clarification, capability_router as router
from agent_service.computer import ComputerError, ComputerSession, read_record, write_new
from test_capability_source_choice import setup, Client, execute, run_task


def ambiguous(fixture):
    result = execute(fixture, Client(None))
    return Path(result['run'])


def prepare(fixture, run, source_id='S2', session=None):
    return clarification.prepare(session or fixture[0], run, source_id, task_base=fixture[1])


def dispatch(fixture, plan, runner=run_task, **kwargs):
    return router.execute(fixture[0], plan, approval=plan['sha256'], task_base=fixture[1],
                          runner=runner, client=object(), **kwargs)


def test_restart_local_preview_and_single_dispatch_preserve_original(setup):
    session, base, refs, old = setup
    run = ambiguous(setup)
    original = {p.name: (sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in run.iterdir()}
    listed = clarification.pending(computer_base=session.root.parent, task_base=base)
    assert listed[0]['run'] == str(run) and len(listed[0]['sources']) == 2
    calls = list((session.root / 'model_calls').iterdir())
    new = prepare(setup, run, session=ComputerSession(session.root))
    assert new['schema'] == 3 and new['source_ref'] == refs[1] and new['instruction'] == old['instruction']
    assert calls == list((session.root / 'model_calls').iterdir())
    assert not base.exists() and not (run / 'source-clarification-claim.json').exists()
    result = dispatch(setup, new)
    assert dispatch(setup, new, runner=lambda *a, **k: pytest.fail('replayed')) == result
    assert calls == list((session.root / 'model_calls').iterdir())
    assert all((sha256((run/n).read_bytes()).hexdigest(),(run/n).stat().st_mtime_ns)==v for n,v in original.items())
    assert clarification.pending(computer_base=session.root.parent, task_base=base) == []
    assert router.inspect_saved(run, task_base=base, computer_base=session.root.parent)['state'] == 'needs_input'


def test_two_previews_cannot_dispatch_twice(setup):
    run = ambiguous(setup)
    first, second = prepare(setup, run, 'S1'), prepare(setup, run, 'S2')
    dispatch(setup, first)
    with pytest.raises(ComputerError, match='clarification_claimed'):
        dispatch(setup, second)
    with pytest.raises(ComputerError, match='clarification_claimed'):
        prepare(setup, run)
    assert not (setup[1] / ('task-' + second['request_id'])).exists()


@pytest.mark.parametrize('mutation', ['parent_plan','parent_outcome','parent_selection','source','task_exists','extra_file'])
def test_changed_parent_or_sources_rejected_before_new_task(setup, mutation):
    run = ambiguous(setup)
    plan = prepare(setup, run)
    if mutation == 'source':
        setup[2][0]['sha256'] = 'f' * 64  # Revalidate even the unselected candidate.
    elif mutation == 'task_exists':
        (setup[1] / ('task-' + setup[3]['request_id'])).mkdir(parents=True)
    elif mutation == 'extra_file':
        (run / 'unexpected-effect.json').write_text('{}')
    else:
        path = {'parent_plan':run.parent/(setup[3]['sha256']+'.json'),
                'parent_outcome':run/'outcome.json','parent_selection':run/'source-selection.json'}[mutation]
        path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises((ValueError, OSError)):
        dispatch(setup, plan, runner=lambda *a,**k: pytest.fail('dispatched after mutation'))
    assert not (setup[1] / ('task-'+plan['request_id'])).exists()


@pytest.mark.parametrize('source_id', ['S0','S3',None,2])
def test_outside_approved_source_rejected(setup, source_id):
    run = ambiguous(setup)
    with pytest.raises(ComputerError):
        prepare(setup, run, source_id)


def test_cancelled_parent_never_offered(setup):
    result = execute(setup, Client(), stop=lambda: True)
    with pytest.raises(ComputerError):
        prepare(setup, result['run'])
    assert clarification.pending(computer_base=setup[0].root.parent, task_base=setup[1]) == []


def test_unknown_parent_never_offered(setup):
    client = Client(); client.answer = {}
    with pytest.raises(ComputerError):
        execute(setup, client)
    run = setup[0].root/'goals'/(setup[3]['sha256']+'.capability')
    with pytest.raises((ValueError,OSError)):
        prepare(setup, run)
    assert clarification.pending(computer_base=setup[0].root.parent, task_base=setup[1]) == []


def test_cancelled_child_consumes_claim_and_rechecks_without_effects(setup):
    run = ambiguous(setup)
    plan = prepare(setup, run)
    result = dispatch(setup, plan, stop=lambda: True)
    assert result['state']=='cancelled' and result['before_start']
    assert not setup[1].exists()
    assert dispatch(setup, plan) == result
    with pytest.raises(ComputerError):
        prepare(setup, run)


def test_unknown_child_never_replayed_or_reclarified(setup):
    run = ambiguous(setup)
    plan = prepare(setup, run)
    def crash(*args,**kwargs):
        raise RuntimeError('uncertain dispatch')
    with pytest.raises(RuntimeError):
        dispatch(setup, plan, runner=crash)
    assert dispatch(setup, plan)['state']=='unknown'
    with pytest.raises(ComputerError):
        prepare(setup, run)


def test_wrong_child_source_cannot_be_published(setup):
    run = ambiguous(setup)
    plan = prepare(setup, run)
    def wrong(*args,**kwargs):
        kwargs['source_artifact'] = setup[2][0]
        return run_task(*args,**kwargs)
    with pytest.raises(ComputerError, match='source_mismatch'):
        dispatch(setup, plan, runner=wrong)
    assert dispatch(setup, plan)['state']=='unknown'


def test_parent_claim_is_required_for_saved_child(setup):
    plan = prepare(setup, ambiguous(setup))
    result = dispatch(setup, plan)
    path = Path(plan['clarification']['run'])/'source-clarification-claim.json'
    path.write_text('{}')
    with pytest.raises(ComputerError):
        router.inspect_saved(result['run'], task_base=setup[1], computer_base=setup[0].root.parent)
