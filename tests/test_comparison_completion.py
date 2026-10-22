"""Offline v3 completion contracts; captures and Office backends are fixtures."""
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit
import json

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service import source_comparison as comparison
from agent_service.comparison_review import assess
from agent_service.comparison_completion import CompletionEnvironment
from agent_service.computer import ComputerError, digest, read_record
from test_comparison_loop import ctx, run
from test_comparison_review import fixture
from test_computer_agent_tools import ctx as tool_ctx, A, B, STAMP
from test_source_comparison import accepted, goal, reviewed_fixture


def grouped():
    c, selected, review = fixture(4)
    c['goal']['comparison']['evidence_version'] = 3
    c['dimensions'] = ['format', 'price']
    for i, source in enumerate(c['sources']):
        group = i // 2
        source['url'] = f'https://brand{group}.example{group}/page{i}'
        source['text'] = f'Publisher Group{group}. Fact{i}.'
        source['sha256'] = sha256(source['text'].encode()).hexdigest()
        selected['sources'][i]['spans'] = [{'quote': f'Fact{i}.'}] * 2
        review['sources'][i].update(publisher={'name': f'Group{group}',
            'quote': f'Publisher Group{group}.'},
            cells=[{'status': 'relevant', 'reason': 'Direct evidence'} for _ in range(2)])
    for pair in review['pairs']:
        pair['relation'] = 'same' if int(pair['left'][1:]) // 2 == int(pair['right'][1:]) // 2 else 'independent'
    return c, selected, review


def test_each_counted_group_must_cover_all_dimensions():
    c, s, r = grouped()
    for i, row in enumerate(r['sources']):
        row['cells'][1 if i < 2 else 0]['status'] = 'irrelevant'
    result = assess(c, s, r)
    assert result['independent_count'] == 2
    assert result['complete_independent_count'] == 0 and not result['eligible']
    assert [g['covered_dimensions'] for g in result['group_details']] == [[True, False], [False, True]]
    assert all(not g['complete'] for g in result['group_details'])


def test_group_can_combine_complementary_pages_and_exact_host_publisher_support():
    c, s, r = grouped()
    for i, row in enumerate(r['sources']):
        row['cells'][i % 2]['status'] = 'irrelevant'
        if i % 2:
            row['publisher'] = None
    result = assess(c, s, r)
    assert result['eligible'] and result['complete_independent_count'] == 2
    assert result['publisher_support'] == {'s0': 's0', 's1': 's0', 's2': 's2', 's3': 's2'}
    assert all(g['publisher_supported'] and g['complete'] for g in result['group_details'])


@pytest.mark.parametrize('host', ['sub.brand0.example0', 'other.example0'])
def test_publisher_support_does_not_cross_exact_hostname(host):
    c, s, r = grouped()
    c['sources'][1]['url'] = f'https://{host}/page1'
    r['sources'][1]['publisher'] = None
    result = assess(c, s, r)
    assert result['publisher_support']['s1'] is None
    assert not result['eligible'] and result['complete_independent_count'] == 1


def test_v2_assessment_contract_remains_byte_compatible():
    c, s, r = grouped()
    c['goal']['comparison']['evidence_version'] = 2
    before = deepcopy((c, s, r))
    result = assess(c, s, r)
    assert set(result) == {'review', 'groups', 'page_count', 'group_count',
                           'independent_count', 'relevant_count', 'eligible', 'cells'}
    assert result['eligible'] and (c, s, r) == before


def test_v3_context_extends_original_prefix_without_changing_v2_window():
    objective, sources = goal(), accepted()
    page = sources[0][1]['observation']
    page['text'] = 'x' * 7000 + 'Publisher anchor.' + 'y' * 2000
    page['sha256'] = sha256(page['text'].encode()).hexdigest()
    objective['comparison']['evidence_version'] = 3
    current = comparison.build_context(objective, sources)
    assert current['sources'][0]['text'] == page['text'][:8000]
    objective['comparison']['evidence_version'] = 2
    historical = comparison.build_context(objective, sources)
    assert historical['sources'][0]['text'] == page['text'][:6000]
    assert 'Publisher anchor.' not in historical['sources'][0]['text']


@pytest.mark.parametrize('mutation', ['support', 'coverage_type', 'count_type', 'version',
                                     'publisher_status', 'required_count'])
def test_v3_assessment_and_support_are_bound_to_evidence(mutation):
    objective, sources, selected, review = reviewed_fixture()
    objective['comparison']['evidence_version'] = 3
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assert comparison.verify_evidence(objective, sources, evidence) == evidence
    if mutation == 'support':
        evidence['assessment']['publisher_support']['c0'] = 'c1'
    elif mutation == 'coverage_type':
        evidence['assessment']['group_details'][0]['covered_dimensions'][0] = 0
    elif mutation == 'count_type':
        evidence['assessment']['complete_independent_count'] = False
    elif mutation == 'publisher_status':
        evidence['assessment']['publisher_evidence']['c0'] = 'absent'
    elif mutation == 'required_count':
        evidence['assessment']['required_independent_count'] = 1
    else:
        evidence['version'] = 2
    with pytest.raises(ComputerError):
        comparison.verify_evidence(objective, sources, evidence)


@pytest.mark.parametrize('status', ['absent', 'ambiguous', 'bare_name', 'name_absent'])
def test_v3_unsupported_publisher_proof_remains_auditable_and_derives_repair(status):
    context, selected, review = fixture()
    context['goal'].update(queries=['first', 'second'])
    context['goal']['comparison'].update(evidence_version=3, dimensions=context['dimensions'])
    publisher = review['sources'][0]['publisher']
    if status == 'absent':
        publisher['quote'] = 'Unavailable publisher ' + publisher['name']
    elif status == 'ambiguous':
        context['sources'][0]['text'] += ' ' + publisher['quote']
    elif status == 'name_absent':
        publisher['name'] = 'Missing Publisher Name'
    else:
        publisher['quote'] = publisher['name']
    original = deepcopy(review)
    assessment = assess(context, selected, review)
    assert assessment['review'] == original == review
    assert assessment['publisher_evidence']['s0'] == status
    assert assessment['publisher_support']['s0'] is None
    assert assessment['independent_count'] == assessment['complete_independent_count'] == 1
    assert not assessment['eligible']
    env = object.__new__(CompletionEnvironment)
    env.goal = context['goal']
    facts = {'candidates': [{**source, 'query_index': i} for i, source in enumerate(context['sources'])]}
    needs = env._needs(facts, {'assessment': assessment})
    assert len(needs) == 1 and needs[0]['host'] == 'brand0.example0'
    assert 'about company' in needs[0]['query']


@pytest.mark.parametrize('status', ['absent', 'ambiguous', 'name_absent'])
def test_saved_v2_still_rejects_unsupported_publisher_quotes(status):
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = 2
    if status == 'absent':
        review['sources'][0]['publisher']['quote'] += ' absent'
    elif status == 'name_absent':
        review['sources'][0]['publisher']['name'] = 'Missing Publisher Name'
    else:
        context['sources'][0]['text'] += review['sources'][0]['publisher']['quote']
    with pytest.raises(ComputerError, match='comparison_review_invalid'):
        assess(context, selected, review)


def test_saved_v2_bare_name_behavior_is_preserved():
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = 2
    review['sources'][0]['publisher']['quote'] = review['sources'][0]['publisher']['name']
    assert assess(context, selected, review)['eligible']


@pytest.mark.parametrize('version', [2, 3])
def test_navigation_menu_publisher_is_unusable_only_for_v3(version):
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = version
    publisher = review['sources'][0]['publisher']
    publisher['quote'] = publisher['name'] + '\nAbout\nWho Are We'
    context['sources'][0]['text'] += '\n' + publisher['quote']
    original = deepcopy(review)
    assessment = assess(context, selected, review)
    assert assessment['review'] == original == review
    if version == 3:
        assert assessment['publisher_evidence']['s0'] == 'navigation'
        assert assessment['publisher_support']['s0'] is None
        assert assessment['independent_count'] == 1 and not assessment['eligible']
    else:
        assert assessment['independent_count'] == 2 and assessment['eligible']
        assert 'publisher_evidence' not in assessment


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('case', ['null_relevant', 'null_irrelevant', 'null_uncertain', 'present_missing'])
def test_v3_cell_span_mismatch_downgrades_without_changing_raw_review(version, case):
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = version
    if case.startswith('null_'):
        selected['sources'][0]['spans'][0] = None
        review['sources'][0]['cells'][0]['status'] = case.removeprefix('null_')
        effective = 'missing'
    else:
        review['sources'][0]['cells'][0]['status'] = 'missing'
        effective = 'uncertain'
    original = deepcopy(review)
    if version == 2:
        with pytest.raises(ComputerError, match='comparison_review_invalid'):
            assess(context, selected, review)
        return
    assessment = assess(context, selected, review)
    assert assessment['review'] == original == review
    assert assessment['cells']['s0'][0]['status'] == effective
    assert assessment['relevant_count'] == 1
    assert assessment['complete_independent_count'] == 1 and not assessment['eligible']
    assert not assessment['group_details'][0]['complete']


def test_v3_unknown_cell_status_is_not_coerced_for_null_selection():
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = 3
    selected['sources'][0]['spans'][0] = None
    review['sources'][0]['cells'][0]['status'] = 'unsupported'
    with pytest.raises(ComputerError, match='comparison_review_invalid'):
        assess(context, selected, review)


@pytest.mark.parametrize('version', [2, 3])
def test_download_product_heading_is_not_v3_publisher_evidence(version):
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = version
    publisher = review['sources'][0]['publisher']
    publisher['quote'] = 'Download free ' + publisher['name']
    context['sources'][0]['text'] += '\n' + publisher['quote']
    original = deepcopy(review)
    assessment = assess(context, selected, review)
    assert assessment['review'] == original == review
    if version == 3:
        assert assessment['publisher_evidence']['s0'] == 'download_label'
        assert assessment['publisher_support']['s0'] is None
        assert assessment['complete_independent_count'] == 1 and not assessment['eligible']
    else:
        assert assessment['eligible'] and assessment['independent_count'] == 2


def downgraded_table_fixture():
    objective, sources, selected, review = reviewed_fixture()
    objective['comparison'].update(evidence_version=3, min_domains=1)
    selected['sources'][0]['spans'] = [None, {'quote': review['sources'][0]['publisher']['quote']}]
    review['sources'][0]['cells'][0]['status'] = 'relevant'
    selected['sources'][1]['spans'][1] = {'quote': review['sources'][1]['publisher']['quote']}
    review['sources'][1]['cells'][1]['status'] = 'relevant'
    return objective, sources, selected, review


def test_office_table_renders_effective_missing_and_uncertain_cells():
    objective, sources, selected, review = downgraded_table_fixture()
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assert evidence['assessment']['eligible']
    assert evidence['review']['sources'][0]['cells'][0]['status'] == 'relevant'
    assert evidence['review']['sources'][0]['cells'][1]['status'] == 'missing'
    table = comparison.table(objective, sources, evidence)
    assert table['rows'][0][2].startswith('待补证（缺少引句）')
    assert table['rows'][0][3].startswith('待补证（证据不足）')
    assert all('原文[' not in cell for cell in table['rows'][0][2:4])
    assert all('原文[' in cell for cell in table['rows'][1][2:4])
    assert evidence['assessment']['relevant_count'] == 2


def test_effective_cell_downgrade_cannot_be_tampered_into_relevant():
    objective, sources, selected, review = downgraded_table_fixture()
    evidence = comparison.create_evidence(objective, sources, selected, review)
    evidence['assessment']['cells']['c0'][0]['status'] = 'relevant'
    with pytest.raises(ComputerError, match='comparison_evidence_invalid'):
        comparison.verify_evidence(objective, sources, evidence)


@pytest.mark.parametrize('publisher', [False, {'name': 'Name'}, {'name': 'Name', 'quote': 7}])
def test_v3_malformed_publisher_schema_remains_invalid(publisher):
    context, selected, review = fixture()
    context['goal']['comparison']['evidence_version'] = 3
    review['sources'][0]['publisher'] = publisher
    with pytest.raises(ComputerError, match='comparison_review_invalid'):
        assess(context, selected, review)


def test_one_required_group_reports_coverage_shortfall_after_identity_passes():
    objective, sources, selected, review = reviewed_fixture()
    objective['comparison'].update(evidence_version=3, min_domains=1)
    review['sources'][0]['cells'][0]['status'] = 'relevant'
    review['sources'][1]['publisher'] = None
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assessment = evidence['assessment']
    assert assessment['required_independent_count'] == assessment['independent_count'] == 1
    assert assessment['complete_independent_count'] == 0
    assert comparison.review_failure(evidence) == 'comparison_coverage_insufficient'


def unsupported_selection_fixture(status='absent'):
    objective, sources, selected, review = reviewed_fixture()
    objective['comparison']['evidence_version'] = 3
    if status == 'absent':
        selected['sources'][0]['spans'][0]['quote'] = 'Absent literal selection'
    else:
        page = sources[0][1]['observation']
        page['text'] += selected['sources'][0]['spans'][0]['quote']
        page['sha256'] = sha256(page['text'].encode()).hexdigest()
    review['sources'][0]['cells'][0]['status'] = 'missing'
    return objective, sources, selected, review


@pytest.mark.parametrize('status', ['absent', 'ambiguous'])
def test_v3_unsupported_selection_is_raw_audit_but_null_for_review_and_coverage(status):
    objective, sources, selected, review = unsupported_selection_fixture(status)
    before = deepcopy(selected)
    context = comparison.review_context(objective, sources, selected)
    assert context['selection']['sources'][0]['spans'] == [None, None]
    assert context['selection']['sources'][1] == selected['sources'][1]
    evidence = comparison.create_evidence(objective, sources, selected, review)
    assert evidence['raw_response'] == before == selected
    assert evidence['raw_response_sha256'] == digest(before)
    assert evidence['response'] == context['selection']
    assert evidence['selection_evidence'] == {'c0': [status, 'missing'], 'c1': ['supported', 'missing']}
    assert evidence['assessment']['cells']['c0'][0]['status'] == 'missing'
    assert evidence['assessment']['relevant_count'] == 1
    assert evidence['assessment']['complete_independent_count'] == 0
    assert not evidence['assessment']['eligible']
    assert comparison.verify_evidence(objective, sources, evidence) == evidence


@pytest.mark.parametrize('version', [None, 2])
@pytest.mark.parametrize('status', ['absent', 'ambiguous'])
def test_v1_v2_unsupported_selection_remains_strict(version, status):
    objective, sources, selected, review = unsupported_selection_fixture(status)
    if version is None:
        objective['comparison'].pop('evidence_version')
    else:
        objective['comparison']['evidence_version'] = version
    with pytest.raises(ComputerError, match='comparison_evidence_invalid'):
        comparison.create_evidence(objective, sources, selected, review if version else None)


@pytest.mark.parametrize('span', [{'quote': ''}, {'quote': None}, {'quote': 'x'*401},
                                 {'quote': 'literal', 'extra': True}, ['literal']])
def test_v3_malformed_selection_still_rejects_before_review(span):
    objective, sources, selected, _ = reviewed_fixture()
    objective['comparison']['evidence_version'] = 3
    selected['sources'][0]['spans'][0] = span
    with pytest.raises(ComputerError, match='comparison_evidence_invalid'):
        comparison.review_context(objective, sources, selected)


@pytest.mark.parametrize('mutation', ['raw', 'raw_hash', 'status', 'effective', 'missing_raw'])
def test_v3_raw_effective_selection_and_status_binding_rejects_tampering(mutation):
    objective, sources, selected, review = unsupported_selection_fixture()
    evidence = comparison.create_evidence(objective, sources, selected, review)
    if mutation == 'raw':
        evidence['raw_response']['sources'][0]['spans'][0]['quote'] = 'Changed absent quote'
    elif mutation == 'raw_hash':
        evidence['raw_response_sha256'] = '0'*64
    elif mutation == 'status':
        evidence['selection_evidence']['c0'][0] = 'supported'
    elif mutation == 'effective':
        evidence['response']['sources'][0]['spans'][0] = deepcopy(selected['sources'][0]['spans'][0])
    else:
        del evidence['raw_response']
    with pytest.raises(ComputerError, match='comparison_evidence_invalid'):
        comparison.verify_evidence(objective, sources, evidence)


def records(result):
    return [read_record(p)['data'] for p in sorted(Path(result['run']).glob('step-*/environment-result.json'))]


def configure(c, *, cached=False, both=False):
    """Alpha lacks attribution until its about page; Beta already has it."""
    c['goal']['min_sources'] = 2 if cached else 4
    c['search'].results = {'first': [A, A + ('about' if cached else 'two')],
                           'second': [B] if cached else [B, B + 'two']}
    for url in (A, A+'two', B, B+'two'):
        group = 'Alpha' if urlsplit(url).hostname == urlsplit(A).hostname else 'Beta'
        missing = group == 'Alpha' or both
        c['browser'].text[url] = ('Unknown ' if missing else 'Publisher ') + group + '; alpha fact omega ' + url
    for base, group in ((A, 'Alpha'), (B, 'Beta')):
        c['browser'].text[base+'about'] = 'Publisher ' + group + '; company ownership details'
    original_search = c['search'].search
    def search(query, max_sources, should_stop, timeout):
        assert max_sources == 2
        if query in c['search'].results:
            return original_search(query, max_sources, should_stop, timeout)
        assert query.startswith('site:') and 'about company' in query
        c['search'].calls.append(query)
        base = A if query.startswith('site:example.com ') else B
        return {'urls': [base+'about'], 'searched_at': STAMP}
    c['search'].search = search
    def select(data):
        assert 'choices' not in data, 'v3 must use deterministic local actions'
        if 'selection' not in data:
            return {'sources': [{'id': source['id'], 'spans': [{'quote': source['text']}]} for source in data['sources']]}
        rows = []
        for source in data['sources']:
            prefix = source['text'].split(';', 1)[0]
            rows.append({'id': source['id'], 'publisher': None if prefix.startswith('Unknown') else
                {'name': prefix.split(' ', 1)[1], 'quote': prefix},
                'cells': [{'status': 'relevant', 'reason': 'Captured fact addresses dimension'}]})
        return {'sources': rows, 'pairs': [{'left': a['id'], 'right': b['id'],
            'relation': 'same' if urlsplit(a['url']).hostname == urlsplit(b['url']).hostname else 'independent',
            'reason': 'Captured source relationship'} for a,b in combinations(data['sources'], 2)]}
    c['client'].select = select
    return c


def test_new_prepare_pins_v3_without_mutating_approved_input(ctx):
    before = deepcopy(ctx['goal'])
    plan = engine.prepare(ctx['session'], 'bounded completion', ctx['goal'])
    assert plan['goal']['comparison']['evidence_version'] == 3 and ctx['goal'] == before


def test_automatic_derived_search_replacement_second_review_then_office(ctx):
    configure(ctx)
    result = run(ctx, engine.prepare(ctx['session'], 'complete evidence', ctx['goal']))
    assert result['ok'], result['code']
    rows = records(result)
    comparisons = [r for r in rows if r['action']['kind'] == 'compare']
    assert [r['result']['ok'] for r in comparisons] == [False, True]
    assert comparisons[0]['comparison']['assessment']['complete_independent_count'] == 1
    assert comparisons[1]['comparison']['assessment']['complete_independent_count'] == 2
    assert len(comparisons[1]['active_ids']) == 4 and len(comparisons[1]['replaced_ids']) == 1
    assert comparisons[0]['active_ids'] != comparisons[1]['active_ids']
    assert [r['action']['kind'] for r in rows][-5:] == ['supplement','read','compare','spreadsheet','document']
    assert len(rows) == 12 and len(ctx['client'].calls) == 4
    assert len(ctx['browser'].calls) == len(set(ctx['browser'].calls)) == 5
    assert len(ctx['search'].calls) == 3 and len(ctx['effects']) == 2
    assert any(f['code'] == 'comparison_independence_insufficient' for f in result['failures'])


def test_cached_about_candidate_skips_supplement_search_and_required_terms(ctx):
    configure(ctx, cached=True)
    result = run(ctx, engine.prepare(ctx['session'], 'cached about', ctx['goal']))
    assert result['ok'], result['code']
    assert ctx['search'].calls == ['first', 'second']
    assert A+'about' in ctx['browser'].calls
    rows = records(result)
    supplemental_read = next(r for r in rows if r.get('read_purpose', {}).get('purpose') == 'supplement')
    assert 'omega' not in supplemental_read['observation']['text'].lower()
    assert supplemental_read['result']['ok'] and len(ctx['client'].calls) == 4


def test_supplement_reuses_unread_unhinted_candidate_without_replaying_completed_urls(ctx):
    configure(ctx, cached=True)
    download = A + 'download'
    ctx['search'].results['first'] = [A, download]
    ctx['browser'].text[download] = ctx['browser'].text[A+'about']
    original_search = ctx['search'].search
    def search(query, *args, **kwargs):
        if query not in ctx['search'].results:
            ctx['search'].calls.append(query)
            return {'urls': [A, download], 'searched_at': STAMP}
        return original_search(query, *args, **kwargs)
    ctx['search'].search = search
    environments = []
    def factory(*args, **kwargs):
        from agent_service.computer_agent_tools import AgentEnvironment
        env = AgentEnvironment(*args, **kwargs)
        environments.append(env)
        return env
    plan = engine.prepare(ctx['session'], 'reuse discovered download', ctx['goal'])
    first = run(ctx, plan, 7, environment_factory=factory)
    assert not first['ok'] and not ctx['effects']
    rows = records(first)
    supplement = next(row for row in rows if row['action']['kind'] == 'supplement')
    assert supplement['discovery']['urls'] == [A, download]
    observation = environments[-1].observe_final()
    assert len(observation['candidates']) == 3
    reused = next(item for item in observation['candidates'] if item['url'] == download)
    assert reused['need_id'] == supplement['action']['need_id']
    assert not any(hint in urlsplit(download).path for hint in ('about', 'copyright', 'legal', 'company', 'format', 'compatib', 'feature'))
    assert ctx['browser'].calls == [A, B, download]
    resumed = recovery.prepare(ctx['session'], first['run'])['plan']
    final = run(ctx, resumed, environment_factory=factory)
    assert final['ok'], final['code']
    assert ctx['browser'].calls == [A, B, download]
    assert ctx['search'].calls[:2] == ['first', 'second'] and len(ctx['search'].calls) == 3
    assert len(environments[-1].observe_final()['candidates']) == 3
    assert len(ctx['client'].calls) == 4 and len(ctx['effects']) == 2


def production_link_fixture(ctx, monkeypatch, *, empty=False):
    """Exercise self.search=None with actual HTML proof and offline transports."""
    from agent_service import comparison_completion as completion, comparison_link_discovery as links, browser
    configure(ctx, cached=True)
    ctx['search'].results['first'] = [A]
    ctx['options'].pop('search')
    monkeypatch.setattr(completion, 'SearchProcess', lambda **kwargs: ctx['search'])
    monkeypatch.setattr(browser, 'fetch_source', lambda *args, **kwargs: pytest.fail('unexpected HTTP'))
    calls = []
    class Linked:
        def discover(self, source, query, excluded, *, should_stop, timeout):
            assert not should_stop() and 0 < timeout <= 20
            calls.append((source, query, list(excluded)))
            html = b'<html><p>No links</p></html>' if empty else b'<a href="/about">About company</a>'
            return links.make_link_result(source, query, excluded, 'text/html', html, STAMP)
    monkeypatch.setattr(links, 'LinkDiscoveryProcess', Linked)
    return calls


def test_production_html_links_discover_candidate_and_recover_without_http_or_bing(ctx, monkeypatch):
    calls = production_link_fixture(ctx, monkeypatch)
    plan = engine.prepare(ctx['session'], 'production anchor recovery', ctx['goal'])
    first = run(ctx, plan, 6)
    assert not first['ok'] and not ctx['effects']
    supplement = next(row for row in records(first) if row['action']['kind'] == 'supplement')
    assert supplement['link_result']['discovery'] == supplement['discovery']
    assert supplement['discovery']['urls'] == [A+'about']
    assert calls == [(A, 'site:example.com about company', [A, B])]
    assert ctx['search'].calls == ['first', 'second']
    assert ctx['browser'].calls == [A, B]
    resumed = recovery.prepare(ctx['session'], first['run'])['plan']
    final = run(ctx, resumed)
    assert final['ok'], final['code']
    assert len(calls) == 1 and ctx['search'].calls == ['first', 'second']
    assert ctx['browser'].calls == [A, B, A+'about']
    assert len(ctx['client'].calls) == 4 and len(ctx['effects']) == 2


def test_production_empty_anchor_proof_falls_back_to_bing_once(ctx, monkeypatch):
    calls = production_link_fixture(ctx, monkeypatch, empty=True)
    result = run(ctx, engine.prepare(ctx['session'], 'empty anchors fallback', ctx['goal']))
    assert result['ok'], result['code']
    supplement = next(row for row in records(result) if row['action']['kind'] == 'supplement')
    assert supplement['link_result']['discovery'] is None
    assert supplement['link_result']['proof']['chosen_urls'] == []
    assert supplement['discovery']['urls'] == [A+'about']
    assert len(calls) == 1 and len(ctx['search'].calls) == 3
    assert ctx['search'].calls[:2] == ['first', 'second']
    assert ctx['browser'].calls == [A, B, A+'about']


@pytest.mark.parametrize('mutation', ['source', 'query', 'output'])
def test_production_anchor_proof_tampering_blocks_recovery(ctx, monkeypatch, mutation):
    calls = production_link_fixture(ctx, monkeypatch)
    first = run(ctx, engine.prepare(ctx['session'], 'bind anchor provenance', ctx['goal']), 6)
    assert not first['ok'] and not ctx['effects']
    path = Path(first['run'])/'step-06'/'environment-result.json'
    record = read_record(path)
    if mutation == 'source':
        record['data']['link_result']['proof']['source_url'] = A+'changed'
    elif mutation == 'query':
        record['data']['link_result']['proof']['query'] = 'site:example.com other company'
    else:
        record['data']['discovery']['urls'] = [A+'different']
    record['sha256'] = digest(record['data'])
    path.write_text(json.dumps(record), encoding='utf-8')
    assert not recovery.inspect(ctx['session'], first['run'])['eligible']
    assert len(calls) == 1 and ctx['search'].calls == ['first', 'second']
    assert ctx['browser'].calls == [A, B] and not ctx['effects']


def retention_loop_fixture(ctx, replacement, *, revoke=False):
    configure(ctx, cached=True)
    ctx['browser'].text[B] += '; alternative statement; DUP DUP'
    original = ctx['client'].select
    selection_count = [0]
    review_contexts = []
    def select(data):
        value = original(data)
        if 'selection' not in data:
            selection_count[0] += 1
            if selection_count[0] == 2:
                sid = next(source['id'] for source in data['sources'] if source['url'] == B)
                row = next(row for row in value['sources'] if row['id'] == sid)
                row['spans'][0] = (None if replacement == 'null' else
                                   {'quote': 'DUP' if replacement == 'repeated' else 'alternative statement'})
        else:
            review_contexts.append(deepcopy(data))
            if revoke and len(review_contexts) == 2:
                sid = next(source['id'] for source in data['sources'] if source['url'] == B)
                next(row for row in value['sources'] if row['id'] == sid)['cells'][0]['status'] = 'irrelevant'
        return value
    ctx['client'].select = select
    return review_contexts


@pytest.mark.parametrize('replacement', ['different', 'repeated', 'null'])
def test_second_comparison_retains_prior_relevant_literal_and_recovers_without_replay(ctx, replacement):
    reviewed = retention_loop_fixture(ctx, replacement)
    plan = engine.prepare(ctx['session'], 'retain verified prior quote', ctx['goal'])
    first = run(ctx, plan, 7)
    assert not first['ok'] and not ctx['effects']
    comparisons = [row for row in records(first) if row['action']['kind'] == 'compare']
    assert len(comparisons) == 2 and comparisons[1]['result']['ok']
    sid = digest(B)[:24]
    previous = next(row for row in comparisons[0]['comparison']['response']['sources'] if row['id'] == sid)['spans'][0]
    model_row = next(row for row in comparisons[1]['selection_response']['sources'] if row['id'] == sid)
    assert model_row['spans'][0] != previous
    effective_row = next(row for row in comparisons[1]['comparison']['raw_response']['sources'] if row['id'] == sid)
    assert effective_row['spans'][0] == previous
    assert {'id': sid, 'dimension_index': 0, 'quote': previous['quote']} in comparisons[1]['retained_selection']
    assert next(row for row in reviewed[1]['selection']['sources'] if row['id'] == sid)['spans'][0] == previous
    calls = deepcopy(ctx['client'].calls)
    prior_bytes = {p: p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    final = run(ctx, recovery.prepare(ctx['session'], first['run'])['plan'])
    assert final['ok'], final['code']
    assert ctx['client'].calls == calls and len(calls) == 4
    assert ctx['search'].calls == ['first', 'second']
    assert ctx['browser'].calls == [A, B, A+'about'] and len(ctx['effects']) == 2
    assert all(p.read_bytes() == content for p, content in prior_bytes.items())


def test_fresh_second_review_can_revoke_retained_quote(ctx):
    retention_loop_fixture(ctx, 'null', revoke=True)
    result = run(ctx, engine.prepare(ctx['session'], 'fresh review revokes retained quote', ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    comparisons = [row for row in records(result) if row['action']['kind'] == 'compare']
    assert len(comparisons) == 2 and comparisons[1]['retained_selection']
    assert not comparisons[1]['result']['ok']
    assert comparisons[1]['comparison']['assessment']['complete_independent_count'] == 1
    assert len(ctx['client'].calls) == 4


def test_retained_selection_audit_tampering_blocks_recovery(ctx):
    retention_loop_fixture(ctx, 'null')
    first = run(ctx, engine.prepare(ctx['session'], 'bind retained quote audit', ctx['goal']), 7)
    assert not first['ok'] and not ctx['effects']
    path = Path(first['run'])/'step-07'/'environment-result.json'
    record = read_record(path)
    assert record['data']['retained_selection']
    record['data']['retained_selection'][0]['quote'] = 'Forged retained quote'
    record['sha256'] = digest(record['data'])
    path.write_text(json.dumps(record), encoding='utf-8')
    assert not recovery.inspect(ctx['session'], first['run'])['eligible']
    assert len(ctx['client'].calls) == 4 and not ctx['effects']


@pytest.mark.parametrize('halt', [7, 9])
def test_resume_after_negative_review_or_supplement_read_does_not_replay(ctx, halt):
    configure(ctx)
    plan = engine.prepare(ctx['session'], 'resume completion', ctx['goal'])
    first = run(ctx, plan, halt)
    assert not first['ok'] and not ctx['effects']
    original = {p: p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    resumed = recovery.prepare(ctx['session'], first['run'])['plan']
    final = run(ctx, resumed)
    assert final['ok'], final['code']
    assert len(ctx['client'].calls) == 4 and len(ctx['search'].calls) == 3
    assert len(ctx['browser'].calls) == len(set(ctx['browser'].calls)) == 5
    assert first['actions'] + final['actions'] == 12
    assert all(p.read_bytes() == content for p, content in original.items())


@pytest.mark.parametrize('failure', ['schema', 'publisher_schema', 'api'])
def test_invalid_schema_or_api_failure_is_terminal_without_supplements(ctx, failure):
    configure(ctx, cached=True)
    original = ctx['client'].select
    def select(data):
        if 'selection' in data:
            if failure == 'api':
                raise ComputerError('model_budget_exceeded')
            if failure == 'publisher_schema':
                review = original(data)
                review['sources'][0]['publisher'] = {'name': 'Alpha'}
                return review
            return {}
        return original(data)
    ctx['client'].select = select
    result = run(ctx, engine.prepare(ctx['session'], 'terminal failure', ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    assert ctx['search'].calls == ['first', 'second'] and A+'about' not in ctx['browser'].calls
    compares = [r for r in records(result) if r['action']['kind'] == 'compare']
    assert len(compares) == 1 and compares[0]['selection_response']
    assert ('review_response' in compares[0]) == (failure != 'api')
    if failure == 'schema':
        assert compares[0]['review_response'] == {}
    elif failure == 'publisher_schema':
        assert compares[0]['review_response']['sources'][0]['publisher'] == {'name': 'Alpha'}
    assert not recovery.inspect(ctx['session'], result['run'])['eligible']


def test_exhausted_repairs_never_compare_unchanged_set(ctx):
    configure(ctx, both=True)
    ctx['goal']['min_sources'] = 2
    def search(query, max_sources, should_stop, timeout):
        ctx['search'].calls.append(query)
        if query not in ctx['search'].results:
            raise ComputerError('search_unavailable')
        return {'urls': ctx['search'].results[query], 'searched_at': STAMP}
    ctx['search'].search = search
    result = run(ctx, engine.prepare(ctx['session'], 'exhaust repairs', ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    rows = records(result)
    assert sum(r['action']['kind'] == 'compare' for r in rows) == 1
    assert sum(r['action']['kind'] == 'supplement' for r in rows) == 2
    assert len(ctx['client'].calls) == 2 and len(ctx['search'].calls) == 4
    assert not recovery.inspect(ctx['session'], result['run'])['eligible']


def test_supplement_receipt_cannot_escape_exact_host(ctx):
    configure(ctx, cached=True)
    ctx['search'].results['first'] = [A]
    prior = ctx['search'].search
    def search(query, *args, **kwargs):
        if query not in ctx['search'].results:
            ctx['search'].calls.append(query)
            return {'urls': ['https://sub.example.com/about'], 'searched_at': STAMP}
        return prior(query, *args, **kwargs)
    ctx['search'].search = search
    result = run(ctx, engine.prepare(ctx['session'], 'exact host only', ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    assert 'https://sub.example.com/about' not in ctx['browser'].calls
    assert any(r['result']['code'] == 'supplement_host_mismatch' for r in records(result))


def test_six_existing_session_claims_stop_before_any_model_request(ctx):
    configure(ctx, cached=True)
    for i in range(6):
        ctx['session'].claim_model({'previous': i})
    result = run(ctx, engine.prepare(ctx['session'], 'claim cap', ctx['goal']))
    assert not result['ok'] and not ctx['client'].calls and not ctx['effects']
    assert records(result)[-1]['result']['code'] == 'session_model_call_limit'


def test_two_repairs_keep_eight_candidate_and_two_review_limits(ctx):
    configure(ctx, both=True)
    ctx['goal']['min_sources'] = 2
    original = ctx['search'].search
    def search(query, *args, **kwargs):
        value = original(query, *args, **kwargs)
        if query not in ctx['search'].results:
            value['urls'].append(value['urls'][0] + '-unused')
        return value
    ctx['search'].search = search
    result = run(ctx, engine.prepare(ctx['session'], 'two repair limit', ctx['goal']))
    assert result['ok'], result['code']
    rows = records(result)
    discovered = {url for row in rows for url in row.get('discovery', {}).get('urls', [])}
    assert len(discovered) == 8 and len(rows) == 12
    assert sum(row['action']['kind'] == 'supplement' for row in rows) == 2
    assert sum(row['action']['kind'] == 'compare' for row in rows) == 2
    assert len(ctx['client'].calls) == 4 and not any('unused' in url for url in ctx['browser'].calls)


def test_initial_pages_still_require_approved_terms(ctx):
    configure(ctx, cached=True)
    ctx['search'].results = {'first': [A], 'second': [B]}
    ctx['browser'].text[A] = 'Publisher Alpha; company ownership details'
    result = run(ctx, engine.prepare(ctx['session'], 'initial terms gate', ctx['goal']))
    assert not result['ok'] and not ctx['effects'] and not ctx['client'].calls
    assert any(row['result']['code'] == 'quality_rejected' for row in records(result))


def test_second_review_cannot_claim_seventh_session_call(ctx):
    configure(ctx, cached=True)
    for i in range(3):
        ctx['session'].claim_model({'previous': i})
    result = run(ctx, engine.prepare(ctx['session'], 'second review claim limit', ctx['goal']))
    assert not result['ok'] and not ctx['effects']
    assert len(ctx['client'].calls) == 3
    assert records(result)[-1]['result']['code'] == 'session_model_call_limit'
    claims = [p for p in (ctx['session'].root/'model_calls').glob('*.json') if not p.name.endswith('.outcome.json')]
    assert len(claims) == 6
