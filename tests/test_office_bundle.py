"""Offline package/receipt integration; fake Office never touches native apps."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service import office_bundle as bundle, artifact_source, office_workflow
from agent_service.computer import ComputerSession, ComputerError, read_record
from test_artifact_source import owned, fake


class Client:
    def __init__(self, supported=True):
        self.answer={'supported':supported,'reason':'literal table comparison'};self.calls=[]
    def complete(self, system, user, **kwargs):
        self.calls.append((system,user,kwargs));return self.answer


@pytest.fixture
def setup(tmp_path,monkeypatch):
    table={'headers':['项目','值'],'rows':[['甲','秘密原文'],['乙','']]}
    base,task,w,run=owned(tmp_path,{'kind':'spreadsheet','title':'表',**table,'sum_columns':[]},
        second={'kind':'document','title':'报告','paragraphs':['范围'],'table':table})
    refs=[artifact_source.prepare_artifact_source(v['artifact'],v['sha256'],base) for v in run['receipts'].values()]
    session=ComputerSession.create(tmp_path/'computer')
    monkeypatch.setattr(bundle,'BASE',session.root.parent)
    plan=bundle.prepare(session,'核对这两份表格的原文，输出Excel与Word核对报告',refs,task_base=base)
    return session,base,refs,plan


def execute(setup, client=None, runner=None, **kwargs):
    s,b,_,p=setup
    def offline(steps,root,**options):
        return office_workflow.run_office_workflow(steps,root,execute=fake,**options)
    return bundle.execute(s,p,approval=p['sha256'],task_base=b,
        client=client or Client(),workflow_runner=runner or offline,**kwargs)


def test_preview_no_model_no_task_and_complete_native_contract(setup):
    s,b,refs,plan=setup;before=set(b.iterdir());client=Client()
    assert 'S2' in bundle.describe(plan) and not list((s.root/'model_calls').iterdir())
    result=execute(setup,client)
    assert result['ok'] and result['positions']==6 and result['differences']==0
    assert len(set(b.iterdir())-before)==1 and len(client.calls)==1
    assert '秘密原文' not in client.calls[0][1] and str(b) not in client.calls[0][1]
    task=Path(result['task_root']);report=read_record(task/'comparison.json')
    assert [r['ref'] for r in report['sources']]==refs
    for item in result['artifacts']:
        ref=artifact_source.prepare_artifact_source(item['path'],item['sha256'],b)
        assert ref['task_root']==str(task)
    assert execute(setup,client,runner=lambda *a,**k:pytest.fail('replay'))==result
    assert len(client.calls)==1


def test_rejection_before_task_and_no_retry(setup):
    before=set(setup[1].iterdir());client=Client(False)
    result=execute(setup,client,runner=lambda *a,**k:pytest.fail('dispatch'))
    assert result['state']=='needs_input' and set(setup[1].iterdir())==before
    assert execute(setup,client)==result and len(client.calls)==1


@pytest.mark.parametrize('answer',[{}, {'supported':'yes','reason':'bad'}, {'supported':True,'reason':''},
    {'supported':True,'reason':'ok','paths':['outside']}])
def test_bad_model_cannot_dispatch(setup,answer):
    c=Client();c.answer=answer
    r=execute(setup,c,runner=lambda *a,**k:pytest.fail('dispatch'))
    assert not r['ok'] and r['code']=='bundle_decision_invalid'
    assert execute(setup,c)==r and len(c.calls)==1


def test_stop_before_model(setup):
    c=Client();r=execute(setup,c,stop=lambda:True)
    assert r['state']=='cancelled' and r['before_start'] and not c.calls
    assert execute(setup,c)==r


@pytest.mark.parametrize('location',['source','snapshot','comparison','manifest','result','claim','decision'])
def test_tampered_success_rejected(setup,location):
    r=execute(setup);task=Path(r['task_root']);run=Path(r['run'])
    if location=='source':p=Path(setup[2][1]['artifact']);p.write_bytes(p.read_bytes()+b'changed')
    elif location=='snapshot':p=next((task/'inputs').glob('S2*'));p.write_bytes(p.read_bytes()+b'changed')
    else:
        p={'comparison':task/'comparison.json','manifest':next(task.glob('workflow-*'))/'manifest.json',
           'result':task/'result.json','claim':run/'claim.json','decision':run/'decision.json'}[location]
        p.write_text('{}',encoding='utf-8')
    with pytest.raises((ValueError,KeyError,TypeError,OSError)):
        execute(setup,runner=lambda *a,**k:pytest.fail('replay'))


def test_unknown_effect_is_not_replayed(setup):
    def crash(steps,root,**kwargs):root.mkdir();raise RuntimeError('partial dispatch')
    r=execute(setup,runner=crash)
    assert r['state']=='unknown'
    assert execute(setup,runner=lambda *a,**k:pytest.fail('replay'))==r


def item(headers,rows,totals=None):
    return {'kind':'table','observed':{'headers':headers,'rows':rows},
        'coverage':{'derived_totals':totals or {}},'ref':{'artifact':'sample.xlsx','sha256':'a'*64}}


def test_differences_include_headers_empty_missing_and_extra_rows():
    a=item(['名称','值'],[['甲',''],['乙','无：该位置不存在']])
    b=item(['名称','金额'],[['甲','']])
    c=item(['名称','值'],[['甲','1'],['乙','无：该位置不存在'],['丙','2']])
    report=bundle.compare([a,b,c])
    assert report['positions']==8 and len(report['differences'])==6
    by={(d['row'],d['column']):d['values'] for d in report['differences']}
    assert by[3,2][0]['present'] and not by[3,2][1]['present']
    assert by[2,2][0]['value']=='' and by[2,2][2]['value']=='1'
    assert len(bundle._steps(report)[0]['data']['rows'])==6


def test_four_sources_and_text_value_policy():
    report=bundle.compare([item(['值'],[[3]]),item(['值'],[['3']]),item(['值'],[[3]]),item(['值'],[['3']])])
    assert report['differences']==[] and len(report['sources'])==4
    assert '数字类型' in bundle._steps(report)[1]['paragraphs'][1]


@pytest.mark.parametrize('inputs,code',[
    ([item(['值'],[[1]],{'0':1}),item(['值'],[[1]])],'bundle_calculated_table_unsupported'),
    ([item(['值'],[['x']]*200),item(['值'],[['x']])],'bundle_comparison_too_large'),
    ([{'kind':'document','observed':{'paragraphs':['x']},'coverage':{},'ref':{}},item(['值'],[[1]])],
     'bundle_table_required')])
def test_unsupported_structure_never_silently_truncates(inputs,code):
    with pytest.raises(ComputerError,match=code):bundle.compare(inputs)


@pytest.mark.parametrize('count',[0,1,5])
def test_invalid_source_count(setup,count):
    s,b,refs,_=setup
    with pytest.raises(ComputerError):bundle.prepare(s,'核对',refs[:count] if count<2 else refs*3,task_base=b)


def test_approval_mismatch_and_duplicate_sources(setup):
    s,b,refs,p=setup
    with pytest.raises(ComputerError):bundle.execute(s,p,approval='a'*64,task_base=b)
    with pytest.raises(ValueError):bundle.prepare(s,'核对',[refs[0],refs[0]],task_base=b)


@pytest.mark.parametrize('location',['inputs.json','comparison.json','bundle-request.json','snapshot'])
def test_history_and_next_source_share_joint_proof_guard(setup,location):
    from agent_service.task_history import inspect_task
    result=execute(setup);task=Path(result['task_root']);artifact=result['artifacts'][0]
    p=next((task/'inputs').glob('S1*')) if location=='snapshot' else task/location
    p.write_bytes(p.read_bytes()+b'changed')
    assert inspect_task(task,setup[1])['state']=='needs_review'
    with pytest.raises(ValueError):
        artifact_source.prepare_artifact_source(artifact['path'],artifact['sha256'],setup[1])


def test_joint_task_never_uses_unguarded_generic_resume(setup):
    from agent_service.office_resume import preview_resume
    def cancelled(steps,root,**kwargs):
        # A deterministic stopped workflow with no native intent.
        return office_workflow.run_office_workflow(steps,root,execute=fake,cancel_requested=lambda:True)
    result=execute(setup,runner=cancelled)
    task=setup[1]/('task-'+setup[3]['request_id'])
    assert result['state']=='cancelled'
    preview=preview_resume(task,setup[1])
    assert not preview['eligible'] and preview['reason']=='resume_bundle_owned'


def test_full_length_cell_values_are_not_prefixed_or_truncated():
    text='长'*2000
    report=bundle.compare([item(['值'],[[text]]),item(['值'],[['短']])])
    data=bundle._steps(report)[0]['data']
    assert data['rows'][0][3]==text and data['rows'][0][5]=='短'


def test_proof_cannot_relocate_to_unmanaged_session_root(setup,monkeypatch):
    result=execute(setup)
    monkeypatch.setattr(bundle,'BASE',setup[0].root.parent/'different-root')
    from agent_service.task_history import inspect_task
    assert inspect_task(result['task_root'],setup[1])['state']=='needs_review'
    with pytest.raises(ValueError):
        artifact_source.prepare_artifact_source(result['artifacts'][0]['path'],
            result['artifacts'][0]['sha256'],setup[1])


def test_prospective_root_is_counted_before_planning(setup,monkeypatch):
    seen=[]
    def four_level_input(refs,base):
        seen.append(bundle._checking.get())
        # The input has four levels, so admitting a child would exceed depth4.
        if bundle._checking.get():raise ComputerError('bundle_source_chain_invalid')
    monkeypatch.setattr(bundle,'_refs',four_level_input)
    with pytest.raises(ComputerError,match='chain_invalid'):
        bundle.prepare(setup[0],'核对',setup[2],task_base=setup[1])
    c=Client()
    with pytest.raises(ComputerError,match='chain_invalid'):execute(setup,c)
    assert not c.calls and all(len(chain)==1 for chain in seen)
    assert bundle._checking.get()==()


def test_joint_proof_dependencies_participate_in_skill_fingerprint():
    from agent_service.local_skills import _CODE
    assert {'office_bundle.py','capability_source_choice.py','office_handoff.py'} <= set(_CODE)
