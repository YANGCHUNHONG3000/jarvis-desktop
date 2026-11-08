from types import SimpleNamespace
import threading
import pytest
from agent_service import office_edit_adapter_panel as panel, computer_goal_panel as main
from test_computer_goal_panel import hidden_root, panel as goal_panel
from test_instruction_draft_panel import DeferredThread


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


@pytest.fixture
def draft_recovery(goal_panel, monkeypatch, tmp_path):
    from copy import deepcopy
    from agent_service.computer import digest, write_new
    p, _ = goal_panel
    monkeypatch.setattr(main, 'describe', lambda value: 'local diff')
    DeferredThread.pending = []
    monkeypatch.setattr(main.threading, 'Thread', DeferredThread)
    session_root = tmp_path / 'session'
    (session_root / 'goals').mkdir(parents=True)
    write_new(session_root / 'session.json', {'schema': 1})
    p.session = SimpleNamespace(root=session_root)
    ticket = p.instruction_draft.adopt('已批准的四处修改')
    p.text.edit_modified(False)
    original = plan()
    original['instruction'] = ticket['text']
    original['sha256'] = digest({k: v for k, v in original.items() if k != 'sha256'})
    write_new(session_root / 'goals' / (original['sha256'] + '.json'), original)
    p.show_plan(original, instruction_ticket=ticket)
    assert panel.capture_instruction(p, p.session, original, ticket) == ticket
    child = deepcopy(original)
    child['recovery'] = {'parent_run': str(session_root / 'goals' / (original['sha256'] + '.run'))}
    child['sha256'] = digest({k: v for k, v in child.items() if k != 'sha256'})
    write_new(session_root / 'goals' / (child['sha256'] + '.json'), child)
    monkeypatch.setattr(panel.adapter, 'current_candidate', lambda: 'b' * 64)
    monkeypatch.setattr(panel.adapter, 'owns_recovery', lambda *a: {'candidate': 'b' * 64})
    outcome = {'ok': True, 'state': 'verified', 'code': 'objective_verified',
               'engine': 'observed_agent', 'goal_kind': 'interactive', 'mode': 'adapters',
               'completed': [], 'artifacts': [], 'failures': [], 'outputs': {}, 'run': 'local-fixture'}
    monkeypatch.setattr(panel.adapter, 'resume', lambda *a, **k: {'outcome': deepcopy(outcome)})
    seen = []
    p.on_instruction_complete = seen.append
    return p, ticket, original, child, outcome, seen


def install_continuation(p, child):
    binding = panel._continued_instruction(p.session, child, p._office_edit_instruction)
    assert binding is not None
    p._office_edit_instruction = binding
    p.show_plan(child, instruction_ticket=binding['ticket'])
    # The generic recovery path still cannot claim a current same-text draft.
    assert p.instruction_ticket(child) is None


def test_bound_recovery_main_execute_consumes_original_revision(draft_recovery):
    p, ticket, original, child, outcome, seen = draft_recovery
    install_continuation(p, child)
    p.execute()
    assert p.plan is None and p.instruction_draft.unchanged(ticket)
    DeferredThread.finish()
    p.poll()
    assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]


@pytest.mark.parametrize('when', ['before_execute', 'during_execute'])
@pytest.mark.parametrize('same_text', [False, True])
def test_recovery_preserves_new_or_retyped_draft(draft_recovery, when, same_text):
    p, ticket, original, child, outcome, seen = draft_recovery
    install_continuation(p, child)
    if when == 'during_execute':
        p.execute()
    p.text.replace('1.0', 'end-1c', '下一条新稿')
    if same_text:
        p.text.replace('1.0', 'end-1c', ticket['text'])
    newer = p.instruction_draft.snapshot()
    p.text.edit_modified(False)
    if when == 'before_execute':
        p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(newer)


@pytest.mark.parametrize('change', ['parent_run', 'goal', 'instruction', 'session', 'saved_plan'])
def test_continuation_needs_exact_parent_plan_and_session(draft_recovery, change, tmp_path):
    from copy import deepcopy
    import json
    p, ticket, original, child, outcome, seen = draft_recovery
    binding = deepcopy(p._office_edit_instruction)
    if change == 'parent_run':
        child['recovery']['parent_run'] = str(tmp_path / 'other.run')
    elif change == 'goal':
        child['goal']['edits'].append({'id': 'foreign'})
    elif change == 'instruction':
        child['instruction'] = 'unrelated task'
    elif change == 'session':
        binding['session_root'] = str(tmp_path / 'other-session')
    else:
        saved = p.session.root / 'goals' / (original['sha256'] + '.json')
        altered = dict(original, instruction='changed durable plan')
        saved.write_text(json.dumps(altered), encoding='utf-8')
    assert panel._continued_instruction(p.session, child, binding) is None


@pytest.mark.parametrize('change', ['none', 'executed', 'saved_changed', 'parent_changed'])
def test_repeated_preview_only_keeps_exact_unstarted_sibling(draft_recovery, change):
    from copy import deepcopy
    from agent_service.computer import digest
    import json
    p, ticket, original, child, outcome, seen = draft_recovery
    install_continuation(p, child)
    sibling = deepcopy(child)
    sibling['prepared_at'] = 2
    sibling['sha256'] = digest({k: v for k, v in sibling.items() if k != 'sha256'})
    if change == 'executed':
        (p.session.root / 'goals' / (child['sha256'] + '.run')).mkdir()
    elif change == 'saved_changed':
        saved = p.session.root / 'goals' / (child['sha256'] + '.json')
        saved.write_text(json.dumps(dict(child, instruction='altered')), encoding='utf-8')
    elif change == 'parent_changed':
        sibling['recovery']['parent_run'] = str(p.session.root / 'goals' / ('f' * 64 + '.run'))
    continued = panel._continued_instruction(p.session, sibling, p._office_edit_instruction)
    if change == 'none':
        assert continued is not None and continued['ticket'] == ticket
        assert continued['plan'] == sibling
    else:
        assert continued is None


@pytest.mark.parametrize('success', [False, True])
def test_recovery_pause_or_unknown_never_consumes_instruction(draft_recovery, success):
    p, ticket, original, child, outcome, seen = draft_recovery
    install_continuation(p, child)
    outcome.update(ok=success, state='unknown' if not success else 'paused',
                   code='office_paused_after_verified_step')
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


@pytest.mark.parametrize('stale', ['none', 'plan', 'revision'])
def test_resume_button_preserves_lineage_across_paused_trial(draft_recovery, monkeypatch, tmp_path, stale):
    from copy import deepcopy
    import tkinter
    from tkinter import ttk
    from agent_service.computer import digest, write_new
    p, ticket, original, child, outcome, seen = draft_recovery
    store = tmp_path / 'adapters'
    candidate_root = store / ('b' * 64)
    candidate_root.mkdir(parents=True)
    write_new(candidate_root / 'trial-intent.json',
              {'session_root': str(p.session.root), 'plan_sha256': original['sha256']})
    monkeypatch.setattr(panel.adapter, 'DEFAULT_STORE', store)
    monkeypatch.setattr(panel.adapter, 'status', lambda *a: state(0))
    monkeypatch.setattr(panel.adapter, 'ensure_candidate', lambda *a, **k: state(0))
    monkeypatch.setattr(panel.adapter, 'reconcile', lambda *a: None)
    sibling = deepcopy(child)
    sibling['prepared_at'] = 2
    sibling['sha256'] = digest({k: v for k, v in sibling.items() if k != 'sha256'})
    write_new(p.session.root / 'goals' / (sibling['sha256'] + '.json'), sibling)
    previews = iter([child, sibling])
    monkeypatch.setattr(panel.adapter, 'prepare_resume', lambda *a: {'plan': next(previews)})
    paused = dict(outcome, ok=False, state='cancelled', code='office_paused_after_verified_step')
    monkeypatch.setattr(panel.adapter, 'trial', lambda *a, **k: {'outcome': paused})
    buttons = []

    class Widget:
        def __init__(self, *a, **k):
            if 'command' in k:
                buttons.append(k)
        def pack(self, **k): pass
        def title(self, *a): pass
        def destroy(self): pass

    monkeypatch.setattr(tkinter, 'Toplevel', Widget)
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    p._office_edit_instruction = None
    panel.open_panel(p)
    next(b for b in buttons if b['text'] == '批准当前差异并试运行')['command']()
    DeferredThread.finish()
    p.poll()
    assert p.plan is None and p.instruction_draft.unchanged(ticket) and seen == []
    assert p._office_edit_instruction['plan'] == original
    buttons.clear()
    panel.open_panel(p)
    if stale != 'none':
        if stale == 'plan':
            p.plan = dict(original, sha256='f' * 64)
        else:
            p.text.replace('1.0', 'end-1c', 'new draft')
            p.text.replace('1.0', 'end-1c', ticket['text'])
        newer = p.instruction_draft.snapshot()
        next(b for b in buttons if b['text'] == '预览未完成步骤')['command']()
        assert not DeferredThread.pending and p.instruction_draft.unchanged(newer)
        assert '旧试运行窗口已失效' in p.status.get()
        return
    next(b for b in buttons if b['text'] == '预览未完成步骤')['command']()
    DeferredThread.finish()
    p.poll()
    assert p.plan == child and p.instruction_draft.unchanged(ticket)
    assert p._office_edit_instruction['plan'] == child
    buttons.clear()
    panel.open_panel(p)
    next(b for b in buttons if b['text'] == '预览未完成步骤')['command']()
    DeferredThread.finish()
    p.poll()
    assert p.plan == sibling and p._office_edit_instruction['plan'] == sibling
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]
