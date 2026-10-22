"""GitHub scope, real Edge against isolated route fixtures, and handoff privacy."""
import io
import json
from types import SimpleNamespace

import pytest

from agent_service.browser_interaction import BrowserInteraction, InteractionError, validate_config
from agent_service.browser_github import GithubEdgeSession


def config(**extra):
    return {'mode':'github_repo', 'start_url':'https://github.com/microsoft/playwright',
            'query':'playwright user:microsoft', 'required_text':['Playwright fixture result'],
            'headless':True, **extra}


@pytest.mark.parametrize('change', [
    {'start_url':'https://github.com.evil/microsoft/playwright'},
    {'start_url':'http://github.com/microsoft/playwright'},
    {'start_url':'https://github.com/microsoft/playwright/issues'},
    {'start_url':'https://github.com/microsoft/playwright?token=secret'},
    {'start_url':'https://github.com/microsoft/playwright#readme'},
    {'query':'arbitrary user:somebody'}, {'human_login':'yes'},
    {'human_login':True}, {'allow_loopback':True}, {'javascript':True},
])
def test_scope_rejects_unapproved_expansion(change):
    with pytest.raises(InteractionError):
        validate_config(config(**change))


def test_canonical_scope_detached_and_no_dns(monkeypatch):
    monkeypatch.setattr('agent_service.browser_github.socket.getaddrinfo',
                        lambda *a, **k: pytest.fail('validation must not resolve DNS'))
    raw = config()
    actual = validate_config(raw)
    raw['required_text'].append('changed')
    assert actual['required_text'] == ['Playwright fixture result']
    assert actual['human_login'] is False


@pytest.fixture
def github_fixture(monkeypatch):
    monkeypatch.setattr('agent_service.browser_github.socket.getaddrinfo',
        lambda *a, **k: [(2,1,6,'',('140.82.112.3',443))])
    class Fixture(GithubEdgeSession):
        logged_in = False
        authenticated_header = False
        search_type = 'repositories'
        repository_results_status = 200
        requests = None
        challenge = False
        def _route(self, route):
            request = route.request
            if self.requests is None:
                self.requests = []
            # Only test metadata; no headers, cookies or request bodies.
            self.requests.append((request.method, request.url, self.manual))
            if self.manual:
                body = '<title>Login</title><input type=password><p>DO NOT OBSERVE LOGIN BODY</p>'
            elif self._request_allowed(request):
                auth = '<meta name="user-login" content="fixture-private-account">' if self.logged_in else ''
                if '/search?' in request.url:
                    body = auth + '<main><a href="/microsoft/playwright">microsoft/playwright</a></main>'
                    if 'type=code' in request.url:
                        body = auth + '<main><nav aria-label="Filter by"><a href="/search?q=playwright+user%3Amicrosoft&amp;type=repositories">Repositories 24 results</a></nav><a href="/microsoft/playwright">microsoft/playwright</a></main>'
                else:
                    body = auth + '''<meta name="octolytics-dimension-repository_nwo" content="microsoft/playwright">
                    <main>Playwright fixture result</main>
                    <header>
                    <button type="button" aria-label="Search or jump to, type / to search" onclick="document.querySelector('section').hidden=false">Search</button>
                    <section role="dialog" hidden><form action="/search" method="GET">
                    <input type="text" role="combobox" aria-label="Search or jump to" name="q">
                    <input type="hidden" name="type" value="REPOSITORY_SEARCH_TYPE"></form></section></header>
                    <input type=text role=combobox aria-label="Go to file">'''
                    body = body.replace('REPOSITORY_SEARCH_TYPE', self.search_type)
                    if self.authenticated_header:
                        body = body.replace('aria-label="Search or jump to, type / to search"','').replace('>Search</button>','>Type / to search</button>').replace('aria-label="Search or jump to"','aria-label="Search"')
                    if self.challenge:
                        body += '<div id="captcha-container">DO NOT OBSERVE CHALLENGE BODY</div>'
            else:
                return super()._route(route)
            status = self.repository_results_status if self._repository_search(request.url) else 200
            route.fulfill(status=status, content_type='text/html', body=body)
    return Fixture


def drive(session):
    events = []
    for _ in range(12):
        observation = session.observe()
        if observation['checks']['goal_met'] or not observation['actions']:
            return observation, events
        events.append(session.execute(observation['actions'][0]))
    return session.observe(), events


def test_real_edge_exact_search_then_result_link_and_repo_response(github_fixture):
    session = github_fixture(config())
    try:
        initial = session.observe()
        assert not initial['checks']['goal_met']  # target terms already present
        assert initial['text'] == ''
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert observation['discovery']['query_verified']
        assert observation['discovery']['repository_status'] == 200
        assert observation['text'] == 'Playwright fixture result'
        assert [e['code'] for e in events] == ['browser_github_search_opened',
            'browser_fill_verified','browser_github_search_submitted','browser_github_repository_opened']
        assert len(session.requests) == 3
        assert '/search?q=playwright+user%3Amicrosoft&type=repositories' in session.requests[1][1]
        assert all(method=='GET' for method, _, _ in session.requests)
    finally:
        session.close()


def test_real_edge_unrelated_missing_and_invalid_links_do_not_abort_observation(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('main').evaluate("""e=>e.insertAdjacentHTML('beforeend',
          '<a name="section">Section marker</a><a href="">Empty link</a>'+
          '<a href="http://[bad">Repositories invalid</a><a href="javascript:void(0)">Repositories</a>')""")
        initial = session.observe()
        assert len(initial['actions']) == 1
        assert initial['actions'][0]['kind'] == 'browser_click'
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert all(e['ok'] for e in events)
        assert len(session.requests) == 3
    finally:
        session.close()


@pytest.mark.parametrize('authenticated', [False, True])
def test_real_edge_global_header_styles_and_code_repository_filter(github_fixture, authenticated):
    github_fixture.authenticated_header = authenticated
    github_fixture.logged_in = authenticated
    github_fixture.search_type = 'code'
    session = github_fixture(config())
    try:
        session.authenticated = session._authenticated()
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert observation['discovery']['authenticated'] is authenticated
        codes = [e['code'] for e in events]
        assert codes == ['browser_github_search_opened','browser_fill_verified',
            'browser_github_search_submitted','browser_github_repository_filter_selected',
            'browser_github_repository_opened']
        code_result = events[2]['observation']
        assert not code_result['discovery']['query_verified']
        assert not code_result['checks']['goal_met']
        assert 'repositories_filter' in code_result['actions'][0]['rule_id']
        assert events[3]['observation']['discovery']['query_verified']
        assert len(session.requests) == 4
        assert all(method=='GET' for method, _, _ in session.requests)
    finally:
        session.close()


@pytest.mark.parametrize('kind', ['opener', 'input'])
def test_real_edge_global_search_ambiguity_stops_without_action(github_fixture, kind):
    if kind == 'filter':
        github_fixture.search_type = 'code'
    session = github_fixture(config())
    try:
        if kind == 'filter':
            for _ in range(3):
                session.execute(session.observe()['actions'][0])
            session.page.locator('main nav a').evaluate('e=>e.after(e.cloneNode(true))')
        elif kind == 'input':
            session.execute(session.observe()['actions'][0])
            session.page.locator('header input[type=text]').evaluate('e=>e.after(e.cloneNode(true))')
        else:
            session.page.locator('header button').evaluate('e=>e.after(e.cloneNode(true))')
        observation = session.observe()
        assert observation['actions'] == []
        assert observation['discovery']['ambiguous_controls']
        assert not observation['checks']['goal_met']
        assert len(session.requests) == (2 if kind == 'filter' else 1)
    finally:
        session.close()


def test_real_edge_many_equal_navigation_links_cannot_hide_two_executable_candidates(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('main').evaluate("e=>e.insertAdjacentHTML('beforeend','<a href=/microsoft/playwright>repository</a>'.repeat(15))")
        session.page.locator('header button').evaluate('e=>e.after(e.cloneNode(true))')
        observation = session.observe()
        assert not observation['discovery']['controls_overflow']
        assert observation['discovery']['ambiguous_controls']
        assert observation['actions'] == []
        assert len(session.requests) == 1
    finally:
        session.close()


@pytest.mark.parametrize('duplicates', [2, 17])
def test_real_edge_equivalent_repository_filter_and_repo_links_are_one_effect(github_fixture, duplicates):
    github_fixture.search_type = 'code'
    session = github_fixture(config())
    try:
        for _ in range(3):
            session.execute(session.observe()['actions'][0])
        session.page.locator('main nav a').evaluate('(e,n)=>{for(let i=1;i<n;i++)e.after(e.cloneNode(true))}', duplicates)
        session.page.locator('main>a').evaluate('(e,n)=>{for(let i=1;i<n;i++)e.after(e.cloneNode(true))}', duplicates)
        # Different queries and unexpected parameter sets never become effects.
        session.page.locator('main').evaluate("e=>e.insertAdjacentHTML('beforeend','<a href=\"/search?q=evil&type=repositories\">Repositories</a><a href=\"/search?q=playwright+user%3Amicrosoft&type=repositories&extra=x\">Repositories</a>')")
        observation = session.observe()
        assert not observation['discovery']['controls_overflow']
        assert not observation['discovery']['ambiguous_controls']
        assert len(observation['actions']) == 1
        assert 'repositories_filter' in observation['actions'][0]['rule_id']
        summary = observation['discovery']['candidate_targets']
        assert {'kind':'repositories_filter','target':'approved_repository_search','count':duplicates} in summary
        assert {'kind':'repo','target':'approved_repository','count':duplicates} in summary
        session.execute(observation['actions'][0])
        session.page.locator('main>a').evaluate('(e,n)=>{for(let i=1;i<n;i++)e.after(e.cloneNode(true))}', duplicates)
        observation = session.observe()
        assert len(observation['actions']) == 1
        assert 'github-repo-' in observation['actions'][0]['rule_id']
        result = session.execute(observation['actions'][0])
        assert result['observation']['checks']['goal_met']
        assert len(session.requests) == 4
    finally:
        session.close()


def test_real_edge_seventeen_distinct_header_controls_still_stop(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('header button').evaluate('e=>{for(let i=1;i<17;i++)e.after(e.cloneNode(true))}')
        observation = session.observe()
        assert observation['discovery']['controls_overflow']
        assert observation['discovery']['ambiguous_controls']
        assert observation['actions'] == []
        assert len(session.requests) == 1
    finally:
        session.close()


def test_real_edge_filter_with_userinfo_cannot_hide_valid_same_origin_filter(github_fixture):
    github_fixture.search_type = 'code'
    session = github_fixture(config())
    try:
        for _ in range(3):
            session.execute(session.observe()['actions'][0])
        session.page.locator('main nav').evaluate("e=>e.insertAdjacentHTML('afterbegin','<a href=\"https://private:credential@github.com/search?q=playwright+user%3Amicrosoft&type=repositories\">Repositories</a>')")
        observation = session.observe()
        summary = observation['discovery']['candidate_targets']
        assert {'kind':'repositories_filter','target':'approved_repository_search','count':1} in summary
        assert len(observation['actions']) == 1
        assert 'credential' not in json.dumps(observation)
        result = session.execute(observation['actions'][0])
        assert result['observation']['discovery']['query_verified']
        assert not result['observation']['locked']
        assert all('credential' not in url for _, url, _ in session.requests)
    finally:
        session.close()


def test_real_edge_repository_filter_stale_and_wrong_query_stays_blocked(github_fixture):
    github_fixture.search_type = 'code'
    session = github_fixture(config())
    try:
        for _ in range(3):
            session.execute(session.observe()['actions'][0])
        observation = session.observe()
        assert not observation['discovery']['query_verified']
        action = observation['actions'][0]
        session.page.locator('main nav a').evaluate("e=>e.href='/search?q=unapproved&type=repositories'")
        result = session.execute(action)
        assert result['code'] == 'browser_action_stale'
        assert len(session.requests) == 2
        assert not session.observe()['discovery']['query_verified']
    finally:
        session.close()


def test_real_edge_late_code_success_cannot_verify_failed_repository_search(github_fixture):
    github_fixture.search_type = 'code'
    github_fixture.repository_results_status = 503
    session = github_fixture(config())
    try:
        for _ in range(4):
            session.execute(session.observe()['actions'][0])
        code_url = 'https://github.com/search?q=playwright+user%3Amicrosoft&type=code'
        session._response(SimpleNamespace(url=code_url,status=200))
        session._finished(SimpleNamespace(url=code_url))
        observation = session.observe()
        assert observation['discovery']['search_status'] == 200
        assert observation['discovery']['repository_search_status'] == 503
        assert not observation['discovery']['query_verified']
        assert not observation['checks']['goal_met']
        assert not any('github-repo-' in a['rule_id'] for a in observation['actions'])
    finally:
        session.close()


def test_real_edge_replaced_control_invalidates_action(github_fixture):
    session = github_fixture(config())
    try:
        action = session.observe()['actions'][0]
        session.page.locator('button').evaluate('e=>e.replaceWith(e.cloneNode(true))')
        result = session.execute(action)
        assert result['code'] == 'browser_action_stale'
        assert not result['verification']['effect_dispatched']
        assert len(session.requests) == 1
    finally:
        session.close()


def test_real_edge_missing_controls_diagnostic_redacts_unknown_labels_and_values(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('button').evaluate('e=>e.remove()')
        session.page.locator('main').evaluate("""e=>e.insertAdjacentHTML('beforeend',
          '<button type=button>Type / to search</button>'+
          '<button type=button aria-label="Search account PRIVATE_ACCOUNT">Account</button>'+
          '<input role=combobox aria-label="Go to file" value="PRIVATE_INPUT_VALUE">')""")
        observation = session.observe()
        semantics = observation['discovery']['search_semantics']
        assert {'tag':'button','role':'button','source':'visible_text','label':'Type / to search'} in semantics
        assert any(s['label']=='unrecognized_search_label' for s in semantics)
        assert observation['discovery']['repository_identity_matches']
        assert observation['discovery']['main_present']
        assert all(a['kind']=='browser_wait' for a in observation['actions'])
        assert 'PRIVATE_ACCOUNT' not in json.dumps(observation)
        assert 'PRIVATE_INPUT_VALUE' not in json.dumps(observation)
    finally:
        session.close()


def test_real_edge_manual_no_dom_observation_and_fresh_auth_page(github_fixture):
    # Visibility requirement itself is separately validated; fixture is hidden
    # until switching configuration after browser construction.
    session = github_fixture(config())
    try:
        old = session.observe()['actions'][0]
        session.config.update(human_login=True, headless=False)
        session.human_needed = True
        session.enter_manual()
        original = session.page.evaluate
        session.page.evaluate = lambda *a, **k: pytest.fail('login DOM observed')
        assert session.observe()['human_required']['site'] == 'github.com'
        assert session.human_tick() == {'manual':True}
        session.page.evaluate = original
        session.logged_in = True  # controlled native-auth substitute, no cookies read
        assert session.resume_manual() == {'resumed':True, 'authenticated':True}
        result = session.execute(old)
        assert result['code'] == 'browser_action_stale'
        observation, _ = drive(session)
        assert observation['checks']['goal_met']
        serialized = json.dumps(observation)
        assert 'fixture-private-account' not in serialized
        assert 'DO NOT OBSERVE' not in serialized
    finally:
        session.close()


def test_real_edge_challenge_only_exposes_safe_handoff(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('main').evaluate("e=>e.insertAdjacentHTML('beforeend','<div id=captcha-container>DO NOT OBSERVE CHALLENGE BODY</div>')")
        observation = session.observe()
        assert observation['human_required']
        assert not observation['actions']
        assert 'DO NOT OBSERVE' not in json.dumps(observation)
        session.config['headless'] = False
        current = session.page
        before = len(session.requests)
        session.enter_manual()
        assert session.page == current
        assert len(session.requests) == before  # preserve actual challenge
        assert session.observe()['human_required']['reason'] == 'github_verification_required'
        assert session.resume_manual() == {'resumed':True, 'authenticated':False}
        assert not session.observe()['checks']['goal_met']
    finally:
        session.close()


def test_real_edge_blocked_captcha_iframe_reload_only_after_human_phase(github_fixture):
    session = github_fixture(config())
    try:
        session.page.locator('main').evaluate("e=>e.insertAdjacentHTML('beforeend','<iframe src=https://octocaptcha.com/challenge></iframe>')")
        session.page.wait_for_timeout(150)
        assert session.challenge_blocked and session.human_needed and not session.locked
        observation = session.observe()
        assert observation['human_required']['reason'] == 'github_verification_required'
        session.config['headless'] = False
        current = session.page
        previous = len(session.requests)
        session.enter_manual()
        assert session.page == current
        assert session.page.url == session.config['start_url']
        assert len(session.requests) == previous + 1
        assert session.requests[-1] == ('GET', session.config['start_url'], True)
        assert not session.challenge_blocked
        assert session.human_tick() == {'manual':True}
        assert session.resume_manual() == {'resumed':True, 'authenticated':False}
    finally:
        session.close()


@pytest.mark.parametrize('url,method,kind,navigation,allowed', [
    ('https://github.com/microsoft/playwright','GET','document',True,True),
    ('https://github.com/microsoft/playwright/issues/new','GET','document',True,False),
    ('https://github.com/microsoft/playwright','POST','document',True,False),
    ('https://evil.example/a.js','GET','script',False,False),
    ('https://github.githubassets.com/assets/github.js','GET','script',False,True),
    ('https://github.githubassets.com/assets/github.js?secret=x','GET','script',False,False),
    ('https://github.com/search?q=playwright+user%3Amicrosoft&type=repositories','GET','document',True,True),
    ('https://github.com/search?q=playwright+user%3Amicrosoft&type=code','GET','document',True,True),
    ('https://github.com/search?q=playwright+user%3Amicrosoft&q=evil','GET','document',True,False),
    ('https://github.com/search?q=playwright+user%3Amicrosoft&token=private','GET','xhr',False,False),
])
def test_network_policy_exact_get_only(url, method, kind, navigation, allowed):
    session = GithubEdgeSession.__new__(GithubEdgeSession)
    session.config = config()
    session.allowed_target = session.search_pending = True
    session.repo_pending = False
    frame = object()
    session.page = SimpleNamespace(main_frame=frame)
    request = SimpleNamespace(url=url, method=method, resource_type=kind,
                              is_navigation_request=lambda:navigation, frame=frame)
    assert session._request_allowed(request) is allowed


def test_worker_protocol_wall_timeout_survives_frozen_goal_clock(monkeypatch):
    session = BrowserInteraction(config(), clock=lambda:0, deadline=360)
    session.process = SimpleNamespace(stdin=io.BytesIO())
    closed = []
    session.close = lambda:closed.append(True)
    times = iter([1, 1, 32])
    monkeypatch.setattr('agent_service.browser_interaction.time.monotonic', lambda:next(times))
    with pytest.raises(InteractionError, match='browser_cancelled_or_timeout'):
        session._send({'op':'human_tick'})
    assert closed


def test_expired_human_wall_deadline_does_not_send_command(monkeypatch):
    session = BrowserInteraction(config(), clock=lambda:0, deadline=360)
    session.process = SimpleNamespace(stdin=io.BytesIO())
    closed = []
    session.close = lambda:closed.append(True)
    monkeypatch.setattr('agent_service.browser_interaction.time.monotonic', lambda:100)
    with pytest.raises(InteractionError, match='browser_cancelled'):
        session.human_tick(wall_deadline=99)
    assert session.process.stdin.getvalue() == b''
    assert closed


@pytest.mark.parametrize('host,handoff', [
    ('octocaptcha.com',True), ('client-api.arkoselabs.com',True),
    ('octocaptcha.com.evil.example',False), ('unknown.example',False)])
def test_embedded_challenge_route_handoff_precedes_locked_state(host, handoff):
    session = GithubEdgeSession.__new__(GithubEdgeSession)
    session.config = config()
    session.page = SimpleNamespace(main_frame=object())
    session.manual = session.locked = session.human_needed = False
    session.challenge_blocked = False
    session.blocked, session.failures = 0, []
    request = SimpleNamespace(url='https://'+host+'/challenge', method='GET',
        resource_type='document', frame=object(), is_navigation_request=lambda:True)
    aborted = []
    route = SimpleNamespace(request=request, abort=lambda:aborted.append(True))
    session._route(route)
    assert aborted
    assert session.human_needed is handoff
    assert session.locked is not handoff
    assert session.challenge_blocked is handoff
