from copy import deepcopy
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk
import pytest
from agent_service import windows_stages_panel as ui
from agent_service.windows_panel import WindowsPanel
from agent_service.computer import ComputerError

@pytest.fixture(scope='module')
def root():
    value=tk.Tk();value.withdraw()
    yield value
    value.destroy()

@pytest.fixture
def panel(root,tmp_path):
    p=ui.WindowsStagesPanel(root,instruction='完整新任务',observations=[{'fixture':True}],session_base=tmp_path)
    p.backend=object()
    yield p
    p.close()

def view(tmp_path,phase='pending',remaining='继续下一步'):
    goal={'instruction':'当前阶段','conditions':[{'id':'target_1','window':{'title':'Explorer'},
        'selector':{'type':'TreeItem','name':'Documents','automation_id':''},'state':'selected','value':True,'retain':False}]}
    return {'root':tmp_path,'instruction':'完整新任务','stage_number':1,'task_root':tmp_path/'task','goal':goal,
        'summary':{'state':phase,'completed':['target_1'] if phase=='verified' else []},
        'remaining_instruction':remaining,'needs_acceptance':phase=='pending',
        'can_advance':phase=='verified' and bool(remaining),'completed_stages':[]}

def settle(root,p):
    until=time.monotonic()+3
    while p.running and time.monotonic()<until:
        root.update();time.sleep(.01)
    root.update()
    assert not p.running


def test_restore_office_backend_and_original_provider_remain_distinct(panel,tmp_path):
    from agent_service.windows_access import WinappBackend, WinappOfficeBackend
    from agent_service.windows_winapp import PROVIDER_BINDING, OFFICE_PROVIDER_BINDING
    panel.state={**view(tmp_path),'provider_binding':deepcopy(OFFICE_PROVIDER_BINDING)}
    assert type(panel.get_backend()) is WinappOfficeBackend
    panel.state['provider_binding']=deepcopy(PROVIDER_BINDING)
    assert type(panel.get_backend()) is WinappBackend
    panel.state['provider_binding']=deepcopy(OFFICE_PROVIDER_BINDING)
    assert type(panel.get_backend()) is WinappOfficeBackend


def test_readonly_close_recheck_preserves_unknown_and_enables_next(root,panel,tmp_path,monkeypatch):
    state=view(tmp_path,'unknown');state.update(can_recheck_close=True,close_reconciled=False)
    panel.state=state;panel.session_root=tmp_path;panel.render()
    assert str(panel.recheck_button['state'])=='normal'
    result=deepcopy(state);result.update(can_recheck_close=False,close_reconciled=True,can_advance=True,
        completed_stages=[{'number':1,'instruction':'关闭查找框','reconciled':True}])
    calls=[]
    def recheck(path,backend,*,stop):
        calls.append(path);return result
    monkeypatch.setattr(ui.workflow,'recheck_current_close',recheck)
    panel.recheck_close();settle(root,panel)
    assert calls==[tmp_path] and panel.state['summary']['state']=='unknown'
    assert str(panel.recheck_button['state'])==str(panel.execute_button['state'])=='disabled'
    assert str(panel.plan_button['state'])=='normal'
    assert '原关闭操作仍为未知' in panel.status.get()
    assert '原 unknown 保留' in panel.preview.get('1.0','end')


def test_collapsed_target_explains_expanded_state(tmp_path):
    condition=view(tmp_path)['goal']['conditions'][0]
    condition.update(state='collapsed',value=False)
    assert '收起（展开状态 = False）' in ui.target_text(condition)


def test_failed_stage_reports_cause_without_claiming_restoration(root,panel,tmp_path,monkeypatch):
    result=view(tmp_path,'failed')
    result['summary']['cause_code']='winapp_target_changed'
    panel.session_root=tmp_path;panel.state=result
    monkeypatch.setattr(ui.workflow,'execute',lambda *a,**kw:result)
    panel.execute();settle(root,panel)
    assert 'winapp_target_changed' in panel.status.get()
    assert '本阶段未通过' in panel.status.get()
    assert '已找回' not in panel.status.get()


@pytest.mark.parametrize('phase,enabled',[('paused',True),('unknown',False)])
def test_popup_handoff_only_enables_safe_explicit_continuation(root,panel,tmp_path,phase,enabled):
    state=view(tmp_path,phase);state['summary']['manual_attention']={'condition_id':'target_1','after_action':not enabled}
    panel.state=state;panel.session_root=tmp_path;panel.render()
    assert (str(panel.execute_button['state'])=='normal') is enabled
    assert '已停止自动操作与观察' in panel.preview.get('1.0','end')
    if enabled: assert '已处理弹窗' in panel.execute_button['text']
    else: assert '禁止直接重试' in panel.preview.get('1.0','end')


def test_legacy_unsealed_stage_does_not_offer_continue(panel,tmp_path):
    state=view(tmp_path,'failed');state['summary'].update(eligible=False,resumable=False,unsealed_effect=True)
    panel.state=state;panel.render()
    assert str(panel.execute_button['state'])=='disabled'


def test_restored_provider_selects_persisted_backend_and_legacy_switches_back(panel,tmp_path):
    from agent_service.windows_access import WinappBackend, WindowsBackend
    from agent_service.windows_winapp import PROVIDER_BINDING
    panel.state = dict(view(tmp_path), provider_binding=deepcopy(PROVIDER_BINDING))
    assert isinstance(panel.get_backend(), WinappBackend)
    panel.state = view(tmp_path)
    assert type(panel.get_backend()) is WindowsBackend

def test_pending_restored_does_not_create_client_or_execute(root,panel,tmp_path,monkeypatch):
    state=view(tmp_path);panel.session_root=tmp_path
    monkeypatch.setattr(ui.workflow,'load',lambda _:deepcopy(state))
    panel.client_factory=lambda _:pytest.fail('no model on pending restoration')
    panel.advance();settle(root,panel)
    assert panel.state==state
    assert str(panel.execute_button['state'])=='normal'
    assert str(panel.plan_button['state'])=='disabled'
    assert '尚未执行' in panel.status.get()

def test_planning_failure_keeps_created_path(root,panel,tmp_path,monkeypatch):
    state=view(tmp_path);state.update(stage_number=0,task_root=None,goal=None,summary=None,can_advance=True,needs_acceptance=False)
    monkeypatch.setattr(ui.workflow,'create',lambda *a,**kw:tmp_path)
    monkeypatch.setattr(ui.workflow,'load',lambda _:deepcopy(state))
    monkeypatch.setattr(ui.workflow,'advance',lambda *a,**kw: (_ for _ in ()).throw(ComputerError('model_budget_exceeded')))
    panel.client_factory=lambda _:object()
    panel.advance();settle(root,panel)
    assert panel.session_root==tmp_path and '预算不足' in panel.status.get()
    assert str(panel.execute_button['state'])=='disabled'

@pytest.mark.parametrize('phase,can_plan,can_run',[('pending',False,True),('paused',False,True),('unknown',False,False),('verified',True,False)])
def test_state_buttons(panel,tmp_path,phase,can_plan,can_run):
    panel.state=view(tmp_path,phase);panel.render()
    assert (str(panel.plan_button['state'])=='normal')==can_plan
    assert (str(panel.execute_button['state'])=='normal')==can_run

def test_empty_remainder_never_claims_overall_success(panel,tmp_path):
    panel.state=view(tmp_path,'verified','');panel.render()
    assert str(panel.plan_button['state'])=='disabled'
    assert '完整任务仍待你验收' in panel.preview.get('1.0','end')

def test_completed_stages_displayed_separately(panel,tmp_path):
    panel.state=view(tmp_path)
    panel.state['completed_stages']=[{'number':1,'instruction':'先前已完成'}]
    panel.render()
    assert '已核验阶段：\n1. 先前已完成' in panel.preview.get('1.0','end')

def test_only_execute_button_calls_approval(root,panel,tmp_path,monkeypatch):
    panel.state=view(tmp_path);panel.session_root=tmp_path
    calls=[]
    monkeypatch.setattr(ui.workflow,'execute',lambda r,b,**kw:(calls.append((r,kw['pause_after_step'])) or view(tmp_path,'verified')))
    panel.pause.set(True);panel.execute();settle(root,panel)
    assert calls==[(tmp_path,True)]
    assert panel.state['summary']['state']=='verified'

def test_external_busy_prevents_thread(panel,monkeypatch):
    panel.external_busy=lambda:True
    panel.start(lambda:pytest.fail('busy'))
    assert not panel.running

def test_close_waits_for_worker_and_saves_result(root,panel,tmp_path):
    import threading
    gate=threading.Event()
    panel.start(lambda:(gate.wait(2),view(tmp_path))[1])
    panel.close()
    assert panel.closing and not panel.closed and panel.stopped.is_set()
    gate.set();settle(root,panel)
    assert panel.closed

def test_main_entry_preserves_new_draft_and_blocks_overlap(root,tmp_path):
    frame=ttk.Frame(root);owner=WindowsPanel(frame,root,task_base=tmp_path)
    owner.instruction_var.set('原始任务');owner.approved_observations=[{'fixture':True}]
    owner.open_stages();child=owner.stage_panel
    try:
        owner.instruction_var.set('用户新写的下一任务')
        assert child.instruction=='原始任务'
        assert owner.instruction.get()=='用户新写的下一任务'
        child.running=True
        assert owner.external_busy()
        child.running=False
        assert not owner.external_busy()
    finally:
        child.close();owner.close();frame.destroy()

def test_bound_artifact_draft_is_not_silently_dropped(root,tmp_path):
    frame=ttk.Frame(root);owner=WindowsPanel(frame,root,task_base=tmp_path)
    owner.sources=[{'source':'keep'}];owner.open_stages()
    assert owner.stage_panel is None and owner.sources==[{'source':'keep'}]
    owner.close();frame.destroy()
