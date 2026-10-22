"""Core dynamic refresh and wait routing; no external network/model/native calls."""
from copy import deepcopy
from pathlib import Path
import tempfile

import pytest
from agent_service import computer_agent as engine, computer_interaction as interaction
from agent_service.computer import ComputerError,ComputerSession,digest,read_record
from test_computer_agent import Environment,Client

CONFIG={'mode':'discover_query','start_url':'https://example.org/','query':'query',
        'required_text':['verified'], 'javascript':True,'headless':True}

@pytest.fixture
def context():
    with tempfile.TemporaryDirectory(prefix='jjs-') as folder:
        session=ComputerSession.create(Path(folder)/'c')
        plan=interaction.prepare(session,'dynamic query',browser=CONFIG)
        holder={}
        def factory(*args,**kwargs):
            holder['env']=Environment(*args,**kwargs)
            return holder['env']
        yield session,plan,holder,factory

def run(ctx,**kwargs):
    session,plan,_,factory=ctx
    return engine.execute(session,plan,approval=plan['sha256'],environment_factory=factory,**kwargs)

def test_model_observation_change_discards_old_choice_then_recovers(context,monkeypatch):
    monkeypatch.setattr(Environment,'actions',lambda self,obs:[
        {'kind':'browser_fill','marker':obs['changed']},{'kind':'browser_wait','marker':obs['changed']}])
    def choose(data):
        context[2]['env'].changed=True
        return {'action_id':data['choices'][0]['id']}
    result=run(context,client=Client(choose))
    assert result['ok'] and len(result['reobservations'])==1
    assert result['reobservations'][0]['effect_dispatched'] is False
    assert all(action['marker'] is True for action in context[2]['env'].effects)
    assert not (Path(result['run'])/'step-01/intent.json').exists()

def test_enumeration_transient_change_reobserves_without_dispatch(context,monkeypatch):
    calls=[]
    def actions(self,obs):
        calls.append(1)
        if len(calls)==1: raise ComputerError('agent_observation_changed')
        return [{'kind':'browser_wait'}]
    monkeypatch.setattr(Environment,'actions',actions)
    result=run(context)
    assert result['ok'] and result['reobservations'][0]['phase']=='enumeration'
    assert len(context[2]['env'].effects)==2

def test_reobservation_limit_never_unbounds_loop(context,monkeypatch):
    def actions(*args): raise ComputerError('agent_observation_changed')
    monkeypatch.setattr(Environment,'actions',actions)
    result=run(context)
    assert result['code']=='agent_reobservation_limit' and len(result['reobservations'])==3
    assert not context[2]['env'].effects

def test_late_no_dispatch_stale_results_share_three_refresh_limit(context,monkeypatch):
    monkeypatch.setattr(Environment,'actions',lambda self,obs:[{'kind':'browser_fill'}])
    monkeypatch.setattr(Environment,'execute',lambda *args:{'ok':False,'state':'failed',
        'code':'browser_action_stale','verification':{'effect_dispatched':False}})
    result=run(context)
    assert result['code']=='agent_reobservation_limit' and len(result['reobservations'])==3
    assert result['actions']==4 and not context[2]['env'].effects

@pytest.mark.parametrize('code',['interaction_source_changed','agent_evidence_changed','agent_observation_invalid'])
def test_integrity_errors_are_never_treated_as_transient(context,monkeypatch,code):
    def actions(*args): raise ComputerError(code)
    monkeypatch.setattr(Environment,'actions',actions)
    result=run(context)
    assert result['code']==code and not result['reobservations'] and not context[2]['env'].effects


class DynamicBrowser:
    def __init__(self,config,**options): self.phase=0; self.effects=[]; self.closed=False
    def observe(self):
        value={'session_id':'fake','phase':self.phase,'locked':False,'checks':{'goal_met':self.phase==4}}
        value['state_id']=digest(value)
        kind=['browser_wait','browser_fill','browser_click','browser_wait',None][self.phase]
        value['actions']=[] if kind is None else [{'kind':kind,'state_id':value['state_id']}]
        return value
    def execute(self,action):
        assert action in self.observe()['actions']
        self.effects.append(deepcopy(action));self.phase+=1
        return {'ok':True,'state':'verified','code':'observed','observation':self.observe()}
    def close(self): self.closed=True

def test_wait_routed_to_browser_and_shared_loop_waits_for_real_goal(context):
    session,plan,holder,_=context
    def factory(*args,**kwargs):
        holder['browser']=DynamicBrowser(*args,**kwargs);return holder['browser']
    result=engine.execute(session,plan,approval=plan['sha256'],browser_factory=factory)
    assert result['ok'] and result['actions']==4 and holder['browser'].closed
    assert [v['kind'] for v in holder['browser'].effects]==[
        'browser_wait','browser_fill','browser_click','browser_wait']

def test_pre_dispatch_stale_environment_records_no_effect_failure(context,monkeypatch):
    session,plan,_,_=context
    root=session.root/'goals'/'isolated.run';root.mkdir()
    env=interaction.InteractionEnvironment(plan['goal'],root,stop=lambda:False,deadline=999999999,
                                          browser_factory=DynamicBrowser)
    action=env.observe()['browser']['actions'][0]
    def changed(*args): raise ComputerError('agent_observation_changed')
    monkeypatch.setattr(env,'actions',changed)
    step=root/'step-01';step.mkdir()
    result=env.execute(action,step)
    assert result['state']=='failed' and result['verification']['effect_dispatched'] is False
    assert not env.browser.effects
    env._facts() # Durable failure is a valid record, not an unrecorded interrupted action.
    assert read_record(step/'environment-result.json')['data']['result']==result

def test_gui_enables_js_without_adding_manual_fields():
    from agent_service.interaction_panel import build_request
    request=build_request({'url':'https://example.org/','query':'query','expected':'verified','word':'','excel':''})
    assert request['browser']['javascript'] is True
    assert not {'rules','allowed_paths'} & request['browser'].keys()

def test_preview_explains_dynamic_wait_and_retry(context):
    text=interaction.describe(context[1])
    assert 'JavaScript' in text and '3次' in text and '未知效果' in text
