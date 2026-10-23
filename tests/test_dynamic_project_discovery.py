"""Proof v5 offline integration: actual declared links and project boundaries."""
import copy
import json
import random
import string

import pytest

from agent_service import comparison_crosssite_discovery as cross
from agent_service import project_discovery as dynamic
from agent_service.computer import ComputerError
from agent_service.project_documents import PURPOSE
from test_dynamic_project_documents import AT, gitlab_fixture, offline_fetch, prohibit_network


def discover(fixture, *, excluded=()):
    query = 'site:gitlab.com/group/subgroup/project project authors'
    request = cross._request(fixture['tree'], query, excluded, purpose=PURPOSE)
    calls = []
    value = dynamic.discover(request, offline_fetch(fixture['pages'], calls), AT)
    return value, calls, request


def test_gitlab_tree_to_authors_blob_and_declared_raw_url_replays_without_network():
    fixture = gitlab_fixture()
    value, calls, request = discover(fixture)
    assert calls == [fixture['tree'], fixture['logs'], fixture['blob'], fixture['endpoint'], fixture['raw']]
    assert value['discovery']['urls'] == [fixture['blob'], fixture['raw']]
    assert value['proof']['version'] == 5
    assert len(value['proof']['pages']) == 3
    assert [row['url'] for row in value['proof']['resources']] == calls
    assert dynamic.validate(value, request) == value
    assert cross.validate_crosssite_result(value, fixture['tree'], request['query'], purpose=PURPOSE) == value


def test_excluded_blob_is_neither_fetched_nor_selected():
    fixture = gitlab_fixture()
    value, calls, request = discover(fixture, excluded=[fixture['blob']])
    assert calls == [fixture['tree'], fixture['logs']]
    assert value['discovery'] is None
    assert dynamic.validate(value, request) == value


def test_declared_raw_link_to_other_project_is_not_fetched_or_selected():
    fixture = gitlab_fixture()
    mime, payload = fixture['pages'][fixture['endpoint']]
    fixture['pages'][fixture['endpoint']] = mime, payload.replace(
        b'/group/subgroup/project/-/raw/', b'/group/subgroup/other/-/raw/')
    value, calls, request = discover(fixture)
    assert value['discovery']['urls'] == [fixture['blob']]
    assert calls == [fixture['tree'], fixture['logs'], fixture['blob'], fixture['endpoint']]
    assert dynamic.validate(value, request) == value


@pytest.mark.parametrize('root,allowed,foreign', [
    ('https://huggingface.co/team/project',
     'https://huggingface.co/team/project/blob/main/AUTHORS',
     'https://huggingface.co/datasets/team/project/blob/main/AUTHORS'),
    ('https://huggingface.co/datasets/team/project',
     'https://huggingface.co/datasets/team/project/blob/main/AUTHORS',
     'https://huggingface.co/team/project/blob/main/AUTHORS'),
])
def test_huggingface_model_and_dataset_authorship_cannot_cross(root, allowed, foreign):
    pages = {
        root: ('text/html', f'<a href="{foreign}">Authors</a><a href="{allowed}">Authors</a>'.encode()),
        allowed: ('text/plain', b'Project authors: Alice Example.'),
    }
    request = cross._request(root, 'site:huggingface.co project authors', [], purpose=PURPOSE)
    calls = []
    value = dynamic.discover(request, offline_fetch(pages, calls), AT)
    assert calls == [root, allowed]
    assert value['discovery']['urls'] == [allowed]
    assert dynamic.validate(value, request) == value


@pytest.mark.parametrize('change', ['resource-body', 'resource-hash', 'resource-order',
    'resource-url', 'resource-extra-field', 'resource-transport', 'extra-resource',
    'missing-resource', 'page-body', 'page-hash', 'page-extra-field', 'chosen',
    'discovery', 'request', 'proof-extra-field', 'version'])
def test_proof5_binds_resources_rendered_pages_and_result_exactly(change):
    fixture = gitlab_fixture()
    value, _, request = discover(fixture)
    proof = value['proof']
    if change == 'resource-body': proof['resources'][0]['body'] = cross._pack(b'<p>changed</p>')
    elif change == 'resource-hash': proof['resources'][0]['sha256'] = '0' * 64
    elif change == 'resource-order': proof['resources'].reverse()
    elif change == 'resource-url': proof['resources'][0]['url'] = fixture['raw']
    elif change == 'resource-extra-field': proof['resources'][0]['unbound'] = 'extra'
    elif change == 'resource-transport': proof['resources'][0]['transport'] = 'unknown'
    elif change == 'extra-resource': proof['resources'].append(copy.deepcopy(proof['resources'][-1]))
    elif change == 'missing-resource': proof['resources'].pop()
    elif change == 'page-body': proof['pages'][0]['html_zlib_base64'] = cross._pack(b'<p>changed</p>')
    elif change == 'page-hash': proof['pages'][0]['source_sha256'] = '0' * 64
    elif change == 'page-extra-field': proof['pages'][0]['unbound'] = True
    elif change == 'chosen': proof['chosen'][0]['anchor'] = 99
    elif change == 'discovery': value['discovery'] = None
    elif change == 'request': proof['query'] = 'other project authors'
    elif change == 'proof-extra-field': proof['unbound'] = True
    else: proof['version'] = True
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        dynamic.validate(value, request)


def test_retained_failed_resource_is_replayed_without_retry():
    fixture = gitlab_fixture()
    fixture['pages'][fixture['endpoint']] = TimeoutError('offline failure')
    value, calls, request = discover(fixture)
    assert calls == [fixture['tree'], fixture['logs'], fixture['blob'], fixture['endpoint']]
    assert value['discovery'] is None
    assert value['proof']['resources'][-1] == {'url': fixture['endpoint'], 'status': 'unavailable'}
    assert dynamic.validate(value, request) == value


def test_compact_proof_stores_http_bodies_once_and_retains_rendered_page_hashes():
    fixture = gitlab_fixture()
    value, _, request = discover(fixture)
    proof = value['proof']
    assert proof['representation'] == 'resource-replay-v1'
    assert all('html_zlib_base64' not in page for page in proof['pages'])
    assert all(len(page['source_sha256']) == 64 for page in proof['pages'])
    assert len(proof['resources']) == len(fixture['pages'])
    for resource in proof['resources']:
        assert cross._unpack(resource['body']) == fixture['pages'][resource['url']][1]
    assert dynamic.validate(value, request) == value


def test_existing_unmarked_full_pages_proof5_remains_exactly_replayable():
    fixture = gitlab_fixture()
    _, _, request = discover(fixture)
    value = dynamic.discover(request, offline_fetch(fixture['pages'], []), AT, compact=False)
    original = copy.deepcopy(value)
    assert 'representation' not in value['proof']
    assert all('html_zlib_base64' in page for page in value['proof']['pages'])
    assert dynamic.validate(value, request) == original
    assert value == original
    assert cross.validate_crosssite_result(value, fixture['tree'], request['query'], purpose=PURPOSE) == original


@pytest.mark.parametrize('change', ['unknown', 'null', 'boolean', 'missing', 'add-body', 'mark-full'])
def test_compact_representation_cannot_be_changed_or_mixed_with_full_pages(change):
    fixture = gitlab_fixture()
    value, _, request = discover(fixture)
    proof = value['proof']
    if change == 'unknown': proof['representation'] = 'resource-replay-v2'
    elif change == 'null': proof['representation'] = None
    elif change == 'boolean': proof['representation'] = True
    elif change == 'missing': del proof['representation']
    elif change == 'add-body': proof['pages'][0]['html_zlib_base64'] = cross._pack(b'forged')
    else:
        value = dynamic.discover(request, offline_fetch(fixture['pages'], []), AT, compact=False)
        value['proof']['representation'] = 'resource-replay-v1'
    with pytest.raises(ComputerError, match='supplement_crosssite_proof_invalid'):
        dynamic.validate(value, request)


def test_three_large_resources_fit_compact_budget_without_duplicate_page_bodies():
    root = 'https://huggingface.co/team/project'
    first = root + '/blob/main/AUTHORS'
    second = root + '/blob/main/MAINTAINERS'
    rng = random.Random(20261023)

    def padding():
        return ''.join(rng.choices(string.ascii_letters + string.digits, k=60_000))

    pages = {
        root: ('text/html', (f'<a href="{first}">Authors</a><a href="{second}">Maintainers</a><!--' + padding() + '-->').encode()),
        first: ('text/html', ('<p>Project authors: Alice Example.</p><!--' + padding() + '-->').encode()),
        second: ('text/html', ('<p>Project maintainers: Bob Example.</p><!--' + padding() + '-->').encode()),
    }
    request = cross._request(root, 'site:huggingface.co project authors', [], purpose=PURPOSE)
    calls = []
    value = dynamic.discover(request, offline_fetch(pages, calls), AT)
    assert calls == [root, first, second]
    assert value['discovery']['urls'] == [first, second]
    assert 150_000 < len(json.dumps(value).encode()) < cross.MAX_RESULT_CHARS
    assert all('html_zlib_base64' not in page for page in value['proof']['pages'])
    assert dynamic.validate(value, request) == value
    with pytest.raises(ComputerError, match='supplement_crosssite_unavailable'):
        dynamic.discover(request, offline_fetch(pages, []), AT, compact=False)
