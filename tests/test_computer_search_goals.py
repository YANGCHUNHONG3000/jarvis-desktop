"""Offline supervised search, typed plans and real OOXML receipt chain."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from agent_service import computer_search as search
from agent_service import computer_goal_adapters as adapters
from agent_service.budget import BudgetError
from agent_service.computer import ComputerError, read_record
from agent_service.computer_goals import prepare_goal
from agent_service.contracts import BrowserReadError, Observation
from agent_service.model import ModelError
from test_computer_goal_adapters import context, execute, URL, BODY


STAMP = '2026-10-17T12:00:00+00:00'
RECEIPT = {'urls': [URL], 'searched_at': STAMP}


class Search:
    def __init__(self, result=None, error=None):
        self.result = RECEIPT if result is None else result
        self.error, self.calls = error, []
    def search(self, query, max_sources, should_stop, timeout):
        assert not should_stop()
        self.calls.append((query, max_sources, timeout))
        if self.error:
            raise self.error
        return deepcopy(self.result)


class Client:
    last_call = {'state': 'offline'}
    def __init__(self, value=None, error=None, after=lambda: None):
        self.value = value if value is not None else {'kind': 'public_research', 'query': '真实主题', 'title': '资料摘录'}
        self.error, self.after, self.calls = error, after, []
    def complete(self, system, user, max_tokens):
        self.calls.append((system, json.loads(user), max_tokens))
        self.after()
        if self.error:
            raise self.error
        return deepcopy(self.value)


def plan(context):
    return prepare_goal(context['session'], '主题公开研究', {}, adapters.search_template('真实主题', '资料摘录'))


def test_search_process_uses_fixed_module_and_forwards_bounded_arguments(monkeypatch):
    calls = []
    def run_child(command, should_stop, timeout):
        calls.append((command, should_stop(), timeout))
        return json.dumps(RECEIPT)
    monkeypatch.setattr('agent_service.process.run_child', run_child)
    assert search.SearchProcess().search('主题 ; literal $(x)', 1, timeout=7) == RECEIPT
    command, stopped, timeout = calls[0]
    assert command[:4] == [sys.executable, '-B', '-m', 'agent_service.computer_search']
    assert len(command) == 5
    assert json.loads(command[4]) == {'query': '主题 ; literal $(x)', 'max_sources': 1}
    assert not stopped and timeout == 7


@pytest.mark.parametrize('gate', [b'', b'0', b'1'])
def test_child_gate_precedes_search_and_returns_only_validated_receipt(monkeypatch, gate):
    calls, output = [], BytesIO()
    monkeypatch.setattr(search.sys, 'stdin', SimpleNamespace(buffer=BytesIO(gate)))
    monkeypatch.setattr(search.sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(search.sys, 'argv', ['fixed', json.dumps({'query': 'topic', 'max_sources': 1})])
    monkeypatch.setattr('agent_service.research.search_public', lambda q, n: calls.append((q, n)) or [URL])
    result = search.main()
    if gate != b'1':
        assert result == 2 and not calls and output.getvalue() == b''
    else:
        assert result == 0 and calls == [('topic', 1)]
        assert search.validate_result(json.loads(output.getvalue()), 1)['urls'] == [URL]


@pytest.mark.parametrize('timeout', [0, -1, 26, True, float('nan'), float('inf')])
def test_invalid_timeout_never_spawns(monkeypatch, timeout):
    monkeypatch.setattr('agent_service.process.run_child', lambda *a, **kw: pytest.fail('must not spawn'))
    with pytest.raises(ComputerError, match='search_timeout'):
        search.SearchProcess().search('topic', timeout=timeout)


@pytest.mark.parametrize('error,code', [(InterruptedError(), 'search_cancelled'),
    (TimeoutError(), 'search_timeout'), (OSError('secret proxy'), 'search_unavailable'),
    (RuntimeError('raw child details'), 'search_unavailable')])
def test_child_errors_are_safe(monkeypatch, error, code):
    def run_child(*args, **kwargs):
        raise error
    monkeypatch.setattr('agent_service.process.run_child', run_child)
    with pytest.raises(ComputerError, match='^' + code + '$'):
        search.SearchProcess().search('topic')


@pytest.mark.parametrize('raw', ['', 'not json', '[]', 'x' * 10001,
    json.dumps({'urls': [], 'searched_at': STAMP}),
    json.dumps({'urls': [URL, URL], 'searched_at': STAMP}),
    json.dumps({'urls': ['https://127.0.0.1/'], 'searched_at': STAMP}),
    json.dumps({'urls': [URL + '#fragment'], 'searched_at': STAMP}),
    json.dumps({'urls': [URL], 'searched_at': '2026-10-17T12:00:00'}),
    json.dumps({'urls': [URL], 'searched_at': STAMP, 'extra': True})])
def test_malformed_child_receipt_rejected(monkeypatch, raw):
    monkeypatch.setattr('agent_service.process.run_child', lambda *a, **kw: raw)
    with pytest.raises(ComputerError, match='search_receipt_invalid'):
        search.SearchProcess().search('topic')


@pytest.mark.parametrize('when', ['before', 'after'])
def test_search_process_cancellation_before_and_after_child(monkeypatch, when):
    stopped, calls = [when == 'before'], []
    def run_child(*args, **kwargs):
        calls.append(True)
        stopped[0] = True
        return json.dumps(RECEIPT)
    monkeypatch.setattr('agent_service.process.run_child', run_child)
    with pytest.raises(ComputerError, match='search_cancelled'):
        search.SearchProcess().search('topic', should_stop=lambda: stopped[0])
    assert len(calls) == (when == 'after')


def test_topic_search_reads_actual_body_then_verified_excel_word(context):
    engine = Search()
    context['options']['search'] = engine
    outcome = execute(context, plan(context))
    assert outcome['ok'] and len(context['effects']) == 2
    assert engine.calls[0][:2] == ('真实主题', 2) and 0 < engine.calls[0][2] <= 25
    assert context['browser'].calls == [URL]
    source = read_record(Path(outcome['outputs']['sources']['path']))
    assert source['discovery'] == RECEIPT and source['pages'][0]['text'] == BODY
    discovery = read_record(Path(outcome['run']) / 'discovery-1.json')
    assert discovery['query'] == '真实主题' and discovery['urls'] == [URL]
    assert context['effects'][0][0]['rows'][0][3] == BODY[:1200]
    assert context['effects'][1][0]['table']['rows'] == context['effects'][0][0]['rows']


@pytest.mark.parametrize('value', [{'urls': [], 'searched_at': STAMP},
    {'urls': [URL, URL], 'searched_at': STAMP},
    {'urls': ['file:///C:/secret'], 'searched_at': STAMP},
    {'urls': [URL, 'https://example.org/', 'https://example.net/'], 'searched_at': STAMP}])
def test_parent_revalidates_injected_search_before_any_browser_or_office(context, value):
    context['options']['search'] = Search(result=value)
    outcome = execute(context, plan(context))
    assert not outcome['ok'] and outcome['code'] == 'search_receipt_invalid'
    assert not context['browser'].calls and not context['effects']


@pytest.mark.parametrize('stage', ['search', 'body'])
def test_search_or_body_failure_never_creates_office(context, stage):
    context['options']['search'] = Search(error=ComputerError('search_timeout') if stage == 'search' else None)
    if stage == 'body':
        def read(*args, **kwargs):
            raise BrowserReadError('network_timeout')
        context['browser'].read = read
    outcome = execute(context, plan(context))
    assert not outcome['ok'] and outcome['state'] == 'stopped' and not context['effects']
    assert outcome['code'] == ('search_timeout' if stage == 'search' else 'network_timeout')


def test_search_timeout_is_clamped_to_remaining_goal_deadline(context):
    tick = [1000]
    engine = Search()
    context['options']['search'] = engine
    def progress(value):
        tick[0] += 235
    outcome = execute(context, plan(context), clock=lambda: tick[0], progress=progress)
    assert engine.calls[0][2] == 5
    assert not outcome['ok'] and not context['effects']


@pytest.mark.parametrize('value', [[], {'kind': 'public_research', 'query': 'topic'},
    {'kind': 'public_research', 'query': 'topic', 'title': 'title', 'urls': [URL]},
    {'kind': 'public_research', 'query': 'line\nbreak', 'title': 'title'},
    {'kind': 'public_research', 'query': 'topic', 'title': 'title\tbad'},
    {'kind': 'unsupported', 'reason': '', 'extra': True}])
def test_model_research_schema_rejected_without_execution(context, value):
    client = Client(value=value)
    with pytest.raises(ComputerError):
        adapters.propose_research_goal(context['session'], 'request', client=client)
    assert len(client.calls) == 1 and not context['effects'] and not context['browser'].calls


def test_model_proposal_only_previews_and_unsupported_never_executes(context):
    client = Client()
    proposed = adapters.propose_research_goal(context['session'], 'request', client=client)
    assert proposed['plan']['steps'][0]['op'] == 'public_search'
    assert len(client.calls) == 1 and not context['effects'] and not context['browser'].calls
    unsupported = Client(value={'kind': 'unsupported', 'reason': '需要登录'})
    assert adapters.propose_research_goal(context['session'], 'request', client=unsupported)['plan'] is None


@pytest.mark.parametrize('when', ['before', 'after'])
def test_model_cancellation_and_claim_accounting(context, when):
    stopped = [when == 'before']
    client = Client(after=lambda: stopped.__setitem__(0, True))
    with pytest.raises(ComputerError, match='cancelled_' + when + '_model'):
        adapters.propose_research_goal(context['session'], 'request', client=client, stop=lambda: stopped[0])
    claims = list((context['session'].root / 'model_calls').glob('[1-6].json'))
    assert len(claims) == len(client.calls) == (when == 'after')
    if when == 'after':
        assert read_record(claims[0].with_suffix('.outcome.json'))['call'] == client.last_call


def test_same_session_six_attempt_cap_includes_model_errors(context):
    client = Client(error=ModelError('offline_model_error'))
    for _ in range(6):
        with pytest.raises(ComputerError, match='offline_model_error'):
            adapters.propose_research_goal(context['session'], 'request', client=client)
    with pytest.raises(ComputerError, match='session_model_call_limit'):
        adapters.propose_research_goal(context['session'], 'request', client=client)
    assert len(client.calls) == 6
    assert len(list((context['session'].root / 'model_calls').glob('*.outcome.json'))) == 6


def test_budget_failure_keeps_claim_and_safe_outcome_without_real_ledger(context):
    client = Client(error=BudgetError('private ledger information'))
    with pytest.raises(ComputerError, match='^model_budget_exceeded$'):
        adapters.propose_research_goal(context['session'], 'request', client=client)
    assert read_record(context['session'].root / 'model_calls' / '1.outcome.json')['call'] == client.last_call
    assert not context['effects'] and not context['browser'].calls


@pytest.mark.parametrize('query,count', [('', 1), ('x\ncode', 1), ('topic', True),
                                        ('topic', 0), ('topic', 3), ('x' * 501, 1)])
def test_query_and_count_rejected_before_child(monkeypatch, query, count):
    monkeypatch.setattr('agent_service.process.run_child', lambda *a, **kw: pytest.fail('must not spawn'))
    with pytest.raises(ComputerError, match='search_query_invalid|search_count_invalid'):
        search.SearchProcess().search(query, count)


def test_child_search_error_returns_only_safe_receipt(monkeypatch):
    output = BytesIO()
    monkeypatch.setattr(search.sys, 'stdin', SimpleNamespace(buffer=BytesIO(b'1')))
    monkeypatch.setattr(search.sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(search.sys, 'argv', ['fixed', json.dumps({'query': 'topic', 'max_sources': 1})])
    def fail(*args, **kwargs):
        raise RuntimeError('proxy password private detail')
    monkeypatch.setattr('agent_service.research.search_public', fail)
    assert search.main() == 0
    assert json.loads(output.getvalue()) == {'error': 'search_unavailable'}


def test_default_client_uses_existing_cloud_constructor_deadline_and_stop(context, monkeypatch):
    calls, client = [], Client()
    def construct(**kwargs):
        calls.append(kwargs)
        return client
    monkeypatch.setattr('agent_service.model.CloudClient', construct)
    stop = lambda: False
    assert adapters.propose_research_goal(context['session'], 'request', stop=stop)['plan']
    assert calls[0]['should_stop'] is stop and type(calls[0]['deadline']) is float
    assert len(client.calls) == 1 and not context['effects'] and not context['browser'].calls


@pytest.mark.parametrize('query', [' ' * 500 + 'x', 'x' + ' ' * 500, ' ' * 250 + 'x' + ' ' * 250])
def test_padded_query_counts_all_original_characters(monkeypatch, query):
    monkeypatch.setattr('agent_service.process.run_child', lambda *a, **kw: pytest.fail('must not spawn'))
    with pytest.raises(ComputerError, match='search_query_invalid'):
        search.SearchProcess().search(query)


@pytest.mark.parametrize('field,limit', [('instruction', 1000), ('title', 200), ('reason', 500)])
def test_padded_model_text_counts_all_original_characters(context, field, limit):
    padded = ' ' * limit + 'x'
    if field == 'instruction':
        client = Client()
        with pytest.raises(ComputerError, match='adapter_text_invalid'):
            adapters.propose_research_goal(context['session'], padded, client=client)
        assert not client.calls
    else:
        value = ({'kind': 'public_research', 'query': 'topic', 'title': padded}
                 if field == 'title' else {'kind': 'unsupported', 'reason': padded})
        with pytest.raises(ComputerError, match='adapter_text_invalid'):
            adapters.propose_research_goal(context['session'], 'request', client=Client(value=value))
    assert not context['effects'] and not context['browser'].calls


SECOND_URL = 'https://example.org/verified'


class CandidateBrowser:
    def __init__(self, fail_all=False, on_first=lambda: None):
        self.calls = []
        self.fail_all, self.on_first = fail_all, on_first
    def read(self, url, should_stop):
        assert not should_stop()
        self.calls.append(url)
        if len(self.calls) == 1:
            self.on_first()
        if url == URL or self.fail_all:
            raise BrowserReadError('browser_failed')
        return Observation(url, url, '真实第二来源', BODY, STAMP)


def configure_candidates(context, browser):
    context['options']['search'] = Search(result={'urls': [URL, SECOND_URL], 'searched_at': STAMP})
    context['options']['browser'] = browser


def test_first_candidate_failure_second_actual_body_creates_truthful_excel_word(context):
    browser = CandidateBrowser()
    configure_candidates(context, browser)
    outcome = execute(context, plan(context))
    assert outcome['ok'] and browser.calls == [URL, SECOND_URL]
    assert len(context['effects']) == 2
    sources = read_record(Path(outcome['outputs']['sources']['path']))
    assert [page['final_url'] for page in sources['pages']] == [SECOND_URL]
    assert sources['pages'][0]['text'] == BODY
    assert len(sources['read_failures']) == 1
    failure = sources['read_failures'][0]
    assert failure['url'] == URL and failure['code'] == 'browser_failed'
    assert isinstance(failure['captured_at'], str) and failure['captured_at']
    sheet, document = [spec for spec, _ in context['effects']]
    assert len(sheet['headers']) == 6 and sheet['headers'][-1] == '读取状态'
    assert len(sheet['rows']) == 2
    successful = next(row for row in sheet['rows'] if row[1] == SECOND_URL)
    failed = next(row for row in sheet['rows'] if row[1] == URL)
    assert successful[3] == BODY[:1200] and successful[-1] == '已读取'
    assert failed[3] == '' and failed[-1] == '读取失败'
    assert failed[4] == '未获得正文；未用于内容；读取失败：browser_failed'
    assert failed[0] == '未读取的候选'
    assert document['table'] == {'headers': sheet['headers'], 'rows': sheet['rows']}
    assert outcome['source_reads'] == 1 and outcome['failures'] == [failure]


def test_all_search_candidates_fail_without_creating_any_office(context):
    browser = CandidateBrowser(fail_all=True)
    configure_candidates(context, browser)
    outcome = execute(context, plan(context))
    assert not outcome['ok'] and outcome['state'] == 'stopped'
    assert browser.calls == [URL, SECOND_URL] and not context['effects']
    assert not outcome['completed'] and not outcome['artifacts']
    assert outcome['source_reads'] == 0
    assert [item['url'] for item in outcome['failures']] == [URL, SECOND_URL]
    for candidate in (1, 2):
        evidence = read_record(Path(outcome['run']) / f'read-1-{candidate}.json')
        assert evidence['ok'] is False and evidence['code'] == 'browser_failed'


def test_cancel_on_failed_candidate_stops_without_second_read_or_office(context):
    stopped = [False]
    browser = CandidateBrowser(on_first=lambda: stopped.__setitem__(0, True))
    configure_candidates(context, browser)
    outcome = execute(context, plan(context), stop=lambda: stopped[0])
    assert not outcome['ok'] and outcome['code'] == 'goal_cancelled'
    assert browser.calls == [URL] and not context['effects']


def test_explicit_public_read_remains_strict_on_first_browser_failure(context):
    browser = CandidateBrowser()
    configure_candidates(context, browser)
    prepared = prepare_goal(context['session'], '明确两来源', {},
                           adapters.template([URL, SECOND_URL], '资料摘录'))
    outcome = execute(context, prepared)
    assert not outcome['ok'] and outcome['code'] == 'browser_failed'
    assert browser.calls == [URL] and not context['effects']


def test_unexpected_candidate_error_is_not_silently_skipped(context):
    browser = CandidateBrowser()
    def read(url, should_stop):
        browser.calls.append(url)
        raise RuntimeError('unexpected adapter corruption')
    browser.read = read
    configure_candidates(context, browser)
    outcome = execute(context, plan(context))
    assert not outcome['ok'] and browser.calls == [URL] and not context['effects']


def test_goal_deadline_on_failed_candidate_stops_without_second_read(context):
    tick = [1000]
    browser = CandidateBrowser(on_first=lambda: tick.__setitem__(0, 1240))
    configure_candidates(context, browser)
    outcome = execute(context, plan(context), clock=lambda: tick[0])
    assert not outcome['ok'] and outcome['code'] == 'goal_deadline'
    assert browser.calls == [URL] and not context['effects']


def test_malformed_successful_observation_is_not_skipped_as_candidate_failure(context):
    browser = CandidateBrowser()
    def read(url, should_stop):
        browser.calls.append(url)
        return Observation(url, SECOND_URL, 'changed URL', BODY, STAMP)
    browser.read = read
    configure_candidates(context, browser)
    outcome = execute(context, plan(context))
    assert not outcome['ok'] and outcome['code'] == 'browser_observation_invalid'
    assert browser.calls == [URL] and not context['effects']


@pytest.mark.parametrize('query,body,start,method', [
    ('Python pathlib', 'navigation\n' * 200 + 'PYTHON\npathlib Module\nActual docs', 2200, 'query_phrase'),
    ('[a+b]', 'menu\n[a+b] literal phrase', 5, 'query_phrase'),
    ('missing topic', 'actual page', 0, 'page_start'),
    ('中文主题', '菜单\n中文主题 内容', 3, 'query_phrase'),
])
def test_query_excerpt_retains_exact_source_interval(query, body, start, method):
    value = adapters._search_excerpt(body, query)
    assert value['start'] == start and value['method'] == method
    assert value['text'] == body[start:value['end']]
    assert value['end'] == min(len(body), start + 1200)


def test_located_excerpt_reaches_saved_excel_and_word(context):
    body = 'navigation\n' * 200 + 'Python pathlib\n' + 'real content ' * 150
    context['options']['search'] = Search()
    context['browser'].read = lambda url, **kw: Observation(url, url, 'page', body, STAMP)
    prepared = prepare_goal(context['session'], '主题', {}, adapters.search_template('Python pathlib', '摘录'))
    outcome = execute(context, prepared)
    assert outcome['ok']
    source = read_record(Path(outcome['outputs']['sources']['path']))
    excerpt = source['excerpts'][0]
    assert excerpt['start'] == 2200 and excerpt['end'] == 3400
    assert source['pages'][0]['text'] == body
    sheet, doc = [spec for spec, _ in context['effects']]
    assert sheet['rows'][0][3] == body[2200:3400].replace('\n', ' ')
    assert '[2200, 3400)' in sheet['rows'][0][4]
    assert doc['table']['rows'] == sheet['rows']


@pytest.mark.parametrize('provider,comparison', [(None,False), ('bing',False), (None,True)])
def test_four_candidate_search_requires_explicit_bing_comparison(monkeypatch, provider, comparison):
    monkeypatch.setattr('agent_service.process.run_child', lambda *a, **k: pytest.fail('must not spawn'))
    with pytest.raises(ComputerError):
        search.SearchProcess(provider=provider, comparison=comparison).search('topic', 4)


def test_explicit_comparison_contract_is_forwarded_and_bounded(monkeypatch):
    receipt = {'urls':[URL+str(i) for i in range(4)], 'searched_at':STAMP}
    calls=[]
    monkeypatch.setattr('agent_service.process.run_child', lambda command, **kw: calls.append(command) or json.dumps(receipt))
    process=search.SearchProcess(provider='bing',comparison=True)
    assert process.search('topic',4)==receipt
    assert json.loads(calls[0][-1]) == {'query':'topic','max_sources':4,'provider':'bing','comparison':True}
    with pytest.raises(ComputerError,match='search_count_invalid'):
        process.search('topic',5)
    assert len(calls)==1
    assert search.validate_result(RECEIPT,4)==RECEIPT  # Old smaller receipts remain valid.
    with pytest.raises(ComputerError,match='search_receipt_invalid'):
        search.validate_result(receipt,2)


def test_child_accepts_only_explicit_four_result_contract(monkeypatch):
    output=BytesIO(); calls=[]
    monkeypatch.setattr(search.sys,'stdin',SimpleNamespace(buffer=BytesIO(b'1')))
    monkeypatch.setattr(search.sys,'stdout',SimpleNamespace(buffer=output))
    monkeypatch.setattr(search.sys,'argv',['fixed',json.dumps({'query':'topic','max_sources':4,'provider':'bing','comparison':True})])
    urls=[URL+str(i) for i in range(4)]
    monkeypatch.setattr('agent_service.research.search_public', lambda q,n,**kw: calls.append((q,n,kw)) or urls)
    assert search.main()==0
    assert calls==[('topic',4,{'provider':'bing','comparison':True})]
    assert search.validate_result(json.loads(output.getvalue()),4)['urls']==urls
