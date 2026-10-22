"""Offline navigation replay, bounds and owned-worker public-fetch contract."""
import base64
import copy
import io
import json
from pathlib import Path
import zlib

import pytest

from agent_service import comparison_crosssite_discovery as cross
from agent_service.computer import ComputerError


URL = 'https://example.org/product'
QUERY = 'site:example.org Office desktop file formats'
AT = '2026-10-21T12:00:00+00:00'
HELP = 'https://docs.example.net'


def tree():
    return {
        URL: '<a href="https://docs.example.net/install">Read instructions</a>'
             '<a href="https://docs.example.net/install-mac">Read instructions</a>',
        HELP + '/install': '<a href="/">Home</a><a href="/desktop">Desktop Apps</a>',
        HELP + '/desktop': '<a href="/start">Getting started</a>',
        HELP + '/start': '<p>Supported formats DOCX ODT XLSX ODS</p><a href="/">Home</a>',
    }


def result(pages=None, *, excluded=(), query=QUERY):
    pages = tree() if pages is None else pages
    calls = []
    def fetch(url):
        calls.append(url)
        content = pages[url]
        if isinstance(content, Exception):
            raise content
        return 'text/html; charset=utf-8', content.encode() if isinstance(content, str) else content
    value = cross._discover(cross._request(URL, query, excluded), fetch, AT)
    return value, calls


def test_actual_crosshost_branch_and_earlier_home_edge_are_locally_replayed(monkeypatch):
    from agent_service import browser
    value, calls = result()
    assert calls == [URL, HELP + '/install', HELP + '/desktop', HELP + '/start']
    assert value['discovery']['urls'] == [HELP + '/start', HELP + '/']
    assert [edge['depth'] for edge in value['proof']['chosen']] == [3, 2]
    assert value['proof']['chosen'][1]['parent'] == 1
    assert len(json.dumps(value)) < cross.MAX_RESULT_CHARS
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('replay used network'))
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


def test_explicit_gap_outweighs_repeated_product_words_and_other_gateways():
    pages = {URL: '<a href="/desktop">Office desktop documentation</a>'
                  '<a href="https://reference.example.net/ODF">OpenDocument</a>',
             'https://reference.example.net/ODF': '<p>ODF specification</p>'}
    value, calls = result(pages)
    assert calls == [URL, 'https://reference.example.net/ODF']
    assert value['discovery']['urls'] == ['https://reference.example.net/ODF']


def test_only_actual_anchors_no_synthesized_root_no_crosshost_identity_transfer():
    pages = {URL: '<a href="https://reference.example.net/formats">Formats</a>',
             'https://reference.example.net/formats': '<a href="https://publisher.example.com/">Home</a>'}
    value, calls = result(pages)
    assert value['discovery']['urls'] == ['https://reference.example.net/formats']
    assert 'https://reference.example.net/' not in calls


@pytest.mark.parametrize('href', ['javascript:alert(1)', 'mailto:x@example.com', '#formats',
    'https://user:secret@example.com/formats', 'http://localhost/formats',
    'https://127.0.0.1/formats', '//10.0.0.1/formats', 'https://example.com:8443/formats',
    '/%zz', 'https://example.net/formats.exe'])
def test_unsafe_or_non_document_links_never_fetched(href):
    value, calls = result({URL: f'<a href="{href}">Formats</a>'})
    assert calls == [URL] and value['discovery'] is None
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


def test_base_fragment_dedup_and_case_sensitive_actual_path():
    pages = {URL: '<base href="https://docs.example.net/guide/">'
                  '<a href="Formats#one">Formats</a><a href="Formats#two">Formats</a>',
             HELP + '/guide/Formats': '<p>File support</p>'}
    value, calls = result(pages)
    assert calls == [URL, HELP + '/guide/Formats']
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


def test_completed_urls_excluded_from_fetch_and_candidates():
    value, calls = result(excluded=(HELP + '/desktop',))
    assert HELP + '/desktop' not in calls
    assert value['discovery'] is None
    assert cross.validate_crosssite_result(value, URL, QUERY, (HELP + '/desktop',)) == value
    # The historical source itself may be fetched to discover its actual links.
    value, calls = result(excluded=(URL, HELP + '/'))
    assert calls[0] == URL and value['discovery']['urls'] == [HELP + '/start']


def test_four_fetches_and_three_edges_never_exceeded():
    pages = {URL: '<a href="/docs/one">Documentation</a>',
             'https://example.org/docs/one': '<a href="/docs/two">Documentation</a>',
             'https://example.org/docs/two': '<a href="/docs/three">Documentation</a>',
             'https://example.org/docs/three': '<a href="/formats">Formats</a>'}
    value, calls = result(pages)
    assert len(calls) == 4 and value['discovery'] is None
    assert max(page['depth'] for page in value['proof']['pages']) == 3
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


def test_failure_and_empty_discovery_retained_without_retry():
    pages = {URL: '<a href="/formats">Formats</a>', 'https://example.org/formats': OSError('HTTP302')}
    value, calls = result(pages)
    assert calls == [URL, 'https://example.org/formats']
    assert value['discovery'] is None
    assert value['proof']['pages'][1]['status'] == 'unavailable'
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


@pytest.mark.parametrize('change', ['html', 'sha', 'parent', 'anchor', 'depth', 'boolean-index',
    'query', 'request-hash', 'source', 'excluded', 'chosen', 'chosen-urls', 'discovery', 'time',
    'extra-page', 'extra-field'])
def test_proof_tampering_rejected(change):
    value, _ = result()
    proof = value['proof']
    if change == 'html': proof['pages'][1]['html_zlib_base64'] = cross._pack(b'<p>changed</p>')
    elif change == 'sha': proof['pages'][1]['source_sha256'] = '0' * 64
    elif change == 'parent': proof['pages'][2]['parent'] = 0
    elif change == 'anchor': proof['pages'][2]['anchor'] = 999
    elif change == 'depth': proof['pages'][2]['depth'] = 1
    elif change == 'boolean-index': proof['pages'][2]['parent'] = True
    elif change == 'query': proof['query'] = 'other'
    elif change == 'request-hash': proof['request_sha256'] = '0' * 64
    elif change == 'source': proof['source_url'] = HELP + '/'
    elif change == 'excluded': proof['excluded_urls'] = [HELP + '/start']
    elif change == 'chosen': proof['chosen'][0]['url'] = HELP + '/invented'
    elif change == 'chosen-urls': proof['chosen_urls'] = [HELP + '/invented']
    elif change == 'discovery': value['discovery'] = None
    elif change == 'time': proof['searched_at'] = 'invalid'
    elif change == 'extra-page': proof['pages'].append(copy.deepcopy(proof['pages'][-1]))
    else: proof['pages'][1]['extra'] = 'unbound'
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.validate_crosssite_result(value, URL, QUERY)


@pytest.mark.parametrize('compressed', [
    zlib.compress(b'x' * (cross.MAX_HTML_BYTES + 1)),
    zlib.compress(b'abc') + b'trailing',
    zlib.compress(b'abc') + zlib.compress(b'def'),
    zlib.compress(b'abc')[:-1], b'invalid',
], ids=['decompression-bomb', 'trailing-bytes', 'concatenated-streams', 'truncated', 'invalid'])
def test_compression_bounds_are_strict(compressed):
    value, _ = result()
    value['proof']['pages'][0]['html_zlib_base64'] = base64.b64encode(compressed).decode()
    with pytest.raises(ComputerError):
        cross.validate_crosssite_result(value, URL, QUERY)


def test_exact_byte_limit_and_oversize_source_failure():
    value, _ = result({URL: b' ' * cross.MAX_HTML_BYTES})
    assert cross.validate_crosssite_result(value, URL, QUERY) == value
    value, _ = result({URL: b' ' * (cross.MAX_HTML_BYTES + 1)})
    assert value['proof']['pages'][0]['status'] == 'unavailable'
    assert cross.validate_crosssite_result(value, URL, QUERY) == value


def test_saved_research_html_uses_exact_actual_paths_when_available():
    directory = Path(__file__).resolve().parents[1] / 'validation/artifacts/format-evidence-20261021'
    if not (directory / 'oo-desktop.html').exists():
        pytest.skip('local archived research snapshots are not repository fixtures')
    files = ('oo-download-cached', 'oo-install', 'oo-desktop', 'oo-start')
    pages = {}
    for name in files:
        metadata = json.loads((directory / (name + '.json')).read_text(encoding='utf-8'))
        pages[metadata['url']] = (directory / (name + '.html')).read_bytes()
    url = 'https://www.onlyoffice.com/download-desktop'
    query = 'site:onlyoffice.com ONLYOFFICE Desktop Editors file formats'
    calls = []
    def fetch(target):
        calls.append(target)
        return 'text/html', pages[target]
    value = cross._discover(cross._request(url, query, ()), fetch, AT)
    assert value['discovery']['urls'] == [
        'https://helpcenter.onlyoffice.com/desktop/getting-started.aspx',
        'https://helpcenter.onlyoffice.com/']
    assert len(calls) == 4 and len(json.dumps(value)) <= cross.MAX_RESULT_CHARS
    assert cross.validate_crosssite_result(value, url, query) == value
    lo_url = 'https://www.libreoffice.org/'
    lo_query = 'site:libreoffice.org LibreOffice file formats'
    pages = {lo_url: (directory / 'lo-root-cached.html').read_bytes(),
             'https://wiki.documentfoundation.org/ODF': (directory / 'lo-odf.html').read_bytes()}
    value = cross._discover(cross._request(lo_url, lo_query, ()), fetch, AT)
    assert value['discovery']['urls'] == ['https://wiki.documentfoundation.org/ODF']
    assert cross.validate_crosssite_result(value, lo_url, lo_query) == value


def test_owned_worker_and_output_validation(monkeypatch):
    from agent_service import process
    value, _ = result()
    calls = []
    def child(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert cross.CrossSiteDiscoveryProcess().discover(URL, QUERY) == value
    assert calls[0][0][1:4] == ['-B', '-m', 'agent_service.comparison_crosssite_discovery']
    assert json.loads(calls[0][0][-1]) == cross._request(URL, QUERY, ())
    assert calls[0][1]['timeout'] == cross.CROSSSITE_TIMEOUT


@pytest.mark.parametrize('timeout', [0, -1, 41, True, float('inf'), float('nan')])
def test_invalid_timeout_never_dispatches(monkeypatch, timeout):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: pytest.fail('unexpected worker'))
    with pytest.raises(ComputerError, match='supplement_crosssite_timeout'):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY, timeout=timeout)


@pytest.mark.parametrize('url,query,excluded', [('http://localhost/', QUERY, ()),
    (URL, '', ()), (URL, 'site:example.org', ()), (URL, QUERY, ['https://example.org/' + str(i) for i in range(9)])])
def test_invalid_request_never_dispatches(monkeypatch, url, query, excluded):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: pytest.fail('unexpected worker'))
    with pytest.raises(ComputerError):
        cross.CrossSiteDiscoveryProcess().discover(url, query, excluded)


@pytest.mark.parametrize('error,code', [(TimeoutError(), 'timeout'),
    (InterruptedError(), 'cancelled'), (OSError(), 'unavailable')])
def test_worker_failure_mapping(monkeypatch, error, code):
    from agent_service import process
    def child(*_a, **_k): raise error
    monkeypatch.setattr(process, 'run_child', child)
    with pytest.raises(ComputerError, match='supplement_crosssite_' + code):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY)


@pytest.mark.parametrize('raw', ['garbage', '{}', '[]', 'x' * (cross.MAX_RESULT_CHARS + 1)],
                         ids=['malformed', 'missing-proof', 'wrong-type', 'oversize'])
def test_invalid_worker_output(monkeypatch, raw):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: raw)
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY)


def test_cancellation_before_dispatch_and_after_output(monkeypatch):
    from agent_service import process
    value, _ = result()
    calls = []
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: calls.append(1) or json.dumps(value))
    with pytest.raises(ComputerError, match='supplement_crosssite_cancelled'):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY, should_stop=lambda: True)
    assert not calls
    stopped = iter((False, True))
    with pytest.raises(ComputerError, match='supplement_crosssite_cancelled'):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY, should_stop=lambda: next(stopped))
    assert calls == [1]


def test_worker_gate_and_existing_public_fetch_contract(monkeypatch):
    from agent_service import browser
    monkeypatch.setattr(cross.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'0')))
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('unowned GET'))
    assert cross.main() == 2
    output = io.BytesIO()
    calls = []
    pages = tree()
    monkeypatch.setattr(cross.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'1')))
    monkeypatch.setattr(cross.sys, 'stdout', io.TextIOWrapper(output, write_through=True))
    monkeypatch.setattr(cross.sys, 'argv', ['worker', json.dumps(cross._request(URL, QUERY, ()))])
    monkeypatch.setattr(browser, 'fetch_source', lambda url: calls.append(url) or ('text/html', pages[url].encode()))
    assert cross.main() == 0
    value = json.loads(output.getvalue())
    assert len(calls) == 4 and cross.validate_crosssite_result(value, URL, QUERY) == value


def test_non_ascii_proof_limit_is_utf8_bytes(monkeypatch):
    value, _ = result(query='格式支持')
    assert cross.validate_crosssite_result(value, URL, '格式支持') == value
    size = len(json.dumps(value, ensure_ascii=False))
    monkeypatch.setattr(cross, 'MAX_RESULT_CHARS', size)
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.validate_crosssite_result(value, URL, '格式支持')


def test_non_ascii_worker_output_limit_is_utf8_bytes(monkeypatch):
    from agent_service import process
    value, _ = result(query='格式支持')
    raw = json.dumps(value, ensure_ascii=False)
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: raw)
    assert cross.CrossSiteDiscoveryProcess().discover(URL, '格式支持') == value
    monkeypatch.setattr(cross, 'MAX_RESULT_CHARS', len(raw))
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.CrossSiteDiscoveryProcess().discover(URL, '格式支持')
