"""Offline repository attribution boundaries and immutable legacy replay."""
from copy import deepcopy
from hashlib import sha256

import pytest

from agent_service.comparison_review import assess
from agent_service.computer import ComputerSession
from agent_service.computer_agent import prepare
from agent_service.publisher_evidence import LEGACY_POLICY, review_instructions
from agent_service.publisher_scope import POLICY, repository_scope, same_repository, transfer_scope
from agent_service.source_comparison import validate_comparison


def scoped_review(left, right, *, policy=POLICY, relation='same', right_publisher=None):
    comparison = {'min_domains': 1, 'evidence_version': 3}
    if policy is not None:
        comparison['publisher_policy'] = policy
    context = {'dimensions': ['format', 'price'], 'sources': [],
               'goal': {'comparison': comparison}}
    selection, review = {'sources': []}, {'sources': [], 'pairs': [
        {'left': 's0', 'right': 's1', 'relation': relation, 'reason': 'model judgment'}]}
    for i, url in enumerate((left, right)):
        sid = f's{i}'
        publisher = {'name': 'Alpha', 'quote': 'Published by Alpha.'} if i == 0 else right_publisher
        text = f'Fact {i}.\n' + (publisher['quote'] if publisher else 'No author statement captured.')
        context['sources'].append({'id': sid, 'url': url, 'title': f'Evidence {i}',
                                   'text': text, 'sha256': sha256(text.encode()).hexdigest()})
        selection['sources'].append({'id': sid, 'spans': [{'quote': f'Fact {i}.'}] * 2})
        review['sources'].append({'id': sid, 'publisher': publisher, 'cells': [
            {'status': 'relevant' if d == i else 'irrelevant', 'reason': 'Captured fact'}
            for d in range(2)]})
    before = deepcopy((context, selection, review))
    result = assess(context, selection, review)
    assert (context, selection, review) == before
    return result


@pytest.mark.parametrize('suffix', [
    '', '/', '/blob/main/README.md', '/blob/main/docs/AUTHORS',
    '/tree/main', '/tree/release/docs', '/commits/main', '/commits/main/AUTHORS',
])
def test_github_supported_content_has_case_insensitive_repository_scope(suffix):
    url = 'https://github.com/Example/Project' + suffix
    assert repository_scope(url) == ('github', 'example', 'project')
    assert transfer_scope(url) == ('github', 'example', 'project')
    assert same_repository(url, 'https://github.com/example/project')


@pytest.mark.parametrize('url', [
    'http://github.com/owner/repo', 'https://www.github.com/owner/repo',
    'https://api.github.com/repos/owner/repo',
    'https://raw.githubusercontent.com/owner/repo/main/AUTHORS',
    'https://github.com.evil.example/owner/repo',
    'https://github.com:443/owner/repo', 'https://user@github.com/owner/repo',
    'https://github.com/owner', 'https://github.com/owner/',
    'https://github.com/orgs/owner', 'https://github.com/search/code',
    'https://github.com/owner/repo.git', 'https://github.com/owner/..',
    'https://github.com/-owner/repo', 'https://github.com/owner-/repo',
    'https://github.com/owner/repo/issues', 'https://github.com/owner/repo/pulls',
    'https://github.com/owner/repo/releases/tag/v1',
    'https://github.com/owner/repo/blob/main',
    'https://github.com/owner/repo/tree', 'https://github.com/owner/repo/commits',
    'https://github.com/owner/repo/blob/main/../AUTHORS',
    'https://github.com/owner/repo/blob/main/./AUTHORS',
    'https://github.com/owner//repo', 'https://github.com/owner/repo//',
    'https://github.com/owner/repo?tab=readme', 'https://github.com/owner/repo?',
    'https://github.com/owner/repo#readme', 'https://github.com/owner/repo#',
    'https://github.com/owner%2frepo/project',
    'https://github.com/owner/repo/blob/main/%41UTHORS',
    'https://github.com/owner/repo\\AUTHORS',
    'https://github.com/owner/repo\n', 'https://github.com/owner/项目',
    None, 7,
])
def test_ambiguous_or_non_repository_urls_have_no_repository_identity(url):
    assert repository_scope(url) is None
    assert not same_repository(url, 'https://github.com/owner/repo')


@pytest.mark.parametrize('url', [
    'https://github.com/owner', 'https://github.com/owner/repo/issues',
    'https://raw.githubusercontent.com/owner/repo/main/AUTHORS',
    'https://api.github.com/repos/owner/repo', 'https://owner.github.io/repo',
    'https://gitlab.com/owner/repo', 'https://bitbucket.org/owner/repo',
    'https://codeberg.org/owner/repo', 'https://sourceforge.net/projects/repo',
    'https://huggingface.co/owner/repo', 'https://medium.com/@owner/about',
    'https://dev.to/owner', 'https://owner.notion.site/about',
    'https://notion.so/owner', 'https://owner.wordpress.com/about',
    'https://owner.blogspot.com/about', 'https://docs.google.com/document/d/id',
    'https://drive.google.com/file/d/id', 'https://sites.google.com/view/owner',
])
def test_known_multitenant_hosts_never_gain_host_wide_transfer_scope(url):
    assert transfer_scope(url) is None


@pytest.mark.parametrize('url,scope', [
    ('https://docs.example.org/authors', ('host', 'docs.example.org')),
    ('http://docs.example.org/tutorial?view=full#part', ('host', 'docs.example.org')),
    ('https://DOCS.EXAMPLE.ORG./about', ('host', 'docs.example.org')),
    ('https://github.com.example.org/owner/repo', ('host', 'github.com.example.org')),
    ('file:///tmp/authors', None), ('about:blank', None), (None, None),
])
def test_ordinary_hosts_keep_exact_host_transfer_scope(url, scope):
    assert transfer_scope(url) == scope


@pytest.mark.parametrize('right', [
    'https://github.com/alpha/other/blob/main/tutorial.md',
    'https://github.com/beta/project/blob/main/tutorial.md',
])
@pytest.mark.parametrize('relation', ['same', 'uncertain'])
def test_model_grouping_cannot_transfer_attribution_across_repositories(right, relation):
    result = scoped_review('https://github.com/alpha/project', right, relation=relation)
    assert result['groups'] == [['s0', 's1']]
    assert result['publisher_evidence']['s0'] == 'supported'
    assert result['publisher_support'] == {'s0': 's0', 's1': None}
    assert result['publisher_scope_rejections']['s1'] == ['s0']
    assert result['publisher_support_reasons']['s1'] == 'publisher_scope_mismatch'
    assert result['complete_independent_count'] == 0 and not result['eligible']
    assert result['review']['pairs'][0]['relation'] == relation


@pytest.mark.parametrize('right', [
    'https://github.com/alpha/project',
    'https://github.com/Alpha/Project/blob/main/docs/tutorial.md',
    'https://github.com/alpha/project/tree/main',
    'https://github.com/alpha/project/commits/main/AUTHORS',
])
def test_same_repository_pages_can_combine_attribution_and_dimension_evidence(right):
    result = scoped_review('https://github.com/alpha/project/blob/main/AUTHORS', right)
    assert result['eligible'] and result['complete_independent_count'] == 1
    assert result['publisher_support'] == {'s0': 's0', 's1': 's0'}
    assert result['publisher_scopes']['s1'] == ['github', 'alpha', 'project']
    assert result['publisher_scope_rejections'] == {'s0': [], 's1': []}


@pytest.mark.parametrize('left,right', [
    ('https://github.com/alpha/project', 'https://github.com/alpha'),
    ('https://github.com/alpha/project', 'https://github.com/alpha/project/issues'),
    ('https://gitlab.com/alpha/project', 'https://gitlab.com/alpha/project/-/blob/main/AUTHORS'),
    ('https://medium.com/@alpha/about', 'https://medium.com/@alpha/tutorial'),
])
def test_unsupported_scope_requires_attribution_on_each_page(left, right):
    result = scoped_review(left, right)
    assert result['publisher_support']['s1'] is None
    assert not result['eligible']
    assert result['publisher_scopes']['s1'] is None
    own = scoped_review(left, right, right_publisher={
        'name': 'Alpha', 'quote': 'Maintained by Alpha.'})
    assert own['publisher_support']['s1'] == 's1'
    assert own['publisher_support_reasons']['s1'] == 'supported'
    assert own['eligible']


def test_missing_scope_without_any_same_host_anchor_has_specific_reason():
    result = scoped_review('https://example.org/authors', 'https://gitlab.com/alpha/project')
    assert result['publisher_scopes']['s1'] is None
    assert result['publisher_scope_rejections']['s1'] == []
    assert result['publisher_support_reasons']['s1'] == 'repository_scope_missing'
    assert not result['eligible']


def test_conflicting_names_within_one_repository_fail_closed():
    result = scoped_review('https://github.com/alpha/project',
                           'https://github.com/alpha/project/blob/main/AUTHORS',
                           right_publisher={'name': 'Beta', 'quote': 'Maintained by Beta.'})
    assert result['publisher_support'] == {'s0': None, 's1': None}
    assert set(result['publisher_support_reasons'].values()) == {'publisher_identity_conflict'}
    assert not result['eligible']


@pytest.mark.parametrize('policy', [None, LEGACY_POLICY, POLICY])
def test_regular_domain_same_host_support_is_preserved(policy):
    result = scoped_review('https://docs.example.org/authors',
                           'https://docs.example.org/tutorial', policy=policy)
    assert result['eligible']
    assert result['publisher_support'] == {'s0': 's0', 's1': 's0'}


@pytest.mark.parametrize('policy', [None, LEGACY_POLICY])
@pytest.mark.parametrize('right', [
    'https://github.com/alpha/other/blob/main/tutorial.md',
    'https://github.com/beta/project/blob/main/tutorial.md',
    'https://github.com/alpha',
])
def test_old_policy_keeps_host_support_and_omits_new_diagnostics(policy, right):
    result = scoped_review('https://github.com/alpha/project', right, policy=policy)
    assert result['eligible']
    assert result['publisher_support'] == {'s0': 's0', 's1': 's0'}
    assert not {'publisher_scopes', 'publisher_scope_rejections',
                'publisher_support_reasons'} & result.keys()


def test_policy_versions_and_default_prepare_do_not_upgrade_approved_input(tmp_path):
    assert POLICY == 'repository_attribution_v2'
    assert LEGACY_POLICY == 'explicit_attribution_v1'
    comparison = {'dimensions': ['format'], 'min_domains': 2}
    for policy in (POLICY, LEGACY_POLICY):
        value = {**comparison, 'evidence_version': 3, 'publisher_policy': policy}
        assert validate_comparison(value) == value
    goal = {'queries': ['one', 'two'], 'title': 'Comparison', 'min_sources': 2,
            'required_terms': [], 'outputs': ['document'], 'comparison': comparison}
    before = deepcopy(goal)
    session = ComputerSession.create(tmp_path / 'session')
    prepared = prepare(session, 'Compare sources', goal)
    assert goal == before
    from agent_service.publisher_evidence import POLICY as CURRENT_POLICY
    assert prepared['goal']['comparison']['publisher_policy'] == CURRENT_POLICY
    legacy_instructions = review_instructions({'comparison': {'publisher_policy': LEGACY_POLICY}})
    scoped_instructions = review_instructions({'comparison': {'publisher_policy': POLICY}})
    assert 'explicit named attribution' in legacy_instructions
    assert 'cannot cross owner/repository boundaries' not in legacy_instructions
    assert 'cannot cross owner/repository boundaries' in scoped_instructions
