"""Public-profile GUI envelopes, independent proofs and dispatch wiring."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_service.browser_checkpoint import verify_browser, verify_history
from agent_service.computer import ComputerError, digest
from agent_service.computer_interaction import validate_goal
from agent_service.interaction_panel import build_request


def config():
    return {'mode':'public_search', 'site':'python_docs', 'start_url':'https://docs.python.org/3/',
            'query':'pathlib', 'target_url':'https://docs.python.org/3/library/pathlib.html',
            'required_text':['pathlib'], 'headless':False}


def values():
    return {'site_mode':'Python 文档（公开检索）', 'url':'', 'query':'pathlib',
            'target_url':config()['target_url'], 'expected':'pathlib', 'word':'', 'excel':''}


def snapshot():
    v = {'session_id':'owned', 'url':config()['target_url'], 'text':'pathlib docs',
         'locked':False, 'human_required':False, 'checks':{'goal_met':True},
         'discovery':{'mode':'public_search', 'target_url':config()['target_url'], 'phase':'verified',
             'search_status':200, 'content_status':200, 'ambiguous_controls':False,
             'controls_overflow':False, **dict.fromkeys(('query_verified','content_verified',
                'main_present','search_submitted','target_clicked','search_finished','content_finished'), True)}}
    v['state_id'] = digest(v)
    v['actions'] = []
    return v


def test_gui_profile_supplies_homepage_and_both_office_outputs():
    request = build_request(values())
    assert request == {'browser':config(), 'edits':[], 'page_office':True}
    assert validate_goal({'kind':'interactive', **request})['page_office'] is True


def test_gui_can_explicitly_omit_office_without_changing_search_scope():
    request = build_request(values() | {'page_office':False})
    assert 'page_office' not in request and request['browser'] == config()


@pytest.mark.parametrize('change', [
    {'url':'https://www.python.org/'}, {'target_url':'https://evil.example/x'},
    {'target_url':'https://docs.python.org/3/search.html'}, {'query':''}])
def test_gui_rejects_scope_expansion(change):
    with pytest.raises(ValueError):
        build_request(values() | change)


@pytest.mark.parametrize('change', [
    {'page_office':'true'}, {'repository_document':True,'page_office':True},
    {'browser':None,'page_office':True}])
def test_goal_rejects_invalid_delivery_scope(change):
    with pytest.raises(ValueError):
        validate_goal({'kind':'interactive','browser':config(),'edits':[]} | change)


def test_independent_browser_proof_accepts_complete_snapshot():
    assert verify_browser(config(), snapshot())['text'] == 'pathlib docs'


@pytest.mark.parametrize('key,value', [
    ('search_finished',False), ('content_finished',False), ('target_clicked',False),
    ('search_submitted',False), ('search_status',True), ('content_status',503),
    ('query_verified',False), ('content_verified',False), ('main_present',False),
    ('ambiguous_controls',True), ('controls_overflow',True),
    ('target_url','https://docs.python.org/3/tutorial/index.html')])
def test_content_boolean_cannot_replace_independent_proof(key, value):
    snap = snapshot()
    snap['discovery'][key] = value
    snap['state_id'] = digest({k:v for k,v in snap.items() if k not in {'state_id','actions'}})
    with pytest.raises(ComputerError, match='target_not_verified'):
        verify_browser(config(), snap)


@pytest.mark.parametrize('codes,good', [
    (['browser_public_search_submitted','browser_public_target_opened','browser_wait_observed'],True),
    (['browser_public_target_opened'],False),
    (['browser_public_target_opened','browser_public_search_submitted'],False),
    (['browser_public_search_submitted','browser_public_search_submitted','browser_public_target_opened'],False),
    (['browser_public_search_submitted','browser_public_target_opened','browser_fill_verified'],False)])
def test_history_requires_single_ordered_search_and_target_click(codes, good):
    snap = snapshot()
    attempts = [{'action':{'kind':'browser_click'}, 'result':{
        'ok':True,'code':c,'observation':deepcopy(snap)}} for c in codes]
    if good:
        verify_history(config(), {'browser':snap}, attempts)
    else:
        with pytest.raises(ComputerError, match='public_read_missing'):
            verify_history(config(), {'browser':snap}, attempts)


def test_production_goal_dispatches_delivery_only_after_success(monkeypatch, tmp_path):
    from agent_service import computer_goals, computer_agent, page_office_delivery
    plan = {'mode':'agent','goal':{'page_office':True}}
    plan['sha256'] = digest(plan)
    monkeypatch.setattr(computer_goals, 'read_record', lambda path:deepcopy(plan))
    reading = {'ok':True,'artifacts':[]}
    monkeypatch.setattr(computer_agent, 'execute', lambda *a, **k:deepcopy(reading))
    calls = []
    monkeypatch.setattr(page_office_delivery, 'deliver_page_office',
        lambda value, **kwargs:calls.append((value,kwargs)) or {'ok':True,'code':'delivered'})
    session = SimpleNamespace(root=tmp_path/'session')
    result = computer_goals.execute_goal(session,plan,approval=plan['sha256'])
    assert result['code'] == 'delivered' and len(calls) == 1
    reading['ok'] = False
    assert not computer_goals.execute_goal(session,plan,approval=plan['sha256'])['ok']
    assert len(calls) == 1
