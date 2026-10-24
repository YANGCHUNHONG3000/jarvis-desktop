"""Bundle-specific proof and interrupted-prefix recovery; no native apps."""
from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service import office_bundle_resume as recovery, office_resume as generic
from agent_service import office_workflow, task_history, artifact_source
from test_office_bundle import setup, execute, Client
from test_artifact_source import fake


def interrupted(setup, *, first=True):
    def runner(steps, root, **kw):
        def stop_after(spec, folder, stop):
            out = fake(spec, folder, stop)
            (root/'cancel.flag').touch()
            return out
        if not first:
            kw['cancel_requested'] = lambda: True
        return office_workflow.run_office_workflow(steps, root, execute=stop_after, **kw)
    result = execute(setup, runner=runner)
    task = setup[1]/('task-'+setup[3]['request_id'])
    assert result['state']=='cancelled'
    return task, setup[1], next(task.glob('workflow-*'))


def test_prefix_recovery_history_next_source_and_generic_stays_closed(setup):
    task,base,workspace=interrupted(setup)
    old={str(f):(f.read_bytes(),f.stat().st_mtime_ns) for root in (task,Path(setup[0].root/'goals'))
         for f in root.rglob('*') if f.is_file()}
    preview=recovery.preview_resume(task,base)
    assert preview['eligible'] and preview['completed']==1 and preview['remaining']==1
    assert generic.preview_resume(task,base)['reason']=='resume_bundle_owned'
    calls=[]
    def rest(spec,folder,stop):
        calls.append(spec['kind']);return fake(spec,folder,stop)
    attempt=uuid.uuid4().hex
    result=recovery.resume_task(task,base,preview['token'],attempt,execute=rest)
    assert result['ok'] and result['kind']=='bundle_resume' and calls==['document']
    assert recovery.read_resume_result(task,base,attempt)==result
    assert recovery.latest_continuation(task,base)==result
    history=task_history.inspect_task(task,base)
    assert history['continuation']=={'state':'succeeded','attempt_id':attempt,'kind':'bundle_resume'}
    assert len(history['artifacts'])==2
    for a in result['artifacts']:
        ref=artifact_source.prepare_artifact_source(a['path'],a['sha256'],base)
        assert ref['task_root']==str(task)
    assert not recovery.preview_resume(task,base)['eligible']
    assert all(Path(f).read_bytes()==value[0] and Path(f).stat().st_mtime_ns==value[1]
               for f,value in old.items())
    with pytest.raises(ValueError):
        recovery.resume_task(task,base,preview['token'],attempt,execute=lambda *_:pytest.fail('replay'))


def test_zero_steps_and_repeated_clean_cancellation(setup):
    task,base,w=interrupted(setup,first=False)
    p=recovery.preview_resume(task,base);assert p['eligible'] and p['completed']==0
    aid=uuid.uuid4().hex;folder=task/('resume-'+aid);folder.mkdir();(folder/'cancel.flag').touch()
    out=recovery.resume_task(task,base,p['token'],aid,execute=fake)
    assert out['state']=='cancelled' and out['completed']==0
    p=recovery.preview_resume(task,base);assert p['eligible']
    out=recovery.resume_task(task,base,p['token'],uuid.uuid4().hex,execute=fake)
    assert out['ok'] and len(out['artifacts'])==2
    assert recovery.latest_continuation(task,base)['ok']


@pytest.mark.parametrize('location',['source','snapshot','report','origin','decision','approval','receipt','request'])
def test_tampered_proof_blocks_before_dispatch(setup,location):
    task,base,w=interrupted(setup);p=recovery.preview_resume(task,base)
    run=Path(json.loads((task/'bundle-request.json').read_text())['run'])
    path={'source':Path(setup[2][0]['artifact']), 'snapshot':task/'inputs/S1.xlsx',
          'report':task/'comparison.json','origin':run/'outcome.json','decision':run/'decision.json',
          'approval':run.with_suffix('.json'),'receipt':w/'s1/receipt.json','request':task/'bundle-request.json'}[location]
    path.write_bytes(path.read_bytes()+b'changed')
    assert not recovery.preview_resume(task,base)['eligible']
    with pytest.raises((ValueError,KeyError,TypeError,OSError)):
        recovery.resume_task(task,base,p['token'],uuid.uuid4().hex,execute=lambda *_:pytest.fail('effect'))
    assert not (w/'s2').exists()


def test_unknown_attempt_cannot_be_ignored(setup):
    task,base,w=interrupted(setup);p=recovery.preview_resume(task,base)
    attempt=task/('resume-'+uuid.uuid4().hex);attempt.mkdir();(attempt/'request.json').write_text('{}')
    assert recovery.preview_resume(task,base)['reason']=='bundle_resume_attempt_unknown'
    with pytest.raises(ValueError):
        recovery.resume_task(task,base,p['token'],uuid.uuid4().hex,execute=lambda *_:pytest.fail('effect'))


def test_unknown_intent_blocks_and_original_success_is_not_recovered(setup):
    task,base,w=interrupted(setup)
    (w/'s2').mkdir();(w/'s2/intent.json').write_text('{}')
    assert not recovery.preview_resume(task,base)['eligible']


def test_completed_task_has_no_resume(setup):
    result=execute(setup)
    assert recovery.preview_resume(result['task_root'],setup[1])['reason']=='resume_already_complete'


def test_limit_before_any_new_attempt(setup):
    task,base,w=interrupted(setup)
    for _ in range(33):(task/('resume-'+uuid.uuid4().hex)).mkdir()
    assert recovery.preview_resume(task,base)['reason']=='bundle_resume_limit'


def test_preview_at_exact_attempt_limit_disables_new_attempt(setup,monkeypatch):
    task,base,w=interrupted(setup)
    for _ in range(32):(task/('resume-'+uuid.uuid4().hex)).mkdir()
    monkeypatch.setattr(generic,'_preview_resume',lambda *a,**k:{'eligible':True,'remaining':1})
    assert recovery.preview_resume(task,base)=={'eligible':False,'remaining':1,
        'reason':'bundle_resume_limit','kind':'bundle_resume'}


def test_new_proof_dependency_fingerprinted():
    from agent_service.local_skills import _CODE
    assert 'office_bundle_resume.py' in _CODE


def test_repeated_preflight_block_preserves_prefix_then_completes(setup,monkeypatch):
    from agent_service import office_readiness
    task,base,w=interrupted(setup)
    monkeypatch.setattr(office_readiness,'preflight_office_window',lambda:'office_ui_blocked')
    for _ in range(2):
        p=recovery.preview_resume(task,base);assert p['eligible']
        result=recovery.resume_task(task,base,p['token'],uuid.uuid4().hex)
        assert result['state']=='blocked' and result['completed']==1 and not (w/'s2').exists()
    p=recovery.preview_resume(task,base);assert p['eligible']
    result=recovery.resume_task(task,base,p['token'],uuid.uuid4().hex,execute=fake)
    assert result['ok'] and recovery.latest_continuation(task,base)['ok']


def test_stale_preview_makes_no_attempt(setup):
    task,base,w=interrupted(setup)
    with pytest.raises(ValueError,match='resume_preview_stale'):
        recovery.resume_task(task,base,'0'*64,uuid.uuid4().hex,execute=fake)
    assert not list(task.glob('resume-*')) and recovery.preview_resume(task,base)['eligible']
