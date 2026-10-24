"""Versioned hosted-document grouping; model evidence remains mandatory."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations

import pytest

from agent_service.comparison_review import assess, build_review_context
from agent_service.comparison_model_transport import call_model
from agent_service.publisher_roles import candidates
from agent_service.hosted_document_policy import POLICY


def fixture(rows=None, policy=POLICY):
    rows = rows or [
        ('https://alpha.readthedocs.io/en/latest/index.html', 'Alice Able'),
        ('https://beta.readthedocs.io/en/latest/index.html', 'Bob Baker')]
    context = {'dimensions': ['Formula evaluation'], 'sources': [], 'goal': {
        'comparison': {'min_domains': 2, 'evidence_version': 4, 'publisher_policy': policy}}}
    selection, review = {'sources': []}, {'sources': [], 'pairs': []}
    for i, (url, name) in enumerate(rows):
        fact, quote = f'Tool {i} does not calculate formula results.', f'Author\n:\n{name}'
        text = f'Documentation\n{quote}\n\n{fact}'
        sid = f'source-{i}'
        context['sources'].append({'id': sid, 'url': url, 'title': 'Documentation',
                                  'text': text, 'sha256': sha256(text.encode()).hexdigest()})
        selection['sources'].append({'id': sid, 'spans': [{'quote': fact}]})
        review['sources'].append({'id': sid, 'publisher': {'name': name, 'quote': quote},
            'cells': [{'status': 'relevant', 'reason': 'Direct explicit statement'}]})
    for i, j in combinations(range(len(rows)), 2):
        review['pairs'].append({'left': f'source-{i}', 'right': f'source-{j}',
                                'relation': 'independent', 'reason': 'Distinct named projects'})
    return context, selection, review


def test_v6_can_review_distinct_project_hosts_without_asserting_independence():
    context, selection, review = fixture()
    before = deepcopy((context, selection, review))
    actual = assess(context, selection, review)
    assert actual['complete_independent_count'] == 2 and actual['eligible']
    assert (context, selection, review) == before
    review['pairs'][0]['relation'] = 'uncertain'
    assert not assess(context, selection, review)['eligible']
    review['pairs'][0]['relation'] = 'independent'
    review['sources'][1]['publisher'] = None
    assert not assess(context, selection, review)['eligible']


@pytest.mark.parametrize('policy', ['explicit_attribution_v1', 'repository_attribution_v2',
    'project_attribution_v3', 'project_attribution_v4', 'project_attribution_v5'])
def test_old_policies_keep_original_shared_host_family(policy):
    context, selection, review = fixture(policy=policy)
    assert assess(context, selection, review)['group_count'] == 1


@pytest.mark.parametrize('case', ['same_host', 'same_name', 'same_content', 'same_normalized', 'model_same'])
def test_v6_does_not_count_duplicate_or_shared_publication_as_independent(case):
    context, selection, review = fixture()
    if case == 'same_host':
        context['sources'][1]['url'] = 'https://alpha.readthedocs.io/zh/latest/index.html'
    elif case == 'same_name':
        context, selection, review = fixture([
            ('https://alpha.readthedocs.io/en/latest/', 'Alice Able'),
            ('https://beta.readthedocs.io/en/latest/', 'Alice Able')])
    elif case == 'same_content':
        context['sources'][1]['sha256'] = context['sources'][0]['sha256']
    elif case == 'same_normalized':
        for source in context['sources']:
            source['normalized_sha256'] = 'a' * 64
    else:
        review['pairs'][0]['relation'] = 'same'
    actual = assess(context, selection, review)
    assert actual['group_count'] == 1 and not actual['eligible']


@pytest.mark.parametrize('target, supported', [
    ('https://alpha.readthedocs.io/en/latest/formula.html', True),
    ('https://alpha.readthedocs.io/projects/other/en/latest/', False),
    ('https://alpha.readthedocs.io/projects/', False)])
def test_v6_author_transfer_is_limited_to_project_scope(target, supported):
    context, selection, review = fixture([
        ('https://alpha.readthedocs.io/en/latest/index.html', 'Alice Able'),
        (target, 'Bob Baker')])
    review['sources'][1]['publisher'] = None
    review['pairs'][0]['relation'] = 'same'
    result = assess(context, selection, review)
    assert (result['publisher_support']['source-1'] is not None) == supported


def test_author_field_candidates_are_v6_only_and_require_model_selection():
    context, selection, review = fixture()
    original = deepcopy(context)
    assert candidates(context['sources'][0]) == []
    wire = build_review_context(context, selection)
    assert wire['sources'][0]['project_role_candidates'][0]['name'] == 'Alice Able'
    assert context == original

    def choose(data):
        response = deepcopy(review)
        for i, row in enumerate(response['sources']):
            row['id'] = data['sources'][i]['id']
            row['publisher'] = {'candidate_index': 0} if i == 0 else None
        response['pairs'][0].update(left=data['sources'][0]['id'], right=data['sources'][1]['id'])
        return response

    response = call_model(wire, choose)
    assert response['sources'][0]['publisher'] == review['sources'][0]['publisher']
    assert response['sources'][1]['publisher'] is None
    assert not assess(context, selection, response)['eligible']


def test_same_project_coauthors_can_support_each_other_without_extra_sources():
    context, selection, review = fixture([
        ('https://alpha.readthedocs.io/en/latest/', 'Alice Able'),
        ('https://alpha.readthedocs.io/en/latest/authors.html', 'Bob Baker'),
        ('https://beta.readthedocs.io/en/latest/', 'Carol Clark')])
    review['pairs'][0]['relation'] = 'same'
    result = assess(context, selection, review)
    assert result['complete_independent_count'] == 2
    assert result['publisher_support_reasons']['source-0'] == 'project_roles_coexist'
