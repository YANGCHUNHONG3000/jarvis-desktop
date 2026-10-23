"""Offline v4 loop/recovery; source import and navigation contracts tested separately."""
from copy import deepcopy
from itertools import combinations
from pathlib import Path

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, ComputerSession, digest, read_record
from agent_service.computer_agent_tools import AgentEnvironment, _selection
from agent_service.comparison_crosssite import CrossSiteEnvironment
from agent_service.contracts import Observation
from test_computer_agent import Client
from test_computer_agent_tools import ctx as tool_ctx, STAMP


@pytest.fixture
def v4(tool_ctx, monkeypatch):
    from agent_service import comparison_seed
    c = tool_ctx
    c['session'] = ComputerSession.create(c['run'].parent.parent / ('v4-'+digest(str(c['run']))[:8]))
    roots = ['https://alpha.example/', 'https://beta.other/']
    texts = {url: f'Published by Group{i}. Editing supported.' for i, url in enumerate(roots)}
    captured = []
    for i, url in enumerate(roots):
        data = Observation(url, url, 'Source', texts[url], STAMP).to_dict()
        captured.append(({'id': digest(url)[:24], 'url': url, 'query_index': i},
            {'action': {'kind': 'read', 'candidate_id': digest(url)[:24]},
             'observation': data, 'excerpt': _selection(texts[url], 'Editing', []),
             'result': {'ok': True, 'state': 'verified', 'code': 'source_verified'},
             'read_purpose': {'purpose': 'initial'}, 'at': STAMP}))
    seed = {'version': 1, 'queries': ['first', 'second'], 'source_ids': [i['id'] for i, _ in captured]}
    monkeypatch.setattr(comparison_seed, 'validate_manifest', lambda m: deepcopy(m))
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda m: deepcopy(captured))
    c['goal'].update(min_sources=2, required_terms=[], comparison={
        'dimensions': ['editing', 'format'], 'min_domains': 2, 'evidence_version': 4, 'seed': seed})
    targets = {url: [url.replace('https://', 'https://docs.')+'formats',
                     url.replace('https://', 'https://docs.')+'about'] for url in roots}
    for i, (url, links) in enumerate(targets.items()):
        c['browser'].text[links[0]] = f'Group{i} documentation. Editing supported. ODF format supported.'
        c['browser'].text[links[1]] = f'Published by Group{i}.'
    c['discovery_calls'] = []
    def discover(self, url, action, excluded):
        c['discovery_calls'].append(url)
        return {'discovery': {'urls': targets[url], 'searched_at': STAMP}}
    monkeypatch.setattr(CrossSiteEnvironment, '_discover_links', discover)
    monkeypatch.setattr(CrossSiteEnvironment, '_validate_links', lambda self, linked, *args: deepcopy(linked))
    def model(data):
        if 'selection' not in data:
            return {'sources': [{'id': s['id'], 'spans': [
                {'quote': quote} if quote in s['text'] else None
                for quote in ('Editing supported.', 'ODF format supported.')]} for s in data['sources']]}
        selected = {r['id']: r for r in data['selection']['sources']}
        def group(s): return 0 if 'alpha' in s['url'] else 1
        return {'sources': [{'id': s['id'], 'publisher': (
            {'name': f'Group{group(s)}', 'quote': f'Published by Group{group(s)}.'}
            if 'Published by' in s['text'] else None),
            'cells': [{'status': 'relevant' if span else 'missing', 'reason': 'Evidence checked'}
                      for span in selected[s['id']]['spans']]} for s in data['sources']],
            'pairs': [{'left': a['id'], 'right': b['id'],
                'relation': 'same' if group(a) == group(b) else 'independent', 'reason': 'Publisher evidence'}
                for a, b in combinations(data['sources'], 2)]}
    c['client'] = Client(model)
    c['options']['search'] = None
    return c


def execute(c, plan, halt=None):
    root = c['session'].root / 'goals' / (plan['sha256']+'.run')
    return engine.execute(c['session'], plan, approval=plan['sha256'], client=c['client'],
        stop=lambda: halt is not None and len(list(root.glob('step-*/verification.json'))) >= halt,
        **{k: v for k, v in c['options'].items() if k not in {'stop', 'deadline'}})


@pytest.mark.parametrize('halt', [None, 1, 3, 6, 8, 9])
def test_v4_crosssite_completion_and_zero_replay_recovery(v4, halt):
    c = v4
    plan = engine.prepare(c['session'], '补齐格式并续接', c['goal'])
    result = execute(c, plan, halt)
    preserved = {}
    if halt:
        assert not result['ok'], result
        preserved = {p: p.read_bytes() for p in Path(result['run']).rglob('*') if p.is_file()}
        plan = recovery.prepare(c['session'], result['run'])['plan']
        result = execute(c, plan)
    assert result['ok'], result
    assert len(c['browser'].calls) == len(set(c['browser'].calls)) == 4
    assert len(c['discovery_calls']) == 2
    assert len(c['client'].calls) == 4
    assert len(c['effects']) == 2
    assert result['source_reads'] == 6
    assert all(p.read_bytes() == data for p, data in preserved.items())
    assert [e[0]['kind'] for e in c['effects']] == ['spreadsheet', 'document']
    assert all('seed' not in call['goal']['comparison'] for call in c['client'].calls)


def test_seed_change_stops_before_model_or_read(v4, monkeypatch):
    from agent_service import comparison_seed
    c = v4
    plan = engine.prepare(c['session'], '补证', c['goal'])
    def reject(_): raise ComputerError('comparison_seed_changed')
    monkeypatch.setattr(comparison_seed, 'load_seed', reject)
    result = execute(c, plan)
    assert not result['ok'] and not c['client'].calls and not c['browser'].calls and not c['effects']


def test_v4_rejects_query_change(v4):
    v4['goal']['queries'][0] = 'unrelated'
    with pytest.raises(ComputerError, match='comparison_seed_queries_changed'):
        engine.prepare(v4['session'], '不允许改变来源归组', v4['goal'])


def test_arbitrary_crosshost_search_is_not_navigation_proof():
    env = object.__new__(CrossSiteEnvironment)
    action = {'host': 'alpha.example'}
    assert not env._supplement_allowed({}, action, ['https://docs.alpha.example/'])
    assert env._supplement_allowed({}, action, ['https://alpha.example/format'])
    linked = {'link_result': {'discovery': {'urls': ['https://docs.alpha.example/format']}}}
    assert env._supplement_allowed(linked, action, linked['link_result']['discovery']['urls'])
    assert not env._supplement_allowed(linked, action, ['https://other.invalid/'])
