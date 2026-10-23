"""Offline dimension navigation, retained-byte replay and immutable worker inputs."""
import base64
import copy
from hashlib import sha256
import io
import json
from pathlib import Path
import zlib

import pytest

from agent_service import comparison_dimension_discovery as dimension
from agent_service.computer import ComputerError, digest

URL = 'https://github.com/shshe/openpyxl'
DOCS = 'https://openpyxl.readthedocs.io/en/stable/changes.html'
QUERY = 'site:github.com 是否计算Excel单元格公式的结果'
AT = '2026-10-23T14:00:05+00:00'


def page(url, text, depth=0, parent=None, anchor=None):
    raw = text.encode('utf-8')
    return {'url': url, 'parent': parent, 'anchor': anchor, 'depth': depth,
            'status': 'ok', 'mime': 'text/html; charset=utf-8',
            'html_zlib_base64': base64.b64encode(zlib.compress(raw)).decode('ascii'),
            'source_sha256': sha256(raw).hexdigest()}


def prior(pages=None, version=1):
    if pages is None:
        pages = [page(URL, '<a href="https://docs.github.com/">Documentation</a>'),
                 page(DOCS, '<a href="formula.html">Parsing Formulas</a>', 1, 0, 146)]
    return [{'discovery': None, 'proof': {'version': version, 'source_url': URL,
                                         'pages': pages}}]


def discover(history=None, pages=None, excluded=(), url=URL, query=QUERY,
             routing_version=1, topic=None):
    history = prior() if history is None else history
    pages = {} if pages is None else pages
    calls = []
    def fetch(target):
        calls.append(target)
        text = pages[target]
        if isinstance(text, Exception):
            raise text
        return 'text/html', text.encode('utf-8') if isinstance(text, str) else text
    value = dimension._discover(dimension._request(url, query, excluded, history,
                                routing_version=routing_version, topic=topic),
                                history, fetch, AT)
    return value, calls, history


def test_formula_candidate_uses_retained_descendant_without_any_get(monkeypatch):
    value, calls, history = discover()
    assert calls == []
    assert value['discovery']['urls'] == ['https://openpyxl.readthedocs.io/en/stable/formula.html']
    assert value['proof']['pages'] == []
    assert value['proof']['chosen'][0]['depth'] == 2
    assert value['proof']['chosen'][0]['parent'] == {
        'prior': 0, 'page': 1, 'sha256': digest(history[0]['proof']['pages'][1])}
    from agent_service import browser
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('validation used network'))
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history) == value
    assert 'html_zlib_base64' not in json.dumps(value)


def test_original_proof6_exact_golden_digest_and_evaluation_routing_preserved():
    value, _, history = discover()
    # Recorded before revision two; protects the original receipt algorithm.
    assert digest(value) == '3b818cd84956527679c72643a47efcfae5e653243d18e5ac50f77e572506c432'
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history) == value
    history = prior([page(URL, '<a href="https://example.org/metrics-evaluation">Evaluation</a>')])
    legacy, calls, _ = discover(history)
    assert calls == [] and legacy['discovery']['urls'] == ['https://example.org/metrics-evaluation']


def test_revision2_reuses_changes_formula_without_get_and_binds_topic():
    topic = 'openpyxl Python Excel documentation'
    value, calls, history = discover(routing_version=2, topic=topic)
    assert calls == []
    assert value['proof']['version'] == 7
    assert value['proof']['routing_version'] == 2 and value['proof']['topic'] == topic
    assert value['discovery']['urls'] == ['https://openpyxl.readthedocs.io/en/stable/formula.html']
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history,
                              routing_version=2, topic=topic) == value
    for kwargs in ({}, {'routing_version': 2, 'topic': 'XlsxWriter Python Excel documentation'}):
        with pytest.raises(ComputerError):
            dimension.validate(value, URL, QUERY, prior_discoveries=history, **kwargs)


@pytest.mark.parametrize('label,path', [
    ('Machine learning metrics evaluation', '/machine-learning/metrics-evaluation/'),
    ('Calculate statistics', '/calculate-statistics/'),
    ('Evaluation manual', '/evaluation-manual/'),
    ('Documentation', '/python/data-science-tutorial/'),
])
def test_revision2_rejects_ml_evaluation_and_unrelated_gateways(label, path):
    source = 'https://www.geeksforgeeks.org/python/xlsxwriter/'
    history = [{'discovery': None, 'proof': {'version': 1, 'source_url': source,
        'pages': [page(source, '<a href="'+path+'">'+label+'</a>')]}}]
    value, calls, _ = discover(history, url=source, routing_version=2,
                              topic='XlsxWriter Python Excel documentation')
    assert calls == [] and value['discovery'] is None


def test_revision2_evaluation_only_boosts_formula_candidate():
    history = prior([page(URL,
        '<a href="https://docs.example.org/formula">Parsing Formula</a>'
        '<a href="https://docs.example.org/evaluation">Evaluation</a>'
        '<a href="https://docs.example.org/formulas-evaluation">Formula Evaluation</a>')])
    value, calls, _ = discover(history, routing_version=2, topic='openpyxl documentation')
    assert calls == []
    assert value['discovery']['urls'] == ['https://docs.example.org/formulas-evaluation']


def test_revision2_gateway_uses_product_host_path_or_label():
    gateway = 'https://xlsxwriter.readthedocs.io/help'
    source = 'https://www.geeksforgeeks.org/python/xlsxwriter/'
    history = [{'discovery': None, 'proof': {'version': 1, 'source_url': source,
        'pages': [page(source, '<a href="https://docs.example.org/help">Python Documentation</a>'
                              '<a href="'+gateway+'">Documentation</a>')]}}]
    value, calls, _ = discover(history, {gateway: '<a href="/formulas">Formula Evaluation</a>'},
        url=source, routing_version=2, topic='XlsxWriter Python Excel documentation')
    assert calls == [gateway]
    assert value['discovery']['urls'] == ['https://xlsxwriter.readthedocs.io/formulas']


def test_revision2_unknown_product_gateway_stays_on_host():
    source = 'https://example.org/root'
    history = [{'discovery': None, 'proof': {'version': 1, 'source_url': source,
        'pages': [page(source, '<a href="https://elsewhere.org/help">Documentation</a>'
                              '<a href="/help">Help</a>')]}}]
    value, calls, _ = discover(history, {'https://example.org/help': OSError()},
        url=source, routing_version=2, topic='Python Excel formula documentation')
    assert calls == ['https://example.org/help'] and value['discovery'] is None


@pytest.mark.parametrize('older_routing', [1, 2, 3])
def test_revision3_reuses_dimension6_7_8_navigation_and_refs_without_get(older_routing):
    gateway = 'https://openpyxl.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    kwargs = {'routing_version': older_routing,
              'topic': 'openpyxl documentation' if older_routing > 1 else None}
    first, calls, _ = discover(history, {gateway: '<a href="/formula">Parsing Formulas</a>'},
                               query='site:github.com format', **kwargs)
    assert calls == [gateway] and first['discovery'] is None
    combined = history + [first]
    value, calls, _ = discover(combined, routing_version=3, topic='openpyxl documentation')
    assert calls == [] and value['proof']['version'] == 8
    assert value['discovery']['urls'] == ['https://openpyxl.example.org/formula']
    assert value['proof']['chosen'][0]['depth'] == 2
    assert value['proof']['chosen'][0]['parent'] == {
        'prior': 1, 'page': 0, 'sha256': digest(first['proof']['pages'][0])}
    assert dimension.validate(value, URL, QUERY, prior_discoveries=combined,
                              routing_version=3, topic='openpyxl documentation') == value


def test_revision3_same_prefix_multiple_receipts_and_cache_dedup():
    gateway = 'https://openpyxl.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    first, _, _ = discover(history, {gateway: '<a href="/formula">Formula</a>'},
        query='site:github.com format', routing_version=2, topic='openpyxl documentation')
    combined = history + [first, copy.deepcopy(first)]
    value, calls, _ = discover(combined, routing_version=3, topic='openpyxl documentation')
    assert calls == []
    assert len(value['proof']['cached_refs']) == 2
    assert value['proof']['chosen'][0]['parent']['prior'] == 1


@pytest.mark.parametrize('change', [
    lambda p: p.__setitem__('prior_sha256', '0'*64),
    lambda p: p['cached_refs'][0].__setitem__('prior', True),
    lambda p: p['cached_refs'][0].__setitem__('page', 1),
    lambda p: p['cached_refs'][0].__setitem__('sha256', '0'*64),
    lambda p: p['pages'][0].__setitem__('depth', 4),
    lambda p: p['pages'][0].__setitem__('source_sha256', '0'*64),
])
def test_revision3_bad_dimension_cache_provenance_rejected(change):
    gateway = 'https://openpyxl.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    first, _, _ = discover(history, {gateway: '<a href="/formula">Formula</a>'},
        query='site:github.com format', routing_version=2, topic='openpyxl documentation')
    change(first['proof'])
    with pytest.raises(ComputerError):
        discover(history+[first], routing_version=3, topic='openpyxl documentation')


def test_revision3_old_dimension_failed_get_not_repeated():
    gateway = 'https://openpyxl.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    first, calls, _ = discover(history, {gateway: OSError()}, query='site:github.com format',
                               routing_version=2, topic='openpyxl documentation')
    assert calls == [gateway]
    value, calls, _ = discover(history+[first], routing_version=3, topic='openpyxl documentation')
    assert calls == [] and value['discovery'] is None


def test_revision3_removes_site_restriction_from_product_vocabulary():
    source = 'https://example.org/root'
    history = [{'discovery': None, 'proof': {'version': 1, 'source_url': source,
        'pages': [page(source, '<a href="https://example.net/help">Documentation</a>')]}}]
    value, calls, _ = discover(history, url=source, routing_version=3,
                              topic='site:example.net Python Excel documentation')
    assert calls == [] and value['discovery'] is None


@pytest.mark.parametrize('count', [9, 10])
def test_revision3_allows_ten_exclusions_but_old_versions_stay_at_eight(count):
    excluded = ['https://example.org/'+str(i) for i in range(count)]
    request = dimension._request(URL, QUERY, excluded, [], routing_version=3, topic='openpyxl')
    assert len(request['excluded_urls']) == count
    for revision in (1, 2):
        with pytest.raises(ComputerError):
            dimension._request(URL, QUERY, excluded, [], routing_version=revision,
                               topic='openpyxl' if revision == 2 else None)


def test_revision3_rejects_eleven_exclusions():
    with pytest.raises(ComputerError):
        dimension._request(URL, QUERY, ['https://example.org/'+str(i) for i in range(11)], [],
                           routing_version=3, topic='openpyxl')


@pytest.mark.parametrize('path', ['/_sources/formula.rst.txt', '/formula.rst.txt', '/_sources/formula.md'])
def test_revision4_rejects_document_source_export_but_revision3_unchanged(path):
    target = 'https://openpyxl.example.org' + path
    history = prior([page(URL, '<a href="'+target+'">Formula documentation source</a>')])
    old, calls, _ = discover(history, routing_version=3, topic='openpyxl documentation')
    assert calls == [] and old['discovery']['urls'] == [target]
    value, calls, _ = discover(history, routing_version=4, topic='openpyxl documentation')
    assert calls == [] and value['discovery'] is None
    assert value['proof']['version'] == 9
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history,
                              routing_version=4, topic='openpyxl documentation') == value


def test_revision4_unknown_document_extension_remains_candidate():
    target = 'https://openpyxl.example.org/formula.custom'
    history = prior([page(URL, '<a href="'+target+'">Formula evaluation</a>')])
    value, calls, _ = discover(history, routing_version=4, topic='openpyxl documentation')
    assert calls == [] and value['discovery']['urls'] == [target]


def test_revision4_reuses_prior9_navigation_cache_with_no_get():
    gateway = 'https://openpyxl.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    first, calls, _ = discover(history, {gateway: '<a href="/formula.html">Formula</a>'},
        query='site:github.com format', routing_version=4, topic='openpyxl documentation')
    assert calls == [gateway] and first['proof']['version'] == 9
    combined = history+[first]
    value, calls, _ = discover(combined, routing_version=4, topic='openpyxl documentation')
    assert calls == [] and value['discovery']['urls'] == ['https://openpyxl.example.org/formula.html']
    assert dimension.validate(value, URL, QUERY, prior_discoveries=combined,
                              routing_version=4, topic='openpyxl documentation') == value


@pytest.mark.parametrize('revision,topic', [(True, None), (3, None), (2, None), (2, ''), (1, 'openpyxl')])
def test_routing_revision_and_topic_request_rejected(revision, topic):
    with pytest.raises(ComputerError):
        dimension._request(URL, QUERY, (), [], routing_version=revision, topic=topic)


@pytest.mark.parametrize('version', [1, 2, 3, 4])
def test_supported_old_versions(version):
    value, calls, _ = discover(history=prior(version=version))
    assert calls == [] and value['discovery'] is not None


def test_dimension_match_precedes_gateway_and_word_boundaries_avoid_information():
    history = prior([page(URL, '<a href="/copyright-information">Copyright information</a>'
        '<a href="https://docs.example.org/help">Documentation</a>'
        '<a href="https://docs.example.org/format.html">Format</a>')])
    value, calls, _ = discover(history, query='site:github.com format')
    assert calls == []
    assert value['discovery']['urls'] == ['https://docs.example.org/format.html']


def test_failed_get_and_old_success_never_repeated_and_old_depth_not_reset():
    history = prior([page(URL, '<a href="https://docs.example.org/help">Help</a>'),
        {'url': 'https://docs.example.org/help', 'depth': 1, 'parent': 0,
         'anchor': 0, 'status': 'unavailable'},
        page(DOCS, '<a href="formula.html">Formulas</a>', 3, 0, 1)])
    value, calls, _ = discover(history)
    assert calls == [] and value['discovery'] is None


def test_new_gateway_get_but_candidate_returned_without_get():
    gateway = 'https://docs.example.org/help'
    history = prior([page(URL, '<a href="'+gateway+'">Documentation</a>')])
    value, calls, _ = discover(history, {gateway: '<a href="/formula">Formula evaluation</a>'})
    assert calls == [gateway]
    assert value['discovery']['urls'] == ['https://docs.example.org/formula']
    assert value['proof']['chosen'][0]['parent'] == {'new': 0}
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history) == value


def test_no_history_fetches_root_once_and_retains_failure():
    value, calls, _ = discover([], {URL: OSError('not available')})
    assert calls == [URL] and value['discovery'] is None
    assert value['proof']['pages'][0]['status'] == 'unavailable'
    assert dimension.validate(value, URL, QUERY, prior_discoveries=[]) == value


def test_new_get_depth_and_page_limits():
    root = 'https://example.org/root'
    pages = {root: '<a href="/help">Help</a>',
             'https://example.org/help': '<a href="/manual">Manual</a>',
             'https://example.org/manual': '<a href="/guide">Guide</a>',
             'https://example.org/guide': '<a href="/formula">Formula</a>'}
    value, calls, _ = discover([], pages, url=root)
    assert len(calls) == 4
    assert max(p['depth'] for p in value['proof']['pages']) == 3
    assert value['discovery'] is None


@pytest.mark.parametrize('href', [
    'https://github.com/other/openpyxl/blob/main/formula.py',
    'https://github.com/shshe/other/blob/main/formula.py',
    'https://gitlab.com/other/project/-/blob/main/formula.py',
    'https://127.0.0.1/formula', 'http://localhost/formula',
    'javascript:formula()', '/formula.zip', '#formula'])
def test_other_projects_and_unsafe_links_never_used(href):
    value, calls, _ = discover(prior([page(URL, '<a href="'+href+'">Formulas</a>')]))
    assert calls == [] and value['discovery'] is None


def test_same_project_candidate_allowed_without_identity_transfer():
    target = URL + '/blob/main/formula.py'
    value, calls, _ = discover(prior([page(URL, '<a href="'+target+'">Formula</a>')]))
    assert calls == [] and value['discovery']['urls'] == [target]
    assert set(value) == {'discovery', 'proof'}


def test_excluded_formula_never_selected():
    target = 'https://openpyxl.readthedocs.io/en/stable/formula.html'
    value, calls, _ = discover(excluded=[target])
    assert calls == [] and value['discovery'] is None


def test_different_source_prior_not_reused():
    history = prior()
    history[0]['proof']['source_url'] = 'https://github.com/other/other'
    value, calls, _ = discover(history, {URL: OSError()})
    assert calls == [URL] and value['proof']['cached_refs'] == []


def test_dynamic_v5_resource_schema_ignored():
    history = prior(version=5)
    history[0]['proof']['pages'] = [{'resources': 'not legacy HTML'}]
    value, calls, _ = discover(history, {URL: OSError()})
    assert calls == [URL] and value['proof']['cached_refs'] == []


@pytest.mark.parametrize('change', [
    lambda x: x['proof'].__setitem__('query', 'different'),
    lambda x: x['proof'].__setitem__('prior_sha256', '0'*64),
    lambda x: x['proof']['cached_refs'][0].__setitem__('page', 1),
    lambda x: x['proof']['chosen'][0].__setitem__('anchor', True),
    lambda x: x['proof']['chosen'][0].__setitem__('depth', 0),
    lambda x: x['proof']['chosen'][0].__setitem__('url', 'https://example.org/formula'),
    lambda x: x['proof'].__setitem__('version', True),
    lambda x: x['proof'].__setitem__('extra', 'bad'),
])
def test_proof_tampering_rejected(change):
    value, _, history = discover()
    change(value)
    with pytest.raises(ComputerError, match='dimension_proof_invalid'):
        dimension.validate(value, URL, QUERY, prior_discoveries=history)


def test_new_html_and_historical_html_tampering_rejected():
    history = prior([page(URL, '<a href="https://docs.example.org/help">Help</a>')])
    value, _, _ = discover(history, {'https://docs.example.org/help': '<a href="/formula">Formula</a>'})
    altered = copy.deepcopy(value)
    altered['proof']['pages'][0]['source_sha256'] = '0'*64
    with pytest.raises(ComputerError):
        dimension.validate(altered, URL, QUERY, prior_discoveries=history)
    history[0]['proof']['pages'][0]['depth'] = 2
    with pytest.raises(ComputerError):
        dimension.validate(value, URL, QUERY, prior_discoveries=history)


@pytest.mark.parametrize('raw', [b'', b'x'*(dimension.MAX_HTML_BYTES+1)], ids=['empty', 'oversize'])
def test_invalid_new_html_retained_as_failure(raw):
    value, _, _ = discover([], {URL: raw})
    assert value['discovery'] is None
    assert value['proof']['pages'][0]['status'] == 'unavailable'


def test_decompression_limit_and_trailing_bytes():
    for raw in (zlib.compress(b'x'*(dimension.MAX_HTML_BYTES+1)), zlib.compress(b'x')+b'junk'):
        with pytest.raises(ComputerError):
            dimension._unpack(base64.b64encode(raw).decode('ascii'))


def test_worker_uses_small_argv_immutable_file_and_revalidates(monkeypatch, tmp_path):
    from agent_service import process
    value, _, history = discover()
    commands = []
    def child(command, **kwargs):
        commands.append(command)
        payload = dimension._input(command[-2], command[-1])
        assert payload['prior_discoveries'] == history
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    worker = dimension.DimensionDiscoveryProcess()
    assert worker.discover(URL, QUERY, prior_discoveries=history, storage_dir=tmp_path) == value
    assert worker.discover(URL, QUERY, prior_discoveries=history, storage_dir=tmp_path) == value
    assert len(list(tmp_path.glob('dimension-input-*.json'))) == 1
    assert len(' '.join(commands[0])) < 1000
    path = Path(commands[0][-2])
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError):
        worker.discover(URL, QUERY, prior_discoveries=history, storage_dir=tmp_path)


def test_revision2_worker_input_and_output_roundtrip(monkeypatch, tmp_path):
    from agent_service import process
    topic = 'openpyxl Python Excel documentation'
    value, _, history = discover(routing_version=2, topic=topic)
    def child(command, **_kwargs):
        payload = dimension._input(command[-2], command[-1])
        assert payload['request']['routing_version'] == 2
        assert payload['request']['topic'] == topic
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    assert dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=history,
        storage_dir=tmp_path, routing_version=2, topic=topic) == value


@pytest.mark.parametrize('timeout', [0, -1, 41, True, float('nan'), float('inf')])
def test_invalid_timeout_does_not_dispatch(monkeypatch, tmp_path, timeout):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: pytest.fail('dispatched'))
    with pytest.raises(ComputerError, match='dimension_timeout'):
        dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=[],
                                                      storage_dir=tmp_path, timeout=timeout)


def test_cancel_before_and_after_dispatch(monkeypatch, tmp_path):
    from agent_service import process
    worker = dimension.DimensionDiscoveryProcess()
    with pytest.raises(ComputerError, match='dimension_cancelled'):
        worker.discover(URL, QUERY, prior_discoveries=[], storage_dir=tmp_path, should_stop=lambda: True)
    flags = iter([False, True])
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: '{}')
    with pytest.raises(ComputerError, match='dimension_cancelled'):
        worker.discover(URL, QUERY, prior_discoveries=[], storage_dir=tmp_path, should_stop=lambda: next(flags))


@pytest.mark.parametrize('error,code', [
    (InterruptedError(), 'cancelled'), (TimeoutError(), 'timeout'),
    (OSError(), 'unavailable'), (RuntimeError(), 'unavailable')])
def test_worker_failure_mapping(monkeypatch, tmp_path, error, code):
    from agent_service import process
    def child(*_a, **_k):
        raise error
    monkeypatch.setattr(process, 'run_child', child)
    with pytest.raises(ComputerError, match='dimension_'+code):
        dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=[], storage_dir=tmp_path)


@pytest.mark.parametrize('raw,code', [
    ('broken', 'proof_invalid'), ('{}', 'proof_invalid'),
    ('{"error":"supplement_dimension_unavailable"}', 'unavailable'),
    (None, 'proof_invalid')])
def test_invalid_worker_output(monkeypatch, tmp_path, raw, code):
    from agent_service import process
    monkeypatch.setattr(process, 'run_child', lambda *_a, **_k: raw)
    with pytest.raises(ComputerError, match='dimension_'+code):
        dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=[], storage_dir=tmp_path)


def test_input_changed_during_dispatch_rejected(monkeypatch, tmp_path):
    from agent_service import process
    value, _, history = discover()
    def child(command, **_kwargs):
        Path(command[-2]).write_text('{}', encoding='utf-8')
        return json.dumps(value)
    monkeypatch.setattr(process, 'run_child', child)
    with pytest.raises(ComputerError, match='dimension_proof_invalid'):
        dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=history, storage_dir=tmp_path)


def test_input_and_result_utf8_limits(monkeypatch, tmp_path):
    value, _, history = discover()
    monkeypatch.setattr(dimension, 'MAX_INPUT_BYTES', len(dimension._json(history))-1)
    with pytest.raises(ComputerError):
        dimension.DimensionDiscoveryProcess().discover(URL, QUERY, prior_discoveries=history, storage_dir=tmp_path)
    monkeypatch.setattr(dimension, 'MAX_INPUT_BYTES', 2*1024*1024)
    monkeypatch.setattr(dimension, 'MAX_RESULT_CHARS', len(dimension._json(value))-1)
    with pytest.raises(ComputerError):
        dimension.validate(value, URL, QUERY, prior_discoveries=history)


def test_child_reads_hash_bound_file_then_uses_public_fetch_gate(monkeypatch, tmp_path):
    from agent_service import browser
    history = []
    request = dimension._request(URL, QUERY, (), history)
    payload = {'request': request, 'prior_discoveries': history}
    path = tmp_path/'input.json'
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    output = io.BytesIO()
    monkeypatch.setattr(dimension.sys, 'argv', ['worker', str(path), sha256(raw).hexdigest()])
    monkeypatch.setattr(dimension.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(b'1')})())
    monkeypatch.setattr(dimension.sys, 'stdout', type('Output', (), {'buffer': output})())
    calls = []
    def fetch(target):
        calls.append(target)
        return 'text/html', b'<a href="https://docs.example.org/formula">Formula</a>'
    monkeypatch.setattr(browser, 'fetch_source', fetch)
    assert dimension.main() == 0
    value = json.loads(output.getvalue())
    assert calls == [URL]
    assert value['discovery']['urls'] == ['https://docs.example.org/formula']
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history) == value


def test_worker_gate_without_external_work(monkeypatch):
    monkeypatch.setattr(dimension.sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(b'0')})())
    assert dimension.main() == 2


@pytest.mark.parametrize('routing_version', [1, 2])
def test_exact_saved_real_changes_anchor_when_available(routing_version):
    path = Path('data/computer_sessions/computer-b342c0073a8d4f80b3c9683ac49e1f77/goals/'
                'c1eaacaf67a7c9dac17e6c4dff825ae6fc90747f3f1113a9ba3f2c12520fabf8.run/'
                'step-03/environment-result.json')
    if not path.is_file():
        pytest.skip('local retained real HTTP proof unavailable')
    history = [json.loads(path.read_text('utf-8'))['data']['link_result']]
    kwargs = {'routing_version': routing_version,
              'topic': 'openpyxl Python Excel documentation' if routing_version == 2 else None}
    value, calls, _ = discover(history, **kwargs)
    assert calls == []
    assert value['discovery']['urls'] == ['https://openpyxl.readthedocs.io/en/stable/formula.html']
    assert value['proof']['chosen'][0]['anchor'] in {2, 987, 991}
    assert dimension.validate(value, URL, QUERY, prior_discoveries=history, **kwargs) == value
