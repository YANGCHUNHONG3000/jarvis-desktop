"""Offline contracts for opt-in hosted document policy; no network or model."""
from copy import deepcopy

import pytest

from agent_service.hosted_document_policy import (
    POLICY, candidates, enabled, family, role, scope, transfer_scope,
)


def source(text, url='https://openpyxl.readthedocs.io/en/stable/index.html'):
    return {'url': url, 'text': text}


def test_policy_is_explicit_and_leaves_legacy_goals_disabled():
    assert POLICY == 'project_attribution_v6'
    assert enabled({'comparison': {'publisher_policy': POLICY}})
    for value in [None, [], {}, {'comparison': None}, *[
            {'comparison': {'publisher_policy': f'project_attribution_v{i}'}}
            for i in range(1, 6)]]:
        assert not enabled(value)


@pytest.mark.parametrize('path', ['', '/', '/en/stable/', '/en/stable/index.html'])
def test_host_is_case_normalized_and_projects_stay_separate(path):
    assert family('https://OpenPyXL.ReadTheDocs.io' + path) == 'openpyxl.readthedocs.io'
    assert scope('https://OpenPyXL.ReadTheDocs.io' + path) == (
        'readthedocs', 'openpyxl.readthedocs.io')
    assert family('https://xlsxwriter.readthedocs.io' + path) != family(
        'https://openpyxl.readthedocs.io' + path)


@pytest.mark.parametrize('url', [None, 3, '', 'http://openpyxl.readthedocs.io/',
    'ftp://openpyxl.readthedocs.io/', 'https://readthedocs.io/',
    'https://a.b.readthedocs.io/',
    'https://openpyxl.readthedocs.io.evil.test/', 'https://openpyxl.readthedocs.io./',
    'https://user@openpyxl.readthedocs.io/', 'https://openpyxl.readthedocs.io:443/',
    'https://openpyxl.readthedocs.io:/', 'https://openpyxl%2ereadthedocs.io/',
    'https://openpyxl.readthedocs.io/%2e%2e/', 'https://openpyxl.readthedocs.io/a\\b',
    'https://openpyxl.readthedocs.io//projects/alias/',
    'https://openpyxl.readthedocs.io/a/../projects/alias/',
    'https://openpyxl.readthedocs.io/?project=other',
    'https://openpyxl.readthedocs.io/#Author', ' https://openpyxl.readthedocs.io/',
    'https://openpyxl.readthedocs.io/\n', 'https://openpyxl.readthedocs.io/é',
    'https://-project.readthedocs.io/', 'https://project-.readthedocs.io/',
    'https://project_name.readthedocs.io/', 'https://example.com/'] )
def test_ambiguous_or_unsupported_urls_never_gain_scope(url):
    assert family(url) is None
    assert scope(url) is None


def test_subprojects_have_distinct_transfer_scopes_and_one_conservative_family():
    base = 'https://main-project.readthedocs.io'
    assert scope(base + '/projects/one/en/stable/') == (
        'readthedocs', 'main-project.readthedocs.io', 'one')
    assert scope(base + '/projects/two/en/stable/') != scope(base + '/projects/one/')
    assert scope(base + '/') != scope(base + '/projects/one/')
    assert family(base + '/projects/one/') == family(base + '/projects/two/')
    for path in ['/projects', '/projects/', '/projects//', '/projects/UPPER/',
                 '/projects/a.b/', '/Projects/one/', '/projects/-one/']:
        assert scope(base + path) is None


@pytest.mark.parametrize('url', [
    'https://readthedocs.io/', 'https://a.b.readthedocs.io/',
    'https://user@project.readthedocs.io/', 'https://project.readthedocs.io:443/',
    'https://project.readthedocs.io/projects/',
    'https://project.readthedocs.io/projects/a.b/',
    'https://project.readthedocs.io/%2e/',
    'https://project.readthedocs.io.evil.test/',
])
def test_bad_hosted_routes_never_fall_back_to_legacy_host_scope(url):
    assert transfer_scope(url) is None


def test_transfer_scope_preserves_other_platforms():
    from agent_service.project_scope import transfer_scope as old
    for url in ['https://github.com/Owner/Repo/blob/main/AUTHORS',
                'https://example.com/docs', None, 3]:
        assert transfer_scope(url) == old(url)
    url = 'https://x.readthedocs.io/projects/alias/en/stable/'
    assert transfer_scope(url) == scope(url)


@pytest.mark.parametrize('field', ['Author\n:\nEric Gazoni, Charlie Clark',
    'Authors: Eric Gazoni, Charlie Clark', 'Author(s):\nEric Gazoni, Charlie Clark',
    'Author\r\n:\r\nEric Gazoni, Charlie Clark'])
def test_complete_literal_author_fields_offer_each_member_without_a_verdict(field):
    data = source('Documentation\n' + field + '\nSource code\n:\nhttps://example.com')
    original = deepcopy(data)
    expected = [{'name': name, 'quote': field, 'role': 'project_author'}
                for name in ['Eric Gazoni', 'Charlie Clark']]
    assert candidates(data) == expected
    for item in expected:
        assert role(data, {key: item[key] for key in ('name', 'quote')}) == 'project_author'
    assert data == original


def test_one_author_and_exact_role_binding():
    data = source('Author: Alice Smith\nSource code\nhttps://example.com')
    item = candidates(data)[0]
    assert item == {'name': 'Alice Smith', 'quote': 'Author: Alice Smith',
                    'role': 'project_author'}
    assert role(data, {'name': 'Alice Smith', 'quote': 'Alice Smith'}) is None
    assert role(data, {'name': 'Alice', 'quote': item['quote']}) is None
    assert role(data, item) is None
    assert role(data, None) is None


@pytest.mark.parametrize('text', [
    'Author', 'Author\nAlice Smith', 'Author:\n',
    'A book Author: Alice Smith', 'The Author: Alice Smith',
    'Not Author: Alice Smith', 'Author: Not Alice Smith',
    'Author: Alice Smith,', 'Author: Alice Smith and Bob Jones',
    'Author: Alice Smith, Bob Jones, Carol King',
    'Author: Alice Smith, Alice Smith', 'Author: Alice Smith; Bob Jones',
    'Author: Alice Smith wrote this page', 'Author: Alice Smith... ',
    'Author: Read the Docs', 'Author: Sphinx', 'Author: GitHub',
    'Author: Microsoft', 'Author: Python', 'Author: @readthedocs',
    'Author: openpyxl', 'Author: Openpyxl',
    'Author: Alice Smith\nBob Jones', 'Author: Alice Smith\nand Bob Jones',
    'Author: Alice Smith\nAuthor: Bob Jones',
    'Author: Alice Smith\nAuthor: Alice Smith',
    'Author: Alice Smith\nAUTHOR: Bob Jones',
    'Author: ' + 'A' * 81,
])
def test_incomplete_platform_prose_or_ambiguous_fields_are_not_suggestions(text):
    assert candidates(source(text)) == []


@pytest.mark.parametrize('value', [None, {}, {'url': None, 'text': 'Author: Alice Smith'},
    {'url': 'https://example.com', 'text': 'Author: Alice Smith'},
    {'url': 'https://x.readthedocs.io', 'text': None}])
def test_candidates_reject_invalid_sources(value):
    assert candidates(value) == []
