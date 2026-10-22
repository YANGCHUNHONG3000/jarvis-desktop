"""Local human gate and shared-loop integration; no network or model calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import pytest
from agent_service.browser_handoff import ActiveClock, HumanHandoff
from agent_service.computer import ComputerError, ComputerSession, digest
from agent_service import computer_agent as engine, computer_interaction as interaction

CONFIG = {'mode':'github_repo','start_url':'https://github.com/example/project',
          'query':'project user:example','required_text':['project'],'human_login':True}

class Browser:
    def __init__(self, config=CONFIG, **kw):
        self.needs = True
        self.phase = 0
        self.manual = self.closed = False
        self.effects, self.deadlines = [], []
        self.entered = self.resumed = 0
    def observe(self):
        value = {'locked':False,'checks':{'goal_met':self.phase==2},'text':'private project content',
                 'human_required':{'secret':'NEVER_PERSIST_AUTH_CANARY'} if self.needs else False,
                 'phase':self.phase,'epoch':self.resumed,'discovery':{'authenticated':not self.needs}}
        value['state_id'] = digest(value)
        value['actions'] = [] if self.needs or self.phase==2 else [{'kind':'browser_click','state_id':value['state_id']}]
        return value
    def enter_manual(self):
        self.manual = True
        self.entered += 1
    def human_tick(self, *, wall_deadline=None):
        assert self.manual
        self.deadlines.append(wall_deadline)
    def resume_manual(self):
        self.manual = self.needs = False
        self.resumed += 1
    def execute(self, action):
        assert not self.needs and not self.manual and action in self.observe()['actions']
        self.effects.append(deepcopy(action))
        self.phase += 1
        return {'ok':True,'state':'verified','code':'fixture_verified','observation':self.observe()}
    def close(self): self.closed = True

def test_active_clock_only_excludes_human_wait_and_translates_native_deadline():
    raw = [100.0]
    clock = ActiveClock(lambda: raw[0])
    clock.pause(); raw[0] += 600
    assert clock() == 100
    clock.resume(); raw[0] += 5
    assert clock() == 105 and clock.wall_deadline(460) == 1060

@pytest.fixture
def gate(tmp_path):
    raw = [100.0]
    clock = ActiveClock(lambda:raw[0])
    browser = Browser()
    def wait(request,tick): tick(); return True
    value = HumanHandoff(tmp_path,CONFIG,stop=lambda:False,clock=clock,callback=wait,wall=lambda:raw[0])
    return value,browser,raw,clock

def test_handoff_is_bounded_logged_and_does_not_capture_auth_data(gate):
    handoff,browser,raw,clock = gate
    for _ in range(3): handoff.perform(browser)
    assert browser.entered == browser.resumed == 3
    assert browser.deadlines == [1000]*3
    with pytest.raises(ComputerError,match='browser_handoff_limit'): handoff.perform(browser)
    assert browser.entered == 3 and clock.paused is None
    assert len(list(handoff.run.glob('human-*.request.json'))) == 3

@pytest.mark.parametrize('variant',['late_before_tick','late_after_tick','late_return','decline','stop','error'])
def test_timeout_stop_decline_and_callback_failure_never_resume(gate,variant):
    handoff,browser,raw,clock = gate
    def wait(request,tick):
        if variant=='late_before_tick': raw[0]+=901; tick()
        if variant=='late_after_tick':
            browser.human_tick=lambda **kw:raw.__setitem__(0,raw[0]+901)
            tick()
        if variant=='late_return': raw[0]+=901; return True
        if variant=='decline': return False
        if variant=='stop': handoff.stop=lambda:True; tick()
        if variant=='error': raise RuntimeError('fixture')
    handoff.callback=wait
    with pytest.raises((ComputerError,RuntimeError)): handoff.perform(browser)
    assert browser.resumed == 0 and clock.paused is None

@pytest.fixture
def ctx():
    with tempfile.TemporaryDirectory(prefix='jgh-') as folder:
        session=ComputerSession.create(Path(folder)/'c')
        plan=interaction.prepare(session,'Search and read approved repository',browser=CONFIG)
        yield session,plan

def test_same_task_waits_and_continues_after_explicit_human_gate_without_cloud(ctx):
    session,plan=ctx
    browser=Browser()
    raw=[100.0]
    def wait(request,tick):
        assert set(request)=={'site','target_url','reason','handoff'}
        raw[0]+=600; tick(); return True
    result=engine.execute(session,plan,approval=plan['sha256'],clock=lambda:raw[0],
        browser_factory=lambda *a,**kw:browser,human_handoff=wait)
    assert result['ok'] and result['actions']==2 and browser.entered==1 and browser.closed
    assert result['repository_read']['authenticated'] and result['repository_read']['text']=='private project content'
    assert not list((session.root/'model_calls').glob('*.json'))
    files=list(Path(result['run']).rglob('*.json'))
    assert not any('NEVER_PERSIST_AUTH_CANARY' in f.read_text(encoding='utf-8') for f in files)

def test_missing_human_callback_stops_without_automatic_login(ctx):
    session,plan=ctx
    browser=Browser()
    result=engine.execute(session,plan,approval=plan['sha256'],browser_factory=lambda *a,**kw:browser)
    assert result['code']=='browser_human_required' and browser.entered==0 and browser.closed
    assert not browser.effects

def test_continue_is_not_auth_proof_three_failed_handoffs_stop(ctx):
    session,plan=ctx
    browser=Browser()
    browser.resume_manual=lambda:None
    result=engine.execute(session,plan,approval=plan['sha256'],browser_factory=lambda *a,**kw:browser,
                          human_handoff=lambda req,tick:True)
    assert result['code']=='browser_handoff_limit' and browser.entered==3 and not browser.effects

def test_private_multiple_choices_do_not_call_model(ctx,monkeypatch):
    session,plan=ctx
    from test_computer_agent import Environment
    monkeypatch.setattr(Environment,'actions',lambda *a:[{'kind':'browser_click'},{'kind':'browser_wait'}])
    monkeypatch.setattr(engine,'model_json',lambda *a,**kw:pytest.fail('private content sent to model'))
    result=engine.execute(session,plan,approval=plan['sha256'],environment_factory=Environment)
    assert result['code']=='github_ambiguous_actions' and result['actions']==0

def test_github_editor_derives_query_and_preview_keeps_content_local():
    from agent_service.interaction_panel import build_request
    request=build_request({'url':CONFIG['start_url'],'site_mode':'GitHub仓库（人工登录）',
                           'query':'ignored','expected':'','word':'','excel':''})
    assert request['browser']['query']==CONFIG['query'] and request['browser']['human_login']
    preview=interaction.describe({'goal':{'kind':'interactive','browser':request['browser'],'edits':[]},'instruction':'read'})
    assert '15分钟' in preview and 'GitHub任务不调用云模型' in preview and '提供给云模型' not in preview
