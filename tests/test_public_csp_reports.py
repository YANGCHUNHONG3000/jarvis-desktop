"""Public CSP refusal diagnostics; no live browser, network, resume or model."""
from types import SimpleNamespace
import pytest
from agent_service.browser_public import PublicSearchEdgeSession


def config(site='python_docs'):
    return {'site':site}


@pytest.mark.parametrize('method,navigation',[('POST',False),('POST',True),('GET',True),('GET',False)])
def test_blocked_request_diagnostics_are_bounded_and_exclude_sensitive_url_parts(method,navigation):
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.config=config()
    s.blocked=0;s.blocked_requests=[];s.blocked_requests_overflow=False
    s.locked=False;s.failures=[];s._request_allowed=lambda req:False
    aborted=[]
    req=SimpleNamespace(method=method,url='https://private-user:private-password@example.org/telemetry?secret-query=x#secret-fragment',
        resource_type='fetch',is_navigation_request=lambda:navigation)
    route=SimpleNamespace(request=req,abort=lambda:aborted.append(True))
    for _ in range(18):s._route(route)
    assert s.blocked==18 and len(aborted)==18
    assert len(s.blocked_requests)==16 and s.blocked_requests_overflow
    assert s.blocked_requests[0]=={'method':method,'host':'example.org','path':'/telemetry','resource':'fetch','navigation':navigation,
        'query_present':True,'query_parameter_names':['secret-query'],'fragment_present':True,'content_type':None}
    assert s.locked is (navigation or method!='GET')
    assert not any('private-' in str(value) or 'secret-fragment' in str(value) for item in s.blocked_requests for value in item.values())


@pytest.mark.parametrize('change',[
    {},{'site':'python_docs'},
    {'url':'https://o20005.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39936/security/?sentry_key=fixture-public-key'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/extra?sentry_key=fixture-public-key'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?secret=value'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key#fragment'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key='},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=a&sentry_key=b'},
    {'url':'https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=a&extra=b'},
    {'url':'http://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key'},
    {'url':'https://o20004.ingest.us.sentry.io:443/api/39935/security/?sentry_key=fixture-public-key'},
    {'url':'https://user@o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key'},
    {'method':'PUT'},{'navigation':True},{'resource':'fetch'},
    {'content_type':'application/json'},
])
def test_only_exact_python_org_csp_report_is_aborted_without_locking(change):
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.config=config(change.get('site','python_org'))
    s.blocked=0;s.blocked_requests=[];s.blocked_requests_overflow=False
    s.locked=False;s.failures=[];s._request_allowed=lambda req:False
    req=SimpleNamespace(method=change.get('method','POST'),
        url=change.get('url','https://o20004.ingest.us.sentry.io/api/39935/security/?sentry_key=fixture-public-key'),
        resource_type=change.get('resource','other'),
        is_navigation_request=lambda:change.get('navigation',False),
        header_value=lambda name:change.get('content_type','application/csp-report'))
    aborted=[];sent=[]
    s._route(SimpleNamespace(request=req,abort=lambda:aborted.append(True),continue_=lambda:sent.append(True)))
    assert aborted==[True] and not sent and s.blocked==1
    assert s.locked is bool(change)
    assert s.blocked_requests[0].get('blocked_without_effect',False) is (not change)
    if change:assert s.failures==[{'code':'browser_public_method_blocked'}]


def test_request_diagnostics_read_only_content_type_and_bounded_parameter_names():
    s=PublicSearchEdgeSession.__new__(PublicSearchEdgeSession)
    s.config=config();s.blocked=0;s.blocked_requests=[];s.blocked_requests_overflow=False
    s.locked=False;s.failures=[];s._request_allowed=lambda req:False
    header_reads=[]
    def header(name):
        header_reads.append(name)
        return 'application/csp-report; boundary=secret-boundary'
    req=SimpleNamespace(method='POST',url='https://example.org/report?'+ '&'.join('field'+str(i)+'=secret-value' for i in range(12))+'#secret-fragment',
        resource_type='other',is_navigation_request=lambda:False,header_value=header)
    s._route(SimpleNamespace(request=req,abort=lambda:None))
    diagnostic=s.blocked_requests[0]
    assert header_reads==['content-type'] and diagnostic['content_type']=='application/csp-report'
    assert diagnostic['query_present'] and diagnostic['fragment_present']
    assert len(diagnostic['query_parameter_names'])==8
    assert 'secret-' not in str(diagnostic)
