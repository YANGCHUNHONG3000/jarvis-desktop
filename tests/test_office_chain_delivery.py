"""Delivery selection preserves drafts and revalidates before next-task use."""
from copy import deepcopy
from pathlib import Path
import ast

import pytest

from agent_service import office_chain_delivery as module
from test_office_chain_drafts_panel import prepared, main_function
from test_office_task_chain_panel import finish, mutate


def setup(tmp_path, monkeypatch):
    panel, context, log, state, draft, calls, opened = prepared(tmp_path, monkeypatch)
    office = deepcopy(state['payload']['records'][0])
    second = {'path': str(tmp_path/'second.xlsx'), 'sha256': 'c'*64}
    state.update(status='complete', index=2,
                 completed_office=[{'index': 0, 'records': [office]}, {'index': 1, 'records': [second]}])
    state['payload'] = {'draft': '', 'records': [second], 'source': None}
    panel.active = deepcopy(state)
    service = module.OfficeChainDelivery(panel, open_result=lambda rec: opened.append(rec) or True)
    return service, panel, context, log, state, draft, office, second, calls, opened


def test_unified_records_keep_every_completed_step_and_unsent_draft(tmp_path, monkeypatch):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    assert module.delivery_records(s)==[(0,office),(0,draft),(1,second)]
    dialogs=[]
    monkeypatch.setattr(d,'_dialog',lambda state:dialogs.append(state))
    assert d.show(); finish(p)
    assert dialogs==[s] and calls==[s['id']] and not l.applied and not l.prepared


@pytest.mark.parametrize('single',[True,False])
def test_selected_earlier_office_outputs_keep_new_instruction_without_execution(tmp_path,monkeypatch,single):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    c['workspace']['payload']['draft']='我的下一任务新稿'
    selected=[(0,office)] if single else [(0,office),(1,second)]
    seen=[]
    def verify(payload,base):
        seen.append(deepcopy(payload))
        return {'payload':deepcopy(payload),'refs':[], 'source_ref':None}
    monkeypatch.setattr(module.workspace_state,'verify',verify)
    assert d.use_office(s['id'],selected); finish(p)
    expected={'draft':'我的下一任务新稿','records':[x[1] for x in selected],
              'source':office if single else None}
    assert expected in seen and l.applied[-1]['payload']==expected
    assert c['workspace']['payload']==expected and calls==[s['id']]
    assert l.scopes[-1]==('当前选中资料' if single else '当前成果批次')
    assert p.issued is None and p.route_context is None and not l.prepared and not l.advanced


@pytest.mark.parametrize('case',['draft','mixed','empty','duplicate','too_many','foreign','wrong_step','bool_step'])
def test_invalid_office_selection_does_not_read_or_silently_drop_items(tmp_path,monkeypatch,case):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    cases={'draft':[(0,draft)],'mixed':[(0,office),(0,draft)],'empty':[],
           'duplicate':[(0,office)]*2,'too_many':[(0,office)]*5,
           'foreign':[(0,{'path':'other.docx','sha256':'a'*64})],
           'wrong_step':[(1,office)],'bool_step':[(False,office)]}
    assert not d.use_office(s['id'],cases[case])
    assert not calls and not l.applied and not l.prepared


@pytest.mark.parametrize('change',['main_new','main_revert','source','source_revert','records','selected',
                                  'goal_new','plan','mode','stop','scope'])
def test_changed_inputs_discard_late_selection(tmp_path,monkeypatch,change):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    assert d.use_office(s['id'],[(0,office)])
    mutate(c,change); before=deepcopy(c); finish(p)
    assert c==before and not l.applied and not l.prepared


@pytest.mark.parametrize('case',['unfinished','goal_draft','goal_plan','unavailable','foreign_chain'])
def test_pending_chain_or_continuous_work_is_preserved(tmp_path,monkeypatch,case):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    if case=='unfinished': p.active['status']='ready'
    elif case=='goal_draft': c['goal']['draft']['text']='已有草稿'
    elif case=='goal_plan': c['goal']['plan']={'pending':True}
    elif case=='unavailable': c['workspace']['unavailable']=True
    ident='f'*32 if case=='foreign_chain' else s['id']
    before=deepcopy(c)
    assert not d.use_office(ident,[(0,office)])
    assert c==before and not calls and not l.applied


def test_membership_change_rejects_and_retry_performs_fresh_validation(tmp_path,monkeypatch):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    assert d.use_office(s['id'],[(0,office)])
    s['completed_office'][0]['records']=[]; finish(p)
    assert not l.applied and p.failed_verification is not None
    assert '一致性' in l.statuses[-1]
    s['completed_office'][0]['records']=[office]
    assert p.retry_verification(); finish(p)
    assert calls==[s['id']]*2 and len(l.applied)==1 and not l.prepared


def test_office_verification_error_retries_without_adopting_unverified_files(tmp_path,monkeypatch):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    original=module.workspace_state.verify
    monkeypatch.setattr(module.workspace_state,'verify',lambda *_:(_ for _ in ()).throw(PermissionError('private')))
    assert d.use_office(s['id'],[(0,office)]); finish(p)
    assert not l.applied and 'private' not in l.statuses[-1]
    monkeypatch.setattr(module.workspace_state,'verify',original)
    assert p.retry_verification(); finish(p)
    assert len(l.applied)==1 and len(calls)==2


@pytest.mark.parametrize('kind',['office','draft'])
def test_open_rechecks_membership_without_changing_input(tmp_path,monkeypatch,kind):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    selected=office if kind=='office' else draft; before=deepcopy(c)
    assert d.open(s['id'],0,selected); finish(p)
    assert opened==[selected] and calls==[s['id']] and c==before and not l.applied


@pytest.mark.parametrize('verified,opened,expected',[(None,True,False),('x.exe',True,False),
    ('x.docx',True,True),('x.xlsx',False,False),('x.eml',True,True)])
def test_main_open_only_allows_exact_verified_supported_files(verified,opened,expected):
    class Status:
        def set(self,*a): pass
    env={'TASK_ROOT':Path('.'),'task_status':Status(),
         '_verified_history_path':lambda *_:Path(verified) if verified else None,
         'office_chain_status':lambda *_:None,'open_artifact':lambda *a,**k:opened}
    callback=main_function('office_chain_open_result',env)
    assert callback({'path':'x','sha256':'a'*64}) is expected


def test_main_unified_button_uses_delivery_service():
    tree=ast.parse((Path(__file__).parents[1]/'run_jarvis_assistant.py').read_text('utf-8'))
    buttons=[n for n in ast.walk(tree) if isinstance(n,ast.Call)
             and any(k.arg=='text' and isinstance(k.value,ast.Constant)
                     and k.value.value=='查看清单成果' for k in n.keywords)]
    assert len(buttons)==1
    assert any(k.arg=='command' and ast.unparse(k.value)=='chain_delivery.show' for k in buttons[0].keywords)


def dialog_widgets(monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    buttons, widgets = {}, []
    class Widget:
        def __init__(self,*args,**kwargs):
            self.selected=[]; self.rows=[]; self.destroyed=False
            self.bindings={}; self.focused=''
            widgets.append(self)
            if 'command' in kwargs: buttons[kwargs['text']]=kwargs['command']
        def pack(self,**kwargs): return None
        def title(self,*args): pass
        def geometry(self,*args): pass
        def heading(self,*args,**kwargs): pass
        def column(self,*args,**kwargs): pass
        def insert(self,*args,**kwargs): self.rows.append(kwargs)
        def selection_set(self,*items):
            self.selected=list(items[0]) if len(items)==1 and isinstance(items[0],(list,tuple)) else list(items)
        def selection(self): return self.selected
        def bind(self,sequence,callback,add=None):
            assert add=='+'
            self.bindings[sequence]=callback
        def identify_row(self,y): return y
        def get_children(self,parent=''):
            return tuple(row['iid'] for row in self.rows) if parent=='' else ()
        def item(self,node,option):
            assert option=='open'
            return False
        def focus(self,node=None):
            if node is not None: self.focused=node
            return self.focused
        def destroy(self): self.destroyed=True
    monkeypatch.setattr(tk,'Toplevel',Widget)
    for name in ['Label','Treeview','Frame','Button']: monkeypatch.setattr(ttk,name,Widget)
    return buttons,widgets


@pytest.mark.parametrize('change',['none','generation','cancel','different_chain'])
def test_dialog_selection_and_expiry(tmp_path,monkeypatch,change):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    buttons,widgets=dialog_widgets(monkeypatch)
    assert d._dialog(s)
    tree=next(w for w in widgets if w.rows)
    assert tree.selected==['2'] and len(tree.rows)==3
    tree.selection_set(['0','2'])
    c['workspace']['payload']['draft']='打开窗口后新写的指令'
    if change=='generation': p.generation+=1
    elif change=='cancel': p.cancel()
    elif change=='different_chain': p.active['id']='e'*32
    buttons['所选办公文件用于下一任务']()
    if change=='none':
        finish(p)
        assert l.applied[-1]['payload']['draft']=='打开窗口后新写的指令'
        assert l.applied[-1]['payload']['records']==[office,second]
        assert widgets[0].destroyed
    else:
        assert not calls and not l.applied and not widgets[0].destroyed


def test_dialog_real_shift_bindings_extend_and_shrink_from_click_anchor(tmp_path,monkeypatch):
    from types import SimpleNamespace
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    buttons,widgets=dialog_widgets(monkeypatch)
    assert d._dialog(s)
    tree=next(w for w in widgets if w.rows)
    assert set(tree.bindings)=={'<Button-1>','<Shift-Up>','<Shift-Down>','<Shift-Home>','<Shift-End>'}
    # Invoke the real shared helper's callbacks installed by the delivery dialog.
    tree.selection_set('0'); tree.focus('0')
    tree.bindings['<Button-1>'](SimpleNamespace(state=0,y='0'))
    assert tree.bindings['<Shift-Down>'](None)=='break'
    assert tree.selected==['0','1'] and tree.focus()=='1'
    assert tree.bindings['<Shift-Down>'](None)=='break'
    assert tree.selected==['0','1','2'] and tree.focus()=='2'
    assert tree.bindings['<Shift-Up>'](None)=='break'
    assert tree.selected==['0','1'] and tree.focus()=='1'
    # A plain click resets the anchor; Ctrl and Shift clicks preserve it.
    tree.bindings['<Button-1>'](SimpleNamespace(state=0,y='2'))
    tree.selection_set('2'); tree.focus('2')
    tree.bindings['<Button-1>'](SimpleNamespace(state=0x0004,y='0'))
    tree.bindings['<Button-1>'](SimpleNamespace(state=0x0001,y='0'))
    assert tree.bindings['<Shift-Up>'](None)=='break'
    assert tree.selected==['1','2'] and tree.focus()=='1'
    assert tree.bindings['<Shift-Home>'](None)=='break'
    assert tree.selected==['0','1','2'] and tree.focus()=='0'
    assert tree.bindings['<Shift-End>'](None)=='break'
    assert tree.selected==['2'] and tree.focus()=='2'
    assert not calls and not l.applied and not opened


def test_selection_or_open_changed_after_worker_is_not_delivered(tmp_path,monkeypatch):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    assert d.open(s['id'],0,office)
    s['completed_office'][0]['records']=[]; finish(p)
    assert not opened and not l.applied


def test_failed_adoption_keeps_same_selection_for_fresh_retry(tmp_path,monkeypatch):
    d,p,c,l,s,draft,office,second,calls,opened=setup(tmp_path,monkeypatch)
    apply=p.apply; p.apply=lambda verified:False
    assert d.use_office(s['id'],[(0,office)]); finish(p)
    assert p.failed_verification is not None and not l.applied
    p.apply=apply
    assert p.retry_verification(); finish(p)
    assert calls==[s['id']]*2 and l.applied[0]['payload']['records']==[office]
