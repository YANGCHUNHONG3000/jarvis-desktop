"""Real owned Edge, local HTTP and no cloud APIs: bounded JS query lifecycle."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_service.browser_interaction import BrowserInteraction, InteractionError, validate_config
from agent_service.browser_javascript import JavascriptEdgeSession


@pytest.fixture
def js_site():
    requests, counts = [], {}
    script = r"""
const mode=document.body.dataset.mode;
const render=()=>{
 document.querySelector('main').innerHTML='<section role="search"><label>Search records <input type="search" name="q" placeholder="Find record"></label><button type="button">Search records</button></section><p id="status"></p>';
 document.querySelector('button').onclick=async()=>{
  const query=document.querySelector('input').value;
  if(mode==='noop'||mode==='preexisting')return;
  if(mode==='post'){fetch('/api/search',{method:'POST',body:query}).catch(()=>{});return}
  if(mode==='sidechannel'){fetch('/api/search?q='+encodeURIComponent(query)+'&token=private').catch(()=>{});return}
  if(mode==='socket'){new WebSocket('ws://'+location.host+'/ws-endpoint');return}
  if(mode==='popup'){window.open('/api/search?q='+encodeURIComponent(query));return}
  if(mode==='local'){document.querySelector('#status').textContent='Record 042 approved JS result';return}
  document.querySelector('#status').setAttribute('aria-busy','true');
  document.querySelector('#status').textContent='Loading';
  const response=await fetch('/api/search?q='+encodeURIComponent(query));
  if(!response.ok){
   if(mode==='retry-late')setTimeout(()=>fetch('/api/search?q='+encodeURIComponent(query)),250);
   render();document.querySelector('#status').textContent='Temporary error; retry available';return}
  const text=await response.text();
  setTimeout(()=>{const status=document.querySelector('#status');status.removeAttribute('aria-busy');status.textContent=text},mode==='delayed'?450:0);
 };
};
if(mode==='hydration')setTimeout(render,350);else render();
"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(('GET', self.path, self.headers.get('Cookie')))
            parsed = urlsplit(self.path)
            status, mime = 200, 'text/html; charset=utf-8'
            if parsed.path == '/bootstrap.js':
                body, mime = script, 'text/javascript; charset=utf-8'
            elif parsed.path == '/api/search':
                counts['query'] = counts.get('query', 0) + 1
                status = 503 if counts.get('retry') and counts['query']==1 else 200
                body, mime = ('Temporary unavailable' if status==503 else 'Record 042 approved JS result'), 'text/plain'
            elif parsed.path == '/asset-redirect.js':
                self.send_response(302)
                self.send_header('Location', '/unapproved?token=private')
                self.end_headers()
                return
            else:
                mode = parsed.path.strip('/') or 'dynamic'
                if mode in {'retry','retry-late'}:
                    counts['retry'] = True
                prior = '<p>Record 042 approved JS result</p>' if mode=='preexisting' else ''
                asset = '/asset-redirect.js' if mode=='asset-redirect' else '/bootstrap.js'
                body = f'<title>JS query fixture</title><body data-mode="{mode}">{prior}<main></main><script src="{asset}"></script></body>'
            content = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Set-Cookie', 'fixture_private=never-send')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            requests.append(('POST', self.path, self.headers.get('Cookie')))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', requests, counts
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def config(base, mode='dynamic'):
    return {'mode':'discover_query','javascript':True,'start_url':base+'/'+mode,
            'query':'单据 042','required_text':['Record 042','approved JS result'],
            'allow_loopback':True,'headless':True}


def save_evidence(name, scope, events, requests):
    folder = os.environ.get('JARVIS_BROWSER_JAVASCRIPT_EVIDENCE')
    if folder:
        root = Path(folder)
        root.mkdir(parents=True,exist_ok=True)
        (root/(name+'.json')).write_text(json.dumps({'passed':True,'config':scope,
            'events':events,'requests':requests},ensure_ascii=False,indent=2),encoding='utf-8')


def drive(session, *, maximum=14):
    events = [session.observe()]
    for _ in range(maximum):
        observation = session.observe()
        if observation['checks']['goal_met'] or not observation['actions'] or observation['locked']:
            return observation, events
        result = session.execute(observation['actions'][0])
        events.append(result)
        if result['state']=='unknown':
            return result['observation'], events
    return session.observe(), events


@pytest.mark.parametrize('mode',['dynamic','hydration','delayed','retry','retry-late'])
def test_real_edge_javascript_discovery_wait_query_and_readonly_retry(js_site, mode):
    base, requests, counts = js_site
    scope = config(base,mode)
    session = JavascriptEdgeSession(scope)
    try:
        assert session.observe() == session.observe()
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert observation['discovery']['query_verified']
        clicks = [event for event in events[1:] if event['code'] in {
            'browser_query_dispatched','browser_query_verified','browser_query_readonly_failed'}]
        assert clicks
        if mode in {'retry','retry-late'}:
            assert observation['discovery']['query_attempts']==2
            assert any(event.get('code')=='browser_query_readonly_failed' for event in events)
            assert [a['requests'][0]['status'] for a in observation['discovery']['attempts']]==[503,200]
            first, second = observation['discovery']['attempts']
            assert first['rule_id'] != second['rule_id']  # actual rerender identities
            assert any(e.get('verification',{}).get('failed_query_realm_destroyed') for e in events)
        if mode in {'hydration','delayed'}:
            assert any(e.get('code')=='browser_wait_observed' for e in events)
        queries = [request for request in requests if request[1].startswith('/api/search')]
        assert len(queries)==(2 if mode in {'retry','retry-late'} else 1)
        assert all(parse_qs(urlsplit(r[1]).query)=={'q':['单据 042']} for r in queries)
        assert all(not r[2] for r in requests)  # fresh context cookies stripped on every request
        save_evidence(mode,scope,events,requests)
    finally:
        session.close()


def test_worker_routes_javascript_and_wait_uses_existing_supervision(js_site):
    base, requests, _ = js_site
    scope = config(base,'delayed')
    session = BrowserInteraction(scope)
    try:
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert any(e.get('code')=='browser_wait_observed' for e in events)
        assert session.process.poll() is None
        save_evidence('worker',scope,events,requests)
    finally:
        session.close()
    assert session.process.poll() is not None


def test_replaced_identical_controls_reject_stale_action_without_query(js_site):
    base, requests, _ = js_site
    session = JavascriptEdgeSession(config(base))
    try:
        fill = session.observe()['actions'][0]
        session.page.locator('[role=search]').evaluate('el=>el.outerHTML=el.outerHTML')
        current = session.observe()
        assert current['actions'][0]['control_id'] != fill['control_id']
        assert session.execute(fill)['code']=='browser_action_stale'
        assert not any('/api/search' in r[1] for r in requests)
    finally:
        session.close()


def test_same_url_identical_document_reload_invalidates_old_action(js_site):
    base, requests, _ = js_site
    session = JavascriptEdgeSession(config(base))
    try:
        original = session.observe()
        session.page.reload(wait_until='domcontentloaded')
        current = session.observe()
        assert current['url']==original['url'] and current['text']==original['text']
        assert current['state_id'] != original['state_id']
        assert session.execute(original['actions'][0])['code']=='browser_action_stale'
        assert not any('/api/search' in r[1] for r in requests)
    finally:
        session.close()


def test_query_transport_never_resolves_hostname_after_initial_pin(js_site,monkeypatch):
    base, requests, _ = js_site
    import socket
    real_lookup = socket.getaddrinfo
    hostname = 'jarvis-fixture.invalid'
    scoped = config(base.replace('127.0.0.1',hostname))
    def first_lookup(host,port,*args,**kwargs):
        return real_lookup('127.0.0.1' if host==hostname else host,port,*args,**kwargs)
    monkeypatch.setattr(socket,'getaddrinfo',first_lookup)
    session = JavascriptEdgeSession(scoped)
    try:
        def forbidden_lookup(*args,**kwargs):
            raise AssertionError('unexpected second DNS resolution')
        monkeypatch.setattr(socket,'getaddrinfo',forbidden_lookup)
        observation, events = drive(session)
        assert observation['checks']['goal_met']
        assert any('/api/search' in r[1] for r in requests)
        save_evidence('pinned-dns',scoped,events,requests)
    finally:
        session.close()


@pytest.mark.parametrize('mode',['post','sidechannel','socket','popup'])
def test_unknown_or_unapproved_effects_lock_without_network_or_replay(js_site,mode):
    base, requests, _ = js_site
    scope = config(base,mode)
    session = JavascriptEdgeSession(scope)
    try:
        fill = session.execute(session.observe()['actions'][0])
        action = fill['observation']['actions'][0]
        result = session.execute(action)
        assert result['state']=='unknown'
        assert result['observation']['locked']
        assert not result['observation']['checks']['goal_met']
        assert session.execute(action)['code']=='browser_session_locked'
        assert not any('/api/search' in r[1] or '/ws-endpoint' in r[1] for r in requests)
        save_evidence(mode,scope,[fill,result],requests)
    finally:
        session.close()


@pytest.mark.parametrize('mode',['noop','preexisting','local'])
def test_noop_and_preexisting_result_never_succeed_and_stop_bounded(js_site,mode):
    base, requests, _ = js_site
    scope = config(base,mode)
    session = JavascriptEdgeSession(scope)
    session.WAIT_MS = 25
    session.MAX_WAITS = 2
    try:
        observation, events = drive(session)
        assert observation['locked'] and not observation['checks']['goal_met']
        assert observation['discovery']['query_attempts']==1
        assert observation['discovery']['wait_count']==2
        assert events[-1]['state']=='unknown'
        assert not any('/api/search' in r[1] for r in requests)
        save_evidence(mode,scope,events,requests)
    finally:
        session.close()


def test_static_asset_redirect_cannot_escape_interception(js_site):
    base, requests, _ = js_site
    session = JavascriptEdgeSession(config(base,'asset-redirect'))
    try:
        observation = session.observe()
        assert observation['locked']
        assert not any('/unapproved' in r[1] for r in requests)
    finally:
        session.close()


@pytest.mark.parametrize('change',[{'javascript':'yes'},{'javascript':1},
    {'start_url':'http://127.0.0.1:12345/search?token=private'}])
def test_javascript_scope_validation_rejects_unsafe_values_without_launch(change):
    value = config('http://127.0.0.1:12345')
    value.update(change)
    with pytest.raises(InteractionError):
        validate_config(value)


@pytest.mark.parametrize('condition',['cancel','deadline'])
def test_cancel_or_deadline_closes_owned_worker_without_query_replay(js_site,condition):
    base, requests, _ = js_site
    stopped = [False]
    session = BrowserInteraction(config(base,'noop'),stop=lambda:stopped[0])
    try:
        fill = session.execute(session.observe()['actions'][0])
        clicked = session.execute(fill['observation']['actions'][0])
        assert clicked['code']=='browser_query_dispatched'
        if condition=='cancel':
            stopped[0] = True
        else:
            session.deadline = time.monotonic()-1
        result = session.execute(clicked['observation']['actions'][0])
        assert result['state']=='unknown'
        assert session.process.poll() is not None
        assert not any('/api/search' in r[1] for r in requests)
    finally:
        session.close()
