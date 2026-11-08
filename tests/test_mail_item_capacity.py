"""Versioned item capacity and real bundle comparison of offline package fixtures.

The existing package/receipt fixture is synthetic; this is not native Office acceptance.
"""
from copy import deepcopy

import pytest

from agent_service import artifact_source, mail_understanding as mail, office_bundle
from test_artifact_source import new_task, owned
from test_mail_understanding import _context, _ready


def _candidate(schema, count, summaries=1):
    body = '\n'.join(f'推荐项目{i}：可考虑候选方案{i}。' for i in range(1, max(count, summaries) + 1))
    value = _ready(body=body, schema=schema)
    if schema == 1:
        value.pop('scope')
    data = _context(body)
    value['summary'] = [
        {'text': data['body_lines'][f'B{i}'],
         'evidence': [{'line': f'B{i}', 'quote': data['body_lines'][f'B{i}']}]}
        for i in range(1, summaries + 1)]
    value['items'] = [
        {'kind': '要点', 'task': data['body_lines'][f'B{i}'], 'owner': None, 'deadline': None,
         'evidence': [{'line': f'B{i}', 'quote': data['body_lines'][f'B{i}']}]}
        for i in range(1, count + 1)]
    return value, data


@pytest.mark.parametrize('schema', [1, 2, 3])
@pytest.mark.parametrize('count', [20, 21])
def test_legacy_schema_keeps_twenty_item_limit(schema, count):
    value, data = _candidate(schema, count)
    if count == 20:
        assert mail.validate(value, data, schema=schema) == value
    else:
        with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
            mail.validate(value, data, schema=schema)


@pytest.mark.parametrize('count', [21, 25, 26])
def test_schema4_accepts_up_to_twenty_five_without_truncation(count):
    value, data = _candidate(4, count)
    before = deepcopy(value)
    if count <= 25:
        normalized = mail.validate(value, data, schema=4)
        assert normalized['items'] == value['items'] and len(normalized['items']) == count
    else:
        with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
            mail.validate(value, data, schema=4)
    assert value == before


def test_twenty_five_items_five_summaries_compile_identical_tables_and_compare_198_positions(tmp_path):
    value, data = _candidate(4, 25, summaries=5)
    normalized = mail.validate(value, data, schema=4)
    steps = mail.compile_steps(normalized)
    sheet, document = [step['data'] for step in steps]
    assert {'headers': sheet['headers'], 'rows': sheet['rows']} == document['table']
    assert len(sheet['rows']) == 32 and len(sheet['headers']) == 6
    assert sheet['rows'][-2][1] == value['items'][-1]['task']

    # Reuse existing offline packages, then run production byte/source checks and compare.
    base, _task, _workflow, run = owned(tmp_path, sheet, second=document)
    inputs = []
    for receipt in run['receipts'].values():
        ref = artifact_source.prepare_artifact_source(receipt['artifact'], receipt['sha256'], base)
        snapshot = artifact_source.snapshot_artifact_source(ref, new_task(base), base)
        inputs.append({**snapshot, 'ref': ref})
    report = office_bundle.compare(inputs)
    assert report['positions'] == 198 and report['differences'] == []
    assert [(source['rows_including_header'], source['columns']) for source in report['sources']] == [(33, 6), (33, 6)]


@pytest.mark.parametrize('change,code', [
    ('four_quotes', 'mail_analysis_evidence_invalid'),
    ('wrong_quote', 'mail_analysis_evidence_invalid'),
    ('unknown_scope', 'mail_analysis_scope_invalid'),
    ('unknown_line', 'mail_analysis_scope_invalid'),
])
def test_larger_item_capacity_keeps_original_evidence_and_scope_checks(change, code):
    value, data = _candidate(4, 25)
    if change == 'four_quotes':
        value['items'][0]['evidence'] *= 4
    elif change == 'wrong_quote':
        value['items'][0]['evidence'][0]['quote'] = '原文没有的引句'
    elif change == 'unknown_scope':
        value['scope']['kind'] = 'unknown'
    else:
        value['scope'].update(kind='subset', lines=['B999'])
    with pytest.raises(mail.MailUnderstandingError, match=code):
        mail.validate(value, data, schema=4)


@pytest.mark.parametrize('summaries', [0, 6])
def test_schema4_office_summary_capacity_remains_one_to_five(summaries):
    value, data = _candidate(4, 25, summaries=summaries)
    with pytest.raises(mail.MailUnderstandingError, match='mail_analysis_schema_invalid'):
        mail.validate(value, data, schema=4)
