"""Opt-in, bounded JavaScript query interaction in a fresh owned Edge context.

Only initial-document-declared same-origin static assets (without query strings)
and approved GET search requests can leave the browser. This intentionally omits
logins, arbitrary SPA APIs, third-party assets, dynamic imports, query-bearing
asset URLs and opaque application protocols. GET semantics remain a website
contract: these restrictions do not establish that every server implements GET
without side effects. Page content and scripts never grant capabilities.
"""
from copy import deepcopy
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import parse_qsl, urljoin, urlsplit
import uuid

from .browser_interaction import (
    InteractionError, _EdgeSession, _SEARCH, _UNSAFE, _digest, _origin, _url_key,
    validate_config,
)


class _Assets(HTMLParser):
    """Collect literal bootstrap declarations before any document JS runs."""
    def __init__(self, base):
        super().__init__()
        self.base, self.urls = base, set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get('src') if tag in {'script', 'img', 'source'} else None
        if tag == 'link' and attrs.get('rel', '').lower() in {
                'stylesheet', 'preload', 'modulepreload', 'icon'}:
            value = attrs.get('href')
        if value:
            try:
                url = urljoin(self.base, value)
                if _origin(url) == _origin(self.base) and not urlsplit(url).query:
                    self.urls.add(_url_key(url))
            except ValueError:
                pass


# Registry is an automation-owned JSHandle: identical replacement DOM elements
# receive different identities, without adding locator attributes to page nodes.
_DOM = r"""registry => {
 const identity=el=>{if(!registry.ids.has(el))registry.ids.set(el,++registry.next);return registry.ids.get(el)};
 const visible=el=>!!el.getClientRects().length && getComputedStyle(el).visibility!=='hidden';
 const path=el=>{const p=[];while(el&&el.nodeType===1){const siblings=el.parentElement?
   Array.from(el.parentElement.children).filter(x=>x.tagName===el.tagName):[el];
   p.unshift(el.tagName.toLowerCase()+':nth-of-type('+(siblings.indexOf(el)+1)+')');el=el.parentElement}return p.join('>')};
 const secret=el=>['password','file','hidden'].includes(el.type)||
  /password|passwd|secret|token|api.?key|credential|cvv|otp|username|credit-card|cc-|one-time/i.test(
  [el.name,el.id,el.getAttribute('autocomplete'),el.getAttribute('aria-label')].join(' '));
 const data=el=>({identity:identity(el),selector:path(el),tag:el.tagName.toLowerCase(),
   type:(el.type||'').toLowerCase(),name:el.name||'',label:(el.getAttribute('aria-label')||
   (el.labels?Array.from(el.labels).map(x=>x.innerText).join(' '):'')||el.innerText||
   (el.type==='submit'?el.value:'')||'').slice(0,200),
   placeholder:(el.getAttribute('placeholder')||'').slice(0,200),
   dom_id:el.id||'',class_name:String(el.className||'').slice(0,200),
   value:secret(el)?null:el.value||'',visible:visible(el),disabled:el.matches(':disabled')||
   el.getAttribute('aria-disabled')==='true',readonly:!!el.readOnly,
   unsafe:secret(el),target:el.formTarget||'',method:el.getAttribute('formmethod')||'',
   action:el.getAttribute('formaction')||'',named:!!el.name});
 const groups=Array.from(document.querySelectorAll('form,[role="search"]')).slice(0,16).map(group=>{
   const owned=Array.from(group.tagName==='FORM'?group.elements:
     group.querySelectorAll('input,textarea,button,[role="button"]')).slice(0,41);
   return {identity:identity(group),selector:path(group),role:group.getAttribute('role')||'',
     form:group.tagName==='FORM',method:group.method||'',action:group.action||'',
     target:group.target||'',count:owned.length,fields:owned.map(data)};
 });
 return {groups,text:(document.body?.innerText||'').replace(/\s+/g,' ').trim().slice(0,12000),
   title:document.title.slice(0,300),loading:!!document.querySelector('[aria-busy="true"],[role="progressbar"]')};
}"""


class JavascriptEdgeSession(_EdgeSession):
    MAX_ATTEMPTS = 2
    MAX_WAITS = 8
    WAIT_MS = 2000

    def __init__(self, config):
        self.config = validate_config(config)
        if not self.config.get('javascript') or self.config.get('mode') != 'discover_query':
            raise InteractionError('browser_javascript_scope_rejected')
        self.origin = _origin(config['start_url'])
        self.session_id = uuid.uuid4().hex
        self.locked, self.blocked = False, 0
        self.query_verified = False
        self.completed, self.failures, self.attempts = [], [], []
        self.wait_count, self.revision = 0, 0
        self.resources = set()
        self.registry, self.registry_url, self.document_id = None, None, None
        self.active = None
        self.phase = 'discovering'
        self.query_names = set()
        self.needs_reset, self.resetting = False, False
        self.navigation_status = None
        self.browser, self.pw = None, None
        addresses = socket.getaddrinfo(self.origin[1], self.origin[2], type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global and not (ip.is_loopback and config['allow_loopback']) for ip in ips):
            raise InteractionError('browser_dns_rejected')
        self.pinned_address = str(ips[0])
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            address = str(ips[0])
            if ':' in address:
                address = '[' + address + ']'
            self.browser = self.pw.chromium.launch(channel='msedge', headless=config['headless'],
                timeout=15000, chromium_sandbox=True,
                args=['--no-proxy-server', f'--host-resolver-rules=MAP {self.origin[1]} {address}',
                      '--disable-background-networking'])
            self.context = self.browser.new_context(java_script_enabled=True,
                accept_downloads=False, service_workers='block')
            self.page = self.context.new_page()
            self.page.set_default_timeout(2500)
            self.page.set_default_navigation_timeout(6000)
            self.context.route('**/*', self._route)
            # No socket transport is part of the approved GET query contract.
            self.context.route_web_socket('**/*', lambda ws: self._socket_blocked(ws))
            self.page.on('response', self._response)
            self.page.on('requestfailed', self._request_failed)
            self.page.on('requestfinished', self._request_finished)
            self.context.on('page', self._popup)
            self.page.on('download', lambda download: self._violation('browser_download_blocked'))
            self.page.on('dialog', lambda dialog: dialog.dismiss())
            response = self.page.goto(config['start_url'], wait_until='domcontentloaded')
            if not response or not response.ok or _origin(self.page.url) != self.origin:
                raise InteractionError('browser_start_failed')
        except BaseException:
            self.close()
            raise

    def _violation(self, code):
        self.blocked += 1
        self.locked = True
        if not self.failures or self.failures[-1]['code'] != code:
            self.failures.append({'code':code})

    def _socket_blocked(self, ws):
        self._violation('browser_websocket_blocked')
        # A routed socket is mocked unless connect_to_server() is called. Leave
        # it unconnected and let the owned context close; sync ws.close() hangs
        # inside this installed Playwright callback (real fixture evidence).

    def _popup(self, page):
        if page != self.page:
            self._violation('browser_popup_blocked')
            page.close()

    def _is_query(self, request):
        try:
            parsed = urlsplit(request.url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            return (self.active is not None and self.active['state']=='pending'
                and self.active.get('dispatched') is True and _origin(request.url) == self.origin
                and request.method == 'GET' and len(pairs) == 1
                and pairs[0][0] in self.query_names and pairs[0][1] == self.config['query']
                and bool(_SEARCH.search(parsed.path)) and not _UNSAFE.search(parsed.path)
                and request.resource_type in {'fetch','xhr','document'}
                and (not request.is_navigation_request() or request.frame == self.page.main_frame))
        except (ValueError, TypeError):
            return False

    def _get(self, url, headers):
        """GET over the approved IP, with original Host/SNI/cert verification.

        Playwright route.fetch uses a separate Node HTTP client and ignores
        Chromium's resolver rules. This transport never resolves a second time,
        follows redirects, reads browser credentials, or uses environment proxies.
        """
        if _origin(url) != self.origin:
            raise InteractionError('browser_url_rejected')
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(self.origin[1], self.origin[2], timeout=6)
        until = time.monotonic()+6
        try:
            connection.sock = socket.socket(socket.AF_INET6 if ':' in self.pinned_address
                                            else socket.AF_INET,socket.SOCK_STREAM)
            connection.sock.settimeout(6)
            connection.sock.connect((self.pinned_address,self.origin[2]))
            if self.origin[0]=='https':
                connection.sock = ssl.create_default_context().wrap_socket(
                    connection.sock,server_hostname=self.origin[1])
            clean = {k:v for k,v in headers.items() if k.lower() not in {
                'cookie','authorization','proxy-authorization','host','accept-encoding',
                'connection','content-length','transfer-encoding'}}
            clean['Host'] = parsed.netloc
            clean['Accept-Encoding'] = 'identity'
            clean['Connection'] = 'close'
            connection.request('GET',(parsed.path or '/')+('?' + parsed.query if parsed.query else ''),
                               headers=clean)
            response = connection.getresponse()
            if response.getheader('Content-Encoding','identity').lower() not in {'','identity'}:
                raise InteractionError('browser_response_encoding_rejected')
            content = bytearray()
            while True:
                remaining = until-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('browser_http_timeout')
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536,2*1024*1024+1-len(content)))
                content.extend(chunk)
                if len(content)>2*1024*1024:
                    raise InteractionError('browser_response_too_large')
                if not chunk:
                    break
            response_headers = {k:v for k,v in response.getheaders() if k.lower() not in {
                'connection','transfer-encoding','content-length'}}
            response_headers['content-length'] = str(len(content))
            return response.status,response_headers,bytes(content)
        finally:
            connection.close()

    def _route(self, route):
        request = route.request
        headers = dict(request.headers)
        # Cookies set by bootstrap responses and HTTP auth never leave this scope.
        headers.pop('authorization', None)
        headers['cookie'] = ''
        try:
            initial = ((not self.attempts or self.resetting) and request.method == 'GET'
                and request.is_navigation_request() and request.frame == self.page.main_frame
                and _url_key(request.url) == _url_key(self.config['start_url']))
            if initial:
                status, response_headers, body = self._get(request.url,headers)
                if status >= 300:
                    route.abort()
                    return
                assets = _Assets(request.url)
                assets.feed(body.decode('utf-8', 'replace'))
                self.resources.update(assets.urls)
                route.fulfill(status=status,headers=response_headers,body=body)
                return
            if self._is_query(request):
                # One query request per click. An unexpected second request is
                # an unknown protocol, even if its URL looks query-shaped.
                if self.active['requests']:
                    self._violation('browser_query_protocol_unknown')
                    route.abort()
                    return
                record = {'url':request.url,'method':'GET','status':None,
                          'failed':False,'finished':False,'request':request}
                self.active['requests'].append(record)
                try:
                    # Never follow server redirects outside interception. Keep
                    # the real HTTP status rather than inventing a 2xx response.
                    status, response_headers, body = self._get(request.url,headers)
                    record['status'] = status
                    if 300 <= status < 400:
                        record['finished'] = True
                        route.abort()
                        return
                    route.fulfill(status=status,headers=response_headers,body=body)
                except Exception:
                    record['failed'], record['finished'] = True, True
                    route.abort()
                return
            if (request.method == 'GET' and _origin(request.url) == self.origin
                    and request.resource_type in {'script','stylesheet','font','image'}
                    and _url_key(request.url) in self.resources):
                status, response_headers, body = self._get(request.url,headers)
                if 300 <= status < 400:
                    self._violation('browser_asset_redirect_blocked')
                    route.abort()
                else:
                    route.fulfill(status=status,headers=response_headers,body=body)
                return
        except Exception:
            self._violation('browser_request_boundary_failed')
            route.abort()
            return
        self._violation('browser_request_blocked')
        route.abort()

    def _response(self, response):
        if self.active:
            for record in self.active['requests']:
                if record['request'] == response.request:
                    record['status'] = response.status
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.navigation_status = response.status

    def _request_failed(self, request):
        if self.active:
            for record in self.active['requests']:
                if record['request'] == request:
                    record['failed'], record['finished'] = True, True

    def _request_finished(self, request):
        if self.active:
            for record in self.active['requests']:
                if record['request'] == request:
                    record['finished'] = True

    def _dom(self):
        if self.registry is None or self.registry_url != self.page.url:
            self.registry = self.page.evaluate_handle('() => ({ids:new WeakMap(),next:0})')
            self.registry_url = self.page.url
            self.document_id = uuid.uuid4().hex
        try:
            dom = self.registry.evaluate(_DOM)
        except Exception:
            # Navigation invalidates JSHandles; recreate only after it happened.
            self.registry = self.page.evaluate_handle('() => ({ids:new WeakMap(),next:0})')
            self.registry_url = self.page.url
            self.document_id = uuid.uuid4().hex
            dom = self.registry.evaluate(_DOM)
        dom['document_id'] = self.document_id
        return dom

    def _candidates(self, dom):
        rules, public = [], []
        seen = set()
        for group in dom['groups']:
            fields = group['fields']
            if group['count'] > 40 or any(f['unsafe'] for f in fields):
                continue
            if group['form']:
                try:
                    if (group['method'].lower() != 'get' or group['target'] not in {'','_self'}
                            or _origin(group['action']) != self.origin or urlsplit(group['action']).query
                            or _UNSAFE.search(group['action'])):
                        continue
                except ValueError:
                    continue
            hints = ' '.join(f['label']+' '+f['placeholder']+' '+f['name'] for f in fields)
            if _UNSAFE.search(hints):
                continue
            inputs = [f for f in fields if f['tag']=='input' and f['type'] in {'text','search'}
                      and f['visible'] and not f['disabled'] and not f['readonly']]
            if len(inputs) != 1:
                continue
            field = inputs[0]
            if any(f['tag'] in {'input','textarea','select'} and f is not field
                   and not f['disabled'] and f['type'] not in {'button','submit','reset'} for f in fields):
                continue
            if not (group['role']=='search' or field['type']=='search' or _SEARCH.search(hints)
                    or field['name'].casefold() in {'q','query','search','keyword'}):
                continue
            name = field['name'] or 'q'
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,59}', name) or _UNSAFE.search(name):
                continue
            buttons = [f for f in fields if (f['tag']=='button' or
                (f['tag']=='input' and f['type']=='submit')) and f['visible'] and not f['disabled']
                and not f['named'] and f['type'] in {'button','submit',''}
                and f['method'].lower() in {'','get'} and not f['action']
                and f['target'] in {'','_self'}]
            if not buttons:
                continue
            if field['identity'] not in seen:
                public.append({'control_id':self._identity(field['identity']), 'label':field['label'] or
                               field['placeholder'] or name,'value_matches':field['value']==self.config['query']})
                seen.add(field['identity'])
                rules.append({'kind':'browser_fill','rule_id':'js-fill-'+self._identity(field['identity']),
                    'control_id':self._identity(field['identity']),'selector':field['selector'],
                    'label':field['label'] or field['placeholder'] or name,'name':name,
                    'matches':field['value']==self.config['query']})
            for button in buttons:
                if button['identity'] in seen:
                    continue
                seen.add(button['identity'])
                rules.append({'kind':'browser_click','rule_id':'js-query-'+self._identity(button['identity']),
                    'control_id':self._identity(button['identity']),'selector':button['selector'],
                    'input_selector':field['selector'],'label':button['label'] or 'Search',
                    'name':name,'matches':field['value']==self.config['query']})
            if len(rules) >= 8:
                break
        return rules[:8], public[:8]

    def _identity(self, identity):
        return self.document_id+'-'+str(identity)

    def _progress(self, dom):
        attempt = self.active
        if not attempt or self.locked or attempt['state'] != 'pending':
            return
        network = attempt['requests']
        if network and all(r['finished'] for r in network) and any(
                r['failed'] or r['status'] is not None and not 200 <= r['status'] < 300 for r in network):
            attempt['state'] = 'readonly_failed'
            self.phase = 'retryable' if len(self.attempts) < self.MAX_ATTEMPTS else 'exhausted'
            self.needs_reset = self.phase=='retryable'
            self.failures.append({'rule_id':attempt['rule_id'],'code':'browser_query_readonly_failed'})
            return
        new_result = (dom['text'] != attempt['before_text'] and not dom['loading']
                      and all(t in dom['text'] for t in self.config['required_text'])
                      and any(t not in attempt['before_text'] for t in self.config['required_text']))
        # Existing initial-page predicates cannot establish a fresh result.
        if not new_result:
            return
        # Optimistic UI and delayed fetch cannot be distinguished from a purely
        # local query using a finite timer. This adapter therefore requires an
        # actual finished query GET with 2xx; local-only JS remains unsupported.
        if not network or any(r['status'] is None or not r['finished'] for r in network):
            return
        attempt['state'] = 'verified'
        self.query_verified = True
        self.phase = 'verified'
        self.completed.append(attempt['rule_id'])

    def observe(self):
        if _origin(self.page.url) != self.origin:
            self._violation('browser_url_rejected')
        dom = self._dom()
        self._progress(dom)
        self.rules, controls = self._candidates(dom)
        available = []
        pending = self.active and self.active['state']=='pending'
        if not self.locked and not self.query_verified:
            if not pending and not self.needs_reset and len(self.attempts) < self.MAX_ATTEMPTS:
                for rule in self.rules:
                    if (rule['kind']=='browser_fill' and not rule['matches'] or
                            rule['kind']=='browser_click' and rule['matches']):
                        available.append({k:rule[k] for k in ('kind','rule_id','control_id','label')})
            if (self.wait_count < self.MAX_WAITS and self.phase != 'exhausted'
                    and (self.needs_reset or pending or not available or dom['loading'])):
                available = [{'kind':'browser_wait','rule_id':'js-wait-'+str(self.wait_count),
                              'control_id':'wait','label':'Wait for page changes'}]
        attempts = [{k:v for k,v in a.items() if k not in {'before_text','requests'}} |
                    {'requests':[{k:v for k,v in r.items() if k!='request'} for r in a['requests']]}
                    for a in self.attempts]
        snap = {'session_id':self.session_id,'url':self.page.url,'title':dom['title'],'text':dom['text'],
                'controls':controls,'completed_rules':list(self.completed),'failures':deepcopy(self.failures),
                'locked':self.locked,'checks':{'goal_met':self.query_verified and not self.locked
                    and all(term in dom['text'] for term in self.config['required_text'])},
                'discovery':{'mode':'discover_query','javascript':True,'dom_sha256':_digest(dom),
                    'candidate_rules':len(self.rules),'query_verified':self.query_verified,
                    'phase':'locked' if self.locked else self.phase,'query_attempts':len(self.attempts),
                    'wait_count':self.wait_count,'attempts':attempts},'revision':self.revision}
        state = _digest(snap)
        snap['state_id'] = state
        snap['actions'] = [dict(a,state_id=state) for a in available]
        return snap

    def _result(self, state, code, **extra):
        return {'ok':state=='verified','state':state,'code':code,
                'verification':{'independent_read':True},'observation':self.observe(),**extra}

    def _stale(self, code):
        return self._result('failed',code,
            verification={'independent_read':True,'effect_dispatched':False})

    def execute(self, action):
        before = self.observe()
        if self.locked:
            return self._result('unknown','browser_session_locked')
        if action not in before['actions']:
            return self._stale('browser_action_stale')
        if action['kind']=='browser_wait':
            self.wait_count += 1
            if self.needs_reset:
                # Destroy the failed query's entire JS realm before retrying.
                # Old onclick timers cannot be attributed to the next attempt.
                self.resetting = True
                self.active = None
                self.query_names = set()
                try:
                    response = self.page.goto(self.config['start_url'],wait_until='domcontentloaded')
                    if not response or not response.ok:
                        raise InteractionError('browser_reset_failed')
                except Exception:
                    self._violation('browser_reset_failed')
                    return self._result('unknown','browser_effect_unknown')
                finally:
                    self.resetting = False
                self.needs_reset = False
                self.phase = 'discovering'
                return self._result('verified','browser_wait_observed',
                    verification={'independent_read':True,'observation_only':True,
                                  'failed_query_realm_destroyed':True})
            self.page.wait_for_timeout(self.WAIT_MS)
            after = self.observe()
            if self.active and self.active['state']=='readonly_failed':
                return self._result('failed','browser_query_readonly_failed')
            if self.wait_count >= self.MAX_WAITS and not after['checks']['goal_met']:
                if self.active and self.active['state']=='pending':
                    self._violation('browser_effect_unknown')
                    return self._result('unknown','browser_effect_unknown')
                self.phase = 'exhausted'
            return self._result('verified','browser_wait_observed',
                verification={'independent_read':True,'observation_only':True})
        rule = next(r for r in self.rules if r['rule_id']==action['rule_id'])
        locator = self.page.locator(rule['selector'])
        # State binding includes actual live element identity and DOM contents.
        if not locator.count() or action not in self.observe()['actions']:
            return self._stale('browser_controls_detached')
        handle = locator.element_handle(timeout=500)
        if (handle is None or not handle.evaluate('el=>el.isConnected') or
                self._identity(self.registry.evaluate('(r,el)=>r.ids.get(el)', handle)) != action['control_id']):
            return self._stale('browser_controls_detached')
        try:
            if action['kind']=='browser_fill':
                handle.fill(self.config['query'], timeout=1500)
                # Input handlers may replace their own control; reobserve rather
                # than reading a detached locator or guessing that fill succeeded.
                self.revision += 1
                after = self.observe()
                if self.locked:
                    return self._result('unknown','browser_effect_unknown')
                return self._result('verified','browser_fill_verified') if any(
                    c['value_matches'] for c in after['controls']) else self._result(
                    'failed','browser_controls_detached')
            self.query_names = {rule['name'],'q','query','search','keyword'}
            # trial checks actionability without dispatching a click. A failure
            # here is known to precede the query action and can reobserve safely.
            try:
                handle.click(trial=True, timeout=1000)
            except Exception:
                return (self._result('unknown','browser_effect_unknown') if self.locked else
                        self._stale('browser_controls_detached'))
            # Trial can pump events; validate one last time before dispatch.
            if action not in self.observe()['actions']:
                return (self._result('unknown','browser_effect_unknown') if self.locked else
                        self._stale('browser_action_stale'))
            if not handle.evaluate('el=>el.isConnected'):
                return self._stale('browser_controls_detached')
            self.active = {'rule_id':rule['rule_id'],'state':'pending','requests':[],
                           'before_text':before['text'],'attempt':len(self.attempts)+1,'dispatched':True}
            self.attempts.append(self.active)
            self.phase = 'pending'
            handle.click(timeout=1500, no_wait_after=True)
            self.page.wait_for_timeout(100)
            self.revision += 1
            after = self.observe()
            if self.locked:
                return self._result('unknown','browser_effect_unknown')
            if self.active['state']=='readonly_failed':
                return self._result('failed','browser_query_readonly_failed')
            return self._result('verified', 'browser_query_verified' if after['checks']['goal_met']
                                else 'browser_query_dispatched')
        except Exception:
            # Once dispatch may have occurred, a Python/Playwright error cannot
            # prove no effects. Only separately observed GET failures may retry.
            self._violation('browser_effect_unknown')
            return self._result('unknown','browser_effect_unknown')

    def close(self):
        if self.pw is not None:
            super().close()
