"""Pure anchor proof and supervised-worker bounds; no live network access."""
import io
import json

import pytest

from agent_service import comparison_link_discovery as links
from agent_service.computer import ComputerError


URL = 'https://example.org/products/source'
QUERY = 'site:example.org project file formats'
AT = '2026-10-21T12:00:00+00:00'


def result(html, *, query=QUERY, excluded=(), mime='text/html; charset=utf-8'):
    return links.make_link_result(URL, query, excluded, mime, html.encode('utf-8'), AT)


def test_rank_real_same_host_anchors_and_replay_without_fetch(monkeypatch):
    from agent_service import browser
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('unexpected GET'))
    value = result('<a href="/home">Home</a><a href="/about">About company</a>'
                   '<a href="/format">File formats</a><a href="/features">Features</a>')
    assert value['discovery']['urls'] == ['https://example.org/format']
    assert links.validate_link_result(value, URL, QUERY) == value


def test_query_gap_prefers_about_and_excludes_existing_targets():
    html = '<a href="/format">Formats</a><a href="/about">About company</a><a href="/legal">Legal</a>'
    value = result(html, query='site:example.org project about company', excluded=['https://example.org/about'])
    assert value['discovery']['urls'] == ['https://example.org/legal']
    assert 'https://example.org/about' not in value['proof']['chosen_urls']
    about = result('<a href="/features">Features</a><a href="/about">About</a>',
                   query='site:example.org project about company')
    assert about['discovery']['urls'][0] == 'https://example.org/about'


@pytest.mark.parametrize('href', [
    'https://other.org/about', 'https://sub.example.org/about', 'https://www.example.org/about',
    'http://localhost/about', 'http://127.0.0.1/about', 'https://example.org:8443/about',
    'https://user:password@example.org/about', 'javascript:alert(1)', 'mailto:hi@example.org',
    '#about', '/%zz', '//10.0.0.1/about',
])
def test_unsafe_external_or_non_exact_host_targets_are_not_candidates(href):
    query = 'site:example.org about company'
    value = result(f'<a href="{href}">About company</a>', query=query)
    assert value['discovery'] is None
    assert links.validate_link_result(value, URL, query) == value


def test_fragment_dedup_and_base_resolution():
    value = result('<base href="/docs/"><a href="formats#one">Formats</a>'
                   '<a href="formats#two">Formats</a><a href="../about">About</a>',
                   query='site:example.org formats about')
    assert value['discovery']['urls'] == ['https://example.org/docs/formats', 'https://example.org/about']
    external = result('<base href="https://other.org/"><a href="about">About</a>'
                      '<a href="https://example.org/about">About</a>', query='site:example.org about')
    assert external['discovery']['urls'] == ['https://example.org/about']
    assert result('<a href="/about/../legal">Legal</a>', query='site:example.org legal')['discovery']['urls'] == ['https://example.org/legal']


def test_identity_gap_outweighs_repeated_topic_on_unrelated_blog_links():
    value = result('<a href="/blog/2020/editor-suite">Editor Suite</a>'
                   '<a href="/blog/2021/editor-suite">Editor Suite latest</a>'
                   '<a href="/legalterms">Legal notice</a>',
                   query='site:example.org Editor Suite about company')
    assert value['discovery']['urls'] == ['https://example.org/legalterms']


def test_format_gap_excludes_unrelated_about_and_external_format_link():
    value = result('<a href="/about">About company</a><a href="/features">Features</a>'
                   '<a href="https://other.org/odf">OpenDocument</a>')
    assert value['discovery'] is None


@pytest.mark.parametrize('path,label', [('/odf', 'ODF'), ('/open-document', 'OpenDocument'),
    ('/compatibility', 'Compatibility'), ('/file-types', 'File types'), ('/extensions', 'Extensions')])
def test_generic_format_equivalents_are_gap_candidates(path, label):
    value = result(f'<a href="{path}">{label}</a><a href="/about">About company</a>')
    assert value['discovery']['urls'] == ['https://example.org' + path]


@pytest.mark.parametrize('change', ['bytes', 'sha', 'urls', 'time', 'query', 'source', 'exclude', 'discovery'])
def test_proof_mutation_rejected(change):
    value = result('<a href="/format">Formats</a>')
    if change == 'bytes': value['proof']['html_base64'] = 'YWJj'
    elif change == 'sha': value['proof']['source_sha256'] = '0' * 64
    elif change == 'urls': value['proof']['chosen_urls'] = ['https://example.org/about']
    elif change == 'time': value['proof']['searched_at'] = 'not-time'
    elif change == 'query': value['proof']['query'] = 'other'
    elif change == 'source': value['proof']['source_url'] = 'https://example.org/other'
    elif change == 'exclude': value['proof']['excluded_urls'] = ['https://example.org/format']
    else: value['discovery'] = None
    with pytest.raises(ComputerError):
        links.validate_link_result(value, URL, QUERY)


def test_byte_limit_exact_proof_and_plain_text():
    content = b'<a href="/about">About</a>'
    content += b' ' * (links.MAX_HTML_BYTES - len(content))
    value = links.make_link_result(URL, QUERY, (), 'text/html', content, AT)
    assert len(json.dumps(value, ensure_ascii=False)) < 280_000
    assert links.validate_link_result(value, URL, QUERY) == value
    with pytest.raises(ComputerError):
        links.make_link_result(URL, QUERY, (), 'text/html', content + b' ', AT)
    assert result('<a href="/about">About</a>', mime='text/plain')['discovery'] is None


@pytest.mark.parametrize('url,query,excluded', [
    ('http://localhost/', QUERY, ()), (URL, '', ()), (URL, 'site:example.org', ()), (URL, 'x' * 501, ()),
    (URL, QUERY, ['https://example.org/' + str(i) for i in range(9)]),
])
def test_invalid_requests_do_not_dispatch(monkeypatch, url, query, excluded):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ComputerError):
        links.LinkDiscoveryProcess().discover(url, query, excluded)


def test_process_uses_fixed_owned_worker_and_validates_output(monkeypatch):
    from agent_service import process
    calls = []
    value = result('<a href="/format">Formats</a>')
    def child(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert links.LinkDiscoveryProcess().discover(URL, QUERY) == value
    assert calls[0][0][1:4] == ['-B', '-m', 'agent_service.comparison_link_discovery']
    assert json.loads(calls[0][0][-1]) == {'source_url': URL, 'query': QUERY, 'excluded_urls': []}
    assert calls[0][1]['timeout'] == links.LINK_TIMEOUT


@pytest.mark.parametrize('error,code', [(TimeoutError(), 'supplement_links_timeout'),
    (InterruptedError(), 'supplement_links_cancelled'), (OSError(), 'supplement_links_unavailable')])
def test_process_maps_safe_failures(monkeypatch, error, code):
    from agent_service import process
    def child(*_a, **_k): raise error
    monkeypatch.setattr(process, 'run_child', child)
    with pytest.raises(ComputerError, match=code):
        links.LinkDiscoveryProcess().discover(URL, QUERY)


@pytest.mark.parametrize('raw', ['garbage', '{}', '[]', '{"error":[]}', 'x' * (links.MAX_RESULT_CHARS + 1)],
                         ids=['malformed', 'missing-proof', 'wrong-type', 'wrong-error-type', 'oversize'])
def test_process_rejects_unverifiable_worker_output(monkeypatch, raw):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: raw)
    with pytest.raises(ComputerError, match='supplement_links_proof_invalid'):
        links.LinkDiscoveryProcess().discover(URL, QUERY)


@pytest.mark.parametrize('timeout', [0, -1, 21, True, float('inf'), float('nan')])
def test_process_invalid_timeout_does_not_dispatch(monkeypatch, timeout):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: pytest.fail('unexpected dispatch'))
    with pytest.raises(ComputerError, match='supplement_links_timeout'):
        links.LinkDiscoveryProcess().discover(URL, QUERY, timeout=timeout)


def test_source_oversize_is_safe_unavailable_for_bing_fallback(monkeypatch):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k:
                        json.dumps({'error': 'supplement_links_source_too_large'}))
    with pytest.raises(ComputerError, match='supplement_links_unavailable'):
        links.LinkDiscoveryProcess().discover(URL, QUERY)


def test_cancellation_before_dispatch_and_after_output(monkeypatch):
    from agent_service import process
    calls = []
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: calls.append(1) or json.dumps(result('<p>none</p>')))
    with pytest.raises(ComputerError, match='supplement_links_cancelled'):
        links.LinkDiscoveryProcess().discover(URL, QUERY, should_stop=lambda: True)
    assert not calls
    stopped = iter((False, True))
    with pytest.raises(ComputerError, match='supplement_links_cancelled'):
        links.LinkDiscoveryProcess().discover(URL, QUERY, should_stop=lambda: next(stopped))
    assert calls == [1]


def test_worker_waits_for_parent_before_network(monkeypatch):
    from agent_service import browser
    monkeypatch.setattr(links.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'0')))
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('unexpected GET'))
    assert links.main() == 2


def test_worker_fetches_source_once_and_emits_replayable_proof(monkeypatch):
    from agent_service import browser
    calls = []
    output = io.BytesIO()
    monkeypatch.setattr(links.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'1')))
    monkeypatch.setattr(links.sys, 'stdout', io.TextIOWrapper(output, write_through=True))
    monkeypatch.setattr(links.sys, 'argv', ['worker', json.dumps({'source_url': URL, 'query': QUERY, 'excluded_urls': []})])
    monkeypatch.setattr(browser, 'fetch_source', lambda url: calls.append(url) or ('text/html', b'<a href="/about">About</a>'))
    assert links.main() == 0 and calls == [URL]
    value = json.loads(output.getvalue())
    assert links.validate_link_result(value, URL, QUERY) == value
