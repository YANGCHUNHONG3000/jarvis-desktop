"""Browser/native isolation only: real engine, durable evidence and Office bytes."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import interaction_recovery as recovery
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_agent_tools import _load
from test_computer_interaction import BROWSER, FakeBrowser, ctx, edits, prepare, run


class ProvenBrowser(FakeBrowser):
    def __init__(self, config, **options):
        super().__init__(config, **options)
        self.failed = True  # Only one legal browser action at each phase.

    def observe(self):
        browser = super().observe()
        browser['failures'] = []
        if self.config.get('mode') == 'github_repo':
            ready = self.phase >= 3
            browser['url'] = self.config['start_url'] if ready else 'https://github.com/search'
            browser['text'] = 'Verified browser result' if ready else 'Search form'
            browser['checks'] = {'goal_met':ready}
            if self.phase == 2:
                browser['actions'] = [{'kind':'browser_click','rule_id':'repo',
                                       'control_id':'repo-actual-dom'}]
            elif ready:
                browser['actions'] = []
            browser['human_required'] = False
            browser['discovery'] = {'mode':'github_repo', 'query_verified':ready,
                'repository_verified':ready, 'repository_identity_matches':True,
                'main_present':True, 'authenticated':True, 'repository_status':200,
                'repository_search_status':200, 'ambiguous_controls':False, 'controls_overflow':False}
        elif self.config.get('mode') == 'discover_query':
            browser['discovery'] = {'mode':'discover_query', 'javascript':True,
                'query_verified':self.phase == 2, 'phase':'verified' if self.phase == 2 else 'pending',
                'attempts':[{'state':'verified','requests':[{'method':'GET','finished':True,
                    'failed':False,'status':200,'url':'https://example.org/result'}]}] if self.phase == 2 else []}
        browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
        for action in browser['actions']:
            action['state_id'] = browser['state_id']
        return browser

    def execute(self, action):
        result = super().execute(action)
        result['verification'] = {'independent_read':True}
        if self.config.get('mode') == 'github_repo':
            result['code'] = {'query':'browser_fill_verified',
                              'submit':'browser_github_search_submitted',
                              'repo':'browser_github_repository_opened'}[action['rule_id']]
        return result


def stopped_after_word(ctx, *, config=BROWSER, repository_document=False):
    browsers = []
    def factory(config, **options):
        browser = ProvenBrowser(config, **options)
        browsers.append(browser)
        return browser
    def edit(spec, output, source_root, **options):
        result = ctx['execute'](spec, output, source_root, **options)
        ctx['stopped'][0] = True
        return result
    if repository_document:
        from agent_service.computer_interaction import prepare as prepare_interaction
        original = prepare_interaction(ctx['session'],'网页完成后编辑文件并交付仓库Word',
            browser=config,edits=edits(ctx),repository_document=True)
    else:
        original = prepare(ctx, browser=config)
    result = run(ctx, original, browser_factory=factory, office_execute=edit)
    ctx['stopped'][0] = False
    assert result['state'] == 'partial' and len(ctx['calls']) == 1
    return original, result, browsers[0]


@pytest.mark.parametrize('mode',['legacy','javascript','github'])
def test_captured_page_and_completed_word_resume_only_excel_without_browser(ctx, mode):
    config = deepcopy(BROWSER)
    if mode == 'javascript':
        config = {'mode':'discover_query','javascript':True,'start_url':'https://example.org/',
                  'query':'approved public query','required_text':['Verified browser result']}
    elif mode == 'github':
        config = {'mode':'github_repo','start_url':'https://github.com/example/repo',
                  'query':'repo user:example','required_text':['Verified browser result'], 'human_login':True}
    original, prior, browser = stopped_after_word(ctx,config=config)
    row = recovery.inspect(ctx['session'], prior['run'])
    assert row['eligible'] and row['completed_edits'] == ['e1']
    resumed = recovery.prepare(ctx['session'], prior['run'])['plan']
    assert resumed['goal'] == original['goal']
    assert '非当前实时页面' in recovery.describe(resumed)
    old_effects = deepcopy(browser.effects)
    def forbidden(*args, **options):
        raise AssertionError('A historical capture must not create a browser or handoff')
    result = run(ctx,resumed,browser_factory=forbidden,human_handoff=forbidden)
    assert result['ok'] and result['actions'] == 1 and len(result['artifacts']) == 2
    assert browser.effects == old_effects
    assert [v['kind'] for v in ctx['calls']] == ['document','spreadsheet']
    observation = read_record(Path(result['run'])/'observation-01.json')
    assert observation['browser']['historical_capture']['run'] == prior['run']
    assert observation['browser']['actions'] == []
    assert read_record(Path(prior['run'])/'outcome.json') == prior


@pytest.mark.parametrize('target',['checkpoint','observation','browser_result','browser_verification','browser_intent'])
def test_browser_evidence_tamper_after_preview_blocks_pending_edits(ctx,target):
    _, prior, _ = stopped_after_word(ctx)
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    root = Path(prior['run'])
    paths = {'checkpoint':root/'browser-checkpoint.json','observation':root/'observation-03.json',
        'browser_result':root/'step-02'/'environment-result.json',
        'browser_verification':root/'step-02'/'verification.json',
        'browser_intent':root/'step-02'/'environment-intent.json'}
    paths[target].write_bytes(paths[target].read_bytes()+b' ')
    with pytest.raises(ValueError):
        run(ctx,plan)
    assert len(ctx['calls']) == 1


def test_missing_completed_capture_and_unknown_browser_are_refused(ctx):
    prior = run(ctx,prepare(ctx),stop=lambda:True)
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_browser_read_not_completed'
    assert recovery.list_candidates(ctx['session'])[0]['eligible'] is False
    _, captured, _ = stopped_after_word(ctx)
    root = Path(captured['run'])
    record, _ = _load(root/'step-02'/'environment-result.json')
    record['result'].update(ok=False,state='unknown',code='browser_effect_unknown')
    # A valid saved page does not authorize replay past an unknown action.
    path = root/'step-02'/'environment-result.json'
    path.write_text(json.dumps({'data':record,'sha256':digest(record)}),encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']


def test_browser_checkpoint_is_inherited_across_cancelled_child_and_ancestor_tamper(ctx):
    _, prior, _ = stopped_after_word(ctx)
    for depth in (1,2):
        plan = recovery.prepare(ctx['session'],prior['run'])['plan']
        assert plan['recovery']['depth'] == depth
        prior = run(ctx,plan,stop=lambda:True,browser_factory=lambda *a,**k:pytest.fail('browser restarted'))
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    parent = Path(plan['recovery']['parent_run'])
    parent.joinpath('outcome.json').write_bytes(parent.joinpath('outcome.json').read_bytes()+b' ')
    with pytest.raises(ValueError):
        run(ctx,plan)
    assert len(ctx['calls']) == 1


def test_exact_native_lock_is_not_immutable_evidence(ctx):
    _, prior, _ = stopped_after_word(ctx)
    artifact = Path(prior['artifacts'][0]['path'])
    lock = artifact.parent/('~$'+artifact.name)
    lock.write_bytes(b'offline synthetic WPS lock')
    plan = recovery.prepare(ctx['session'],prior['run'])['plan']
    lock.write_bytes(b'changed ephemeral lock')
    assert run(ctx,plan)['ok']


@pytest.mark.parametrize('change',['text','session','unknown','no_independent_read'])
def test_coherent_wrapper_changes_do_not_replace_completed_read_proof(ctx,change):
    from hashlib import sha256
    _, prior, _ = stopped_after_word(ctx)
    root = Path(prior['run'])
    if change in {'text','session'}:
        path = root/'browser-checkpoint.json'
        record, _ = _load(path)
        browser = record['browser']
        if change == 'text':
            browser['text'] = 'different body without required terms'
            record['content_sha256'] = sha256(browser['text'].encode()).hexdigest()
        else:
            browser['session_id'] = 'other-browser'
        browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
        path.write_text(json.dumps({'data':record,'sha256':digest(record)}),encoding='utf-8')
        if change == 'session':
            # Keep the saved independent observation consistent too; action
            # records must still establish this exact owned browser session.
            path = root/'observation-03.json'
            observed = read_record(path)
            observed['browser'] = browser
            path.write_text(json.dumps(observed),encoding='utf-8')
    else:
        path = root/'step-02'/'environment-result.json'
        record, _ = _load(path)
        if change == 'unknown':
            record['result'].update(ok=False,state='unknown')
        else:
            record['result']['verification'] = {}
        path.write_text(json.dumps({'data':record,'sha256':digest(record)}),encoding='utf-8')
        path = root/'step-02'/'verification.json'
        path.write_text(json.dumps(record['result']),encoding='utf-8')
    assert not recovery.inspect(ctx['session'],root)['eligible']
    assert len(ctx['calls']) == 1


def test_repository_document_flag_without_completed_read_still_refuses(ctx):
    from agent_service.computer_interaction import prepare as prepare_interaction
    config = {'mode':'github_repo','start_url':'https://github.com/example/repo',
              'query':'repo user:example','required_text':['repo']}
    plan = prepare_interaction(ctx['session'],'读取仓库并交付Word',browser=config,repository_document=True)
    prior = run(ctx,plan,stop=lambda:True)
    assert recovery.inspect(ctx['session'],prior['run'])['code'] == 'recovery_browser_read_not_completed'


def successful_repository_child(ctx):
    config = {'mode':'github_repo','start_url':'https://github.com/example/repo',
              'query':'repo user:example','required_text':['Verified browser result']}
    _, parent, _ = stopped_after_word(ctx,config=config,repository_document=True)
    plan = recovery.prepare(ctx['session'],parent['run'])['plan']
    child = run(ctx,plan,browser_factory=lambda *a,**k:pytest.fail('browser restarted'))
    assert child['ok']
    return parent, child


def test_successful_repository_child_can_deliver_word_with_ancestor_bound_source(ctx):
    from agent_service import repository_document as repo
    from test_artifact_source import fake as native_fake
    parent, child = successful_repository_child(ctx)
    base = ctx['session'].root.parent
    # The partial parent is never accepted as a successful full goal source.
    with pytest.raises(ComputerError,match='repository_source_invalid'):
        repo.verify_repository_source(parent['run'],computer_base=base)
    source = repo.verify_repository_source(child['run'],computer_base=base)
    assert source['metadata']['recovery']['captured_run'] == parent['run']
    plan = repo.prepare_repository_document(child['run'],computer_base=base)
    def execute(**options):
        return repo.execute_repository_document(plan,approval=plan['sha256'],computer_base=base,
            task_base=ctx['task_base'],office_execute=native_fake,**options)
    result = execute()
    assert result['ok'] and execute() == result
    assert len(ctx['calls']) == 2
    assert recovery.inspect(ctx['session'],child['run'])['eligible'] is False


def test_recovered_repository_delivery_rechecks_ancestor_after_preview(ctx):
    from agent_service import repository_document as repo
    from test_artifact_source import fake as native_fake
    parent, child = successful_repository_child(ctx)
    base = ctx['session'].root.parent
    plan = repo.prepare_repository_document(child['run'],computer_base=base)
    path = Path(parent['run'])/'browser-checkpoint.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ComputerError,match='repository_source_invalid'):
        repo.execute_repository_document(plan,approval=plan['sha256'],computer_base=base,
            task_base=ctx['task_base'],office_execute=native_fake)
    assert not (Path(child['run'])/repo._CLAIM).exists()


@pytest.mark.parametrize('change',['status','method','pending'])
def test_javascript_goal_boolean_cannot_replace_finished_get_evidence(change):
    from agent_service.browser_checkpoint import verify_browser
    config = {'mode':'discover_query','javascript':True,'start_url':'https://example.org/',
              'query':'approved public query','required_text':['Verified browser result']}
    browser = ProvenBrowser(config,ready=True).observe()
    request = browser['discovery']['attempts'][-1]['requests'][0]
    if change == 'status':
        request['status'] = 503
    elif change == 'method':
        request['method'] = 'POST'
    else:
        request['finished'] = False
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'state_id','actions'}})
    with pytest.raises(ComputerError,match='recovery_browser_query_not_verified'):
        verify_browser(config,browser)
