"""Repository-scoped repair, proof replay and pause/resume (offline backends)."""
from copy import deepcopy
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_service import comparison_crosssite_discovery as links, comparison_seed
from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.comparison_completion import CompletionEnvironment
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.computer import ComputerError, ComputerSession, digest, read_record
from agent_service.computer_agent_tools import _selection
from agent_service.contracts import Observation
from agent_service.publisher_evidence import LEGACY_POLICY
from agent_service.publisher_scope import POLICY
from agent_service.source_comparison import introduction
from test_computer_agent import Client
from test_computer_agent_tools import ctx as tool_ctx, STAMP
from test_comparison_crosssite import execute

ROOT = 'https://github.com/acme/sheets'
AUTHORS = ROOT + '/blob/main/AUTHORS.rst'
OTHER = 'https://github.com/acme/other/blob/main/AUTHORS.rst'


def missing(env_type, *, policy=POLICY, url=ROOT):
    env = object.__new__(env_type)
    env.goal = {'queries': ['first', 'second'], 'comparison': {
        'dimensions': ['XLSX'], 'publisher_policy': policy, 'evidence_version': 4}}
    items = [{'id': 'other', 'url': 'https://github.com/acme/other', 'query_index': 0},
             {'id': 'target', 'url': url, 'query_index': 1}]
    facts = {'candidates': items, 'active_ids': ['target'],
             'reads': {'target': {'observation': {'text': 'XLSX'}}}}
    evidence = {'assessment': {'group_details': [{'ids': ['target'], 'complete': False,
        'publisher_supported': False, 'covered_dimensions': [True]}],
        'publisher_support': {'target': None}}}
    return env, facts, evidence


@pytest.mark.parametrize('env_type', [CompletionEnvironment, CrossSiteEnvironment])
def test_repo_need_query_source_and_capture_are_bound(env_type):
    env, facts, evidence = missing(env_type)
    need, = env._needs(facts, evidence)
    assert need['purpose'] == 'repository_attribution'
    assert need['source_id'] == 'target' and need['scope_url'] == ROOT
    assert need['query'].startswith('site:github.com/acme/sheets ')
    assert env._link_inputs(facts, need)[0] == ROOT
    assert env._supplement_allowed({}, need, [AUTHORS])
    assert not env._supplement_allowed({}, need, [OTHER])
    assert not env._supplement_allowed({'link_result': {'discovery': {'urls': [OTHER]}}}, need, [OTHER])
    facts['reads']['target']['observation']['text'] = 'changed'
    with pytest.raises(ComputerError, match='comparison_seed_capture_changed'):
        env._link_inputs(facts, need)


@pytest.mark.parametrize('env_type', [CompletionEnvironment, CrossSiteEnvironment])
def test_ambiguous_repo_never_creates_whole_platform_attribution_search(env_type):
    env, facts, evidence = missing(env_type, url='https://github.com/acme')
    assert env._needs(facts, evidence) == []
    env.goal['comparison']['publisher_policy'] = LEGACY_POLICY
    need, = env._needs(facts, evidence)
    assert need['query'].startswith('site:github.com ') and 'scope_url' not in need


def test_cached_other_repo_cannot_satisfy_v3_need():
    env, facts, evidence = missing(CompletionEnvironment)
    need, = env._needs(facts, evidence)
    assert env._matches_need({'url': AUTHORS}, need)
    assert not env._matches_need({'url': OTHER}, need)


def test_v3_need_deduplication_uses_repository_scope():
    env, facts, evidence = missing(CompletionEnvironment)
    facts['reads']['other'] = {'observation': {'text': 'other'}}
    evidence['assessment']['group_details'].append({
        'ids': ['other'], 'complete': False, 'publisher_supported': False,
        'covered_dimensions': [True]})
    evidence['assessment']['publisher_support']['other'] = None
    assert {n['source_id'] for n in env._needs(facts, evidence)} == {'target', 'other'}
    env.goal['comparison']['publisher_policy'] = LEGACY_POLICY
    assert len(env._needs(facts, evidence)) == 1


def test_replay_binds_query_to_source_not_first_same_host_candidate():
    env, captured, evidence = missing(CompletionEnvironment)
    need, = env._needs(captured, evidence)
    action = {'kind': 'supplement', **need}
    initial = CompletionEnvironment._empty(env)
    initial.update(deepcopy(captured))
    env._empty = lambda: deepcopy(initial)
    env._frontier = lambda facts: [action]
    record = {'action': action, 'result': {'ok': True, 'state': 'verified', 'code': 'search_discovered'},
              'discovery': {'urls': [AUTHORS], 'searched_at': STAMP}, 'at': STAMP}
    preserved = deepcopy(record)
    result = env._replay([(record, digest(record))])
    assert result['candidates'][-1]['query_index'] == 1
    assert result['candidates'][-1]['need_id'] == need['need_id']
    assert env._replay([(record, digest(record))]) == result and record == preserved


@pytest.mark.parametrize('version', [3, 4])
def test_report_new_scope_and_old_text(version):
    goal = {'comparison': {'evidence_version': version, 'publisher_policy': POLICY}}
    assert '同一仓库' in introduction(goal)
    goal['comparison']['publisher_policy'] = LEGACY_POLICY
    old = introduction(goal)
    del goal['comparison']['publisher_policy']
    assert introduction(goal) == old and '同精确主机' in old


@pytest.fixture
def scoped_run(tool_ctx, monkeypatch):
    c = tool_ctx
    c['session'] = ComputerSession.create(c['run'].parent.parent / ('repo-' + digest(str(c['run']))[:8]))
    roots = ['https://independent.example/', ROOT]
    text = {roots[0]: 'Published by Independent. XLSX supported.', ROOT: 'XLSX supported.',
            AUTHORS: 'This project was started by Alice Smith.'}
    captured = []
    for i, url in enumerate(roots):
        sid = digest(url)[:24]
        data = Observation(url, url, 'Source', text[url], STAMP).to_dict()
        captured.append(({'id': sid, 'url': url, 'query_index': i}, {
            'observation': data, 'excerpt': _selection(text[url], 'XLSX', []),
            'action': {'kind': 'read', 'candidate_id': sid}, 'at': STAMP,
            'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'},
            'read_purpose': {'purpose': 'initial'}}))
    seed = {'version': 1, 'queries': ['first', 'second'], 'source_ids': [i['id'] for i, _ in captured]}
    monkeypatch.setattr(comparison_seed, 'validate_manifest', lambda value: deepcopy(value))
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda value: deepcopy(captured))
    c['goal'].update(min_sources=2, required_terms=[], comparison={
        'dimensions': ['XLSX'], 'min_domains': 2, 'evidence_version': 4, 'seed': seed})
    c['browser'].text.update(text)
    c['discovery_calls'] = []
    def discover(self, url, query, excluded_urls=(), **kwargs):
        c['discovery_calls'].append(url)
        pages = {ROOT: f'<a href="{OTHER}">Authors</a><a href="{AUTHORS}">Authors</a>',
                 AUTHORS: '<h1>Authors</h1><p>This project was started by Alice Smith.</p>'}
        return links._discover(links._request(url, query, excluded_urls, purpose=kwargs['purpose']),
                               lambda target: ('text/html', pages[target].encode()), STAMP)
    monkeypatch.setattr(links.CrossSiteDiscoveryProcess, 'discover', discover)
    from agent_service.computer_search import SearchProcess
    def no_network(*args, **kwargs):
        raise AssertionError('offline fixture must not invoke real search')
    monkeypatch.setattr(SearchProcess, 'search', no_network)
    def model(data):
        if 'selection' not in data:
            return {'sources': [{'id': s['id'], 'spans': [{'quote': 'XLSX supported.'}]
                    if 'XLSX supported.' in s['text'] else [None]} for s in data['sources']]}
        def publisher(s):
            if s['url'] == roots[0]:
                return {'name': 'Independent', 'quote': 'Published by Independent.'}
            if s['url'] == AUTHORS:
                return {'name': 'Alice Smith', 'quote': text[AUTHORS]}
            return None
        return {'sources': [{'id': s['id'], 'publisher': publisher(s), 'cells': [
                    {'status': 'relevant' if 'XLSX supported.' in s['text'] else 'missing',
                     'reason': 'Exact captured text'}]} for s in data['sources']],
                'pairs': [{'left': a['id'], 'right': b['id'], 'relation': 'same'
                    if a['url'].startswith(ROOT) and b['url'].startswith(ROOT) else 'independent',
                    'reason': 'Explicit publication provenance'} for a, b in combinations(data['sources'], 2)]}
    c['client'] = Client(model)
    c['options']['search'] = None
    return c


@pytest.mark.parametrize('halt', [2, 3])
def test_new_repository_proof_resumes_without_repeating_completed_effects(scoped_run, halt):
    c = scoped_run
    # Construct a historical v2 approval; execution/recovery still uses current code.
    with patch('agent_service.publisher_evidence.POLICY', POLICY):
        plan = engine.prepare(c['session'], 'Find attribution in this repository', c['goal'])
    first = execute(c, plan, halt)
    assert not first['ok'] and not c['effects']
    before = {p: p.read_bytes() for p in Path(first['run']).rglob('*') if p.is_file()}
    supplement = read_record(Path(first['run']) / 'step-02/environment-result.json')['data']
    assert supplement['link_result']['proof']['version'] == 3
    assert supplement['action']['purpose'] == 'repository_attribution'
    resumed = recovery.prepare(c['session'], first['run'])['plan']
    result = execute(c, resumed)
    assert result['ok'], result
    assert c['discovery_calls'] == [ROOT] and c['browser'].calls == [AUTHORS]
    assert all(p.read_bytes() == value for p, value in before.items())
    assert len(c['effects']) == 2 and len(c['client'].calls) == 4


def test_wrong_repository_search_is_retained_as_failure_without_office(scoped_run, monkeypatch):
    c = scoped_run
    c['options']['search'] = c['search']
    def search(query, *args, **kwargs):
        return {'urls': [OTHER], 'searched_at': STAMP}
    monkeypatch.setattr(c['search'], 'search', search)
    plan = engine.prepare(c['session'], 'Find attribution', c['goal'])
    result = execute(c, plan)
    assert not result['ok'] and not c['effects'] and not c['browser'].calls
    receipt = read_record(Path(result['run']) / 'step-02/environment-result.json')['data']
    assert receipt['result']['code'] == 'supplement_host_mismatch'
    assert receipt['discovery']['urls'] == [OTHER]
