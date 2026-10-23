"""Offline attribution navigation and versioned proof replay."""
import copy
import io
import json

import pytest

from agent_service import comparison_crosssite_discovery as cross
from agent_service.computer import ComputerError


URL = 'https://docs.example.org/tutorial'
QUERY = 'site:docs.example.org about company publisher'
AT = '2026-10-23T12:00:00+00:00'
AUTHORS = 'https://docs.example.org/authors'
ABOUT = 'https://docs.example.org/about'


def discover(pages, excluded=()):
    calls = []
    def fetch(url):
        calls.append(url)
        content = pages[url]
        if isinstance(content, Exception):
            raise content
        return 'text/html', content.encode()
    value = cross._discover(cross._request(URL, QUERY, excluded, purpose='attribution'),
                            fetch, AT)
    return value, calls


def test_actual_authors_anchor_preferred_and_replayed():
    pages = {URL: '<a href="https://other.example.net/about">About us</a>'
                  '<a href="/about">About</a><a href="/authors">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>',
             ABOUT: '<h1>About us</h1><p>Published by Example Foundation.</p>',
             'https://other.example.net/about': '<h1>About us</h1>'}
    value, calls = discover(pages)
    assert calls[:2] == [URL, AUTHORS]
    assert value['discovery']['urls'] == [AUTHORS, ABOUT]
    assert all(url in calls for url in value['discovery']['urls'])
    assert value['proof']['version'] == 2
    assert value['proof']['purpose'] == 'attribution'
    assert value['proof']['request_sha256'] != cross._request(URL, QUERY, ())['request_sha256']
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value


def test_same_host_index_gateway_leads_to_actual_authors_anchor():
    index = 'https://docs.example.org/index.html'
    pages = {URL: '<a href="/news">News</a><a href="index.html">index</a>'
                  '<p>See AUTHORS for details.</p>',
             index: '<a href="/authors">Authors and maintainers</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'}
    value, calls = discover(pages)
    assert calls == [URL, index, AUTHORS]
    assert value['discovery']['urls'] == [AUTHORS]
    assert value['proof']['chosen'][0]['parent'] == 1
    assert value['proof']['chosen'][0]['depth'] == 2
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value


def test_unrelated_and_cross_host_gateways_are_not_followed():
    pages = {URL: '<a href="/news">News</a>'
                  '<a href="https://other.example.net/index.html">Documentation index</a>'}
    value, calls = discover(pages)
    assert calls == [URL] and value['discovery'] is None
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value


def test_no_format_requirement_exclusions_and_failed_fetch_retained():
    pages = {URL: '<a href="/authors">Authors</a><a href="/about">About us</a>',
             AUTHORS: OSError('unavailable'),
             ABOUT: '<h1>About us</h1><p>Copyright Example Foundation.</p>'}
    value, calls = discover(pages)
    assert calls == [URL, AUTHORS, ABOUT]
    assert value['proof']['pages'][1]['status'] == 'unavailable'
    assert value['discovery']['urls'] == [ABOUT]
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value
    value, calls = discover(pages, excluded=(AUTHORS,))
    assert calls == [URL, ABOUT]
    assert cross.validate_crosssite_result(value, URL, QUERY, (AUTHORS,),
                                            purpose='attribution') == value


def test_four_fetches_three_edges_and_no_synthesized_urls():
    a = 'https://docs.example.org/authors/a'
    b = 'https://docs.example.org/authors/b'
    c = 'https://docs.example.org/authors/c'
    d = 'https://docs.example.org/authors/d'
    pages = {URL: '<a href="/authors/a">Authors</a>',
             a: '<a href="/authors/b">Authors</a>',
             b: '<a href="/authors/c">Authors</a>',
             c: '<a href="/authors/d">Authors</a>'}
    value, calls = discover(pages)
    assert calls == [URL, a, b, c]
    assert max(p['depth'] for p in value['proof']['pages']) == 3
    assert value['discovery'] is None
    assert d not in calls
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value


def test_wrong_purpose_and_tampered_proof_rejected():
    value, _ = discover({URL: '<a href="/authors">Authors</a>',
                         AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'})
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.validate_crosssite_result(value, URL, QUERY)
    for change in ('purpose', 'version', 'chosen', 'content', 'request_sha256'):
        tampered = copy.deepcopy(value)
        if change == 'purpose': tampered['proof']['purpose'] = 'format'
        elif change == 'version': tampered['proof']['version'] = 1
        elif change == 'chosen': tampered['proof']['chosen'][0]['url'] = ABOUT
        elif change == 'content': tampered['proof']['pages'][1]['source_sha256'] = '0' * 64
        else: tampered['proof']['request_sha256'] = '0' * 64
        with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
            cross.validate_crosssite_result(tampered, URL, QUERY, purpose='attribution')
    legacy = cross._discover(cross._request(URL, QUERY, ()),
                             lambda _url: ('text/html', b'<p>plain</p>'), AT)
    assert legacy['proof']['version'] == 1 and 'purpose' not in legacy['proof']
    assert cross.validate_crosssite_result(legacy, URL, QUERY) == legacy
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.validate_crosssite_result(legacy, URL, QUERY, purpose='attribution')


def test_owned_worker_accepts_bound_attribution_request(monkeypatch):
    from agent_service import browser
    pages = {URL: '<a href="/authors">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'}
    output = io.BytesIO()
    monkeypatch.setattr(cross.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'1')))
    monkeypatch.setattr(cross.sys, 'stdout', io.TextIOWrapper(output, write_through=True))
    monkeypatch.setattr(cross.sys, 'argv', ['worker', json.dumps(
        cross._request(URL, QUERY, (), purpose='attribution'))])
    monkeypatch.setattr(browser, 'fetch_source',
                        lambda url: ('text/html', pages[url].encode()))
    assert cross.main() == 0
    value = json.loads(output.getvalue())
    assert cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution') == value


def test_process_forwards_purpose_and_rejects_wrong_worker_proof(monkeypatch):
    from agent_service import process
    pages = {URL: '<a href="/authors">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'}
    value, _ = discover(pages)
    commands = []
    def child(command, **_kwargs):
        commands.append(json.loads(command[-1]))
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert cross.CrossSiteDiscoveryProcess().discover(
        URL, QUERY, purpose='attribution') == value
    assert commands[0] == cross._request(URL, QUERY, (), purpose='attribution')
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.CrossSiteDiscoveryProcess().discover(URL, QUERY)
