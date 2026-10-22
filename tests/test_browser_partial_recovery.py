"""Durable partial GitHub phases; browser/native boundaries are offline only."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode
import uuid

import pytest

from agent_service import interaction_recovery as recovery
from agent_service.browser_resume import validate
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_interaction import prepare
from test_computer_interaction import ctx, edits, run


CONFIG = {'mode':'github_repo','start_url':'https://github.com/example/repo',
          'query':'repo user:example','required_text':['Verified browser result']}


class PartialBrowser:
    def __init__(self, config, *, resume=None, stopped=None, stop_phase=None, **options):
        self.config, self.resume = config, deepcopy(resume)
        self.phase = 0 if resume is None else {'search_results':2,'repository_opened':3}[resume['phase']]
        self.session = uuid.uuid4().hex
        self.stopped, self.stop_phase = stopped, stop_phase
        self.effects = []

    def observe(self):
        ready = self.phase == 4
        historical = self.resume is not None and self.resume['phase'] == 'repository_opened'
        search_url = 'https://github.com/search?' + urlencode({'q':self.config['query'],'type':'repositories'})
        discovery = {'mode':'github_repo','query_verified':self.phase >= 2,
            'repository_verified':ready,'repository_identity_matches':True,'main_present':True,
            'authenticated':False,'repository_status':200 if ready else None,
            'repository_proof':'fresh' if ready else None,
            'repository_search_status':None if historical or self.phase < 2 else 200,
            'ambiguous_controls':False,'controls_overflow':False}
        if self.resume is not None:
            discovery['resume'] = self.resume
            discovery['search_proof'] = 'historical' if historical else 'fresh'
        browser = {'session_id':self.session,'url':self.config['start_url'] if self.phase >= 3 else search_url,
            'text':'Verified browser result' if ready else '', 'locked':False,'human_required':False,
            'checks':{'goal_met':ready},'discovery':discovery}
        browser['state_id'] = digest(browser)
        browser['actions'] = [] if ready else [{'kind':'browser_fill' if self.phase == 0 else
            'browser_wait' if self.phase == 3 else 'browser_click',
            'rule_id':('query','submit','repo','wait')[self.phase],
            'control_id':self.session+'-'+str(self.phase),'state_id':browser['state_id']}]
        return browser

    def execute(self, action):
        assert action in self.observe()['actions']
        self.effects.append(deepcopy(action))
        code = ('browser_fill_verified','browser_github_search_submitted',
                'browser_github_repository_opened','browser_wait_observed')[self.phase]
        self.phase += 1
        if self.phase == self.stop_phase:
            self.stopped[0] = True
        return {'ok':True,'state':'verified','code':code,'verification':{'independent_read':True},
                'observation':self.observe()}

    def close(self):
        pass


def parent(ctx, phase=2, office=False):
    browsers = []
    def factory(config, **options):
        browser = PartialBrowser(config, stopped=ctx['stopped'],stop_phase=phase,**options)
        browsers.append(browser)
        return browser
    plan = prepare(ctx['session'],'继续GitHub读取并生成Word',browser=CONFIG,
                   edits=edits(ctx) if office else [],repository_document=True)
    prior = run(ctx,plan,browser_factory=factory)
    ctx['stopped'][0] = False
    assert prior['state'] == 'partial'
    assert not (Path(prior['run'])/'browser-checkpoint.json').exists()
    return prior, browsers[0]


@pytest.mark.parametrize('phase',[2,3])
@pytest.mark.parametrize('office',[False,True])
def test_partial_phase_new_session_only_remaining_actions_and_verified_delivery(ctx,phase,office):
    from agent_service.repository_document import verify_repository_source
    prior, browser = parent(ctx,phase,office)
    original = deepcopy(browser.effects)
    row = recovery.inspect(ctx['session'],prior['run'])
    assert row['eligible'], row
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    assert plan['recovery']['kind'] == 'interactive_browser_pending'
    result = run(ctx,plan,browser_factory=PartialBrowser)
    assert result['ok'], result
    codes = [a['result']['code'] for a in result['attempts'] if a['action']['kind'].startswith('browser_')]
    assert codes == (['browser_github_repository_opened','browser_wait_observed'] if phase == 2 else
                     ['browser_wait_observed'])
    assert browser.effects == original
    source = verify_repository_source(result['run'],computer_base=ctx['session'].root.parent)
    assert source['text'] == 'Verified browser result'
    assert source['metadata']['recovery']['captured_run'] == result['run']
    if phase == 3:
        assert source['metadata']['repository_search_status'] is None
        assert source['metadata']['search_proof'] == 'historical'


def test_partial_cursor_survives_cancelled_hops_and_rechecks_ancestor(ctx):
    prior, _ = parent(ctx)
    first = Path(prior['run'])
    for depth in (1,2):
        plan = recovery.prepare(ctx['session'],prior['run'])['plan']
        assert plan['recovery']['depth'] == depth
        prior = run(ctx,plan,stop=lambda:True,browser_factory=PartialBrowser)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    record = first/'step-02'/'environment-result.json'
    record.write_bytes(record.read_bytes()+b' ')
    with pytest.raises(ValueError):
        run(ctx,plan,browser_factory=PartialBrowser)


def test_repository_cursor_can_complete_on_fresh_observation_without_new_action(ctx):
    from agent_service import repository_document as repo
    from test_artifact_source import fake as native_fake
    prior, _ = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def fresh(config, **options):
        browser = PartialBrowser(config, **options)
        browser.phase = 4
        return browser
    result = run(ctx,plan,browser_factory=fresh)
    assert result['ok'] and result['actions'] == 0
    base = ctx['session'].root.parent
    document = repo.prepare_repository_document(result['run'],computer_base=base)
    delivered = repo.execute_repository_document(document,approval=document['sha256'],
        computer_base=base,task_base=ctx['task_base'],office_execute=native_fake)
    assert delivered['ok']


@pytest.mark.parametrize('change',['session','cursor','fresh_proof','ancestral_session'])
def test_zero_action_capture_cannot_replace_initial_session_or_approved_cursor(ctx,change):
    from agent_service.computer_agent_tools import _load
    import json
    prior, old_browser = parent(ctx,3)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def fresh(config, **options):
        browser = PartialBrowser(config,**options)
        browser.phase = 4
        return browser
    result = run(ctx,plan,browser_factory=fresh)
    root = Path(result['run'])
    checkpoint, _ = _load(root/'browser-checkpoint.json')
    browser = checkpoint['browser']
    if change == 'session':
        browser['session_id'] = 'unrelated-fresh-session'
    elif change == 'cursor':
        browser['discovery']['resume'] = None
    elif change == 'fresh_proof':
        browser['discovery']['repository_proof'] = 'historical'
    else:
        browser['session_id'] = old_browser.session
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    (root/'browser-checkpoint.json').write_text(
        json.dumps({'data':checkpoint,'sha256':digest(checkpoint)}),encoding='utf-8')
    observed = read_record(root/'observation-01.json')
    observed['browser'] = browser
    # A consistent extra observation does not replace the first new-session observation.
    (root/'observation-02.json').write_text(json.dumps(observed),encoding='utf-8')
    if change == 'ancestral_session':
        (root/'observation-01.json').write_text(json.dumps(observed),encoding='utf-8')
    with pytest.raises(ComputerError):
        recovery.verify_completed(ctx['session'],root)


def test_partial_phase_advances_across_multiple_browser_sessions(ctx):
    prior, _ = parent(ctx,2)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    def until_open(config, **options):
        return PartialBrowser(config,stopped=ctx['stopped'],stop_phase=3,**options)
    child = run(ctx,plan,browser_factory=until_open)
    ctx['stopped'][0] = False
    assert child['state'] == 'partial'
    value = recovery._inspect(ctx['session'],child['run'])
    assert value['browser_resume']['phase'] == 'repository_opened'
    plan = recovery.prepare(ctx['session'],child['run'])['plan']
    result = run(ctx,plan,browser_factory=PartialBrowser)
    assert result['ok']
    assert [a['result']['code'] for a in result['attempts']] == ['browser_wait_observed']


def test_ancestor_changes_during_new_session_are_rechecked_before_observation(ctx):
    prior, _ = parent(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    path = Path(prior['run'])/'outcome.json'
    browsers = []
    def changed(config, **options):
        browser = PartialBrowser(config,**options)
        browsers.append(browser)
        path.write_bytes(path.read_bytes()+b' ')
        return browser
    result = run(ctx,plan,browser_factory=changed)
    assert result['ok'] is False
    assert browsers[0].effects == []


@pytest.mark.parametrize('name',['environment-result.json','verification.json','environment-intent.json'])
def test_partial_missing_dispatched_receipt_refuses(ctx,name):
    prior, _ = parent(ctx)
    (Path(prior['run'])/'step-02'/name).unlink()
    assert not recovery.inspect(ctx['session'],prior['run'])['eligible']


def test_fill_only_has_no_restorable_cursor(ctx):
    prior, _ = parent(ctx,1)
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_browser_read_not_completed'


@pytest.mark.parametrize('change',[{'phase':'other'}, {'url':'https://github.com/example/other'},
                                 {'config_sha256':'0'*64}, {'version':True}])
def test_cursor_shape_or_target_change_refused(ctx,change):
    prior, _ = parent(ctx)
    value = recovery._inspect(ctx['session'],prior['run'])['browser_resume']
    with pytest.raises(ComputerError,match='recovery_browser_resume_invalid'):
        validate(read_record(Path(prior['run']).with_suffix('.json'))['goal']['browser'],{**value,**change})
