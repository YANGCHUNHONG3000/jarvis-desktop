"""A delayed search GET is proved by the independently bound pre-click read."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from agent_service import interaction_recovery as recovery
from agent_service.browser_resume import derive, public_search_url
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_interaction import prepare
from test_computer_interaction import ctx, run
from test_public_selection_recovery import PublicBrowser


TARGET = 'https://docs.djangoproject.com/en/5.2/topics/email/'
CONFIG = {'mode':'public_search','site':'django_docs',
    'start_url':'https://docs.djangoproject.com/en/5.2/','query':'email',
    'required_text':['Verified browser result']}


def refresh(browser):
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    for action in browser['actions']:
        action['state_id'] = browser['state_id']
    return browser


class AsyncSearchBrowser(PublicBrowser):
    def observe(self):
        browser = super().observe()
        if self.phase >= 3:
            browser['url'] = browser['discovery']['target_url'] = TARGET
        if self.phase == 2:
            browser['actions'][0].update(target_url=TARGET,title='Sending email',snippet='Django email')
        browser['discovery'].update(site='django_docs',
            phase='verified' if self.phase == 4 else 'target_pending' if self.phase == 3
                  else 'search_results' if self.phase == 2 else 'search_pending')
        return refresh(browser)

    def execute(self, action):
        result = super().execute(action)
        if result['code'] == 'browser_public_search_submitted':
            browser = result['observation']
            browser['url'] = self.config['start_url']
            browser['discovery'].update(query_verified=False,search_status=None,
                search_finished=False,phase='search_pending',search_proof=None)
            browser['actions'] = [{'kind':'browser_wait','rule_id':'wait-search',
                'control_id':'wait-search','state_id':browser['state_id']}]
            refresh(browser)
        return result


def stopped(ctx):
    browsers = []
    def factory(config, **options):
        browser = AsyncSearchBrowser(config,stopped=ctx['stopped'],stop_phase=3,**options)
        browsers.append(browser)
        return browser
    plan = prepare(ctx['session'],'Read Django email documentation',browser=CONFIG,page_office=True)
    result = run(ctx,plan,browser_factory=factory)
    ctx['stopped'][0] = False
    assert result['state'] == 'partial' and result['actions'] == 3, result
    assert result['attempts'][1]['result']['observation']['discovery']['query_verified'] is False
    assert result['attempts'][2]['result']['observation']['url'] == TARGET
    return plan, result, browsers[0]


def test_preclick_search_read_restores_opened_phase_without_replaying_any_action(ctx):
    plan, prior, old = stopped(ctx)
    root = Path(prior['run'])
    original = {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    inspected = recovery._inspect(ctx['session'],root)
    assert inspected['browser_resume']['phase'] == 'target_opened'
    assert inspected['browser_resume']['target_url'] == TARGET
    before = inspected['browser_history'][-1]['browser_before']
    assert before['url'] == public_search_url(plan['goal']['browser'])
    assert before['discovery']['search_finished'] is True
    resumed = recovery.prepare(ctx['session'],root)['plan']
    def fresh(config, **options):
        browser = AsyncSearchBrowser(config,**options)
        browser.phase = 4
        return browser
    result = run(ctx,resumed,browser_factory=fresh)
    assert result['ok'] and result['actions'] == 0 and ctx['calls'] == []
    assert len(old.effects) == 3
    verified = recovery.verify_reading(ctx['session'],result['run'])
    assert verified['browser_checkpoint']['browser']['url'] == TARGET
    assert all((root/path).read_bytes() == value for path,value in original.items())
    assert read_record(root/'outcome.json') == prior


@pytest.mark.parametrize('change',['no_submit','no_before','wrong_query','cross_session','not_issued',
    'unfinished','status','bool_status','ambiguous','wrong_site','not_submitted','before_hash','unknown_click'])
def test_preclick_read_cannot_invent_search_completion(ctx,change):
    plan, prior, _ = stopped(ctx)
    history = deepcopy(recovery._inspect(ctx['session'],prior['run'])['browser_history'])
    before = history[-1]['browser_before']
    if change == 'no_submit':
        del history[1]
    elif change == 'no_before':
        del history[-1]['browser_before']
    elif change == 'wrong_query':
        before['url'] = before['url'].replace('q=email','q=other')
    elif change == 'cross_session':
        before['session_id'] = 'unrelated'
        history[-1]['result']['observation']['session_id'] = 'unrelated'
    elif change == 'not_issued':
        before['actions'] = []
    elif change == 'unfinished':
        before['discovery']['search_finished'] = False
    elif change in ('status','bool_status'):
        before['discovery']['search_status'] = 503 if change == 'status' else True
    elif change == 'ambiguous':
        before['discovery']['ambiguous_controls'] = True
    elif change == 'wrong_site':
        before['discovery']['site'] = 'python_docs'
    elif change == 'not_submitted':
        before['discovery']['search_submitted'] = False
    elif change == 'unknown_click':
        history[-1]['result'].update(ok=False,state='unknown')
    else:
        before['title'] = 'Unhashed replacement'
    if change not in ('no_submit','no_before','before_hash','unknown_click'):
        refresh(before)
        if before['actions']:
            # Keep the candidate state link coherent; wrong query/session must
            # still fail independently of a stale candidate identifier.
            history[-1]['action']['state_id'] = before['state_id']
    with pytest.raises(ComputerError):
        derive(plan['goal']['browser'],history)


@pytest.mark.parametrize('record',['observation-03.json','step-03/choices.json','step-03/intent.json'])
def test_preclick_record_changes_after_preview_block_before_new_browser(ctx,record):
    _, prior, _ = stopped(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    path = Path(prior['run'])/record
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ComputerError):
        run(ctx,plan,browser_factory=lambda *a,**k:pytest.fail('changed ancestor opened browser'))


def test_unbound_observation_does_not_replace_changed_choice(ctx):
    _, prior, _ = stopped(ctx)
    root = Path(prior['run'])
    path = root/'step-03'/'intent.json'
    chosen = read_record(path)
    chosen['action']['target_url'] = 'https://docs.djangoproject.com/en/5.2/ref/settings/'
    path.write_text(json.dumps(chosen),encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']
