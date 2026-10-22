"""Offline v4 model transport and literal evidence compatibility."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations

import pytest

from agent_service import source_comparison as comparison
from agent_service.comparison_model_transport import call_model, MAX_INPUT_CHARS, MAX_OUTPUT_TOKENS
from agent_service.comparison_retention import retain_selection
from agent_service.comparison_review import build_review_context, assess, REVIEW_SYSTEM_V4, REVIEW_MAX_TOKENS
from agent_service.computer import ComputerError
from agent_service.model import MAX_OUTPUT


def fixture():
    goal = {'min_sources': 2, 'comparison': {'dimensions': ['file formats'],
                                           'min_domains': 2, 'evidence_version': 4}}
    accepted, selection, reviews = [], [], []
    for i in range(6):
        group = i // 3
        host = ['one.example', 'two.example'][group]
        sid = f'candidate:{i}:' + 'a' * 120
        text = f'Page {i}. Files support ODF{i}. '
        if i % 3 == 2:
            text += f'Published by Brand{group}.'
        page = {'title': f'Page{i}', 'text': text,
                'sha256': sha256(text.encode()).hexdigest(), 'captured_at': '2026-10-21T00:00:00+00:00'}
        accepted.append(({'id': sid, 'url': f'https://{host}/p{i}'}, {'observation': page}))
        selection.append({'id': sid, 'spans': [{'quote': f'Files support ODF{i}.'}]})
        reviews.append({'id': sid,
                        'publisher': {'name': f'Brand{group}', 'quote': f'Published by Brand{group}.'}
                                     if i % 3 == 2 else None,
                        'cells': [{'status': 'relevant', 'reason': 'Formats'}]})
    pairs = [{'left': selection[i]['id'], 'right': selection[j]['id'],
              'relation': 'same' if i // 3 == j // 3 else 'independent', 'reason': 'Attribution'}
             for i, j in combinations(range(6), 2)]
    return goal, accepted, {'sources': selection}, {'sources': reviews, 'pairs': pairs}


def aliased(value, canonical_ids):
    result = deepcopy(value)
    mapping = {sid: f's{i}' for i, sid in enumerate(canonical_ids)}
    for row in result['sources']:
        row['id'] = mapping[row['id']]
    for pair in result.get('pairs', []):
        for key in ('left', 'right'):
            pair[key] = mapping[pair[key]]
    return result


def test_six_pages_reconstruct_complete_groups_and_fresh_review_context():
    goal, accepted, selected, review = fixture()
    context = comparison.build_context(goal, accepted)
    review_context = build_review_context(context, selected)
    assert review_context['goal'] == goal
    assert assess(review_context, review_context['selection'], review)['eligible']
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    assert comparison.verify_evidence(goal, accepted, evidence) == evidence
    assert evidence['assessment']['complete_independent_count'] == 2
    assert len(comparison.table(goal, accepted, evidence)['rows']) == 6
    assert all(evidence['assessment']['publisher_support'].values())
    assert '跨站导航' in comparison.introduction(goal)
    assert 'Wiki contribution/license' in REVIEW_SYSTEM_V4


def test_v4_prefix_stays_8000_min_sources_stays_four():
    goal, accepted, selected, review = fixture()
    for _, record in accepted:
        page = record['observation']
        page['text'] += 'x' * 9000
        page['sha256'] = sha256(page['text'].encode()).hexdigest()
    context = comparison.build_context(goal, accepted)
    assert all(len(source['text']) == 8000 for source in context['sources'])
    assert len(build_review_context(context, selected)['sources']) == 6
    goal['min_sources'] = 5
    with pytest.raises(ComputerError): comparison.build_context(goal, accepted)


@pytest.mark.parametrize('case', ['absent', 'ambiguous', 'missing_status'])
def test_v4_retains_raw_invalid_literal_or_review_but_never_counts_it(case):
    goal, accepted, selected, review = fixture()
    if case == 'absent': selected['sources'][0]['spans'][0]['quote'] = 'not in page'
    elif case == 'ambiguous': selected['sources'][0]['spans'][0]['quote'] = ' '
    else: review['sources'][0]['cells'][0]['status'] = 'missing'
    if case == 'ambiguous':
        selected['sources'][0]['spans'][0]['quote'] = 'Page'
        accepted[0][1]['observation']['text'] += ' Page'
        page = accepted[0][1]['observation']
        page['sha256'] = sha256(page['text'].encode()).hexdigest()
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    assert evidence['raw_response'] == selected
    assert evidence['review'] == review
    cell = evidence['assessment']['cells'][selected['sources'][0]['id']][0]
    assert cell['status'] == ('uncertain' if case == 'missing_status' else 'missing')
    assert comparison.verify_evidence(goal, accepted, evidence) == evidence


def test_v4_cross_host_same_relation_does_not_transfer_identity():
    goal, accepted, selected, review = fixture()
    accepted[0][0]['url'] = 'https://wiki.one.example/format'
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    assert evidence['assessment']['publisher_support'][selected['sources'][0]['id']] is None
    assert not evidence['assessment']['eligible']
    assert evidence['assessment']['independent_count'] == 1


def test_v4_self_attributed_wiki_project_merges_without_ownership_claim():
    goal, accepted, selected, review = fixture()
    # Literal publication attribution from the locally captured ODF Wiki footer.
    quote = ('Please note that all contributions to The Document Foundation Wiki '
             'are considered to be released under the\n'
             'Creative Commons Attribution-ShareAlike 3.0 Unported License')
    page = accepted[2][1]['observation']
    page['text'] += ' ' + quote
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    accepted[2][0]['url'] = 'https://wiki.one.example/ODF'
    review['sources'][2]['publisher'] = {'name': 'The Document Foundation Wiki', 'quote': quote}
    # Other host pages require their own attribution; cross-host same never transfers it.
    root_page = accepted[0][1]['observation']
    root_page['text'] += ' Published by Brand0.'
    root_page['sha256'] = sha256(root_page['text'].encode()).hexdigest()
    review['sources'][0]['publisher'] = {'name': 'Brand0', 'quote': 'Published by Brand0.'}
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    assert evidence['assessment']['eligible']
    assert evidence['assessment']['publisher_evidence'][selected['sources'][2]['id']] == 'supported'
    assert evidence['assessment']['group_count'] == 2
    assert evidence['assessment']['complete_independent_count'] == 2
    review['sources'][2]['publisher']['quote'] = 'The Document Foundation Wiki'
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    assert evidence['assessment']['publisher_evidence'][selected['sources'][2]['id']] == 'bare_name'
    assert not evidence['assessment']['eligible']


def test_v4_retention_preserves_literal_and_review_is_fresh():
    goal, accepted, selected, review = fixture()
    evidence = comparison.create_evidence(goal, accepted, selected, review)
    missing = {'sources': [{'id': row['id'], 'spans': [None]} for row in selected['sources']]}
    result = retain_selection(goal, accepted, missing, goal, accepted, evidence)
    assert result['response'] == selected and len(result['retained']) == 6
    for row in review['sources']:
        row['cells'][0]['status'] = 'uncertain'
    fresh = comparison.create_evidence(goal, accepted, result['response'], review)
    assert not fresh['assessment']['eligible']


@pytest.mark.parametrize('reviewing', [False, True])
def test_transport_calls_once_maps_every_id_and_preserves_canonical_data(reviewing):
    goal, accepted, selected, review = fixture()
    context = comparison.build_context(goal, accepted)
    if reviewing: context = build_review_context(context, selected)
    ids = [row['id'] for row in context['sources']]
    context['goal']['comparison']['seed'] = {'private_run': 'C:/local/path'}
    before = deepcopy(context)
    response = aliased(review if reviewing else selected, ids)
    response_before = deepcopy(response)
    calls = []
    def callback(wire):
        calls.append(deepcopy(wire))
        assert [row['id'] for row in wire['sources']] == [f's{i}' for i in range(6)]
        assert 'seed' not in wire['goal']['comparison']
        if reviewing:
            assert [row['id'] for row in wire['selection']['sources']] == [f's{i}' for i in range(6)]
        wire['sources'][0]['text'] = 'callback mutation'
        return response
    assert call_model(context, callback) == (review if reviewing else selected)
    assert len(calls) == 1 and context == before and response == response_before
    assert MAX_INPUT_CHARS == 48000 and MAX_OUTPUT_TOKENS == REVIEW_MAX_TOKENS == MAX_OUTPUT == 2048


@pytest.mark.parametrize('case', ['unknown', 'canonical', 'duplicate', 'missing_source',
                                 'pair_unknown', 'duplicate_pair', 'missing_pair', 'self_pair'])
def test_transport_strictly_rejects_response_identity_errors_without_retry(case):
    goal, accepted, selected, review = fixture()
    context = comparison.review_context(goal, accepted, selected)
    response = aliased(review, [s['id'] for s in selected['sources']])
    if case == 'unknown': response['sources'][0]['id'] = 's8'
    elif case == 'canonical': response['sources'][0]['id'] = selected['sources'][0]['id']
    elif case == 'duplicate': response['sources'][0]['id'] = 's1'
    elif case == 'missing_source': response['sources'].pop()
    elif case == 'pair_unknown': response['pairs'][0]['left'] = 's9'
    elif case == 'duplicate_pair': response['pairs'][0] = deepcopy(response['pairs'][1])
    elif case == 'missing_pair': response['pairs'].pop()
    elif case == 'self_pair': response['pairs'][0]['left'] = response['pairs'][0]['right']
    calls = []
    def callback(wire):
        calls.append(wire)
        return response
    with pytest.raises(ComputerError, match='comparison_transport_invalid'):
        call_model(context, callback)
    assert len(calls) == 1


def test_transport_input_limit_rejects_before_model_without_cutting_source_text():
    goal, accepted, _, _ = fixture()
    context = comparison.build_context(goal, accepted)
    for source in context['sources']: source['text'] = 'x' * 8000
    calls = []
    with pytest.raises(ComputerError, match='model_input_invalid'):
        call_model(context, lambda wire: calls.append(wire))
    assert calls == [] and all(len(s['text']) == 8000 for s in context['sources'])


@pytest.mark.parametrize('version,digest', [
    (1, '9e933b13db8533f26f3fd98fdeca4926bde1f864b4ad0c0e986c633025e9a8fa'),
    (2, 'd7dbeff476537d4c65eb1b10251bf9a82544c8738ca7e145bf45d85399ebec89'),
    (3, '2f2564723e4af715e01f1b9cecb0ea87c64d4c2e041c0f8a245e831e18f0cac2'),
])
def test_legacy_evidence_hash_matches_pre_v4_baseline(version, digest):
    # Digests independently compared with HEAD's source/review implementations.
    from test_source_comparison import reviewed_fixture
    goal, accepted, selected, review = reviewed_fixture()
    if version == 1:
        del goal['comparison']['evidence_version']
    else:
        goal['comparison']['evidence_version'] = version
    evidence = comparison.create_evidence(goal, accepted, selected, None if version == 1 else review)
    assert comparison._digest(evidence) == digest
