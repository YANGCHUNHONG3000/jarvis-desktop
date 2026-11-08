"""Contract tests use fake native facts; live acceptance is kept separately."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from agent_service.computer import ComputerError, digest
from agent_service import windows_reviewed_controls as reviewed
from agent_service import windows_visual_proposal as proposal, windows_task as task, windows_intent as intent

WINDOW = {'hwnd':100,'pid':200,'created':'offline','image':'C:/wps.exe','class_name':'OpusApp','title':'offline','profile':'observe_only'}


@pytest.fixture
def context(tmp_path,monkeypatch):
    state={'done':set(),'popup':False,'acts':[],'unknown':False}
    def observe(window,source,role,text,check):
        if state['popup']:raise ComputerError('windows_reviewed_popup_requires_attention')
        return {'verified':role in state['done'],'facts':{'role':role,'done':role in state['done']}}
    def act(window,source,role,text,check,epoch):
        state['acts'].append(role);state['done'].add(role)
        if state['unknown']:raise ComputerError('native_lost')
    provider=SimpleNamespace(validate=lambda s:s,observe=observe,act=act)
    monkeypatch.setattr(reviewed,'native',lambda kind:provider)
    monkeypatch.setattr(reviewed,'_text',lambda role,value:value if type(value) is str and value else (_ for _ in ()).throw(ComputerError('text_invalid')))
    from agent_service import windows_file_dialog as files
    monkeypatch.setattr(files,'snapshot_file',lambda text:{'path':text,'sha256':'fake','size':1})
    snap={'window':WINDOW,'application':'wps','kind':'open_dialog','binding':{},
          'controls':[{'role':'filename','label':'文件名称(N)'},{'role':'cancel','label':'取消'}]}
    native_root=tmp_path/'native';native_root.mkdir()
    for name,data in [('native-request.json',{'op':'reviewed_snapshot','window':WINDOW,'kind':'open_dialog'}),
                      ('native-outcome.json',{'ok':True,'snapshot':snap})]:
        (native_root/name).write_text(json.dumps(data),encoding='utf8')
    snap['native_proof']={'root':str(native_root),'request_sha256':sha256((native_root/'native-request.json').read_bytes()).hexdigest(),
                          'outcome_sha256':sha256((native_root/'native-outcome.json').read_bytes()).hexdigest()}
    options=reviewed.candidates_for(snap,parent=tmp_path/'sources')
    obs={'window':WINDOW,'application':'wps','controls':[],'dialogs':[],'truncated':False,'scope':'visible_controls','reviewed_snapshot':snap}
    return state,snap,options,obs


def plan(context,tmp_path):
    _,_,options,obs=context
    text,targets=intent._context('填写 C:/existing.xlsx 后取消',[obs],options)
    assert 'native_proof' not in text and 'offline' not in text
    response={'conditions':[{'id':key,'state':value[2][0],'value':'C:/existing.xlsx' if value[2]==['filename'] else True,'retain':False}
                            for key,value in targets.items() if value[2][0] in {'filename','cancel'}]}
    goal=intent._goal('填写 C:/existing.xlsx 后取消',response,targets)
    ref=proposal.save(goal,options,[obs],parent=tmp_path/'proposals')
    return task.create(tmp_path/'tasks',goal,visual_proposal=ref),goal


class Backend:
    candidates=staticmethod(reviewed.actions)
    def __init__(self):self.reads=0
    def observe_condition(self,c,*,stop):
        self.reads+=1
        return reviewed.observation(c['window'],c['selector'],c['value'],lambda:None)
    def act(self,window,control,action,value,before,operation,*,stop):
        return reviewed.perform({'window':window,'selector':{k:control[k] for k in ('type','name','automation_id')},
                                 'value':value,'before_sha':before,'control':control},operation,lambda:None,None)


def test_review_restart_popup_and_completed_steps(context,tmp_path):
    state,_,_,_=context;root,goal=plan(context,tmp_path);backend=Backend()
    with pytest.raises(ComputerError,match='approval_required'):task.run(root,backend)
    assert backend.reads==0
    proposal.approve(root)
    first=task.run(root,backend,pause_after_step=True)
    assert first['completed']==['target_1'] and state['acts']==['filename']
    prior={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    state['popup']=True
    blocked=task.run(root,Backend())
    assert not blocked['ok'] and blocked['completed']==['target_1'] and state['acts']==['filename']
    state['popup']=False
    assert task.run(root,Backend())['ok'] and state['acts']==['filename','cancel']
    assert all(p.read_bytes()==raw for p,raw in prior.items())


def test_unknown_effect_never_replays(context,tmp_path):
    state,_,_,_=context;root,_=plan(context,tmp_path);proposal.approve(root);state['unknown']=True
    result=task.run(root,Backend())
    assert not result['ok'] and state['acts']==['filename']
    with pytest.raises(ComputerError,match='unknown_requires_review'):task.run(root,Backend())
    assert state['acts']==['filename']


@pytest.mark.parametrize('mutation',['proof','window','role','label','extra','target'])
def test_source_and_approval_tampering_rejected(context,tmp_path,mutation):
    _,snap,options,_=context
    item=deepcopy(options[0]);item['expected']=['C:/existing.xlsx']
    c=reviewed.condition(item)
    if mutation=='proof':Path(snap['native_proof']['root'],'native-outcome.json').write_text('{}')
    elif mutation=='window':item['window']={**WINDOW,'hwnd':555}
    elif mutation=='role':item['source']['role']='delete'
    elif mutation=='label':item['label']='Open'
    elif mutation=='extra':item['source']['command']='invoke'
    else:
        data=json.loads(c['value']);data['target_file']['sha256']='changed';c['value']=json.dumps(data)
    with pytest.raises(ComputerError):
        reviewed.decode(c['selector'],c['value']) if mutation in {'proof','target'} else reviewed.verify_candidate(item)


def test_task_cannot_bypass_review(context,tmp_path):
    _,goal=plan(context,tmp_path)
    with pytest.raises(ComputerError,match='approval_required'):task.create(tmp_path/'bypass',goal)


def test_cancel_before_filename_rejected_before_any_action(context,tmp_path):
    _,goal=plan(context,tmp_path)
    goal['conditions'].reverse()
    with pytest.raises(ComputerError,match='sequence_invalid'):task.validate_goal(goal)


@pytest.mark.parametrize('roles',[('up','directory'),('directory','up'),('directory','directory')])
def test_explorer_source_needs_new_observation_between_navigation(monkeypatch,roles):
    monkeypatch.setattr(reviewed,'decode',lambda selector,value:({'role':value},{'window':WINDOW,'kind':'explorer'}))
    conditions=[{'selector':{'type':reviewed.TYPE,'automation_id':reviewed.ID},'value':role} for role in roles]
    with pytest.raises(ComputerError,match='new_observation_required'):reviewed.validate_sequence(conditions)


@pytest.mark.parametrize('value',[False,0,'click',{},['cancel']])
def test_model_button_requires_exact_true(context,value):
    _,_,options,obs=context;_,targets=intent._context('取消',[obs],options)
    key=next(k for k,v in targets.items() if v[2]==['cancel'])
    with pytest.raises(ComputerError):intent._goal('取消',{'conditions':[{'id':key,'state':'cancel','value':value,'retain':False}]},targets)


def test_full_proposal_never_uses_navigation_capture(context,tmp_path,monkeypatch):
    _,snap,_,obs=context;original=reviewed.candidates_for
    monkeypatch.setattr(reviewed,'candidates_for',lambda s:original(s,parent=tmp_path/'auto'))
    class B:
        def observe(self,*a,**kw):return obs
        def visual_snapshot(self,*a,**kw):pytest.fail('specialised controls must not capture navigation')
        def visual_document_identity(self,*a,**kw):pytest.fail('document already bound')
    class Client:
        def complete(self,system,text,*,max_tokens):
            rows=json.loads(text)['windows'][0]['candidates'];button=next(r for r in rows if r['states']==['cancel'])
            return {'conditions':[{'id':button['id'],'state':'cancel','value':True,'retain':False}]}
    result=proposal.propose('取消',[obs],B(),Client(),task_base=tmp_path/'tasks',proposal_base=tmp_path/'proposals')
    assert result['visual'] and task.load(result['root'])['summary']['approval_required']
    assert list((result['root']/'attempts').iterdir())==[]


def test_explorer_view_model_uses_plain_search_and_fixed_button(context,tmp_path):
    _,snap,_,_=context
    raw={k:deepcopy(v) for k,v in snap.items() if k!='native_proof'}
    raw.update(kind='explorer_view',application='explorer',controls=[
        {'role':'search_text','label':'Search current window'}, {'role':'preview_pane','label':'Preview pane'}])
    proof_root=tmp_path/'view-native';proof_root.mkdir()
    for name,data in [('native-request.json',{'op':'reviewed_snapshot','window':WINDOW,'kind':'explorer_view'}),
                      ('native-outcome.json',{'ok':True,'snapshot':raw})]:
        (proof_root/name).write_text(json.dumps(data),encoding='utf8')
    raw['native_proof']={'root':str(proof_root),'request_sha256':sha256((proof_root/'native-request.json').read_bytes()).hexdigest(),
                        'outcome_sha256':sha256((proof_root/'native-outcome.json').read_bytes()).hexdigest()}
    options=reviewed.candidates_for(raw,parent=tmp_path/'view-sources')
    obs={'window':WINDOW,'application':'explorer','controls':[],'dialogs':[],'truncated':False,'scope':'visible_controls'}
    _,targets=intent._context('填写 query 然后切换预览',[obs],options)
    goal=intent._goal('填写 query 然后切换预览',{'conditions':[
        {'id':key,'state':v[2][0],'value':'query' if v[2]==['search_text'] else True,'retain':False}
        for key,v in targets.items()]},targets)
    assert [json.loads(c['value'])['role'] for c in goal['conditions']]==['search_text','preview_pane']
    assert all(json.loads(c['value'])['target_file'] is None for c in goal['conditions'])
    ref=proposal.save(goal,options,[obs],parent=tmp_path/'view-proposals')
    assert task.load(task.create(tmp_path/'view-tasks',goal,visual_proposal=ref))['summary']['approval_required']
