"""Offline project role evidence, conservative coexistence and legacy replay."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations

import pytest

from agent_service.comparison_review import assess, build_review_context
from agent_service.computer import ComputerError
from agent_service.publisher_evidence import attribution_status, review_instructions
from agent_service.publisher_roles import POLICY, candidates, enabled, role


AUTHORS = 'https://github.com/Owner/Project/blob/main/AUTHORS'
README = 'https://github.com/Owner/Project/blob/main/README.md'
OLD_POLICIES = (None, 'explicit_attribution_v1', 'repository_attribution_v2',
                'project_attribution_v3', 'project_attribution_v4')


@pytest.fixture
def evidence():
    def make(text, name='Alice', quote=None, url=AUTHORS):
        return ({'id': 's0', 'url': url, 'title': 'Project evidence', 'text': text,
                 'sha256': sha256(text.encode()).hexdigest()},
                {'name': name, 'quote': text if quote is None else quote})
    return make


def reviewed(rows, *, policy=POLICY, min_domains=2):
    """First rows form one reviewed project group; last row is independent."""
    context = {'dimensions': ['format', 'price'], 'sources': [], 'goal': {
        'comparison': {'min_domains': min_domains, 'evidence_version': 3}}}
    if policy is not None:
        context['goal']['comparison']['publisher_policy'] = policy
    selection, review = {'sources': []}, {'sources': [], 'pairs': []}
    for i, (url, captured, publisher) in enumerate(rows):
        sid, fact = f's{i}', f'Fact {i}.'
        text = fact + '\n' + captured
        if captured.startswith('AUTHORS\n'):
            text, fact = captured, publisher['quote'] if publisher else 'AUTHORS'
        context['sources'].append({'id': sid, 'url': url, 'title': 'Evidence',
            'text': text, 'sha256': sha256(text.encode()).hexdigest()})
        selection['sources'].append({'id': sid, 'spans': [{'quote': fact}] * 2})
        review['sources'].append({'id': sid, 'publisher': publisher, 'cells': [
            {'status': 'relevant', 'reason': 'Captured fact'} for _ in range(2)]})
    last = len(rows) - 1
    for i, j in combinations(range(len(rows)), 2):
        review['pairs'].append({'left': f's{i}', 'right': f's{j}',
            'relation': 'independent' if j == last else 'same', 'reason': 'Reviewed origin'})
    before = deepcopy((context, selection, review))
    result = assess(context, selection, review)
    assert (context, selection, review) == before
    return result


def statement(url, text, name):
    return url, text, {'name': name, 'quote': text}


INDEPENDENT = statement('https://independent.example.org/reference',
                        'Published by Independent Foundation.', 'Independent Foundation')


def test_policy_is_explicit_and_old_goals_do_not_enable_roles():
    assert POLICY == 'project_attribution_v5'
    assert enabled({'comparison': {'publisher_policy': POLICY}})
    for policy in OLD_POLICIES:
        assert not enabled({'comparison': {'publisher_policy': policy}})
    assert not enabled({})


@pytest.mark.parametrize('text,name,expected', [
    ('This project was authored by Alice.', 'Alice', 'project_author'),
    ('This project is maintained by Bob.', 'Bob', 'project_maintainer'),
    ('Alice became co-maintainer of the project.', 'Alice', 'project_maintainer'),
])
def test_explicit_project_subject_has_a_role_and_supported_attribution(evidence, text, name, expected):
    source, publisher = evidence(text, name, url=README)
    assert role(source, publisher) == expected
    assert attribution_status(source, publisher, policy=POLICY) == 'supported'


@pytest.mark.parametrize('text,name', [
    ('Authored by Alice.', 'Alice'),
    ('Published by Alice.', 'Alice'),
    ('Copyright 2026 Alice.', 'Alice'),
    ('This paper was authored by Alice.', 'Alice'),
    ('Reference: Alice, This project was authored by Bob.', 'Alice'),
    ('This project was authored by Alice Smith.', 'Alice'),
    ('This project is maintained by Bob Smith.', 'Bob'),
    ('Alice Smith became co-maintainer of the project.', 'Alice'),
    ('This project was not authored by Alice.', 'Alice'),
])
def test_generic_publishers_citations_negations_and_partial_names_have_no_project_role(evidence, text, name):
    source, publisher = evidence(text, name, url=README)
    assert role(source, publisher) is None


@pytest.mark.parametrize('url', [
    AUTHORS, AUTHORS + '.txt', AUTHORS + '.md', AUTHORS + '.rst',
    'https://github.com/Owner/Project/blob/main/docs/AUTHORS',
    'https://gitlab.com/Owner/Sub/Project/-/blob/main/AUTHORS',
    'https://huggingface.co/Owner/Project/blob/main/AUTHORS',
    'https://codeberg.org/Owner/Project/src/branch/main/AUTHORS',
])
@pytest.mark.parametrize('name,quote', [
    ('Alice', 'AUTHORS\nAlice'),
    ('Alice', 'Alice\nBob'),
    ('Bob', 'Alice\nBob'),
    ('@carol-dev', 'Bob\n@carol-dev'),
])
def test_known_project_authors_roster_requires_a_complete_name_line_and_context(evidence, url, name, quote):
    source, publisher = evidence('AUTHORS\nAlice\nBob\n@carol-dev', name, quote, url)
    assert role(source, publisher) == 'project_author'
    assert attribution_status(source, publisher, policy=POLICY) == 'supported'


@pytest.mark.parametrize('url', [
    'https://github.com/Owner/Project',
    'https://github.com/Owner/Project/tree/main/AUTHORS',
    'https://github.com/Owner/Project/commits/main/AUTHORS',
    'https://github.com/blob/Project/tree/main/AUTHORS',
    'https://github.com/Owner/blob/tree/main/AUTHORS',
    'https://gitlab.com/blob/Project/-/tree/main/AUTHORS',
    'https://codeberg.org/src/Project/commits/branch/main/AUTHORS',
    'https://github.com/Owner/Project/blob/AUTHORS/README.md',
    'https://github.com/Owner/Project/blob/main/AUTHORS/README.md',
    'https://github.com/Owner/Project/blob/main/NOT_AUTHORS',
    'https://github.com/Owner/Project/blob/main/AUTHORS.bak',
    'https://github.com/Owner/Project/blob/main/AUTHORS.md.bak',
    'https://github.com/Owner/Project/blob/main/AUTHORS?raw=1',
    'https://github.com/Owner/Project/issues/1',
    'https://ordinary.example.org/AUTHORS',
    'https://gitlab.com/Owner/Project/AUTHORS',
])
def test_roster_scope_comes_from_actual_authors_file_path(evidence, url):
    source, publisher = evidence('AUTHORS\nAlice\nBob', quote='Alice\nBob', url=url)
    assert role(source, publisher) is None
    assert attribution_status(source, publisher, policy=POLICY) != 'supported'


@pytest.mark.parametrize('text,quote,name', [
    ('Alice\nBob', 'Alice\nBob', 'Alice'),
    ('Introduction\nAUTHORS\nAlice\nBob', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob\nMaintainers', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob\nThis project has many contributors.', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nThis project uses Python.', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nAlice et al. 2026', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nMore names follow', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob\n...', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob\n…', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob\nReferences:', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice\nBob', 'Alice', 'Alice'),
    ('AUTHORS\nAlice Smith\nBob', 'AUTHORS\nAlice', 'Alice'),
    ('AUTHORS\nAlice Smith\nBob', 'AUTHORS\nAlice Smith', 'Alice'),
    ('AUTHORS\nAlice\nBob', 'AUTHORS Alice', 'Alice'),
    ('AUTHORS\nAlice\nBob', 'AUTHORS\nAlice\nBob\nCarol', 'Alice'),
    ('AUTHORS\nAlice\nBob\nAlice\nBob', 'Alice\nBob', 'Alice'),
])
def test_roster_missing_context_prose_partial_or_nonunique_quotes_fail_closed(evidence, text, quote, name):
    source, publisher = evidence(text, name, quote)
    assert role(source, publisher) is None


@pytest.mark.parametrize('policy', OLD_POLICIES)
def test_roster_acceptance_is_not_backported_to_old_attribution_policies(evidence, policy):
    source, publisher = evidence('AUTHORS\nAlice\nBob', quote='Alice\nBob')
    assert attribution_status(source, publisher, policy=policy) != 'supported'


@pytest.mark.parametrize('heading', ['AUTHORS', '# AUTHORS', '## AUTHORS', '### AUTHORS'])
def test_first_nonempty_roster_heading_and_whole_name_lines(evidence, heading):
    source, publisher = evidence('\n\n' + heading + '\nAlice Smith\nBob Jones\n',
                                 'Alice Smith', 'Alice Smith\nBob Jones')
    assert role(source, publisher) == 'project_author'


@pytest.mark.parametrize('count,expected', [(250, 'project_author'), (251, None)])
def test_roster_name_count_is_bounded(evidence, count, expected):
    text = 'AUTHORS\n' + '\n'.join(f'Person{i}' for i in range(count))
    source, publisher = evidence(text, 'Person0', 'AUTHORS\nPerson0')
    assert role(source, publisher) == expected


def test_project_roles_coexist_without_counting_project_twice():
    result = reviewed([
        statement(README, 'This project was authored by Alice.', 'Alice'),
        statement(AUTHORS, 'Bob became co-maintainer of the project.', 'Bob'),
        ('https://github.com/Owner/Project/blob/main/docs/usage.md', 'Usage reference.', None),
        INDEPENDENT,
    ])
    assert result['groups'] == [['s0', 's1', 's2'], ['s3']]
    assert result['publisher_roles'] == {
        's0': 'project_author', 's1': 'project_maintainer', 's2': None, 's3': None}
    assert result['publisher_evidence']['s0'] == result['publisher_evidence']['s1'] == 'supported'
    assert all(result['publisher_support'][sid] in {'s0', 's1'} for sid in ('s0', 's1', 's2'))
    assert {result['publisher_support_reasons'][sid] for sid in ('s0', 's1', 's2')} == {'project_roles_coexist'}
    assert result['group_count'] == 2
    assert result['complete_independent_count'] == 2 and result['eligible']


def test_roster_author_and_narrative_maintainer_coexist_in_full_assessment():
    result = reviewed([
        (AUTHORS, 'AUTHORS\nAlice\nCarol', {'name': 'Alice', 'quote': 'Alice\nCarol'}),
        statement(README, 'This project is maintained by Bob.', 'Bob'), INDEPENDENT,
    ])
    assert result['publisher_roles']['s0'] == 'project_author'
    assert result['publisher_roles']['s1'] == 'project_maintainer'
    assert result['publisher_support_reasons']['s0'] == 'project_roles_coexist'
    assert result['groups'] == [['s0', 's1'], ['s2']]
    assert result['complete_independent_count'] == 2 and result['eligible']


@pytest.mark.parametrize('policy', OLD_POLICIES)
def test_old_policies_keep_multi_name_identity_conflict(policy):
    result = reviewed([
        statement(README, 'This project was authored by Alice.', 'Alice'),
        statement(AUTHORS, 'Bob became co-maintainer of the project.', 'Bob'),
        INDEPENDENT,
    ], policy=policy)
    assert result['publisher_support']['s0'] is None
    assert result['publisher_support']['s1'] is None
    if policy not in (None, 'explicit_attribution_v1'):
        assert result['publisher_support_reasons']['s0'] == 'publisher_identity_conflict'
    assert 'publisher_roles' not in result
    assert result['complete_independent_count'] == 1 and not result['eligible']


@pytest.mark.parametrize('quote,name', [
    ('Published by Acme.', 'Acme'),
    ('This project is published by Acme.', 'Acme'),
    ('Copyright 2026 Acme.', 'Acme'),
    ('Authored by Acme.', 'Acme'),
])
def test_actual_publisher_or_generic_identity_conflict_is_not_relaxed(quote, name):
    result = reviewed([
        statement(README, 'This project was authored by Alice.', 'Alice'),
        statement(AUTHORS, quote, name), INDEPENDENT,
    ])
    assert result['publisher_support']['s0'] is None
    assert result['publisher_support']['s1'] is None
    assert result['publisher_support_reasons']['s0'] == 'publisher_identity_conflict'
    assert result['complete_independent_count'] == 1 and not result['eligible']


@pytest.mark.parametrize('url', [
    'https://github.com/Owner/Other/blob/main/README.md',
    'https://github.com/Other/Project/blob/main/README.md',
    'https://github.com/Owner',
    'https://github.com/Owner/Project/issues/1',
])
def test_project_role_cannot_supply_identity_across_project_boundary(url):
    result = reviewed([
        statement(AUTHORS, 'This project was authored by Alice.', 'Alice'),
        (url, 'No captured attribution.', None), INDEPENDENT,
    ])
    assert result['publisher_support']['s1'] is None
    assert result['publisher_scope_rejections']['s1'] == ['s0']
    assert result['publisher_support_reasons']['s1'] == 'publisher_scope_mismatch'
    assert result['complete_independent_count'] == 1 and not result['eligible']


def test_bare_roster_name_remains_rejected_by_full_assessment():
    result = reviewed([
        (AUTHORS, 'AUTHORS\nAlice\nBob', {'name': 'Alice', 'quote': 'Alice'}),
        (README, 'No captured attribution.', None), INDEPENDENT,
    ])
    assert result['publisher_evidence']['s0'] == 'bare_name'
    assert result['publisher_support']['s0'] is None
    assert result['publisher_support']['s1'] is None
    assert result['complete_independent_count'] == 1 and not result['eligible']


@pytest.mark.parametrize('text,url,expected', [
    ('AUTHORS\nAlice\nBob\nCarol\nDave', AUTHORS,
     [('Alice', 'project_author'), ('Bob', 'project_author')]),
    ('This project was authored by Alice.\nThis project is maintained by Bob.\n'
     'Carol became co-maintainer of the project.', README,
     [('Alice', 'project_author'), ('Bob', 'project_maintainer')]),
    ('In 2012 Alice became co-maintainer of the project.', README,
     [('Alice', 'project_maintainer')]),
])
def test_candidates_are_bounded_exact_unique_role_suggestions(evidence, text, url, expected):
    source, _ = evidence(text, url=url)
    before = deepcopy(source)
    found = candidates(source)
    assert [(item['name'], item['role']) for item in found] == expected
    assert len(found) <= 2 and source == before
    for item in found:
        assert set(item) == {'name', 'quote', 'role'}
        assert source['text'].count(item['quote']) == 1
        assert role(source, {key: item[key] for key in ('name', 'quote')}) == item['role']


@pytest.mark.parametrize('text,url', [
    ('', AUTHORS), ('AUTHORS', AUTHORS),
    ('AUTHORS\nAlice\nAlice', AUTHORS),
    ('AUTHORS\nAlice\nThis project uses Python.', AUTHORS),
    ('AUTHORS\nAlice\nAlice et al. 2026', AUTHORS),
    ('AUTHORS\nAlice\nBob', README),
    ('AUTHORS\nAlice\nBob', 'https://github.com/Owner/Project/tree/main/AUTHORS'),
    ('This project was authored by Alice.', 'https://ordinary.example.org/reference'),
    ('Authored by Alice. Copyright Bob.', README),
    ('This project was authored by Alice.\nThis project was authored by Alice.', README),
])
def test_candidates_reject_empty_invalid_out_of_scope_or_ambiguous_evidence(evidence, text, url):
    source, _ = evidence(text, url=url)
    assert candidates(source) == []


@pytest.fixture
def role_evidence_fixture():
    from test_source_comparison import accepted, goal
    objective, source_template = goal(), accepted()[0]
    objective['comparison'].update(evidence_version=3, publisher_policy=POLICY)
    rows = [
        (AUTHORS, 'AUTHORS\nAlice\nCarol', {'name': 'Alice', 'quote': 'Alice\nCarol'}),
        statement(README, 'This project is maintained by Bob.', 'Bob'), INDEPENDENT,
    ]
    sources, selection, review = [], {'sources': []}, {'sources': [], 'pairs': []}
    for i, (url, text, publisher) in enumerate(rows):
        source, record = deepcopy(source_template)
        sid = f'c{i}'
        source.update(id=sid, url=url)
        record['observation'].update(title=f'Role evidence {i}', text=text,
            sha256=sha256(text.encode()).hexdigest(), requested_url=url, final_url=url)
        record['excerpt'] = {'start': 0, 'end': len(text), 'text': text}
        sources.append((source, record))
        selection['sources'].append({'id': sid, 'spans': [{'quote': text}] * 2})
        review['sources'].append({'id': sid, 'publisher': publisher, 'cells': [
            {'status': 'relevant', 'reason': 'Captured role evidence'} for _ in range(2)]})
    for i, j in combinations(range(3), 2):
        review['pairs'].append({'left': f'c{i}', 'right': f'c{j}',
            'relation': 'same' if j == 1 else 'independent', 'reason': 'Reviewed origin'})
    return objective, sources, selection, review


@pytest.mark.parametrize('policy', OLD_POLICIES)
def test_role_candidates_leave_entire_old_policy_review_context_unchanged(role_evidence_fixture, policy):
    from agent_service import source_comparison as comparison
    objective, sources, selected, _ = role_evidence_fixture
    if policy is None:
        del objective['comparison']['publisher_policy']
    else:
        objective['comparison']['publisher_policy'] = policy
    context = comparison.build_context(objective, sources)
    expected = {'dimensions': deepcopy(context['dimensions']),
                'sources': deepcopy(context['sources']), 'selection': deepcopy(selected)}
    if policy is not None:
        expected['goal'] = deepcopy(objective)
    before = deepcopy((context, selected))
    assert build_review_context(context, selected) == expected
    assert (context, selected) == before


def test_role_candidates_extend_only_v5_copied_sources_and_do_not_replace_null_verdict(role_evidence_fixture):
    from agent_service import source_comparison as comparison
    objective, sources, selected, review = role_evidence_fixture
    context = comparison.build_context(objective, sources)
    before = deepcopy((context, selected))
    built = build_review_context(context, selected)
    assert (context, selected) == before
    assert set(built) == {'dimensions', 'sources', 'selection', 'goal'}
    assert all(set(row) == set(original) | {'project_role_candidates'}
               for row, original in zip(built['sources'], context['sources']))
    assert built['sources'][0]['project_role_candidates'][0]['name'] == 'Alice'
    assert built['sources'][1]['project_role_candidates'][0]['name'] == 'Bob'
    assert built['sources'][2]['project_role_candidates'] == []
    review['sources'][0]['publisher'] = review['sources'][1]['publisher'] = None
    result = assess(built, selected, review)
    assert result['publisher_evidence']['c0'] == result['publisher_evidence']['c1'] == 'unknown'
    assert result['publisher_support']['c0'] is result['publisher_support']['c1'] is None
    assert result['complete_independent_count'] == 1 and not result['eligible']


def test_role_office_table_labels_anchor_role_and_same_project_coexistence(role_evidence_fixture):
    from agent_service import source_comparison as comparison
    objective, sources, selected, review = role_evidence_fixture
    evidence = comparison.create_evidence(objective, sources, selected, review)
    output = comparison.table(objective, sources, evidence)
    assert evidence['assessment']['complete_independent_count'] == 2
    for row in output['rows'][:2]:
        assert '具名归属依据（项目作者，不证明法律所有权）' in row[-1]
        assert '同项目多位作者/维护者并存，仍计一个来源组' in row[-1]
    assert '发布方依据：' in output['rows'][2][-1]
    review['sources'][0]['publisher'] = None
    maintained = comparison.create_evidence(objective, sources, selected, review)
    output = comparison.table(objective, sources, maintained)
    assert all('具名归属依据（项目维护者，不证明法律所有权）' in row[-1]
               for row in output['rows'][:2])


@pytest.mark.parametrize('mutation', ['role', 'support_reason', 'candidate_context_hash', 'candidate_extra'])
def test_role_evidence_tampering_is_rejected(role_evidence_fixture, mutation):
    from agent_service import source_comparison as comparison
    objective, sources, selected, review = role_evidence_fixture
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assert comparison.verify_evidence(objective, sources, evidence) == evidence
    if mutation == 'role':
        evidence['assessment']['publisher_roles']['c0'] = 'project_maintainer'
    elif mutation == 'support_reason':
        evidence['assessment']['publisher_support_reasons']['c0'] = 'supported'
    elif mutation == 'candidate_context_hash':
        evidence['review_context_sha256'] = '0' * 64
    else:
        evidence['project_role_candidates'] = [{'name': 'Injected', 'quote': 'Injected'}]
    with pytest.raises(ComputerError, match='comparison_evidence_invalid'):
        comparison.verify_evidence(objective, sources, evidence)


@pytest.fixture
def role_transport_fixture(role_evidence_fixture):
    from agent_service import source_comparison as comparison
    from test_comparison_model_transport import aliased
    objective, sources, selected, review = role_evidence_fixture
    objective['comparison']['evidence_version'] = 4
    context = comparison.review_context(objective, sources, selected)
    response = aliased(review, [source['id'] for source in context['sources']])
    response['sources'][0]['publisher'] = {'candidate_index': 0}
    response['sources'][1]['publisher'] = {'candidate_index': 0}
    review['sources'][0]['publisher'] = {key: context['sources'][0]['project_role_candidates'][0][key]
                                        for key in ('name', 'quote')}
    return context, response, review


@pytest.mark.parametrize('index', [0, 1])
def test_role_transport_decodes_only_the_selected_bound_literal_candidate(role_transport_fixture, index):
    from agent_service.comparison_model_transport import call_model
    context, response, expected = role_transport_fixture
    response['sources'][0]['publisher']['candidate_index'] = index
    chosen = context['sources'][0]['project_role_candidates'][index]
    expected['sources'][0]['publisher'] = {key: chosen[key] for key in ('name', 'quote')}
    before = deepcopy((context, response))
    calls = []
    def callback(wire):
        calls.append(deepcopy(wire))
        assert wire['sources'][0]['id'] == 's0'
        assert wire['sources'][0]['project_role_candidates'] == context['sources'][0]['project_role_candidates']
        return response
    assert call_model(context, callback) == expected
    assert len(calls) == 1 and (context, response) == before


@pytest.mark.parametrize('publisher', [
    {'candidate_index': True}, {'candidate_index': False},
    {'candidate_index': -1}, {'candidate_index': 2},
    {'candidate_index': 0.0}, {'candidate_index': '0'}, {'candidate_index': None},
    {'candidate_index': 0, 'name': 'Injected'}, {},
    {'name': 'Alice', 'quote': 'AUTHORS\nAlice'},
])
def test_role_transport_rejects_invalid_or_injected_candidate_selections(role_transport_fixture, publisher):
    from agent_service.comparison_model_transport import call_model
    context, response, _ = role_transport_fixture
    response['sources'][0]['publisher'] = publisher
    with pytest.raises(ComputerError, match='comparison_transport_invalid'):
        call_model(context, lambda wire: response)


@pytest.mark.parametrize('mutation', ['name', 'quote', 'role', 'missing', 'swap', 'text'])
def test_role_transport_rejects_tampered_bound_candidate_context(role_transport_fixture, mutation):
    from agent_service.comparison_model_transport import call_model
    context, response, _ = role_transport_fixture
    source = context['sources'][0]
    if mutation == 'missing':
        del source['project_role_candidates']
    elif mutation == 'swap':
        source['project_role_candidates'] = deepcopy(context['sources'][1]['project_role_candidates'])
    elif mutation == 'text':
        source['text'] = 'AUTHORS\nDifferent\nPeople'
    else:
        source['project_role_candidates'][0][mutation] = 'Injected'
    with pytest.raises(ComputerError, match='comparison_transport_invalid'):
        call_model(context, lambda wire: response)


def test_role_transport_callback_mutation_cannot_inject_a_new_literal(role_transport_fixture):
    from agent_service.comparison_model_transport import call_model
    context, response, expected = role_transport_fixture
    before = deepcopy(context)
    def callback(wire):
        wire['sources'][0]['project_role_candidates'][0].update(name='Injected', quote='Injected')
        return response
    assert call_model(context, callback) == expected
    assert context == before


def test_role_transport_keeps_null_choice_and_empty_candidate_sources_without_fallback(role_transport_fixture):
    from agent_service.comparison_model_transport import call_model
    context, response, expected = role_transport_fixture
    response['sources'][0]['publisher'] = None
    expected['sources'][0]['publisher'] = None
    decoded = call_model(context, lambda wire: response)
    assert decoded == expected
    assert decoded['sources'][2]['publisher'] == expected['sources'][2]['publisher']
    result = assess(context, context['selection'], decoded)
    assert result['publisher_evidence']['c0'] == 'unknown'


@pytest.mark.parametrize('policy', OLD_POLICIES)
def test_role_transport_never_decodes_candidate_index_for_old_policies(role_evidence_fixture, policy):
    from agent_service import source_comparison as comparison
    from agent_service.comparison_model_transport import call_model
    from test_comparison_model_transport import aliased
    objective, sources, selected, review = role_evidence_fixture
    objective['comparison']['evidence_version'] = 4
    if policy is None:
        del objective['comparison']['publisher_policy']
    else:
        objective['comparison']['publisher_policy'] = policy
    context = comparison.review_context(objective, sources, selected)
    response = aliased(review, [source['id'] for source in context['sources']])
    response['sources'][0]['publisher'] = {'candidate_index': 0}
    expected = deepcopy(review)
    expected['sources'][0]['publisher'] = {'candidate_index': 0}
    assert call_model(context, lambda wire: response) == expected
    with pytest.raises(ComputerError, match='comparison_review_invalid'):
        assess(context, selected, expected)


@pytest.mark.parametrize('version', [None, 2, 3, 4])
def test_role_prompt_candidate_index_is_limited_to_version_four_transport(version):
    comparison = {'publisher_policy': POLICY}
    if version is not None:
        comparison['evidence_version'] = version
    instructions = review_instructions({'comparison': comparison})
    if version == 4:
        assert 'publisher MUST be null or {"candidate_index":0}' in instructions
        assert 'For sources with no candidates retain the original name/quote schema.' in instructions
    else:
        assert 'candidate_index' not in instructions
        assert 'copy its exact name and quote only, omitting its role field.' in instructions


@pytest.mark.parametrize('policy', OLD_POLICIES)
@pytest.mark.parametrize('version', [3, 4])
def test_role_prompt_old_policies_keep_original_output_shape(policy, version):
    instructions = review_instructions({'comparison': {
        'publisher_policy': policy, 'evidence_version': version}})
    assert 'candidate_index' not in instructions
    assert 'project_role_candidates' not in instructions


def test_role_v3_copied_candidate_name_quote_works_without_wire_decoder(role_evidence_fixture):
    from agent_service import source_comparison as comparison
    objective, sources, selected, review = role_evidence_fixture
    assert objective['comparison']['evidence_version'] == 3
    context = comparison.review_context(objective, sources, selected)
    for source, row in zip(context['sources'], review['sources']):
        if source['project_role_candidates']:
            chosen = source['project_role_candidates'][0]
            row['publisher'] = {key: chosen[key] for key in ('name', 'quote')}
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assert comparison.verify_evidence(objective, sources, evidence) == evidence
    assert evidence['assessment']['publisher_evidence']['c0'] == 'supported'
    assert evidence['assessment']['complete_independent_count'] == 2
    assert evidence['assessment']['eligible']
