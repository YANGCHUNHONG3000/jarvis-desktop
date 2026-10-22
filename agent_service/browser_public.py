"""Fixed public-site GET search and approved document capture in owned Edge."""
from copy import deepcopy
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit
import uuid

from .browser_github import GithubEdgeSession
from .browser_interaction import InteractionError, _digest, _text, _validate_resume
from .public_site_profiles import PROFILES, public_search_url

_PROFILES = {site: (p['start'], p['search']) for site, p in PROFILES.items()}


def validate_public_target(config, url):
    """Canonical document capability, shared by discovery and history validation."""
    _text(url, 1000)
    start, search = _PROFILES[config['site']]
    p, base = urlsplit(url), urlsplit(start)
    path = p.path
    if (p.scheme != 'https' or p.netloc != base.netloc or p.query or p.fragment
            or '%' in path or '\\' in path or '//' in path or any(x in {'.', '..'} for x in path.split('/'))
            or path == search or url == start or urlunsplit(p) != url
            or not re.fullmatch(PROFILES[config['site']]['target'], path)):
        raise InteractionError('browser_public_target_rejected')
    return url


def validate_public_config(config):
    required = {'mode', 'site', 'start_url', 'query', 'required_text'}
    if (type(config) is not dict or not required <= config.keys()
            or config.keys() - required - {'headless', 'target_url'} or config['mode'] != 'public_search'
            or type(config['site']) is not str or config['site'] not in _PROFILES):
        raise InteractionError('browser_public_config_invalid')
    value = deepcopy(config)
    start, _ = _PROFILES[value['site']]
    if value['start_url'] != start:
        raise InteractionError('browser_public_start_rejected')
    _text(value['query'], 200)
    value.setdefault('target_url', '')
    if value['target_url'] != '':
        validate_public_target(value, value['target_url'])
    elif type(value['target_url']) is not str:
        raise InteractionError('browser_public_target_rejected')
    terms = value['required_text']
    if type(terms) is not list or not 1 <= len(terms) <= 3:
        raise InteractionError('browser_goal_invalid')
    for term in terms:
        _text(term, 200)
    value.setdefault('headless', False)
    if type(value['headless']) is not bool:
        raise InteractionError('browser_public_config_invalid')
    return value


_DOM = r"""({registry,query,target,site,profile,search,read,discover_form,discover_target})=>{
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const identity=e=>{if(!registry.ids.has(e))registry.ids.set(e,++registry.next);return registry.ids.get(e)};
 const path=e=>{const a=[];while(e&&e.nodeType===1){const s=e.parentElement?Array.from(e.parentElement.children).filter(x=>x.tagName===e.tagName):[e];a.unshift(e.tagName.toLowerCase()+':nth-of-type('+(s.indexOf(e)+1)+')');e=e.parentElement}return a.join('>')};
 const controls=[],counts={input:0,submit:0,target:0};let invalid=false,forms=0;
 if(discover_form)for(const f of document.querySelectorAll('form')){
  const inputs=Array.from(f.elements).filter(e=>e.tagName==='INPUT'&&e.name==='q'&&['text','search'].includes(e.type)&&visible(e)&&!e.disabled&&!e.readOnly);
  if(!inputs.length)continue;
  const buttons=Array.from(f.elements).filter(e=>((e.tagName==='INPUT'&&e.type==='submit')||(e.tagName==='BUTTON'&&e.type==='submit'))&&visible(e)&&!e.disabled);
  const enter=profile.submit==='enter',all=Array.from(f.elements);
  forms++;counts.input+=inputs.length;counts.submit+=enter?1:buttons.length;
  const hidden=all.filter(e=>e.tagName==='INPUT'&&e.type==='hidden'&&e.name==='category');
  const hiddenOK=site!=='django_docs'||hidden.length===1&&hidden[0].value===''&&!hidden[0].disabled;
  const others=all.filter(e=>e.name&&e!==inputs[0]&&!(site==='python_org'&&buttons.includes(e)&&e.name==='submit'&&e.value==='')&&!(site==='django_docs'&&hidden.includes(e)));
  const extraNew=(site==='pandas_docs'||site==='django_docs')&&all.some(e=>e!==inputs[0]&&!buttons.includes(e)&&!hidden.includes(e));
  const action=new URL(f.action);if(enter&&action.hash===''&&f.getAttribute('action')==='#')action.hash='';
  const actionOK=f.action===search||(site==='pandas_docs'&&f.getAttribute('action')==='#'&&action.origin+action.pathname===search&&!action.search);
  const badOverride=e=>e.hasAttribute('formaction')||e.hasAttribute('formmethod')||e.hasAttribute('formtarget')||e.hasAttribute('formenctype')||e.hasAttribute('formnovalidate');
  if(!actionOK||f.method.toLowerCase()!=='get'||(f.target&&f.target!=='_self')||inputs.length!==1||(enter?all.some(e=>(e.tagName==='BUTTON'&&e.type==='submit')||(e.tagName==='INPUT'&&['submit','image'].includes(e.type))):buttons.length!==1)||others.length||extraNew||!hiddenOK||inputs.some(badOverride)||buttons.some(e=>badOverride(e)||e.formTarget||e.name!==profile.button_name||(site==='python_org'&&e.value!==''))){invalid=true;continue}
  // Each complete form has the same approved GET effect. Pick one whole form
  // in document order; never combine one form's input with another's button.
  if(!controls.some(c=>c.kind==='input')){
   const e=inputs[0],button=enter?e:buttons[0];
   controls.push({kind:'input',identity:identity(e),selector:path(e),value_matches:e.value===query});
   controls.push({kind:'submit',identity:identity(button),selector:path(button),value_matches:e.value===query});
  }
 }
 const safe=u=>u.protocol==='https:'&&u.host===location.host&&!u.username&&!u.password&&!u.search&&!u.hash&&!/[\\%]/.test(u.pathname)&&!u.pathname.includes('//')&&!u.pathname.split('/').some(x=>x==='.'||x==='..')&&
  u.pathname!==new URL(search).pathname&&new RegExp('^(?:'+profile.target+')$').test(u.pathname)&&u.href!==profile.start;
 const clean=(s,n)=>Array.from((s||'').replace(/[\u0000-\u001f\u007f]/g,' ').replace(/\s+/g,' ').trim()).slice(0,n).join(''),seen=new Set();
 const region=discover_target?document.querySelector(profile.region):null;
 if(region)for(const e of region.querySelectorAll(profile.links)){
  try{const u=new URL(e.href);u.hash='';if(!safe(u)||(target&&u.href!==target)||!visible(e)||e.hasAttribute('download')||(e.target&&e.target!=='_self'))continue;
   counts.target++;if(seen.has(u.href))continue;seen.add(u.href);
   if(controls.filter(x=>x.kind==='target').length>=8)continue;
   let snippet=(e.closest('li')||e.parentElement||e).innerText;
   if(site==='django_docs'){
    const dt=e.closest('dt'),dd=dt&&dt.parentElement===region?dt.nextElementSibling:null;
    snippet=e.innerText+(dd&&dd.tagName==='DD'&&visible(dd)?' '+dd.innerText:'');
   }
   controls.push({kind:'target',identity:identity(e),selector:path(e),value_matches:false,target_url:u.href,
    title:clean(e.innerText,200),snippet:clean(snippet,400)});
  }catch{}
 }
 const main=document.querySelector(profile.main);
 return {controls:controls.slice(0,16),controls_overflow:controls.length>16||forms>16,
  ambiguous_controls:invalid, candidate_targets:counts,
  title:(document.title||'').slice(0,300),main_present:!!main,text:read&&main?(main.innerText||'').replace(/\s+/g,' ').trim().slice(0,24000):''};
}"""


class PublicSearchEdgeSession(GithubEdgeSession):
    """Owned public GET browser; task reasoning stays above this adapter."""
    MAX_RESULT_SETTLE_READS = 20
    RESULT_SETTLE_MS = 150
    _dom_script = _DOM

    def __init__(self, config, resume=None):
        self.config = validate_public_config(config)
        self.resume = _validate_resume(self.config, resume)
        self._safe_resume = deepcopy(self.resume)
        self.target_url = (self.resume['target_url'] if self.resume and self.resume['phase']=='target_opened'
                           else self.config['target_url'])
        self._historical_search = bool(self.resume and self.resume['phase']=='target_opened')
        self.session_id = uuid.uuid4().hex
        self.manual = self.human_needed = self.locked = False
        self.blocked = self.wait_count = self.navigation_generation = self.observation_retries = 0
        self.blocked_requests = []
        self.blocked_requests_overflow = False
        self.last_observation_error = None
        self.completed, self.failures, self.rules = [], [], []
        self.registry = self.document_id = None
        self.query_verified = self.content_verified = False
        self.search_pending = self.content_pending = self.content_clicked = False
        self.search_status = self.content_status = None
        self.search_finished = self.content_finished = False
        self.search_request = self.content_request = None
        if self.resume:
            self.search_pending = True
            if self._historical_search:
                self.query_verified = self.content_pending = self.content_clicked = True
        self.bootstrap = True
        self.browser = self.pw = None
        self.host = urlsplit(self.config['start_url']).netloc
        self.asset_requests = 0
        pins = []
        for host in self._dns_hosts():
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
            if not ips or any(not a.is_global for a in ips):
                raise InteractionError('browser_public_dns_rejected')
            address = str(ips[0])
            pins.append(f'MAP {host} [{address}]' if ':' in address else f'MAP {host} {address}')
        pin = ','.join(pins)
        from playwright.sync_api import sync_playwright
        from .research import _public_search_proxy
        proxy = _public_search_proxy()
        self.pw = sync_playwright().start()
        try:
            args = ['--disable-background-networking', '--host-resolver-rules='+pin]
            network = {'proxy': {'server': proxy}} if proxy else {}
            if not proxy:
                args.append('--no-proxy-server')
            self.browser = self.pw.chromium.launch(channel='msedge', headless=self.config['headless'],
                timeout=15000, chromium_sandbox=True, args=args, **network)
            self.context = self.browser.new_context(java_script_enabled=True, accept_downloads=False, service_workers='block')
            self.context.route('**/*', self._route)
            self.context.route_web_socket('**/*', self._socket)
            self.context.on('page', self._popup)
            self.page = self.context.new_page()
            self._bind_page(self.page)
            self.page.goto(self.resume['url'] if self.resume else self.config['start_url'], wait_until='domcontentloaded', timeout=12000)
            self.bootstrap = False
        except BaseException:
            self.close()
            raise

    def _canonical(self, url):
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))

    def _dns_hosts(self):
        return [self.host]

    def _search_ready(self, url):
        return bool(self.search_pending and self.search_finished and self.search_status==200 and self._is_search(url))

    def _search_url(self):
        return public_search_url(self.config)

    def _remember_phase(self, phase):
        self._safe_resume = {'version':1, 'mode':'public_search', 'phase':phase,
            'url':self.target_url if phase=='target_opened' else self._search_url(),
            'config_sha256':_digest(self.config),
            'target_url':self.target_url if phase=='target_opened' else ''}

    def _is_search(self, url):
        try:
            p = urlsplit(url); pairs = parse_qsl(p.query, keep_blank_values=True)
            expected = {'q':self.config['query'], **PROFILES[self.config['site']]['fixed']}
            return (p.scheme=='https' and p.netloc==self.host and not p.fragment
                and p.path==_PROFILES[self.config['site']][1]
                and len(pairs)==len(expected) and dict(pairs)==expected)
        except ValueError:
            return False

    def _request_allowed(self, request):
        p = urlsplit(request.url)
        if request.method!='GET' or p.scheme!='https' or p.netloc!=self.host or p.fragment:
            return False
        if request.is_navigation_request():
            return request.frame==self.page.main_frame and (
                self.bootstrap and request.url==self.config['start_url'] or
                self.search_pending and not self.content_pending and self._is_search(request.url)
                and (self.search_request is None or request == self.search_request) or
                self.content_pending and request.url==self.target_url
                and (self.content_request is None or request == self.content_request))
        pairs = parse_qsl(p.query, keep_blank_values=True)
        profile=PROFILES[self.config['site']]
        if self.config['site'] in {'pandas_docs','django_docs'} and any(part in {'.','..'} for part in p.path.split('/')):
            return False
        version = not pairs or profile['asset_query'] in {'v','pandas'} and len(pairs)==1 and (
            pairs[0][0]=='v' and bool(re.fullmatch('[a-f0-9]{8}',pairs[0][1])) or
            profile['asset_query']=='pandas' and pairs[0][0]=='digest' and bool(re.fullmatch('[a-f0-9]{20}',pairs[0][1])))
        support = not p.query and (p.path,request.resource_type) in profile['support']
        static = bool(profile['static'] and re.fullmatch(profile['static'],p.path)) and version and request.resource_type in {'script','stylesheet','image','font','other'}
        return bool((static or support) and self.asset_requests < 128)

    def _route(self, route):
        request = route.request
        try:
            allowed = self._request_allowed(request)
        except (ValueError, AttributeError):
            allowed = False
        if allowed:
            if request.is_navigation_request():
                if self.search_pending and self._is_search(request.url):
                    if request != self.search_request:
                        self.search_status, self.search_finished = None, False
                        self.query_verified = self.content_verified = False
                    self.search_request = request
                elif self.content_pending and request.url==self.target_url:
                    if request != self.content_request:
                        self.content_status, self.content_finished = None, False
                        self.content_verified = False
                    self.content_request = request
            else:
                self.asset_requests += 1
            route.continue_()
        else:
            self.blocked += 1
            try:
                parsed=urlsplit(request.url)
                host,path=parsed.hostname or '',parsed.path
                names=sorted({unquote_plus(part.partition('=')[0])[:64]
                    for part in parsed.query.split('&') if part})[:8]
            except (ValueError,TypeError):
                parsed=None
                host,path,names='','',[]
            try:
                content_type=request.header_value('content-type')
                content_type=content_type.split(';',1)[0].strip().lower()[:80] if content_type else None
            except (AttributeError,TypeError,ValueError):
                content_type=None
            # The observed python.org CSP report is background telemetry. Keep
            # it aborted; its refusal cannot invalidate the approved GET read.
            blocked_without_effect = (self.config['site']=='python_org'
                and request.method=='POST' and not request.is_navigation_request()
                and request.resource_type=='other'
                and content_type=='application/csp-report' and parsed is not None
                and parsed.scheme=='https' and parsed.netloc=='o20004.ingest.us.sentry.io'
                and path=='/api/39935/security/' and '#' not in request.url
                and parsed.query.startswith('sentry_key=') and '&' not in parsed.query
                and 0<len(parsed.query.removeprefix('sentry_key='))<=200)
            if len(self.blocked_requests)<16:
                diagnostic={'method':request.method[:16],
                    'host':host[:253],'path':path[:300],
                    'resource':request.resource_type[:32],
                    'navigation':bool(request.is_navigation_request()),
                    'query_present':'?' in request.url.split('#',1)[0],
                    'query_parameter_names':names,'fragment_present':'#' in request.url,
                    'content_type':content_type}
                if blocked_without_effect:
                    diagnostic['blocked_without_effect']=True
                self.blocked_requests.append(diagnostic)
            else:
                self.blocked_requests_overflow = True
            if not blocked_without_effect and (request.is_navigation_request() or request.method!='GET'):
                self._violation('browser_public_navigation_blocked' if request.method=='GET' else 'browser_public_method_blocked')
            route.abort()

    def _response(self, response):
        if response.request == self.search_request:
            self.search_status = response.status
        if response.request == self.content_request:
            self.content_status = response.status
        if response.status in {401,403,407} and response.request.is_navigation_request():
            self._violation('browser_public_auth_or_challenge')

    def _finished(self, request):
        if request == self.search_request:
            self.search_finished = True
        if request == self.content_request:
            self.content_finished = True

    def _quiet_observation(self):
        snap = {'session_id':self.session_id,'url':self.config['start_url'],'title':'Public website',
            'text':'','controls':[],'completed_rules':list(self.completed),'failures':deepcopy(self.failures),
            'locked':self.locked,'human_required':False,'checks':{'goal_met':False},
            'discovery':{'mode':'public_search','site':self.config['site'],'phase':'halted',
                'query_verified':False,'content_verified':False,'search_status':self.search_status,
                'content_status':self.content_status,'target_url':self.target_url,'main_present':False,
                'resume':deepcopy(self.resume),'search_proof':'historical' if self._historical_search else None,
                'content_proof':None,
                'blocked_requests':deepcopy(self.blocked_requests),
                'blocked_requests_overflow':self.blocked_requests_overflow,
                'ambiguous_controls':False,'controls_overflow':False,'observation_retries':self.observation_retries,
                'last_observation_error':self.last_observation_error}}
        snap['state_id']=_digest(snap);snap['actions']=[]
        return snap

    def _observe_once(self):
        generation,url=self.navigation_generation,self.page.url
        canonical=self._canonical(url)
        if canonical not in {self.config['start_url'],self.target_url} and not self._is_search(url):
            self._violation('browser_public_navigation_blocked');return self._quiet_observation()
        if self.page.evaluate("() => !!document.querySelector('input[type=password],iframe[src*=captcha],[id*=captcha],#challenge-form')"):
            self._violation('browser_public_auth_or_challenge');return self._quiet_observation()
        if self.registry is None:
            self.registry=self.page.evaluate_handle('({ids:new WeakMap(),next:0})');self.document_id=uuid.uuid4().hex
        dom_args={'registry':self.registry,'query':self.config['query'],
            'target':self.target_url,'site':self.config['site'],
            'profile':PROFILES[self.config['site']],
            'search':'https://'+self.host+_PROFILES[self.config['site']][1],
            'discover_form':not self.search_pending,
            'discover_target':not self.content_pending and bool(self.query_verified or self._search_ready(url)),
            'read':self.content_pending and canonical==self.target_url}
        dom_args=self._dom_arguments(dom_args)
        dom=self.page.evaluate(self._dom_script,dom_args)
        if self.config['site'] in {'python_docs','pandas_docs'} and dom_args['discover_target']:
            dom=self._settle_result_dom(dom,dom_args)
        if generation!=self.navigation_generation or url!=self.page.url:
            raise InteractionError('browser_github_page_changed')
        if self.locked:
            return self._quiet_observation()
        if self._search_ready(url):
            self.query_verified=True
        if self.query_verified and self.content_clicked and canonical==self.target_url:
            self._remember_phase('target_opened')
        elif self.query_verified and self._is_search(url):
            self._remember_phase('search_results')
        opened=bool(self.query_verified and self.content_clicked and self.content_finished and self.content_status==200 and canonical==self.target_url and dom['main_present'])
        goal=bool(opened and dom['text'] and all(t in dom['text'] for t in self.config['required_text']))
        self.content_verified=goal
        ambiguous=dom['ambiguous_controls'] or dom['controls_overflow']
        self._last_dom_args=dom_args
        self.rules=[]
        if not goal and not ambiguous:
            wanted='target' if self.query_verified and not self.content_pending else 'input' if not self.search_pending else None
            if wanted=='input' and any(c['kind']=='input' and c['value_matches'] for c in dom['controls']):wanted='submit'
            for c in dom['controls']:
                if c['kind']!=wanted:continue
                if wanted=='target':
                    try:validate_public_target(self.config,c['target_url'])
                    except (InteractionError,ValueError):continue
                ident=self.document_id+'-'+str(c['identity'])
                self.rules.append(dict(c,kind='browser_fill' if wanted=='input' else 'browser_click',
                    target_kind=wanted,rule_id='public-'+wanted+'-'+ident,control_id=ident,
                    label={'input':'Fill approved query','submit':'Submit approved GET search','target':'Open exact approved result'}[wanted]))
            if not self.rules and self.wait_count<self.MAX_WAITS:
                self.rules=[{'kind':'browser_wait','rule_id':'public-wait-'+str(self.wait_count),'control_id':'wait','label':'Wait for public search'}]
        snap={'session_id':self.session_id,'url':canonical,'title':dom['title'],
            'text':dom['text'] if goal else '','controls':[],'completed_rules':list(self.completed),
            'failures':deepcopy(self.failures),'locked':self.locked,'human_required':False,
            'checks':{'goal_met':goal},'discovery':{'mode':'public_search','site':self.config['site'],
            'phase':'verified' if goal else 'target_pending' if self.content_pending else 'search_results' if self.query_verified else 'search_pending' if self.search_pending else 'search_form',
            'query_verified':self.query_verified,'content_verified':goal,'search_status':self.search_status,
            'content_status':self.content_status,'search_finished':self.search_finished,'content_finished':self.content_finished,
            'target_url':self.target_url,'main_present':dom['main_present'],
            'resume':deepcopy(self.resume),
            'search_proof':'historical' if self._historical_search else 'fresh' if self.query_verified else None,
            'content_proof':'fresh' if opened else None,
            'search_submitted':self.search_pending,'target_clicked':self.content_clicked,
            'ambiguous_controls':ambiguous,'controls_overflow':dom['controls_overflow'],
            'candidate_targets':dom['candidate_targets'],'observation_retries':self.observation_retries,
            'last_observation_error':self.last_observation_error,'dom_sha256':_digest(dom)}}
        snap['state_id']=_digest(snap)
        snap['actions']=[{k:r[k] for k in ('kind','rule_id','control_id','label','target_url','title','snippet') if k in r}|{'state_id':snap['state_id']} for r in self.rules]
        return snap

    def _dom_arguments(self, args):
        return args

    def _settle_result_dom(self, dom, args):
        """Wait briefly for Sphinx's incremental results, without dispatching."""
        previous,stable=_digest(dom),0
        for _ in range(self.MAX_RESULT_SETTLE_READS):
            self.page.wait_for_timeout(self.RESULT_SETTLE_MS)
            if self.locked:
                return dom
            current=self.page.evaluate(self._dom_script,args)
            current_digest=_digest(current)
            stable=stable+1 if current_digest==previous else 0
            dom,previous=current,current_digest
            if stable>=2:
                return dom
        self._violation('browser_public_results_unsettled')
        return dom

    def execute(self, action):
        before=self.observe()
        if self.locked:return self._result('unknown','browser_session_locked')
        if action not in before['actions']:
            return self._result('failed','browser_action_stale',verification={'effect_dispatched':False})
        rule=next(r for r in self.rules if r['rule_id']==action['rule_id'])
        if action['kind']=='browser_wait':
            self.wait_count+=1
            try:self.page.wait_for_timeout(500)
            except Exception:
                self._violation('browser_observation_unavailable');return self._result('unknown','browser_effect_unknown')
            return self._result('verified','browser_wait_observed')
        try:
            handle=self.page.locator(rule['selector']).element_handle(timeout=1000)
            fresh=handle is not None and handle.evaluate('el=>el.isConnected') and self.registry is not None and self.registry.evaluate('(r,el)=>r.ids.get(el)',handle)==rule['identity']
            if fresh:
                current_dom=self.page.evaluate(self._dom_script,self._last_dom_args)
                fresh=_digest(current_dom)==before['discovery']['dom_sha256']
            if fresh and rule['target_kind']=='target':
                # A retained element identity does not authorize a replaced href.
                current=handle.evaluate("el=>({url:el.href,visible:!!el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden',safe:!el.hasAttribute('download')&&(!el.target||el.target==='_self')})")
                fresh=bool(current['visible'] and current['safe'] and
                    validate_public_target(self.config,self._canonical(current['url']))==rule['target_url'])
        except Exception:
            self.registry,self.rules=None,[];fresh=False
        if not fresh:return self._result('failed','browser_action_stale',verification={'effect_dispatched':False})
        try:
            if action['kind']=='browser_fill':
                handle.fill(self.config['query'],timeout=2000)
                if handle.input_value()!=self.config['query']:raise InteractionError('browser_public_fill_unverified')
                code='browser_fill_verified'
            elif rule['target_kind']=='submit':
                self.search_pending=True
                if PROFILES[self.config['site']]['submit']=='enter':
                    handle.press('Enter',timeout=2500,no_wait_after=True)
                else:
                    handle.click(timeout=2500,no_wait_after=True)
                code='browser_public_search_submitted'
            else:
                self.target_url=rule['target_url']
                self.content_pending=self.content_clicked=True
                handle.click(timeout=2500,no_wait_after=True)
                code='browser_public_target_opened'
            self.completed.append(rule['rule_id'])
        except Exception:
            self._violation('browser_effect_unknown');return self._result('unknown','browser_effect_unknown')
        # Observation failures have their own finite recovery and never repeat dispatch.
        return self._result('verified',code)
