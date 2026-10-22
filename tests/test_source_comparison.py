"""Offline validation of model-selected literal source quotations."""

from copy import deepcopy
from hashlib import sha256

import pytest

from agent_service.computer import ComputerError
from agent_service import source_comparison as comparison


def reviewed_fixture():
    objective, sources = goal(), accepted()
    objective['comparison']['evidence_version'] = 2
    for i, (_, record) in enumerate(sources):
        page = record['observation']
        page['text'] += f' Publisher Brand{i}.'
        page['sha256'] = sha256(page['text'].encode()).hexdigest()
    selection = response()
    review = {'sources':[{'id':f'c{i}',
                'publisher':{'name':f'Brand{i}', 'quote':f'Publisher Brand{i}.'},
                'cells':[{'status':'irrelevant' if i == 0 else 'relevant', 'reason':'维度判断'},
                         {'status':'missing','reason':'缺少证据'}]} for i in range(2)],
              'pairs':[{'left':'c0','right':'c1','relation':'independent','reason':'不同发布方'}]}
    return objective, sources, selection, review


def test_v2_unrelated_quote_is_auditable_but_not_reported_as_conclusion():
    g, s, selected, review = reviewed_fixture()
    evidence = comparison.create_evidence(g, s, selected, review)
    assert evidence['version'] == 2
    assert comparison.verify_evidence(g, s, evidence) == evidence
    assert evidence['response'] == selected
    output = comparison.table(g, s, evidence)
    assert output['rows'][0][2] == '待补证（与维度不相关）：维度判断'
    assert '原文[' not in output['rows'][0][2]
    assert output['rows'][1][2].startswith('相关性通过（模型复核）')
    assert '2页/2来源组/2独立组' in output['rows'][0][-1]
    assert '不是人工事实确认' in comparison.introduction(g)
    assert comparison.introduction(goal()) == comparison.WORD_INTRO


@pytest.mark.parametrize('mutation', ['context','selection','review_input','review_hash','review',
                                    'assessment','bool_count','version','downgrade','missing_review','extra'])
def test_v2_review_is_bound_to_goal_selection_and_assessment(mutation):
    g,s,selected,review = reviewed_fixture()
    e = comparison.create_evidence(g,s,selected,review)
    if mutation == 'context': g['comparison']['dimensions'][0] = 'changed'
    elif mutation == 'selection': e['response']['sources'][0]['spans'][0] = None
    elif mutation == 'review_input': e['review_context_sha256'] = '0'*64
    elif mutation == 'review_hash': e['review_sha256'] = '0'*64
    elif mutation == 'review': e['review']['sources'][0]['cells'][0]['status'] = 'relevant'
    elif mutation == 'assessment': e['assessment']['independent_count'] = 1
    elif mutation == 'bool_count': e['assessment']['relevant_count'] = True
    elif mutation == 'version': e['version'] = 2.0
    elif mutation == 'downgrade': e = comparison.create_evidence(goal(),s,selected)
    elif mutation == 'missing_review': del e['review']
    elif mutation == 'extra': e['unknown'] = 1
    with pytest.raises(ComputerError): comparison.verify_evidence(g,s,e)


@pytest.mark.parametrize('failure', ['semantic','independence'])
def test_negative_review_receipt_is_preserved_but_office_table_refused(failure):
    g,s,selected,review = reviewed_fixture()
    if failure == 'semantic': review['sources'][1]['cells'][0]['status'] = 'uncertain'
    else: review['pairs'][0]['relation'] = 'same'
    e = comparison.create_evidence(g,s,selected,review)
    assert comparison.verify_evidence(g,s,e) == e
    code = 'comparison_' + failure + '_insufficient'
    assert comparison.review_failure(e) == code
    with pytest.raises(ComputerError, match=code): comparison.table(g,s,e)


def test_explicit_one_group_report_keeps_other_publisher_unknown():
    g,s,selected,review = reviewed_fixture()
    g['comparison']['min_domains'] = 1
    review['sources'][1]['publisher'] = None
    e = comparison.create_evidence(g,s,selected,review)
    assert e['assessment']['independent_count'] == 1
    output = comparison.table(g,s,e)
    assert '2页/2来源组/1独立组' in output['rows'][1][-1]
    assert output['rows'][1][-1].endswith('本页发布方缺证')
    assert '本页发布方缺证' not in output['rows'][0][-1]


@pytest.mark.parametrize('version', [True,1,2.0,3.0,4.0,5,'2',None])
def test_comparison_version_is_exact(version):
    g = goal()['comparison']; g['evidence_version'] = version
    with pytest.raises(ComputerError): comparison.validate_comparison(g)


def goal():
    return {'queries': ['keyboard', 'mouse'], 'title': '采购核对', 'min_sources': 2,
            'required_terms': [], 'outputs': ['spreadsheet', 'document'],
            'comparison': {'dimensions': ['价格', '规格'], 'min_domains': 2}}


def accepted():
    result = []
    for index, host in enumerate(['www.adafruit.com', 'logitech.com']):
        url = f'https://{host}/product/{index}'
        content = '=SUM(A1:A2)\n价格 $25\t尺寸：中文😀\r\nMore captured text.'
        result.append(({'id': f'c{index}', 'url': url},
                       {'observation': {'title': f'=候选{index}', 'text': content,
                                        'sha256': sha256(content.encode()).hexdigest(),
                                        'captured_at': '2026-10-20T12:00:00+00:00',
                                        'requested_url': url, 'final_url': url},
                        'excerpt': {'start': 0, 'end': len(content), 'text': content}}))
    return result


def response():
    return {'sources': [{'id': f'c{i}', 'spans': [{'quote': accepted()[i][1]['observation']['text'][:25]}, None]}
                        for i in range(2)]}


def test_context_is_deterministic_bounded_and_copied():
    sources, objective = accepted(), goal()
    page = sources[0][1]['observation']
    page['text'] = '中😀' * 6000
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    before = deepcopy((objective, sources))
    context = comparison.build_context(objective, sources)
    assert context == comparison.build_context(objective, sources)
    assert len(context['sources'][0]['text']) == 6000
    assert context['sources'][0]['sha256'] == page['sha256']
    assert context['dimensions'] == ['价格', '规格']
    context['goal']['outputs'].append('other')
    context['dimensions'].append('other')
    assert (objective, sources) == before


@pytest.mark.parametrize('value', [None, [], {}, {'dimensions': ['价格'], 'min_domains': 1, 'other': 0},
    {'dimensions': [], 'min_domains': 1}, {'dimensions': ['a', 'b', 'c', 'd'], 'min_domains': 1},
    {'dimensions': ['A', ' a '], 'min_domains': 1}, {'dimensions': [' '], 'min_domains': 1},
    {'dimensions': ['a\n'], 'min_domains': 1}, {'dimensions': ['a' * 61], 'min_domains': 1},
    {'dimensions': [True], 'min_domains': 1}, {'dimensions': ['a'], 'min_domains': True},
    {'dimensions': ['a'], 'min_domains': 0}, {'dimensions': ['a'], 'min_domains': 3}])
def test_comparison_schema_is_exact(value):
    with pytest.raises(ComputerError):
        comparison.validate_comparison(value)


def test_comparison_accepts_up_to_three_distinct_plain_dimensions():
    value = {'dimensions': ['价格', '连接', '尺寸'], 'min_domains': 2}
    assert comparison.validate_comparison(value) == value
    comparison.validate_comparison(value)['dimensions'].append('x')
    assert len(value['dimensions']) == 3


def test_domain_readiness_and_legacy_count_only():
    objective, sources = goal(), accepted()
    assert comparison.site_key('https://WWW.Example.COM:443/path') == 'example.com'
    assert comparison.site_key('https://sub.example.com/a') == 'sub.example.com'
    assert comparison.sources_ready(objective, sources)
    sources[1][0]['url'] = 'https://adafruit.com/another'
    assert not comparison.sources_ready(objective, sources)
    objective['comparison']['min_domains'] = 1
    assert comparison.sources_ready(objective, sources)
    assert not comparison.sources_ready(objective, sources[:1])
    del objective['comparison']
    assert comparison.sources_ready(objective, [({}, {}), ({}, {})])


@pytest.mark.parametrize('url', ['https://', 'file:///path', 'https://user:pass@example.com',
                                      'https://example.com:bad', 'https://example.com/\n'])
def test_invalid_source_url_rejected(url):
    with pytest.raises(ComputerError):
        comparison.site_key(url)


@pytest.mark.parametrize('mutation', ['extra_root', 'missing_root', 'unknown_id', 'duplicate_id',
    'missing_id', 'extra_source', 'extra_field', 'missing_spans', 'dimension_count', 'tuple_spans',
    'scalar_span', 'extra_span_field', 'missing_quote', 'bool_quote', 'number_quote',
    'empty', 'normalized', 'invented', 'legacy_offsets', 'string_id', 'unhashable_id'])
def test_response_rejects_unknown_fields_ids_and_invalid_quotes(mutation):
    value = response()
    entry, span = value['sources'][0], value['sources'][0]['spans'][0]
    if mutation == 'extra_root': value['other'] = 0
    elif mutation == 'missing_root': value = {}
    elif mutation == 'unknown_id': entry['id'] = 'unknown'
    elif mutation == 'duplicate_id': value['sources'][1]['id'] = 'c0'
    elif mutation == 'missing_id': value['sources'].pop()
    elif mutation == 'extra_source': value['sources'].append(deepcopy(entry))
    elif mutation == 'extra_field': entry['text'] = 'invented'
    elif mutation == 'missing_spans': del entry['spans']
    elif mutation == 'dimension_count': entry['spans'].pop()
    elif mutation == 'tuple_spans': entry['spans'] = tuple(entry['spans'])
    elif mutation == 'scalar_span': entry['spans'][0] = 3
    elif mutation == 'extra_span_field': span['text'] = 'invented'
    elif mutation == 'missing_quote': del span['quote']
    elif mutation == 'bool_quote': span['quote'] = True
    elif mutation == 'number_quote': span['quote'] = 3
    elif mutation == 'empty': span['quote'] = ''
    elif mutation == 'normalized': span['quote'] = span['quote'].replace('\n', ' ')
    elif mutation == 'invented': span['quote'] = 'no such quotation'
    elif mutation == 'legacy_offsets': entry['spans'][0] = {'start': 0, 'end': 5}
    elif mutation == 'string_id': entry['id'] = 1
    elif mutation == 'unhashable_id': entry['id'] = []
    with pytest.raises(ComputerError):
        comparison.create_evidence(goal(), accepted(), value)


def test_span_unicode_bounds_length_and_whitespace():
    sources = accepted()
    page = sources[0][1]['observation']
    page['text'] = '😀中' + 'x' * 500 + ' \n\t' + 'y' * 6500
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    value = response()
    value['sources'][0]['spans'][0] = {'quote': page['text'][:400]}
    evidence = comparison.create_evidence(goal(), sources, value)
    assert comparison.table(goal(), sources, evidence)['rows'][0][2].startswith('原文[0,400)：😀中')
    for quote in [page['text'][:401], ' \n\t', page['text'][5999:6001], 'xxx']:
        value['sources'][0]['spans'][0] = {'quote': quote}
        with pytest.raises(ComputerError):
            comparison.create_evidence(goal(), sources, value)


@pytest.mark.parametrize('mutation', ['goal', 'dimension', 'id', 'url', 'title', 'capture',
                                    'content', 'content_hash', 'response', 'context_hash',
                                    'response_hash', 'extra_evidence', 'unbound'])
def test_evidence_rejects_tampering_and_changed_inputs(mutation):
    objective, sources = goal(), accepted()
    evidence = comparison.create_evidence(objective, sources, response())
    if mutation == 'goal': objective['title'] = 'changed'
    elif mutation == 'dimension': objective['comparison']['dimensions'][0] = 'changed'
    elif mutation == 'id': sources[0][0]['id'] = 'changed'
    elif mutation == 'url': sources[0][0]['url'] += '/changed'
    elif mutation == 'title': sources[0][1]['observation']['title'] = 'changed'
    elif mutation == 'capture': sources[0][1]['observation']['captured_at'] = '2026-10-21T12:00:00+00:00'
    elif mutation in {'content', 'content_hash'}:
        page = sources[0][1]['observation']
        page['text'] += 'changed'
        if mutation == 'content_hash': page['sha256'] = sha256(page['text'].encode()).hexdigest()
    elif mutation == 'response': evidence['response']['sources'][0]['spans'][0]['quote'] = '=SUM(A1:A2)'
    elif mutation == 'context_hash': evidence['context_sha256'] = '0' * 64
    elif mutation == 'response_hash': evidence['response_sha256'] = '0' * 64
    elif mutation == 'extra_evidence': evidence['other'] = 1
    elif mutation == 'unbound': del evidence['context_sha256']
    with pytest.raises(ComputerError):
        comparison.verify_evidence(objective, sources, evidence)


def test_full_hash_binds_content_beyond_model_window():
    sources = accepted()
    page = sources[0][1]['observation']
    page['text'] += 'x' * 6000 + 'tail'
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    evidence = comparison.create_evidence(goal(), sources, response())
    page['text'] += 'changed'
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    with pytest.raises(ComputerError):
        comparison.verify_evidence(goal(), sources, evidence)


def test_table_retains_literal_formula_like_text_and_traceability():
    objective, sources, value = goal(), accepted(), response()
    value['sources'].reverse()
    evidence = comparison.create_evidence(objective, sources, value)
    assert evidence['response']['sources'][0]['id'] == 'c0'
    assert comparison.verify_evidence(objective, sources, evidence) == evidence
    output = comparison.table(objective, sources, evidence)
    assert len(output['headers']) == 7
    assert output['headers'][2:4] == ['价格（原文引句）', '规格（原文引句）']
    for row, (item, record) in zip(output['rows'], sources):
        assert row[0] == record['observation']['title'] == '=候选' + item['id'][1:]
        assert row[1] == comparison.site_key(item['url'])
        expected = record['observation']['text'][:25].replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
        assert row[2] == '原文[0,25)：' + expected
        assert row[3] == '未找到原文引句（待人工核对）'
        assert row[-3:] == [item['url'], record['observation']['captured_at'], '待人工核对']
        assert all(type(cell) is str and len(cell) <= 2000 for cell in row)
    assert '人工核对' in comparison.WORD_INTRO and '价格' in comparison.WORD_INTRO


def test_four_sources_and_three_dimensions_stay_bounded():
    sources, objective = accepted(), goal()
    for index in (2, 3):
        item, record = deepcopy(sources[0])
        item['id'] = f'c{index}'
        sources.append((item, record))
    objective['comparison']['dimensions'].append('库存')
    value = {'sources': [{'id': item['id'], 'spans': [None] * 3} for item, _ in sources]}
    evidence = comparison.create_evidence(objective, sources, value)
    output = comparison.table(objective, sources, evidence)
    assert len(output['rows']) == 4 and len(output['headers']) == 8
    with pytest.raises(ComputerError):
        comparison.build_context(objective, sources + [deepcopy(sources[0])])
