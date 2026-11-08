from copy import deepcopy
from pathlib import Path
import json
import pytest

from agent_service.computer import ComputerError
from agent_service import windows_task as task, windows_visual as visual
from agent_service import windows_visual_proposal as proposal, windows_visual_memory as memory
from agent_service import windows_intent as intent

WINDOW={'hwnd':100,'pid':200,'created':'start','image':'C:/wps.exe','class_name':'OpusApp',
        'title':'Offline workbook','profile':'observe_only'}
CANDIDATE={'window':WINDOW,'label':'视图','expected':['冻结窗格','重排窗口'],'source':{'kind':'offline'}}
CONDITION=visual.condition(WINDOW,'视图',CANDIDATE['expected'])
GOAL={'schema':1,'instruction':'查看显示工具','conditions':[CONDITION],'max_steps':12}


def observation(*controls):
    return {'window':deepcopy(WINDOW),'application':'wps','scope':'visible_controls',
            'controls':list(controls),'truncated':False,'dialogs':[]}


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setattr(memory,'verify',lambda item:None)
    monkeypatch.setattr(memory,'discover',lambda windows:[deepcopy(CANDIDATE)])


def prepared(tmp_path):
    ref=proposal.save(GOAL,[CANDIDATE],[observation()],parent=tmp_path/'proposals')
    return task.create(tmp_path/'tasks',GOAL,visual_proposal=ref),ref


class Backend:
    def __init__(self):self.observations=0;self.acts=0
    def observe(self,window,*,stop):
        self.observations+=1
        return observation()
    def observe_condition(self,c,*,stop):
        self.observations+=1
        frame={'schema':1,'sha256':'a'*64,'width':900,'height':240,'words':[
            {'text':s,'box':[x,50,28,16]} for s,x in [('开始',100),('插入',150),('视图',200)]]+[
            {'text':'冻结窗格','box':[300,100,60,16]},{'text':'重排窗口','box':[400,100,60,16]}]}
        control,details=visual.presentation(frame,c['selector'],c['value'])
        return {**observation(),'scope':'visual_navigation','controls':[control],'visual':details}
    def act(self,*a,**kw):self.acts+=1;pytest.fail('already satisfied page must not click')
    candidates=staticmethod(visual.candidates)


class Client:
    def __init__(self):self.calls=0
    def complete(self,system,context,*,max_tokens):
        self.calls+=1
        data=json.loads(context)
        rows=[c for w in data['windows'] for c in w['candidates'] if c['type']=='VisualNavigation']
        assert len(rows)==1 and rows[0]['evidence']==CANDIDATE['expected']
        assert 'source' not in context and 'C:/wps.exe' not in context and 'Offline workbook' not in context
        return {'conditions':[{'id':rows[0]['id'],'state':'navigate','value':True,'retain':False}]}


def test_auto_proposal_saves_pending_without_action_or_approval(tmp_path,trusted):
    backend,client=Backend(),Client()
    r=proposal.propose(GOAL['instruction'],[observation()],backend,client,
                       task_base=tmp_path/'tasks',proposal_base=tmp_path/'proposals')
    assert r['visual'] and r['goal']==GOAL and client.calls==1 and backend.acts==0
    loaded=task.load(r['root'])
    assert loaded['summary']['approval_required'] and not loaded['summary']['resumable']
    assert not (Path(r['root'])/'visual-approval.json').exists()


def test_unapproved_run_stops_before_observation(tmp_path,trusted):
    root,_=prepared(tmp_path);backend=Backend()
    with pytest.raises(ComputerError,match='approval_required'):task.run(root,backend)
    assert backend.observations==backend.acts==0 and list((root/'attempts').iterdir())==[]


def test_approval_is_immutable_and_recovered_without_model(tmp_path,trusted):
    root,_=prepared(tmp_path);proposal.approve(root)
    before=(root/'visual-approval.json').read_bytes()
    proposal.approve(root)
    assert (root/'visual-approval.json').read_bytes()==before
    assert not task.load(root)['summary']['approval_required']
    backend=Backend();r=task.run(root,backend)
    assert r['state']=='verified' and backend.acts==0


def test_modified_proposal_invalidates_approval(tmp_path,trusted):
    root,ref=prepared(tmp_path);proposal.approve(root)
    path=Path(ref['root'])/'proposal.json'
    data=json.loads(path.read_text('utf-8'));data['payload']['goal']['instruction']='different'
    path.write_text(json.dumps(data),'utf-8')
    with pytest.raises(ComputerError):task.run(root,Backend())


def test_source_change_blocks_existing_approved_task(tmp_path,trusted,monkeypatch):
    root,_=prepared(tmp_path);proposal.approve(root)
    def changed(item):raise ComputerError('source_changed')
    monkeypatch.setattr(memory,'verify',changed)
    with pytest.raises(ComputerError,match='source_changed'):task.run(root,Backend())


def test_popup_blocks_before_cloud_or_saved_proposal(tmp_path,trusted):
    backend,client=Backend(),Client()
    backend.observe=lambda *a,**kw:{**observation(),'blocked_reason':'windows_application_blocked'}
    with pytest.raises(ComputerError,match='application_blocked'):
        proposal.propose('查看显示工具',[observation()],backend,client,proposal_base=tmp_path)
    assert client.calls==0 and backend.acts==0 and list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('change',[{'value':'视图'},{'retain':True},{'state':'value','value':'guessed words'}, {'id':'invented'}])
def test_model_cannot_invent_visual_effects_or_verification(trusted,change):
    _,targets=intent._context('查看显示工具',[observation()],[CANDIDATE])
    key=next(k for k,t in targets.items() if len(t)==4)
    response={'conditions':[{'id':key,'state':'navigate','value':True,'retain':False,**change}]}
    with pytest.raises(ComputerError):intent._goal('查看显示工具',response,targets)


def test_matching_uia_tab_prevents_visual_candidate(trusted):
    tab={'id':'tab','type':'TabItem','name':'视图','automation_id':'view','selected':False,
         'visible':True,'enabled':True,'patterns':['select'],'ancestors':[]}
    context,targets=intent._context('查看显示工具',[observation(tab)],[CANDIDATE])
    assert 'VisualNavigation' not in context and all(len(t)==3 for t in targets.values())


def test_approval_is_bound_to_exact_task(tmp_path,trusted):
    a,_=prepared(tmp_path/'a');b,_=prepared(tmp_path/'b');proposal.approve(a)
    (b/'visual-approval.json').write_bytes((a/'visual-approval.json').read_bytes())
    with pytest.raises(ComputerError,match='approval_changed'):task.run(b,Backend())


@pytest.fixture(scope='module')
def hidden_root():
    import tkinter as tk
    root=tk.Tk();root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(hidden_root):
    from tkinter import ttk
    from agent_service.windows_panel import WindowsPanel
    frame=ttk.Frame(hidden_root)
    p=WindowsPanel(frame,hidden_root)
    yield p
    p.close();frame.destroy()


def test_restored_pending_proposal_has_confirm_button_but_no_approval(tmp_path,trusted,panel):
    root,_=prepared(tmp_path)
    panel.messages.put(('restore',task.load(root),None));panel.poll()
    assert str(panel.execute_button.cget('state'))=='normal'
    assert panel.execute_button.cget('text')=='确认方案并执行'
    assert str(panel.resume_button.cget('state'))=='disabled'
    assert not (root/'visual-approval.json').exists()


def test_new_draft_is_not_overwritten_by_late_proposal(tmp_path,trusted,panel):
    root,_=prepared(tmp_path)
    panel.instruction.delete(0,'end');panel.instruction.insert(0,'新的任务')
    panel.messages.put(('propose',{'goal':GOAL,'root':root,'visual':True,
        'instruction':'旧的任务','revision':panel.draft_revision},None));panel.poll()
    assert panel.instruction.get()=='新的任务' and panel.conditions==[] and panel.task_root is None


def test_changed_visual_draft_cannot_be_previewed_as_unbound_task(tmp_path,trusted,panel):
    root,_=prepared(tmp_path)
    panel.messages.put(('restore',task.load(root),None));panel.poll()
    panel.instruction.delete(0,'end');panel.instruction.insert(0,'另一个任务')
    panel.preview()
    assert panel.task_root is None and '重新生成' in panel.status.get()
    assert str(panel.execute_button.cget('state'))=='disabled'
