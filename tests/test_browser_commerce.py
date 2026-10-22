"""Dedicated read-only commerce gate and real owned Edge fixture acceptance."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest

from agent_service.browser_commerce import (CommerceSearchEdgeSession, API_URL, API_MIME,
    commerce_search_body, verify_commerce_search)
from agent_service.browser_interaction import InteractionError, _digest
from agent_service.public_site_profiles import public_search_url

PRODUCT = 'https://www.ikea.com/us/en/p/micke-desk-white-80213074/'
OTHER = 'https://www.ikea.com/us/en/p/alex-drawer-unit-white-00473546/'


def config(**extra):
    return {'mode':'public_search','site':'ikea_us','start_url':'https://www.ikea.com/us/en/',
        'query':'desk','required_text':['MICKE','109.99'],'headless':True,**extra}


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr('agent_service.browser_public.socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('151.101.0.223',443))])
    monkeypatch.setattr('agent_service.research._public_search_proxy',lambda:None)
    class Fixture(CommerceSearchEdgeSession):
        api_code = 200
        shell_code = 200
        api_delay = 0
        double_api = False
        api_change = None
        outside_card = False
        missing_detail = False
        requests = None
        disabled_after_fill = False
        suggestion_on_input = False
        api_extra_products = []
        def _route(self,route):
            req = route.request
            if self.requests is None:self.requests=[]
            self.requests.append((req.method,req.url,req.resource_type))
            def fulfill():
                if req.url == API_URL:
                    data = {'results':[{'component':'PRIMARY_AREA','items':[
                        {'type':'PRODUCT','product':{'pipUrl':PRODUCT}},
                        {'type':'PLANNER','product':{'pipUrl':OTHER}}]},
                        {'component':'SIMILAR_PRODUCTS','items':[{'type':'PRODUCT','product':{'pipUrl':OTHER}}]}]}
                    data['results'][0]['items'].extend(self.api_extra_products)
                    return route.fulfill(status=self.api_code,content_type='application/json',body=json.dumps(data))
                form = '<form method=get action="https://www.ikea.com/us/en/search/"><input type=search name=q><button type=submit disabled>Go</button></form>'
                form += '<script>document.querySelector("input").addEventListener("input",e=>{document.querySelector("button").disabled='+('true' if self.disabled_after_fill else '!e.target.value')+'})</script>'
                if self.suggestion_on_input:
                    form += '<script>document.querySelector("input").addEventListener("input",()=>fetch("https://sik.search.blue.cdtapps.com/us/en/autocomplete?q=SECRET_QUERY",{method:"POST",body:"SECRET_BODY"}).catch(()=>{}))</script>'
                if req.url == self.config['start_url']:
                    html = form; code=200
                elif self._is_search(req.url):
                    body=commerce_search_body(self.config['query'])
                    if self.api_change:self.api_change(body)
                    card = '<article data-testid="plp-product-card" data-product-number="80213074"><a class="plp-price-module__product-link" href="'+PRODUCT+'" aria-label="MICKE desk">MICKE $109.99</a></article>'
                    other = '<article data-testid="plp-product-card" data-product-number="00473546"><a class="plp-price-module__product-link" href="'+OTHER+'">ALEX related recommendation</a></article>'
                    result = card if self.outside_card else '<section id="product-list">'+card+other+'</section>'
                    html = form+'<main>'+result+'</main><script>setTimeout(async()=>{const r=await fetch('+json.dumps(API_URL)+',{method:"POST",headers:{"content-type":'+json.dumps(API_MIME)+'},body:'+json.dumps(json.dumps(body))+'});await r.json();'+('fetch('+json.dumps(API_URL)+',{method:"POST",headers:{"content-type":'+json.dumps(API_MIME)+'},body:'+json.dumps(json.dumps(body))+'});' if self.double_api else '')+'},'+str(self.api_delay)+')</script>'
                    code=self.shell_code
                else:
                    html='<main><h1>MICKE Desk</h1>'+('' if self.missing_detail else '<div class="pipcom-price-module">$109.99</div>')+'Public product dimensions and materials.</main>'
                    code=200
                route.fulfill(status=code,content_type='text/html',body='<title>IKEA real fixture</title>'+html)
            wrapped=SimpleNamespace(request=req,continue_=fulfill,abort=route.abort)
            return super()._route(wrapped)
    return Fixture


def result_page(s):
    first=s.observe();assert [a['kind'] for a in first['actions']]==['browser_fill']
    assert s.execute(first['actions'][0])['code']=='browser_fill_verified'
    obs=s.observe();assert len(obs['actions'])==1
    submitted=s.execute(obs['actions'][0])
    assert submitted['code']=='browser_public_search_submitted' or (submitted['code']=='browser_effect_unknown' and submitted['observation']['locked'])
    for _ in range(12):
        obs=s.observe()
        if obs['locked'] or obs['discovery']['query_verified']:return obs
        if not obs['actions']:return obs
        s.execute(obs['actions'][0])
    return s.observe()


def test_real_edge_read_and_typed_proof(fixture):
    s=fixture(config())
    try:
        obs=result_page(s)
        proof=verify_commerce_search(s.config,obs)
        assert [a['target_url'] for a in obs['actions']]==[PRODUCT]
        assert proof['response']['products']==[{'url':PRODUCT,'number':'80213074'}]
        assert len(proof['candidates'])==1 and not proof['selected_target']
        assert s.execute(obs['actions'][0])['code']=='browser_public_target_opened'
        for _ in range(5):
            obs=s.observe()
            if obs['checks']['goal_met']:break
            s.execute(obs['actions'][0])
        assert obs['checks']['goal_met'] and 'Public product dimensions' in obs['text']
        assert verify_commerce_search(s.config,obs)['selected_target']==PRODUCT
        assert sum(u==API_URL for m,u,t in s.requests)==1
        assert [r for r in s.completed if r.startswith('public-input')]
    finally:s.close()


@pytest.mark.parametrize('kind',['api_code','shell_code','outside_card','missing_detail'])
def test_shell_and_recommendations_never_prove_content(fixture,kind):
    setattr(fixture,kind,503 if kind.endswith('code') else True)
    s=fixture(config())
    try:
        obs=result_page(s)
        if kind=='missing_detail':
            s.execute(obs['actions'][0])
            for _ in range(3):
                obs=s.observe()
                if obs['actions']:s.execute(obs['actions'][0])
        assert not obs['checks']['goal_met'] and not obs['text']
        if kind!='missing_detail':assert not any('target_url' in a for a in obs['actions'])
    finally:s.close()


def test_disabled_submit_input_only_then_wait(fixture):
    fixture.disabled_after_fill=True
    s=fixture(config())
    try:
        first=s.observe();assert first['actions'][0]['kind']=='browser_fill'
        s.execute(first['actions'][0]);obs=s.observe()
        assert [a['kind'] for a in obs['actions']]==['browser_wait']
        assert not s.search_pending
    finally:s.close()


def test_duplicate_api_locks_before_second_dispatch(fixture):
    fixture.double_api=True;s=fixture(config())
    try:
        result_page(s);s.page.wait_for_timeout(50);obs=s.observe()
        assert obs['locked'] and not obs['actions'] and not obs['text']
        assert obs['failures'][-1]['code']=='browser_commerce_request_blocked'
    finally:s.close()


def test_delayed_api_cannot_expose_stale_cards(fixture):
    fixture.api_delay=1600;s=fixture(config())
    try:
        first=s.observe();s.execute(first['actions'][0]);obs=s.observe();s.execute(obs['actions'][0])
        obs=s.observe()
        assert not obs['discovery']['query_verified'] and not any('target_url' in a for a in obs['actions'])
        s.page.wait_for_timeout(1700);obs=s.observe()
        assert verify_commerce_search(s.config,obs)
    finally:s.close()


@pytest.mark.parametrize('change',[
    lambda p:p.update(extra=True),lambda p:p.update(isUserLoggedIn=True),
    lambda p:p.update(isB2B=True),lambda p:p.update(allowAutocorrect=1),
    lambda p:p['searchParameters'].update(input='other'),lambda p:p['components'][0]['window'].update(size=25),
])
def test_exact_body_changes_block_before_dispatch(fixture,change):
    fixture.api_change=staticmethod(change);s=fixture(config())
    try:
        obs=result_page(s)
        assert obs['locked'] and not obs['actions'] and not s.api_seen
    finally:s.close()


@pytest.mark.parametrize('field,value',[
    ('method','GET'),('url',API_URL+'&extra=1'),('url',API_URL.replace('sik.search.blue','evil.search.blue')),
    ('resource_type','xhr'),('mime','application/json'),('navigation',True),
    ('post_data','{"searchParameters":{"input":"desk"}}'),
])
def test_api_request_identity_rejected(field,value):
    s=object.__new__(CommerceSearchEdgeSession);s.config=config();s.search_pending=True;s.content_pending=False
    s.page=SimpleNamespace(url=public_search_url(s.config),main_frame=object());s.search_request=object();s.api_seen=False
    vals=dict(method='POST',url=API_URL,resource_type='fetch',mime=API_MIME,navigation=False,
        post_data=json.dumps(commerce_search_body('desk')))
    vals[field]=value
    req=SimpleNamespace(**vals,frame=s.page.main_frame,is_navigation_request=lambda:vals['navigation'],header_value=lambda _:vals['mime'])
    assert not s._approved_api(req)


@pytest.mark.parametrize('tamper',[
    lambda p:p.update(query='chair'),lambda p:p.update(session_id='other'),
    lambda p:p.update(search_page_url='https://www.ikea.com/us/en/search/?q=chair'),
    lambda p:p['request']['body'].update(isUserLoggedIn=True),
    lambda p:p['response'].update(status=503),lambda p:p['response'].update(finished=False),
    lambda p:p['response']['products'][0].update(url=OTHER),
    lambda p:p['candidates'][0].update(target_url=OTHER),
    lambda p:p['candidates'][0].update(rule_id='invented'),
    lambda p:p.update(dom_sha256='0'*64),lambda p:p.update(selected_target=OTHER),
    lambda p:p['response'].update(body_sha256='not-a-hash'),
    lambda p:p['shell'].update(status=200.0),lambda p:p['response'].update(status=200.0),
])
def test_independent_proof_tampering_rejected(fixture,tamper):
    s=fixture(config())
    try:
        obs=result_page(s);p=obs['discovery']['commerce_search'];tamper(p)
        p['proof_sha256']=_digest({k:v for k,v in p.items() if k!='proof_sha256'})
        with pytest.raises(InteractionError):verify_commerce_search(s.config,obs)
    finally:s.close()


def test_fresh_search_resume_has_no_fill_or_submit(fixture):
    cfg=config();from agent_service.browser_public import validate_public_config
    cfg=validate_public_config(cfg)
    cursor={'version':1,'mode':'public_search','phase':'search_results','url':public_search_url(cfg),
        'target_url':'','config_sha256':_digest(cfg)}
    s=fixture(cfg,resume=cursor)
    try:
        s.page.wait_for_timeout(100);obs=s.observe()
        assert verify_commerce_search(cfg,obs) and [a['kind'] for a in obs['actions']]==['browser_click']
        assert not s.completed
    finally:s.close()


def test_historical_product_resume_needs_no_search_api(fixture):
    from agent_service.browser_public import validate_public_config
    cfg=validate_public_config(config())
    cursor={'version':1,'mode':'public_search','phase':'target_opened','url':PRODUCT,
        'target_url':PRODUCT,'config_sha256':_digest(cfg)}
    s=fixture(cfg,resume=cursor)
    try:
        s.page.wait_for_timeout(50);obs=s.observe()
        assert obs['checks']['goal_met'] and obs['discovery']['search_proof']=='historical'
        assert obs['discovery']['commerce_search'] is None and not s.completed
        assert not any(u==API_URL for m,u,t in s.requests)
        with pytest.raises(InteractionError):verify_commerce_search(cfg,obs)
    finally:s.close()


def test_noncontracted_background_requests_aborted_without_dispatch():
    s=object.__new__(CommerceSearchEdgeSession)
    s.config=config();s.host='www.ikea.com';s.asset_requests=0;s.blocked=0;s.locked=False;s.failures=[]
    for url,method in [('https://sik.search.blue.cdtapps.com/us/en/autocomplete?x=desk','POST'),
            ('https://www.ikea.com/us/en/cart/','POST'),('https://telemetry.example/ping','POST')]:
        called=[]
        req=SimpleNamespace(url=url,method=method,resource_type='fetch',is_navigation_request=lambda:False)
        s._route(SimpleNamespace(request=req,abort=lambda:called.append('abort'),continue_=lambda:called.append('dispatch')))
        assert called==['abort'] and not s.locked


@pytest.mark.parametrize('url,kind',[
    ('https://www.ikea.com/global/assets/dwf/nav/main.js','script'),
    ('https://www.ikea.com/global/assets/fonts/en/font.woff2','font'),
    ('https://www.ikea.com/us/en/products/price-package-scripts.html','fetch'),
])
def test_only_proven_static_paths_allowed(url,kind):
    s=object.__new__(CommerceSearchEdgeSession);s.host='www.ikea.com';s.asset_requests=0
    req=SimpleNamespace(url=url,method='GET',resource_type=kind,is_navigation_request=lambda:False)
    assert s._request_allowed(req)
    req.url+='?extra=1';assert not s._request_allowed(req)
    req.url=url;req.resource_type='xhr';assert not s._request_allowed(req)


def test_same_document_href_change_invalidates_issued_target(fixture):
    s=fixture(config())
    try:
        obs=result_page(s);action=obs['actions'][0]
        s.page.evaluate("url=>document.querySelector('#product-list a').href=url",OTHER)
        result=s.execute(action)
        assert result['code']=='browser_action_stale' and result['verification']['effect_dispatched'] is False
        assert not s.content_clicked
    finally:s.close()


def test_delayed_response_from_replaced_document_cannot_prove_search():
    s=object.__new__(CommerceSearchEdgeSession)
    req=object();s.api_request=req;s.search_request=s.content_request=None;s.api_status=200
    s.api_finished=False;s.config=config();s.api_document_url=public_search_url(s.config)
    s.page=SimpleNamespace(url=PRODUCT);s.content_pending=False;s.locked=False;s.failures=[]
    data={'results':[{'component':'PRIMARY_AREA','items':[{'type':'PRODUCT','product':{'pipUrl':PRODUCT}}]}]}
    s.api_response=SimpleNamespace(body=lambda:json.dumps(data).encode())
    s._finished(req)
    assert not s.api_finished and s.locked and s.failures[-1]['code']=='browser_commerce_response_invalid'


def test_duplicate_json_key_never_passes_request_gate():
    s=object.__new__(CommerceSearchEdgeSession);s.config=config();s.search_pending=True;s.content_pending=False
    s.page=SimpleNamespace(url=public_search_url(s.config),main_frame=object());s.search_request=object();s.api_seen=False
    body=json.dumps(commerce_search_body('desk')).replace('"allowAutocorrect": true','"allowAutocorrect": false,"allowAutocorrect": true')
    req=SimpleNamespace(url=API_URL,method='POST',resource_type='fetch',frame=s.page.main_frame,
        post_data=body,header_value=lambda _:API_MIME,is_navigation_request=lambda:False)
    assert not s._approved_api(req)


def test_input_suggestion_aborts_and_sanitized_diagnostics(fixture):
    fixture.suggestion_on_input=True;s=fixture(config())
    try:
        obs=result_page(s)
        assert not obs['locked'] and verify_commerce_search(s.config,obs)
        saved=json.dumps(s.blocked_requests)
        assert 'autocomplete' in saved and 'SECRET_QUERY' not in saved and 'SECRET_BODY' not in saved
        assert 'blocked_requests' not in obs['discovery']
    finally:s.close()


def test_blocked_diagnostics_have_a_finite_bound():
    s=object.__new__(CommerceSearchEdgeSession);s.blocked=0;s.blocked_requests=[];s.blocked_requests_overflow=False
    req=SimpleNamespace(url='https://example.com/telemetry?session-id=SECRET_SESSION',method='POST',
        resource_type='fetch',is_navigation_request=lambda:False)
    for _ in range(40):s._record_blocked(req)
    assert len(s.blocked_requests)==32 and s.blocked_requests_overflow and s.blocked==40
    assert 'SECRET_SESSION' not in json.dumps(s.blocked_requests)


@pytest.mark.parametrize('path,allowed',[
    ('/global/assets/dwf/nav/main.js',True),('/global/assets/dwf/in-clude/in-clude.min.js',True),
    ('/global/assets/dwf/hri/root.js',False),('/global/assets/dwf/lower-funnel-fragments/root.js',False),
    ('/global/assets/dwf/listing/client.js',False),('/us/en/recommendations/panels/main.js',False),
])
def test_home_asset_budget_reserved_by_narrow_prefixes(path,allowed):
    s=object.__new__(CommerceSearchEdgeSession);s.host='www.ikea.com';s.asset_requests=0
    req=SimpleNamespace(url='https://www.ikea.com'+path,method='GET',resource_type='script',is_navigation_request=lambda:False)
    assert s._request_allowed(req) is allowed


def test_ephemeral_ikea_context_denies_device_permissions(fixture):
    s=fixture(config())
    try:
        states=s.page.evaluate("async()=>Promise.all(['geolocation','notifications','clipboard-read','camera','microphone','local-network-access'].map(async name=>(await navigator.permissions.query({name})).state))")
        assert states==['denied']*6 and not s.locked
    finally:s.close()


def test_unsupported_combination_products_excluded_without_widening_scope(fixture):
    combination='https://www.ikea.com/us/en/p/trotten-desk-ash-effect-white-s39621923/'
    fixture.api_extra_products=[{'type':'PRODUCT','product':{'pipUrl':combination}},
        {'type':'PRODUCT','product':{'pipUrl':'https://evil.example/us/en/p/fake-desk-80213074/'}}]
    s=fixture(config())
    try:
        obs=result_page(s);p=verify_commerce_search(s.config,obs)
        assert not s.locked and s.api_unsupported_products==2
        assert p['response']['products']==[{'url':PRODUCT,'number':'80213074'}]
        assert [a['target_url'] for a in obs['actions']]==[PRODUCT]
        from agent_service.browser_public import validate_public_target
        with pytest.raises(InteractionError):validate_public_target(s.config,combination)
    finally:s.close()


@pytest.mark.parametrize('response,reason',[
    ({'results':[]},'shape'),({'results':[{'component':'PRIMARY_AREA','items':[]}]},'no_supported_products'),
    ({'results':[{'component':'PRIMARY_AREA','items':[{'type':'PRODUCT','product':{'pipUrl':123}}]}]},'shape'),
])
def test_response_failure_has_sanitized_reason_enum(response,reason):
    s=object.__new__(CommerceSearchEdgeSession);req=object()
    s.api_request=req;s.search_request=s.content_request=None;s.api_status=200;s.api_finished=False
    s.config=config();s.api_document_url=public_search_url(s.config);s.page=SimpleNamespace(url=s.api_document_url)
    s.content_pending=False;s.locked=False;s.failures=[];s.api_unsupported_products=0
    s.api_response=SimpleNamespace(body=lambda:json.dumps(response).encode())
    s._finished(req)
    assert s.locked and not s.api_finished and s.api_error==reason
