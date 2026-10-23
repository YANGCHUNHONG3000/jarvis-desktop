"""Offline project boundaries, attribution transfer and immutable policy replay."""
from copy import deepcopy

import pytest

from agent_service.computer import ComputerSession
from agent_service.computer_agent import prepare
from agent_service.project_scope import (
    POLICY, enabled, project_scope, project_site, same_project, transfer_scope,
)
from agent_service.publisher_evidence import POLICIES
from agent_service.publisher_scope import POLICY as REPOSITORY_POLICY, repository_scope
from agent_service.source_comparison import validate_comparison
from test_publisher_scope import scoped_review


PROJECTS = [
    ('https://gitlab.com/Owner/Sub/Project/-/blob/main/AUTHORS',
     'https://gitlab.com/Owner/Sub/Project/-/tree/main/docs',
     ('gitlab', 'Owner', 'Sub', 'Project'), 'gitlab.com/Owner/Sub/Project'),
    ('https://huggingface.co/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/Owner/Project',
     ('huggingface', 'model', 'Owner', 'Project'), 'huggingface.co/Owner/Project'),
    ('https://huggingface.co/datasets/Owner/Project/raw/main/AUTHORS',
     'https://huggingface.co/datasets/Owner/Project/tree/main',
     ('huggingface', 'dataset', 'Owner', 'Project'),
     'huggingface.co/datasets/Owner/Project'),
    ('https://huggingface.co/spaces/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/spaces/Owner/Project/commits/main',
     ('huggingface', 'space', 'Owner', 'Project'),
     'huggingface.co/spaces/Owner/Project'),
    ('https://codeberg.org/Owner/Project/src/branch/main/AUTHORS',
     'https://codeberg.org/Owner/Project/raw/tag/v1/README.md',
     ('codeberg', 'Owner', 'Project'), 'codeberg.org/Owner/Project'),
]


@pytest.mark.parametrize('left,right,scope,site', PROJECTS)
def test_explicit_projects_preserve_path_case_and_search_restriction(left, right, scope, site):
    assert project_scope(left) == scope
    assert project_scope(right) == scope
    assert transfer_scope(left) == scope
    assert project_site(left) == site
    assert same_project(left, right)
    assert not same_project(left, right.replace('/Owner/', '/owner/'))
    assert not same_project(left, right.replace('/Project', '/project'))


@pytest.mark.parametrize('route', ['blob/main/AUTHORS', 'raw/main/AUTHORS',
                                  'tree/main', 'commits/main'])
@pytest.mark.parametrize('namespace', ['Owner/Project', 'Owner/Sub/Project',
                                      'Owner/Sub/Nested/Project'])
def test_gitlab_content_routes_bind_complete_namespace(namespace, route):
    expected = ('gitlab', *namespace.split('/'))
    assert project_scope(f'https://gitlab.com/{namespace}/-/{route}') == expected


@pytest.mark.parametrize('prefix,kind', [('', 'model'), ('datasets/', 'dataset'),
                                       ('spaces/', 'space')])
@pytest.mark.parametrize('suffix', ['', '/', '/blob/main/AUTHORS', '/raw/main/AUTHORS',
                                   '/tree/main', '/commits/main'])
def test_huggingface_type_is_part_of_identity(prefix, kind, suffix):
    assert project_scope(f'https://huggingface.co/{prefix}Owner/Project{suffix}') == (
        'huggingface', kind, 'Owner', 'Project')


@pytest.mark.parametrize('route', ['src', 'raw', 'commits'])
@pytest.mark.parametrize('ref_kind', ['branch', 'tag', 'commit'])
def test_codeberg_content_routes_require_explicit_reference_type(route, ref_kind):
    assert project_scope(f'https://codeberg.org/Owner/Project/{route}/{ref_kind}/main/AUTHORS') == (
        'codeberg', 'Owner', 'Project')


@pytest.mark.parametrize('url', [
    'https://gitlab.com/Owner', 'https://gitlab.com/Owner/Project',
    'https://gitlab.com/Owner/Sub/Project', 'https://gitlab.com/Owner/Project/-/',
    'https://gitlab.com/Owner/-/tree/main',
    'https://gitlab.com/Owner/Project/-/blob/main',
    'https://gitlab.com/Owner/Project/-/raw/main',
    'https://gitlab.com/Owner/Project/-/tree',
    'https://gitlab.com/Owner/Project/-/commits',
    'https://gitlab.com/Owner/Project/-/issues/1',
    'https://gitlab.com/Owner/Project/-/merge_requests/1',
    'https://gitlab.com/Owner/Project/-/releases/v1',
    'https://gitlab.com/Owner/Project/blob/main/AUTHORS',
    'https://huggingface.co/Owner', 'https://huggingface.co/datasets/Owner',
    'https://huggingface.co/spaces/Owner',
    'https://huggingface.co/Owner/Project/blob/main',
    'https://huggingface.co/Owner/Project/raw/main',
    'https://huggingface.co/Owner/Project/tree',
    'https://huggingface.co/Owner/Project/commits',
    'https://huggingface.co/Owner/Project/discussions/1',
    'https://huggingface.co/Owner/Project/resolve/main/AUTHORS',
    'https://huggingface.co/Owner/Project/unknown/main',
    'https://codeberg.org/Owner', 'https://codeberg.org/Owner/Project/issues/1',
    'https://codeberg.org/Owner/Project/wiki/Home',
    'https://codeberg.org/Owner/Project/src/main/AUTHORS',
    'https://codeberg.org/Owner/Project/src/branch',
    'https://codeberg.org/Owner/Project/raw/tag',
    'https://codeberg.org/Owner/Project/commits/commit',
    'https://codeberg.org/Owner/Project/src/unknown/main',
    'https://codeberg.org/Owner/Project/unknown/branch/main',
    None, 7,
])
def test_ambiguous_roots_incomplete_content_and_unknown_routes_have_no_scope(url):
    assert project_scope(url) is None
    assert transfer_scope(url) is None
    assert project_site(url) is None
    assert not same_project(url, url)


@pytest.mark.parametrize('left,right,scope,site', PROJECTS)
@pytest.mark.parametrize('mutation', [
    lambda u: u.replace('https://', 'http://'),
    lambda u: u.replace('https://', 'https://user@'),
    lambda u: u.replace('.com/', '.com:443/').replace('.co/', '.co:443/').replace('.org/', '.org:443/'),
    lambda u: u.replace('https://', 'https://www.'),
    lambda u: u.replace('/Owner/', '/Owner%2f/'),
    lambda u: u.replace('/Project/', '/Project%41/'),
    lambda u: u.replace('/Owner/', '/Owner//'),
    lambda u: u.replace('/Owner/', '/Owner/../'),
    lambda u: u.replace('/Owner/', '/Owner/./'),
    lambda u: u + '?', lambda u: u + '?page=1',
    lambda u: u + '#', lambda u: u + '#authors',
    lambda u: u + '\\README', lambda u: u + '\n',
])
def test_new_platforms_reject_url_ambiguity_without_host_fallback(left, right, scope, site, mutation):
    url = mutation(left)
    assert url != left
    assert project_scope(url) is None
    assert transfer_scope(url) is None


@pytest.mark.parametrize('url', [
    'https://gitlab.com/users/Profile/-/tree/main',
    'https://gitlab.com/groups/Owner/Project/-/tree/main',
    'https://gitlab.com/explore/Project/-/tree/main',
    'https://huggingface.co/organizations/Owner',
    'https://huggingface.co/settings/Profile',
    'https://huggingface.co/models/Owner',
    'https://codeberg.org/user/Profile',
    'https://codeberg.org/explore/Project',
    'https://codeberg.org/api/Project',
])
def test_platform_and_profile_routes_cannot_be_projects(url):
    assert project_scope(url) is None
    assert transfer_scope(url) is None


@pytest.mark.parametrize('left,right,scope,site', PROJECTS)
def test_same_project_captured_attribution_can_support_other_page(left, right, scope, site):
    result = scoped_review(left, right, policy=POLICY)
    assert result['eligible'] and result['complete_independent_count'] == 1
    assert result['publisher_support'] == {'s0': 's0', 's1': 's0'}
    assert result['publisher_scopes'] == {'s0': list(scope), 's1': list(scope)}
    assert result['publisher_scope_rejections'] == {'s0': [], 's1': []}


@pytest.mark.parametrize('left,right', [
    ('https://gitlab.com/Owner/Sub/Project/-/blob/main/AUTHORS',
     'https://gitlab.com/Owner/Sub/Other/-/tree/main'),
    ('https://gitlab.com/Owner/Sub/Project/-/blob/main/AUTHORS',
     'https://gitlab.com/Owner/Other/Project/-/tree/main'),
    ('https://gitlab.com/Owner/Sub/Project/-/blob/main/AUTHORS',
     'https://gitlab.com/Owner/Project/-/tree/main'),
    ('https://huggingface.co/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/Owner/Other'),
    ('https://huggingface.co/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/datasets/Owner/Project'),
    ('https://huggingface.co/datasets/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/spaces/Owner/Project'),
    ('https://huggingface.co/spaces/Owner/Project/blob/main/AUTHORS',
     'https://huggingface.co/Owner/Project'),
    ('https://codeberg.org/Owner/Project/src/branch/main/AUTHORS',
     'https://codeberg.org/Owner/Other'),
])
@pytest.mark.parametrize('relation', ['same', 'uncertain'])
def test_model_grouping_cannot_transfer_between_projects_or_project_types(left, right, relation):
    result = scoped_review(left, right, policy=POLICY, relation=relation)
    assert result['groups'] == [['s0', 's1']]
    assert result['publisher_support'] == {'s0': 's0', 's1': None}
    assert result['publisher_scope_rejections']['s1'] == ['s0']
    assert result['publisher_support_reasons']['s1'] == 'publisher_scope_mismatch'
    assert not result['eligible'] and result['complete_independent_count'] == 0


@pytest.mark.parametrize('left,right,scope,site', PROJECTS)
def test_legacy_repository_policy_keeps_new_platforms_source_local(left, right, scope, site):
    result = scoped_review(left, right, policy=REPOSITORY_POLICY)
    assert result['publisher_scopes'] == {'s0': None, 's1': None}
    assert result['publisher_support'] == {'s0': 's0', 's1': None}
    assert not result['eligible']
    own = scoped_review(left, right, policy=REPOSITORY_POLICY, right_publisher={
        'name': 'Alpha', 'quote': 'Maintained by Alpha.'})
    assert own['eligible']
    assert own['publisher_support'] == {'s0': 's0', 's1': 's1'}


@pytest.mark.parametrize('url', [
    'https://github.com/Owner/Project',
    'https://github.com/Owner/Project/blob/main/AUTHORS',
    'https://github.com/Owner/Project/tree/main',
    'https://github.com/Owner/Project/commits/main',
    'https://github.com/Owner/Project/issues',
    'https://github.com/Owner', 'https://github.com/Owner/Project?tab=readme',
])
def test_github_v2_parsing_is_unchanged(url):
    assert project_scope(url) == repository_scope(url)


def test_ordinary_host_fallback_and_unsupported_multitenant_hosts():
    assert transfer_scope('https://docs.example.org/authors') == ('host', 'docs.example.org')
    assert transfer_scope('https://DOCS.EXAMPLE.ORG./about') == ('host', 'docs.example.org')
    assert transfer_scope('https://bitbucket.org/Owner/Project') is None
    assert transfer_scope('https://sub.gitlab.com/Owner/Project/-/tree/main') is None


def test_new_policy_default_prepare_and_validation_preserve_input(tmp_path):
    assert POLICY == 'project_attribution_v3'
    assert REPOSITORY_POLICY == 'repository_attribution_v2'
    assert POLICY in POLICIES and REPOSITORY_POLICY in POLICIES
    assert enabled({'comparison': {'publisher_policy': POLICY}})
    assert not enabled({'comparison': {'publisher_policy': REPOSITORY_POLICY}})
    comparison = {'dimensions': ['format'], 'min_domains': 2}
    for policy in POLICIES:
        value = {**comparison, 'evidence_version': 3, 'publisher_policy': policy}
        before = deepcopy(value)
        assert validate_comparison(value) == value == before
    goal = {'queries': ['one', 'two'], 'title': 'Comparison', 'min_sources': 2,
            'required_terms': [], 'outputs': ['document'], 'comparison': comparison}
    before = deepcopy(goal)
    session = ComputerSession.create(tmp_path / 'session')
    prepared = prepare(session, 'Compare sources', goal)
    assert goal == before
    from agent_service.publisher_evidence import POLICY as CURRENT_POLICY
    assert prepared['goal']['comparison']['publisher_policy'] == CURRENT_POLICY
    assert prepared['goal']['comparison']['evidence_version'] == 3
