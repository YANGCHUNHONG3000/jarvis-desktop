"""New public contracts with isolated real Edge HTML; no external website calls."""
from types import SimpleNamespace
import pytest
from agent_service.browser_public import PublicSearchEdgeSession, validate_public_config, validate_public_target
from agent_service.browser_interaction import InteractionError, _digest
from agent_service.public_site_profiles import PROFILES, PUBLIC_SITES, public_search_url


def config(site='pandas_docs', **changes):
    return dict(mode='public_search', site=site, start_url=PROFILES[site]['start'],
        query='read_excel' if site=='pandas_docs' else 'email', required_text=['approved document content'],
        target_url='',headless=True,**changes)


def target(site, number=0):
    return ('https://pandas.pydata.org/docs/reference/api/item'+str(number)+'.html' if site=='pandas_docs'
        else 'https://docs.djangoproject.com/en/5.2/topics/item'+str(number)+'/')


@pytest.fixture
def expansion(monkeypatch):
    monkeypatch.setattr('agent_service.browser_public.socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('151.101.0.223',443))])
    monkeypatch.setattr('agent_service.research._public_search_proxy',lambda:None)
    class Fixture(PublicSearchEdgeSession):
        def _route(self,route):
            req=route.request
            if not self._request_allowed(req):return super()._route(route)
            if self.search_pending and self._is_search(req.url):self.search_request=req
            elif self.content_pending and req.url==self.target_url:self.content_request=req
            site=self.config['site']; pandas=site=='pandas_docs'
            if req.url==self.config['start_url']:
                form=('<form action="#" method=get><input name=q type=search></form>' if pandas else
                    '<form action="/en/5.2/search/"><input id=id_q name=q type=search><input type=hidden name=category value=""><button type=submit>Submit</button></form>')
                body=('<div hidden>'+form+'</div>' if pandas else '')+form
            elif self._is_search(req.url):
                links=''.join(('<li>' if pandas else '<dt>')+'<h2 class="result-title"><a href="'+target(site,n)+'#section">Result '+str(n)+'</a></h2>'+('<p>Snippet '+str(n)+'</p></li>' if pandas else '</dt><dd>Snippet '+str(n)+'</dd>') for n in range(11))
                body=('<div id=search-results>'+links+'</div>' if pandas else '<dl class=search-links>'+links+'</dl>')
            else:
                body=('<article class=bd-article>' if pandas else '<article id=docs-content>')+'approved document content</article>'
            route.fulfill(status=200,content_type='text/html',body='<title>public contract fixture</title>'+body)
    return Fixture


def through_search(session):
    assert session.execute(session.observe()['actions'][0])['code']=='browser_fill_verified'
    assert session.execute(session.observe()['actions'][0])['code']=='browser_public_search_submitted'
    obs=session.observe()
    for _ in range(4):
        if obs['discovery']['query_verified']:break
        session.page.wait_for_timeout(100);obs=session.observe()
    return obs


@pytest.mark.parametrize('site',['pandas_docs','django_docs'])
def test_search_native_submit_eight_candidates_and_body(expansion,site):
    s=expansion(config(site))
    try:
        before=s.observe();assert not before['discovery']['ambiguous_controls']
        obs=through_search(s)
        assert s._canonical(s.page.url)==public_search_url(s.config)
        assert obs['discovery']['query_verified']
        assert len(obs['actions'])==8
        assert [a['target_url'] for a in obs['actions']]==[target(site,n) for n in range(8)]
        assert s.execute(obs['actions'][2])['code']=='browser_public_target_opened'
        for _ in range(5):
            final=s.observe()
            if final['checks']['goal_met']:break
            s.page.wait_for_timeout(100)
        assert final['checks']['goal_met'] and final['url']==target(site,2)
        assert len(s.completed)==3
    finally:s.close()


@pytest.mark.parametrize('site,mutation',[
    ('pandas_docs',"f.action='/docs/other.html'"),
    ('pandas_docs',"f.method='post'"),
    ('pandas_docs',"f.insertAdjacentHTML('beforeend','<button>GO</button>')"),
    ('pandas_docs',"f.insertAdjacentHTML('beforeend','<input name=extra value=x>')"),
    ('pandas_docs',"f.insertAdjacentHTML('beforeend','<input>')"),
    ('pandas_docs',"f.querySelector('input').setAttribute('formaction','https://evil.example/')"),
    ('django_docs',"f.querySelector('[name=category]').value='ref'"),
    ('django_docs',"f.querySelector('[name=category]').remove()"),
    ('django_docs',"f.querySelector('[name=category]').disabled=true"),
    ('django_docs',"f.insertAdjacentHTML('beforeend','<input type=hidden name=category value=\"\">')"),
    ('django_docs',"f.querySelector('button').name='submit'"),
    ('django_docs',"f.querySelector('button').setAttribute('formmethod','post')"),
    ('django_docs',"f.insertAdjacentHTML('beforeend','<input name=extra>')"),
])
def test_changed_form_has_no_action(expansion,site,mutation):
    s=expansion(config(site))
    try:
        s.page.locator('form:visible').evaluate('(f)=>{'+mutation+'}')
        obs=s.observe()
        assert obs['discovery']['ambiguous_controls'] and not obs['actions']
    finally:s.close()


@pytest.mark.parametrize('site',['pandas_docs','django_docs'])
def test_form_mutation_between_observation_and_dispatch_is_stale(expansion,site):
    s=expansion(config(site))
    try:
        s.execute(s.observe()['actions'][0]);action=s.observe()['actions'][0]
        original=s.observe
        def race():
            result=original()
            s.page.locator('form:visible').evaluate("f=>f.method='post'")
            return result
        s.observe=race
        result=s.execute(action)
        assert result['code']=='browser_action_stale' and not s.search_pending
    finally:s.close()


@pytest.mark.parametrize('site',['pandas_docs','django_docs'])
@pytest.mark.parametrize('suffix',['?x=1','#x','/../bad','%2fmore','//bad'])
def test_target_capability_rejects_unapproved_urls(site,suffix):
    with pytest.raises(InteractionError):validate_public_target(config(site),target(site)+suffix)


@pytest.mark.parametrize('site',['pandas_docs','django_docs'])
def test_profile_search_url_and_config_are_shared(site):
    raw=config(site);valid=validate_public_config(raw)
    assert valid==raw and _digest(valid)==_digest(raw)
    assert public_search_url(valid)==('https://pandas.pydata.org/docs/search.html?q=read_excel' if site=='pandas_docs'
        else 'https://docs.djangoproject.com/en/5.2/search/?q=email&category=')
    assert PUBLIC_SITES['Python 文档（公开检索）']==('python_docs','https://docs.python.org/3/')


def request(s,url,kind='script',method='GET'):
    return SimpleNamespace(url=url,method=method,resource_type=kind,is_navigation_request=lambda:False)


def test_new_asset_contract_is_bounded_and_exact(expansion):
    s=expansion(config())
    try:
        root='https://pandas.pydata.org'
        for path in ['/docs/searchindex.js','/docs/_static/searchtools.js','/docs/_static/scripts/bootstrap.js?digest=956a7be408686d7d6a7b','/docs/_static/doctools.js?v=abcdef12']:
            assert s._request_allowed(request(s,root+path))
        for path in ['/docs/_static/../private.js','/docs/_static/a.js?v=abcd','/docs/_static/a.js?digest=1234','/docs/_static/a.js?v=abcdef12&extra=x','/docs/other.html','/versions.json']:
            assert not s._request_allowed(request(s,root+path))
        assert not s._request_allowed(request(s,root+'/docs/searchindex.js',method='POST'))
        assert not s._request_allowed(request(s,'https://evil.example/docs/searchindex.js'))
        s.asset_requests=128
        assert not s._request_allowed(request(s,root+'/docs/searchindex.js'))
    finally:s.close()


def test_django_has_no_asset_or_snippet_permission(expansion):
    s=expansion(config('django_docs'))
    try:
        for url in ['https://static.djangoproject.com/js/main.js','https://docs.djangoproject.com/en/5.2/topics/email/','https://docs.djangoproject.com/_static/main.js']:
            assert not s._request_allowed(request(s,url))
    finally:s.close()


def test_django_snippets_bind_adjacent_dd_and_are_bounded(expansion):
    s=expansion(config('django_docs'))
    try:
        obs=through_search(s)
        assert [a['snippet'] for a in obs['actions']]==['Result '+str(n)+' Snippet '+str(n) for n in range(8)]
        s.page.locator('dl.search-links dd').first.evaluate("e=>e.innerText='FIRST '+ 'x'.repeat(500)")
        obs=s.observe()
        assert obs['actions'][0]['snippet']=='Result 0 FIRST '+'x'*385
        assert len(obs['actions'][0]['snippet'])==400
        assert obs['actions'][1]['snippet']=='Result 1 Snippet 1'
    finally:s.close()


def test_django_missing_adjacent_dd_cannot_read_another_result(expansion):
    s=expansion(config('django_docs'))
    try:
        through_search(s)
        s.page.locator('dl.search-links dd').first.evaluate('e=>e.remove()')
        obs=s.observe()
        assert obs['actions'][0]['snippet']=='Result 0'
        assert obs['actions'][1]['snippet']=='Result 1 Snippet 1'
    finally:s.close()
