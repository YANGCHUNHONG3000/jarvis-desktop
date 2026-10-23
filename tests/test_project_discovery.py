"""Offline actual-anchor navigation for versioned hosted-project discovery."""
import copy
from hashlib import sha256
import io
import json

import pytest

from agent_service import comparison_crosssite_discovery as cross
from agent_service.computer import ComputerError


AT = '2026-10-23T12:00:00+00:00'
PURPOSE = 'project_attribution'
INVALID = 'supplement_crosssite_proof_invalid'


def discover(source, pages, *, excluded=(), purpose=PURPOSE):
    query = 'project authors and maintainers'
    calls = []
    def fetch(url):
        calls.append(url)
        assert url in pages, 'unexpected URL: ' + url
        content = pages[url]
        if isinstance(content, Exception):
            raise content
        if isinstance(content, tuple):
            return content
        return 'text/html', content.encode()
    value = cross._discover(cross._request(source, query, excluded, purpose=purpose), fetch, AT)
    assert cross.validate_crosssite_result(value, source, query, excluded,
                                          purpose=purpose) == value
    return value, calls


@pytest.mark.parametrize('source,inside,outside', [
    ('https://gitlab.com/group/sub/project/-/tree/main',
     'https://gitlab.com/group/sub/project/-/blob/main/AUTHORS',
     ['https://gitlab.com/group/sub/other/-/blob/main/AUTHORS',
      'https://gitlab.com/group/other/project/-/blob/main/AUTHORS',
      'https://gitlab.com/group/sub/project/extra/-/blob/main/AUTHORS']),
    ('https://huggingface.co/owner/repo',
     'https://huggingface.co/owner/repo/blob/main/AUTHORS',
     ['https://huggingface.co/datasets/owner/repo/blob/main/AUTHORS',
      'https://huggingface.co/spaces/owner/repo/blob/main/AUTHORS',
      'https://huggingface.co/owner/other/blob/main/AUTHORS']),
    ('https://huggingface.co/datasets/owner/repo',
     'https://huggingface.co/datasets/owner/repo/raw/main/AUTHORS',
     ['https://huggingface.co/owner/repo/blob/main/AUTHORS',
      'https://huggingface.co/spaces/owner/repo/blob/main/AUTHORS']),
    ('https://huggingface.co/spaces/owner/repo',
     'https://huggingface.co/spaces/owner/repo/blob/main/AUTHORS',
     ['https://huggingface.co/owner/repo/blob/main/AUTHORS',
      'https://huggingface.co/datasets/owner/repo/blob/main/AUTHORS']),
    ('https://codeberg.org/owner/repo',
     'https://codeberg.org/owner/repo/src/branch/main/AUTHORS',
     ['https://codeberg.org/owner/other/src/branch/main/AUTHORS',
      'https://codeberg.org/other/repo/src/branch/main/AUTHORS']),
])
def test_only_actual_same_project_links_fetched(source, inside, outside):
    # Invalid siblings precede the accepted anchor, retaining document-order ID.
    body = ''.join(f'<a href="{url}">Authors</a>' for url in outside + [inside])
    value, calls = discover(source, {source: body,
        inside: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'})
    assert calls == [source, inside]
    assert value['discovery']['urls'] == [inside]
    assert value['proof']['version'] == 4
    assert value['proof']['purpose'] == PURPOSE
    assert value['proof']['chosen'][0]['anchor'] == len(outside)


@pytest.mark.parametrize('source', [
    'https://gitlab.com/group/sub/project',
    'https://gitlab.com/group',
    'https://gitlab.com/group/sub/project/-/issues',
    'https://huggingface.co/owner',
    'https://huggingface.co/datasets/owner',
    'https://codeberg.org/owner/repo/issues',
    'https://docs.example.org/authors',
    'https://bitbucket.org/owner/repo',
])
def test_unknown_project_source_rejected_before_worker(source, monkeypatch):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_kw: pytest.fail('unexpected worker'))
    with pytest.raises(ComputerError, match=INVALID):
        cross.CrossSiteDiscoveryProcess().discover(source, 'project authors', purpose=PURPOSE)


@pytest.mark.parametrize('suffix', ['#authors', '?raw=true', '%2fAUTHORS',
                                   '/./AUTHORS', '//AUTHORS'])
def test_raw_resolved_anchor_checked_before_url_normalization(suffix):
    source = 'https://codeberg.org/owner/repo'
    target = source + '/src/branch/main/AUTHORS' + suffix
    value, calls = discover(source, {source: f'<a href="{target}">Authors</a>'})
    assert calls == [source]
    assert value['discovery'] is None


def test_base_element_cannot_move_scope_and_relative_links_use_actual_base():
    source = 'https://gitlab.com/group/sub/project/-/tree/main'
    other = 'https://gitlab.com/group/other/project/-/blob/main/'
    value, calls = discover(source, {source:
        f'<base href="{other}"><a href="AUTHORS">Authors</a>'})
    assert calls == [source] and value['discovery'] is None
    base = 'https://gitlab.com/group/sub/project/-/blob/main/'
    target = base + 'AUTHORS'
    value, calls = discover(source, {source:
        f'<base href="{base}"><a href="AUTHORS">Authors</a>',
        target: '<h1>Authors</h1><p>Maintained by Alice Smith.</p>'})
    assert calls == [source, target]
    assert value['discovery']['urls'] == [target]


@pytest.mark.parametrize('cue', ['README.md', 'CONTRIBUTING.md', 'LICENSE', 'COPYING'])
def test_additional_file_cues_are_v4_navigation_only(cue):
    source = 'https://codeberg.org/owner/repo'
    file_url = source + '/src/branch/main/' + cue
    author_url = source + '/raw/branch/main/AUTHORS'
    pages = {source: f'<a href="{file_url}">{cue}</a>',
        file_url: f'<p>Project information.</p><a href="{author_url}">Authors</a>',
        author_url: ('text/plain', b'Authors\nMaintained by Alice Smith.')}
    value, calls = discover(source, pages)
    assert calls == [source, file_url, author_url]
    assert value['discovery']['urls'] == [author_url]
    assert value['proof']['chosen'][0]['depth'] == 2
    assert value['proof']['chosen'][0]['parent'] == 1


@pytest.mark.parametrize('label', ['Files', 'Files and versions', 'File tree'])
def test_file_tree_gateway_is_followed_only_as_an_actual_link(label):
    source = 'https://huggingface.co/owner/repo'
    gateway = source + '/tree/main'
    authors = source + '/blob/main/AUTHORS'
    value, calls = discover(source, {source: f'<a href="{gateway}">{label}</a>',
        gateway: f'<a href="{authors}">Authors</a>', authors: '<p>Authors Alice Smith</p>'})
    assert calls == [source, gateway, authors]
    assert value['discovery']['urls'] == [authors]
    value, calls = discover(source, {source: '<p>Files and versions: see AUTHORS.</p>'})
    assert calls == [source] and value['discovery'] is None


def test_license_template_remains_a_candidate_hint_not_a_publisher_decision():
    source = 'https://codeberg.org/owner/repo'
    license_url = source + '/src/branch/main/LICENSE'
    value, calls = discover(source, {source: f'<a href="{license_url}">LICENSE</a>',
        license_url: '<p>Copyright (C) Free Software Foundation, Inc. '
                     'Everyone is permitted to copy this license document.</p>'})
    assert calls == [source, license_url]
    assert value['proof']['chosen'] == [{'url': license_url, 'parent': 0,
                                        'anchor': 0, 'depth': 1, 'role': 'attribution'}]
    assert set(value) == {'discovery', 'proof'}


def test_depth_page_candidate_bounds_exclusion_and_unavailable_replayed():
    source = 'https://codeberg.org/owner/repo'
    urls = [source + '/src/branch/main/' + name
            for name in ('README', 'CONTRIBUTING', 'LICENSE', 'AUTHORS')]
    pages = {source: f'<a href="{urls[0]}">README</a>'}
    for previous, target in zip(urls, urls[1:]):
        pages[previous] = f'<a href="{target}">Authors</a>'
    value, calls = discover(source, pages)
    assert calls == [source, *urls[:3]]
    assert value['discovery'] is None
    assert max(page['depth'] for page in value['proof']['pages']) == 3
    pages = {source: ''.join(f'<a href="{u}">Authors</a>' for u in urls),
             **{url: '<p>Authors Alice Smith</p>' for url in urls}}
    value, calls = discover(source, pages)
    assert value['discovery']['urls'] == urls[:2] and calls == [source, *urls[:2]]
    pages[urls[0]] = OSError('unavailable')
    value, calls = discover(source, pages, excluded=(urls[2],))
    assert calls == [source, urls[0], urls[1], urls[3]]
    assert value['proof']['pages'][1]['status'] == 'unavailable'
    assert value['discovery']['urls'] == [urls[1], urls[3]]


@pytest.mark.parametrize('change', ['version', 'purpose', 'source', 'query', 'excluded',
    'request_sha256', 'url', 'parent', 'anchor', 'depth', 'sha', 'html', 'chosen', 'extra'])
def test_proof_v4_binding_and_actual_edges_reject_tampering(change):
    source = 'https://gitlab.com/group/sub/project/-/tree/main'
    target = 'https://gitlab.com/group/sub/project/-/blob/main/AUTHORS'
    value, _ = discover(source, {source: f'<a href="{target}">Authors</a>',
                                target: '<p>Authors Alice Smith</p>'})
    proof = value['proof']
    if change == 'version': proof['version'] = 3
    elif change == 'purpose': proof['purpose'] = 'repository_attribution'
    elif change == 'source': proof['source_url'] = source.replace('/sub/', '/other/')
    elif change == 'query': proof['query'] = 'other authors'
    elif change == 'excluded': proof['excluded_urls'] = [target]
    elif change == 'request_sha256': proof['request_sha256'] = '0' * 64
    elif change == 'url': proof['pages'][1]['url'] = target.replace('/sub/', '/other/')
    elif change == 'parent': proof['pages'][1]['parent'] = True
    elif change == 'anchor': proof['pages'][1]['anchor'] = 1
    elif change == 'depth': proof['pages'][1]['depth'] = 2
    elif change == 'sha': proof['pages'][1]['source_sha256'] = '0' * 64
    elif change == 'html':
        html = f'<a href="{target.replace("/sub/", "/other/")}">Authors</a>'.encode()
        proof['pages'][0].update(html_zlib_base64=cross._pack(html), source_sha256=sha256(html).hexdigest())
    elif change == 'chosen': proof['chosen'][0]['url'] = target.replace('/sub/', '/other/')
    else: proof['pages'][1]['extra'] = 'unbound'
    with pytest.raises(ComputerError, match=INVALID):
        cross.validate_crosssite_result(value, source, 'project authors and maintainers', purpose=PURPOSE)


def test_owned_worker_and_process_forward_project_purpose(monkeypatch):
    from agent_service import browser, process
    source = 'https://codeberg.org/owner/repo'
    target = source + '/src/branch/main/AUTHORS'
    pages = {source: f'<a href="{target}">Authors</a>', target: '<p>Authors Alice Smith</p>'}
    query = 'project authors and maintainers'
    request = cross._request(source, query, (), purpose=PURPOSE)
    output = io.BytesIO()
    monkeypatch.setattr(cross.sys, 'stdin', io.TextIOWrapper(io.BytesIO(b'1')))
    monkeypatch.setattr(cross.sys, 'stdout', io.TextIOWrapper(output, write_through=True))
    monkeypatch.setattr(cross.sys, 'argv', ['worker', json.dumps(request)])
    monkeypatch.setattr(browser, 'fetch_source', lambda url: ('text/html', pages[url].encode()))
    assert cross.main() == 0
    value = json.loads(output.getvalue())
    commands = []
    def child(command, **_kwargs):
        commands.append(json.loads(command[-1]))
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert cross.CrossSiteDiscoveryProcess().discover(source, query, purpose=PURPOSE) == value
    assert commands == [request]
    with pytest.raises(ComputerError, match=INVALID):
        cross.CrossSiteDiscoveryProcess().discover(source, query, purpose='attribution')


def test_proof_v3_exact_prechange_output_unchanged():
    source = 'https://github.com/example/repo'
    query = 'site:github.com/example/repo authors maintainer'
    pages = {source:
        '<a href="/example/other/blob/main/AUTHORS">Authors</a>'
        '<a href="/example/repo/blob/main/README.md">README</a>'
        '<a href="/example/repo/tree/main/docs">Documentation</a>'
        '<a href="/example/repo/blob/main/AUTHORS">Authors</a>',
        source + '/blob/main/AUTHORS': '<h1>Authors</h1><p>Maintained by Alice Smith.</p>',
        source + '/tree/main/docs': '<a href="/example/repo/blob/main/LICENSE">LICENSE</a>',
        source + '/blob/main/README.md': '<p>About Example</p>'}
    value = cross._discover(cross._request(source, query, (), purpose='repository_attribution'),
        lambda target: ('text/html', pages[target].encode()), AT)
    # Captured before adding proof v4; includes exact request hash and packed bytes.
    assert sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == (
        '7774aae7a397e27ef0147c93f0260956ebecb5d415c58b632a1944ef556b054f')
    assert cross.validate_crosssite_result(value, source, query,
                                          purpose='repository_attribution') == value
    with pytest.raises(ComputerError, match=INVALID):
        cross.validate_crosssite_result(copy.deepcopy(value), source, query, purpose=PURPOSE)


@pytest.mark.parametrize('purpose', [None, 'attribution', 'repository_attribution'])
@pytest.mark.parametrize('cue', ['README.md', 'CONTRIBUTING.md', 'LICENSE', 'COPYING'])
def test_new_file_cues_do_not_change_legacy_discovery(purpose, cue):
    source = 'https://github.com/example/repo'
    target = source + '/blob/main/' + cue
    value, calls = discover(source, {source: f'<a href="{target}">{cue}</a>'}, purpose=purpose)
    assert calls == [source] and value['discovery'] is None
    assert value['proof']['version'] == {None: 1, 'attribution': 2, 'repository_attribution': 3}[purpose]
