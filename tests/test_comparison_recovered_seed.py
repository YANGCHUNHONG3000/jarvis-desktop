"""Bounded read-only imports from a completed first v4 recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from agent_service import comparison_recovered_seed as recovered, comparison_seed as seed
from agent_service import computer, computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, read_record
from test_comparison_crosssite import v4, execute
from test_computer_agent_tools import ctx as tool_ctx

PRODUCTION_VALIDATE = seed.validate_manifest
PRODUCTION_LOAD = seed.load_seed


def snapshot(root):
    return {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def completed(v4, monkeypatch):
    c = v4
    monkeypatch.setattr(os, 'fsync', lambda *_: None)
    # The v4 loop fixture has verified, fixed capture pairs. Its adapter stubs
    # isolate this test from the independent version-two producer tests.
    c['goal']['comparison']['seed']['version'] = 2
    captured_loader = seed.load_seed
    monkeypatch.setattr(seed, 'load_seed', lambda m: (
        PRODUCTION_LOAD(m) if m['version'] == 3 else captured_loader(m)))
    monkeypatch.setattr(seed, 'validate_manifest', lambda m: (
        PRODUCTION_VALIDATE(m) if m['version'] != 2 else deepcopy(m)))
    monkeypatch.setattr(computer, 'BASE', c['session'].root.parent)
    monkeypatch.setattr(seed, 'TASK_BASE', c['options']['task_base'])
    monkeypatch.setattr(recovered, 'TASK_BASE', c['options']['task_base'])
    plan = engine.prepare(c['session'], 'recover comparison', c['goal'])
    first = execute(c, plan, halt=8)
    assert not first['ok'], first
    parent = Path(first['run'])
    resumed = recovery.prepare(c['session'], parent)['plan']
    second = execute(c, resumed)
    assert second['ok'], second
    return c, parent, Path(second['run']), resumed


def test_import_replays_completed_recovery_read_only(completed):
    c, parent, child, plan = completed
    before = snapshot(parent), snapshot(child)
    effects = len(c['effects']), len(c['browser'].calls), len(c['client'].calls)
    manifest = seed.create_manifest(child)
    assert manifest['version'] == 3
    assert manifest['queries'] == plan['goal']['queries']
    assert 1 <= len(manifest['source_ids']) <= 6
    pairs = seed.load_seed(manifest)
    assert len(pairs) == len(manifest['source_ids'])
    assert all(record['result']['ok'] for _, record in pairs)
    assert snapshot(parent) == before[0] and snapshot(child) == before[1]
    assert effects == (len(c['effects']), len(c['browser'].calls), len(c['client'].calls))
    assert not (child / 'continuation-claim.json').exists()


@pytest.mark.parametrize('target', ['parent', 'child', 'capture', 'claim'])
def test_changed_run_or_parent_rejected(completed, target):
    _, parent, child, _ = completed
    manifest = seed.create_manifest(child)
    path = ({'parent': parent / 'step-01' / 'verification.json',
             'child': child / 'step-01' / 'verification.json',
             'capture': next(path for path in parent.glob('step-*/environment-result.json')
                             if read_record(path)['data']['action']['kind'] == 'read'
                             and read_record(path)['data']['result']['ok']),
             'claim': parent / 'continuation-claim.json'})[target]
    if target == 'claim':
        value = read_record(path)
        value['child'] = '0' * 64
        path.write_text(json.dumps(value), encoding='utf-8')
    else:
        path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ComputerError):
        seed.load_seed(manifest)


def test_manifest_shape_is_pure_and_unknown_version_rejected(completed, monkeypatch):
    _, _, child, _ = completed
    manifest = seed.create_manifest(child)
    monkeypatch.setattr(seed, 'safe_root', lambda *_: pytest.fail('shape touched files'))
    assert seed.validate_manifest(manifest) == manifest
    bad = deepcopy(manifest)
    bad['version'] = 4
    with pytest.raises(ComputerError, match='comparison_seed_invalid'):
        seed.validate_manifest(bad)


@pytest.mark.parametrize('change', ['depth', 'parent_recovery', 'seed', 'cycle', 'continuation'])
def test_only_first_v4_recovery_from_original_research(completed, monkeypatch, change):
    _, parent, child, _ = completed
    if change == 'continuation':
        (child / 'continuation-claim.json').write_text('{}', encoding='utf-8')
    else:
        original = recovery._plan
        def changed(session, root):
            value = original(session, root)
            if root == child:
                value = deepcopy(value)
                if change == 'depth':
                    value['recovery']['depth'] = 2
                elif change == 'seed':
                    value['goal']['comparison']['seed']['version'] = 1
                elif change == 'cycle':
                    value['recovery']['parent_run'] = str(child)
            elif root == parent and change == 'parent_recovery':
                value = deepcopy(value)
                value['recovery'] = {}
            return value
        monkeypatch.setattr(recovery, '_plan', changed)
    with pytest.raises(ComputerError):
        seed.create_manifest(child)
