"""Offline public selection lineage; real engine and durable local evidence."""
from copy import deepcopy
from pathlib import Path
import json
import uuid

import pytest

from agent_service import interaction_recovery as recovery, page_office_delivery as page
from agent_service.browser_checkpoint import verify_browser, verify_history
from agent_service.browser_public import validate_public_config
from agent_service.browser_resume import derive, public_search_url, validate
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_agent_tools import _load
from agent_service.computer_interaction import prepare
from test_computer_interaction import ctx, edits, run
from test_artifact_source import fake as office_fake


TARGET = 'https://docs.python.org/3/library/pathlib.html'
OTHER = 'https://docs.python.org/3/library/os.html'
CONFIG = {'mode':'public_search','site':'python_docs',
          'start_url':'https://docs.python.org/3/','query':'pathlib',
          'required_text':['Verified browser result']}


class PublicBrowser:
    def __init__(self, config, *, resume=None, stopped=None, stop_phase=None, **options):
        self.config, self.resume = config, deepcopy(resume)
        self.phase = 0 if resume is None else {'search_results':2,'target_opened':3}[resume['phase']]
        self.session = uuid.uuid4().hex
        self.stopped, self.stop_phase = stopped, stop_phase
        self.effects = []

    def observe(self):
        ready = self.phase == 4
        historical = self.resume is not None and self.resume['phase'] == 'target_opened'
        target = TARGET if self.phase >= 3 else self.config['target_url']
        discovery = {'mode':'public_search','site':self.config['site'],
            'query_verified':self.phase >= 2,'content_verified':ready,'main_present':True,
            'phase':'verified' if ready else 'target_pending' if self.phase == 3 else 'search_results',
            'search_status':200 if self.phase >= 2 and not historical else None,
            'content_status':200 if ready else None, 'target_url':target,
            'search_finished':self.phase >= 2 and not historical,'content_finished':ready,
            'search_submitted':self.phase >= 2,'target_clicked':self.phase >= 3,
            'search_proof':'historical' if historical else 'fresh' if self.phase >= 2 else None,
            'content_proof':'fresh' if ready else None,'resume':deepcopy(self.resume),
            'ambiguous_controls':False,'controls_overflow':False}
        browser = {'session_id':self.session,'title':'Actual public title',
            'url':target if self.phase >= 3 else public_search_url(self.config) if self.phase >= 2 else self.config['start_url'],
            'text':'Verified browser result' if ready else '', 'locked':False,'human_required':False,
            'checks':{'goal_met':ready},'discovery':discovery}
        browser['state_id'] = digest(browser)
        browser['actions'] = [] if ready else [{'kind':'browser_fill' if self.phase == 0 else
            'browser_wait' if self.phase == 3 else 'browser_click',
            'rule_id':('query','submit','target','wait')[self.phase],
            'control_id':self.session+'-'+str(self.phase),'state_id':browser['state_id']}]
        if self.phase == 2:
            browser['actions'][0].update(target_url=TARGET,title='pathlib',snippet='Filesystem paths')
        return browser

    def execute(self, action):
        assert action in self.observe()['actions']
        self.effects.append(deepcopy(action))
        code = ('browser_fill_verified','browser_public_search_submitted',
                'browser_public_target_opened','browser_wait_observed')[self.phase]
        self.phase += 1
        if self.phase == self.stop_phase:
            self.stopped[0] = True
        return {'ok':True,'state':'verified','code':code,'verification':{'independent_read':True},
                'observation':self.observe()}

    def close(self):
        pass


def parent(ctx, phase=2, *, explicit=False, office=False):
    config = {**CONFIG, **({'target_url':TARGET} if explicit else {})}
    browsers = []
    def factory(config, **options):
        browser = PublicBrowser(config,stopped=ctx['stopped'],stop_phase=phase,**options)
        browsers.append(browser)
        return browser
    plan = prepare(ctx['session'],'Read selected public result and deliver files',browser=config,
                   edits=edits(ctx) if office else [],page_office=True)
    prior = run(ctx,plan,browser_factory=factory)
    ctx['stopped'][0] = False
    assert prior['state'] == 'partial', prior
    return prior, browsers[0]


@pytest.mark.parametrize('phase',[2,3])
@pytest.mark.parametrize('explicit',[False,True])
@pytest.mark.parametrize('office',[False,True])
def test_public_phase_continuation_retains_target_and_only_remaining_actions(ctx,phase,explicit,office):
    prior, old = parent(ctx,phase,explicit=explicit,office=office)
    effects = deepcopy(old.effects)
    inspected = recovery._inspect(ctx['session'],prior['run'])
    cursor = inspected['browser_resume']
    assert cursor['phase'] == ('search_results' if phase == 2 else 'target_opened')
    assert cursor['target_url'] == ('' if phase == 2 else TARGET)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    assert plan['goal']['page_office'] is True
    result = run(ctx,plan,browser_factory=PublicBrowser)
    assert result['ok'], result
    codes = [a['result']['code'] for a in result['attempts'] if a['action']['kind'].startswith('browser_')]
    assert codes == (['browser_public_target_opened','browser_wait_observed'] if phase == 2 else ['browser_wait_observed'])
    assert old.effects == effects
    source = page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)
    assert source['metadata']['url'] == TARGET
    assert source['metadata']['captured_run'] == result['run']
    final = recovery.verify_reading(ctx['session'],result['run'])['browser_checkpoint']['browser']
    assert final['discovery']['search_status'] == (200 if phase == 2 else None)
    assert final['discovery']['search_proof'] == ('fresh' if phase == 2 else 'historical')
    delivered = page.deliver_page_office(result,computer_base=ctx['session'].root.parent,
        task_base=ctx['task_base'],office_execute=office_fake)
    assert delivered['ok'], delivered
    assert page.deliver_page_office(result,computer_base=ctx['session'].root.parent,
        task_base=ctx['task_base'],office_execute=lambda *a:pytest.fail('replayed Office')) == delivered


def fresh(config, **options):
    browser = PublicBrowser(config,**options)
    browser.phase = 4
    return browser


class DelayedBrowser(PublicBrowser):
    def execute(self, action):
        result = super().execute(action)
        if result['code'] == 'browser_public_target_opened':
            browser = result['observation']
            browser['url'] = public_search_url(self.config)
            browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
            for candidate in browser['actions']:
                candidate['state_id'] = browser['state_id']
        return result


class NoWaitBrowser(DelayedBrowser):
    def execute(self, action):
        result = super().execute(action)
        if result['code'] == 'browser_public_target_opened':
            self.phase = 4  # Completion first appears in the next loop observation.
        return result


class BlockedInitialBrowser(PublicBrowser):
    def observe(self):
        browser = super().observe()
        browser.update(url=self.config['start_url'],title='Public website',text='',
            controls=[],completed_rules=[],actions=[],locked=True,
            failures=[{'code':'browser_public_method_blocked'}],checks={'goal_met':False})
        browser['discovery'].update(phase='halted',query_verified=False,content_verified=False,
            main_present=False,content_proof=None,content_status=200,
            observation_retries=0,last_observation_error=None)
        browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
        return browser


def blocked_initial(ctx):
    prior, old = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    result = run(ctx,plan,browser_factory=BlockedInitialBrowser)
    assert result['state'] == 'stopped' and result['code'] == 'agent_no_safe_next_action'
    assert result['actions'] == 0 and result['attempts'] == []
    return prior, old, result


def test_denied_zero_action_bootstrap_claims_child_and_preserves_failed_snapshot(ctx):
    prior, old, blocked = blocked_initial(ctx)
    root = Path(blocked['run'])
    observation = read_record(root/'observation-01.json')
    assert 'blocked_requests' not in observation['browser']['discovery']
    assert observation['browser']['locked'] is True
    assert observation['browser']['checks'] == {'goal_met':False}
    before = {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
    inspected = recovery._inspect(ctx['session'],root)
    assert inspected['rejected_initial_read']
    assert inspected['browser_checkpoint'] is None
    assert inspected['browser_resume']['phase'] == 'target_opened'
    assert not recovery.inspect(ctx['session'],prior['run'])['eligible']
    prepared = recovery.prepare(ctx['session'],root)
    assert '初始读取被明确拒绝' in prepared['reason']
    result = run(ctx,prepared['plan'],browser_factory=fresh)
    assert result['ok'] and result['actions'] == 0
    assert read_record(root/'continuation-claim.json') == {'child':prepared['plan']['sha256']}
    assert all((root/name).read_bytes() == value for name,value in before.items())
    assert len(old.effects) == 3
    assert page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)['metadata']['url'] == TARGET
    with pytest.raises(ComputerError,match='recovery_already_claimed'):
        recovery.prepare(ctx['session'],root)


@pytest.mark.parametrize('change',['captcha','unknown_failure','additional_failure','human','goal',
    'cursor','hash','ancestral_session','partial','unknown','actions','intent','source_read','output','office_claim','second_observation'])
def test_bootstrap_denial_exception_does_not_allow_other_failures_or_effects(ctx,change):
    _, old, blocked = blocked_initial(ctx)
    root = Path(blocked['run'])
    path = root/'observation-01.json'
    observed = read_record(path)
    browser = observed['browser']
    outcome = read_record(root/'outcome.json')
    if change in ('captcha','unknown_failure'):
        browser['failures'] = [{'code':'browser_public_auth_or_challenge' if change == 'captcha' else 'browser_effect_unknown'}]
    elif change == 'additional_failure':
        browser['failures'].append({'code':'browser_public_auth_or_challenge'})
    elif change == 'human':
        browser['human_required'] = True
    elif change == 'goal':
        browser['checks']['goal_met'] = True
    elif change == 'cursor':
        browser['discovery']['resume']['target_url'] = OTHER
    elif change == 'hash':
        browser['title'] = 'Changed without digest'
    elif change == 'ancestral_session':
        browser['session_id'] = old.session
    elif change in ('partial','unknown'):
        outcome['state'] = change
    elif change == 'actions':
        outcome['actions'] = 1
    elif change == 'intent':
        step = root/'step-01'
        step.mkdir()
        (step/'environment-intent.json').write_text('{}',encoding='utf-8')
    elif change == 'source_read':
        outcome['source_reads'] = 1
    elif change == 'output':
        outcome['outputs'] = {'unexpected':'effect'}
    elif change == 'office_claim':
        delivery = root.parent.parent/'page-deliveries'/root.stem
        delivery.mkdir(parents=True)
        (delivery/'claim.json').write_text('{}',encoding='utf-8')
    else:
        (root/'observation-02.json').write_text(json.dumps(observed),encoding='utf-8')
    if change != 'hash':
        browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    path.write_text(json.dumps(observed),encoding='utf-8')
    (root/'outcome.json').write_text(json.dumps(outcome),encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']


def test_bootstrap_denial_keeps_lease_and_ancestor_tamper_protections(ctx):
    from agent_service.run_lifecycle import _lock, _unlock
    prior, _, blocked = blocked_initial(ctx)
    root = Path(blocked['run'])
    with (root/'owner.lease').open('r+b',buffering=0) as stream:
        _lock(stream)
        try:
            assert recovery.inspect(ctx['session'],root)['code'] == 'run_owner_active'
        finally:
            _unlock(stream)
    plan = recovery.prepare(ctx['session'],root)['plan']
    path = Path(prior['run'])/'outcome.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ComputerError):
        run(ctx,plan,browser_factory=fresh)


@pytest.mark.parametrize('resumed',[False,True])
def test_target_completed_in_next_loop_observation_without_wait_delivers(ctx,resumed):
    if resumed:
        prior, _ = parent(ctx,2)
        plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    else:
        plan = prepare(ctx['session'],'Read selected page',browser=CONFIG,page_office=True)
    result = run(ctx,plan,browser_factory=NoWaitBrowser)
    assert result['ok'], result
    browser_attempts = [a for a in result['attempts'] if a['action']['kind'].startswith('browser_')]
    assert browser_attempts[-1]['result']['code'] == 'browser_public_target_opened'
    assert browser_attempts[-1]['result']['observation']['url'] == public_search_url(plan['goal']['browser'])
    assert not any(a['result']['observation']['checks']['goal_met'] for a in browser_attempts)
    assert all(a['action']['kind'] != 'browser_wait' for a in browser_attempts)
    source = page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)
    assert source['metadata']['url'] == TARGET
    delivered = page.deliver_page_office(result,computer_base=ctx['session'].root.parent,
        task_base=ctx['task_base'],office_execute=office_fake)
    assert delivered['ok'], delivered


@pytest.mark.parametrize('resumed',[False,True])
def test_next_observation_capture_recovers_only_remaining_office(ctx,resumed):
    if resumed:
        prior, _ = parent(ctx,2,office=True)
        plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    else:
        plan = prepare(ctx['session'],'Read then edit',browser=CONFIG,edits=edits(ctx),page_office=True)
    def edit(spec, output, source_root, **options):
        result = ctx['execute'](spec,output,source_root,**options)
        ctx['stopped'][0] = True
        return result
    partial = run(ctx,plan,browser_factory=NoWaitBrowser,office_execute=edit)
    ctx['stopped'][0] = False
    assert partial['state'] == 'partial' and len(ctx['calls']) == 1, partial
    inspected = recovery._inspect(ctx['session'],partial['run'])
    assert inspected['browser_checkpoint'] is not None and inspected['browser_resume'] is None
    plan = recovery.prepare(ctx['session'],partial['run'])['plan']
    result = run(ctx,plan,browser_factory=lambda *a,**k:pytest.fail('captured page reopened'))
    assert result['ok'] and result['actions'] == 1 and len(ctx['calls']) == 2
    assert page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)['metadata']['url'] == TARGET


def test_next_observation_completion_needs_record_and_current_selected_session(ctx):
    plan = prepare(ctx['session'],'Read selected page',browser=CONFIG,page_office=True)
    result = run(ctx,plan,browser_factory=NoWaitBrowser)
    root = Path(result['run'])
    checkpoint, _ = _load(root/'browser-checkpoint.json')
    attempts = deepcopy(result['attempts'])
    attempts[-1]['result']['observation']['session_id'] = 'unrelated-click-session'
    with pytest.raises(ComputerError):
        verify_history(plan['goal']['browser'],checkpoint,attempts)
    for path in root.glob('observation-*.json'):
        observed = read_record(path)
        if observed.get('browser') == checkpoint['browser']:
            observed['browser']['text'] = 'Different observed content'
            path.write_text(json.dumps(observed),encoding='utf-8')
    with pytest.raises(ComputerError,match='page_office_source_invalid'):
        page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)


def test_delayed_target_navigation_wait_proves_phase_and_office_delivery(ctx):
    prior, _ = parent(ctx,2)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    result = run(ctx,plan,browser_factory=DelayedBrowser)
    assert result['ok'], result
    assert result['attempts'][0]['result']['observation']['url'] == public_search_url(plan['goal']['browser'])
    source = page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)
    assert source['metadata']['url'] == TARGET
    delivered = page.deliver_page_office(result,computer_base=ctx['session'].root.parent,
        task_base=ctx['task_base'],office_execute=office_fake)
    assert delivered['ok'], delivered


def test_acknowledged_target_without_observed_navigation_never_reverts_to_search(ctx):
    prior, _ = parent(ctx,3)
    attempts = deepcopy(prior['attempts'])
    config = read_record(Path(prior['run']).with_suffix('.json'))['goal']['browser']
    attempts[-1]['result']['observation']['url'] = public_search_url(config)
    with pytest.raises(ComputerError,match='recovery_browser_read_not_completed'):
        derive(config,attempts)


def test_target_resume_zero_action_capture_is_a_delivery_source(ctx):
    prior, _ = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    result = run(ctx,plan,browser_factory=fresh)
    assert result['ok'] and result['actions'] == 0
    source = page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)
    assert source['metadata']['url'] == TARGET
    assert read_record(Path(prior['run'])/'outcome.json') == prior


@pytest.mark.parametrize('change',['session','cursor','fresh_proof','ancestral_session','target'])
def test_zero_action_public_capture_cannot_replace_session_or_approved_target(ctx,change):
    prior, old = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    result = run(ctx,plan,browser_factory=fresh)
    root = Path(result['run'])
    checkpoint, _ = _load(root/'browser-checkpoint.json')
    browser = checkpoint['browser']
    if change == 'session':
        browser['session_id'] = 'unrelated-session'
    elif change == 'cursor':
        browser['discovery']['resume'] = None
    elif change == 'fresh_proof':
        browser['discovery']['content_proof'] = 'historical'
    elif change == 'target':
        browser['url'] = browser['discovery']['target_url'] = OTHER
    else:
        browser['session_id'] = old.session
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    (root/'browser-checkpoint.json').write_text(json.dumps({'data':checkpoint,'sha256':digest(checkpoint)}),encoding='utf-8')
    observed = read_record(root/'observation-01.json')
    observed['browser'] = browser
    (root/'observation-02.json').write_text(json.dumps(observed),encoding='utf-8')
    if change == 'ancestral_session':
        (root/'observation-01.json').write_text(json.dumps(observed),encoding='utf-8')
    with pytest.raises(ComputerError):
        recovery.verify_reading(ctx['session'],root)


def test_public_phase_advances_across_sessions_and_keeps_ancestor_bytes(ctx):
    prior, old = parent(ctx,2)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def until_open(config, **options):
        return PublicBrowser(config,stopped=ctx['stopped'],stop_phase=3,**options)
    child = run(ctx,plan,browser_factory=until_open)
    ctx['stopped'][0] = False
    assert recovery._inspect(ctx['session'],child['run'])['browser_resume']['target_url'] == TARGET
    plan = recovery.prepare(ctx['session'],child['run'])['plan']
    result = run(ctx,plan,browser_factory=fresh)
    assert result['ok'] and result['actions'] == 0
    assert len(old.effects) == 2
    assert page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)['metadata']['url'] == TARGET
    path = Path(prior['run'])/'step-02'/'verification.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ComputerError):
        page.verify_page_source(result['run'],computer_base=ctx['session'].root.parent)


def test_zero_action_interrupted_session_cannot_be_reused_by_next_child(ctx):
    prior, _ = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    sessions = []
    def observe_then_stop(config, **options):
        browser = PublicBrowser(config,**options)
        sessions.append(browser.session)
        original = browser.observe
        def observe():
            result = original()
            ctx['stopped'][0] = True
            return result
        browser.observe = observe
        return browser
    child = run(ctx,plan,browser_factory=observe_then_stop)
    ctx['stopped'][0] = False
    assert child['state'] in ('partial','stopped') and child['actions'] == 0
    plan = recovery.prepare(ctx['session'],child['run'])['plan']
    def reused(config, **options):
        browser = fresh(config,**options)
        browser.session = sessions[0]
        return browser
    result = run(ctx,plan,browser_factory=reused)
    assert result['ok'], result
    with pytest.raises(ComputerError,match='recovery_browser_resume_changed'):
        recovery.verify_reading(ctx['session'],result['run'])


@pytest.mark.parametrize('change',[{'version':True},{'phase':'filled'}, {'target_url':OTHER},
    {'url':'https://evil.example/'}, {'config_sha256':'0'*64}, {'cookie':'forbidden'}, {'phase':[]}])
def test_public_cursor_rejects_changed_contract(change):
    config = validate_public_config(CONFIG)
    cursor = {'version':1,'mode':'public_search','phase':'search_results','url':public_search_url(config),
              'target_url':'','config_sha256':digest(config)}
    assert validate(config,cursor) == cursor
    with pytest.raises(ComputerError,match='recovery_browser_resume_invalid'):
        validate(config,{**cursor,**change})


@pytest.mark.parametrize('change',['duplicate_submit','duplicate_target','wrong_target','wrong_url','missing_target','unproven_search'])
def test_public_history_cannot_replay_or_substitute_target(ctx,change):
    prior, _ = parent(ctx,3)
    attempts = deepcopy(prior['attempts'])
    if change == 'duplicate_submit':
        attempts.insert(2,deepcopy(attempts[1]))
    elif change == 'duplicate_target':
        attempts.append(deepcopy(attempts[2]))
    elif change == 'wrong_target':
        attempts[2]['action']['target_url'] = OTHER
    elif change == 'wrong_url':
        attempts[2]['result']['observation']['url'] = OTHER
    elif change == 'missing_target':
        del attempts[2]['action']['target_url']
    else:
        attempts[1]['result']['observation']['discovery']['search_finished'] = False
    config = read_record(Path(prior['run']).with_suffix('.json'))['goal']['browser']
    with pytest.raises(ComputerError):
        derive(config,attempts)


@pytest.mark.parametrize('name',['environment-result.json','verification.json','environment-intent.json'])
def test_public_missing_dispatch_receipt_refuses(ctx,name):
    prior, _ = parent(ctx)
    (Path(prior['run'])/'step-02'/name).unlink()
    assert not recovery.inspect(ctx['session'],prior['run'])['eligible']


def test_public_fill_only_and_unknown_do_not_establish_phase(ctx):
    prior, _ = parent(ctx,1)
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_browser_read_not_completed'


@pytest.mark.parametrize('change',['fresh_status','historical_status','wrong_resume_phase','wrong_target','missing_fresh_content'])
def test_public_complete_capture_requires_matching_fresh_and_historical_proofs(change):
    config = validate_public_config(CONFIG)
    cursor = {'version':1,'mode':'public_search','phase':'target_opened','url':TARGET,
              'target_url':TARGET,'config_sha256':digest(config)}
    browser = fresh(config,resume=cursor).observe()
    verify_browser(config,browser)
    discovery = browser['discovery']
    if change == 'fresh_status':
        discovery.update(search_proof='fresh',search_status=None)
    elif change == 'historical_status':
        discovery['search_status'] = 200
    elif change == 'wrong_resume_phase':
        discovery['resume'].update(phase='search_results',url=public_search_url(config),target_url='')
    elif change == 'wrong_target':
        discovery['target_url'] = OTHER
    else:
        discovery['content_proof'] = None
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    with pytest.raises(ComputerError):
        verify_browser(config,browser)
