"""Offline original-research imports: frozen receipts and no source writes."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from agent_service import comparison_research_seed as research_seed
from agent_service import comparison_seed as seed, computer, computer_agent as engine
from agent_service.computer import ComputerError, read_record
from test_comparison_loop import run
from test_computer_agent_tools import ctx as tool_ctx
from test_computer_agent import Client


@pytest.fixture
def completed(tool_ctx, monkeypatch):
    monkeypatch.setattr(os, 'fsync', lambda *_: None)
    c = tool_ctx
    c['session'] = computer.ComputerSession.create(c['run'].parent.parent / 'research-seed')
    c['goal']['min_sources'] = 2
    c['client'] = Client()
    monkeypatch.setattr(computer, 'BASE', c['session'].root.parent)
    monkeypatch.setattr(seed, 'TASK_BASE', c['options']['task_base'])
    monkeypatch.setattr(research_seed, 'TASK_BASE', c['options']['task_base'])
    plan = engine.prepare(c['session'], 'two verified research pages', c['goal'])
    result = run(c, plan)
    assert result['ok'], result
    assert result['source_reads'] == 2
    return c, Path(result['run']), plan, result


def snapshot(root):
    return {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def as_legacy(root):
    """Convert only a temporary fixture to the pre-lifecycle claim format."""
    claim_path = root / 'claim.json'
    claim = read_record(claim_path)
    claim_path.write_text(json.dumps({key: claim[key] for key in ('approval', 'mode', 'at')}),
                          encoding='utf-8')
    (root / 'owner.json').unlink()
    (root / 'owner.lease').unlink()


def test_replays_original_reads_without_side_effects(completed):
    c, root, plan, outcome = completed
    original = snapshot(root)
    effects = len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls)
    manifest = research_seed.create_manifest(root)
    pairs = research_seed.load_seed(manifest)
    assert manifest['version'] == 2 and manifest['queries'] == plan['goal']['queries']
    assert len(pairs) == 2 and {item['query_index'] for item, _ in pairs} == {0, 1}
    assert all(record['action']['kind'] == 'read' and record['result']['ok'] for _, record in pairs)
    original_records = [read_record(path)['data'] for path in root.glob('step-*/environment-result.json')]
    assert all(record in original_records for _, record in pairs)
    pairs[0][1]['observation']['text'] = 'mutated copy'
    assert research_seed.load_seed(manifest)[0][1]['observation']['text'] != 'mutated copy'
    assert snapshot(root) == original and read_record(root / 'outcome.json') == outcome
    assert not (root / 'continuation-claim.json').exists()
    assert effects == (len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls))


def test_dispatch_and_v4_environment_uses_both_historical_reads(completed):
    from agent_service.comparison_crosssite import CrossSiteEnvironment, prepare_followup
    c, root, plan, _ = completed
    original = snapshot(root)
    manifest = seed.create_manifest(root)
    assert manifest['version'] == 2 and seed.load_seed(manifest) == research_seed.load_seed(manifest)
    goal = deepcopy(plan['goal'])
    goal['required_terms'] = []
    goal['comparison'] = {'dimensions': ['fact'], 'min_domains': 2}
    followup = prepare_followup(c['session'], 'compare saved pages', goal, root)
    new_run = c['session'].root / 'goals' / (followup['sha256'] + '.run')
    new_run.mkdir()
    env = CrossSiteEnvironment(followup['goal'], new_run, **c['options'])
    observed = env.observe_final()
    assert observed['source_reads'] == 2
    assert observed['imported_source_ids'] == manifest['source_ids']
    assert env.actions(observed) == [{'kind': 'compare'}]
    assert snapshot(root) == original
    assert c['browser'].calls == [item['url'] for item, _ in seed.load_seed(manifest)]


def test_one_imported_query_searches_only_the_other_topic(completed):
    from agent_service.comparison_crosssite import CrossSiteEnvironment, prepare_followup
    c, root, plan, _ = completed
    all_sources = seed.create_manifest(root)
    selected = [all_sources['source_ids'][1]]
    goal = deepcopy(plan['goal'])
    goal['required_terms'] = []
    goal['comparison'] = {'dimensions': ['fact'], 'min_domains': 2}
    followup = prepare_followup(c['session'], 'compare one saved page', goal, root, selected)
    new_run = c['session'].root / 'goals' / (followup['sha256'] + '.run')
    new_run.mkdir()
    env = CrossSiteEnvironment(followup['goal'], new_run, **c['options'])
    observed = env.observe_final()
    assert observed['source_reads'] == 1
    assert observed['imported_source_ids'] == selected
    assert env.actions(observed) == [{'kind': 'search', 'query_index': 0}]
    assert len(c['browser'].calls) == 2 and len(c['search'].calls) == 2


def test_one_capture_requires_other_original_query(completed):
    _, root, _, _ = completed
    all_sources = research_seed.create_manifest(root)
    one = research_seed.create_manifest(root, all_sources['source_ids'][:1])
    assert len(research_seed.load_seed(one)) == 1
    malformed = deepcopy(one)
    malformed['queries'] = malformed['queries'][:1]
    with pytest.raises(ComputerError, match='comparison_seed_insufficient'):
        research_seed.validate_manifest(malformed)
    with pytest.raises(ComputerError, match='comparison_seed_insufficient'):
        research_seed.load_seed(malformed)


@pytest.mark.parametrize('target', ['read', 'receipt', 'plan', 'outcome', 'office', 'added'])
def test_changed_frozen_bytes_rejected_before_replay(completed, monkeypatch, target):
    _, root, _, _ = completed
    manifest = research_seed.create_manifest(root)
    if target == 'read':
        path = next(p for p in root.glob('step-*/environment-result.json')
                    if read_record(p)['data']['action']['kind'] == 'read')
    elif target == 'receipt':
        path = root / 'step-01' / 'verification.json'
    elif target == 'plan':
        path = root.with_suffix('.json')
    elif target == 'outcome':
        path = root / 'outcome.json'
    elif target == 'office':
        path = Path(read_record(root / 'outcome.json')['artifacts'][0]['path'])
    else:
        path = root / 'unexpected.txt'
    path.write_bytes((path.read_bytes() if path.exists() else b'') + b' ')
    def forbidden(*args, **kwargs):
        pytest.fail('frozen-byte check must precede receipt replay')
    monkeypatch.setattr(research_seed, '_verified', forbidden)
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        research_seed.load_seed(manifest)


def test_rehashed_forged_receipt_fails_semantic_replay(completed):
    _, root, _, _ = completed
    manifest = research_seed.create_manifest(root)
    path = root / 'step-01' / 'verification.json'
    forged = read_record(path)
    forged['code'] = 'forged'
    path.write_text(json.dumps(forged), encoding='utf-8')
    manifest.update(seed._freeze(root))
    with pytest.raises(ComputerError):
        research_seed.load_seed(manifest)


def test_original_plan_must_be_depth_zero_research(completed, monkeypatch):
    _, root, plan, _ = completed
    from agent_service import research_recovery
    for change in ('recovery', 'comparison'):
        invalid = deepcopy(plan)
        if change == 'recovery':
            invalid['recovery'] = {}
        else:
            invalid['goal']['comparison'] = {'dimensions': ['fact'], 'min_domains': 1}
        monkeypatch.setattr(research_recovery, '_plan', lambda *args, value=invalid: value)
        with pytest.raises(ComputerError, match='comparison_seed_invalid'):
            research_seed.create_manifest(root)


def test_shape_validation_does_not_touch_files(completed, monkeypatch):
    _, root, _, _ = completed
    manifest = research_seed.create_manifest(root)
    def forbidden(*args, **kwargs):
        pytest.fail('shape check must remain pure')
    monkeypatch.setattr(seed, 'safe_root', forbidden)
    monkeypatch.setattr(seed, 'read_record', forbidden)
    assert research_seed.validate_manifest(manifest) == manifest
    bad = deepcopy(manifest)
    bad['version'] = True
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        research_seed.validate_manifest(bad)


def test_legacy_completed_original_is_read_only(completed):
    c, root, _, _ = completed
    as_legacy(root)
    original = snapshot(root)
    effects = len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls)
    manifest = seed.create_manifest(root)
    assert manifest['version'] == 2
    assert 'owner.json' not in manifest['inventory'] and 'owner.lease' not in manifest['inventory']
    assert len(seed.load_seed(manifest)) == 2
    assert seed.load_seed(manifest) == research_seed.load_seed(manifest)
    assert snapshot(root) == original
    assert effects == (len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls))


@pytest.mark.parametrize('change', [
    'owner_only', 'lease_only', 'current_claim_without_owner', 'extra_claim',
    'bad_time', 'continuation', 'unfinished',
])
def test_legacy_boundary_rejects_ambiguous_or_incomplete_runs(completed, change):
    _, root, _, _ = completed
    if change == 'current_claim_without_owner':
        (root / 'owner.json').unlink()
        (root / 'owner.lease').unlink()
    else:
        as_legacy(root)
        if change == 'owner_only':
            (root / 'owner.json').write_text('{}', encoding='utf-8')
        elif change == 'lease_only':
            (root / 'owner.lease').write_bytes(b'!invalid')
        elif change in {'extra_claim', 'bad_time'}:
            path = root / 'claim.json'
            claim = read_record(path)
            if change == 'extra_claim':
                claim['extra'] = 'forbidden'
            else:
                claim['at'] = float('inf')
            path.write_text(json.dumps(claim), encoding='utf-8')
        elif change == 'continuation':
            (root / 'continuation-claim.json').write_text('{}', encoding='utf-8')
        else:
            path = root / 'outcome.json'
            outcome = read_record(path)
            outcome['ok'] = False
            path.write_text(json.dumps(outcome), encoding='utf-8')
    with pytest.raises(ComputerError):
        research_seed.create_manifest(root)


def test_legacy_manifest_detects_changed_bytes(completed):
    _, root, _, _ = completed
    as_legacy(root)
    manifest = research_seed.create_manifest(root)
    path = root / 'step-01' / 'verification.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        research_seed.load_seed(manifest)


def test_legacy_plan_time_must_be_finite(completed, monkeypatch):
    _, root, plan, _ = completed
    as_legacy(root)
    from agent_service import research_recovery
    invalid = deepcopy(plan)
    invalid['prepared_at'] = float('nan')
    monkeypatch.setattr(research_recovery, '_plan', lambda *args: invalid)
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        research_seed._freeze(root)


def test_modern_completed_source_with_continuation_claim_is_rejected(completed):
    _, root, _, _ = completed
    (root / 'continuation-claim.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        research_seed.create_manifest(root)


@pytest.fixture
def resumed(tool_ctx, monkeypatch):
    from agent_service import research_recovery
    monkeypatch.setattr(os, 'fsync', lambda *_: None)
    c = tool_ctx
    c['session'] = computer.ComputerSession.create(c['run'].parent.parent / 'research-resumed-seed')
    c['goal']['min_sources'] = 2
    c['client'] = Client()
    monkeypatch.setattr(computer, 'BASE', c['session'].root.parent)
    monkeypatch.setattr(seed, 'TASK_BASE', c['options']['task_base'])
    monkeypatch.setattr(research_seed, 'TASK_BASE', c['options']['task_base'])
    plan = engine.prepare(c['session'], 'continue two research pages', c['goal'])
    first = run(c, plan, halt=3)
    assert not first['ok'] and first['state'] in {'partial', 'stopped'}
    parent = Path(first['run'])
    followup = research_recovery.prepare(c['session'], parent)['plan']
    finished = run(c, followup)
    assert finished['ok'], finished
    return c, parent, Path(finished['run']), followup


def test_first_recovery_child_import_rechecks_lineage_without_reread(resumed):
    c, parent, child, plan = resumed
    originals = snapshot(parent), snapshot(child)
    effects = len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls)
    manifest = seed.create_manifest(child)
    assert manifest['version'] == 2 and len(manifest['source_ids']) == 2
    assert [item['query_index'] for item, _ in seed.load_seed(manifest)] == [0, 1]
    assert seed.load_seed(manifest) == research_seed.load_seed(manifest)
    assert snapshot(parent) == originals[0] and snapshot(child) == originals[1]
    assert effects == (len(c['effects']), len(c['browser'].calls), len(c['search'].calls), len(c['client'].calls))
    assert plan['recovery']['depth'] == 1


def test_recovery_child_rejects_parent_claim_or_current_receipt_change(resumed):
    _, parent, child, _ = resumed
    manifest = research_seed.create_manifest(child)
    claim_path = parent / 'continuation-claim.json'
    original_claim = claim_path.read_bytes()
    try:
        claim_path.write_text(json.dumps({'child': '0' * 64}), encoding='utf-8')
        with pytest.raises(ComputerError):
            research_seed.load_seed(manifest)
    finally:
        claim_path.write_bytes(original_claim)
    assert research_seed.load_seed(manifest)
    receipt = child / 'step-01' / 'verification.json'
    receipt.write_bytes(receipt.read_bytes() + b' ')
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        research_seed.load_seed(manifest)


def test_recovery_child_rejects_deeper_lineage(resumed, monkeypatch):
    _, _, child, _ = resumed
    from agent_service import research_recovery
    original = research_recovery._plan
    def forged_depth(session, root):
        value = original(session, root)
        if root == child:
            value = deepcopy(value)
            value['recovery']['depth'] = 2
        return value
    monkeypatch.setattr(research_recovery, '_plan', forged_depth)
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        research_seed.create_manifest(child)
