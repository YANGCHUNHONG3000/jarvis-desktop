"""Historical v3 imports use real local replay and offline Office fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
from itertools import combinations
from urllib.parse import urlsplit

import pytest

from agent_service import comparison_seed as seed, computer, computer_agent as engine
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_agent_tools import AgentEnvironment
from test_comparison_completion import configure
from test_comparison_loop import ctx, run
from test_computer_agent_tools import ctx as tool_ctx, A, B, STAMP
from test_computer_agent import Client


@pytest.fixture
def completed(ctx, monkeypatch):
    # These offline import contracts inspect actual saved bytes and receipts;
    # they do not exercise crash durability. Avoid repeated Windows flush cost.
    monkeypatch.setattr(os, 'fsync', lambda *_: None)
    configure(ctx, cached=True)
    monkeypatch.setattr(computer, 'BASE', ctx['session'].root.parent)
    monkeypatch.setattr(seed, 'TASK_BASE', ctx['options']['task_base'])
    plan = engine.prepare(ctx['session'], 'immutable source import', ctx['goal'])
    result = run(ctx, plan)
    assert result['ok'], result['code']
    root = Path(result['run'])
    seed._CACHE.clear()
    return ctx, root, plan, result


def snapshot(root):
    return {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def shape():
    ids = [str(i) * 24 for i in range(3)]
    return {'version': 1, 'run': 'unresolved historical run', 'queries': ['first', 'second'],
            'plan_sha256': '0' * 64, 'outcome_sha256': '1' * 64,
            'inventory': {name: '1' * 64 for name in (
                'claim.json', 'owner.json', 'owner.lease', 'environment-goal.json', 'outcome.json')},
            'outputs': {'unresolved native task': {'result.json': '2' * 64}},
            'source_ids': ids, 'sources': [{'id': sid, 'query_index': i % 2,
                'record_sha256': '3' * 64, 'text_sha256': '4' * 64} for i, sid in enumerate(ids)]}


def test_import_returns_only_selected_original_receipts_and_query_indices(completed):
    context, root, plan, outcome = completed
    original = snapshot(root)
    effects = (len(context['effects']), list(context['browser'].calls), list(context['search'].calls),
               len(context['client'].calls))
    manifest = seed.create_manifest(root)
    pairs = seed.load_seed(manifest)
    assert manifest['queries'] == plan['goal']['queries']
    assert len(pairs) == 3 and {item['query_index'] for item, _ in pairs} == {0, 1}
    assert all(record['action']['kind'] == 'read' for _, record in pairs)
    assert all(record['result']['ok'] for _, record in pairs)
    original_records = [read_record(path)['data'] for path in root.glob('step-*/environment-result.json')]
    assert all(record in original_records for _, record in pairs)
    assert any(row['read_purpose']['purpose'] == 'supplement' for _, row in pairs)
    assert all('comparison' not in row for _, row in pairs)
    ids = manifest['source_ids'][::-1][:2]
    selected = seed.create_manifest(root, ids)
    assert [candidate['id'] for candidate, _ in seed.load_seed(selected)] == ids
    assert snapshot(root) == original and read_record(root / 'outcome.json') == outcome
    assert not (root / 'continuation-claim.json').exists()
    assert effects == (len(context['effects']), context['browser'].calls, context['search'].calls,
                       len(context['client'].calls))
    # Callers cannot change the cached receipts or other manifests by mutation.
    pairs[0][1]['observation']['text'] = 'changed caller copy'
    assert seed.load_seed(manifest)[0][1]['observation']['text'] != 'changed caller copy'


def test_manifest_shape_validation_has_no_file_or_effect_access(monkeypatch):
    manifest = shape()
    def forbidden(*args, **kwargs):
        pytest.fail('shape validation must not access local files')
    monkeypatch.setattr(seed, 'safe_root', forbidden)
    monkeypatch.setattr(seed, 'read_record', forbidden)
    monkeypatch.setattr(Path, 'stat', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    copied = seed.validate_manifest(manifest)
    assert copied == manifest and copied is not manifest
    copied['inventory']['owner.json'] = '0' * 64
    assert copied != manifest


@pytest.mark.parametrize('mutation', [
    'version_bool', 'unknown_field', 'queries_duplicate', 'queries_control', 'ids_duplicate',
    'ids_too_many', 'ids_unknown_type', 'index_bool', 'index_out_of_range', 'hash',
    'path_escape', 'path_absolute', 'path_windows', 'path_dot', 'inventory_empty',
    'outputs_empty', 'sources_order', 'sources_extra', 'outcome_mismatch',
])
def test_manifest_rejects_malformed_shape(mutation):
    value = shape()
    if mutation == 'version_bool':
        value['version'] = True
    elif mutation == 'unknown_field':
        value['coverage'] = True
    elif mutation == 'queries_duplicate':
        value['queries'] = ['first', ' FIRST ']
    elif mutation == 'queries_control':
        value['queries'][0] = 'first\n'
    elif mutation == 'ids_duplicate':
        value['source_ids'][1] = value['source_ids'][0]
    elif mutation == 'ids_too_many':
        value['source_ids'] *= 2
    elif mutation == 'ids_unknown_type':
        value['source_ids'][0] = {}
    elif mutation == 'index_bool':
        value['sources'][0]['query_index'] = True
    elif mutation == 'index_out_of_range':
        value['sources'][0]['query_index'] = 2
    elif mutation == 'hash':
        value['plan_sha256'] = 'not a sha'
    elif mutation.startswith('path_'):
        name = {'path_escape': '../outcome.json', 'path_absolute': '/outside',
                'path_windows': 'C:/outside', 'path_dot': 'step-01/./proof'}[mutation]
        value['inventory'][name] = '0' * 64
    elif mutation == 'inventory_empty':
        value['inventory'] = {}
    elif mutation == 'outputs_empty':
        value['outputs'] = {}
    elif mutation == 'sources_order':
        value['sources'].reverse()
    elif mutation == 'sources_extra':
        value['sources'][0]['eligible'] = True
    else:
        value['outcome_sha256'] = '0' * 64
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.validate_manifest(value)


@pytest.mark.parametrize('kind', ['read', 'observation', 'plan', 'outcome', 'office', 'receipt', 'added', 'removed'])
def test_changed_bytes_fail_before_cached_replay_or_any_effect(completed, monkeypatch, kind):
    context, root, _, _ = completed
    manifest = seed.create_manifest(root)
    assert seed.load_seed(manifest)
    if kind == 'read':
        path = next(path for path in root.glob('step-*/environment-result.json')
                    if read_record(path)['data']['action']['kind'] == 'read')
    elif kind == 'observation':
        path = root / 'observation-01.json'
    elif kind == 'plan':
        path = root.with_suffix('.json')
    elif kind == 'outcome':
        path = root / 'outcome.json'
    elif kind == 'office':
        path = Path(read_record(root / 'outcome.json')['artifacts'][0]['path'])
    elif kind == 'receipt':
        path = next(Path(next(iter(manifest['outputs']))).rglob('result.json'))
    elif kind == 'added':
        path = root / 'unexpected.txt'
    else:
        path = root / 'step-01' / 'verification.json'
    if kind == 'removed':
        path.unlink()
    else:
        path.write_bytes((path.read_bytes() if path.exists() else b'') + b' ')
    def forbidden(*args, **kwargs):
        pytest.fail('changed frozen bytes must fail before replay or model/effects')
    monkeypatch.setattr(seed, '_verified', forbidden)
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        seed.load_seed(manifest)
    assert len(context['effects']) == 2


def test_cached_load_rehashes_without_repeating_original_office_verifier(completed, monkeypatch):
    _, root, _, _ = completed
    manifest = seed.create_manifest(root)
    def forbidden(*args, **kwargs):
        pytest.fail('verified immutable snapshot must use its bounded cache')
    monkeypatch.setattr(AgentEnvironment, '_verify_artifact', forbidden)
    monkeypatch.setattr(AgentEnvironment, '_verify_origin', forbidden)
    assert seed.load_seed(manifest)
    assert seed.create_manifest(root) == manifest


@pytest.mark.parametrize('field', ['queries', 'record_sha256', 'text_sha256', 'query_index'])
def test_semantically_changed_manifest_rejected(completed, field):
    _, root, _, _ = completed
    manifest = seed.create_manifest(root)
    if field == 'queries':
        manifest['queries'][0] = 'new topic'
    elif field == 'query_index':
        manifest['sources'][0][field] = 1 - manifest['sources'][0][field]
    else:
        manifest['sources'][0][field] = '0' * 64
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        seed.load_seed(manifest)


@pytest.mark.parametrize('ids', [[], ['0' * 24], ['0' * 24] * 5, 'source', [False]])
def test_unknown_or_unbounded_selection_rejected(completed, ids):
    _, root, _, _ = completed
    with pytest.raises(ComputerError):
        seed.create_manifest(root, ids)


@pytest.mark.parametrize('kind', ['outside', 'lease_active', 'recovery', 'v2'])
def test_only_inactive_completed_v3_original_runs_allowed(completed, monkeypatch, tmp_path, kind):
    _, root, plan, _ = completed
    if kind == 'outside':
        outside = tmp_path / ('0' * 64 + '.run')
        outside.mkdir()
        with pytest.raises(ComputerError, match='comparison_seed_invalid'):
            seed.create_manifest(outside)
    elif kind == 'lease_active':
        from agent_service.run_lifecycle import _lock, _unlock
        with (root / 'owner.lease').open('r+b', buffering=0) as stream:
            _lock(stream)
            try:
                with pytest.raises(ComputerError, match='run_owner_active'):
                    seed.create_manifest(root)
            finally:
                _unlock(stream)
    else:
        from agent_service import research_recovery
        invalid = deepcopy(plan)
        if kind == 'recovery':
            invalid['recovery'] = {}
        else:
            invalid['goal']['comparison']['evidence_version'] = 2
        monkeypatch.setattr(research_recovery, '_plan', lambda *args: invalid)
        with pytest.raises(ComputerError, match='comparison_seed_invalid'):
            seed.create_manifest(root)


def test_rehashed_forgery_still_runs_original_receipt_validators(completed):
    _, root, _, _ = completed
    manifest = seed.create_manifest(root)
    seed._CACHE.clear()
    # Rehashing a rewritten standalone success claim cannot make it valid.
    path = root / 'step-01' / 'verification.json'
    verification = read_record(path)
    verification['code'] = 'forged verification'
    path.write_text(json.dumps(verification), encoding='utf-8')
    frozen = seed._freeze(root)
    manifest.update(frozen)
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.load_seed(manifest)


def test_removed_environment_goal_is_never_recreated_during_inspection(completed, monkeypatch):
    _, root, _, _ = completed
    from agent_service import research_recovery
    original = research_recovery._plan
    def remove_after_validation(*args):
        plan = original(*args)
        (root / 'environment-goal.json').unlink()
        return plan
    monkeypatch.setattr(research_recovery, '_plan', remove_after_validation)
    with pytest.raises(ComputerError):
        seed.create_manifest(root)
    assert not (root / 'environment-goal.json').exists()


def test_inventory_bounds_all_files_and_directories(completed, monkeypatch):
    _, root, _, _ = completed
    monkeypatch.setattr(seed, 'MAX_FILES', 2)
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.create_manifest(root)


@pytest.fixture
def completed_v4(completed, monkeypatch):
    from agent_service.comparison_crosssite import CrossSiteEnvironment, prepare_followup
    context, ancestor, _, _ = completed
    context['session'] = computer.ComputerSession.create(computer.BASE)
    links = {A: [A+'formats'], B: [B+'formats', B+'about']}
    context['browser'].text.update({A+'formats': 'Publisher Alpha; ODF format supported.',
        B+'formats': 'Publisher Beta; ODF format supported.', B+'about': 'Publisher Beta; company attribution.'})
    def discover(self, source, action, excluded):
        return {'discovery': {'urls': links[source], 'searched_at': STAMP}}
    monkeypatch.setattr(CrossSiteEnvironment, '_discover_links', discover)
    monkeypatch.setattr(CrossSiteEnvironment, '_validate_links', lambda self, value, *args: deepcopy(value))
    def model(data):
        if 'selection' not in data:
            return {'sources': [{'id': row['id'], 'spans': [
                {'quote': 'ODF format supported.'} if 'ODF format supported.' in row['text'] else None]}
                for row in data['sources']]}
        spans = {row['id']: row['spans'] for row in data['selection']['sources']}
        rows = []
        for row in data['sources']:
            prefix = row['text'].split(';', 1)[0]
            rows.append({'id': row['id'], 'publisher': None if prefix.startswith('Unknown') else
                {'name': prefix.split(' ', 1)[1], 'quote': prefix}, 'cells': [
                {'status': 'relevant' if spans[row['id']][0] else 'missing', 'reason': 'Literal format evidence'}]})
        return {'sources': rows, 'pairs': [{'left': a['id'], 'right': b['id'],
            'relation': 'same' if urlsplit(a['url']).hostname == urlsplit(b['url']).hostname else 'independent',
            'reason': 'Captured publisher attribution'} for a, b in combinations(data['sources'], 2)]}
    context['client'] = Client(model)
    context['options']['search'] = None
    goal = deepcopy(context['goal'])
    goal['required_terms'] = []
    goal['comparison']['dimensions'] = ['format']
    plan = prepare_followup(context['session'], 'six historical captures', goal, ancestor)
    result = run(context, plan)
    assert result['ok'], result
    assert result['source_reads'] == 6
    return context, Path(result['run']), ancestor, goal


def test_completed_v4_import_revalidates_ancestors_and_bounds_depth(completed_v4):
    from agent_service.comparison_crosssite import prepare_followup
    context, root, ancestor, goal = completed_v4
    preserved = snapshot(root), snapshot(ancestor)
    before = len(context['browser'].calls), len(context['client'].calls), len(context['effects'])
    manifest = seed.create_manifest(root)
    pairs = seed.load_seed(manifest)
    assert len(pairs) == 6 and len(manifest['source_ids']) == 6
    assert {candidate['query_index'] for candidate, _ in pairs} == {0, 1}
    assert all(record['action']['kind'] == 'read' for _, record in pairs)
    assert preserved == (snapshot(root), snapshot(ancestor))
    assert before == (len(context['browser'].calls), len(context['client'].calls), len(context['effects']))
    assert seed.load_seed(manifest) == pairs  # Cached child still validates its ancestor.
    # Reuse one immutable offline fixture; restore each synthetic byte change
    # before checking the next independent ancestor boundary.
    paths = [next(path for path in ancestor.glob('step-*/environment-result.json')
                  if read_record(path)['data']['action']['kind'] == 'read'),
             ancestor / 'outcome.json',
             Path(read_record(ancestor / 'outcome.json')['artifacts'][0]['path'])]
    for path in paths:
        original = path.read_bytes()
        try:
            path.write_bytes(original + b' ')
            with pytest.raises(ComputerError, match='comparison_seed_changed'):
                seed.load_seed(manifest)
        finally:
            path.write_bytes(original)
        assert seed.load_seed(manifest) == pairs
    assert preserved == (snapshot(root), snapshot(ancestor))
    context['session'] = computer.ComputerSession.create(computer.BASE)
    before = len(context['browser'].calls)
    plan = prepare_followup(context['session'], 'fresh review from six captures', goal, root)
    result = run(context, plan)
    assert result['ok'] and result['actions'] == 3
    assert len(context['browser'].calls) == before
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.create_manifest(result['run'])


def test_v3_source_selection_limit_stays_four_with_extended_manifest_shape():
    value = shape()
    for i in range(3, 6):
        sid = str(i) * 24
        value['source_ids'].append(sid)
        value['sources'].append({'id': sid, 'query_index': i % 2,
            'record_sha256': '3' * 64, 'text_sha256': '4' * 64})
    assert seed.validate_manifest(value) == value
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed._selected({'evidence_version': 3}, value['source_ids'])
