"""Local history selection contracts; no browser, model, or Office calls."""
from copy import deepcopy
import json

import pytest

from agent_service import comparison_history as history
from agent_service.computer import ComputerError


def _run(base, session, number, *, version=3, ok=True, recovery=False, title='对比'):
    root = base / f'computer-{session:03d}' / 'goals' / (f'{number:064x}.run')
    root.mkdir(parents=True)
    plan = {'goal': {'title': title, 'comparison': {'evidence_version': version}}}
    if recovery:
        plan['recovery'] = {'parent_run': 'older'}
    root.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    (root / 'outcome.json').write_text(json.dumps({'ok': ok}), encoding='utf-8')
    return root


def _goal():
    from agent_service.comparison_panel import build_goal
    return build_goal({'query1': 'site:a.example office', 'query2': 'site:b.example office',
                       'title': '原题', 'dimensions': '免费 | 格式',
                       'min_sources': '4', 'min_domains': '2'})


def test_recent_candidates_filters_only_completed_v3_v4_and_bounds_rows(tmp_path):
    first = _run(tmp_path, 1, 1, title='有效 v3')
    second = _run(tmp_path, 1, 2, version=4, title='有效 v4')
    _run(tmp_path, 1, 3, version=2)
    _run(tmp_path, 1, 4, ok=False)
    _run(tmp_path, 1, 5, recovery=True)
    rows = history.recent_candidates(base=tmp_path)
    assert {row['run'] for row in rows} == {str(first), str(second)}
    assert {row['title'] for row in rows} == {'有效 v3', '有效 v4'}
    for number in range(6, 42):
        _run(tmp_path, 1, number)
    assert len(history.recent_candidates(base=tmp_path)) == 30


def test_recent_candidates_limits_sessions_and_cancels(tmp_path):
    old = _run(tmp_path, 0, 1)
    for number in range(1, 202):
        _run(tmp_path, number, 1)
    rows = history.recent_candidates(base=tmp_path)
    assert old.as_posix() not in {row['run'].replace('\\', '/') for row in rows}
    assert len(rows) <= 30
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        history.recent_candidates(base=tmp_path, stop=lambda: True)


def test_select_source_rebuilds_editable_v3_goal_and_respects_cancel(monkeypatch):
    from agent_service import comparison_seed
    goal = _goal()
    manifest = {'run': 'original', 'queries': deepcopy(goal['queries']),
                'source_ids': ['a' * 24, 'b' * 24]}
    captures = [({'url': 'https://a.example/page', 'query_index': 0}, {}),
                ({'url': 'https://b.example/page', 'query_index': 1}, {})]
    monkeypatch.setattr(comparison_seed, 'create_manifest', lambda run: deepcopy(manifest))
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda selected: deepcopy(captures))
    monkeypatch.setattr(history, 'safe_root', lambda run: type('Root', (), {
        'with_suffix': lambda self, suffix: 'source-plan.json'})())
    monkeypatch.setattr(history, 'read_record', lambda path: {'goal': deepcopy(goal)})
    selected = history.select_source('original')
    assert selected['goal'] == goal
    assert selected['fields']['dimensions'] == '免费 | 格式'
    assert selected['pages'] == [{'url': 'https://a.example/page', 'query_index': 0},
                                 {'url': 'https://b.example/page', 'query_index': 1}]
    assert selected['manifest'] == manifest
    assert selected['goal']['comparison']['evidence_version'] == 3
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        history.select_source('original', stop=lambda: True)


def test_select_source_cancel_after_verification_discards_result(monkeypatch):
    from agent_service import comparison_seed
    goal = _goal()
    manifest = {'run': 'original', 'queries': goal['queries'], 'source_ids': ['a' * 24]}
    monkeypatch.setattr(comparison_seed, 'create_manifest', lambda run: manifest)
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda value: [
        ({'url': 'https://a.example/page', 'query_index': 0}, {})])
    monkeypatch.setattr(history, 'safe_root', lambda run: type('Root', (), {
        'with_suffix': lambda self, suffix: 'source-plan.json'})())
    monkeypatch.setattr(history, 'read_record', lambda path: {'goal': goal})
    checks = iter([False, True])
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        history.select_source('original', stop=lambda: next(checks))


def test_prepare_selected_rejects_changed_source_before_writing_plan(monkeypatch):
    from agent_service import comparison_seed, comparison_crosssite
    selection = {'manifest': {'run': 'old', 'source_ids': ['a' * 24]}}
    def changed(_):
        raise ComputerError('comparison_seed_changed')
    monkeypatch.setattr(comparison_seed, 'load_seed', changed)
    monkeypatch.setattr(comparison_crosssite, 'prepare_followup',
                        lambda *args, **kwargs: pytest.fail('must not write a plan'))
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        history.prepare_selected(object(), 'new task', _goal(), selection)
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        history.prepare_selected(object(), 'new task', _goal(), selection, stop=lambda: True)


def test_prepare_selected_passes_original_selection_and_rejects_reblessing(monkeypatch):
    from agent_service import comparison_seed, comparison_crosssite
    manifest = {'run': 'old', 'source_ids': ['a' * 24], 'queries': _goal()['queries']}
    selection = {'manifest': manifest}
    calls = []
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda value: calls.append(('load', value)))
    def prepared(session, instruction, goal, run, ids):
        calls.append(('prepare', session, instruction, goal, run, ids))
        return {'goal': {'comparison': {'seed': deepcopy(manifest)}}}
    monkeypatch.setattr(comparison_crosssite, 'prepare_followup', prepared)
    session, goal = object(), _goal()
    result = history.prepare_selected(session, '新维度', goal, selection)
    assert result['goal']['comparison']['seed'] == manifest
    assert calls == [('load', manifest), ('prepare', session, '新维度', goal, 'old', ['a' * 24])]
    def reblessed(*args):
        return {'goal': {'comparison': {'seed': {**manifest, 'source_ids': ['b' * 24]}}}}
    monkeypatch.setattr(comparison_crosssite, 'prepare_followup', reblessed)
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        history.prepare_selected(session, '新维度', goal, selection)


def test_prepare_selected_cancel_after_plan_does_not_return_approval(monkeypatch):
    from agent_service import comparison_seed, comparison_crosssite
    manifest = {'run': 'old', 'source_ids': ['a' * 24], 'queries': _goal()['queries']}
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda value: None)
    monkeypatch.setattr(comparison_crosssite, 'prepare_followup', lambda *args: {
        'goal': {'comparison': {'seed': manifest}}})
    checks = iter([False, True])
    with pytest.raises(ComputerError, match='comparison_selection_cancelled'):
        history.prepare_selected(object(), 'new task', _goal(), {'manifest': manifest},
                                 stop=lambda: next(checks))


def test_recent_candidates_includes_bounded_ordinary_research(tmp_path):
    roots = [_run(tmp_path, 1, n) for n in range(1, 6)]
    for root, count, queries, engine in zip(roots, (1, 2, 1, 0, 1),
            (['a', 'b'], ['a'], ['a'], ['a', 'b'], ['a', 'b']),
            ('observed_agent', 'observed_agent', 'observed_agent', 'observed_agent', 'other')):
        root.with_suffix('.json').write_text(json.dumps({'goal': {
            'title': '已有资料', 'queries': queries}}), encoding='utf-8')
        (root / 'outcome.json').write_text(json.dumps({
            'ok': True, 'engine': engine, 'source_reads': count}), encoding='utf-8')
    rows = history.recent_candidates(base=tmp_path)
    assert {r['run'] for r in rows} == {str(roots[0]), str(roots[1])}
    assert all(r['kind'] == 'research' for r in rows)


def test_select_ordinary_source_locks_queries_preserves_time_and_resets_review(monkeypatch):
    from agent_service import comparison_seed
    manifest = {'version': 2, 'run': 'old', 'queries': ['old first', 'old second'],
                'source_ids': ['a' * 24]}
    monkeypatch.setattr(comparison_seed, 'create_manifest', lambda run: deepcopy(manifest))
    monkeypatch.setattr(comparison_seed, 'load_seed', lambda value: [
        ({'url': 'https://a.example/page', 'query_index': 1},
         {'observation': {'captured_at': '2026-10-18T01:00:00+00:00'}})])
    monkeypatch.setattr(history, 'safe_root', lambda run: type('Root', (), {
        'with_suffix': lambda self, suffix: 'old.json'})())
    monkeypatch.setattr(history, 'read_record', lambda path: {'goal': {
        'queries': manifest['queries'], 'title': '旧资料', 'min_sources': 1,
        'required_terms': ['old required term'], 'outputs': ['document']}})
    selected = history.select_source('old')
    goal = selected['goal']
    assert goal['queries'] == manifest['queries']
    assert goal['min_sources'] == 2 and goal['comparison']['min_domains'] == 2
    assert goal['required_terms'] == [] and goal['outputs'] == ['spreadsheet', 'document']
    assert goal['comparison']['evidence_version'] == 3 and 'seed' not in goal['comparison']
    assert selected['pages'][0]['captured_at'] == '2026-10-18T01:00:00+00:00'
    description = history.source_description(selected)
    assert '普通资料检索' in description and '2026-10-18T01:00:00+00:00' in description
    assert '旧结论不沿用' in description


def test_history_only_lists_first_ordinary_recovery_as_unverified_candidate(tmp_path):
    roots = [_run(tmp_path, 1, i) for i in range(1, 5)]
    for i, root in enumerate(roots):
        goal = {'title': '已恢复资料', 'queries': ['q0', 'q1']}
        if i == 3:
            goal['comparison'] = {'evidence_version': 4}
        plan = {'goal': goal, 'recovery': {'kind': 'research', 'version': 2,
                                         'depth': (1, 2, True, 1)[i]}}
        root.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
        (root/'outcome.json').write_text(json.dumps({'ok': True,
            'engine': 'observed_agent', 'source_reads': 2}), encoding='utf-8')
    assert [r['run'] for r in history.recent_candidates(base=tmp_path)] == [str(roots[0])]


def test_metadata_lists_compact_capable_failures_without_claiming_verification(tmp_path):
    import json
    from agent_service import comparison_history as history
    root = tmp_path/'computer-example'/'goals'/'candidate.run'
    root.mkdir(parents=True)
    root.with_suffix('.json').write_text(json.dumps({'goal': {'title': '缺证',
        'comparison': {'evidence_version': 4, 'seed': {'version': 5}}}}), encoding='utf8')
    (root/'outcome.json').write_text(json.dumps({'ok': False, 'artifacts': [],
        'engine': 'observed_agent', 'mode': 'adapters', 'state': 'stopped',
        'code': 'agent_no_safe_next_action'}), encoding='utf8')
    assert history.recent_candidates(base=tmp_path) == [
        {'run': str(root), 'title': '缺证', 'kind': 'comparison_failed'}]
