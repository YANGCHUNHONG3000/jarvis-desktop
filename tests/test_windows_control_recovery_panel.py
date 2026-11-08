"""Real Tk draft/approval boundaries around independent Cancel readback."""
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import pytest
from agent_service.computer import ComputerError
from agent_service.windows_panel import WindowsPanel
from agent_service import windows_control_recovery as recovery

WINDOW={'hwnd':100,'pid':200,'created':'fixture','image':'C:/wps.exe','class_name':'XLMAIN','title':'fixture','profile':'observe_only'}
OBS={'window':WINDOW,'application':'wps','controls':[],'dialogs':[],'scope':'visible_controls','truncated':False}

@pytest.fixture(scope='module')
def root():
    value=tk.Tk();value.withdraw()
    yield value
    value.destroy()

@pytest.fixture
def panel(root,tmp_path,monkeypatch):
    frame=ttk.Frame(root);p=WindowsPanel(frame,root,task_base=tmp_path)
    monkeypatch.setattr(recovery,'inspect',lambda parent:{})
    monkeypatch.setattr(recovery,'latest',lambda parent:None)
    p.instruction_var.set('原任务指令')
    p.conditions=[{'id':'old'}]
    p.task_root=tmp_path/'original'
    p.configure_cancel_recheck({'state':'unknown'})
    p.cancel_recheck_receipt=tmp_path/'receipt'
    p.control_recheck_buttons()
    monkeypatch.setattr(recovery,'verified_window',lambda folder:deepcopy(WINDOW))
    yield p
    p.close();frame.destroy()

class Backend:
    def __init__(self,value=None):self.reads=0;self.value=value or deepcopy(OBS)
    def observe(self,window,*,stop):
        self.reads+=1
        assert window==WINDOW
        return deepcopy(self.value)
    def act(self,*args,**kwargs):raise AssertionError('No input permission')

def run_next(panel,monkeypatch,backend=None):
    backend=backend or Backend()
    monkeypatch.setattr(panel,'start',lambda kind,op:panel.messages.put((kind,op(backend),None)))
    panel.next_after_cancel();panel.poll()
    return backend

def test_unchanged_old_instruction_clears_without_carrying_approval(panel,monkeypatch):
    backend=run_next(panel,monkeypatch)
    assert backend.reads==1 and panel.instruction.get()=='' and panel.conditions==[]
    assert panel.task_root is None and panel.proposal_task is None and panel.sources==[]
    assert panel.approved_observations==[OBS]
    assert str(panel.execute_button['state'])=='disabled'
    assert str(panel.resume_button['state'])=='disabled'

def test_user_new_instruction_survives_next_context(panel,monkeypatch):
    panel.instruction_var.set('我新写的任务')
    run_next(panel,monkeypatch)
    assert panel.instruction.get()=='我新写的任务' and panel.conditions==[]

def test_new_targets_are_not_cleared_or_observed(panel,monkeypatch):
    panel.conditions=[{'id':'new'}]
    monkeypatch.setattr(panel,'start',lambda *args:pytest.fail('must not start'))
    panel.next_after_cancel()
    assert panel.conditions==[{'id':'new'}]

def test_late_readback_does_not_overwrite_new_draft(panel,monkeypatch):
    pending=[];backend=Backend()
    monkeypatch.setattr(panel,'start',lambda kind,op:pending.append((kind,op)))
    panel.next_after_cancel()
    panel.instruction_var.set('读取期间新写')
    kind,op=pending.pop();panel.messages.put((kind,op(backend),None));panel.poll()
    assert panel.instruction.get()=='读取期间新写' and panel.conditions==[{'id':'old'}]
    assert panel.approved_observations==[]

@pytest.mark.parametrize('change',[{'blocked_reason':'popup'},{'truncated':True},{'window':{**WINDOW,'hwnd':101}}])
def test_bad_fresh_context_preserves_draft_and_targets(panel,monkeypatch,change):
    before=(panel.instruction.get(),deepcopy(panel.conditions))
    with pytest.raises(ComputerError,match='next_observation_invalid'):
        run_next(panel,monkeypatch,Backend({**OBS,**change}))
    assert (panel.instruction.get(),panel.conditions)==before

@pytest.mark.parametrize('missing',['application','truncated','scope','dialogs'])
def test_incomplete_fresh_context_never_clears_old_draft(panel,monkeypatch,missing):
    observed=deepcopy(OBS);observed.pop(missing)
    before=(panel.instruction.get(),deepcopy(panel.conditions))
    with pytest.raises(ComputerError,match='next_observation_invalid'):
        run_next(panel,monkeypatch,Backend(observed))
    assert (panel.instruction.get(),panel.conditions)==before

def test_receipt_drift_after_observation_blocks_followup(panel,monkeypatch):
    calls=[]
    def window(folder):
        calls.append(folder)
        if len(calls)>1:raise ComputerError('windows_control_recheck_source_changed')
        return deepcopy(WINDOW)
    monkeypatch.setattr(recovery,'verified_window',window)
    with pytest.raises(ComputerError,match='source_changed'):run_next(panel,monkeypatch)
    assert panel.conditions==[{'id':'old'}] and panel.instruction.get()=='原任务指令'

def test_historical_receipt_reload_never_rereads_native_ui(panel,monkeypatch):
    receipt={'ok':True,'root':str(panel.cancel_recheck_receipt),'historical':True}
    monkeypatch.setattr(recovery,'latest',lambda parent:receipt)
    monkeypatch.setattr(recovery,'run',lambda *a,**k:pytest.fail('must not repeat native read'))
    monkeypatch.setattr(panel,'start',lambda kind,op:panel.messages.put((kind,op(object()),None)))
    panel.recheck_cancel();panel.poll()
    assert panel.cancel_recheck_receipt==Path(receipt['root'])
    assert str(panel.resume_button['state'])=='disabled'

def test_injected_ui_backend_not_forwarded_to_recovery(panel,monkeypatch):
    seen=[]
    def run(parent,**kwargs):
        assert set(kwargs)=={'stop'}
        seen.append(parent)
        return {'ok':True,'root':str(panel.cancel_recheck_receipt)}
    monkeypatch.setattr(recovery,'run',run)
    monkeypatch.setattr(panel,'start',lambda kind,op:panel.messages.put((kind,op(object()),None)))
    panel.recheck_cancel();panel.poll()
    assert seen==[panel.cancel_recheck_context['parent']]

def test_cached_receipt_configuration_requires_eligible_parent(panel,monkeypatch):
    monkeypatch.setattr(recovery,'inspect',lambda parent:(_ for _ in ()).throw(ComputerError('ineligible')))
    panel.configure_cancel_recheck({'state':'unknown'})
    assert panel.cancel_recheck_context is None and panel.cancel_recheck_receipt is None
    assert str(panel.cancel_next_button['state'])=='disabled'
