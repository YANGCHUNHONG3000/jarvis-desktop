"""Real owned Edge with isolated public-site response fixtures; no live internet."""
from types import SimpleNamespace
import pytest
from agent_service.browser_public import PublicSearchEdgeSession, validate_public_config, validate_public_target
from agent_service.browser_interaction import InteractionError, _digest


def config(site='python_docs', **extra):
    docs=site=='python_docs'
    return {'mode':'public_search','site':site,'start_url':'https://docs.python.org/3/' if docs else 'https://www.python.org/',
        'target_url':'https://docs.python.org/3/library/pathlib.html' if docs else 'https://www.python.org/downloads/release/python-340/',
        'query':'pathlib','required_text':['approved document content'],'headless':True,**extra}


@pytest.mark.parametrize('change',[
    {'site':'wikipedia'},{'start_url':'https://www.python.org/search/'},
    {'target_url':'https://evil.example/3/library/pathlib.html'},
    {'target_url':'https://docs.python.org/3/library/pathlib.html?q=a'},
    {'target_url':'https://docs.python.org/3/library/pathlib.html#x'},
    {'target_url':'https://docs.python.org/3/a/../library/pathlib.html'},
    {'target_url':'https://docs.python.org/3/%2e%2e/library/pathlib.html'},
    {'target_url':'https://docs.python.org/3/search.html'},
    {'query':''},{'query':'x'*201},{'required_text':[]},{'headless':1},{'resume':{}},
])
def test_config_scope(change):
    with pytest.raises((InteractionError,ValueError)):
        validate_public_config(config(**change))


def test_config_detached_and_default_headless():
    raw=config();raw.pop('headless');actual=validate_public_config(raw)
    raw['required_text'].append('later')
    assert actual['headless'] is False and len(actual['required_text'])==1


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr('agent_service.browser_public.socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('151.101.0.223',443))])
    monkeypatch.setattr('agent_service.research._public_search_proxy',lambda:None)
    class Fixture(PublicSearchEdgeSession):
        search_code=content_code=200
        duplicate_input=False
        duplicate_form=False
        duplicate_target=False
        result_form_extra=''
        result_form_action=None
        wrong_link=False
        challenge=False
        document_title='Actual public fixture title'
        requests=None
        def _route(self,route):
            req=route.request
            if self.requests is None:self.requests=[]
            self.requests.append((req.method,req.url,req.resource_type))
            if not self._request_allowed(req):return super()._route(route)
            if req.is_navigation_request():
                if self.search_pending and self._is_search(req.url):self.search_request=req
                elif self.content_pending and req.url==self.target_url:self.content_request=req
            else:return route.fulfill(status=200,content_type='text/javascript',body='')
            docs=self.config['site']=='python_docs'
            form='<form method=get action="'+('/3/search.html' if docs else '/search/')+'"><input name=q type=search><'+('input type=submit value=Go' if docs else 'button type=submit name=submit value=""')+'>'+('' if docs else 'GO</button>')+'</form>'
            status=200
            if req.url==self.config['start_url']:
                if self.duplicate_input:
                    form=form.replace('<input name=q type=search>','<input name=q type=search><input name=q type=search>')
                body=form+ (form if self.duplicate_form else '')
                if self.challenge:body+='<div id=captcha-container>SECRET CHALLENGE TEXT</div>'
            elif self._is_search(req.url):
                target=(self.config['target_url'] or config(self.config['site'])['target_url'])+('#module-pathlib' if docs else '')
                if self.wrong_link:target=target.replace('pathlib','os').replace('python-340','python-345')
                link='<a href="'+target+'">pathlib</a>'
                result_form=form.replace('</form>',self.result_form_extra+'</form>')
                if self.result_form_action:
                    result_form=result_form.replace('action="'+('/3/search.html' if docs else '/search/')+'"','action="'+self.result_form_action+'"')
                body=result_form+'<div '+('id="search-results"' if docs else 'class="list-recent-events"')+'>'+link+(link if self.duplicate_target else '')+'</div><main>approved document content in results is not proof</main>'
                status=self.search_code
            else:
                body='<div '+('class="body"' if docs else 'id="content"')+'>approved document content '+'x'*25000+'</div>'
                status=self.content_code
            route.fulfill(status=status,content_type='text/html',body='<title>'+self.document_title+'</title>'+body)
    return Fixture


def drive(s):
    events=[]
    for _ in range(15):
        obs=s.observe()
        if obs['checks']['goal_met'] or not obs['actions']:return obs,events
        events.append(s.execute(obs['actions'][0]))
    return s.observe(),events


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_actual_edge_search_open_content(fixture,site):
    s=fixture(config(site))
    try:
        obs,events=drive(s)
        assert obs['checks']['goal_met'] and obs['actions']==[]
        assert obs['url']==s.config['target_url'] and len(obs['text'])==24000
        assert obs['title']=='Actual public fixture title'
        d=obs['discovery']
        assert d['phase']=='verified'
        assert all(d[x] for x in ['query_verified','content_verified','main_present','search_finished','content_finished','search_submitted','target_clicked'])
        assert d['search_status']==d['content_status']==200
        assert [e['code'] for e in events if e['code']!='browser_wait_observed']==['browser_fill_verified','browser_public_search_submitted','browser_public_target_opened']
        assert sum(s._is_search(u) for m,u,t in s.requests)==1
        assert sum(u==s.config['target_url'] for m,u,t in s.requests)==1
    finally:s.close()


@pytest.mark.parametrize('kind',['search_code','content_code'])
def test_non_200_never_verifies(fixture,kind):
    setattr(fixture,kind,503)
    s=fixture(config())
    try:
        obs,_=drive(s)
        assert not obs['checks']['goal_met'] and obs['text']==''
        if kind=='search_code':assert not s.content_clicked
    finally:s.close()


def test_multiple_controls_stop_but_duplicate_exact_target_is_one_effect(fixture):
    fixture.duplicate_input=True
    s=fixture(config())
    try:
        obs=s.observe();assert obs['discovery']['ambiguous_controls'] and obs['actions']==[]
    finally:s.close()
    fixture.duplicate_input=False;fixture.duplicate_target=True
    s=fixture(config())
    try:assert drive(s)[0]['checks']['goal_met']
    finally:s.close()


def test_unapproved_result_not_clickable(fixture):
    fixture.wrong_link=True;s=fixture(config())
    try:
        obs,_=drive(s);assert not obs['checks']['goal_met'] and not s.content_clicked
    finally:s.close()


def test_challenge_halts_without_body(fixture):
    fixture.challenge=True;s=fixture(config())
    try:
        obs=s.observe();assert obs['locked'] and obs['actions']==[] and obs['text']==''
        assert obs['failures'][-1]['code']=='browser_public_auth_or_challenge'
    finally:s.close()


def request(s,url,method='GET',kind='script',navigation=False):
    return SimpleNamespace(url=url,method=method,resource_type=kind,frame=s.page.main_frame,is_navigation_request=lambda:navigation)


def test_network_contract(fixture):
    s=fixture(config())
    try:
        assert s._request_allowed(request(s,'https://docs.python.org/3/_static/searchtools.js'))
        assert s._request_allowed(request(s,'https://docs.python.org/3/_static/doctools.js?v=abcdef12'))
        assert s._request_allowed(request(s,'https://docs.python.org/3/searchindex.js'))
        for url,method,kind in [
            ('https://evil.example/a.js','GET','script'),('https://docs.python.org/3/_static/a.js?key=secret','GET','script'),
            ('https://docs.python.org/3/_static/a.js?v=abcdef12&v=abcdef12','GET','script'),
            ('https://docs.python.org/3/library/pathlib.html','GET','fetch'),
            ('https://docs.python.org/3/_static/a.js','POST','script')]:
            assert not s._request_allowed(request(s,url,method,kind))
        assert not s._is_search('https://docs.python.org/3/search.html?q=pathlib&q=pathlib')
        assert not s._request_allowed(request(s,s.config['target_url'],kind='document',navigation=True))
    finally:s.close()


def test_navigation_read_retries_do_not_repeat_dispatch(fixture):
    from playwright.sync_api import Error
    s=fixture(config());original=s._observe_once;injected=[False]
    def once():
        if s.search_pending and not injected[0]:
            injected[0]=True
            raise Error('Execution context was destroyed, most likely because of a navigation')
        return original()
    s._observe_once=once
    try:
        obs,events=drive(s)
        assert obs['checks']['goal_met'] and injected[0] and s.observation_retries>=1
        assert sum(e['code']=='browser_public_search_submitted' for e in events)==1
        assert sum(s._is_search(u) for m,u,t in s.requests)==1
    finally:s.close()



def test_unrelated_responses_cannot_establish_http_proof(fixture):
    s=fixture(config())
    try:
        unrelated=request(s,s.config['target_url'],kind='fetch')
        s.search_pending=s.content_pending=s.content_clicked=True
        s._response(SimpleNamespace(request=unrelated,status=200))
        s._finished(unrelated)
        assert s.search_status is None and s.content_status is None
        assert not s.search_finished and not s.content_finished
        assert not s.observe()['checks']['goal_met']
    finally:s.close()


def test_exhausted_observation_is_quiet_and_locked(fixture):
    from playwright.sync_api import Error
    s=fixture(config());calls=[]
    def unavailable():
        calls.append(1)
        raise Error('Execution context was destroyed')
    s._observe_once=unavailable
    try:
        obs=s.observe()
        assert len(calls)==3 and obs['locked'] and obs['text']=='' and obs['actions']==[]
        assert s.observe()['locked'] and len(calls)==3
    finally:s.close()


@pytest.mark.parametrize('stage',['search','content'])
def test_same_url_replacement_navigation_cannot_reuse_old_success(fixture,stage):
    s=fixture(config())
    try:
        if stage=='search':
            for _ in range(10):
                observation=s.observe()
                if s.query_verified:break
                s.execute(observation['actions'][0])
            assert s.query_verified and s.search_finished and s.search_status==200
            old=s.search_request
            url=old.url
        else:
            assert drive(s)[0]['checks']['goal_met']
            old=s.content_request
            url=s.config['target_url']
        newer=request(s,url,kind='document',navigation=True)
        continued=[];aborted=[]
        route=SimpleNamespace(request=newer,continue_=lambda:continued.append(True),abort=lambda:aborted.append(True))
        PublicSearchEdgeSession._route(s,route)
        assert aborted==[True] and not continued and s.locked
        # Old completion can arrive late but cannot authorize a new DOM/load.
        s._response(SimpleNamespace(request=old,status=200))
        s._finished(old)
        observation=s.observe()
        assert not observation['checks']['goal_met'] and observation['actions']==[] and observation['text']==''
        assert getattr(s,stage+'_request') is old
    finally:s.close()


@pytest.mark.parametrize('stage',['search','content'])
def test_new_request_binding_resets_proof_and_ignores_old_late_events(fixture,stage):
    s=fixture(config())
    try:
        s.search_pending=True
        if stage=='content':
            s.content_pending=s.content_clicked=s.query_verified=True
            url=s.config['target_url']
        else:
            url='https://docs.python.org/3/search.html?q=pathlib'
        setattr(s,stage+'_status',200)
        setattr(s,stage+'_finished',True)
        s.content_verified=True
        newer=request(s,url,kind='document',navigation=True)
        old=request(s,url,kind='document',navigation=True)
        continued=[]
        route=SimpleNamespace(request=newer,continue_=lambda:continued.append(True),abort=lambda:pytest.fail('first request rejected'))
        PublicSearchEdgeSession._route(s,route)
        assert continued==[True]
        assert getattr(s,stage+'_request') is newer
        assert getattr(s,stage+'_status') is None and not getattr(s,stage+'_finished')
        assert not s.content_verified
        s._response(SimpleNamespace(request=old,status=200));s._finished(old)
        assert getattr(s,stage+'_status') is None and not getattr(s,stage+'_finished')
        s._response(SimpleNamespace(request=newer,status=200))
        assert not getattr(s,stage+'_finished') and not s.observe()['checks']['goal_met']
    finally:s.close()



@pytest.mark.parametrize('title',['','Long title '*40])
def test_capture_title_is_current_bounded_document_title(fixture,title):
    fixture.document_title=title
    s=fixture(config())
    try:
        observation,_=drive(s)
        assert observation['checks']['goal_met']
        assert observation['title']==title.strip()[:300]
        assert observation['title']!='Public website document'
    finally:s.close()


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_duplicate_equivalent_forms_choose_one_complete_instance(fixture,site):
    fixture.duplicate_form=True
    s=fixture(config(site))
    try:
        observation=s.observe()
        assert observation['discovery']['candidate_targets']['input']==2
        assert observation['discovery']['candidate_targets']['submit']==2
        assert not observation['discovery']['ambiguous_controls']
        assert len(observation['actions'])==1 and observation['actions'][0]['kind']=='browser_fill'
        reply=s.execute(observation['actions'][0])
        assert reply['code']=='browser_fill_verified'
        assert s.page.locator('input[name=q]').evaluate_all('es=>es.map(e=>e.value)')==['pathlib','']
        observation=s.observe()
        assert len(observation['actions'])==1 and observation['actions'][0]['kind']=='browser_click'
        reply=s.execute(observation['actions'][0])
        assert reply['code']=='browser_public_search_submitted'
        final,events=drive(s)
        assert final['checks']['goal_met']
        assert sum(s._is_search(u) for m,u,t in s.requests)==1
    finally:s.close()


@pytest.mark.parametrize('mutation',[
    "f.insertAdjacentHTML('beforeend','<input name=q type=search>')",
    "f.insertAdjacentHTML('beforeend','<input type=submit value=Go>')",
    "f.insertAdjacentHTML('beforeend','<input type=hidden name=q value=other>')",
    "f.insertAdjacentHTML('beforeend','<input type=hidden name=extra value=other>')",
    "f.querySelector('[type=submit]').setAttribute('formaction','/3/other.html')",
    "const clone=f.cloneNode(true);clone.action='/3/other.html';document.body.append(clone)",
    "const clone=f.cloneNode(true);clone.querySelector('input[name=q]').remove();f.querySelector('[type=submit]').remove();document.body.append(clone)",
])
def test_forms_with_different_effects_or_incomplete_pairs_remain_ambiguous(fixture,mutation):
    s=fixture(config())
    try:
        s.page.locator('form').evaluate('(f)=>{'+mutation+'}')
        observation=s.observe()
        assert observation['discovery']['ambiguous_controls']
        assert observation['actions']==[] and not s.search_pending
        assert not any(s._is_search(u) for m,u,t in s.requests)
    finally:s.close()


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_result_page_unrelated_search_form_cannot_block_approved_target(fixture,site):
    fixture.result_form_extra='<input type=hidden name=check_keywords value=yes><input type=hidden name=area value=default><input name=q type=search>'
    fixture.result_form_action='/search/?different=parameters'
    s=fixture(config(site))
    try:
        for _ in range(12):
            observation=s.observe()
            if observation['discovery']['query_verified']:break
            assert observation['actions']
            s.execute(observation['actions'][0])
        assert observation['discovery']['query_verified']
        assert not observation['discovery']['ambiguous_controls']
        assert observation['discovery']['candidate_targets']=={'input':0,'submit':0,'target':1}
        assert len(observation['actions'])==1 and observation['actions'][0]['kind']=='browser_click'
        final,events=drive(s)
        assert final['checks']['goal_met'] and not final['discovery']['ambiguous_controls']
        assert final['discovery']['candidate_targets']=={'input':0,'submit':0,'target':0}
        assert sum(e['code']=='browser_public_target_opened' for e in events)==1
        assert sum(s._is_search(u) for m,u,t in s.requests)==1
    finally:s.close()


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_optional_target_normalizes_without_widening_document_contract(site):
    raw=config(site,target_url='');missing=dict(raw);missing.pop('target_url')
    assert validate_public_config(raw)==validate_public_config(missing)
    target=config(site)['target_url']
    assert validate_public_target(raw,target)==target
    for url in [raw['start_url'],target+'?x=y',target+'#fragment',
            target.replace('https:','http:'),target.replace('.org/','.org.evil/'),target+'?']:
        with pytest.raises(InteractionError):validate_public_target(raw,url)


def search_results(session):
    for _ in range(12):
        obs=session.observe()
        if obs['discovery']['query_verified']:return obs
        assert obs['actions']
        session.execute(obs['actions'][0])
    pytest.fail('search did not complete')


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_automatic_target_is_bound_to_selected_issued_action(fixture,site):
    s=fixture(config(site,target_url=''))
    try:
        obs=search_results(s)
        assert len(obs['actions'])==1
        action=obs['actions'][0]
        assert action['target_url']==config(site)['target_url'] and action['title']=='pathlib'
        assert action['state_id']==obs['state_id'] and len(action['snippet'])<=400
        assert obs['discovery']['target_url']==s.config['target_url']==''
        result=s.execute(action)
        final,_=drive(s)
        assert result['code']=='browser_public_target_opened' and final['checks']['goal_met']
        assert final['discovery']['target_url']==final['url']==action['target_url']==s.target_url
        assert final['discovery']['search_proof']==final['discovery']['content_proof']=='fresh'
        assert s.config['target_url']==''
    finally:s.close()


@pytest.mark.parametrize('site',['python_docs','python_org'])
def test_automatic_candidates_are_distinct_visible_bounded_and_safe(fixture,site):
    s=fixture(config(site,target_url=''))
    try:
        search_results(s)
        region='#search-results' if site=='python_docs' else '.list-recent-events'
        base='https://docs.python.org/3/library/item' if site=='python_docs' else 'https://www.python.org/about/item'
        urls=[base+str(i)+('.html' if site=='python_docs' else '/') for i in range(10)]
        html=''.join('<li><a href="'+u+'">'+'t'*199+'😀'+'t'*50+'</a><span>'+'s'*500+'</span></li>' for u in urls)
        html+='<li><a href="'+urls[0]+'#same">duplicate</a></li>'
        for attrs in ['href="https://evil.example/a/"','href="'+urls[0]+'?secret=a"',
                'href="'+urls[1]+'" download','href="'+urls[2]+'" target=_blank',
                'href="'+urls[3]+'" style="display:none"','href="javascript:alert(1)"',
                'href="'+urls[4].replace('https://','https://user@')+'"']:
            html+='<li><a '+attrs+'>unsafe</a></li>'
        s.page.locator(region).evaluate('(e,html)=>e.innerHTML=html',html)
        obs=s.observe();actions=obs['actions']
        assert len(actions)==8 and len({a['target_url'] for a in actions})==8
        assert [a['target_url'] for a in actions]==urls[:8]
        assert all(len(a['title'])==200 and len(a['snippet'])==400 for a in actions)
        assert all(a['title'].endswith('😀') for a in actions)
        assert obs['discovery']['candidate_targets']['target']==11
        assert not obs['discovery']['ambiguous_controls']
        altered=dict(actions[0],target_url=urls[9])
        assert s.execute(altered)['code']=='browser_action_stale' and not s.content_clicked
        assert s.execute(actions[1])['code']=='browser_public_target_opened'
        assert drive(s)[0]['url']==urls[1]
    finally:s.close()


@pytest.mark.parametrize('mutation',[
    "el.href='https://docs.python.org/3/library/os.html'",
    "el.href='https://evil.example/3/library/pathlib.html'",
    "el.setAttribute('download','x')",
    "el.target='_blank'",
    "el.style.visibility='hidden'",
])
def test_target_is_revalidated_after_identity_check_even_without_dom_replacement(fixture,mutation):
    s=fixture(config(target_url=''))
    try:
        action=search_results(s)['actions'][0]
        original=s.observe
        def race():
            observation=original()
            # Change after execute's reobservation, keeping the issued node identity.
            s.page.locator('#search-results a').evaluate('(el)=>{'+mutation+'}')
            return observation
        s.observe=race
        result=s.execute(action)
        assert result['code']=='browser_action_stale' and result['verification']['effect_dispatched'] is False
        assert not s.content_clicked
        assert not any(u==config()['target_url'] for m,u,t in s.requests)
    finally:s.close()


def public_cursor(cfg,phase):
    cfg=validate_public_config(cfg)
    docs=cfg['site']=='python_docs'
    url=(cfg['start_url']+('search.html?q=pathlib' if docs else 'search/?q=pathlib&submit='))
    target=config(cfg['site'])['target_url']
    return {'version':1,'mode':'public_search','phase':phase,
        'url':target if phase=='target_opened' else url,'config_sha256':_digest(cfg),
        'target_url':target if phase=='target_opened' else ''}


@pytest.mark.parametrize('site',['python_docs','python_org'])
@pytest.mark.parametrize('phase',['search_results','target_opened'])
def test_public_resume_fresh_get_does_not_replay_proven_actions(fixture,site,phase):
    cfg=config(site,target_url='');cursor=public_cursor(cfg,phase)
    s=fixture(cfg,resume=cursor)
    try:
        initial=s.observe()
        assert initial['discovery']['resume']==cursor
        assert len([u for m,u,t in s.requests if t=='document'])==1
        assert not any(u==cfg['start_url'] for m,u,t in s.requests)
        final,events=drive(s)
        assert final['checks']['goal_met'] and final['discovery']['content_proof']=='fresh'
        assert not any(e['code'] in {'browser_fill_verified','browser_public_search_submitted'} for e in events)
        assert sum(e['code']=='browser_public_target_opened' for e in events)==(1 if phase=='search_results' else 0)
        assert final['discovery']['search_proof']==('historical' if phase=='target_opened' else 'fresh')
        assert final['discovery']['search_status']==(None if phase=='target_opened' else 200)
        assert final['discovery']['target_url']==config(site)['target_url']
        assert sum(u==config(site)['target_url'] for m,u,t in s.requests)==1
    finally:s.close()


def test_search_resume_preserves_explicit_target_filter(fixture):
    cfg=config();s=fixture(cfg,resume=public_cursor(cfg,'search_results'))
    try:
        s.page.locator('#search-results').evaluate("e=>e.insertAdjacentHTML('beforeend','<a href=/3/library/os.html>os</a>')")
        obs=s.observe()
        assert [a['target_url'] for a in obs['actions']]==[cfg['target_url']]
    finally:s.close()


@pytest.mark.parametrize('kind',['search_code','content_code'])
def test_resume_fetch_requires_new_http_200_proof(fixture,kind):
    setattr(fixture,kind,503)
    phase='search_results' if kind=='search_code' else 'target_opened'
    cfg=config(target_url='');s=fixture(cfg,resume=public_cursor(cfg,phase))
    try:
        obs,events=drive(s)
        assert not obs['checks']['goal_met'] and obs['text']==''
        if kind=='search_code':assert not s.content_clicked and not obs['discovery']['query_verified']
        assert not any(e['code']=='browser_public_search_submitted' for e in events)
    finally:s.close()




def test_blocked_request_diagnostics_appear_in_locked_observation_without_reading_browser(fixture):
    s=fixture(config())
    try:
        req=request(s,'https://example.org/telemetry?hidden=value',method='POST',kind='ping')
        aborted=[]
        s._route(SimpleNamespace(request=req,abort=lambda:aborted.append(True)))
        obs=s.observe()
        assert aborted==[True] and obs['locked'] and not obs['checks']['goal_met']
        assert obs['discovery']['blocked_requests']==[{'method':'POST','host':'example.org','path':'/telemetry','resource':'ping','navigation':False,
            'query_present':True,'query_parameter_names':['hidden'],'fragment_present':False,'content_type':None}]
        assert not obs['discovery']['blocked_requests_overflow']
        assert obs['actions']==[] and obs['text']==''
    finally:s.close()




def test_python_org_csp_report_abort_keeps_fresh_resumed_content_proof(fixture):
    cfg=config('python_org',target_url='');s=fixture(cfg,resume=public_cursor(cfg,'target_opened'))
    try:
        req=request(s,'https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key',method='POST',kind='other')
        req.header_value=lambda name:'application/csp-report'
        aborted=[]
        s._route(SimpleNamespace(request=req,abort=lambda:aborted.append(True)))
        obs=s.observe()
        assert aborted==[True] and obs['checks']['goal_met'] and not obs['locked']
        assert obs['discovery']['search_status'] is None and obs['discovery']['content_status']==200
        assert obs['discovery']['search_proof']=='historical' and obs['discovery']['content_proof']=='fresh'
        assert 'blocked_requests' not in obs['discovery']
        assert s.blocked_requests[0]['blocked_without_effect'] is True
        assert obs['discovery']['target_url']==config('python_org')['target_url']
        assert s.completed==[] and len([u for m,u,t in s.requests if t=='document'])==1
    finally:s.close()


def test_non_actionable_request_diagnostics_do_not_change_action_snapshot(fixture):
    s=fixture(config('python_org',target_url=''))
    try:
        before=search_results(s)
        req=request(s,'https://analytics.python.org/metrics?hidden=value',kind='fetch')
        s._route(SimpleNamespace(request=req,abort=lambda:None))
        after=s.observe()
        assert before==after and before['actions']
        assert s.blocked_requests and 'blocked_requests' not in after['discovery']
        assert not s.locked
    finally:s.close()


@pytest.mark.parametrize('field',['candidate_targets','controls'])
def test_sphinx_settle_waits_for_unchanged_full_candidate_dom(field):
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.locked=False;s.failures=[];waits=[]
    samples=[{field:x} for x in [31,51,70,70,70]]
    s.page=SimpleNamespace(wait_for_timeout=lambda ms:waits.append(ms),evaluate=lambda *args:samples.pop(0))
    result=s._settle_result_dom({field:10},{})
    assert result=={field:70} and waits==[150]*5 and not samples
    assert not s.locked


def test_sphinx_settle_has_a_fixed_read_only_limit():
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.locked=False;s.failures=[];waits=[];reads=[]
    def changing(*args):
        reads.append(True)
        return {'controls':[{'title':str(len(reads))}]}
    s.page=SimpleNamespace(wait_for_timeout=lambda ms:waits.append(ms),evaluate=changing)
    s._settle_result_dom({'controls':[]},{})
    assert len(reads)==len(waits)==s.MAX_RESULT_SETTLE_READS==20
    assert sum(waits)==3000 and s.locked
    assert s.failures==[{'code':'browser_public_results_unsettled'}]


def test_sphinx_settle_stops_reading_immediately_after_request_violation():
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.locked=False;s.failures=[]
    s.page=SimpleNamespace(wait_for_timeout=lambda ms:s._violation('browser_public_method_blocked'),
        evaluate=lambda *args:pytest.fail('must not read after locked'))
    initial={'controls':[]}
    assert s._settle_result_dom(initial,{})==initial
    assert s.locked and s.failures==[{'code':'browser_public_method_blocked'}]


def test_sphinx_progressive_results_settle_before_issuing_current_candidates(fixture):
    s=fixture(config(target_url=''))
    try:
        search_results(s)
        s.page.locator('#search-results').evaluate("""e=>{
            let n=0;const timer=setInterval(()=>{
                e.insertAdjacentHTML('beforeend','<li><a href="/3/library/item'+(++n)+'.html">candidate '+n+'</a></li>');
                if(n===12)clearInterval(timer);
            },40);
        }""")
        obs=s.observe()
        assert not obs['locked'] and obs['discovery']['candidate_targets']['target']==13
        assert len(obs['actions'])==8 and s.observe()==obs
        assert len(s.completed)==2
        assert not s.content_clicked
    finally:s.close()
