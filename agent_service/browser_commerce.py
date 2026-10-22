"""IKEA public procurement search: one fixed read-only POST, never commerce writes."""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import unquote_plus, urlsplit

from .browser_interaction import InteractionError, _digest
from .browser_public import PublicSearchEdgeSession, validate_public_target, validate_public_config
from .public_site_profiles import PROFILES, public_search_url

API_URL = 'https://sik.search.blue.cdtapps.com/us/en/search?c=sr&v=20260727'
API_MIME = 'text/plain;charset=UTF-8'
_BODY = {'searchParameters': {'input': '', 'type': 'QUERY'}, 'allowAutocorrect': True, 'isUserLoggedIn': False, 'isB2B': False, 'listingABTest': True, 'components': [{'component': 'PRIMARY_AREA', 'columns': 4, 'types': {'main': 'PRODUCT', 'breakouts': ['PLANNER', 'CATEGORY', 'CONTENT', 'MATTRESS_WARRANTY', 'FINANCIAL_SERVICES']}, 'filterConfig': {'subcategories-style': 'tree-navigation', 'max-num-filters': 4}, 'window': {'size': 24, 'offset': 0}, 'allVariants': False, 'forceFilterCalculation': True}, {'component': 'CONTENT_AREA', 'types': {'main': 'CONTENT', 'breakouts': []}, 'window': {'size': 12, 'offset': 0}}, {'component': 'RELATED_SEARCHES'}, {'component': 'QUESTIONS_AND_ANSWERS'}, {'component': 'STORES'}, {'component': 'CATEGORIES'}, {'component': 'SIMILAR_PRODUCTS'}, {'component': 'SEARCH_SUMMARY'}, {'component': 'PAGE_MESSAGES'}, {'component': 'RELATED_CATEGORIES'}, {'component': 'PRODUCT_GROUP'}]}


def commerce_search_body(query):
    value = deepcopy(_BODY)
    value['searchParameters']['input'] = query
    return value


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def _parse_body(text):
    return json.loads(text, object_pairs_hook=_json_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON constant')))


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _hex(value):
    return type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None


def verify_commerce_search(config, browser):
    """Independently validate a saved fresh proof against its observation and scope.

    Historical opened-product resumes deliberately carry no fresh commerce proof;
    their independent ancestral cursor must instead be checked by the caller.
    """
    def check(value):
        if not value:
            raise InteractionError('browser_commerce_proof_invalid')
    try:
        config = validate_public_config(config)
        check(config['site'] == 'ikea_us' and type(browser) is dict)
        d = browser['discovery']; p = d['commerce_search']
        check(browser.get('locked') is False and browser.get('human_required') is False)
        check(d['mode'] == 'public_search' and d['site'] == 'ikea_us' and type(d['target_clicked']) is bool)
        check(type(browser['actions']) is list)
        check(type(p) is dict and set(p) == {'version','session_id','config_sha256','query',
            'search_page_url','search_document_id','shell','request','response','dom_sha256',
            'candidates','selected_target','proof_sha256'})
        check(type(p['version']) is int and p['version'] == 1)
        check(p['session_id'] == browser['session_id'] and type(p['session_id']) is str and bool(p['session_id']))
        check(p['config_sha256'] == _digest(config) and p['query'] == config['query'])
        check(p['search_page_url'] == public_search_url(config))
        check(type(p['search_document_id']) is str and re.fullmatch('[a-f0-9]{32}',p['search_document_id']))
        check(d['query_verified'] is True and d['search_proof'] == 'fresh')
        check(type(d['search_status']) is int and d['search_status'] == 200 and d['search_finished'] is True)
        check(type(p['shell']) is dict and set(p['shell']) == {'status','finished'})
        check(type(p['shell']['status']) is int and p['shell']['status'] == 200 and p['shell']['finished'] is True)
        r = p['request']
        check(set(r) == {'url','method','content_type','body','body_sha256'})
        check(r['url'] == API_URL and r['method'] == 'POST' and r['content_type'] == API_MIME)
        check(_digest(r['body']) == _digest(commerce_search_body(config['query'])))
        check(r['body_sha256'] == _digest(r['body']))
        response = p['response']
        check(set(response) == {'status','finished','body_sha256','products','products_sha256'})
        check(type(response['status']) is int and response['status'] == 200 and response['finished'] is True)
        check(_hex(response['body_sha256']) and response['products_sha256'] == _digest(response['products']))
        products = response['products']; check(type(products) is list and 1 <= len(products) <= 24)
        urls = set()
        for item in products:
            check(type(item) is dict and set(item) == {'url','number'})
            check(validate_public_target(config,item['url']) == item['url'])
            check(type(item['number']) is str and re.fullmatch('[0-9]{8}',item['number']))
            check(item['url'].endswith('-'+item['number']+'/') and item['url'] not in urls)
            urls.add(item['url'])
        candidates = p['candidates']; check(type(candidates) is list and 1 <= len(candidates) <= 8)
        ids = set(); selected_urls = set()
        for candidate in candidates:
            check(type(candidate) is dict and set(candidate) == {'rule_id','control_id','target_url','title','snippet'})
            check(candidate['target_url'] in urls and candidate['target_url'] not in selected_urls)
            ident = candidate['control_id']; check(type(ident) is str and ident.startswith(p['search_document_id']+'-'))
            check(ident[len(p['search_document_id'])+1:].isdigit() and candidate['rule_id'] == 'public-target-'+ident and ident not in ids)
            check(type(candidate['title']) is str and 0 < len(candidate['title']) <= 200)
            check(type(candidate['snippet']) is str and len(candidate['snippet']) <= 400)
            ids.add(ident); selected_urls.add(candidate['target_url'])
        check(_hex(p['dom_sha256']))
        selected = p['selected_target']; current_url = browser['url']
        check(type(selected) is str and (not selected or selected in selected_urls))
        check(not selected or d['target_url'] == selected)
        if current_url == p['search_page_url']:
            if not d['target_clicked']:
                check(not selected and p['dom_sha256'] == d['dom_sha256'])
                actual = [{k:a[k] for k in ('rule_id','control_id','target_url','title','snippet')}
                    for a in browser['actions'] if a.get('kind') == 'browser_click' and 'target_url' in a]
                check(actual == candidates)
            else:
                check(bool(selected))
        else:
            check(bool(selected) and current_url == selected and d['target_clicked'] is True)
        check(p['proof_sha256'] == _digest({k:v for k,v in p.items() if k != 'proof_sha256'}))
        return deepcopy(p)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InteractionError('browser_commerce_proof_invalid') from None


_COMMERCE_DOM = r"""({registry,query,target,profile,search,read,discover_form,discover_target,products})=>{
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const identity=e=>{if(!registry.ids.has(e))registry.ids.set(e,++registry.next);return registry.ids.get(e)};
 const path=e=>{const a=[];while(e&&e.nodeType===1){const s=e.parentElement?Array.from(e.parentElement.children).filter(x=>x.tagName===e.tagName):[e];a.unshift(e.tagName.toLowerCase()+':nth-of-type('+(s.indexOf(e)+1)+')');e=e.parentElement}return a.join('>')};
 const controls=[],counts={input:0,submit:0,target:0};let invalid=false,forms=0;
 if(discover_form)for(const f of document.querySelectorAll('form')){
  const inputs=Array.from(f.elements).filter(e=>e.tagName==='INPUT'&&e.name==='q'&&e.type==='search'&&visible(e)&&!e.disabled&&!e.readOnly);
  if(!inputs.length)continue;
  const all=Array.from(f.elements),buttons=all.filter(e=>e.tagName==='BUTTON'&&e.type==='submit');
  forms++;counts.input+=inputs.length;counts.submit+=buttons.filter(e=>visible(e)&&!e.disabled).length;
  const override=e=>['formaction','formmethod','formtarget','formenctype','formnovalidate'].some(k=>e.hasAttribute(k));
  if(f.action!==search||f.method.toLowerCase()!=='get'||(f.target&&f.target!=='_self')||inputs.length!==1||buttons.length!==1||
     all.some(e=>e.name&&e!==inputs[0])||inputs.some(override)||buttons.some(override)) {invalid=true;continue}
  if(!controls.some(c=>c.kind==='input')){
   const e=inputs[0],b=buttons[0];
   controls.push({kind:'input',identity:identity(e),selector:path(e),value_matches:e.value===query});
   if(visible(b)&&!b.disabled)controls.push({kind:'submit',identity:identity(b),selector:path(b),value_matches:e.value===query});
  }
 }
 const clean=(s,n)=>Array.from((s||'').replace(/[\u0000-\u001f\u007f]/g,' ').replace(/\s+/g,' ').trim()).slice(0,n).join('');
 const seen=new Set(),allowed=new Map((products||[]).map(p=>[p.url,p.number]));
 if(discover_target)for(const e of document.querySelectorAll(profile.region+' '+profile.links)){
  try{const u=new URL(e.href),card=e.closest('[data-testid="plp-product-card"][data-product-number]');
   if(!card||!card.closest('#product-list')||!allowed.has(u.href)||allowed.get(u.href)!==card.getAttribute('data-product-number')||
      (target&&u.href!==target)||!visible(e)||e.hasAttribute('download')||(e.target&&e.target!=='_self'))continue;
   counts.target++;if(seen.has(u.href))continue;seen.add(u.href);if(controls.filter(c=>c.kind==='target').length>=8)continue;
   const title=clean(e.getAttribute('aria-label')||e.innerText,200);if(!title)continue;
   controls.push({kind:'target',identity:identity(e),selector:path(e),value_matches:false,target_url:u.href,title,snippet:clean(card.innerText,400)});
  }catch{}
 }
 const main=document.querySelector('main'),detail=main&&main.querySelector('h1')&&main.querySelector('.pipcom-price-module');
 return {controls:controls.slice(0,16),controls_overflow:controls.length>16||forms>16,ambiguous_controls:invalid,candidate_targets:counts,
  title:(document.title||'').slice(0,300),main_present:!!detail,text:read&&detail?(main.innerText||'').replace(/\s+/g,' ').trim().slice(0,24000):''};
}"""


class CommerceSearchEdgeSession(PublicSearchEdgeSession):
    """One request per approved search document; no account/cart permissions."""
    _dom_script = _COMMERCE_DOM
    MAX_API_BYTES = 4 * 1024 * 1024

    def __init__(self, config, resume=None):
        self.api_request = self.api_response = None
        self.api_status = None
        self.api_finished = False
        self.api_products = []
        self.api_request_proof = self.api_response_sha = None
        self.commerce_proof = None
        self.search_document_id = None
        self.api_document_url = None
        self.api_seen = False
        self.api_error = None
        self.api_unsupported_products = 0
        if type(config) is not dict or config.get('site') != 'ikea_us':
            raise InteractionError('browser_public_config_invalid')
        super().__init__(config,resume)

    def _dns_hosts(self):
        return [self.host,urlsplit(API_URL).hostname]

    def _bind_page(self, page):
        super()._bind_page(page)
        # Chromium's empty permission override denies every permission for this
        # origin in the owned ephemeral context. It never grants a capability.
        self.context.grant_permissions([],origin='https://www.ikea.com')

    def _search_ready(self, url):
        return bool(super()._search_ready(url) and self.api_finished and self.api_status == 200 and self.api_products)

    def _is_api(self, request):
        p = urlsplit(request.url)
        return p.path == '/us/en/search' or (p.netloc == urlsplit(API_URL).netloc and p.path.startswith('/us/en/search/'))

    def _approved_api(self, request):
        try:
            body = request.post_data
            return (request.url == API_URL and request.method == 'POST' and not request.is_navigation_request()
                and request.resource_type == 'fetch' and request.frame == self.page.main_frame
                and request.header_value('content-type') == API_MIME and type(body) is str
                and len(body) <= 5000 and _digest(_parse_body(body)) == _digest(commerce_search_body(self.config['query']))
                and self.search_pending and not self.content_pending and self._is_search(self.page.url)
                and self.search_request is not None and not self.api_seen)
        except (ValueError,TypeError,AttributeError):
            return False

    def _request_allowed(self, request):
        if self._is_api(request):
            return self._approved_api(request)
        p = urlsplit(request.url)
        if request.method != 'GET' or p.scheme != 'https' or p.netloc != self.host or p.fragment:
            return False
        if request.is_navigation_request():
            return super()._request_allowed(request)
        if (p.query or '%' in p.path or '\\' in p.path or '//' in p.path
                or any(x in {'.','..'} for x in p.path.split('/')) or self.asset_requests >= 128):
            return False
        prefixes = ('/global/assets/dwf/nav/','/global/assets/dwf/in-clude/','/global/assets/fonts/','/us/en/search/v2/',
            '/us/en/product-lists/v3/','/us/en/products/')
        if request.resource_type == 'fetch':
            return p.path in {'/us/en/products/price-package-scripts.html','/us/en/products/price-package-styles.html'}
        return (request.resource_type in {'script','stylesheet','font'} and p.path.startswith(prefixes)
            and re.fullmatch(r'[A-Za-z0-9_./-]+',p.path) is not None
            and re.search(r'(?i)(?:account|checkout|cart|token|favorites|gift-registry)',p.path) is None)

    def _route(self, route):
        req = route.request
        if self._is_api(req):
            if not self._approved_api(req):
                self._record_blocked(req)
                self._violation('browser_commerce_request_blocked');route.abort();return
            self.api_seen = True
            self.api_request = req
            self.api_document_url = self.page.url
            body = _parse_body(req.post_data)
            self.api_request_proof = {'url':API_URL,'method':'POST','content_type':API_MIME,
                'body':body,'body_sha256':_digest(body)}
            route.continue_();return
        if self._request_allowed(req):
            return super()._route(route)
        # Background requests receive no permission and are aborted before dispatch.
        # Navigation is different: it changes the approved document and locks.
        self._record_blocked(req)
        if req.is_navigation_request():
            self._violation('browser_public_navigation_blocked')
        route.abort()

    def _record_blocked(self, request):
        """Bounded path/type diagnostics, kept out of actionable state hashes."""
        self.blocked += 1
        if not hasattr(self,'blocked_requests'):
            self.blocked_requests = []
        if len(self.blocked_requests) >= 32:
            self.blocked_requests_overflow = True
            return
        try:
            p = urlsplit(request.url)
            names = sorted({unquote_plus(part.partition('=')[0])[:64]
                for part in p.query.split('&') if part})[:8]
            entry = {'method':request.method[:16],'host':(p.hostname or '')[:253],
                'path':p.path[:300],'resource':request.resource_type[:32],
                'navigation':bool(request.is_navigation_request()),
                'query_present':bool(p.query),'query_parameter_names':names,
                'fragment_present':bool(p.fragment)}
        except (ValueError,TypeError,AttributeError):
            entry = {'request_identity_invalid':True}
        self.blocked_requests.append(entry)

    def _response(self, response):
        super()._response(response)
        if response.request == self.api_request:
            self.api_status = response.status
            self.api_response = response

    def _finished(self, request):
        super()._finished(request)
        if request != self.api_request:
            return
        reason = 'read'
        try:
            response = self.api_response
            if response is None or self.api_status != 200:
                return
            raw = response.body()
            reason = 'size'
            if not 1 <= len(raw) <= self.MAX_API_BYTES:
                raise ValueError('response size')
            reason = 'json'
            data = _parse_body(raw.decode('utf-8'))
            reason = 'shape'
            primary = [r for r in data['results'] if r.get('component') == 'PRIMARY_AREA']
            if len(primary) != 1 or type(primary[0]['items']) is not list or len(primary[0]['items']) > 24:
                raise ValueError('primary area')
            products = []
            for item in primary[0]['items']:
                if item.get('type') != 'PRODUCT':
                    continue
                product = item['product']; url = product['pipUrl']
                if type(url) is not str:
                    raise ValueError('product URL type')
                # The API can also list combination products ending in s+8
                # digits. They are outside the contracted product capability,
                # just like planners; exclude them without authorizing a URL.
                try:
                    validate_public_target(self.config,url)
                except (InteractionError,ValueError):
                    self.api_unsupported_products += 1
                    continue
                number = url.rstrip('/').rsplit('-',1)[-1]
                if not re.fullmatch('[0-9]{8}',number):
                    self.api_unsupported_products += 1
                    continue
                entry = {'url':url,'number':number}
                if entry not in products:products.append(entry)
            reason = 'no_supported_products'
            if not products:raise ValueError('no product results')
            reason = 'stale'
            if self.api_document_url != self.page.url or not self._is_search(self.page.url) or self.content_pending:
                raise ValueError('stale API')
            self.api_products = products
            self.api_response_sha = _sha(raw)
            self.api_finished = True
        except Exception:
            self.api_error = reason
            self._violation('browser_commerce_response_invalid')

    def _observe_once(self):
        # Base loop keeps its GET proof and control identity machinery; API proof
        # gates result discovery independently of the shell HTTP200.
        snap = super()._observe_once()
        ready = bool(self.api_finished and self.api_status == 200 and self.api_products)
        d = snap['discovery']
        d['commerce_response_error'] = self.api_error
        if self.locked:
            d['commerce_search'] = None
        elif self._historical_search:
            d['commerce_search'] = None
        elif self.query_verified and ready:
            if self._is_search(snap['url']) and not self.content_clicked:
                candidates = [{k:a[k] for k in ('rule_id','control_id','target_url','title','snippet')}
                    for a in snap['actions'] if 'target_url' in a]
                if candidates:
                    self.commerce_proof = {'version':1,'session_id':self.session_id,'config_sha256':_digest(self.config),
                        'query':self.config['query'],'search_page_url':self._search_url(),
                        'search_document_id':self.document_id,'shell':{'status':self.search_status,'finished':self.search_finished},
                        'request':deepcopy(self.api_request_proof),'response':{'status':self.api_status,'finished':self.api_finished,
                            'body_sha256':self.api_response_sha,'products':deepcopy(self.api_products),'products_sha256':_digest(self.api_products)},
                        'dom_sha256':d['dom_sha256'],'candidates':candidates,'selected_target':''}
            if self.commerce_proof:
                self.commerce_proof['selected_target'] = self.target_url if self.content_clicked else ''
                self.commerce_proof['proof_sha256'] = _digest({k:v for k,v in self.commerce_proof.items() if k != 'proof_sha256'})
            d['commerce_search'] = deepcopy(self.commerce_proof)
            if not self.commerce_proof:
                # Result-less pages may finish their API but cannot issue authority.
                d['query_verified'] = self.query_verified = False
                d['search_proof'] = None
        else:
            d['commerce_search'] = None
        snap['state_id'] = _digest({k:v for k,v in snap.items() if k not in {'state_id','actions'}})
        for action in snap['actions']:action['state_id'] = snap['state_id']
        return snap

    def _settle_result_dom(self, dom, args):
        return dom

    def _dom_arguments(self, args):
        return dict(args,products=deepcopy(self.api_products))

    def _navigated(self, frame):
        super()._navigated(frame)
        if frame == self.page.main_frame and hasattr(self,'api_document_url'):
            if self.api_document_url and self.page.url != self.api_document_url:
                if not (self.content_clicked and self.page.url == self.target_url):
                    self.commerce_proof = None
                    self.query_verified = False
                    self.api_finished = False
