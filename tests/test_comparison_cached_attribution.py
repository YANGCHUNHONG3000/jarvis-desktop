"""Versioned cached extraction keeps literal identity without changing old reads."""
from copy import deepcopy
from hashlib import sha256

import pytest

from agent_service.comparison_cached_reader import CachedNavigationReader, _capture, validate_record
from agent_service.comparison_evidence_discovery import cached_page
from agent_service.computer import ComputerError, digest
from agent_service.project_documents import extract
from test_comparison_evidence_discovery import history, page, AT

URL = 'https://xlsxwriter.readthedocs.io/working_with_formulas.html'
QUERY = 'XlsxWriter formula calculation'


def long_history(footer='<footer>© Copyright 2013 - 2026, John McNamara.</footer>'):
    paragraphs = ''.join('<p>Formula calculation results: '+str(i)+' '+('formula results '*50)+'</p>'
                         for i in range(70))
    body = '<html><title>Working with Formulas</title><body><p>XlsxWriter doesn’t calculate the result of a formula.</p>'+paragraphs+footer+'</body></html>'
    return history([page(URL, body, 2)])


def record_for(prior, kind='navigation_capture_v3'):
    observation, proof = _capture(URL, QUERY, prior, kind=kind)
    return {'observation': observation.to_dict(), 'document_proof': proof}


def test_existing_v1_exact_extraction_and_proof_replay_stays_unchanged():
    prior = long_history()
    retained = cached_page(URL, prior)
    legacy = extract(URL, retained['mime'], retained['content'], QUERY, AT)
    record = record_for(prior, 'navigation_capture_v1')
    assert record['observation'] == legacy.to_dict()
    assert set(record['document_proof']) == {'kind', 'url', 'query', 'prior_sha256', 'ref', 'source_sha256'}
    assert 'John McNamara' not in legacy.text
    assert validate_record(record, URL, QUERY, prior)
    unchanged = deepcopy(record)
    reader = CachedNavigationReader(None, QUERY, prior)
    assert reader.read(URL).text != legacy.text
    assert record == unchanged


def test_new_default_preserves_negative_formula_sentence_and_literal_footer():
    prior = long_history()
    reader = CachedNavigationReader(None, QUERY, prior)
    observation = reader.read(URL)
    assert reader.last_document_proof['kind'] == 'navigation_capture_v3'
    assert 'XlsxWriter doesn’t calculate the result of a formula.' in observation.text
    assert '© Copyright 2013 - 2026, John McNamara.' in observation.text
    assert '[Additional literal attribution passages from the same captured page]' in observation.text
    assert len(observation.text) <= 8000 and observation.captured_at == AT
    record = {'observation': observation.to_dict(), 'document_proof': reader.last_document_proof}
    assert validate_record(record, URL, QUERY, prior)


def test_literal_span_offsets_and_full_text_hash_reconstruct_exact_source_text():
    prior = long_history()
    record = record_for(prior)
    retained = cached_page(URL, prior)
    from agent_service.project_documents import _soup
    soup = _soup(retained['content'])
    text = (soup.body or soup).get_text('\n', strip=True).strip()
    proof = record['document_proof']
    assert proof['full_text_sha256'] == sha256(text.encode()).hexdigest()
    for span in proof['attribution_spans']:
        assert text[span['start']:span['end']] == span['text']
        assert span['text'] in record['observation']['text']


@pytest.mark.parametrize('footer', [
    '<dl><dt>Author</dt><dd>:</dd><dd>Eric Gazoni, Charlie Clark</dd></dl>',
    '<footer><span>©</span><span>Copyright 2026, John McNamara.</span></footer>',
    '<footer>Copyright 2026, John McNamara.</footer>',
])
def test_split_html_marker_keeps_following_literal_name_lines(footer):
    prior = long_history(footer)
    record = record_for(prior)
    assert any(name in record['observation']['text'] for name in ('John McNamara', 'Eric Gazoni'))
    assert validate_record(record, URL, QUERY, prior)


def test_placeholder_preserved_as_literal_without_inventing_a_name():
    prior = long_history('<footer>© Copyright 2010 - 2024, See AUTHORS.</footer>')
    record = record_for(prior)
    assert 'See AUTHORS.' in record['observation']['text']
    assert 'John McNamara' not in record['observation']['text']
    assert 'publisher' not in record['document_proof']


def test_large_many_attribution_lines_respect_both_budgets():
    footer = ''.join('<p>Copyright 2026, '+('Literal Person '*300)+'</p>' for _ in range(20))
    record = record_for(long_history(footer))
    spans = record['document_proof']['attribution_spans']
    assert sum(len(s['text']) for s in spans) <= 2000
    assert len(spans) <= 8 and len(record['observation']['text']) <= 8000


@pytest.mark.parametrize('change', [
    lambda r: r['document_proof']['attribution_spans'][0].__setitem__('start', 0),
    lambda r: r['document_proof']['attribution_spans'][0].__setitem__('text', 'Author: Invented Name'),
    lambda r: r['document_proof'].__setitem__('full_text_sha256', '0'*64),
    lambda r: r['document_proof'].__setitem__('source_sha256', '0'*64),
    lambda r: r['document_proof'].__setitem__('query', 'different query'),
    lambda r: r['document_proof'].__setitem__('ref', {'prior': 0, 'page': 5, 'sha256': '0'*64}),
    lambda r: r['document_proof'].__setitem__('kind', 'navigation_capture_v1'),
    lambda r: r['document_proof'].__setitem__('kind', 'navigation_capture_v2'),
    lambda r: r['observation'].__setitem__('text', r['observation']['text']+' invented owner'),
])
def test_modified_literal_identity_or_binding_fails_replay(change):
    prior = long_history()
    record = record_for(prior)
    change(record)
    with pytest.raises(ComputerError):
        validate_record(record, URL, QUERY, prior)


def test_plain_text_marker_follows_same_source_offset_contract():
    prior = history([page(URL, 'XlsxWriter does not calculate formulas.\n'+'ordinary prose '*2000+
                          '\nAuthor:\nJohn McNamara\n', 1)])
    prior[0]['proof']['pages'][0]['mime'] = 'text/plain'
    record = record_for(prior)
    assert 'Author:\nJohn McNamara' in record['observation']['text']
    assert validate_record(record, URL, QUERY, prior)


def test_script_or_template_name_is_not_inserted_as_page_attribution():
    prior = long_history('<script>Author: Script Person</script><template>Copyright Fake Person</template>'
                         '<footer>Copyright 2026, John McNamara.</footer>')
    record = record_for(prior)
    assert 'John McNamara' in record['observation']['text']
    assert 'Script Person' not in record['observation']['text']
    assert 'Fake Person' not in record['observation']['text']


def test_v2_exact_long_page_golden_record_still_replays_unchanged():
    prior = long_history()
    record = record_for(prior, 'navigation_capture_v2')
    # Recorded from v2 before the 8000-character review-window repair.
    assert digest(record) == '2167daadf4362dca001bacbe297da1798c35edbf19d8a405c8576ed76ae182cc'
    assert len(record['observation']['text']) == 9111
    assert validate_record(record, URL, QUERY, prior)


def test_actual_build_context_preserves_direct_negative_and_copyright_in_v3():
    from agent_service.source_comparison import build_context
    prior = long_history()
    goal = {'min_sources': 1, 'comparison': {'dimensions': ['Does the library calculate formula results?'],
                                           'min_domains': 1, 'evidence_version': 4}}
    item = {'id': digest(URL)[:24], 'url': URL}
    v2 = record_for(prior, 'navigation_capture_v2')
    legacy_context = build_context(goal, [(item, v2)])
    assert 'John McNamara' not in legacy_context['sources'][0]['text']
    v3 = record_for(prior)
    context = build_context(goal, [(item, v3)])
    supplied = context['sources'][0]['text']
    assert len(supplied) <= 8000 and supplied == v3['observation']['text']
    assert 'XlsxWriter doesn’t calculate the result of a formula.' in supplied
    assert '© Copyright 2013 - 2026, John McNamara.' in supplied
    assert validate_record(v3, URL, QUERY, prior)


def test_v3_attribution_spans_and_query_excerpt_each_keep_their_own_bounds():
    prior = long_history('<footer>Copyright 2026, '+('Long Literal Name '*200)+'</footer>')
    record = record_for(prior)
    spans = record['document_proof']['attribution_spans']
    primary = record['observation']['text'].split('\n\n[Additional literal attribution passages', 1)[0]
    assert len(primary) <= 5600
    assert sum(len(span['text']) for span in spans) <= 2000
    assert len(record['observation']['text']) <= 8000
    assert validate_record(record, URL, QUERY, prior)
