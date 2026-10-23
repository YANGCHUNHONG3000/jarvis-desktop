"""Offline proof10 traversal/replay, source literals and retained-byte promotion."""
import base64
import copy
from hashlib import sha256
import json
from pathlib import Path
import zlib

import pytest

from agent_service import comparison_evidence_discovery as evidence
from agent_service.computer import ComputerError, digest

ROOT = 'https://github.com/shshe/openpyxl'
INDEX = 'https://openpyxl.readthedocs.io/en/stable/index.html'
TUTORIAL = 'https://openpyxl.readthedocs.io/en/stable/tutorial.html'
USAGE = 'https://openpyxl.readthedocs.io/en/stable/usage.html'
QUERY = 'openpyxl formula calculation'
TOPIC = 'openpyxl Python Excel documentation'
AT = '2026-10-23T14:00:05+00:00'


def page(url, text, depth):
    raw = text.encode()
    return {'url': url, 'parent': None, 'anchor': None, 'depth': depth,
            'status': 'ok', 'mime': 'text/html',
            'html_zlib_base64': base64.b64encode(zlib.compress(raw)).decode(),
            'source_sha256': sha256(raw).hexdigest()}


def history(pages=None):
    return [{'discovery': None, 'proof': {'version': 9, 'source_url': ROOT,
        'searched_at': AT, 'pages': pages if pages is not None else [
            page(INDEX, '<dl><dt>Author:</dt><dd>Eric Gazoni, Charlie Clark</dd></dl>', 2),
            page(TUTORIAL, '<a href="usage.html">Simple usage</a>', 3)]}}]


def observation(url, text):
    return {'requested_url': url, 'final_url': url, 'title': 'Retained product page',
            'text': text, 'captured_at': AT, 'sha256': sha256(text.encode()).hexdigest()}


def discover(prior=None, contents=None, observations=(), **kwargs):
    prior = history() if prior is None else prior
    contents = {USAGE: 'NB openpyxl never evaluates formulae.'} if contents is None else contents
    options = {'topic': TOPIC, **kwargs}
    url = options.pop('url', ROOT)
    query = options.pop('query', QUERY)
    excluded = options.pop('excluded_urls', ())
    request = evidence._request(url, query, excluded, prior,
                               source_observations=observations, **options)
    calls = []
    def fetch(target):
        calls.append(target)
        item = contents[target]
        if isinstance(item, Exception):
            raise item
        return 'text/html', item.encode() if isinstance(item, str) else item
    result = evidence._discover(request, prior, fetch, AT, source_observations=observations)
    return result, calls, prior


def test_old_depth_three_extends_without_rewriting_old_depth_or_getting_it():
    value, calls, prior = discover()
    assert calls == [USAGE]
    assert value['discovery']['urls'] == [USAGE, INDEX]
    new = value['proof']['pages'][0]
    assert new['depth'] == 4 and new['extension_depth'] == 1
    assert new['parent'] == {'prior': 0, 'page': 1, 'sha256': digest(prior[0]['proof']['pages'][1])}
    assert prior[0]['proof']['pages'][1]['depth'] == 3
    assert value['proof']['chosen'][0]['page'] == {'new': 0}
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value
    captured = evidence.cached_page(USAGE, prior, value)
    assert captured['content'] == b'NB openpyxl never evaluates formulae.'
    assert captured['captured_at'] == AT and captured['ref'] == {'new': 0}


def test_usage_gateway_precedes_generic_formula_api_links():
    prior = history()
    prior[0]['proof']['pages'].append(page('https://openpyxl.readthedocs.io/py-modindex.html',
        '<a href="api/formula.html">Parsing formulas</a>', 3))
    value, calls, _ = discover(prior)
    assert calls == [USAGE]
    assert value['discovery']['urls'] == [USAGE, INDEX]


def test_generic_formula_link_is_fetched_and_promoted_without_second_get():
    formula = 'https://openpyxl.readthedocs.io/formula.html'
    prior = history([page(INDEX, '<a href="'+formula+'">Working with formulas</a>', 2)])
    value, calls, _ = discover(prior, {formula: 'openpyxl never evaluates formulae.'})
    assert calls == [formula] and value['discovery']['urls'] == [formula]
    assert value['proof']['chosen'][0]['reason'] == 'formula_passage'


def test_formula_calculation_link_can_be_selected_without_get():
    formula = 'https://openpyxl.readthedocs.io/formulas-calculation.html'
    prior = history([page(INDEX, '<a href="'+formula+'">Formula calculation</a>', 2)])
    value, calls, _ = discover(prior, {})
    assert calls == [] and value['discovery']['urls'] == [formula]


def test_proof_ten_can_supply_next_request_without_another_get():
    first, _, prior = discover()
    combined = prior + [first]
    value, calls, _ = discover(combined, contents={})
    assert calls == [] and value['discovery']['urls'] == [USAGE, INDEX]
    assert value['proof']['chosen'][0]['page']['prior'] == 1
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=combined, topic=TOPIC) == value


def test_literal_product_url_is_offset_and_observation_hash_bound():
    root = 'https://github.com/jmcnamara/XlsxWriter'
    docs = 'https://xlsxwriter.readthedocs.io'
    text = 'See the full documentation at: ' + docs
    obs = [observation(root, text)]
    value, calls, prior = discover([], {docs+'/': 'XlsxWriter does not calculate the result of a formula.'},
        obs, url=root, query='XlsxWriter formula calculation', topic='XlsxWriter Python Excel documentation')
    assert calls == [docs+'/'] and value['discovery']['urls'] == [docs+'/']
    edge = value['proof']['literal_edges'][0]
    assert edge['parent'] == {'source': 0, 'sha256': digest(obs[0])}
    assert text[edge['literal']['start']:edge['literal']['end']] == docs == edge['literal']['text']
    assert evidence.validate(value, root, 'XlsxWriter formula calculation', prior_discoveries=prior,
        source_observations=obs, topic='XlsxWriter Python Excel documentation') == value
    changed = copy.deepcopy(obs)
    changed[0]['text'] = 'Changed ' + text
    changed[0]['sha256'] = sha256(changed[0]['text'].encode()).hexdigest()
    with pytest.raises(ComputerError):
        evidence.validate(value, root, 'XlsxWriter formula calculation', prior_discoveries=prior,
            source_observations=changed, topic='XlsxWriter Python Excel documentation')


def test_same_page_formula_and_attribution_selects_one_url():
    prior = history([page(USAGE, 'openpyxl never evaluates formulae.\nAuthor: Eric Gazoni', 3)])
    value, calls, _ = discover(prior)
    assert calls == [] and value['discovery']['urls'] == [USAGE]


@pytest.mark.parametrize('footer', ['Copyright 2010 - 2024, See AUTHORS.', 'Built with Sphinx', 'Author:',
    'Copyright 2010 - 2024,\nCreated using Sphinx', 'Copyright 2024, All rights reserved'])
def test_placeholder_or_theme_is_not_explicit_attribution(footer):
    value, _, _ = discover(history([page(INDEX, footer, 2), page(TUTORIAL, '<a href="usage.html">Simple usage</a>', 3)]))
    assert value['discovery']['urls'] == [USAGE]


def test_cross_host_attribution_does_not_join_documentation_host():
    prior = history([page('https://openpyxl.other.org/index', 'Author: Eric Gazoni', 1),
                     page(TUTORIAL, '<a href="usage.html">Simple usage</a>', 3)])
    value, _, _ = discover(prior)
    assert value['discovery']['urls'] == [USAGE]


@pytest.mark.parametrize('label,url', [
    ('Evaluation', 'https://openpyxl.readthedocs.io/evaluation'),
    ('Formula docs', 'https://unrelated.readthedocs.io/formula'),
    ('Simple usage', 'https://github.com/another/openpyxl/usage'),
    ('Formula text', 'https://openpyxl.readthedocs.io/en/stable/_sources/formula.rst.txt'),
])
def test_routes_reject_nonformula_unrelated_project_and_source_exports(label, url):
    prior = history([page(TUTORIAL, '<a href="'+url+'">'+label+'</a>', 3)])
    value, calls, _ = discover(prior, {}, [observation(ROOT, 'Existing root capture')])
    assert calls == [] and value['discovery'] is None


def test_four_gets_and_three_extension_edges_are_finite():
    first = 'https://openpyxl.readthedocs.io/examples1'
    second = 'https://openpyxl.readthedocs.io/examples2'
    third = 'https://openpyxl.readthedocs.io/examples3'
    fourth = 'https://openpyxl.readthedocs.io/examples4'
    prior = history([page(TUTORIAL, '<a href="'+first+'">Examples</a>', 3)])
    value, calls, _ = discover(prior, {first: '<a href="'+second+'">Examples</a>',
        second: '<a href="'+third+'">Examples</a>', third: '<a href="'+fourth+'">Examples</a>'},
        [observation(ROOT, 'Existing root capture')])
    assert calls == [first, second, third]
    assert [p['extension_depth'] for p in value['proof']['pages']] == [1, 2, 3]
    assert value['discovery'] is None


def test_no_repeat_of_excluded_or_already_navigated_urls():
    prior = history([page(TUTORIAL, '<a href="usage.html">Simple usage</a>', 3),
                     page(USAGE, 'openpyxl never evaluates formulae.', 4)])
    value, calls, _ = discover(prior, {}, [observation(ROOT, 'Existing root capture')], excluded_urls=[USAGE])
    assert calls == [] and value['discovery'] is None


@pytest.mark.parametrize('change', [
    lambda v: v['proof']['pages'][0].__setitem__('extension_depth', 0),
    lambda v: v['proof']['pages'][0].__setitem__('depth', 0),
    lambda v: v['proof']['pages'][0].__setitem__('anchor', 1),
    lambda v: v['proof']['cached_refs'][0].__setitem__('sha256', '0'*64),
    lambda v: v['proof']['pages'][0].__setitem__('source_sha256', '0'*64),
    lambda v: v['proof']['chosen'][0].__setitem__('url', INDEX),
    lambda v: v['proof'].__setitem__('observations_sha256', '0'*64),
    lambda v: v['proof'].__setitem__('version', True),
])
def test_tampered_proof_is_rejected_without_network(change):
    value, _, prior = discover()
    change(value)
    with pytest.raises(ComputerError):
        evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC)


def test_bad_observation_hash_or_count_and_excluded_bounds():
    obs = observation(ROOT, 'Existing root capture')
    obs['sha256'] = '0'*64
    with pytest.raises(ComputerError):
        discover(observations=[obs])
    with pytest.raises(ComputerError):
        discover(observations=[observation(ROOT, 'Text')]*7)
    with pytest.raises(ComputerError):
        discover(excluded_urls=[ROOT]*17)
    # Exactly sixteen is permitted and deduplicated for the request binding.
    value, _, _ = discover(excluded_urls=[ROOT]*16)
    assert value['proof']['excluded_urls'] == [ROOT]


def test_cached_page_rejects_modified_bytes_and_ambiguous_versions():
    prior = history()
    prior[0]['proof']['pages'][0]['source_sha256'] = '0'*64
    with pytest.raises(ComputerError):
        evidence.cached_page(INDEX, prior)
    with pytest.raises(ComputerError):
        evidence.cached_page(INDEX, history([page(INDEX, 'First', 1), page(INDEX, 'Second', 1)]))


def test_worker_uses_immutable_bound_inputs_and_parent_replay(tmp_path, monkeypatch):
    from agent_service import process
    prior = history()
    def child(command, **kwargs):
        payload = evidence._input(command[-2], command[-1])
        value = evidence._discover(payload['request'], payload['prior_discoveries'],
            lambda u: ('text/html', b'openpyxl never evaluates formulae.'), AT,
            source_observations=payload['source_observations'])
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    worker = evidence.EvidenceDiscoveryProcess()
    value = worker.discover(ROOT, QUERY, prior_discoveries=prior, storage_dir=tmp_path, topic=TOPIC)
    assert value['discovery']['urls'] == [USAGE, INDEX]
    assert len(list(tmp_path.glob('evidence-input-*.json'))) == 1
    assert worker.discover(ROOT, QUERY, prior_discoveries=prior, storage_dir=tmp_path, topic=TOPIC) == value


def test_source_literal_cannot_cross_hosted_project_scope():
    url = 'https://github.com/unrelated/openpyxl'
    obs = [observation(ROOT, 'See '+url+'/examples')]
    value, calls, _ = discover([], {}, obs)
    assert calls == [] and value['discovery'] is None


def test_proof_eleven_formula_noun_precedes_generic_examples_and_promotes_bytes():
    docs = 'https://xlsxwriter.readthedocs.io/'
    formula = docs+'working_with_formulas.html'
    root = 'https://github.com/jmcnamara/XlsxWriter'
    prior = history([page(docs, '<a href="examples.html">Examples</a>'
        '<a href="working_with_formulas.html">Working with Formulas</a>', 1)])
    value, calls, _ = discover(prior, {formula: 'XlsxWriter does not calculate the result of a formula.'},
        url=root, query='XlsxWriter formula calculation', topic='XlsxWriter Python Excel documentation',
        navigation_policy='formula_direct_v2')
    assert calls == [formula] and value['discovery']['urls'] == [formula]
    assert value['proof']['version'] == 11
    assert value['proof']['chosen'][0]['reason'] == 'formula_passage'
    assert evidence.validate(value, root, 'XlsxWriter formula calculation', prior_discoveries=prior,
        topic='XlsxWriter Python Excel documentation') == value
    assert evidence.cached_page(formula, prior, value)['ref'] == {'new': 0}


def test_proof_eleven_keeps_usage_before_api_formula():
    prior = history()
    prior[0]['proof']['pages'].append(page('https://openpyxl.readthedocs.io/py-modindex.html',
        '<a href="api/formula.html">Parsing formulas</a>', 3))
    value, calls, _ = discover(prior, navigation_policy='formula_direct_v2')
    assert calls == [USAGE] and value['proof']['version'] == 11


def test_proof_eleven_reuses_preceding_eleven_without_get():
    first, _, prior = discover(navigation_policy='formula_direct_v2')
    value, calls, _ = discover(prior+[first], {}, navigation_policy='formula_direct_v2')
    assert calls == [] and value['discovery']['urls'] == [USAGE, INDEX]
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior+[first], topic=TOPIC) == value


def test_proof_ten_ranking_and_binding_still_replay_without_policy():
    docs = 'https://xlsxwriter.readthedocs.io/'
    formula = docs+'working_with_formulas.html'
    example = docs+'examples.html'
    root = 'https://github.com/jmcnamara/XlsxWriter'
    prior = history([page(docs, '<a href="examples.html">Examples</a>'
        '<a href="working_with_formulas.html">Working with Formulas</a>', 1)])
    value, calls, _ = discover(prior, {example: 'Example prose', formula: 'Formula parsing'},
        [observation(root, 'Existing capture')], url=root, query='XlsxWriter formula calculation',
        topic='XlsxWriter Python Excel documentation')
    assert calls == [example, formula] and value['proof']['version'] == 10
    assert 'navigation_policy' not in value['proof']
    assert evidence.validate(value, root, 'XlsxWriter formula calculation', prior_discoveries=prior,
        source_observations=[observation(root, 'Existing capture')],
        topic='XlsxWriter Python Excel documentation') == value
    altered = copy.deepcopy(value)
    altered['proof']['version'] = 11
    altered['proof']['navigation_policy'] = 'formula_direct_v2'
    with pytest.raises(ComputerError):
        evidence.validate(altered, root, 'XlsxWriter formula calculation', prior_discoveries=prior,
            source_observations=[observation(root, 'Existing capture')], topic='XlsxWriter Python Excel documentation')


def test_actual_retained_live_xlsxwriter_root_next_edge_is_formula_without_repeated_get():
    outcome = Path('validation/artifacts/formula-evidence-20261023/start-outcome.json')
    if not outcome.is_file():
        pytest.skip('Local live evidence is intentionally outside Git')
    run = Path(json.loads(outcome.read_text(encoding='utf8'))['run'])
    receipt = run/'step-02/environment-result.json'
    if not receipt.is_file():
        pytest.skip('Local live supplement receipt unavailable')
    recorded = json.loads(receipt.read_text(encoding='utf8'))['data']['link_result']
    if recorded['proof']['version'] != 10:
        pytest.skip('The retained baseline is the immutable proof10 live attempt')
    input_file = run/('evidence-input-'+recorded['proof']['request_sha256']+'.json')
    payload = json.loads(input_file.read_text(encoding='utf8'))
    assert evidence.validate(recorded, recorded['proof']['source_url'], recorded['proof']['query'],
        recorded['proof']['excluded_urls'], prior_discoveries=payload['prior_discoveries'],
        source_observations=payload['source_observations'], topic=recorded['proof']['topic']) == recorded
    prior = payload['prior_discoveries']+[recorded]
    request = evidence._request(recorded['proof']['source_url'], recorded['proof']['query'],
        recorded['proof']['excluded_urls'], prior, source_observations=payload['source_observations'],
        topic=recorded['proof']['topic'], navigation_policy='formula_direct_v2')
    calls = []
    def offline(target):
        calls.append(target)
        raise OSError('All GETs blocked; retained bytes only')
    value = evidence._discover(request, prior, offline, AT, source_observations=payload['source_observations'])
    assert calls[0] == 'https://xlsxwriter.readthedocs.io/working_with_formulas.html'
    assert not set(calls) & {p['url'] for p in recorded['proof']['pages']}
    assert evidence.validate(value, request['source_url'], request['query'], request['excluded_urls'],
        prior_discoveries=prior, source_observations=payload['source_observations'], topic=request['topic']) == value


SEARCH = 'https://openpyxl.readthedocs.io/en/stable/search.html'
SEARCH_INDEX = 'https://openpyxl.readthedocs.io/en/stable/searchindex.js'
FORMULA_DOC = 'https://openpyxl.readthedocs.io/en/stable/simple_formulae.html'


def search_history():
    return history([page(INDEX, '<dl><dt>Author:</dt><dd>Eric Gazoni, Charlie Clark</dd></dl>'
                         '<a href="search.html">Search Page</a>', 2)])


def index_text(names=None, titles=None):
    return 'Search.setIndex('+json.dumps({'docnames': ['simple_formulae'] if names is None else names,
        'titles': ['Simple Formulae'] if titles is None else titles, 'terms': {}, 'filenames': ['simple_formulae.rst']})+');'


def search_discover(prior=None, index=None, search=None, document='openpyxl never evaluates formulae.',
                    navigation_policy='formula_search_v3'):
    prior = search_history() if prior is None else prior
    pages = {SEARCH: ('text/html', (search or '<script src="searchindex.js"></script>').encode()),
             SEARCH_INDEX: ('application/javascript', (index_text() if index is None else index).encode()),
             FORMULA_DOC: ('text/html', document.encode())}
    calls = []
    def fetch(url):
        calls.append(url)
        return pages[url]
    request = evidence._request(ROOT, QUERY, (), prior, topic=TOPIC, navigation_policy=navigation_policy)
    value = evidence._discover(request, prior, fetch, AT)
    return value, calls, prior


def test_search_policy_follows_observed_search_anchor_script_and_static_index():
    value, calls, prior = search_discover()
    assert calls == [SEARCH, SEARCH_INDEX, FORMULA_DOC]
    assert value['proof']['version'] == 13
    assert value['discovery']['urls'] == [FORMULA_DOC, INDEX]
    assert [p['extension_depth'] for p in value['proof']['pages']] == [1, 2, 3]
    assert value['proof']['pages'][1]['script'] == 0
    assert value['proof']['pages'][1]['document_base'] == 'https://openpyxl.readthedocs.io/en/stable/'
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value
    resource = evidence.cached_page(SEARCH_INDEX, prior, value)
    assert evidence._passages(resource) == (False, False)
    token = value['proof']['pages'][2]['index_entry']
    raw = index_text()
    for field in ('docname_token', 'title_token'):
        span = token[field]
        assert raw[span['start']:span['end']] == span['text']
    assert json.loads(token['docname_token']['text']) == token['docname']
    assert token['matching_terms'] == ['formulae']
    assert sha256(raw[token['json_start']:token['json_end']].encode()).hexdigest() == token['json_sha256']


def test_search_policy_worker_covers_all_gets_in_one_discover_call(tmp_path, monkeypatch):
    from agent_service import process
    calls = []
    contents = {SEARCH: ('text/html', b'<script src="searchindex.js"></script>'),
                SEARCH_INDEX: ('application/javascript', index_text().encode()),
                FORMULA_DOC: ('text/html', b'openpyxl never evaluates formulae.')}
    def child(command, **kwargs):
        payload = evidence._input(command[-2], command[-1])
        def fetch(url):
            calls.append(url)
            return contents[url]
        return json.dumps(evidence._discover(payload['request'], payload['prior_discoveries'], fetch, AT,
            source_observations=payload['source_observations']))
    monkeypatch.setattr(process, 'run_child', child)
    value = evidence.EvidenceDiscoveryProcess().discover(ROOT, QUERY, prior_discoveries=search_history(),
        storage_dir=tmp_path, topic=TOPIC, navigation_policy='formula_search_v3')
    assert calls == [SEARCH, SEARCH_INDEX, FORMULA_DOC]
    assert value['discovery']['urls'] == [FORMULA_DOC, INDEX]


def test_search_policy_never_infers_calculation_from_index_terms_or_parsing_page():
    value, calls, _ = search_discover(index=index_text(titles=['Formula calculation evaluation']),
                                     document='This page describes parsing formulas.')
    assert calls[:3] == [SEARCH, SEARCH_INDEX, FORMULA_DOC]
    assert value['discovery'] is None


@pytest.mark.parametrize('index', [
    'Search.setIndex({docnames:["simple_formulae"],titles:["Formula"]});',
    index_text()+'evil();',
    'other('+index_text()+')',
    'Search.setIndex({"docnames":["simple_formulae"],"titles":[]});',
    'Search.setIndex({"docnames":[true],"titles":["Formula"]});',
    'Search.setIndex({"docnames":["../simple_formulae"],"titles":["Formula"]});',
    'Search.setIndex({"docnames":["https://bad.example/formula"],"titles":["Formula"]});',
    'Search.setIndex({"docnames":["simple_formulae"],"titles":["Formula"],"titles":["Other"]});',
    'Search.setIndex({"docnames":["simple_formulae"],"titles":["Formula"],"terms":NaN});',
])
def test_unknown_or_executable_index_structures_fail_closed(index):
    value, calls, _ = search_discover(index=index)
    assert FORMULA_DOC not in calls and value['discovery'] is None


@pytest.mark.parametrize('script', [
    '<script src="https://cdn.example/searchindex.js"></script>',
    '<script src="../_static/searchtools.js"></script>',
    '<script>Search.loadIndex("searchindex.js");</script>',
    '<a href="searchindex.js">Index</a>',
])
def test_only_actual_same_host_index_script_src_is_allowed(script):
    value, calls, _ = search_discover(search=script)
    assert SEARCH_INDEX not in calls and value['discovery'] is None


def test_index_names_are_relative_to_observed_search_document_directory():
    static_index = 'https://openpyxl.readthedocs.io/en/stable/_static/searchindex.js'
    request = evidence._request(ROOT, QUERY, (), search_history(), topic=TOPIC,
                                navigation_policy='formula_search_v3')
    calls = []
    def fetch(url):
        calls.append(url)
        return {SEARCH: ('text/html', b'<script src="_static/searchindex.js"></script>'),
                static_index: ('text/javascript', index_text().encode()),
                FORMULA_DOC: ('text/html', b'openpyxl never evaluates formulae.')}[url]
    value = evidence._discover(request, search_history(), fetch, AT)
    assert calls == [SEARCH, static_index, FORMULA_DOC]
    assert value['discovery']['urls'] == [FORMULA_DOC, INDEX]


def test_search_capture_and_index_are_reused_without_repeat_gets():
    first, _, prior = search_discover()
    combined = prior+[first]
    request = evidence._request(ROOT, QUERY, (), combined, topic=TOPIC, navigation_policy='formula_search_v3')
    value = evidence._discover(request, combined, lambda u: pytest.fail('cached proof13 refetched'), AT)
    assert value['proof']['pages'] == [] and value['discovery']['urls'] == [FORMULA_DOC, INDEX]
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=combined, topic=TOPIC) == value


@pytest.mark.parametrize('change', [
    lambda p: p['pages'][1].__setitem__('script', 20),
    lambda p: p['pages'][1].__setitem__('document_base', 'https://openpyxl.readthedocs.io/elsewhere/'),
    lambda p: p['pages'][2]['index_entry'].__setitem__('document', 2),
    lambda p: p['pages'][2]['index_entry']['docname_token'].__setitem__('start', 0),
    lambda p: p['pages'][2]['index_entry'].__setitem__('matching_terms', ['calculate']),
    lambda p: p['pages'][2]['index_entry'].__setitem__('json_sha256', '0'*64),
    lambda p: p.__setitem__('navigation_policy', 'formula_direct_v2'),
])
def test_search_edges_index_positions_hash_and_policy_tamper_rejected(change):
    value, _, prior = search_discover()
    change(value['proof'])
    with pytest.raises(ComputerError):
        evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC)


def test_existing_live_openpyxl_retained_index_exposes_search_page_without_network():
    root = Path('data/computer_sessions/computer-25c27e9f4188402688e3866245c3f584/goals/'
                '475cc3030ed881de39c9664e4c0c0dbabeb378bd73e1a3ca9a4bfcbe619284a7.run')
    inputs = list(root.glob('evidence-input-*.json'))
    if not inputs:
        pytest.skip('Retained local live evidence intentionally outside Git')
    payload = json.loads(inputs[0].read_text(encoding='utf8'))
    previous = payload['request']
    request = evidence._request(previous['source_url'], previous['query'], previous['excluded_urls'],
        payload['prior_discoveries'], topic=previous['topic'], source_observations=payload['source_observations'],
        navigation_policy='formula_search_v3')
    calls = []
    def blocked(url):
        calls.append(url)
        raise OSError('No real GET permitted in offline test')
    value = evidence._discover(request, payload['prior_discoveries'], blocked, AT,
                               source_observations=payload['source_observations'])
    assert calls[0] == SEARCH
    assert evidence.validate(value, request['source_url'], request['query'], request['excluded_urls'],
        prior_discoveries=payload['prior_discoveries'], source_observations=payload['source_observations'],
        topic=request['topic']) == value


def large_index_text(size):
    skeleton = 'Search.setIndex('+json.dumps({'docnames': ['simple_formulae'],
        'titles': ['Simple Formulae'], 'padding': ''})+');'
    return skeleton.replace('"padding": ""', '"padding": "'+'x'*(size-len(skeleton))+'"')


def test_proof14_accepts_actual_sized_937369_byte_static_index_and_replays():
    body = large_index_text(937369)
    assert len(body.encode()) == 937369
    value, calls, prior = search_discover(index=body, navigation_policy='formula_search_index_v4')
    assert value['proof']['version'] == 14
    assert calls == [SEARCH, SEARCH_INDEX, FORMULA_DOC]
    assert value['discovery']['urls'] == [FORMULA_DOC, INDEX]
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value
    assert len(evidence.cached_page(SEARCH_INDEX, prior, value)['content']) == 937369


def test_proof13_keeps_512k_limit_for_the_same_index_and_old_receipt_replay():
    value, calls, prior = search_discover(index=large_index_text(937369))
    assert value['proof']['version'] == 13
    assert value['proof']['pages'][1]['status'] == 'unavailable'
    assert FORMULA_DOC not in calls and value['discovery'] is None
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value


@pytest.mark.parametrize('size', [1024*1024, 1024*1024+1])
def test_proof14_index_decompression_and_capture_upper_boundary(size):
    body = large_index_text(size)
    value, calls, prior = search_discover(index=body, navigation_policy='formula_search_index_v4')
    if size == evidence.MAX_INDEX_BYTES:
        assert value['discovery']['urls'] == [FORMULA_DOC, INDEX]
        assert len(evidence.cached_page(SEARCH_INDEX, prior, value)['content']) == size
    else:
        assert value['discovery'] is None and FORMULA_DOC not in calls
        assert value['proof']['pages'][1]['status'] == 'unavailable'
        with pytest.raises(ComputerError):
            evidence._unpack_index(base64.b64encode(zlib.compress(body.encode())).decode())
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value


def test_proof14_html_still_limited_to_512k_even_on_an_index_url():
    prior = search_history()
    request = evidence._request(ROOT, QUERY, (), prior, topic=TOPIC, navigation_policy='formula_search_index_v4')
    def fetch(url):
        return ('text/html', b'<script src="searchindex.js"></script>') if url == SEARCH else (
            'text/html', b'x'*(evidence.MAX_HTML_BYTES+1))
    value = evidence._discover(request, prior, fetch, AT)
    assert value['proof']['pages'][1]['status'] == 'unavailable'
    assert value['discovery'] is None
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=prior, topic=TOPIC) == value


def test_proof14_resource_retry_is_once_from_failed13_and_keeps_old_success_bytes():
    old, _, prior = search_discover(index=large_index_text(937369))
    old_bytes = json.dumps(old, sort_keys=True)
    combined = prior+[old]
    value, calls, _ = search_discover(combined, index=large_index_text(937369),
                                     navigation_policy='formula_search_index_v4')
    assert calls == [SEARCH_INDEX, FORMULA_DOC]
    assert value['proof']['pages'][0]['retry_from'] == {
        'prior': 1, 'page': 1, 'sha256': digest(old['proof']['pages'][1])}
    assert json.dumps(old, sort_keys=True) == old_bytes
    assert evidence.validate(value, ROOT, QUERY, prior_discoveries=combined, topic=TOPIC) == value
    again, repeated, _ = search_discover(combined+[value], index=large_index_text(937369),
                                        navigation_policy='formula_search_index_v4')
    assert repeated == [] and again['discovery']['urls'] == [FORMULA_DOC, INDEX]


def test_proof14_failed_retry_is_consumed_and_cannot_retry_again_or_retry_html():
    old, _, prior = search_discover(index=large_index_text(937369))
    combined = prior+[old]
    failed, calls, _ = search_discover(combined, index=large_index_text(evidence.MAX_INDEX_BYTES+1),
                                      navigation_policy='formula_search_index_v4')
    assert calls == [SEARCH_INDEX] and failed['discovery'] is None
    again, calls, _ = search_discover(combined+[failed], navigation_policy='formula_search_index_v4')
    assert calls == [] and again['discovery'] is None
    changed = copy.deepcopy(old)
    changed['proof']['pages'][1].pop('resource_kind')
    changed['proof']['pages'][1].pop('document_base')
    html_failure, calls, _ = search_discover(prior+[changed], navigation_policy='formula_search_index_v4')
    assert calls == [] and html_failure['discovery'] is None


def test_proof14_retry_from_tamper_rejected_without_network():
    old, _, prior = search_discover(index=large_index_text(937369))
    combined = prior+[old]
    value, _, _ = search_discover(combined, navigation_policy='formula_search_index_v4')
    value['proof']['pages'][0]['retry_from']['sha256'] = '0'*64
    with pytest.raises(ComputerError):
        evidence.validate(value, ROOT, QUERY, prior_discoveries=combined, topic=TOPIC)


@pytest.mark.parametrize('mime,size,large,accept,limit', [
    ('text/javascript', 937369, True, True, 1024*1024),
    ('text/javascript', 937369, False, False, 512*1024),
    ('text/javascript', 1024*1024+1, True, False, 1024*1024),
    ('text/html', 512*1024+1, True, False, 512*1024),
])
def test_pinned_resource_get_applies_versioned_mime_specific_read_cap(monkeypatch, mime, size, large, accept, limit):
    import socket
    from agent_service import browser
    calls = []
    class Response:
        status = 200
        def getheader(self, name, default=''):
            return mime if name == 'Content-Type' else 'identity'
        def read(self, bound):
            calls.append(('read', bound))
            return b'x'*min(size, bound)
    class Connection:
        def __init__(self, *args):
            pass
        def request(self, method, path, headers):
            calls.append((method, path))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append(('close',))
    monkeypatch.setattr(browser, 'PinnedConnection', Connection)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))])
    if accept:
        assert len(evidence._fetch_search_resource(SEARCH_INDEX, large_index=large)[1]) == size
    else:
        with pytest.raises(browser.SourceError):
            evidence._fetch_search_resource(SEARCH_INDEX, large_index=large)
    assert ('read', limit+1) in calls and sum(c[0] == 'GET' for c in calls) == 1


def test_real_retained13_failed_index_gets_only_one_bound_retry_in14():
    path = Path('validation/artifacts/formula-evidence-20261023/live-navigation-result.json')
    if not path.is_file():
        pytest.skip('Real retained navigation is intentionally outside Git')
    old = json.loads(path.read_text(encoding='utf8'))
    input_path = path.parent/'live-navigation'/('evidence-input-'+old['proof']['request_sha256']+'.json')
    payload = json.loads(input_path.read_text(encoding='utf8'))
    original = payload['request']
    assert evidence.validate(old, original['source_url'], original['query'], original['excluded_urls'],
        prior_discoveries=payload['prior_discoveries'], source_observations=payload['source_observations'],
        topic=original['topic']) == old
    prior = payload['prior_discoveries']+[old]
    request = evidence._request(original['source_url'], original['query'], original['excluded_urls'], prior,
        source_observations=payload['source_observations'], topic=original['topic'], navigation_policy='formula_search_index_v4')
    calls = []
    def blocked(url):
        calls.append(url)
        raise OSError('All live GETs blocked in this offline regression')
    value = evidence._discover(request, prior, blocked, AT, source_observations=payload['source_observations'])
    assert calls == [SEARCH_INDEX] and value['proof']['pages'][0]['retry_from']['prior'] == len(prior)-1
    assert value['proof']['pages'][0]['retry_from']['sha256'] == digest(old['proof']['pages'][1])
    assert evidence.validate(value, request['source_url'], request['query'], request['excluded_urls'],
        prior_discoveries=prior, source_observations=payload['source_observations'], topic=request['topic']) == value


def test_proof14_oversized_compressed_result_is_rejected_before_complete_result_transport(tmp_path, monkeypatch):
    from agent_service import process
    skeleton = large_index_text(937369)
    data = json.loads(skeleton[len('Search.setIndex('):-2])
    count = len(data['padding'])
    noise = ''.join(sha256(str(i).encode()).hexdigest() for i in range(count//64+1))[:count]
    data['padding'] = noise
    body = 'Search.setIndex('+json.dumps(data)+');'
    with pytest.raises(ComputerError, match='supplement_dimension_unavailable'):
        search_discover(index=body, navigation_policy='formula_search_index_v4')
    monkeypatch.setattr(process, 'run_child', lambda *a, **k: ' '*(evidence.MAX_RESULT_CHARS+1))
    with pytest.raises(ComputerError):
        evidence.EvidenceDiscoveryProcess().discover(ROOT, QUERY, prior_discoveries=search_history(),
            storage_dir=tmp_path, topic=TOPIC, navigation_policy='formula_search_index_v4')


def test_proof14_index_decompression_rejects_trailing_stream_and_invalid_base64():
    for value in (base64.b64encode(zlib.compress(b'Search.setIndex({})')+b'extra').decode(), '!!!!'):
        with pytest.raises(ComputerError):
            evidence._unpack_index(value)


def wrap_receipt(value):
    data = {'action': {'kind': 'supplement', 'query': QUERY, 'topic': TOPIC,
        'source_url': ROOT, 'source_id': digest(ROOT)[:24], 'need_id': 'n'*24,
        'capture_sha256': '0'*64, 'review_sha256': '0'*64, 'purpose': 'dimension_evidence_v6',
        'dimensions': ['Whether the library calculates formula results']},
        'link_result': value, 'discovery': value['discovery'], 'intent_sha256': '0'*64, 'at': AT,
        'result': {'ok': True, 'state': 'verified', 'code': 'search_discovered'}}
    return {'data': data, 'sha256': digest(data)}


def test_proof14_937369_byte_index_complete_receipt_uses_actual_300k_reader(tmp_path):
    from agent_service.computer import read_record, write_new
    value, _, _ = search_discover(index=large_index_text(937369), navigation_policy='formula_search_index_v4')
    assert len(evidence._json(value)) <= evidence.MAX_RESULT_CHARS
    record = wrap_receipt(value)
    path = tmp_path/'environment-result.json'
    write_new(path, record)
    assert path.stat().st_size < 300_000
    assert read_record(path) == record


def test_actual_retained_proof14_fits_existing_receipt_reader_without_graph_replay(tmp_path):
    from agent_service.computer import read_record, write_new
    source = Path('validation/artifacts/formula-evidence-20261023/live-navigation-v14-result.json')
    if not source.is_file():
        pytest.skip('Local real navigation bytes intentionally outside Git')
    value = json.loads(source.read_text(encoding='utf8'))
    assert value['proof']['version'] == 14
    assert len(evidence._json(value)) <= evidence.MAX_RESULT_CHARS
    record = wrap_receipt(value)
    path = tmp_path/'environment-result.json'
    write_new(path, record)
    assert path.stat().st_size < 300_000 and read_record(path) == record
