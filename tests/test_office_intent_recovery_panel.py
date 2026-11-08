"""Real Tk draft transitions with offline planning; no external actions or API."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from pathlib import Path

import pytest

from agent_service import office_clarification as core, office_intent_recovery_panel as ui
from agent_service.computer import ComputerSession, ComputerError
from agent_service.computer_goal_panel import GoalPanel, MODES
from test_office_intent import Client, valid_response
from test_office_edit import word_fixture, sheet_fixture


@pytest.fixture(scope='module')
def tk_root():
    root=tk.Tk(); root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(tmp_path, monkeypatch, tk_root):
    root=tk_root
    frame=ttk.Frame(root)
    g=GoalPanel(frame,root)
    g.session=ComputerSession.create(tmp_path/'sessions')
    files=[tmp_path/'brief.docx',tmp_path/'budget.xlsx']
    word_fixture(files[0]); sheet_fixture(files[1])
    g.mode.set(MODES[9]); g.mode_changed()
    g.office_planning_files=tuple(map(str,files))
    g.instruction_draft.adopt('更新那个金额')
    g.text.edit_modified(False)
    result=core.begin(g.session,'更新那个金额',files,client=Client(
        lambda _: {'kind':'clarify','issue':'ambiguous','reason':'请明确哪个金额和新值。'}))
    c=g.intent_recovery
    item=core.load(g.session,result)
    c.active=item; c.binding=c.context()
    jobs=[]
    def start(kind,operation,ticket):
        jobs.append((kind,operation,ticket))
    g.start_instruction=start
    dialogs=[]
    monkeypatch.setattr(ui,'open_question',lambda *args: dialogs.append(args) or True)
    yield g,c,item,jobs,dialogs
    g.close(); frame.destroy()


def test_confirm_retains_original_session_and_snapshots(panel,monkeypatch):
    g,c,item,jobs,dialogs=panel
    original=core.continue_attempt
    monkeypatch.setattr(core,'continue_attempt',lambda *args,**kwargs:original(*args,**kwargs,client=Client()))
    roots={p.name for p in (g.session.root/'edit-inputs').iterdir()}
    session=g.session
    assert c.submit(item,c.binding,'把预算表纸张金额改为15，并确认Word预算')
    assert len(jobs)==1 and jobs[0][0]=='propose'
    assert jobs[0][2]['text'].startswith('更新那个金额；用户补充：')
    result=jobs[0][1](g.session)
    assert c.accepts(result)
    g.messages.put(('propose',result,None,jobs[0][2])); g.poll()
    assert g.plan and g.plan['instruction']==jobs[0][2]['text']
    assert g.session is session and c.active is None
    assert roots=={p.name for p in (g.session.root/'edit-inputs').iterdir()}
    assert not list(g.session.root.glob('goals/*.run'))


@pytest.mark.parametrize('change',['draft','revert','files','scope','refs','mode','session','plan','stop','busy','closed'])
def test_modal_answer_cannot_overwrite_changed_context(panel,change):
    g,c,item,jobs,_=panel
    context=deepcopy(c.binding)
    if change=='draft':g.instruction_draft.adopt('新草稿')
    elif change=='revert':g.instruction_draft.adopt('新稿');g.instruction_draft.adopt(item['instruction'])
    elif change=='files':g.office_planning_files=()
    elif change=='scope':g.office_scope=[]
    elif change=='refs':g.office_artifact_refs=[]
    elif change=='mode':g.mode.set(MODES[0])
    elif change=='session':g.session=ComputerSession.create(g.session.root.parent)
    elif change=='plan':g.plan={'other':True}
    elif change=='stop':g.stopped.set()
    elif change=='busy':g.running=True
    elif change=='closed':g.closed=True
    before=g.instruction_draft.snapshot()
    assert not c.submit(item,context,'只改纸张金额为15')
    assert before==g.instruction_draft.snapshot() and jobs==[]
    g.running=g.closed=False


def test_repeated_click_does_not_submit_twice(panel):
    _,c,item,jobs,_=panel
    context=deepcopy(c.binding)
    assert c.submit(item,context,'纸张金额改为15')
    assert not c.submit(item,context,'纸张金额改为15')
    assert len(jobs)==1


def test_invalid_answer_or_range_retains_pending_and_exact_draft(panel):
    g,c,item,jobs,_=panel
    before=g.instruction_draft.snapshot()
    assert not c.submit(item,c.binding,'')
    assert not c.submit(item,c.binding,'有效说明',{'files':(),'artifact_refs':None,'scope':[]})
    assert before==g.instruction_draft.snapshot() and c.active==item and jobs==[]


def test_cloud_button_reopens_pending_without_new_call(panel):
    _,c,_,jobs,dialogs=panel
    c.propose()
    assert len(dialogs)==1 and jobs==[]


def test_restore_keeps_same_session_and_displays_original_question(panel):
    g,c,item,_,dialogs=panel
    session=str(g.session.root)
    g.instruction_draft.adopt(''); g.office_planning_files=()
    g.plan=None; g.session=ComputerSession.create(g.session.root.parent)
    context=c.context()
    assert c.handle('office_pending_restore',{'item':item,'session':session,'context':context},None)
    assert str(g.session.root)==session and g.text.get('1.0','end-1c')==item['instruction']
    assert tuple(item['files'])==g.office_planning_files and dialogs
    assert g.plan is None and str(g.execute_button['state'])=='disabled'


def test_late_restore_does_not_replace_new_draft(panel):
    g,c,item,_,dialogs=panel
    context=c.context(); g.instruction_draft.adopt('以后再做的新稿')
    assert c.handle('office_pending_restore',{'item':item,'session':str(g.session.root),'context':context},None)
    assert g.text.get('1.0','end-1c')=='以后再做的新稿' and dialogs==[]


def test_late_model_result_is_not_executable(panel):
    g,c,item,_,_=panel
    context=c.context()
    result={**item['result'],'_office_continuation':item,'_intent_context':context,
            '_intent_session':str(g.session.root)}
    g.instruction_draft.adopt('新稿')
    g.messages.put(('propose',result,None));g.poll()
    assert g.plan is None and g.text.get('1.0','end-1c')=='新稿'


@pytest.mark.parametrize('code,word',[
    ('office_planning_source_changed','原文件'),('model_budget_exceeded','预算'),
    ('session_model_call_limit','调用次数'),('office_clarification_already_claimed_or_resolved','已提交'),
    ('model_network_failed','未知调用')])
def test_failure_explains_recovery_boundary(panel,code,word):
    g,c,_,_,_=panel
    c.failure(code)
    assert word in g.status.get()
