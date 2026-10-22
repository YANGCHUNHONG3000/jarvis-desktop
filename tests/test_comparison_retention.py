"""Offline literal retention never transfers the previous semantic verdict."""
from copy import deepcopy
from hashlib import sha256

import pytest

from agent_service.comparison_retention import retain_selection
from agent_service.computer import ComputerError
from agent_service import source_comparison as comparison
from test_source_comparison import reviewed_fixture


def fixture():
    goal, accepted, selected, review = reviewed_fixture()
    goal['comparison']['evidence_version'] = 3
    review['sources'][0]['cells'][0]['status'] = 'relevant'
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    raw = {'sources': [{'id': item['id'], 'spans': [None, None]} for item, _ in accepted]}
    return goal, accepted, selected, review, evidence, raw


def test_prior_relevant_quotes_survive_new_missing_spans_without_mutation():
    goal, accepted, selected, _, prior, raw = fixture()
    before = deepcopy((goal, accepted, selected, prior, raw))
    result = retain_selection(goal, accepted, raw, goal, accepted, prior)
    assert result['response'] == selected
    assert result['retained'] == [{'id': row['id'], 'dimension_index': 0, 'quote': row['spans'][0]['quote']}
                                  for row in selected['sources']]
    assert (goal, accepted, selected, prior, raw) == before
    result['response']['sources'][0]['spans'][0]['quote'] = 'mutated'
    assert (goal, accepted, selected, prior, raw) == before


@pytest.mark.parametrize('status', ['irrelevant', 'uncertain', 'missing'])
def test_unproven_old_cells_are_not_retained(status):
    goal, accepted, selected, review, _, raw = fixture()
    review['sources'][0]['cells'][0]['status'] = status
    if status == 'missing': selected['sources'][0]['spans'][0] = None
    prior = comparison.create_evidence(goal, accepted, selected, review)
    result = retain_selection(goal, accepted, raw, goal, accepted, prior)
    assert result['response']['sources'][0]['spans'][0] is None
    assert [row['id'] for row in result['retained']] == ['c1']


@pytest.mark.parametrize('change', ['body', 'url', 'id', 'tail'])
def test_source_identity_and_complete_hash_are_required(change):
    goal, accepted, _, _, prior, raw = fixture()
    current = deepcopy(accepted)
    if change == 'url':
        current[0][0]['url'] = 'https://other.example/new'
        current[0][1]['observation']['requested_url'] = current[0][0]['url']
        current[0][1]['observation']['final_url'] = current[0][0]['url']
    elif change == 'id':
        current[0][0]['id'] = 'replacement'
        raw['sources'][0]['id'] = 'replacement'
    else:
        page = current[0][1]['observation']
        page['text'] += '\nChanged.' if change == 'body' else 'x' * 8100
        page['sha256'] = sha256(page['text'].encode()).hexdigest()
    result = retain_selection(goal, current, raw, goal, accepted, prior)
    assert result['response']['sources'][0]['spans'][0] is None
    assert [row['id'] for row in result['retained']] == ['c1']


def test_same_literal_prefix_with_changed_uncaptured_tail_is_not_same_source():
    goal, accepted, selected, review, _, raw = fixture()
    page = accepted[0][1]['observation']
    page['text'] += 'x' * 8100
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    prior = comparison.create_evidence(goal, accepted, selected, review)
    current = deepcopy(accepted)
    page = current[0][1]['observation']
    page['text'] = page['text'][:-1] + 'y'
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    assert comparison.build_context(goal, current)['sources'][0]['text'] == comparison.build_context(goal, accepted)['sources'][0]['text']
    result = retain_selection(goal, current, raw, goal, accepted, prior)
    assert [row['id'] for row in result['retained']] == ['c1']


def test_raw_unsupported_quotes_remain_raw_unless_a_prior_relevant_span_replaces_them():
    goal, accepted, selected, _, prior, raw = fixture()
    raw['sources'][0]['spans'] = [{'quote': 'new absent quote'}, {'quote': 'another absent quote'}]
    result = retain_selection(goal, accepted, raw, goal, accepted, prior)
    assert result['response']['sources'][0]['spans'][0] == selected['sources'][0]['spans'][0]
    assert result['response']['sources'][0]['spans'][1] == {'quote': 'another absent quote'}
    effective = comparison._response(comparison.build_context(goal, accepted), result['response'])
    assert effective['sources'][0]['spans'][1] is None
    assert raw['sources'][0]['spans'][0] == {'quote': 'new absent quote'}


@pytest.mark.parametrize('change', ['extra', 'wrong-id', 'duplicate', 'bad-span', 'bad-spans-length'])
def test_raw_structural_errors_cannot_be_hidden_by_retention(change):
    goal, accepted, _, _, prior, raw = fixture()
    if change == 'extra': raw['extra'] = True
    elif change == 'wrong-id': raw['sources'][0]['id'] = 'unknown'
    elif change == 'duplicate': raw['sources'][0]['id'] = 'c1'
    elif change == 'bad-span': raw['sources'][0]['spans'][0] = {'quote': 123}
    else: raw['sources'][0]['spans'] = []
    with pytest.raises(ComputerError):
        retain_selection(goal, accepted, raw, goal, accepted, prior)


@pytest.mark.parametrize('change', ['assessment', 'selection', 'hash', 'source'])
def test_prior_evidence_is_fully_reverified(change):
    goal, accepted, _, _, prior, raw = fixture()
    previous = deepcopy(accepted)
    if change == 'assessment': prior['assessment']['cells']['c0'][0]['status'] = 'irrelevant'
    elif change == 'selection': prior['response']['sources'][0]['spans'][0] = None
    elif change == 'hash': prior['context_sha256'] = '0' * 64
    else: previous[0][1]['observation']['sha256'] = '0' * 64
    with pytest.raises(ComputerError):
        retain_selection(goal, accepted, raw, goal, previous, prior)


def test_fresh_review_can_revoke_every_retained_quote():
    goal, accepted, _, review, prior, raw = fixture()
    merged = retain_selection(goal, accepted, raw, goal, accepted, prior)
    for row in review['sources']:
        row['cells'][0] = {'status': 'irrelevant', 'reason': 'New review rejects relevance'}
    evidence = comparison.create_evidence(goal, accepted, merged['response'], review)
    assert len(merged['retained']) == 2
    assert evidence['assessment']['relevant_count'] == 0
    assert not evidence['assessment']['eligible']
    assert comparison.review_failure(evidence) == 'comparison_semantic_insufficient'


def test_retention_order_follows_current_source_and_dimension_order():
    goal, accepted, _, _, prior, raw = fixture()
    current = list(reversed(accepted))
    result = retain_selection(goal, current, raw, goal, accepted, prior)
    assert [row['id'] for row in result['retained']] == ['c1', 'c0']
    assert [row['id'] for row in result['response']['sources']] == ['c0', 'c1']


@pytest.mark.parametrize('change', ['dimension', 'version', 'other-goal'])
def test_cross_goal_or_legacy_retention_is_rejected(change):
    goal, accepted, _, _, prior, raw = fixture()
    current = deepcopy(goal)
    if change == 'dimension': current['comparison']['dimensions'][0] = 'another dimension'
    elif change == 'version': current['comparison']['evidence_version'] = 2
    else: current['title'] = 'Different task'
    with pytest.raises(ComputerError, match='comparison_retention_invalid'):
        retain_selection(current, accepted, raw, goal, accepted, prior)
