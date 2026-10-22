"""Owned real Edge against a local HTTP fixture; never contacts a paid API."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

from agent_service.browser_interaction import BrowserInteraction, InteractionError, validate_config, _EdgeSession


@pytest.fixture
def local_page():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(('GET', self.path))
            path = urlsplit(self.path)
            if path.path in {'/query-records','/query-library'}:
                text = '<title>Discovered result</title><body>Jarvis discovered query result</body>'
            elif path.path.startswith('/discover'):
                random_id = 'random-'+uuid.uuid4().hex
                first = f'''<form method='get' action='/query-records' role='search'>
                    <label for='{random_id}'>搜索记录</label>
                    <input id='{random_id}' type='search' name='q'>
                    <button id='button-{uuid.uuid4().hex}'>Search records</button></form>'''
                second = '''<form action='/query-library' method='get'>
                    <label>资料查询 <input name='term' placeholder='检索资料'></label>
                    <input type='submit' value='查找结果'></form>'''
                if path.path == '/discover-no-id':
                    forms = second
                elif path.path == '/discover-many':
                    forms = first+second
                elif path.path == '/discover-unsafe':
                    forms = ''.join([
                        "<form method='post' action='/query-records'><input type='search' name='q'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><input type='hidden' name='extra' value='never-secret'><button>Search</button></form>",
                        "<form method='get' action='/login'><input type='search' name='q'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><input type='password'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><input type='file'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><button name='action' value='send'>Search</button></form>",
                        "<form method='get' action='https://example.invalid/search'><input type='search' name='q'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><button>删除</button></form>",
                        "<form method='get' action='/query-records?token=never-secret'><input type='search' name='q'><button>Search</button></form>",
                        "<form method='get' action='/query-records'><input type='text' name='address'><button>Continue</button></form>",
                        "<form method='get' action='/query-records'><input type='search' name='q'><button formmethod='post'>Search</button></form>",
                    ])
                else:
                    forms = first
                initial_result = '<p>Jarvis discovered query result</p>' if path.path == '/discover-preexisting' else ''
                text = '<title>Auto discovery</title><body><h1>Public query forms</h1>'+initial_result+forms+'</body>'
            elif path.path == '/done':
                text = '<title>Result</title><body><main>Search verified: Jarvis browser result</main></body>'
            else:
                hidden = "<input type='hidden' name='token' value='unapproved'>" if path.path == '/hidden' else ''
                text = '''<title>Form</title><body>
                  <script>document.title='script ran';fetch('/forbidden')</script>
                  <p>Approved local search</p>
                  <span hidden>Jarvis browser result</span>
                  <form action='/done' method='get'>HIDDEN_PLACEHOLDER
                    <label for='q'>Query</label><input id='q' name='q' type='search'>
                    <button id='search'>Search</button>
                    <button id='post' formmethod='post'>Unsafe POST</button>
                    <button id='override' formaction='https://example.invalid/'>Other origin</button>
                  </form>
                  <input id='password' type='password' value='never-observe-this'>
                  <input id='token' type='text' value='never-observe-token'>
                  <input id='readonly' readonly value='fixed'>
                  <a id='wrong' href='/done'>Reviewed alternative</a>
                  <a id='outside' href='https://example.invalid/'>External</a>
                  <a id='blocked' href='/forbidden'>Forbidden path</a>
                  <a id='popup' target='_blank' href='/done'>New window</a>
                  <a id='noop' href='/'>Same page</a>
                </body>'''.replace('HIDDEN_PLACEHOLDER', hidden)
            content = text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            requests.append(('POST', self.path))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    yield base, requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def config_for(base):
    return {'start_url':base + '/', 'allowed_paths':['/', '/done'],
        'allow_loopback':True, 'headless':True, 'required_text':['Jarvis browser result'],
        'rules':[
            {'id':'wrong', 'kind':'click', 'selector':'#wrong', 'effect':'navigate',
             'expected_url':base + '/done?expected=other', 'expected_text':'Jarvis browser result'},
            {'id':'query', 'kind':'fill', 'selector':'#q', 'value':'Jarvis fixture query'},
            {'id':'search', 'kind':'click', 'selector':'#search', 'effect':'query',
             'expected_text':'Jarvis browser result'},
        ]}


def action_for(observation, rule_id):
    return next(a for a in observation['actions'] if a['rule_id'] == rule_id)


def test_real_edge_fill_query_recovery_and_independent_verification(local_page, tmp_path):
    base, requests = local_page
    session = BrowserInteraction(config_for(base))
    evidence = []
    try:
        observation = session.observe()
        evidence.append(observation)
        assert observation == session.observe()  # stable observation, no timestamp churn
        assert observation['title'] == 'Form'  # scripts did not run
        assert observation['checks']['goal_met'] is False  # hidden text is not evidence
        assert {a['rule_id'] for a in observation['actions']} == {'wrong', 'query'}
        bad = session.execute(action_for(observation, 'wrong'))
        evidence.append(bad)
        assert bad['state'] == 'failed'
        assert bad['code'] == 'browser_target_mismatch'
        assert bad['observation']['failures'][0]['rule_id'] == 'wrong'
        assert {a['rule_id'] for a in bad['observation']['actions']} == {'query'}
        assert requests == [('GET', '/')]
        filled = session.execute(action_for(bad['observation'], 'query'))
        evidence.append(filled)
        assert filled['ok'] and filled['verification']['independent_read']
        assert {a['rule_id'] for a in filled['observation']['actions']} == {'search'}
        assert session.execute(action_for(observation, 'query'))['code'] == 'browser_action_stale'
        result = session.execute(action_for(filled['observation'], 'search'))
        evidence.append(result)
        assert result['state'] == 'verified'
        assert result['observation']['checks']['goal_met']
        assert result['observation']['actions'] == []
        assert result['observation']['completed_rules'] == ['query', 'search']
        assert parse_qs(urlsplit(result['observation']['url']).query) == {'q':['Jarvis fixture query']}
        assert requests == [('GET', '/'), ('GET', '/done?q=Jarvis+fixture+query')]
        artifact = tmp_path / 'browser_acceptance.json'
        artifact.write_text(json.dumps({'passed':True, 'events':evidence,
                                       'requests':requests}, ensure_ascii=False, indent=2), encoding='utf-8')
        # Optional caller-chosen evidence folder makes the exact real run reviewable.
        import os
        destination = os.environ.get('JARVIS_BROWSER_EVIDENCE')
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'acceptance.json').write_text(artifact.read_text(encoding='utf-8'), encoding='utf-8')
    finally:
        session.close()
    assert session.process.poll() is not None


def test_real_edge_disallowed_controls_and_secret_values_not_observed(local_page):
    base, requests = local_page
    config = config_for(base)
    config['rules'] = [config['rules'][1]]
    for name, effect in [('post','query'), ('override','query'), ('outside','navigate'),
                         ('blocked','navigate'), ('popup','navigate')]:
        config['rules'].append({'id':name, 'kind':'click', 'selector':'#'+name,
                                'effect':effect, 'expected_text':'unreachable'})
    config['rules'].extend([
        {'id':'password', 'kind':'fill', 'selector':'#password', 'value':'not allowed'},
        {'id':'readonly', 'kind':'fill', 'selector':'#readonly', 'value':'not editable'}])
    session = BrowserInteraction(config)
    try:
        observation = session.observe()
        assert 'never-observe' not in json.dumps(observation)
        assert 'password' not in {c['rule_id'] for c in observation['controls']}
        assert {a['rule_id'] for a in observation['actions']} == {'query'}
        observation = session.execute(action_for(observation, 'query'))['observation']
        assert observation['actions'] == []
        assert requests == [('GET', '/')]
    finally:
        session.close()


def test_real_edge_unverified_click_is_unknown_and_never_replayed(local_page):
    base, requests = local_page
    config = config_for(base)
    config['rules'] = [{'id':'noop', 'kind':'click', 'selector':'#noop',
                         'effect':'navigate', 'expected_text':'missing result'}]
    session = BrowserInteraction(config)
    try:
        action = action_for(session.observe(), 'noop')
        result = session.execute(action)
        assert result['state'] == 'unknown'
        assert result['observation']['locked']
        count = len(requests)
        assert session.execute(action)['code'] == 'browser_session_locked'
        assert len(requests) == count
        assert session.observe()['actions'] == []
    finally:
        session.close()


def test_real_edge_unapproved_hidden_form_value_blocks_query(local_page):
    base, requests = local_page
    config = config_for(base)
    config['start_url'] = base + '/hidden'
    config['allowed_paths'].append('/hidden')
    session = BrowserInteraction(config)
    try:
        observation = session.observe()
        result = session.execute(action_for(observation, 'query'))
        assert result['state'] == 'verified'
        assert 'search' not in {a['rule_id'] for a in result['observation']['actions']}
        assert 'unapproved' not in json.dumps(result['observation'])
        assert requests == [('GET', '/hidden')]
    finally:
        session.close()


def test_two_real_sessions_close_only_owned_edge(local_page):
    base, _ = local_page
    first, second = BrowserInteraction(config_for(base)), BrowserInteraction(config_for(base))
    try:
        one, two = first.observe(), second.observe()
        assert one['session_id'] != two['session_id']
        first.close()
        assert first.process.poll() is not None
        assert second.observe() == two
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('change', [
    {'allow_loopback':False}, {'headless':'yes'}, {'allowed_paths':['/other']},
    {'required_text':[]}, {'rules':[]}, {'extra':'value'},
    {'start_url':'http://user:secret@127.0.0.1/'},
    {'start_url':'file:///tmp/example'}, {'allowed_paths':['/', '//elsewhere']},
    {'allowed_paths':[[]]}, {'rules':[{'kind':[]}]},
])
def test_invalid_config_rejected_without_browser(change):
    config = config_for('http://127.0.0.1:12345')
    config.update(change)
    with pytest.raises(InteractionError):
        validate_config(config)


def test_config_is_detached_and_outside_expected_url_rejected():
    config = config_for('http://127.0.0.1:12345')
    canonical = validate_config(config)
    assert canonical == config and canonical is not config
    config['rules'][0]['expected_url'] = 'https://elsewhere.invalid/'
    with pytest.raises(InteractionError):
        validate_config(config)
    assert canonical['rules'][0]['expected_url'].startswith('http://127.0.0.1:12345/')


def test_cancel_before_start_does_not_launch_browser(local_page):
    base, requests = local_page
    session = BrowserInteraction(config_for(base), stop=lambda:True)
    try:
        with pytest.raises(InteractionError):
            session.observe()
        assert requests == []
    finally:
        session.close()


def discover_config(base, path='/discover-random'):
    return {'mode':'discover_query','start_url':base+path,'query':'单据 042',
            'required_text':['Jarvis discovered query result'],'headless':True,'allow_loopback':True}


@pytest.mark.parametrize('path,endpoint,parameter',[
    ('/discover-random','/query-records','q'),('/discover-no-id','/query-library','term')])
def test_real_edge_discovers_actual_query_controls_without_selectors_or_paths(local_page,path,endpoint,parameter):
    base, requests = local_page
    config = discover_config(base,path)
    assert 'rules' not in config and 'allowed_paths' not in config
    session = BrowserInteraction(config)
    events = []
    try:
        observed = session.observe()
        events.append(observed)
        assert observed == session.observe()
        assert len(observed['actions']) == 1
        fill = observed['actions'][0]
        assert fill['kind'] == 'browser_fill' and fill['label']
        assert observed['discovery']['candidate_rules'] == 2
        filled = session.execute(fill)
        events.append(filled)
        assert filled['state'] == 'verified'
        assert len(filled['observation']['actions']) == 1
        click = filled['observation']['actions'][0]
        assert click['kind'] == 'browser_click' and click['label']
        assert click['state_id'] != fill['state_id']
        result = session.execute(click)
        events.append(result)
        assert result['state'] == 'verified' and result['observation']['checks']['goal_met']
        assert urlsplit(result['observation']['url']).path == endpoint
        assert parse_qs(urlsplit(result['observation']['url']).query) == {parameter:['单据 042']}
        assert len(requests) == 2 and all(method=='GET' for method,_ in requests)
        assert session.execute(click)['state'] == 'failed'  # successful click cannot replay
        import os
        destination = os.environ.get('JARVIS_BROWSER_DISCOVERY_EVIDENCE')
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True,exist_ok=True)
            (folder/(path.lstrip('/')+'.json')).write_text(json.dumps(
                {'passed':True,'config':config,'events':events,'requests':requests},
                ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        session.close()


def test_real_edge_multiple_query_forms_leave_actual_choice_to_loop(local_page):
    base, requests = local_page
    session = BrowserInteraction(discover_config(base,'/discover-many'))
    try:
        observed = session.observe()
        assert len(observed['actions']) == 2
        assert len({a['control_id'] for a in observed['actions']}) == 2
        assert all(a['kind']=='browser_fill' for a in observed['actions'])
        chosen = next(a for a in observed['actions'] if '资料查询' in a['label'])
        filled = session.execute(chosen)
        actions = filled['observation']['actions']
        assert {a['kind'] for a in actions} == {'browser_fill','browser_click'}
        result = session.execute(next(a for a in actions if a['kind']=='browser_click'))
        assert result['state']=='verified'
        assert urlsplit(requests[-1][1]).path == '/query-library'
    finally:
        session.close()


def test_discovered_query_unknown_result_locks_and_does_not_replay(local_page):
    base, requests = local_page
    config = discover_config(base)
    config['required_text'] = ['Result that the page never contains']
    session = BrowserInteraction(config)
    try:
        filled = session.execute(session.observe()['actions'][0])
        action = filled['observation']['actions'][0]
        result = session.execute(action)
        assert result['state'] == 'unknown' and result['observation']['locked']
        assert not result['observation']['checks']['goal_met']
        count = len(requests)
        assert session.execute(action)['code'] == 'browser_session_locked'
        assert len(requests) == count == 2
    finally:
        session.close()


def test_initial_target_words_cannot_finish_discovery_without_verified_query_submission(local_page):
    base, requests = local_page
    session = BrowserInteraction(discover_config(base,'/discover-preexisting'))
    try:
        initial = session.observe()
        assert 'Jarvis discovered query result' in initial['text']
        assert initial['checks']['goal_met'] is False
        assert initial['discovery']['query_verified'] is False
        filled = session.execute(initial['actions'][0])
        assert filled['state'] == 'verified'
        assert filled['observation']['checks']['goal_met'] is False
        assert filled['observation']['discovery']['query_verified'] is False
        result = session.execute(filled['observation']['actions'][0])
        assert result['state'] == 'verified'
        assert result['observation']['checks']['goal_met'] is True
        assert result['observation']['discovery']['query_verified'] is True
        assert len(requests) == 2
        assert urlsplit(requests[-1][1]).path == '/query-records'
    finally:
        session.close()


def test_real_edge_discovery_never_authorizes_unknown_or_unsafe_forms(local_page):
    base, requests = local_page
    session = BrowserInteraction(discover_config(base,'/discover-unsafe'))
    try:
        observed = session.observe()
        assert observed['actions'] == [] and observed['controls'] == []
        assert observed['discovery']['candidate_rules'] == 0
        assert 'never-secret' not in json.dumps(observed)
        assert requests == [('GET','/discover-unsafe')]
    finally:
        session.close()


def test_real_dom_button_change_and_added_form_field_invalidate_prior_discovery(local_page):
    base, requests = local_page
    session = _EdgeSession(discover_config(base))
    try:
        observed = session.observe()
        filled = session.execute(observed['actions'][0])
        old_click = filled['observation']['actions'][0]
        session.page.locator('button').evaluate("el=>el.textContent='Find updated records'")
        current = session.observe()
        assert current['state_id'] != old_click['state_id']
        assert current['actions'][0]['label'] == 'Find updated records'
        assert session.execute(old_click)['code'] == 'browser_action_stale'
        fresh = current['actions'][0]
        session.page.locator('form').evaluate("""form=>{
          const hidden=document.createElement('input');hidden.type='hidden';
          hidden.name='extra';hidden.value='private-field';form.appendChild(hidden);
        }""")
        assert session.execute(fresh)['code'] == 'browser_action_stale'
        assert session.observe()['actions'] == []
        assert 'private-field' not in json.dumps(session.observe())
        assert requests == [('GET','/discover-random')]
    finally:
        session.close()


@pytest.mark.parametrize('change',[
    {'rules':[]},{'allowed_paths':['/']},{'query':''},{'mode':'arbitrary_click'},
    {'required_text':[]},{'allow_loopback':'yes'}])
def test_invalid_discovery_scope_rejected_without_io(change):
    value = discover_config('http://127.0.0.1:12345')
    value.update(change)
    with pytest.raises(InteractionError):
        validate_config(value)
