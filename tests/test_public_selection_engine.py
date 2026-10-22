"""Public result selection uses the existing model/ledger gate and bounded data."""
from copy import deepcopy
import json

import pytest

from agent_service import computer_agent as agent
from agent_service.computer import ComputerSession, digest, read_record
from agent_service.computer_interaction import describe
from agent_service.interaction_panel import build_request
from agent_service.budget import BudgetError


def config():
    return {'mode':'public_search','site':'python_docs','start_url':'https://docs.python.org/3/',
            'query':'json','target_url':'','required_text':['json'],'headless':False}


class Environment:
    def __init__(self, goal, run, **kwargs):
        self.n = 0
        self.effects = []
    def observe(self):
        return {'checks':{'goal_met':self.n == 1,'sources_ready':False},
                'secret_office_original':'DO NOT UPLOAD',
                'artifacts':[],'failures':[],'source_reads':0}
    def actions(self, observed):
        return [
            {'kind':'browser_click','target_url':'https://docs.python.org/3/library/json.html',
             'title':'JSON encoding and decoding','snippet':'json.loads and json.dumps'},
            {'kind':'browser_click','target_url':'https://docs.python.org/3/whatsnew/3.14.html',
             'title':'What is new','snippet':'json changes'},
        ]
    def execute(self, action, root):
        self.effects.append(action)
        self.n += 1
        return {'ok':True,'state':'verified','code':'browser_public_target_opened'}


class Client:
    last_call = {'state':'offline_test'}
    def __init__(self, selector=None):
        self.calls = []
        self.selector = selector or (lambda data: {'action_id':data['choices'][0]['id']})
    def complete(self, system, user, **kwargs):
        data = json.loads(user)
        self.calls.append((data,kwargs))
        return self.selector(data)


def run(tmp_path, client, monkeypatch=None, target=''):
    session = ComputerSession.create(tmp_path.parent/('select-'+digest(str(tmp_path))[:12]))
    plan = agent.prepare(session,'查JSON标准库编码和解码用法',
                         {'kind':'interactive','browser':config()|{'target_url':target},'edits':[]})
    holder = {}
    def factory(*args,**kwargs):
        holder['env'] = Environment(*args,**kwargs)
        return holder['env']
    result = agent.execute(session,plan,approval=plan['sha256'],
                           client=client,environment_factory=factory)
    return result,holder['env'],session


def test_task_chooses_issued_result_and_only_metadata_is_uploaded(tmp_path):
    client = Client()
    result,env,session = run(tmp_path,client)
    assert result['ok'] and len(env.effects) == len(client.calls) == 1
    data,options = client.calls[0]
    assert set(data) == {'instruction','site','query','choices'}
    assert all(set(c) == {'id','url','title','snippet'} for c in data['choices'])
    assert 'DO NOT UPLOAD' not in json.dumps(data)
    assert data['instruction'] == '查JSON标准库编码和解码用法' and options['max_tokens'] == 80
    assert env.effects[0]['target_url'].endswith('/library/json.html')
    assert read_record(__import__('pathlib').Path(result['run'])/'step-01/decision.json')['method'] == 'model_choice'
    assert read_record(session.root/'model_calls/1.json')['context'].keys() == {'agent_context_sha256'}
    assert not (session.root/'model_calls/2.json').exists()


@pytest.mark.parametrize('response,code', [
    ({'action_id':None},'public_site_no_relevant_result'),
    ({'action_id':'invented'},'agent_decision_invalid'),
    ({'url':'https://evil.example'},'agent_decision_invalid'),
])
def test_rejected_choice_never_clicks(tmp_path,response,code):
    result,env,_ = run(tmp_path,Client(lambda data:response))
    assert not result['ok'] and result['code'] == code and env.effects == []


def test_budget_failure_stops_before_dispatch(tmp_path):
    def denied(data):
        raise BudgetError('offline budget deny')
    result,env,_ = run(tmp_path,Client(denied))
    assert result['code'] == 'model_budget_exceeded' and not env.effects


def test_single_candidate_uses_no_model(tmp_path,monkeypatch):
    original = Environment.actions
    monkeypatch.setattr(Environment,'actions',lambda self,obs:original(self,obs)[:1])
    client = Client()
    result,env,_ = run(tmp_path,client)
    assert result['ok'] and not client.calls and len(env.effects) == 1


def test_explicit_target_keeps_local_only_boundary(tmp_path):
    client = Client()
    result,env,_ = run(tmp_path,client,target='https://docs.python.org/3/library/json.html')
    assert result['code'] == 'public_site_ambiguous_actions' and not client.calls and not env.effects


def test_new_gui_defaults_to_task_selection_and_discloses_small_upload():
    request = build_request({'site_mode':'Python 文档（公开检索）','url':'','query':'json',
                             'expected':'','word':'','excel':''})
    assert request['browser']['target_url'] == ''
    assert request['browser']['required_text'] == ['json']
    text = describe({'instruction':'查JSON','goal':{'kind':'interactive',**request}})
    assert '最多8个' in text and '公开标题/网址/简短摘录' in text
    assert '此站点任务不调用云模型' not in text


@pytest.mark.parametrize('change', [
    {'kind':'office_edit'}, {'target_url':'https://evil.example/a'},
    {'title':'x'*201}, {'snippet':'x'*401},
])
def test_metadata_builder_rejects_out_of_contract_candidates(change):
    action = Environment(None,None).actions({})[0] | change
    with pytest.raises(ValueError):
        agent.public_selection_context('task',config(),[{'id':'issued','action':action}])
