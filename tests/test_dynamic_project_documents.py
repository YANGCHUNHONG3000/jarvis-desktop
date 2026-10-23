"""Offline declared-resource reconstruction and exact project read replay."""
import copy
from html import escape
import json

import pytest

from agent_service import project_documents as docs
from agent_service import project_reader as reader
from agent_service.browser import SourceError
from agent_service.contracts import BrowserReadError


AT = '2026-10-23T12:00:00+00:00'
PROJECT = 'group/subgroup/project'


def gitlab_fixture(project=PROJECT):
    prefix = 'https://gitlab.com/' + project + '/-/'
    tree = prefix + 'tree/main'
    blob = prefix + 'blob/main/AUTHORS'
    raw = prefix + 'raw/main/AUTHORS'
    logs = prefix + 'refs/main/logs_tree/?format=json&offset=0&ref_type=HEADS'
    endpoint = blob + '?format=json&viewer=simple'
    template = '/' + project + '/-/blob/main'

    def shell(route, startup=None):
        metadata = ' data-blob-path="AUTHORS"' if route == 'blob' else ''
        script = ('<script>window.mustNeverRun(); gl.startup_calls = ' +
                  json.dumps(startup) + ';</script>') if startup is not None else ''
        return ('<html><head><title>Project files</title></head>'
                f'<body data-project-full-path="{project}">'
                f'<div data-command-palette="{escape(json.dumps({"project_blob_url": template}), quote=True)}"></div>'
                f'<div data-project-path="{project}" data-ref="main"{metadata}></div>'
                + script + '</body></html>').encode()

    pages = {
        tree: ('text/html', shell('tree', {logs.removeprefix('https://gitlab.com'): None})),
        logs: ('application/json', json.dumps([{'file_name': 'AUTHORS'}]).encode()),
        blob: ('text/html', shell('blob')),
        endpoint: ('application/json', json.dumps({
            'path': 'AUTHORS', 'binary': False,
            'html': '<p>Project authors: Alice Example and Bob Example.</p>',
            'raw_path': raw.removeprefix('https://gitlab.com'),
        }).encode()),
        raw: ('text/plain', b'Project authors: Alice Example and Bob Example.'),
    }
    return {'tree': tree, 'blob': blob, 'raw': raw, 'logs': logs,
            'endpoint': endpoint, 'pages': pages}


def offline_fetch(pages, calls):
    def fetch(url):
        calls.append(url)
        assert url in pages, 'Unexpected GET: ' + url
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        return (*value, 'direct-pinned-v1')
    return fetch


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *_a, **_k: pytest.fail('Network forbidden'))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_a, **_k: pytest.fail('DNS forbidden'))


def render(fixture, url=None):
    calls = []
    resources = docs.ResourceCapture(offline_fetch(fixture['pages'], calls))
    mime, body = docs.document(url or fixture['tree'], resources)
    return mime, body, resources, calls


@pytest.mark.parametrize('project', ['team/project', PROJECT, 'team/division/subteam/project'])
def test_declared_file_list_uses_exact_same_project_template(project):
    fixture = gitlab_fixture(project)
    mime, content, resources, calls = render(fixture)
    assert mime.startswith('text/html')
    assert fixture['blob'].encode() in content
    assert b'window.mustNeverRun' not in content
    assert calls == [fixture['tree'], fixture['logs']]
    assert [row['url'] for row in resources.resources] == calls
    assert all(row['transport'] == 'direct-pinned-v1' for row in resources.resources)


def test_blob_read_keeps_exact_observation_and_complete_resource_transcript():
    fixture = gitlab_fixture()
    calls = []
    result = reader.capture(fixture['blob'], 'Alice', offline_fetch(fixture['pages'], calls), AT)
    observation = reader.validate_read(result, fixture['blob'], 'Alice')
    assert calls == [fixture['blob'], fixture['endpoint']]
    assert observation.requested_url == observation.final_url == fixture['blob']
    assert observation.captured_at == AT
    assert observation.title == 'Project files'
    assert 'Project authors: Alice Example and Bob Example.' in observation.text
    assert [row['url'] for row in result['document_proof']['resources']] == calls


@pytest.mark.parametrize('change', ['project', 'ref', 'template', 'palette-missing',
    'palette-duplicate', 'startup-missing', 'startup-unknown', 'startup-cross-project',
    'startup-query', 'startup-pagination', 'startup-duplicate-query'])
def test_tree_rejects_ambiguous_or_unrecognized_bootstrap(change):
    fixture = gitlab_fixture()
    mime, body = fixture['pages'][fixture['tree']]
    text = body.decode()
    if change == 'project': text = text.replace('data-project-full-path="' + PROJECT, 'data-project-full-path="other/project')
    elif change == 'ref': text = text.replace('data-ref="main"', 'data-ref="other"')
    elif change == 'template': text = text.replace('/-/blob/main', '/-/blob/feature%2Fbranch')
    elif change == 'palette-missing': text = text.replace('data-command-palette=', 'data-not-palette=')
    elif change == 'palette-duplicate': text = text.replace('</body>', text[text.index('<div data-command-palette'):text.index('</div>') + 6] + '</body>')
    elif change == 'startup-missing': text = text.replace('gl.startup_calls', 'gl.unknown')
    elif change == 'startup-unknown': text = text.replace('/-/refs/main/logs_tree/', '/-/unknown/')
    elif change == 'startup-cross-project': text = text.replace('/' + PROJECT + '/-/refs/', '/other/project/-/refs/')
    elif change == 'startup-query': text = text.replace('ref_type=HEADS', 'ref_type=HEADS&arbitrary=1')
    elif change == 'startup-pagination': text = text.replace('offset=0', 'offset=100')
    else: text = text.replace('offset=0', 'offset=0&offset=0')
    fixture['pages'][fixture['tree']] = mime, text.encode()
    with pytest.raises(SourceError):
        render(fixture)


@pytest.mark.parametrize('change', ['path', 'binary', 'empty-html', 'render-error', 'wrong-mime', 'blob-marker'])
def test_blob_rejects_mismatched_or_nontext_content(change):
    fixture = gitlab_fixture()
    mime, raw = fixture['pages'][fixture['endpoint']]
    value = json.loads(raw)
    if change == 'path': value['path'] = 'other/AUTHORS'
    elif change == 'binary': value['binary'] = True
    elif change == 'empty-html': value['html'] = ' '
    elif change == 'render-error': value['render_error'] = 'failed'
    elif change == 'wrong-mime': mime = 'text/html'
    else:
        smime, shell = fixture['pages'][fixture['blob']]
        fixture['pages'][fixture['blob']] = smime, shell.replace(b'data-blob-path="AUTHORS"', b'data-blob-path="README"')
    fixture['pages'][fixture['endpoint']] = mime, json.dumps(value).encode()
    with pytest.raises(SourceError):
        render(fixture, fixture['blob'])


def test_directory_entries_cannot_smuggle_paths_or_query():
    fixture = gitlab_fixture()
    names = ['AUTHORS', '../AUTHORS', 'nested/AUTHORS', 'AUTHORS?x=1', '.', '..', 'https://evil.test/AUTHORS']
    fixture['pages'][fixture['logs']] = 'application/json', json.dumps([{'file_name': x} for x in names]).encode()
    _, body, _, _ = render(fixture)
    assert body.count(b'<a ') == 1
    assert fixture['blob'].encode() in body


def test_tree_accepts_only_explicit_readme_resource_and_preserves_raw_link():
    fixture = gitlab_fixture()
    mime, shell = fixture['pages'][fixture['tree']]
    readme = fixture['blob'].replace('/AUTHORS', '/README.md') + '?format=json&ref_type=heads&viewer=rich'
    calls = {fixture['logs'].removeprefix('https://gitlab.com'): None,
             readme.removeprefix('https://gitlab.com'): None}
    start = shell.decode().index('gl.startup_calls = ')
    end = shell.decode().index(';</script>', start)
    text = shell.decode()
    fixture['pages'][fixture['tree']] = mime, (text[:start] + 'gl.startup_calls = ' + json.dumps(calls) + text[end:]).encode()
    fixture['pages'][readme] = 'application/json', json.dumps({
        'path': 'README.md', 'binary': False,
        'html': '<p>Read project documentation.</p>', 'raw_path': fixture['raw'],
    }).encode()
    _, body, _, fetched = render(fixture)
    assert fetched == [fixture['tree'], fixture['logs'], readme]
    assert fixture['raw'].encode() in body


@pytest.mark.parametrize('change', ['observation', 'query', 'url', 'sha', 'body', 'order', 'extra-resource', 'extra-field'])
def test_read_proof_rejects_each_changed_binding(change):
    fixture = gitlab_fixture()
    value = reader.capture(fixture['blob'], 'Alice', offline_fetch(fixture['pages'], []), AT)
    proof = value['document_proof']
    if change == 'observation': value['observation']['text'] += ' forged'
    elif change == 'query': proof['query'] = 'Bob'
    elif change == 'url': proof['url'] = fixture['raw']
    elif change == 'sha': proof['resources'][0]['sha256'] = '0' * 64
    elif change == 'body':
        from agent_service.comparison_crosssite_discovery import _pack
        proof['resources'][0]['body'] = _pack(b'<p>changed</p>')
    elif change == 'order': proof['resources'].reverse()
    elif change == 'extra-resource': proof['resources'].append(copy.deepcopy(proof['resources'][-1]))
    else: proof['resources'][0]['unexpected'] = True
    with pytest.raises(BrowserReadError, match='source_rejected'):
        reader.validate_read(value, fixture['blob'], 'Alice')


@pytest.mark.parametrize('transport', ['unknown', 'hf-loopback-connect-v1'])
def test_transport_profile_cannot_be_borrowed_for_gitlab(transport):
    resources = docs.ResourceCapture(lambda url: ('text/plain', b'Authors', transport))
    with pytest.raises(SourceError):
        resources('https://gitlab.com/team/project/-/raw/main/AUTHORS')
    assert resources.resources == [{'url': 'https://gitlab.com/team/project/-/raw/main/AUTHORS', 'status': 'unavailable'}]


def test_capture_resource_count_and_byte_limits():
    capture = docs.ResourceCapture(lambda _: ('text/plain', b'x', 'direct-pinned-v1'))
    for index in range(docs.MAX_RESOURCES): capture('https://codeberg.org/team/project/' + str(index))
    with pytest.raises(SourceError): capture('https://codeberg.org/team/project/extra')
    assert len(capture.resources) == docs.MAX_RESOURCES
    oversized = docs.ResourceCapture(lambda _: ('text/plain', b'x' * (512 * 1024 + 1), 'direct-pinned-v1'))
    with pytest.raises(SourceError): oversized('https://codeberg.org/team/project')
    total = docs.ResourceCapture(lambda _: ('text/plain', b'x' * (512 * 1024), 'direct-pinned-v1'))
    for index in range(4): total('https://codeberg.org/team/project/' + str(index))
    with pytest.raises(SourceError): total('https://codeberg.org/team/project/overflow')


@pytest.mark.parametrize('payload', [b'{}', b'null', b'[{"file_name":"AUTHORS","file_name":"README"}]',
    json.dumps([{'file_name': 'AUTHORS'}] * 101).encode()])
def test_logs_tree_unknown_duplicate_or_oversize_schema_rejected(payload):
    fixture = gitlab_fixture()
    fixture['pages'][fixture['logs']] = 'application/json', payload
    with pytest.raises(SourceError): render(fixture)


def test_blob_template_from_different_project_rejected_before_followup_get():
    fixture = gitlab_fixture()
    mime, shell = fixture['pages'][fixture['tree']]
    fixture['pages'][fixture['tree']] = mime, shell.replace(
        b'/group/subgroup/project/-/blob/main', b'/group/subgroup/other/-/blob/main')
    calls = []
    capture = docs.ResourceCapture(offline_fetch(fixture['pages'], calls))
    with pytest.raises(SourceError): docs.document(fixture['tree'], capture)
    assert calls == [fixture['tree']]


@pytest.mark.parametrize('url,query', [('other-url', 'Alice'), ('same-url', 'Bob')])
def test_read_proof_cannot_be_reused_for_another_requested_url_or_query(url, query):
    fixture = gitlab_fixture()
    value = reader.capture(fixture['blob'], 'Alice', offline_fetch(fixture['pages'], []), AT)
    requested = fixture['raw'] if url == 'other-url' else fixture['blob']
    with pytest.raises(BrowserReadError, match='source_rejected'):
        reader.validate_read(value, requested, query)


def test_raw_link_is_never_guessed_when_blob_json_does_not_declare_it():
    fixture = gitlab_fixture()
    mime, payload = fixture['pages'][fixture['endpoint']]
    value = json.loads(payload)
    del value['raw_path']
    fixture['pages'][fixture['endpoint']] = mime, json.dumps(value).encode()
    _, rendered, _, calls = render(fixture, fixture['blob'])
    assert fixture['raw'].encode() not in rendered
    assert b'<a ' not in rendered
    assert calls == [fixture['blob'], fixture['endpoint']]


@pytest.mark.parametrize('count', [0, docs.MAX_RESOURCES + 1])
def test_replay_resource_count_is_bounded_before_any_get(count):
    with pytest.raises(SourceError): docs.ResourceReplay([{}] * count)
