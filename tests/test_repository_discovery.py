"""Offline repository-scoped discovery and version-three proof replay."""
import copy
import json

import pytest

from agent_service import comparison_crosssite_discovery as cross
from agent_service.computer import ComputerError


URL = 'https://github.com/acme/sheets'
QUERY = 'site:github.com authors maintainers copyright publisher'
AT = '2026-10-23T12:00:00+00:00'
AUTHORS = 'https://github.com/acme/sheets/blob/main/AUTHORS.rst'
GATEWAY = 'https://github.com/acme/sheets/tree/main/docs'


def discover(pages, *, source=URL, excluded=()):
    calls = []
    def fetch(url):
        calls.append(url)
        content = pages[url]
        return 'text/html', content.encode()
    request = cross._request(source, QUERY, excluded, purpose='repository_attribution')
    return cross._discover(request, fetch, AT), calls


def test_same_repository_authors_selected_cross_repository_ignored_before_fetch():
    pages = {URL: '<a href="https://github.com/other/sheets/blob/main/AUTHORS.rst">Authors</a>'
                  '<a href="https://github.com/acme/other/blob/main/AUTHORS.rst">Authors</a>'
                  '<a href="/acme/sheets/blob/main/AUTHORS.rst">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'}
    value, calls = discover(pages)
    assert calls == [URL, AUTHORS]
    assert value['discovery']['urls'] == [AUTHORS]
    assert value['proof']['version'] == 3
    assert value['proof']['purpose'] == 'repository_attribution'
    assert cross.validate_crosssite_result(value, URL, QUERY,
                                            purpose='repository_attribution') == value


def test_cross_repository_authors_does_not_hide_same_repository_gateway():
    pages = {URL: '<a href="/other/sheets/blob/main/AUTHORS.rst">Authors</a>'
                  '<a href="/acme/sheets/tree/main/docs">Documentation</a>',
             GATEWAY: '<a href="/acme/sheets/blob/main/AUTHORS.rst">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Published by Acme.</p>'}
    value, calls = discover(pages)
    assert calls == [URL, GATEWAY, AUTHORS]
    assert value['discovery']['urls'] == [AUTHORS]
    assert cross.validate_crosssite_result(value, URL, QUERY,
                                            purpose='repository_attribution') == value


@pytest.mark.parametrize('href', [
    'https://github.com/other/sheets/blob/main/AUTHORS.rst',
    'https://github.com/acme/other/blob/main/AUTHORS.rst',
    'https://github.com/acme/sheets/issues/1',
    'https://github.com/acme/sheets/blob/main/AUTHORS.rst?tab=readme',
    'https://github.com/acme/sheets/blob/main/AUTHORS.rst#x',
    'https://github.com:443/acme/sheets/blob/main/AUTHORS.rst',
    'https://user:pass@github.com/acme/sheets/blob/main/AUTHORS.rst',
    'https://github.com/acme/sheets/blob/main%2FAUTHORS.rst',
    'https://github.com/acme/sheets/blob/%2e%2e/AUTHORS.rst',
    'https://github.com/acme//sheets/blob/main/AUTHORS.rst',
    'https://github.com.evil/acme/sheets/blob/main/AUTHORS.rst',
    'https://gitlab.com/acme/sheets/-/blob/main/AUTHORS.rst',
    'https://bitbucket.org/acme/sheets/src/main/AUTHORS.rst',
])
def test_unsafe_or_other_repository_anchors_never_fetched(href):
    value, calls = discover({URL: f'<a href="{href}">Authors</a>'})
    assert calls == [URL] and value['discovery'] is None
    assert cross.validate_crosssite_result(value, URL, QUERY,
                                            purpose='repository_attribution') == value


@pytest.mark.parametrize('source', [
    'https://github.com/acme',
    'https://github.com/acme/sheets/issues/1',
    'https://github.com/acme/sheets?tab=readme',
    'https://github.com:443/acme/sheets',
    'https://github.com/acme%2Fsheets',
    'https://gitlab.com/acme/sheets',
    'https://bitbucket.org/acme/sheets',
])
def test_unknown_repository_scope_rejected_before_fetch(source):
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross._request(source, QUERY, (), purpose='repository_attribution')


def test_v3_purpose_and_proof_tampering_rejected():
    value, _ = discover({URL: '<a href="/acme/sheets/blob/main/AUTHORS.rst">Authors</a>',
                         AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'})
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        cross.validate_crosssite_result(value, URL, QUERY, purpose='attribution')
    for change in ('version', 'purpose', 'hash', 'page', 'selected'):
        changed = copy.deepcopy(value)
        if change == 'version': changed['proof']['version'] = 2
        elif change == 'purpose': changed['proof']['purpose'] = 'attribution'
        elif change == 'hash': changed['proof']['request_sha256'] = '0' * 64
        elif change == 'page': changed['proof']['pages'][1]['url'] = 'https://github.com/other/sheets'
        else: changed['proof']['chosen'][0]['url'] = 'https://github.com/other/sheets'
        with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
            cross.validate_crosssite_result(changed, URL, QUERY,
                                            purpose='repository_attribution')


def test_v1_v2_proofs_keep_their_versions_and_requests():
    pages = {URL: '<a href="/acme/sheets/blob/main/AUTHORS.rst">Authors</a>',
             AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'}
    def fetch(url):
        return 'text/html', pages[url].encode()
    old = cross._discover(cross._request(URL, QUERY, ()), fetch, AT)
    prior = cross._discover(cross._request(URL, QUERY, (), purpose='attribution'), fetch, AT)
    assert old['proof']['version'] == 1 and 'purpose' not in old['proof']
    assert prior['proof']['version'] == 2 and prior['proof']['purpose'] == 'attribution'
    assert cross.validate_crosssite_result(old, URL, QUERY) == old
    assert cross.validate_crosssite_result(prior, URL, QUERY, purpose='attribution') == prior


def test_owned_process_binds_v3_request(monkeypatch):
    from agent_service import process
    value, _ = discover({URL: '<a href="/acme/sheets/blob/main/AUTHORS.rst">Authors</a>',
                         AUTHORS: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'})
    requests = []
    def child(command, **_kwargs):
        requests.append(json.loads(command[-1]))
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert cross.CrossSiteDiscoveryProcess().discover(
        URL, QUERY, purpose='repository_attribution') == value
    assert requests == [cross._request(URL, QUERY, (), purpose='repository_attribution')]
