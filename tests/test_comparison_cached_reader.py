from copy import deepcopy
import pytest
from agent_service.computer import ComputerError, digest
from agent_service.comparison_cached_reader import CachedNavigationReader, validate_record
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.comparison_completion import CompletionEnvironment
from test_comparison_evidence_discovery import history, INDEX, AT


def test_cached_read_has_original_time_and_no_network():
    class Fallback:
        def read(self, *args, **kwargs):
            pytest.fail('cached page must never fetch')
    prior = history()
    reader = CachedNavigationReader(Fallback(), 'openpyxl author', prior)
    value = reader.read(INDEX)
    record = {'observation': value.to_dict(), 'document_proof': reader.last_document_proof}
    assert value.captured_at == AT and 'Eric Gazoni' in value.text
    assert validate_record(record, INDEX, 'openpyxl author', prior)
    for key in ('observation', 'document_proof'):
        wrong = deepcopy(record)
        wrong[key]['sha256' if key == 'observation' else 'prior_sha256'] = '0'*64
        with pytest.raises(ComputerError):
            validate_record(wrong, INDEX, 'openpyxl author', prior)
    with pytest.raises(ComputerError):
        validate_record(record, INDEX, 'changed query', prior)


def test_missing_cached_proof_or_unbound_page_cannot_pass():
    reader = CachedNavigationReader(None, 'openpyxl', history())
    value = reader.read(INDEX)
    with pytest.raises(ComputerError):
        validate_record({'observation': value.to_dict()}, INDEX, 'openpyxl', history())
    with pytest.raises(ComputerError):
        validate_record({'document_proof': reader.last_document_proof}, INDEX, 'openpyxl', [])


def test_navigation_prefix_does_not_include_current_or_future_proof():
    env = object.__new__(CrossSiteEnvironment)
    env._prior_discoveries = lambda: ['seed']
    records = [({'intent_sha256': str(n), 'action': {'kind': 'supplement'}, 'link_result': {'id': n}}, str(n)) for n in range(3)]
    env._records = lambda: records
    assert env._navigation_inputs(records[1][0]) == ['seed', {'id': 0}]
    assert env._navigation_inputs() == ['seed', {'id': 0}, {'id': 1}, {'id': 2}]


def test_execute_scope_avoids_reading_its_pending_intent_and_clears_on_error(monkeypatch):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'dimension_routing': 5}}
    calls = []
    env._records = lambda: calls.append(1) or []
    def execute(self, action, step):
        assert self._navigation_records() == []
        raise ComputerError('injected')
    monkeypatch.setattr(CompletionEnvironment, 'execute', execute)
    with pytest.raises(ComputerError, match='injected'):
        env.execute({}, None)
    assert calls == [1]
    assert not hasattr(env, '_navigation_execution_records')


def test_new_need_reads_both_formula_and_attribution():
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {}}
    facts = {'candidates': [{'id': 'a', 'need_id': 'n'}, {'id': 'b', 'need_id': 'n'}],
             'reads': {'a': {'result': {'ok': True}}}}
    need = {'purpose': 'dimension_evidence_v5', 'need_id': 'n'}
    assert not env._need_satisfied(facts, need)
    facts['reads']['b'] = {'result': {'ok': True}}
    assert env._need_satisfied(facts, need)


def test_new_route_does_not_repeat_search():
    env = object.__new__(CrossSiteEnvironment)
    with pytest.raises(ComputerError, match='search_unavailable'):
        env._search_candidates({'purpose': 'dimension_evidence_v5'}, 'prior query')


@pytest.mark.parametrize('route,limit', [(2, 8), (3, 10), (4, 10), (5, 16)])
def test_versioned_history_bound(route, limit):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'dimension_routing': route}}
    assert env._historical_limit() == limit


def test_two_page_supplement_reserves_both_reads(monkeypatch):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'dimension_routing': 5}, 'outputs': ['xlsx', 'docx']}
    monkeypatch.setattr(CompletionEnvironment, '_frontier', lambda *_: [
        {'kind': 'supplement', 'purpose': 'dimension_evidence_v5'}])
    assert env._frontier({'records': [None]*7}) == []


def test_final_compare_cannot_skip_selected_attribution_page(monkeypatch):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'dimension_routing': 5}, 'outputs': ['xlsx', 'docx']}
    monkeypatch.setattr(CompletionEnvironment, '_frontier', lambda *_: [{'kind': 'compare'}])
    facts = {'records': [None]*9, 'comparisons': [{}],
             'candidates': [{'id': 'a', 'need_id': 'n'}, {'id': 'b', 'need_id': 'n'}],
             'needs': [{'need_id': 'n', 'purpose': 'dimension_evidence_v5'}], 'reads': {'a': {}}}
    assert env._frontier(facts) == []


@pytest.mark.parametrize('seed', [None, [], 'invalid'])
def test_route5_malformed_seed_is_typed_failure(seed):
    from agent_service.source_comparison import validate_comparison
    with pytest.raises(ComputerError, match='comparison_invalid'):
        validate_comparison({'dimensions': ['formula'], 'min_domains': 2, 'evidence_version': 4,
                             'completion_policy': 'dimension_first_v4', 'dimension_routing': 5,
                             'seed': seed})


def test_consumed_urls_include_current_failed_read_but_not_future():
    env = object.__new__(CrossSiteEnvironment)
    url = 'https://sample.example/formula'
    env._repair_input = lambda: ([], {'read_urls': ['https://old.example/']})
    records = [({'action': {'kind': 'supplement'}, 'discovery': {'urls': [url]}, 'intent_sha256': '1'}, ''),
               ({'action': {'kind': 'read', 'candidate_id': digest(url)[:24]}, 'intent_sha256': '2'}, '')]
    env._navigation_records = lambda: deepcopy(records)
    assert env._consumed_urls() == {'https://old.example/', url}
    assert env._consumed_urls(records[1][0]) == {'https://old.example/'}


@pytest.mark.parametrize('ok', [True, False])
def test_consumed_urls_resolves_verified_inherited_unread_candidate_without_new_discovery(ok):
    env = object.__new__(CrossSiteEnvironment)
    url = 'https://inherited.example/formula'
    sid = digest(url)[:24]
    env._repair_input = lambda: ([], {'read_urls': ['https://old.example/'],
        'candidates': [{'id': sid, 'url': url, 'query_index': 1}]})
    record = {'action': {'kind': 'read', 'candidate_id': sid}, 'intent_sha256': 'inherited-read',
              'result': {'ok': ok, 'state': 'verified' if ok else 'failed'}}
    env._navigation_records = lambda: [(deepcopy(record), 'inherited-record')]
    assert env._consumed_urls(record) == {'https://old.example/'}
    assert env._consumed_urls() == {'https://old.example/', url}


def test_parsed_source_cache_still_validates_bundle_each_access(monkeypatch):
    from agent_service import comparison_compact_seed
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {'binding': 'immutable'}}}
    checked, loaded = [], []
    env._repair_input = lambda: checked.append(1)
    monkeypatch.setattr(comparison_compact_seed, 'load_navigation_observations',
        lambda manifest, topic: loaded.append(topic) or [{'text': topic}])
    first = env._navigation_sources({'topic': 'formula'})
    first[0]['text'] = 'changed'
    assert env._navigation_sources({'topic': 'formula'}) == [{'text': 'formula'}]
    assert checked == [1, 1] and loaded == ['formula']
    def changed():
        raise ComputerError('comparison_seed_changed')
    env._repair_input = changed
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        env._navigation_sources({'topic': 'formula'})


def test_parsed_source_cache_has_four_entries_and_seed_binding(monkeypatch):
    from agent_service import comparison_compact_seed
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'seed': {'binding': 'one'}}}
    env._repair_input = lambda: None
    loaded = []
    monkeypatch.setattr(comparison_compact_seed, 'load_navigation_observations',
        lambda manifest, topic: loaded.append((manifest['binding'], topic)) or [])
    for n in range(5):
        env._navigation_sources({'topic': str(n)})
    assert len(env._navigation_verified_sources) == 4
    env.goal['comparison']['seed']['binding'] = 'two'
    env._navigation_sources({'topic': '4'})
    assert loaded[-1] == ('two', '4')
