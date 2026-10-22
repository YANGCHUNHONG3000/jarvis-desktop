"""Isolated Tk goal-panel checks; no network or native actions."""
import tkinter as tk
from tkinter import ttk
import pytest
from agent_service.computer_goal_panel import attach_goal_tab


@pytest.fixture(scope='module')
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()
    import gc
    gc.collect()


@pytest.fixture
def panel(hidden_root):
    tabs = ttk.Notebook(hidden_root)
    changes = []
    panel = attach_goal_tab(tabs, hidden_root, on_busy=changes.append)
    yield panel, changes
    panel.close()
    tabs.destroy()


def test_idle_no_session_or_api(panel):
    p, changes = panel
    assert p.session is None and p.plan is None and not changes
    assert str(p.execute_button['state']) == 'disabled'
    p.preview()
    assert p.plan is None and '先读取' in p.status.get()


def test_busy_blocks_execution(panel):
    p, changes = panel
    p.external_busy = lambda: True
    p.start('observe', lambda _: pytest.fail('busy'))
    p.execute()
    assert not changes and p.session is None


def test_thread_failure_releases_busy(panel, monkeypatch):
    from agent_service import computer_goal_panel
    p, changes = panel
    class BrokenThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('fixture')
    monkeypatch.setattr(computer_goal_panel.threading, 'Thread', BrokenThread)
    p.start('observe', lambda _: None)
    assert changes == [True, False] and not p.running
    assert str(p.source['state']) == 'readonly'


def test_progress_does_not_release_busy(panel):
    p, changes = panel
    p.set_busy(True)
    p.messages.put(('progress', {'step':1,'total':2,'op':'calculate'}, None))
    p.poll()
    assert p.running and changes == [True]
    assert '1/2' in p.status.get()


def test_stop_discards_late_plan(panel):
    p, changes = panel
    p.set_busy(True)
    p.stop()
    p.messages.put(('propose', {'plan':{},'reason':'late'}, None))
    p.poll()
    assert p.plan is None and not p.running and changes == [True, False]


def test_partial_outcome_visible_after_stop(panel):
    p, _ = panel
    p.stop()
    p.messages.put(('execute', {'ok':False,'code':'user_takeover','completed':[{}],
                                'outputs':{'result':'5'},'run':'local-evidence'}, None))
    p.poll()
    assert 'user_takeover' in p.status.get() and '1 步' in p.status.get()
    assert p.plan is None and not p.snapshots


def test_foreground_grant_runs_in_ui_poll_without_releasing_busy(panel, monkeypatch):
    import threading
    from agent_service import computer_host
    p, changes = panel
    calls = []
    monkeypatch.setattr(computer_host, 'grant_foreground', lambda pid: calls.append(pid) or True)
    p.set_busy(True)
    event, answer = threading.Event(), {'allowed':False}
    p.messages.put(('foreground', (123, event, answer), None))
    p.poll()
    assert calls == [123] and event.is_set() and answer['allowed']
    assert p.running and changes == [True]


def test_stop_denies_queued_foreground_grant(panel, monkeypatch):
    import threading
    from agent_service import computer_host
    p, _ = panel
    monkeypatch.setattr(computer_host, 'grant_foreground', lambda pid: pytest.fail('stopped'))
    p.set_busy(True)
    p.stop()
    event, answer = threading.Event(), {'allowed':False}
    p.messages.put(('foreground', (123, event, answer), None))
    p.poll()
    assert event.is_set() and not answer['allowed']


def test_expired_grant_never_authorizes_reused_pid(panel, monkeypatch):
    import threading
    from agent_service import computer_host
    p, _ = panel
    monkeypatch.setattr(computer_host, 'grant_foreground', lambda pid: pytest.fail('expired'))
    p.set_busy(True)
    event, answer = threading.Event(), {'allowed':False}
    event.set()
    p.messages.put(('foreground', (123, event, answer), None))
    p.poll()
    assert not answer['allowed']


def test_adapter_preview_needs_no_windows_and_no_model(panel, tmp_path, monkeypatch):
    from agent_service.computer import ComputerSession
    from agent_service import computer_goal_panel as ui
    p, _ = panel
    session = ComputerSession.create(tmp_path / 'sessions')
    monkeypatch.setattr(ui.ComputerSession, 'create', lambda **kw: session)
    p.mode.set(ui.MODES[4])
    p.mode_changed()
    p.expression.insert(0, 'https://example.org/')
    p.text.insert('1.0', '公开资料摘录')
    p.text.edit_modified(False)
    p.preview()
    assert p.plan['mode'] == 'adapters' and p.plan['snapshots'] == {}
    assert '1200' in p.preview_box.get('1.0', 'end')
    assert str(p.source['state']) == 'disabled'
    p.propose()
    assert '不需要' in p.status.get() and not p.running


def test_adapter_result_handoff_on_ui_thread(panel):
    p, changes = panel
    records = [{'kind':'document','path':'verified.docx'}]
    calls = []
    p.on_artifacts = lambda value: calls.append((value, p.running))
    p.set_busy(True)
    p.messages.put(('execute', {'ok':True, 'mode':'adapters', 'completed':[{}, {}, {}],
                              'artifacts':records, 'run':'evidence'}, None))
    p.poll()
    assert calls == [(records, False)] and changes == [True, False]
    assert '未保存' not in p.preview_box.get('1.0', 'end')


def test_partial_adapter_keeps_completed_artifact_handoff(panel):
    p, _ = panel
    records = [{'kind':'spreadsheet','path':'verified.xlsx'}]
    calls = []
    p.on_artifacts = calls.append
    p.messages.put(('execute', {'ok':False, 'mode':'adapters','code':'office_unknown',
                      'completed':[{}, {}], 'artifacts':records,'run':'evidence'}, None))
    p.poll()
    assert calls == [records] and 'office_unknown' in p.status.get()


def test_adapter_handoff_failure_preserves_result(panel):
    p, _ = panel
    def fail(_): raise ValueError('changed')
    p.on_artifacts = fail
    p.messages.put(('execute', {'ok':True,'mode':'adapters','completed':[{}],
                      'artifacts':[{'kind':'document','path':'saved.docx'}],'run':'evidence'}, None))
    p.poll()
    assert '历史任务' in p.status.get() and 'saved.docx' in p.preview_box.get('1.0','end')


def test_search_preview_and_cloud_planning_have_explicit_scope(panel, tmp_path, monkeypatch):
    from agent_service.computer import ComputerSession
    from agent_service import computer_goal_panel as ui
    from agent_service import computer_goal_adapters as adapters
    p, _ = panel
    session = ComputerSession.create(tmp_path / 'sessions')
    monkeypatch.setattr(ui.ComputerSession, 'create', lambda **kw: session)
    p.mode.set(ui.MODES[5])
    p.mode_changed()
    p.text.insert('1.0', 'Python pathlib')
    p.text.edit_modified(False)
    p.preview()
    assert p.plan['steps'][0] == {'op':'public_search','query':'Python pathlib','max_sources':2,'as':'sources'}
    assert '搜索服务会接收' in p.preview_box.get('1.0','end')
    assert '至少读到一个正文' in p.preview_box.get('1.0','end')
    assert '仅上传任务描述' in str(p.propose_button['text'])
    assert not p.plan['snapshots'] and str(p.source['state']) == 'disabled'
    calls = []
    monkeypatch.setattr(adapters, 'propose_research_goal', lambda s, text, **kw: calls.append((s,text)) or {'plan':None,'reason':'unsupported'})
    monkeypatch.setattr(p, 'start', lambda kind, operation: operation(session))
    p.propose()
    assert calls == [(session, 'Python pathlib')] and p.plan is None


def test_search_result_discloses_unread_candidates(panel):
    p, _ = panel
    p.messages.put(('execute', {'ok':True, 'mode':'adapters', 'completed':[{}, {}, {}],
                              'artifacts':[], 'run':'evidence', 'source_reads':1,
                              'failures':[{'url':'https://example.org/', 'code':'browser_failed'}]}, None))
    p.poll()
    assert '已读取 1 个来源，1 个候选未读到正文' in p.status.get()
    assert '已在文档标明' in p.preview_box.get('1.0', 'end')


def test_agent_goal_preview_exposes_dynamic_effects_and_no_window(panel,tmp_path,monkeypatch):
    from agent_service.computer import ComputerSession
    from agent_service import computer_goal_panel as ui
    p,_=panel
    session=ComputerSession.create(tmp_path/'sessions')
    monkeypatch.setattr(ui.ComputerSession,'create',lambda **kw:session)
    p.mode.set(ui.MODES[6]); p.mode_changed()
    p.text.insert('1.0','Python pathlib'); p.text.edit_modified(False)
    p.preview()
    assert p.plan['mode']=='agent' and 'steps' not in p.plan
    preview=p.preview_box.get('1.0','end')
    assert '360秒' in preview and '6次模型' in preview and '有限公开摘录' in preview
    assert str(p.source['state'])=='disabled'


def test_agent_progress_uses_observation_decision_message(panel):
    p,_=panel
    p.messages.put(('progress',{'op':'agent_cycle','step':2,'total':12,'message':'第2轮：复核实际来源'},None))
    p.poll()
    assert '复核实际来源' in p.status.get() and '可停止' in p.status.get()

def test_dynamic_reobserve_progress_does_not_crash_or_release_busy(panel):
    p,changes=panel
    p.set_busy(True)
    p.messages.put(('progress',{'op':'agent_reobserve','step':2,'total':12,
        'message':'页面已变化，未执行旧动作；重新观察当前控件。'},None))
    p.poll()
    assert '重新观察' in p.status.get() and '可停止' in p.status.get()
    assert p.running and changes==[True]

def test_human_continue_requires_current_live_gate_and_keeps_busy(panel):
    import threading
    p,changes=panel
    p.set_busy(True)
    event,answer=threading.Event(),{'accepted':False}
    p.messages.put(('human',({'target_url':'https://github.com/example/project'},event,answer),None))
    p.poll()
    assert p.running and str(p.human_continue_button['state'])=='normal' and not event.is_set()
    assert '登录' in p.status.get()
    p.continue_human()
    assert event.is_set() and answer['accepted'] and changes==[True]
    p.messages.put(('human_finished',event,None));p.poll()
    assert p.human_pending is None and str(p.human_continue_button['state'])=='disabled'

def test_stop_revokes_human_continue_and_late_pending_gate(panel):
    import threading
    p,_=panel
    p.set_busy(True)
    event,answer=threading.Event(),{'accepted':False}
    p.messages.put(('human',({'target_url':'https://github.com/example/project'},event,answer),None))
    p.stop();p.poll();p.continue_human()
    assert event.is_set() and not answer['accepted'] and p.human_pending is None
    assert '不能继续旧任务' in p.status.get()


def test_human_timeout_explains_new_preview_without_reusing_claim(panel):
    import threading
    p, _ = panel
    p.set_busy(True)
    event, answer = threading.Event(), {'accepted': False}
    p.messages.put(('human', ({'target_url':'https://github.com/example/project'}, event, answer), None))
    p.poll()
    event.set()
    p.messages.put(('human_finished', event, None))
    p.messages.put(('execute', {'ok':False,'code':'browser_human_timeout',
        'mode':'adapters','engine':'observed_agent','goal_kind':'interactive',
        'completed':[],'artifacts':[],'run':'original-record'}, None))
    p.poll()
    assert not p.running and p.plan is None and p.human_pending is None
    assert str(p.execute_button['state']) == str(p.human_continue_button['state']) == 'disabled'
    assert '超过 15 分钟' in p.status.get() and 'original-record' in p.status.get()
    assert '本地预览目标' in p.preview_box.get('1.0','end')
    p.continue_human()
    assert not answer['accepted'] and '不能继续旧任务' in p.status.get()


def test_duplicate_continue_does_not_claim_live_resume_expired(panel):
    import threading
    p, _ = panel
    p.set_busy(True)
    event, answer = threading.Event(), {'accepted': False}
    p.messages.put(('human', ({'target_url':'https://github.com/example/project'}, event, answer), None))
    p.poll()
    assert '第 1/3 次人工接管' in p.preview_box.get('1.0','end')
    p.continue_human()
    p.continue_human()
    assert answer['accepted'] and '正在重新观察' in p.status.get() and p.running


def test_agent_failure_disclosure_distinguishes_search_from_unread_pages(panel):
    p,_=panel
    p.messages.put(('execute',{'ok':True,'engine':'observed_agent','mode':'adapters',
        'completed':[{}], 'artifacts':[], 'run':'record', 'source_reads':1,
        'failures':[{'kind':'search','code':'search_unavailable'}]},None))
    p.poll()
    assert '检索失败 1 次，候选失败 0 个' in p.status.get()
    assert '已在文档标明' not in p.status.get()


@pytest.mark.parametrize('mode,interactive', [('agent',False),('agent',True),('adapters',False)])
def test_pause_checkbox_is_local_and_captured_before_worker(panel,monkeypatch,mode,interactive):
    import threading
    from agent_service import computer_goal_panel as ui
    p,_=panel
    assert p.pause_each_step.get() is False
    p.pause_each_step.set(True)
    p.plan={'mode':mode,'sha256':'a'*64,'goal':{'kind':'interactive'} if interactive else {}}
    work=[];calls=[];errors=[]
    monkeypatch.setattr(p,'start',lambda label,operation: work.append(operation))
    def execute(*args,**kw):
        options=kw['agent_options']
        calls.append(options)
        if mode=='agent':
            assert options['pause_after_step']() is True
            assert ('human_handoff' in options)==interactive
        else:
            assert options is None
    monkeypatch.setattr(ui,'execute_goal',execute)
    p.execute()
    # A later UI change must not cause the background callback to touch Tk.
    p.pause_each_step.set(False)
    def worker():
        try: work[0](None)
        except BaseException as exc: errors.append(exc)
    thread=threading.Thread(target=worker);thread.start();thread.join()
    assert not errors and len(calls)==1
