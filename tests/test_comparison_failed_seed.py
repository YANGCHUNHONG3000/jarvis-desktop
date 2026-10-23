"""Failed comparisons export verified captures, never their earlier verdict.

All producers below run the saved production loop against local browser/model
fixtures. No network, paid API, native Office, or user history is accessed.
"""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from agent_service import comparison_seed as seed, computer, computer_agent as engine
from agent_service import comparison_history as history, research_recovery as recovery
from agent_service.computer import ComputerError, digest, read_record
from test_comparison_completion import configure
from test_comparison_loop import ctx, run, semantic_review
from test_comparison_research_seed import completed as research_completed
from test_comparison_seed import shape
from test_computer_agent import Client
from test_computer_agent_tools import ctx as tool_ctx


def snapshot(root):
    return {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def records(root):
    return [read_record(path)['data'] for path in sorted(root.glob('step-*/environment-result.json'))]


def failed_model(data):
    return {'sources': []}


def select_model(data):
    if 'selection' in data:
        review = semantic_review(data)
        for row in review['sources']:
            row['publisher']['name'] = row['publisher']['name'].removeprefix('Publisher ')
        return review
    return {'sources': [{'id': row['id'], 'spans': [{'quote': row['text']}]}
                        for row in data['sources']]}


def install_roots(c, monkeypatch):
    from agent_service import comparison_failed_seed as failed
    monkeypatch.setattr(os, 'fsync', lambda *_: None)
    monkeypatch.setattr(computer, 'BASE', c['session'].root.parent)
    monkeypatch.setattr(seed, 'TASK_BASE', c['options']['task_base'])
    monkeypatch.setattr(failed, 'TASK_BASE', c['options']['task_base'])


@pytest.fixture
def failed(ctx, monkeypatch):
    c = ctx
    install_roots(c, monkeypatch)
    c['client'] = Client(failed_model)
    plan = engine.prepare(c['session'], 'retain failed comparison captures', c['goal'])
    outcome = run(c, plan)
    assert outcome['ok'] is False and outcome['code'] == 'agent_no_safe_next_action', outcome
    assert outcome['state'] in {'partial', 'stopped'} and not c['effects']
    root = Path(outcome['run'])
    assert records(root)[-1]['action'] == {'kind': 'compare'}
    assert records(root)[-1]['result']['code'] == 'comparison_evidence_invalid'
    return c, root, plan, outcome


@pytest.fixture(params=[False, True], ids=['v4-original', 'v4-first-recovery'])
def failed_v4(research_completed, monkeypatch, request):
    from agent_service.comparison_crosssite import prepare_followup
    c, ancestor, original_plan, _ = research_completed
    install_roots(c, monkeypatch)
    goal = deepcopy(original_plan['goal'])
    goal['required_terms'] = []
    goal['comparison'] = {'dimensions': ['fact'], 'min_domains': 2}
    c['client'] = Client(failed_model)
    plan = prepare_followup(c['session'], 'failed review of actual research captures', goal, ancestor)
    verified_manifest = deepcopy(plan['goal']['comparison']['seed'])
    production_load = seed.load_seed
    verified_pairs = production_load(verified_manifest)
    ancestor_before = snapshot(ancestor)
    parent = None
    if request.param:
        # One selected import leaves a real original-query search/read frontier.
        plan = prepare_followup(c['session'], 'resume then fail review', goal, ancestor,
                                [verified_manifest['source_ids'][0]])
        verified_manifest = deepcopy(plan['goal']['comparison']['seed'])
        verified_pairs = production_load(verified_manifest)
    # The ordinary producer's full verifier is covered separately. Cache only
    # its already verified in-memory imports while producing this test's run;
    # restore the real loader before inspecting any failed run or lineage.
    with monkeypatch.context() as producer:
        producer.setattr(seed, 'load_seed', lambda value: deepcopy(verified_pairs)
                         if value == verified_manifest else production_load(value))
        if request.param:
            interrupted = run(c, plan, halt=1)
            assert interrupted['ok'] is False, interrupted
            parent = Path(interrupted['run'])
            plan = recovery.prepare(c['session'], parent)['plan']
        outcome = run(c, plan)
    assert snapshot(ancestor) == ancestor_before
    assert outcome['ok'] is False and outcome['code'] == 'agent_no_safe_next_action', outcome
    root = Path(outcome['run'])
    assert records(root)[-1]['action']['kind'] == 'compare'
    return c, root, plan, outcome, ancestor, parent


def test_failed_capture_import_is_read_only_and_drops_old_verdict(failed):
    c, root, plan, outcome = failed
    before = snapshot(root), root.with_suffix('.json').read_bytes()
    effects = (len(c['client'].calls), list(c['search'].calls), list(c['browser'].calls))
    manifest = seed.create_manifest(root)
    assert manifest['version'] == 4 and manifest['outputs'] == {}
    assert manifest['queries'] == plan['goal']['queries']
    assert len(manifest['source_ids']) == 4
    pairs = seed.load_seed(manifest)
    assert all(record['action']['kind'] == 'read' and record['result']['ok'] for _, record in pairs)
    assert all('comparison' not in record for _, record in pairs)
    assert all(record in records(root) for _, record in pairs)
    assert read_record(root / 'outcome.json') == outcome
    assert (snapshot(root), root.with_suffix('.json').read_bytes()) == before
    assert effects == (len(c['client'].calls), c['search'].calls, c['browser'].calls)
    assert not (root / 'continuation-claim.json').exists()
    pairs[0][1]['observation']['text'] = 'caller mutation'
    assert seed.load_seed(manifest)[0][1]['observation']['text'] != 'caller mutation'
    selected = manifest['source_ids'][::-1][:2]
    assert [item['id'] for item, _ in seed.load_seed(seed.create_manifest(root, selected))] == selected


def test_failed_v4_and_first_recovery_replay_original_research_lineage(failed_v4):
    c, root, _, _, ancestor, parent = failed_v4
    roots = [root, ancestor] + ([parent] if parent else [])
    before = [snapshot(value) for value in roots]
    manifest = seed.create_manifest(root)
    assert manifest['version'] == 4 and manifest['outputs'] == {}
    assert 1 <= len(seed.load_seed(manifest)) <= 6
    assert [snapshot(value) for value in roots] == before
    output = Path(read_record(ancestor / 'outcome.json')['artifacts'][0]['path'])
    parent_paths = ([parent / 'step-01' / 'verification.json', parent / 'continuation-claim.json']
                    if parent else [])
    for target in [ancestor / 'outcome.json', output] + parent_paths:
        old = target.read_bytes()
        try:
            if target.name == 'continuation-claim.json':
                # The existing lineage contract excludes claim serialization
                # from its inventory, but binds its exact parsed child claim.
                value = read_record(target)
                value['child'] = '0' * 64
                target.write_text(json.dumps(value), encoding='utf-8')
            else:
                target.write_bytes(old + b' ')
            with pytest.raises(ComputerError):
                seed.load_seed(manifest)
        finally:
            target.write_bytes(old)
    assert seed.load_seed(manifest)
    from agent_service.run_lifecycle import _lock, _unlock
    for owner in [ancestor] + ([parent] if parent else []):
        with (owner / 'owner.lease').open('r+b', buffering=0) as stream:
            _lock(stream)
            try:
                with pytest.raises(ComputerError, match='run_owner_active'):
                    seed.load_seed(manifest)
            finally:
                _unlock(stream)
    assert seed.load_seed(manifest)


def test_followup_performs_new_review_without_repeating_search_or_reads(failed):
    from agent_service.comparison_crosssite import prepare_followup
    c, root, original_plan, _ = failed
    before = snapshot(root)
    calls = list(c['search'].calls), list(c['browser'].calls)
    c['client'] = Client(select_model)
    goal = deepcopy(original_plan['goal'])
    plan = prepare_followup(c['session'], 'new independent review', goal, root)
    assert plan['goal']['comparison']['seed']['version'] == 4
    result = run(c, plan)
    assert result['ok'], result
    assert [row['action']['kind'] for row in result['attempts']] == ['compare', 'spreadsheet', 'document']
    assert len(c['client'].calls) == 2 and 'selection' in c['client'].calls[1]
    assert (c['search'].calls, c['browser'].calls) == calls
    assert snapshot(root) == before
    assert all('seed' not in data['goal']['comparison'] for data in c['client'].calls)


def test_version_four_shape_is_pure_and_old_empty_output_rules_stay(monkeypatch):
    value = shape()
    value.update(version=4, outputs={})
    def forbidden(*args, **kwargs):
        pytest.fail('shape validation must not access files')
    monkeypatch.setattr(seed, 'safe_root', forbidden)
    monkeypatch.setattr(seed, 'read_record', forbidden)
    monkeypatch.setattr(Path, 'stat', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    assert seed.validate_manifest(value) == value
    for version in (1, 2, 3):
        old = shape()
        old['version'] = version
        if version == 2:
            old['source_ids'] = old['source_ids'][:2]
            old['sources'] = old['sources'][:2]
        assert seed.validate_manifest(old) == old
        old['outputs'] = {}
        with pytest.raises(ComputerError, match='comparison_seed_invalid'):
            seed.validate_manifest(old)
    for field in ('owner.json', 'owner.lease', 'environment-goal.json'):
        bad = deepcopy(value)
        del bad['inventory'][field]
        with pytest.raises(ComputerError, match='comparison_seed_invalid'):
            seed.validate_manifest(bad)
    value['version'] = 5
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.validate_manifest(value)


@pytest.mark.parametrize('field', ['queries', 'record_sha256', 'text_sha256', 'query_index'])
def test_changed_manifest_semantics_rejected(failed, field):
    _, root, _, _ = failed
    manifest = seed.create_manifest(root)
    if field == 'queries':
        manifest['queries'][0] = 'other subject'
    elif field == 'query_index':
        manifest['sources'][0][field] = 1 - manifest['sources'][0][field]
    else:
        manifest['sources'][0][field] = '0' * 64
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        seed.load_seed(manifest)


def test_present_but_null_recovery_metadata_is_not_an_original_run(failed, monkeypatch):
    _, root, _, _ = failed
    original = recovery._plan
    def null_recovery(session, run_root):
        plan = original(session, run_root)
        if run_root == root:
            plan = deepcopy(plan)
            plan['recovery'] = None
        return plan
    monkeypatch.setattr(recovery, '_plan', null_recovery)
    with pytest.raises(ComputerError):
        seed.create_manifest(root)


@pytest.mark.parametrize('target', ['plan', 'outcome', 'result', 'source', 'candidate', 'verification', 'added'])
def test_every_original_byte_boundary_rehashed_on_load(failed, target):
    _, root, _, _ = failed
    manifest = seed.create_manifest(root)
    assert seed.load_seed(manifest)
    if target == 'plan':
        path = root.with_suffix('.json')
    elif target == 'outcome':
        path = root / 'outcome.json'
    elif target in {'source', 'candidate'}:
        kind = 'read' if target == 'source' else 'search'
        path = next(path for path in root.glob('step-*/environment-result.json')
                    if read_record(path)['data']['action']['kind'] == kind)
    elif target == 'result':
        path = sorted(root.glob('step-*/environment-result.json'))[-1]
    elif target == 'verification':
        path = root / 'step-01' / 'verification.json'
    else:
        path = root / 'unexpected.txt'
    path.write_bytes((path.read_bytes() if path.exists() else b'') + b' ')
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        seed.load_seed(manifest)


@pytest.mark.parametrize('kind', ['lease', 'continuation', 'missing_owner', 'unknown_effect',
                                 'unknown_receipt', 'unknown_compare_code', 'nonterminal',
                                 'office_started', 'verification', 'candidate'])
def test_unsafe_or_rehashed_forged_failures_are_never_importable(failed, kind):
    _, root, _, _ = failed
    if kind == 'lease':
        from agent_service.run_lifecycle import _lock, _unlock
        manifest = seed.create_manifest(root)
        assert seed.load_seed(manifest)
        with (root / 'owner.lease').open('r+b', buffering=0) as stream:
            _lock(stream)
            try:
                with pytest.raises(ComputerError, match='run_owner_active'):
                    seed.create_manifest(root)
                with pytest.raises(ComputerError, match='run_owner_active'):
                    seed.load_seed(manifest)
            finally:
                _unlock(stream)
        return
    if kind == 'continuation':
        (root / 'continuation-claim.json').write_text('{}', encoding='utf-8')
    elif kind == 'missing_owner':
        (root / 'owner.json').unlink()
    elif kind in {'unknown_effect', 'nonterminal'}:
        path = root / 'outcome.json'
        value = read_record(path)
        value['state' if kind == 'unknown_effect' else 'code'] = (
            'unknown' if kind == 'unknown_effect' else 'agent_paused')
        path.write_text(json.dumps(value), encoding='utf-8')
    elif kind in {'unknown_receipt', 'unknown_compare_code'}:
        path = sorted(root.glob('step-*/environment-result.json'))[-1]
        value = read_record(path)
        result = value['data']['result']
        result['state' if kind == 'unknown_receipt' else 'code'] = (
            'unknown' if kind == 'unknown_receipt' else 'unapproved_failure')
        value['sha256'] = digest(value['data'])
        path.write_text(json.dumps(value), encoding='utf-8')
        path.with_name('verification.json').write_text(json.dumps(result), encoding='utf-8')
        outcome_path = root / 'outcome.json'
        outcome = read_record(outcome_path)
        outcome['attempts'][-1]['result'] = deepcopy(result)
        outcome['failures'][-1]['code'] = result['code']
        outcome_path.write_text(json.dumps(outcome), encoding='utf-8')
    elif kind == 'verification':
        path = root / 'step-01' / 'verification.json'
        value = read_record(path)
        value['code'] = 'forged'
        path.write_text(json.dumps(value), encoding='utf-8')
    else:
        kind_to_edit = 'compare' if kind == 'office_started' else 'search'
        path = next(path for path in root.glob('step-*/environment-result.json')
                    if read_record(path)['data']['action']['kind'] == kind_to_edit)
        value = read_record(path)
        if kind == 'office_started':
            value['data']['action'] = {'kind': 'spreadsheet'}
        else:
            value['data']['discovery']['urls'][0] = 'https://changed.invalid/'
        value['sha256'] = digest(value['data'])
        path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ComputerError):
        seed.create_manifest(root)


@pytest.mark.parametrize('ids', [[], ['0' * 24], ['0' * 24] * 5, 'source', [False]])
def test_empty_unknown_duplicate_or_unbounded_selection_rejected(failed, ids):
    _, root, _, _ = failed
    with pytest.raises(ComputerError):
        seed.create_manifest(root, ids)


def test_history_failed_rows_remain_metadata_until_selected_and_verified(failed):
    c, root, _, _ = failed
    before = snapshot(root)
    rows = history.recent_candidates(base=c['session'].root.parent)
    row = next(row for row in rows if row['run'] == str(root))
    assert row['kind'] == 'comparison_failed'
    assert snapshot(root) == before
    selected = history.select_source(root)
    assert selected['manifest']['version'] == 4
    assert 'seed' not in selected['goal']['comparison']
    assert '旧结论不沿用' in history.source_description(selected)
    path = root / 'outcome.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ComputerError):
        history.prepare_selected(c['session'], 'changed after selection', selected['goal'], selected)


def test_actual_office_start_after_successful_review_blocks_capture_import(ctx, monkeypatch):
    c = ctx
    install_roots(c, monkeypatch)
    c['client'] = Client(select_model)
    started = []
    def unknown_office(spec, root, stop):
        started.append(deepcopy(spec))
        raise RuntimeError('controlled unknown Office effect')
    c['options']['office_execute'] = unknown_office
    plan = engine.prepare(c['session'], 'Office effect cannot become a failed capture seed', c['goal'])
    result = run(c, plan)
    assert result['ok'] is False and started, result
    root = Path(result['run'])
    saved = records(root)
    assert any(row['action']['kind'] == 'compare' and row['result']['ok'] for row in saved)
    assert saved[-1]['action']['kind'] == 'spreadsheet'
    assert saved[-1]['result']['state'] in {'failed', 'unknown'}
    with pytest.raises(ComputerError):
        seed.create_manifest(root)


def test_known_supplement_failure_after_semantic_rejection_still_exports_reads(ctx, monkeypatch):
    c = configure(ctx)
    install_roots(c, monkeypatch)
    original = c['search'].search
    def unavailable(query, max_sources, should_stop, timeout):
        if query not in c['goal']['queries']:
            raise ComputerError('search_timeout')
        return original(query, max_sources, should_stop, timeout)
    c['search'].search = unavailable
    plan = engine.prepare(c['session'], 'known failed supplement preserves captures', c['goal'])
    result = run(c, plan)
    assert result['ok'] is False and result['code'] == 'agent_no_safe_next_action', result
    root = Path(result['run'])
    saved = records(root)
    compare = next(row for row in saved if row['action']['kind'] == 'compare')
    assert compare['result']['code'] == 'comparison_independence_insufficient'
    assert saved[-1]['action']['kind'] == 'supplement' and saved[-1]['result']['ok'] is False
    before = snapshot(root)
    manifest = seed.create_manifest(root)
    assert manifest['version'] == 4 and len(seed.load_seed(manifest)) == 4
    assert snapshot(root) == before and not c['effects']
