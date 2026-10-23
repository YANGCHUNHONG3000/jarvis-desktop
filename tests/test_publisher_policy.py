"""Offline policy contracts for literal publication attribution."""
from copy import deepcopy
from hashlib import sha256

import pytest

from agent_service.computer import ComputerError, ComputerSession
from agent_service.computer_agent import prepare
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.comparison_review import assess
from agent_service.publisher_evidence import POLICY, review_instructions
from agent_service.source_comparison import validate_comparison


def reviewed(quote, name, *, url='https://docs.example.org/tutorial',
             title='Tutorial — openpyxl 3.1.3 documentation', policy=True):
    text = 'Feature: saves XLSX.\n' + quote + '\nEnd.'
    source = {'id': 's1', 'url': url, 'title': title, 'text': text,
              'sha256': sha256(text.encode()).hexdigest()}
    comparison = {'min_domains': 1, 'evidence_version': 3}
    if policy:
        comparison['publisher_policy'] = POLICY
    context = {'dimensions': ['XLSX'], 'sources': [source],
               'goal': {'comparison': comparison}}
    selection = {'sources': [{'id': 's1', 'spans': [{'quote': 'Feature: saves XLSX.'}]}]}
    review = {'sources': [{'id': 's1', 'publisher': {'name': name, 'quote': quote},
                          'cells': [{'status': 'relevant', 'reason': '直接说明'}]}], 'pairs': []}
    return assess(context, selection, review)


def test_document_title_demoted_only_under_new_policy():
    quote = 'openpyxl 3.1.3 documentation'
    fresh = reviewed(quote, 'openpyxl')
    legacy = reviewed(quote, 'openpyxl', policy=False)
    assert fresh['publisher_evidence']['s1'] == 'title_only'
    assert not fresh['eligible'] and fresh['publisher_support']['s1'] is None
    assert legacy['publisher_evidence']['s1'] == 'supported'
    assert legacy['eligible'] and legacy['publisher_support']['s1'] == 's1'


@pytest.mark.parametrize('quote,name', [
    ('Published by Example Foundation.', 'Example Foundation'),
    ('Maintained by Alice Smith.', 'Alice Smith'),
    ('Copyright 2024 Example Foundation.', 'Example Foundation'),
    ('@GeeksforGeeks, Sanchhaya Education Private Limited, All rights reserved',
     'GeeksforGeeks'),
])
def test_named_relation_supported(quote, name):
    result = reviewed(quote, name, title='Publication')
    assert result['publisher_evidence']['s1'] == 'supported'
    assert result['eligible']


@pytest.mark.parametrize('quote,name,reason', [
    ('openpyxl 3.1.3 documentation', 'openpyxl', 'title_only'),
    ('See AUTHORS. openpyxl', 'openpyxl', 'attribution_placeholder'),
    ('Created using Sphinx 7.3.7. openpyxl', 'openpyxl', 'platform_credit'),
    ('openpyxl. Copyright 2024 Other Foundation.', 'openpyxl', 'attribution_missing'),
    ('Not published by Alpha.', 'Alpha', 'attribution_missing'),
    ('Copyright 2024 Alpha. Licensed under MIT license.', 'Alpha', 'attribution_missing'),
    ('Alpha trademarks are owned by their respective owners.', 'Alpha', 'attribution_missing'),
])
def test_unsupported_relation_demoted(quote, name, reason):
    result = reviewed(quote, name)
    assert result['publisher_evidence']['s1'] == reason
    assert not result['eligible']


@pytest.mark.parametrize('title', ['Publisher Alpha', 'Publisher Alpha | Example',
                                   'Example — Publisher Alpha'])
def test_role_words_in_exact_document_title_do_not_prove_publisher(title):
    result = reviewed('Publisher Alpha', 'Alpha', title=title)
    assert result['publisher_evidence']['s1'] == 'title_only'
    assert not result['eligible']


def test_hosting_platform_copyright_is_not_project_publisher():
    result = reviewed('© 2026 GitHub, Inc.', 'GitHub',
                      url='https://github.com/example/project')
    assert result['publisher_evidence']['s1'] == 'platform_credit'
    assert not result['eligible']


def test_missing_attribution_reason_reaches_office_and_failure_message():
    from agent_service.source_comparison import _reviewed_table
    from agent_service.task_diagnostics import explain_failure
    quote = 'openpyxl 3.1.3 documentation'
    assessment = reviewed(quote, 'openpyxl')
    source = {'id': 's1', 'url': 'https://docs.example.org/tutorial',
              'title': quote, 'text': 'Feature: saves XLSX.\n' + quote,
              'captured_at': '2026-10-23T10:00:00+00:00'}
    message = explain_failure('comparison_independence_insufficient',
                              assessment=assessment, dimensions=['XLSX'])
    assert '仅文档或产品标题' in str(message)
    good = {**source, 'id': 's2', 'url': 'https://another.example.net/page',
            'title': 'Example page', 'text': 'Feature: saves XLSX. Published by Alpha.'}
    for row in [source, good]:
        row['sha256'] = sha256(row['text'].encode()).hexdigest()
    context = {'sources': [source, good], 'dimensions': ['XLSX'],
               'goal': {'comparison': {'publisher_policy': POLICY,
                                       'evidence_version': 3, 'min_domains': 1}}}
    selection = {'sources': [{'id': row['id'], 'spans': [{'quote': 'Feature: saves XLSX.'}]}
                             for row in context['sources']]}
    review = deepcopy(assessment['review'])
    review['sources'].append({'id': 's2', 'publisher': {'name': 'Alpha', 'quote': 'Published by Alpha.'},
                              'cells': [{'status': 'relevant', 'reason': 'explicit XLSX'}]})
    review['pairs'] = [{'left': 's1', 'right': 's2', 'relation': 'independent', 'reason': 'distinct'}]
    checked = assess(context, selection, review)
    assert checked['eligible'] and checked['complete_independent_count'] == 1
    evidence = {'version': 3, 'assessment': checked, 'review': checked['review'], 'response': selection}
    table = _reviewed_table(context, evidence)
    assert '仅文档或产品标题' in table['rows'][0][-1]



@pytest.mark.parametrize('quote,name', [
    ('This project was started by Eric Gazoni.', 'Eric Gazoni'),
    ('This project was founded by Eric Gazoni.', 'Eric Gazoni'),
    ('In 2013 Charlie Clark became co-maintainer of the project.', 'Charlie Clark'),
])
def test_authors_page_names_are_project_attribution(quote, name):
    result = reviewed(quote, name, title='AUTHORS')
    assert result['publisher_evidence']['s1'] == 'supported'
    assert result['eligible']


@pytest.mark.parametrize('quote,name', [
    ('This project was not started by Eric Gazoni.', 'Eric Gazoni'),
    ('This project was never founded by Eric Gazoni.', 'Eric Gazoni'),
    ('In 2013 Charlie Clark never became co-maintainer of the project.', 'Charlie Clark'),
])
def test_negated_authors_page_relations_are_missing(quote, name):
    result = reviewed(quote, name, title='AUTHORS')
    assert result['publisher_evidence']['s1'] == 'attribution_missing'
    assert not result['eligible']


def test_review_prompt_suffix_is_new_policy_only():
    assert review_instructions({'comparison': {'evidence_version': 3}}) == ''
    fresh = review_instructions({'comparison': {'evidence_version': 3,
                                               'publisher_policy': POLICY}})
    assert 'AUTHORS' in fresh and 'started/founded' in fresh
    assert 'co-maintainer' in fresh


def test_unknown_document_title_and_uncaptured_relation_still_fail():
    result = reviewed('openpyxl 3.1.3 documentation', 'openpyxl', title='Some other page')
    assert result['publisher_evidence']['s1'] == 'title_only'
    assert not result['eligible']
    quote = 'This project was started by Eric Gazoni.'
    text = 'Feature: saves XLSX.\nAUTHORS\nEnd.'
    source = {'id': 's1', 'url': 'https://docs.example.org/authors', 'title': 'AUTHORS',
              'text': text, 'sha256': sha256(text.encode()).hexdigest()}
    context = {'dimensions': ['XLSX'], 'sources': [source],
               'goal': {'comparison': {'min_domains': 1, 'evidence_version': 3,
                                       'publisher_policy': POLICY}}}
    selection = {'sources': [{'id': 's1', 'spans': [{'quote': 'Feature: saves XLSX.'}]}]}
    review = {'sources': [{'id': 's1', 'publisher': {'name': 'Eric Gazoni', 'quote': quote},
                          'cells': [{'status': 'relevant', 'reason': '直接说明'}]}], 'pairs': []}
    result = assess(context, selection, review)
    assert result['publisher_evidence']['s1'] == 'absent'
    assert not result['eligible']


def test_github_header_requires_exact_repository_url_and_title():
    quote = 'jmcnamara\nXlsxWriter'
    valid = reviewed(quote, 'jmcnamara', url='https://github.com/jmcnamara/XlsxWriter',
                     title='jmcnamara/XlsxWriter: A Python module')
    assert valid['publisher_evidence']['s1'] == 'supported' and valid['eligible']
    for url, title in (
        ('https://github.com/someone/XlsxWriter', 'jmcnamara/XlsxWriter'),
        ('https://github.com/jmcnamara/Other', 'jmcnamara/XlsxWriter'),
        ('https://example.org/jmcnamara/XlsxWriter', 'jmcnamara/XlsxWriter'),
        ('https://github.com/jmcnamara/XlsxWriter', 'XlsxWriter documentation'),
    ):
        result = reviewed(quote, 'jmcnamara', url=url, title=title)
        assert not result['eligible']


def test_publisher_gap_selects_specific_unsupported_same_query_source():
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['Excel sources'], 'comparison': {
        'dimensions': ['XLSX'], 'publisher_policy': POLICY}}
    facts = {'candidates': [
        {'id': 'good', 'url': 'https://docs.example.org/one', 'query_index': 0},
        {'id': 'missing', 'url': 'https://docs.example.org/two', 'query_index': 0}],
        'active_ids': ['good', 'missing'],
        'reads': {'good': {'observation': {'id': 'good'}},
                  'missing': {'observation': {'id': 'missing'}}}}
    evidence = {'assessment': {'group_details': [
        {'ids': ['good'], 'complete': True, 'covered_dimensions': [True]},
        {'ids': ['missing'], 'complete': False, 'covered_dimensions': [True]}],
        'publisher_support': {'good': 'good', 'missing': None}}}
    needs = env._needs(facts, evidence)
    assert len(needs) == 1 and needs[0]['source_id'] == 'missing'
    assert needs[0]['purpose'] == 'attribution'
    assert needs[0]['query'] == 'site:docs.example.org authors maintainers copyright publisher'
    legacy = deepcopy(env.goal)
    del legacy['comparison']['publisher_policy']
    env.goal = legacy
    assert env._needs(facts, evidence)[0]['source_id'] == 'good'
    assert 'purpose' not in env._needs(facts, evidence)[0]


def test_policy_schema_and_new_plan_flag_preserve_legacy_input(tmp_path):
    comparison = {'dimensions': ['XLSX'], 'min_domains': 2}
    assert validate_comparison(comparison) == comparison
    with pytest.raises(ComputerError, match='comparison_invalid'):
        validate_comparison({**comparison, 'publisher_policy': POLICY})
    with pytest.raises(ComputerError, match='comparison_invalid'):
        validate_comparison({**comparison, 'evidence_version': 3,
                             'publisher_policy': 'unknown'})
    goal = {'queries': ['one', 'two'], 'title': 'Comparison', 'min_sources': 2,
            'required_terms': [], 'outputs': ['document'], 'comparison': comparison}
    session = ComputerSession.create(tmp_path / 'session')
    prepared = prepare(session, 'Compare sources', goal)
    assert 'publisher_policy' not in goal['comparison']
    assert prepared['goal']['comparison']['publisher_policy'] == POLICY
