"""Bounded GitHub repository search/read in a fresh, browser owned session.

Native browser networking owns authentication cookies. They are never read,
copied, serialized, or replayed by Python. During an explicit human handoff no
DOM observations occur; short ticks only keep Playwright's event loop alive.
"""
from copy import deepcopy
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit
import uuid

from .browser_interaction import InteractionError, _digest, _text, _validate_resume


_ASSET_HOSTS = {'github.githubassets.com', 'avatars.githubusercontent.com'}
_SEARCH = re.compile(r'search or jump|search github|search repositories', re.I)


def validate_github_config(config):
    required = {'mode', 'start_url', 'query', 'required_text'}
    if (type(config) is not dict or not required <= config.keys()
            or config.keys() - required - {'headless', 'human_login'}
            or config['mode'] != 'github_repo'):
        raise InteractionError('browser_github_config_invalid')
    value = deepcopy(config)
    _text(value['start_url'], 1000)
    parsed = urlsplit(value['start_url'])
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com'
            or parsed.query or parsed.fragment or not re.fullmatch(
                r'/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}', parsed.path)
            or parsed.path.rsplit('/', 1)[-1] in {'.', '..'}):
        raise InteractionError('browser_github_target_rejected')
    owner, repo = parsed.path.strip('/').split('/')
    if value['query'] != f'{repo} user:{owner}':
        raise InteractionError('browser_github_query_rejected')
    terms = value['required_text']
    if type(terms) is not list or not 1 <= len(terms) <= 3:
        raise InteractionError('browser_goal_invalid')
    for term in terms:
        _text(term, 200)
    for key in ('headless', 'human_login'):
        value.setdefault(key, False)
        if type(value[key]) is not bool:
            raise InteractionError('browser_github_config_invalid')
    if value['headless'] and value['human_login']:
        raise InteractionError('browser_github_human_requires_visible_browser')
    return value


_DOM = r"""({registry,target,read}) => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const identity=e=>{if(!registry.ids.has(e))registry.ids.set(e,++registry.next);return registry.ids.get(e)};
 const path=e=>{const a=[];while(e&&e.nodeType===1){const s=e.parentElement?
 Array.from(e.parentElement.children).filter(x=>x.tagName===e.tagName):[e];
 a.unshift(e.tagName.toLowerCase()+':nth-of-type('+(s.indexOf(e)+1)+')');e=e.parentElement}return a.join('>')};
 const controls=[],search_semantics=[],candidateCounts=new Map(),navigationEffects=new Set();
 const openerPhrase=value=>/^(?:search or jump to(?:, type \/ to search|\.{3}|…)?|type \/ to search)$/i.test(value);
 const inputPhrase=value=>/^(?:search(?: or jump to(?:\.{3}|…)?| github| repositories)?)$/i.test(value);
 const normalized=value=>(value||'').replace(/\s+/g,' ').trim();
 for(const e of document.querySelectorAll('button,input,a')){
  if(!visible(e)||e.disabled||e.readOnly||e.getAttribute('aria-disabled')==='true')continue;
  const label=e.getAttribute('aria-label')||e.getAttribute('placeholder')||'';
  // Retain only static search UI phrases. Unknown labels are represented by a
  // fixed marker: account names, arbitrary labels and all field values stay
  // inside the page. This also diagnoses unsupported authenticated headers.
  if(e.tagName==='BUTTON'||(e.tagName==='INPUT'&&e.getAttribute('role')==='combobox')){
   const candidates=[['aria-label',e.getAttribute('aria-label')||''],
    ['placeholder',e.getAttribute('placeholder')||''],
    ['visible_text',e.tagName==='BUTTON'?(e.innerText||''):'']];
   for(const [source,raw] of candidates){
    const value=raw.replace(/\s+/g,' ').trim();
    if(!/search/i.test(value))continue;
    const known=/^(?:search(?: or jump to(?:, type \/ to search|\.{3}|…)?| github| repositories| all of github)?|type \/ to search)$/i.test(value);
    if(search_semantics.length<12)search_semantics.push({tag:e.tagName.toLowerCase(),
     role:e.getAttribute('role')==='combobox'?'combobox':e.tagName==='BUTTON'?'button':'',
     source,label:known?value:'unrecognized_search_label'});
   }
  }
  const outsideMain=!e.closest('main');
  const header=outsideMain&&!!e.closest('header,[role="banner"],qbsearch-input');
  const searchRegion=outsideMain&&!!e.closest('header,[role="banner"],qbsearch-input,[role="dialog"],dialog');
  const input=e.tagName==='INPUT'&&searchRegion&&['text','search'].includes(e.type)&&
    (e.getAttribute('role')==='combobox'||e.type==='search'||!!e.closest('qbsearch-input'))&&
    inputPhrase(normalized(label))&&
    !/password|username|token|secret|one-time/i.test([e.name,e.autocomplete,label].join(' '));
  const opener=e.tagName==='BUTTON'&&header&&e.type==='button'&&
    [label,e.innerText].some(value=>openerPhrase(normalized(value)));
  const repo=e.tagName==='A'&&e.closest('main')&&e.href===target&&!e.hasAttribute('download')&&
    (!e.target||e.target==='_self');
  let repositoryFilter=false;
  if(e.tagName==='A'&&e.closest('main')&&!e.hasAttribute('download')&&(!e.target||e.target==='_self')){
   // Anchors without href (including README section markers) are not links.
   // An unrelated malformed destination must not abort the whole observation.
   try {
   const u=new URL(e.href),pairs=Array.from(u.searchParams.entries());
   repositoryFilter=u.origin==='https://github.com'&&!u.username&&!u.password&&u.pathname==='/search'&&!u.hash&&
    pairs.length===2&&new Set(pairs.map(x=>x[0])).size===2&&
    u.searchParams.get('q')===read.query&&u.searchParams.get('type')==='repositories'&&
    /^Repositories(?:\s|$)/i.test(normalized(e.getAttribute('aria-label')||e.innerText));
   } catch {}
  }
  if(input||opener||repo||repositoryFilter){
   const kind=input?'input':opener?'opener':repo?'repo':'repositories_filter';
   candidateCounts.set(kind,(candidateCounts.get(kind)||0)+1);
   // Only these two link kinds have fully validated fixed GET destinations.
   // Repeated repository labels in code results represent the same approved
   // navigation. Inputs/openers retain distinct identities and ambiguity.
   if(repo||repositoryFilter){if(navigationEffects.has(kind))continue;navigationEffects.add(kind)}
   controls.push({identity:identity(e),selector:path(e),kind,value_matches:input?e.value===read.query:false});
  }
 }
 const repository=read.repo?document.querySelector('meta[name="octolytics-dimension-repository_nwo"]')?.content||'':'';
 return {controls:controls.slice(0,16),controls_overflow:controls.length>16,
  candidate_targets:Array.from(candidateCounts,([kind,count])=>({kind,count,
   target:kind==='repo'?'approved_repository':kind==='repositories_filter'?
    'approved_repository_search':kind==='input'?'global_search_input':'global_search_opener'})),
  search_semantics,main_present:!!document.querySelector('main'),
  repo_matches:repository.toLowerCase()===read.name.toLowerCase(),
  text:read.repo?(document.querySelector('main')?.innerText||'').replace(/\s+/g,' ').trim().slice(0,24000):''};
}"""


class GithubEdgeSession:
    MAX_WAITS = 8
    MAX_OBSERVATION_ATTEMPTS = 3

    def __init__(self, config, resume=None):
        self.config = validate_github_config(config)
        self.resume = _validate_resume(self.config, resume)
        self._safe_resume = deepcopy(self.resume)
        self._handoff_resume = None
        self._historical_search = False
        self.session_id = uuid.uuid4().hex
        self.manual = False
        self.human_needed = self.config['human_login']
        self.human_reason = 'github_login_required'
        self.challenge_blocked = False
        self.authenticated = False
        self.locked = False
        self.blocked = 0
        self.completed, self.failures = [], []
        self.wait_count = 0
        self.query_verified = self.repo_clicked = False
        self.search_pending = self.repo_pending = False
        self.allowed_target = True  # bootstrap GET, never establishes success
        self.search_status = self.repo_status = None
        self.repository_search_status = None
        self.repository_search_finished = False
        self.bootstrap_status = None
        self.search_finished = self.repo_finished = False
        self.registry = None
        self.document_id = None
        self.rules = []
        self.navigation_generation = 0
        self.observation_retries = 0
        self.last_observation_error = None
        self.browser = self.pw = None
        pins = []
        for host in ['github.com', *sorted(_ASSET_HOSTS)]:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
            if not ips or any(not a.is_global for a in ips):
                raise InteractionError('browser_github_dns_rejected')
            address = str(ips[0])
            pins.append(f'MAP {host} [{address}]' if ':' in address else f'MAP {host} {address}')
        from playwright.sync_api import sync_playwright
        # Reuse only the repository's existing explicit, credential-free HTTPS
        # proxy configuration. The public Edge probe established direct routing
        # is unavailable on this host. No proxy authentication or cookie replay.
        from .research import _public_search_proxy
        proxy = _public_search_proxy()
        self.pw = sync_playwright().start()
        try:
            args = ['--disable-background-networking', '--host-resolver-rules=' + ','.join(pins)]
            network = {'proxy':{'server':proxy}} if proxy else {}
            if not proxy:
                args.append('--no-proxy-server')
            self.browser = self.pw.chromium.launch(channel='msedge', headless=self.config['headless'],
                timeout=15000, chromium_sandbox=True, args=args, **network)
            self.context = self.browser.new_context(java_script_enabled=True,
                accept_downloads=False, service_workers='block')
            self.context.route('**/*', self._route)
            self.context.route_web_socket('**/*', self._socket)
            self.context.on('page', self._popup)
            self.page = self.context.new_page()
            self._bind_page(self.page)
            self._goto_target()
        except BaseException:
            self.close()
            raise

    def _bind_page(self, page):
        page.set_default_timeout(2500)
        page.set_default_navigation_timeout(12000)
        page.on('response', self._response)
        page.on('requestfinished', self._finished)
        page.on('framenavigated', self._navigated)
        page.on('download', lambda d: self._violation('browser_download_blocked'))
        page.on('dialog', self._dialog)
        self.registry = None

    def _navigated(self, frame):
        if not self.manual and frame == self.page.main_frame:
            self.registry = None
            self.navigation_generation += 1

    def _violation(self, code):
        self.locked = True
        if not self.failures or self.failures[-1]['code'] != code:
            self.failures.append({'code':code})

    def _popup(self, page):
        if self.manual or getattr(self, '_creating_page', False) or not hasattr(self, 'page') or page == self.page:
            return
        self._violation('browser_popup_blocked')
        page.close()

    def _dialog(self, dialog):
        if not self.manual:
            self._violation('browser_dialog_blocked')
            dialog.dismiss()

    def _socket(self, ws):
        # Automatic sockets are mocked; native human sockets remain browser
        # owned. No URL or payload enters observations in either phase.
        if self.manual:
            ws.connect_to_server()
        else:
            self.blocked += 1

    def _is_search(self, url):
        try:
            p = urlsplit(url)
            pairs = parse_qsl(p.query, keep_blank_values=True)
            return (p.scheme == 'https' and p.netloc == 'github.com' and p.path == '/search'
                and not p.fragment and len(pairs) in {1, 2} and len(dict(pairs)) == len(pairs)
                and dict(pairs).get('q') == self.config['query']
                and set(dict(pairs)) <= {'q', 'type'}
                and dict(pairs).get('type', 'code').lower() in {'repositories','code'})
        except ValueError:
            return False

    def _repository_search(self, url):
        return self._is_search(url) and dict(parse_qsl(urlsplit(url).query)).get('type', '').lower() == 'repositories'

    def _request_allowed(self, request):
        p = urlsplit(request.url)
        if (request.method != 'GET' or p.scheme != 'https' or p.username or p.password
                or p.port not in {None, 443}):
            return False
        if request.is_navigation_request():
            return request.frame == self.page.main_frame and (
                request.url == self.config['start_url'] and self.allowed_target
                or self.search_pending and self._is_search(request.url))
        if p.hostname in _ASSET_HOSTS:
            return request.resource_type in {'script', 'stylesheet', 'font', 'image'} and not p.query
        if self.repo_pending and request.url == self.config['start_url']:
            return request.resource_type in {'fetch', 'xhr'}
        return bool(p.netloc == 'github.com' and self.search_pending and self._is_search(request.url)
                    and request.resource_type in {'fetch', 'xhr'})

    def _route(self, route):
        if self.manual:
            route.continue_()
            return
        request = route.request
        try:
            allowed = self._request_allowed(request)
        except (ValueError, AttributeError):
            allowed = False
        if allowed:
            route.continue_()
        else:
            self.blocked += 1
            if request.is_navigation_request():
                p = urlsplit(request.url)
                host = (p.hostname or '').lower()
                challenge = (p.scheme == 'https' and request.frame != self.page.main_frame and
                    any(host == domain or host.endswith('.'+domain)
                        for domain in ('octocaptcha.com', 'arkoselabs.com')))
                if challenge:
                    self.human_needed = self.challenge_blocked = True
                    self.human_reason = 'github_verification_required'
                elif p.hostname == 'github.com' and p.path in {'/login', '/session', '/sessions'}:
                    self.human_needed = True
                    self.human_reason = 'github_login_required'
                else:
                    self._violation('browser_github_navigation_blocked')
            route.abort()

    def _response(self, response):
        if self.manual:
            return
        if self.search_pending and self._is_search(response.url):
            self.search_status = response.status
            if self._repository_search(response.url):
                self.repository_search_status = response.status
        if self.repo_pending and response.url == self.config['start_url']:
            self.repo_status = response.status

    def _finished(self, request):
        if self.manual:
            return
        if self.search_pending and self._is_search(request.url):
            self.search_finished = True
            if self._repository_search(request.url):
                self.repository_search_finished = True
        if self.repo_pending and request.url == self.config['start_url']:
            self.repo_finished = True

    def _goto_target(self):
        phase = self.resume['phase'] if self.resume else None
        target = self.resume['url'] if self.resume else self.config['start_url']
        self.query_verified = self.repo_clicked = phase == 'repository_opened'
        self._historical_search = phase == 'repository_opened'
        self.search_pending = phase == 'search_results'
        self.repo_pending = phase == 'repository_opened'
        self.search_status = self.repo_status = self.repository_search_status = None
        self.search_finished = self.repo_finished = self.repository_search_finished = False
        self.bootstrap_status = None
        self.allowed_target = True
        try:
            response = self.page.goto(target, wait_until='domcontentloaded')
            self.bootstrap_status = response.status if response else None
        except Exception:
            if not self.human_needed:
                raise InteractionError('browser_github_start_failed') from None
        finally:
            self.allowed_target = False
            self.registry = None

    def _safe_human(self):
        return {'reason':self.human_reason, 'site':'github.com',
                'target_url':self.config['start_url']}

    def enter_manual(self):
        if self.locked or self.config['headless'] or not self.human_needed or self.manual:
            raise InteractionError('browser_github_handoff_rejected')
        if self.repo_clicked and (not self._safe_resume or self._safe_resume['phase'] != 'repository_opened'):
            # Returning to the last search would offer an already dispatched
            # repository click again, without independent proof of its result.
            self._violation('browser_effect_unknown')
            raise InteractionError('browser_github_unverified_repository_opening')
        if self.search_pending and self._safe_resume is None:
            self._violation('browser_effect_unknown')
            raise InteractionError('browser_github_unverified_search_submission')
        self._handoff_resume = deepcopy(self._safe_resume)
        self.manual = True
        self.registry, self.rules = None, []
        self.document_id = None
        self.query_verified = self.repo_clicked = False
        self.search_pending = self.repo_pending = False
        # The phase gate releases native networking only for the human browser.
        # Neither this method nor ticks inspect login DOM, cookies or headers.
        if self.human_reason != 'github_verification_required':
            try:
                target = self._handoff_resume['url'] if self._handoff_resume else self.config['start_url']
                self.page.goto('https://github.com/login?' + urlencode({'return_to':target}),
                               wait_until='commit', timeout=12000)
            except Exception:
                pass  # human may complete navigation; no raw exception returned
        elif self.challenge_blocked:
            # Reload this same page under human ownership so the previously
            # aborted embedded challenge can actually load. No login DOM read.
            try:
                self.page.reload(wait_until='commit', timeout=12000)
            except Exception:
                pass
            self.challenge_blocked = False
        return {'manual':True, 'human_required':self._safe_human()}

    def human_tick(self):
        if not self.manual:
            raise InteractionError('browser_github_handoff_inactive')
        # No DOM, URL, request, page title or screenshot observation here.
        self.page.wait_for_timeout(200)
        return {'manual':True}

    def resume_manual(self):
        if not self.manual:
            raise InteractionError('browser_github_handoff_inactive')
        self.manual = False
        old = list(self.context.pages)
        self._creating_page = True
        self.page = self.context.new_page()
        self._creating_page = False
        self._bind_page(self.page)
        for page in old:
            page.close()
        self.resume = deepcopy(self._handoff_resume)
        self._goto_target()
        self.authenticated = self._authenticated()
        challenge = self._challenge()
        require_auth = self.config['human_login'] or self.human_reason == 'github_login_required'
        ready = (not challenge and (self.authenticated or not require_auth)
                 and self.bootstrap_status is not None and 200 <= self.bootstrap_status < 300)
        self.human_needed = not ready
        if challenge:
            self.human_reason = 'github_verification_required'
        self.wait_count = 0
        return {'resumed':ready, 'authenticated':self.authenticated}

    def _remember_phase(self, phase):
        url = (self.config['start_url'] if phase == 'repository_opened' else
               'https://github.com/search?' + urlencode({'q':self.config['query'], 'type':'repositories'}))
        self._safe_resume = {'version':1, 'mode':'github_repo', 'phase':phase,
                             'url':url, 'config_sha256':_digest(self.config)}

    def _authenticated(self):
        if self.page.url != self.config['start_url'] and not self._is_search(self.page.url):
            return False
        return self.page.evaluate("() => !!document.querySelector('meta[name=\"user-login\"]')?.content")

    def _challenge(self):
        # Boolean structural signal only: challenge/authentication pages never
        # enter the main-text or controls snapshot.
        return self.page.evaluate("""() => !!document.querySelector(
            'iframe[src*="octocaptcha"],iframe[src*="arkoselabs"],iframe[src*="captcha"],'+
            '[id*="captcha"]')""")

    def _dom(self):
        if self.registry is None:
            self.registry = self.page.evaluate_handle('({ids:new WeakMap(),next:0})')
            self.document_id = uuid.uuid4().hex
        return self.page.evaluate(_DOM, {'registry':self.registry,
            'target':self.config['start_url'], 'read':{'query':self.config['query'],
            'name':urlsplit(self.config['start_url']).path.strip('/'),
            'repo':self.page.url == self.config['start_url']}})

    def _quiet_observation(self):
        """No browser calls, including when the page/context is already gone."""
        human = (self.manual or self.human_needed) and not self.locked
        snap = {'session_id':self.session_id, 'url':self.config['start_url'],
            'title':'GitHub', 'text':'', 'controls':[], 'completed_rules':list(self.completed),
            'failures':deepcopy(self.failures), 'locked':self.locked,
            'checks':{'goal_met':False}, 'human_required':self._safe_human() if human else False,
            'discovery':{'mode':'github_repo', 'phase':'human' if human else None,
                'authenticated':False, 'query_verified':False, 'repository_verified':False,
                'observation_retries':self.observation_retries,
                'last_observation_error':self.last_observation_error,
                'resume':deepcopy(self.resume)}}
        snap['state_id'] = _digest(snap)
        snap['actions'] = []
        return snap

    @staticmethod
    def _read_error(exc):
        from playwright.sync_api import Error
        if isinstance(exc, InteractionError) and str(exc) == 'browser_github_page_changed':
            return 'document_changed'
        if isinstance(exc, Error):
            message = str(exc)
            if ('Execution context was destroyed' in message or
                    'JSHandles can be evaluated only in the context they were created' in message):
                return 'document_context_changed'
        return 'observation_failed'

    def observe(self):
        for attempt in range(self.MAX_OBSERVATION_ATTEMPTS):
            if self.manual or self.human_needed or self.locked:
                return self._quiet_observation()
            try:
                return self._observe_once()
            except Exception as exc:
                self.last_observation_error = self._read_error(exc)
                self.registry, self.rules = None, []
                if (self.last_observation_error == 'observation_failed' or
                        attempt + 1 == self.MAX_OBSERVATION_ATTEMPTS):
                    self._violation('browser_observation_unavailable')
                    return self._quiet_observation()
                # Re-observe only. No navigation, input, or dispatched effect is repeated.
                self.observation_retries += 1
                if self.manual or self.human_needed or self.locked:
                    return self._quiet_observation()
                try:
                    self.page.wait_for_timeout(150)
                except Exception:
                    self._violation('browser_observation_unavailable')
                    return self._quiet_observation()

    def _observe_once(self):
        generation = self.navigation_generation
        url = self.page.url
        if url != self.config['start_url'] and not self._is_search(url):
            self._violation('browser_github_navigation_blocked')
            return self._quiet_observation()
        if self._challenge():
            self.human_needed = True
            self.human_reason = 'github_verification_required'
        if self.manual or self.human_needed or self.locked:
            return self._quiet_observation()
        if self.config['human_login'] and not self._authenticated():
            self.human_needed = True
        if self.manual or self.human_needed or self.locked:
            return self._quiet_observation()
        dom = self._dom()
        if self.manual or self.human_needed or self.locked:
            return self._quiet_observation()
        if generation != self.navigation_generation or url != self.page.url:
            raise InteractionError('browser_github_page_changed')
        if self.search_pending and self.repository_search_finished and self.repository_search_status is not None:
            if (200 <= self.repository_search_status < 300 and self._repository_search(url)
                    and (not self.resume or self.resume['phase'] != 'search_results'
                         or url == self.resume['url'])):
                self.query_verified = True
        repository_opened = bool(self.query_verified and self.repo_clicked and self.repo_finished
            and self.repo_status is not None and 200 <= self.repo_status < 300
            and url == self.config['start_url'] and dom['repo_matches'] and not self.locked)
        if repository_opened:
            self._remember_phase('repository_opened')
        elif self.query_verified and self._repository_search(url) and not self.locked:
            self._remember_phase('search_results')
        goal = bool(repository_opened and all(t in dom['text'] for t in self.config['required_text']))
        self.rules = []
        if not self.locked and not goal:
            for c in dom['controls']:
                kind = None
                if (self.search_pending and not self.query_verified and self.search_finished
                        and self.search_status is not None and 200 <= self.search_status < 300
                        and self._is_search(url) and c['kind']=='repositories_filter'):
                    kind, label = 'browser_click', 'Select Repositories for the approved query'
                elif self.query_verified and self._repository_search(url) and c['kind'] == 'repo':
                    kind, label = 'browser_click', 'Open exact approved repository result'
                elif not self.search_pending and not self.query_verified and c['kind'] == 'input':
                    kind = 'browser_click' if c['value_matches'] else 'browser_fill'
                    label = 'Submit approved repository query' if c['value_matches'] else 'Fill approved repository query'
                elif not self.search_pending and not self.query_verified and c['kind'] == 'opener' and not any(x['kind']=='input' for x in dom['controls']):
                    kind, label = 'browser_click', 'Open GitHub search'
                if kind:
                    identity = self.document_id + '-' + str(c['identity'])
                    self.rules.append(dict(c, kind=kind, label=label,
                        rule_id='github-'+c['kind']+'-'+identity, control_id=identity))
            ambiguous = len(self.rules) > 1 or dom['controls_overflow']
            if ambiguous:
                self.rules = []
            if not self.rules and not ambiguous and self.wait_count < self.MAX_WAITS:
                self.rules = [{'kind':'browser_wait', 'rule_id':'github-wait-'+str(self.wait_count),
                               'control_id':'wait', 'label':'Wait for GitHub changes'}]
        snap = {'session_id':self.session_id, 'url':url, 'title':'GitHub repository',
            'text':dom['text'] if goal else '', 'controls':[], 'completed_rules':list(self.completed),
            'failures':deepcopy(self.failures), 'locked':self.locked, 'checks':{'goal_met':goal},
            'human_required':False, 'discovery':{'mode':'github_repo',
                'query_verified':self.query_verified, 'repository_verified':goal,
                'authenticated':self.authenticated, 'wait_count':self.wait_count,
                'search_status':self.search_status, 'repository_status':self.repo_status,
                'repository_search_status':self.repository_search_status,
                'search_proof':('historical' if self._historical_search else
                                'fresh' if self.query_verified else None),
                'repository_proof':'fresh' if repository_opened else None,
                'phase':self._safe_resume['phase'] if self._safe_resume else None,
                'resume':deepcopy(self.resume),
                'ambiguous_controls':ambiguous if not self.locked and not goal else False,
                'controls_overflow':dom['controls_overflow'],
                'candidate_targets':dom['candidate_targets'],
                'search_semantics':dom['search_semantics'],
                'repository_identity_matches':dom['repo_matches'],'main_present':dom['main_present'],
                'observation_retries':self.observation_retries,
                'last_observation_error':self.last_observation_error,
                'dom_sha256':_digest(dom)}}
        snap['state_id'] = _digest(snap)
        snap['actions'] = [{k:r[k] for k in ('kind','rule_id','control_id','label')} |
                           {'state_id':snap['state_id']} for r in self.rules]
        return snap

    def _result(self, state, code, **extra):
        observation = self.observe()
        if state == 'verified' and observation['locked']:
            state, code = 'unknown', 'browser_effect_unknown'
        return {'ok':state=='verified', 'state':state, 'code':code,
                'observation':observation, 'verification':{'independent_read':True}, **extra}

    def execute(self, action):
        before = self.observe()
        if self.manual or self.human_needed:
            return self._result('failed', 'browser_human_required')
        if self.locked:
            return self._result('unknown', 'browser_session_locked')
        if action not in before['actions']:
            return self._result('failed', 'browser_action_stale',
                                verification={'effect_dispatched':False})
        rule = next(r for r in self.rules if r['rule_id']==action['rule_id'])
        if action['kind'] == 'browser_wait':
            self.wait_count += 1
            try:
                self.page.wait_for_timeout(1500)
            except Exception:
                self._violation('browser_observation_unavailable')
                return self._result('unknown', 'browser_effect_unknown')
            return self._result('verified', 'browser_wait_observed')
        try:
            handle = self.page.locator(rule['selector']).element_handle(timeout=1000)
            fresh = (handle is not None and handle.evaluate('el=>el.isConnected') and
                self.registry is not None and
                self.registry.evaluate('(r,el)=>r.ids.get(el)', handle) == rule['identity'])
            fresh = fresh and not (self.manual or self.human_needed or self.locked)
        except Exception:
            self.registry, self.rules = None, []
            fresh = False
        if not fresh:
            return self._result('failed', 'browser_action_stale', verification={'effect_dispatched':False})
        try:
            if action['kind'] == 'browser_fill':
                handle.fill(self.config['query'], timeout=2000)
                if handle.input_value() != self.config['query']:
                    raise InteractionError('browser_github_fill_unverified')
                code = 'browser_fill_verified'
            elif rule['kind'] == 'browser_click' and rule.get('value_matches'):
                self.search_pending = True
                handle.press('Enter', timeout=2500, no_wait_after=True)
                self.page.wait_for_timeout(1200)
                self.registry = None
                code = 'browser_github_search_submitted'
            elif 'github-repositories_filter-' in rule['rule_id']:
                self.search_status = None
                self.search_finished = False
                self.repository_search_status = None
                self.repository_search_finished = False
                handle.click(timeout=2500, no_wait_after=True)
                self.page.wait_for_timeout(1200)
                self.registry = None
                code = 'browser_github_repository_filter_selected'
            elif 'github-repo-' in rule['rule_id']:
                self.allowed_target = self.repo_pending = self.repo_clicked = True
                handle.click(timeout=2500, no_wait_after=True)
                self.page.wait_for_timeout(1200)
                self.allowed_target = False
                self.registry = None
                code = 'browser_github_repository_opened'
            else:
                handle.click(timeout=2500)
                # GitHub hydration can lag the rendered search opener.
                self.page.wait_for_timeout(1500)
                self.wait_count += 1
                if self.wait_count >= self.MAX_WAITS:
                    self._violation('browser_github_controls_unavailable')
                self.observe()
                code = ('browser_github_search_opened' if any(
                    r['rule_id'].startswith('github-input-') for r in self.rules) else
                    'browser_github_search_opener_dispatched')
            self.completed.append(rule['rule_id'])
            return self._result('verified', code)
        except Exception:
            self._violation('browser_effect_unknown')
            return self._result('unknown', 'browser_effect_unknown')

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.pw:
                self.pw.stop()
