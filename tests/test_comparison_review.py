"""Offline semantic-review validation; no browser or model calls."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations

import pytest

from agent_service.computer import ComputerError
from agent_service.comparison_review import assess, build_review_context, REVIEW_MAX_TOKENS


def fixture(n=2):
    sources = []
    for i in range(n):
        text = f'Publisher 名{i}😀. Files DOCX{i}. More.'
        sources.append({'id': f's{i}', 'title': f'Title{i}', 'url': f'https://brand{i}.example{i}/p',
                        'text': text, 'sha256': sha256(text.encode()).hexdigest()})
    context = {'dimensions': ['文件格式'], 'sources': sources,
               'goal': {'comparison': {'min_domains': 2}}}
    selection = {'sources': [{'id': s['id'], 'spans': [{'quote': f'Files DOCX{i}.'}]}
                             for i, s in enumerate(sources)]}
    review = {'sources': [{'id': s['id'], 'publisher': {'name': f'名{i}😀',
               'quote': f'Publisher 名{i}😀.'},
               'cells': [{'status': 'relevant', 'reason': '直接列出文件格式'}]}
              for i, s in enumerate(sources)],
              'pairs': [{'left': f's{i}', 'right': f's{j}', 'relation': 'independent',
                         'reason': '正文给出不同发行商'} for i, j in combinations(range(n), 2)]}
    return context, selection, review


def test_valid_review_copies_inputs_and_context_has_no_extractor_reason():
    c, s, r = fixture()
    before = deepcopy((c, s, r))
    built = build_review_context(c, s)
    assert set(built) == {'dimensions', 'sources', 'selection'}
    a = assess(c, s, r)
    assert (a['page_count'], a['group_count'], a['independent_count'], a['relevant_count']) == (2,2,2,2)
    from agent_service.model import MAX_OUTPUT
    assert a['eligible'] and REVIEW_MAX_TOKENS <= MAX_OUTPUT
    a['review']['sources'][0]['cells'][0]['reason'] = 'changed'
    built['sources'][0]['text'] = 'changed'
    assert (c,s,r) == before


@pytest.mark.parametrize('case', ['extra', 'missing_pair', 'duplicate_pair', 'self_pair',
    'unknown_pair', 'bad_relation', 'long_reason', 'empty_reason', 'control_reason',
    'surrogate_reason', 'extra_source', 'duplicate_source', 'unknown_source', 'publisher_extra',
    'publisher_invented', 'publisher_not_literal', 'publisher_long', 'publisher_ambiguous',
    'cell_extra', 'cell_missing', 'cell_bad', 'cell_type', 'pair_type', 'source_type', 'root_type'])
def test_invalid_review_rejected(case):
    c,s,r = fixture()
    source = r['sources'][0]
    if case == 'extra': r['extra'] = 1
    elif case == 'missing_pair': r['pairs'] = []
    elif case == 'duplicate_pair':
        c,s,r = fixture(3); r['pairs'][1] = deepcopy(r['pairs'][0])
    elif case == 'self_pair': r['pairs'][0]['right'] = 's0'
    elif case == 'unknown_pair': r['pairs'][0]['right'] = 'unknown'
    elif case == 'bad_relation': r['pairs'][0]['relation'] = 'different'
    elif case == 'long_reason': source['cells'][0]['reason'] = 'x'*161
    elif case == 'empty_reason': source['cells'][0]['reason'] = ' '
    elif case == 'control_reason': source['cells'][0]['reason'] = 'x\n'
    elif case == 'surrogate_reason': source['cells'][0]['reason'] = '\ud800'
    elif case == 'extra_source': r['sources'].append(deepcopy(source))
    elif case == 'duplicate_source': r['sources'][1] = deepcopy(source)
    elif case == 'unknown_source': source['id'] = 'unknown'
    elif case == 'publisher_extra': source['publisher']['extra'] = 1
    elif case == 'publisher_invented': source['publisher']['quote'] = 'invented 名0😀'
    elif case == 'publisher_not_literal': source['publisher']['name'] = 'Other'
    elif case == 'publisher_long': source['publisher']['name'] = 'x'*81
    elif case == 'publisher_ambiguous': c['sources'][0]['text'] += 'Publisher 名0😀.'
    elif case == 'cell_extra': source['cells'][0]['extra'] = 1
    elif case == 'cell_missing': source['cells'] = []
    elif case == 'cell_bad': source['cells'][0]['status'] = 'missing'
    elif case == 'cell_type': source['cells'][0]['status'] = []
    elif case == 'pair_type': r['pairs'][0] = []
    elif case == 'source_type': r['sources'][0] = []
    elif case == 'root_type': r = []
    with pytest.raises(ComputerError, match='comparison_review_invalid'):
        assess(c,s,r)


@pytest.mark.parametrize('status', ['irrelevant', 'uncertain'])
def test_wrong_dimension_or_uncertainty_does_not_support_output(status):
    c,s,r = fixture()
    for source in r['sources']:
        source['cells'][0] = {'status': status, 'reason': '只是产品名，并非文件格式证据'}
    result = assess(c,s,r)
    assert not result['eligible'] and result['relevant_count'] == 0
    assert result['review'] == r


def test_missing_selection_requires_missing_status():
    c,s,r = fixture()
    s['sources'][0]['spans'][0] = None
    with pytest.raises(ComputerError): assess(c,s,r)
    r['sources'][0]['cells'][0]['status'] = 'missing'
    assert assess(c,s,r)['relevant_count'] == 1


@pytest.mark.parametrize('case', ['domain', 'publisher', 'translation', 'full_hash'])
def test_same_sources_merge_and_contradictions_never_increase_count(case):
    c,s,r = fixture()
    if case == 'domain': c['sources'][1]['url'] = 'https://es.brand0.example0/download'
    elif case == 'publisher':
        c['sources'][1]['text'] += ' Publisher 名0😀.'
        r['sources'][1]['publisher'] = deepcopy(r['sources'][0]['publisher'])
    elif case == 'translation': r['pairs'][0]['relation'] = 'same'
    elif case == 'full_hash': c['sources'][1]['sha256'] = c['sources'][0]['sha256']
    result = assess(c,s,r)
    assert result['group_count'] == 1 and result['independent_count'] <= 1
    assert not result['eligible']


def test_equal_snippets_with_different_complete_hashes_do_not_merge():
    c,s,r = fixture()
    c['sources'][1]['text'] = c['sources'][0]['text']
    s['sources'][1]['spans'] = deepcopy(s['sources'][0]['spans'])
    r['sources'][1]['publisher'] = None
    result = assess(c,s,r)
    assert result['group_count'] == 2 and result['independent_count'] == 1


def test_complete_normalized_hash_merges_even_when_raw_hash_differs():
    c,s,r = fixture()
    for source in c['sources']:
        source['normalized_sha256'] = 'a'*64
    result = assess(c,s,r)
    assert result['group_count'] == 1 and result['independent_count'] == 0


def test_conservative_two_label_suffix_can_merge_distinct_tenants():
    c,s,r = fixture()
    c['sources'][0]['url'] = 'https://brand-a.co.uk/p'
    c['sources'][1]['url'] = 'https://brand-b.co.uk/p'
    assert assess(c,s,r)['group_count'] == 1


def test_overlapping_publisher_occurrences_are_not_unique():
    c,s,r = fixture()
    c['sources'][0]['text'] += ' aaaa'
    r['sources'][0]['publisher'] = {'name': 'aa', 'quote': 'aaa'}
    with pytest.raises(ComputerError): assess(c,s,r)


def test_transitive_same_group_with_internal_independent_contradiction_is_excluded():
    c,s,r = fixture(3)
    r['pairs'][0]['relation'] = 'same'
    r['pairs'][2]['relation'] = 'same'
    result = assess(c,s,r)
    assert result['groups'] == [['s0','s1','s2']]
    assert result['independent_count'] == 0


def test_unknown_publisher_and_uncertain_pair_do_not_claim_independence():
    c,s,r = fixture()
    r['sources'][0]['publisher'] = None
    assert assess(c,s,r)['independent_count'] == 1
    r['sources'][0]['publisher'] = {'name': '名0😀', 'quote': 'Publisher 名0😀.'}
    r['pairs'][0]['relation'] = 'uncertain'
    assert assess(c,s,r)['independent_count'] == 1


def test_maximum_clique_and_all_cross_member_pairs_required():
    c,s,r = fixture(4)
    r['pairs'][0]['relation'] = 'same'  # s0+s1; publisher evidence remains distinct.
    next(p for p in r['pairs'] if (p['left'],p['right']) == ('s1','s2'))['relation'] = 'uncertain'
    result = assess(c,s,r)
    assert result['groups'] == [['s0','s1'], ['s2'], ['s3']]
    assert result['independent_count'] == 2


def test_reversed_pairs_and_sources_are_canonicalized():
    c,s,r = fixture(3)
    r['sources'].reverse(); r['pairs'].reverse()
    for p in r['pairs']: p['left'],p['right'] = p['right'],p['left']
    result = assess(c,s,r)
    assert [s['id'] for s in result['review']['sources']] == ['s0','s1','s2']
    assert result['independent_count'] == 3


@pytest.mark.parametrize('case', ['quote', 'id', 'dimensions', 'hash', 'selection_extra'])
def test_input_tampering_is_rejected(case):
    c,s,r = fixture()
    if case == 'quote': s['sources'][0]['spans'][0]['quote'] = 'invented'
    elif case == 'id': c['sources'][0]['id'] = []
    elif case == 'dimensions': c['dimensions'] = []
    elif case == 'hash': c['sources'][0]['sha256'] = 'bad'
    elif case == 'selection_extra': s['extra'] = 1
    with pytest.raises(ComputerError): build_review_context(c,s)
