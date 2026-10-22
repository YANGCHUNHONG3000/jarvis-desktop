"""Offline core-loop contracts; no network, native apps or real budget ledger."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from agent_service import computer_agent as agent
from agent_service.computer import ComputerError, ComputerSession, read_record, digest

GOAL = {'queries':['topic','alternative'], 'title':'资料', 'min_sources':1,
        'required_terms':['fact'], 'outputs':['document']}


class Environment:
    def __init__(self, goal, run, **kwargs):
        self.n, self.changed, self.effects = 0, False, []
        self.options = kwargs
    def observe(self):
        return {'n':self.n, 'changed':self.changed, 'checks':{'sources_ready':False,'goal_met':self.n >= 2},
                'artifacts':[], 'source_reads':min(self.n,2), 'failures':[]}
    def actions(self, observation):
        return [{'kind':'search','query_index':self.n}]
    def execute(self, action, root):
        self.effects.append(action)
        self.n += 1
        return {'ok':True,'state':'verified','code':'verified'}


@pytest.fixture
def context(tmp_path):
    session = ComputerSession.create(tmp_path.parent/('loop-'+digest(str(tmp_path))[:10]))
    plan = agent.prepare(session, '真实目标', GOAL)
    holder = {}
    def factory(*args, **kwargs):
        holder['env'] = Environment(*args, **kwargs)
        return holder['env']
    return session, plan, holder, factory


def run(context, **kwargs):
    session, plan, _, factory = context
    return agent.execute(session, plan, approval=plan['sha256'], environment_factory=factory, **kwargs)


class Client:
    last_call = {'state':'test_only'}
    def __init__(self, select=None):
        self.calls, self.select = [], select
    def complete(self, system, user, **kwargs):
        data = json.loads(user)
        self.calls.append(data)
        return self.select(data) if self.select else {'action_id':data['choices'][0]['id']}


def multiple(monkeypatch):
    monkeypatch.setattr(Environment, 'actions', lambda self,obs:[{'kind':'search','query_index':self.n},
                                                               {'kind':'read','candidate_id':'c1'}])


def test_single_choice_observes_executes_verifies_without_model(context):
    client = Client()
    result = run(context, client=client)
    assert result['ok'] and result['code'] == 'objective_verified'
    assert result['actions'] == 2 and not client.calls
    root = Path(result['run'])
    assert read_record(root/'step-01'/'decision.json')['method'] == 'single_legal_action'
    assert (root/'observation-03.json').is_file()
    with pytest.raises(ComputerError,match='goal_already_claimed'):
        run(context,client=client)


def test_multiple_choices_use_new_observations_and_durable_claims(context,monkeypatch):
    multiple(monkeypatch)
    client = Client()
    result = run(context,client=client)
    assert result['ok'] and len(client.calls)==2
    assert [c['observation']['n'] for c in client.calls]==[0,1]
    assert client.calls[0]['choices'][0]['id'] != client.calls[1]['choices'][0]['id']
    assert len(list((context[0].root/'model_calls').glob('*.outcome.json')))==2


@pytest.mark.parametrize('response',[None,{}, {'action_id':'forged'}, {'action_id':[]},
                                    {'action_id':'x','command':'delete'}, {'ok':True}])
def test_invalid_model_response_never_executes(context,monkeypatch,response):
    multiple(monkeypatch)
    result=run(context,client=Client(lambda _:response))
    assert result['code']=='agent_decision_invalid' and not context[2]['env'].effects


def test_observation_changes_while_model_runs_stop_effect(context,monkeypatch):
    multiple(monkeypatch)
    def select(data):
        context[2]['env'].changed=True
        return {'action_id':data['choices'][0]['id']}
    result=run(context,client=Client(select))
    assert result['code']=='agent_observation_changed' and not context[2]['env'].effects


def test_previous_choice_id_is_not_valid_next_round(context,monkeypatch):
    multiple(monkeypatch)
    saved=[]
    def select(data):
        saved.append(data['choices'][0]['id'])
        return {'action_id':saved[0]}
    result=run(context,client=Client(select))
    assert result['code']=='agent_decision_invalid' and len(context[2]['env'].effects)==1


def test_cancel_after_model_keeps_call_but_no_effect(context,monkeypatch):
    multiple(monkeypatch)
    stopped=[False]
    def select(data):
        stopped[0]=True
        return {'action_id':data['choices'][0]['id']}
    result=run(context,client=Client(select),stop=lambda:stopped[0])
    assert result['code']=='cancelled_after_model' and not context[2]['env'].effects


def test_cancel_before_loop_no_call_or_effect(context):
    client=Client()
    result=run(context,client=client,stop=lambda:True)
    assert result['code']=='goal_cancelled' and not client.calls


def test_budget_error_preserved_and_not_retried(context,monkeypatch):
    from agent_service.budget import BudgetError
    multiple(monkeypatch)
    def fail(_): raise BudgetError('no available budget')
    client=Client(fail)
    result=run(context,client=client)
    assert result['code']=='model_budget_exceeded' and len(client.calls)==1
    assert not context[2]['env'].effects


def test_existing_six_model_claims_cannot_be_bypassed(context,monkeypatch):
    multiple(monkeypatch)
    for n in range(6): context[0].claim_model({'previous':n})
    client=Client()
    result=run(context,client=client)
    assert result['code']=='session_model_call_limit' and not client.calls


@pytest.mark.parametrize('state',['unknown','failed'])
def test_office_failure_never_replays(context,monkeypatch,state):
    monkeypatch.setattr(Environment,'actions',lambda self,obs:[{'kind':'document'}])
    def fail(self,action,root):
        self.effects.append(action)
        return {'ok':False,'state':state,'code':'native_failure'}
    monkeypatch.setattr(Environment,'execute',fail)
    result=run(context)
    assert not result['ok'] and result['code']=='agent_office_'+state
    assert len(context[2]['env'].effects)==1
    assert (result['state']=='unknown') == (state=='unknown')


def test_final_observation_integrity_error_prevents_success(context,monkeypatch):
    original=Environment.observe
    def observe(self):
        if self.n==2: raise ComputerError('artifact_changed')
        return original(self)
    monkeypatch.setattr(Environment,'observe',observe)
    result=run(context)
    assert not result['ok'] and result['code']=='artifact_changed' and not result['artifacts']


def test_known_readonly_failure_is_observed_before_next_choice(context,monkeypatch):
    original=Environment.execute
    def execute(self,action,root):
        value=original(self,action,root)
        return {'ok':False,'state':'failed','code':'search_unavailable'} if self.n==1 else value
    monkeypatch.setattr(Environment,'execute',execute)
    result=run(context)
    assert result['ok'] and len(result['attempts'])==2 and len(result['completed'])==1
    assert read_record(Path(result['run'])/'step-01'/'verification.json')['ok'] is False


@pytest.mark.parametrize('key,value',[('queries',[]),('queries',['x','X']),('queries',['x']*3),
    ('min_sources',True),('min_sources',3),('required_terms',['a']*4),('outputs',['document']*2),
    ('outputs',['shell']),('title','x\ncommand'),('title',' '*200+'x')])
def test_invalid_goals_rejected(context,key,value):
    goal=deepcopy(GOAL); goal[key]=value
    with pytest.raises(ComputerError): agent.prepare(context[0],'goal',goal)


def test_expired_preview_and_changed_plan_rejected(context):
    session,plan,_,_=context
    altered=deepcopy(plan); altered['goal']['outputs']=['spreadsheet']
    with pytest.raises(ComputerError,match='goal_plan_changed'):
        agent.execute(session,altered,approval=plan['sha256'])


def test_action_limit_enforced(context,monkeypatch):
    original=Environment.observe
    def observe(self):
        result=original(self); result['checks']['goal_met']=False; return result
    monkeypatch.setattr(Environment,'observe',observe)
    result=run(context)
    assert result['code']=='agent_action_limit' and len(context[2]['env'].effects)==12


def test_deadline_after_action_prevents_next_action(context,monkeypatch):
    tick=[0]
    original=Environment.execute
    def execute(self,*args):
        value=original(self,*args); tick[0]=361; return value
    monkeypatch.setattr(Environment,'execute',execute)
    result=run(context,clock=lambda:tick[0])
    assert result['code']=='goal_deadline' and len(context[2]['env'].effects)==1


def test_mutating_executor_cannot_change_office_failure_policy(context,monkeypatch):
    monkeypatch.setattr(Environment,'actions',lambda self,obs:[{'kind':'document'}])
    def mutate(self,action,root):
        action['kind']='read'
        self.effects.append(action)
        return {'ok':False,'state':'failed','code':'known_failure'}
    monkeypatch.setattr(Environment,'execute',mutate)
    result=run(context)
    assert result['code']=='agent_office_failed' and len(result['attempts'])==1
    assert result['attempts'][0]['action']=={'kind':'document'}


def test_nonboolean_goal_met_is_rejected(context,monkeypatch):
    original=Environment.observe
    def observe(self):
        value=original(self); value['checks']['goal_met']='yes'; return value
    monkeypatch.setattr(Environment,'observe',observe)
    result=run(context)
    assert result['code']=='agent_observation_invalid' and not context[2]['env'].effects


def test_real_environment_loop_recovers_readonly_failures_and_passes_office(tmp_path):
    from agent_service.contracts import Observation, BrowserReadError
    from test_artifact_source import fake as native_fake
    session=ComputerSession.create(tmp_path.parent/('loop-'+digest(str(tmp_path))[:10]))
    goal={'queries':['bad query','topic'], 'title':'来源','min_sources':1,
          'required_terms':['正文'], 'outputs':['spreadsheet','document']}
    plan=agent.prepare(session,'找正文生成表格和文档',goal)
    class Search:
        def search(self,query,*args,**kwargs):
            if query=='bad query': raise ComputerError('search_unavailable')
            return {'urls':['https://example.org/failed','https://example.org/ok'],
                    'searched_at':'2026-10-17T12:00:00+00:00'}
    class Browser:
        def read(self,url,**kwargs):
            if url.endswith('failed'): raise BrowserReadError('network_timeout')
            return Observation(url,url,'真实来源','正文内容','2026-10-17T12:00:00+00:00')
    client=Client()
    outcome=agent.execute(session,plan,approval=plan['sha256'],client=client,
                         search=Search(),browser=Browser(),office_execute=native_fake,
                         task_base=session.root.parent/'tasks')
    assert outcome['ok'] and outcome['actions']==6
    assert len(outcome['failures'])==2 and len(outcome['artifacts'])==2
    assert len(client.calls)==2
    assert client.calls[1]['observation']['failures'][0]['code']=='search_unavailable'
    assert [x['action']['kind'] for x in outcome['attempts']]==['search','search','read','read','spreadsheet','document']


def test_objective_proposal_only_prepares_local_approval(context):
    value=agent.propose(context[0],'请找资料',client=Client(lambda _:{'kind':'supported','goal':GOAL}))
    assert value['plan']['mode']=='agent' and value['plan']['goal']==GOAL
    assert not list((context[0].root/'goals').glob('*.run'))


def test_unsupported_objective_is_not_executed(context):
    value=agent.propose(context[0],'unsupported',client=Client(lambda _:{'kind':'unsupported','reason':'范围不支持'}))
    assert value['plan'] is None and not list((context[0].root/'goals').glob('*.run'))


def test_stop_after_real_office_receipt_retains_verified_artifact(tmp_path):
    from agent_service.contracts import Observation
    from test_artifact_source import fake as native_fake
    session=ComputerSession.create(tmp_path.parent/('loop-'+digest(str(tmp_path))[:10]))
    goal={'queries':['topic'],'title':'资料','min_sources':1,'required_terms':[],
          'outputs':['spreadsheet','document']}
    plan=agent.prepare(session,'资料',goal)
    stopped=[False]
    class Search:
        def search(self,*a,**kw):
            return {'urls':['https://example.org/'],'searched_at':'2026-10-17T12:00:00+00:00'}
    class Browser:
        def read(self,url,**kw):
            return Observation(url,url,'source','topic actual text','2026-10-17T12:00:00+00:00')
    def office(spec,root,stop):
        value=native_fake(spec,root,stop); stopped[0]=True; return value
    result=agent.execute(session,plan,approval=plan['sha256'],stop=lambda:stopped[0],
        search=Search(),browser=Browser(),office_execute=office,task_base=session.root.parent/'tasks')
    assert not result['ok'] and result['state']=='partial' and result['code']=='goal_cancelled'
    assert len(result['artifacts'])==1 and result['artifacts'][0]['kind']=='spreadsheet'
    assert len(result['attempts'])==3


def test_verified_step_pause_is_after_receipt_and_never_passed_to_tools(context):
    def pause():
        env=context[2]['env']
        assert 'pause_after_step' not in env.options
        assert (context[0].root/'goals'/(context[1]['sha256']+'.run')/'step-01'/'verification.json').is_file()
        return True
    result=run(context,pause_after_step=pause)
    assert result['code']=='agent_paused' and result['state']=='partial'
    assert result['actions']==len(result['completed'])==1


def test_final_verified_step_wins_over_pause(context):
    calls=[]
    result=run(context,pause_after_step=lambda: calls.append(1) or len(calls)==2)
    assert result['ok'] and result['code']=='objective_verified'
    assert len(calls)==2 and result['actions']==2
