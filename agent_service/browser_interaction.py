"""Bounded, owned Edge session for approved navigation and GET search forms.

Rules are capabilities, not a sequence. Discovery mode derives query controls
from live forms; the legacy mode accepts explicitly approved selectors. A model
can only choose observed action IDs and public literal values stay fixed.
Page scripts, non-document requests, downloads and new windows are disabled.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid


class InteractionError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _validate_resume(config, resume):
    if resume is None:
        return None
    from .browser_resume import validate
    try:
        return validate(config, resume)
    except (ValueError, TypeError, KeyError):
        raise InteractionError('browser_resume_invalid') from None


def _text(value, limit):
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise InteractionError('browser_config_invalid')
    return value


def _origin(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment):
        raise InteractionError('browser_url_rejected')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise InteractionError('browser_url_rejected') from None
    return parsed.scheme, parsed.hostname.lower(), port


def _url_key(url):
    parsed = urlsplit(url)
    return _origin(url), parsed.path or '/', parsed.query


def validate_config(config):
    """Return a detached canonical scope, never launch a browser or access DNS."""
    if type(config) is dict and config.get('mode') == 'github_repo':
        from .browser_github import validate_github_config
        return validate_github_config(config)
    if type(config) is dict and config.get('mode') == 'public_search':
        from .browser_public import validate_public_config
        return validate_public_config(config)
    discovery = type(config) is dict and config.get('mode') == 'discover_query'
    required = ({'mode', 'start_url', 'query', 'required_text'} if discovery
                else {'start_url', 'allowed_paths', 'rules', 'required_text'})
    if (type(config) is not dict or not required <= config.keys()
            or config.keys() - required - ({'allow_loopback', 'headless', 'javascript'} if discovery
                                          else {'allow_loopback', 'headless'})):
        raise InteractionError('browser_config_invalid')
    value = deepcopy(config)
    value.setdefault('allow_loopback', False)
    value.setdefault('headless', False)
    if any(type(value[k]) is not bool for k in ('allow_loopback', 'headless')):
        raise InteractionError('browser_config_invalid')
    _text(value['start_url'], 2000)
    origin = _origin(value['start_url'])
    if origin[1] == 'localhost' or origin[1].endswith('.localhost'):
        if not value['allow_loopback']:
            raise InteractionError('browser_loopback_rejected')
    else:
        try:
            address = ipaddress.ip_address(origin[1])
        except ValueError:
            address = None
        if address and not address.is_global and not (address.is_loopback and value['allow_loopback']):
            raise InteractionError('browser_private_address_rejected')
    terms = value['required_text']
    if type(terms) is not list or not 1 <= len(terms) <= 3:
        raise InteractionError('browser_goal_invalid')
    for term in terms:
        _text(term, 200)
    if discovery:
        _text(value['query'], 500)
        if 'javascript' in value and type(value['javascript']) is not bool:
            raise InteractionError('browser_config_invalid')
        if value.get('javascript') and urlsplit(value['start_url']).query:
            raise InteractionError('browser_javascript_start_query_rejected')
        return value
    paths = value['allowed_paths']
    if (type(paths) is not list or not 1 <= len(paths) <= 12
            or any(type(path) is not str for path in paths)
            or len(set(paths)) != len(paths)):
        raise InteractionError('browser_paths_invalid')
    for path in paths:
        _text(path, 600)
        if not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path or '\\' in path:
            raise InteractionError('browser_paths_invalid')
    if (urlsplit(value['start_url']).path or '/') not in paths:
        raise InteractionError('browser_paths_invalid')
    rules = value['rules']
    if type(rules) is not list or not 1 <= len(rules) <= 8:
        raise InteractionError('browser_rules_invalid')
    ids = set()
    for rule in rules:
        if (type(rule) is not dict or type(rule.get('kind')) is not str
                or rule.get('kind') not in {'fill', 'click'}):
            raise InteractionError('browser_rules_invalid')
        keys = {'id', 'kind', 'selector', 'value'} if rule['kind'] == 'fill' else {
            'id', 'kind', 'selector', 'effect', 'expected_text'}
        if not keys <= rule.keys() or rule.keys() - keys - ({'expected_url'} if rule['kind'] == 'click' else set()):
            raise InteractionError('browser_rules_invalid')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', _text(rule['id'], 60)) or rule['id'] in ids:
            raise InteractionError('browser_rules_invalid')
        ids.add(rule['id'])
        _text(rule['selector'], 300)
        if rule['kind'] == 'fill':
            _text(rule['value'], 500)
        else:
            if type(rule['effect']) is not str or rule['effect'] not in {'navigate', 'query'}:
                raise InteractionError('browser_rules_invalid')
            _text(rule['expected_text'], 200)
            if 'expected_url' in rule:
                _text(rule['expected_url'], 2000)
                if (_origin(rule['expected_url']) != origin
                        or (urlsplit(rule['expected_url']).path or '/') not in paths):
                    raise InteractionError('browser_url_rejected')
    return value


def describe_config(config):
    return validate_config(config)


# This script only reads the main DOM; it never invokes page-defined callbacks.
_CONTROL = """el => {
 const tag=el.tagName.toLowerCase(), type=(el.getAttribute('type')||'').toLowerCase();
 const secret=type==='password'||type==='file'||
  /password|credit-card|cc-|one-time|username/i.test(el.getAttribute('autocomplete')||'')||
  /password|passwd|secret|token|api.?key|credential|cvv|otp/i.test(
    [el.id,el.name,el.getAttribute('aria-label')||''].join(' '));
 const labels=el.labels ? Array.from(el.labels).map(x=>x.innerText).join(' ') : '';
 const f=el.form;
 return {tag,type,secret,label:(el.getAttribute('aria-label')||labels||el.innerText||
   (type==='submit'?el.value:'')||'').slice(0,200),
   placeholder:(el.getAttribute('placeholder')||'').slice(0,200),name:el.name||'',
   value:!secret&&['input','textarea'].includes(tag)?el.value:null,
   href:tag==='a'?el.href:null, download:el.hasAttribute('download'),
   target:el.target||el.formTarget||'',readonly:!!el.readOnly,
   form:f?{method:(el.getAttribute('formmethod')||f.method).toLowerCase(),
     action:el.getAttribute('formaction')?el.formAction:f.action,
     target:el.formTarget||f.target||'',
     secret:!!f.querySelector('input[type=password],input[type=file]')}:null,
   submit:tag==='button'?(type===''||type==='submit'):(tag==='input'&&type==='submit')};
}"""


_DISCOVER = """() => {
 const path = el => {
   const parts=[];
   while (el && el.nodeType===1) {
     const tag=el.tagName.toLowerCase();
     const siblings=el.parentElement?Array.from(el.parentElement.children).filter(x=>x.tagName===el.tagName):[el];
     parts.unshift(tag+':nth-of-type('+(siblings.indexOf(el)+1)+')');
     el=el.parentElement;
   }
   return parts.join('>');
 };
 const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility!=='hidden';
 const attrs = el => Object.fromEntries(Array.from(el.attributes).map(x=>[x.name,x.value]).sort((a,b)=>a[0].localeCompare(b[0])));
 const data = el => ({selector:path(el),tag:el.tagName.toLowerCase(),type:el.type||'',
   name:el.name||'',value:el.value||'',disabled:el.matches(':disabled'),readonly:!!el.readOnly,
   visible:visible(el),attributes:attrs(el),
   label:el.getAttribute('aria-label')||(el.labels?Array.from(el.labels).map(x=>x.innerText).join(' '):'')||
      el.innerText||(el.type==='submit'?el.value:'')||'',placeholder:el.getAttribute('placeholder')||''});
 return Array.from(document.forms).slice(0,16).map(form=>({
   selector:path(form),attributes:attrs(form),role:form.getAttribute('role')||'',
   method:form.method.toLowerCase(),action:form.action,target:form.target,
   field_count:form.elements.length,fields:Array.from(form.elements).slice(0,40).map(data)
 }));
}"""

_SEARCH = re.compile(r'search|query|find|查[询找]|搜[索寻]|检索|关键[字词]', re.I)
_UNSAFE = re.compile(r'login|log.?in|sign.?in|register|subscribe|purchase|checkout|delete|remove|'
                     r'buy|order|payment|password|upload|send|transfer|'
                     r'发[送布]|登[录入]|注[册销]|删除|付款|支付|购买|订阅|上传|下单|转账', re.I)


class _EdgeSession:
    def __init__(self, config):
        self.config = validate_config(config)
        config = self.config
        self.origin = _origin(config['start_url'])
        self.discovery = config.get('mode') == 'discover_query'
        self.query_verified = False
        self.rules = config.get('rules', [])
        self.allowed_urls = {_url_key(config['start_url'])}
        self.dom_sha256 = None
        self.completed, self.failures = [], []
        self.locked, self.blocked = False, 0
        self.navigation_status = None
        self.session_id = uuid.uuid4().hex
        addresses = socket.getaddrinfo(self.origin[1], self.origin[2], type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
        if not ips or any(not ip.is_global and not (ip.is_loopback and config['allow_loopback']) for ip in ips):
            raise InteractionError('browser_dns_rejected')
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = None
        try:
            address = str(ips[0])
            if ':' in address:
                address = '[' + address + ']'
            self.browser = self.pw.chromium.launch(channel='msedge', headless=config['headless'],
                timeout=15000, chromium_sandbox=True,
                args=['--no-proxy-server', f'--host-resolver-rules=MAP {self.origin[1]} {address}'])
            self.context = self.browser.new_context(java_script_enabled=False,
                accept_downloads=False, service_workers='block')
            self.page = self.context.new_page()
            self.page.set_default_timeout(3500)
            self.page.set_default_navigation_timeout(6000)
            self.context.route('**/*', self._route)
            self.page.on('response', self._response)
            self.context.on('page', lambda page: page.close() if page != self.page else None)
            self.page.on('dialog', lambda dialog: dialog.dismiss())
            response = self.page.goto(config['start_url'], wait_until='domcontentloaded')
            if not response or not response.ok or not self._allowed(self.page.url):
                raise InteractionError('browser_start_failed')
        except BaseException:
            self.close()
            raise

    def _allowed(self, url):
        try:
            return (_origin(url) == self.origin
                    and (_url_key(url) in self.allowed_urls if self.discovery else
                         (urlsplit(url).path or '/') in self.config['allowed_paths']))
        except (ValueError, TypeError):
            return False

    def _route(self, route):
        req = route.request
        if (req.method == 'GET' and req.is_navigation_request()
                and req.frame == self.page.main_frame and self._allowed(req.url)):
            route.continue_()
        else:
            self.blocked += 1
            route.abort()

    def _response(self, response):
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.navigation_status = response.status

    def _control(self, rule):
        locator = self.page.locator(rule['selector'])
        if locator.count() != 1 or not locator.is_visible() or not locator.is_enabled():
            return None
        info = locator.evaluate(_CONTROL)
        if info['secret']:
            return None
        info['control_id'] = _digest({'rule':rule['id'], 'selector':rule['selector'],
                                      'info':info})[:24]
        return locator, info

    def _discover_rules(self):
        """Derive query capabilities from current real forms, never page claims."""
        forms = self.page.evaluate(_DISCOVER)
        self.dom_sha256 = _digest(forms)
        rules = []
        for form in forms:
            try:
                target = urlsplit(form['action'])
                if (form['field_count'] > 40 or form['method'] != 'get'
                        or form['target'] not in {'', '_self'} or target.query
                        or _origin(form['action']) != self.origin):
                    continue
                fields = form['fields']
                # Deny sensitive or state-changing form purposes before filling.
                hints = form['action']+' '+json.dumps(form['attributes'],ensure_ascii=False)
                hints += ' '+' '.join(json.dumps({k:f[k] for k in
                    ('type','name','label','placeholder','attributes')},ensure_ascii=False) for f in fields)
                if _UNSAFE.search(hints) or any(f['type'] in {'password','file'} for f in fields):
                    continue
                successful = [f for f in fields if f['name'] and not f['disabled']
                              and f['type'] not in {'button','submit','reset'}]
                if len(successful) != 1:
                    continue
                field = successful[0]
                if (field['tag'] != 'input' or field['type'] not in {'text','search'}
                        or not field['visible'] or field['readonly']):
                    continue
                field_hint = field['label']+' '+field['placeholder']+' '+field['name']
                field_search = (field['type'] == 'search' or form['role'] == 'search'
                                or field['name'].casefold() in {'q','query','search','keyword'}
                                or bool(_SEARCH.search(field_hint)))
                buttons = [f for f in fields if f['type'] == 'submit' and f['visible']
                           and not f['disabled'] and not f['name']]
                valid = []
                for button in buttons:
                    if not field_search and not _SEARCH.search(button['label']):
                        continue
                    # Overrides are allowed only if they preserve the query form.
                    attrs = button['attributes']
                    if (attrs.get('formmethod','get').lower() != 'get'
                            or attrs.get('formtarget','') not in {'','_self'}
                            or 'formaction' in attrs or 'download' in attrs):
                        continue
                    valid.append(button)
                if not valid:
                    continue
                identity = _digest({'url':self.page.url,'form':form['selector'],
                                    'action':form['action'],'input':field['selector'],
                                    'name':field['name']})[:16]
                fill = {'id':'discovered-fill-'+identity,'kind':'fill',
                        'selector':field['selector'],'value':self.config['query']}
                rules.append(fill)
                for button in valid:
                    button_id = _digest(button['selector'])[:10]
                    rules.append({'id':'discovered-query-'+identity+'-'+button_id,
                                  'kind':'click','effect':'query','selector':button['selector'],
                                  'expected_text':self.config['required_text'][0]})
                # Keep the same model candidate bound as the shared agent.
                if len(rules) >= 8:
                    break
            except (ValueError,KeyError,TypeError):
                continue
        self.rules = rules[:8]

    def _eligible(self, rule, info):
        if rule['kind'] == 'fill':
            return (info['tag'] in {'input', 'textarea'}
                    and info['type'] in {'', 'text', 'search', 'url'}
                    and not info['readonly']
                    and info['value'] != rule['value'])
        if rule['effect'] == 'navigate':
            return (info['tag'] == 'a' and not info['download']
                    and info['target'] in {'', '_self'} and self._allowed(info['href']))
        if not (info['submit'] and info['form'] and info['form']['method'] == 'get'
                and info['form']['target'] in {'', '_self'}
                and not info['form']['secret']
                and (_origin(info['form']['action']) == self.origin if self.discovery
                     else self._allowed(info['form']['action']))
                and not urlsplit(info['form']['action']).query):
            return False
        fills = [r for r in self.rules if r['kind'] == 'fill']
        matching = 0
        for fill in fills:
            control = self._control(fill)
            if not control:
                continue
            same_form = self.page.locator(rule['selector']).evaluate(
                '(el, selector) => el.form === document.querySelector(selector)?.form', fill['selector'])
            if same_form:
                matching += 1
                if control[1]['value'] != fill['value']:
                    return False
        # A form is a query capability only when every successful input is a
        # specifically approved public literal. Hidden tokens and submit-button
        # name/value pairs are not covered by approval of the visible query.
        safe = self.page.locator(rule['selector']).evaluate("""(button, fills) => {
          if (button.name) return false;
          const approved=fills.map(r=>({el:document.querySelector(r.selector),value:r.value}));
          for (const el of button.form.elements) {
            if (!el.name || el.disabled || ['button','submit','reset'].includes(el.type)) continue;
            const match=approved.find(r=>r.el===el);
            if (!match || el.type==='hidden' || el.value!==match.value) return false;
          }
          return true;
        }""", [{'selector':r['selector'], 'value':r['value']} for r in fills])
        return matching > 0 and safe

    def observe(self):
        if not self._allowed(self.page.url):
            raise InteractionError('browser_url_rejected')
        if self.discovery:
            self._discover_rules()
        # innerText excludes hidden/script content and input/password values.
        text = re.sub(r'\s+', ' ', self.page.locator('body').inner_text()).strip()[:12000]
        controls, actions = [], []
        failed = {x['rule_id'] for x in self.failures}
        for rule in self.rules:
            try:
                control = self._control(rule)
                if not control:
                    continue
                _, info = control
                public = {k:v for k,v in info.items() if k != 'value'}
                if rule['kind'] == 'fill':
                    public['value_matches'] = info['value'] == rule['value']
                controls.append(dict(public, rule_id=rule['id']))
                if (not self.locked and rule['id'] not in self.completed and rule['id'] not in failed
                        and self._eligible(rule, info)):
                    candidate = {'kind':'browser_' + rule['kind'], 'rule_id':rule['id'],
                                 'control_id':info['control_id']}
                    if self.discovery:
                        candidate['label'] = info['label'] or info['placeholder'] or info['name']
                    actions.append(candidate)
            except Exception:
                # Invalid/missing selectors are unavailable capabilities, not IO.
                continue
        snap = {'session_id':self.session_id, 'url':self.page.url,
                'title':self.page.title()[:300], 'text':text, 'controls':controls,
                'completed_rules':list(self.completed), 'failures':deepcopy(self.failures),
                'locked':self.locked,
                'checks':{'goal_met':not self.locked and self.navigation_status is not None
                          and 200 <= self.navigation_status < 300
                          and (not self.discovery or self.query_verified)
                          and all(t in text for t in self.config['required_text'])}}
        if self.discovery:
            snap['discovery'] = {'mode':'discover_query','dom_sha256':self.dom_sha256,
                                 'candidate_rules':len(self.rules),'query_verified':self.query_verified}
        state = _digest(snap)
        snap['state_id'] = state
        snap['actions'] = [dict(a, state_id=state) for a in actions]
        return snap

    def execute(self, action):
        before = self.observe()
        if self.locked:
            return {'ok':False, 'state':'unknown', 'code':'browser_session_locked', 'observation':before}
        if action not in before['actions']:
            return {'ok':False, 'state':'failed', 'code':'browser_action_stale', 'observation':before}
        rule = next(r for r in self.rules if r['id'] == action['rule_id'])
        control = self._control(rule)
        if control is None or control[1]['control_id'] != action['control_id']:
            return {'ok':False, 'state':'failed', 'code':'browser_action_stale', 'observation':self.observe()}
        locator, info = control
        # Detected before the action: safe to recover to another observed rule.
        if rule['kind'] == 'click' and rule['effect'] == 'navigate' and 'expected_url' in rule and info['href'] != rule['expected_url']:
            self.failures.append({'rule_id':rule['id'], 'code':'browser_target_mismatch'})
            return {'ok':False, 'state':'failed', 'code':'browser_target_mismatch', 'observation':self.observe()}
        try:
            if rule['kind'] == 'fill':
                if self.discovery and action not in self.observe()['actions']:
                    return {'ok':False,'state':'failed','code':'browser_action_stale','observation':self.observe()}
                locator.fill(rule['value'])
                verified = locator.input_value() == rule['value']
            else:
                if self.discovery:
                    # Only the exact GET request derived from the revalidated
                    # form is released. Neither model nor page can add fields.
                    target = locator.evaluate("""(button, query) => {
                      const field=Array.from(button.form.elements).find(el=>el.name&&!el.disabled&&
                        !['button','submit','reset'].includes(el.type));
                      const url=new URL(button.form.action);
                      url.search=new URLSearchParams([[field.name,query]]).toString();
                      return url.href;
                    }""", self.config['query'])
                    if _origin(target) != self.origin:
                        raise InteractionError('browser_url_rejected')
                    current = self.observe()
                    if action not in current['actions']:
                        return {'ok':False,'state':'failed','code':'browser_action_stale','observation':current}
                    self.allowed_urls.add(_url_key(target))
                locator.click()
                self.page.wait_for_load_state('domcontentloaded')
                # Separate DOM read, not action return text, determines success.
                after = self.observe()
                verified = (rule['expected_text'] in after['text']
                    and ('expected_url' not in rule or self.page.url == rule['expected_url'])
                    and self.navigation_status is not None and 200 <= self.navigation_status < 300
                    and (after['url'] != before['url'] or after['text'] != before['text']))
            if not verified:
                raise InteractionError('browser_verification_failed')
            if self.discovery and rule['kind'] == 'click':
                self.query_verified = True
            self.completed.append(rule['id'])
            return {'ok':True, 'state':'verified', 'code':'browser_' + rule['kind'] + '_verified',
                    'verification':{'rule_id':rule['id'], 'independent_read':True},
                    'observation':self.observe()}
        except Exception:
            self.locked = True
            self.failures.append({'rule_id':rule['id'], 'code':'browser_effect_unknown'})
            return {'ok':False, 'state':'unknown', 'code':'browser_effect_unknown', 'observation':self.observe()}

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            self.pw.stop()


class BrowserInteraction:
    """JSONL worker owned by a Windows Job; never attaches to a user's Edge."""
    def __init__(self, config, *, stop=lambda: False, deadline=None, clock=time.monotonic, resume=None):
        self.config = validate_config(config)
        self.resume = _validate_resume(self.config, resume)
        self.stop, self.deadline, self.clock = stop, deadline, clock
        self.process, self.owner = None, None
        self.replies = queue.Queue()
        self.closed, self.unknown = False, False

    def _start(self):
        if self.closed:
            raise InteractionError('browser_session_closed')
        if self.process:
            return
        options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       cwd=Path(__file__).resolve().parents[1])
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        self.process = subprocess.Popen([sys.executable, '-m', 'agent_service.browser_interaction'], **options)
        try:
            if os.name == 'nt':
                from .process import WindowsJob
                self.owner = WindowsJob(self.process)
            threading.Thread(target=self._reader, args=(self.process.stdout,), daemon=True).start()
            self.process.stdin.write(b'1')
            self.process.stdin.flush()
            self._send({'op':'init', 'config':self.config, 'resume':self.resume})
        except BaseException:
            self.close()
            raise

    def _reader(self, pipe):
        try:
            while True:
                line = pipe.readline(256 * 1024)
                if not line:
                    break
                try:
                    self.replies.put(json.loads(line))
                except (ValueError, UnicodeError):
                    break
        finally:
            self.replies.put({'error':'browser_worker_failed'})

    def _send(self, value, *, wall_deadline=None):
        until = min(self.clock() + 30, self.deadline if self.deadline is not None else float('inf'))
        wall_until = time.monotonic() + 30
        if wall_deadline is not None:
            wall_until = min(wall_until, wall_deadline)
        if self.stop() or self.clock() >= until or time.monotonic() >= wall_until:
            self.close()
            raise InteractionError('browser_cancelled')
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')
        self.process.stdin.flush()
        while True:
            if self.stop() or self.clock() >= until or time.monotonic() >= wall_until:
                self.close()
                raise InteractionError('browser_cancelled_or_timeout')
            try:
                reply = self.replies.get(timeout=0.05)
            except queue.Empty:
                continue
            if 'error' in reply:
                self.close()
                raise InteractionError(reply['error'])
            return reply

    def observe(self):
        self._start()
        return self._send({'op':'observe'})

    def enter_manual(self):
        self._start()
        return self._send({'op':'enter_manual'})

    def human_tick(self, *, wall_deadline=None):
        return self._send({'op':'human_tick'}, wall_deadline=wall_deadline)

    def resume_manual(self):
        return self._send({'op':'resume_manual'})

    def execute(self, action):
        if self.unknown:
            return {'ok':False, 'state':'unknown', 'code':'browser_session_locked'}
        try:
            self._start()
            result = self._send({'op':'execute', 'action':deepcopy(action)})
        except (InteractionError, OSError):
            self.unknown = True
            return {'ok':False, 'state':'unknown', 'code':'browser_effect_unknown'}
        self.unknown = result.get('state') == 'unknown'
        return result

    def close(self):
        self.closed = True
        if not self.process:
            return
        if self.owner:
            self.owner.close()
            self.owner = None
        elif os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()


def _worker():
    if sys.stdin.buffer.read(1) != b'1':
        return
    session = None
    try:
        for line in sys.stdin.buffer:
            try:
                value = json.loads(line)
                if value['op'] == 'init' and session is None:
                    if value['config'].get('mode') == 'github_repo':
                        from .browser_github import GithubEdgeSession
                        session = GithubEdgeSession(value['config'], resume=value.get('resume'))
                    elif value['config'].get('mode') == 'public_search':
                        from .browser_public import PublicSearchEdgeSession
                        if value['config'].get('site') == 'ikea_us':
                            from .browser_commerce import CommerceSearchEdgeSession
                            session = CommerceSearchEdgeSession(value['config'], resume=value.get('resume'))
                        else:
                            session = PublicSearchEdgeSession(value['config'], resume=value.get('resume'))
                    elif value['config'].get('javascript') is True:
                        if value.get('resume') is not None:
                            raise InteractionError('browser_resume_invalid')
                        from .browser_javascript import JavascriptEdgeSession
                        session = JavascriptEdgeSession(value['config'])
                    else:
                        if value.get('resume') is not None:
                            raise InteractionError('browser_resume_invalid')
                        session = _EdgeSession(value['config'])
                    result = {'ready':True}
                elif value['op'] == 'observe':
                    result = session.observe()
                elif value['op'] == 'execute':
                    result = session.execute(value['action'])
                elif value['op'] in {'enter_manual', 'human_tick', 'resume_manual'} and isinstance(
                        session.config, dict) and session.config.get('mode') == 'github_repo':
                    result = getattr(session, value['op'])()
                else:
                    raise InteractionError('browser_operation_rejected')
            except Exception as exc:
                result = {'error':str(exc) if isinstance(exc, InteractionError) else 'browser_worker_failed'}
            sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8') + b'\n')
            sys.stdout.buffer.flush()
            if 'error' in result:
                break
    finally:
        if session:
            session.close()


if __name__ == '__main__':
    _worker()
