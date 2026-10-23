from types import SimpleNamespace
import threading
import pytest
from agent_service import office_edit_adapter_panel as panel, computer_goal_panel as main


def plan(recovery=False):
    value={'mode':'agent','sha256':'a'*64,'goal':{'kind':'interactive','browser':None,'edits':[{'id':'e1'}]}}
    if recovery:value['recovery']={}
    return value


def state(remaining=1):
    return {'candidate':'b'*64,'offline':True,'reviewed':True,'trial_passed':True,'remaining':remaining}


def test_unowned_recovery_is_refused_without_generic_fallback(monkeypatch):
    monkeypatch.setattr(panel.adapter,'owns_recovery',lambda *a:None)
    assert panel.uses_adapter(object(),plan(True))
    with pytest.raises(ValueError,match='edit_adapter_recovery_not_owned'):
        panel.execute(SimpleNamespace(stopped=threading.Event()),object(),plan(True),
                      progress=lambda p:None,pause_after_step=lambda:False)
    assert panel.uses_adapter(object(),plan())
    mixed=plan();mixed['goal']['browser']={'mode':'public'}
    assert not panel.uses_adapter(object(),mixed)


def test_adapter_recovery_uses_owning_candidate(monkeypatch):
    monkeypatch.setattr(panel.adapter,'owns_recovery',lambda *a:{'candidate':'c'*64})
    calls=[]
    monkeypatch.setattr(panel.adapter,'resume',lambda *a,**k:calls.append((a,k)) or {'outcome':{'ok':True}})
    parent=SimpleNamespace(stopped=threading.Event())
    assert panel.execute(parent,object(),plan(True),progress=lambda p:None,pause_after_step=lambda:False)=={'ok':True}
    assert calls[0][0][1]=='c'*64 and calls[0][1]['approval']=='a'*64


@pytest.mark.parametrize('reviewed',[False,True])
def test_offline_button_returns_state_shape(monkeypatch,tmp_path,reviewed):
    import tkinter
    from tkinter import ttk
    buttons=[]
    class Widget:
        def __init__(self,*a,**k):
            if 'command' in k:buttons.append(k)
        def pack(self,**k):pass
        def title(self,*a):pass
        def destroy(self):pass
    monkeypatch.setattr(tkinter,'Toplevel',Widget)
    monkeypatch.setattr(ttk,'Label',Widget);monkeypatch.setattr(ttk,'Button',Widget)
    monkeypatch.setattr(panel.adapter,'ensure_candidate',lambda *a,**k:state())
    monkeypatch.setattr(panel.adapter,'offline',lambda *a:{'candidate':'b'*64,'passed':True,'fixtures':[]})
    existing=state();existing['reviewed']=reviewed
    (tmp_path/('b'*64)).mkdir()
    monkeypatch.setattr(panel.adapter,'DEFAULT_STORE',tmp_path)
    monkeypatch.setattr(panel.adapter,'current_candidate',lambda:'b'*64)
    monkeypatch.setattr(panel.adapter,'status',lambda *a:existing)
    reconciled=[]
    monkeypatch.setattr(panel.adapter,'reconcile',lambda *a:reconciled.append(True))
    results=[]
    parent=SimpleNamespace(running=False,closed=False,external_busy=lambda:False,root=object(),
        session=object(),plan=plan(),status=SimpleNamespace(set=lambda x:None),
        start=lambda kind,fn:results.append((kind,fn(None))))
    panel.open_panel(parent)
    assert reconciled==([True] if reviewed else [])
    next(b for b in buttons if b['text']=='运行离线检查')['command']()
    assert results[0][0]=='edit_adapter_state'
    assert '离线检查：通过' in panel.describe_state(results[0][1])


@pytest.mark.parametrize('available',[False,True])
def test_main_execute_keeps_diff_until_ready(monkeypatch,available):
    instance=main.GoalPanel.__new__(main.GoalPanel)
    original=plan();events=[]
    instance.running=False;instance.external_busy=lambda:False;instance.plan=original
    instance.session=object();instance.pause_each_step=SimpleNamespace(get=lambda:False)
    instance.invalidate=lambda:events.append('invalidate')
    instance.open_edit_adapter=lambda:events.append('open')
    instance.start=lambda kind,fn:events.append(kind)
    monkeypatch.setattr(panel.adapter,'status',lambda *a:state(1 if available else 0))
    instance.execute()
    assert events==(['invalidate','execute'] if available else ['open'])
    if not available:assert instance.plan is original


def test_reopen_uses_active_intent_instead_of_resume_intent(monkeypatch,tmp_path):
    import tkinter
    from tkinter import ttk
    from agent_service import computer
    buttons=[];reads=[]
    class Widget:
        def __init__(self,*a,**k):
            if 'command' in k:buttons.append(k)
        def pack(self,**k):pass
        def title(self,*a):pass
        def destroy(self):pass
    monkeypatch.setattr(tkinter,'Toplevel',Widget)
    monkeypatch.setattr(ttk,'Label',Widget);monkeypatch.setattr(ttk,'Button',Widget)
    root=tmp_path/('b'*64);root.mkdir()
    (root/'active-1-intent.json').touch();(root/'active-1-resume-1-intent.json').touch()
    monkeypatch.setattr(panel.adapter,'DEFAULT_STORE',tmp_path)
    monkeypatch.setattr(panel.adapter,'current_candidate',lambda:'b'*64)
    monkeypatch.setattr(panel.adapter,'status',lambda *a:state())
    monkeypatch.setattr(panel.adapter,'reconcile',lambda *a:None)
    monkeypatch.setattr(computer,'ComputerSession',lambda *a,**k:SimpleNamespace(root=tmp_path/'session'))
    def read(path):
        reads.append(path.name)
        if path.name=='active-1-intent.json':return {'session_root':str(tmp_path/'session'),'plan_sha256':'a'*64}
        if path.name=='a'*64+'.json':return plan()
        pytest.fail('Resume intent must not be used as origin')
    monkeypatch.setattr(computer,'read_record',read)
    parent=SimpleNamespace(running=False,closed=False,external_busy=lambda:False,root=object(),
        session=None,plan=None,host=None,status=SimpleNamespace(set=lambda x:None))
    panel.open_panel(parent)
    assert reads==['active-1-intent.json','a'*64+'.json']
    assert any(b['text']=='预览未完成步骤' for b in buttons)
