"""External capture bytes participate in every cache/graph ownership boundary.

Synthetic graph fixtures and a stubbed import derivation isolate dependency
checking; they do not establish navigation or semantic eligibility.
"""
from copy import deepcopy
from collections import OrderedDict
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from agent_service import comparison_capture_import as imports
from agent_service import comparison_compact_seed as compact
from agent_service import comparison_seed as seed
from agent_service import research_recovery as recovery
from agent_service.computer import ComputerError, digest, read_record
from test_comparison_compact_seed import graph_tree
from test_comparison_seed import shape
from test_recovery_verification_cache import compact_parent, comparison_ctx
from test_computer_agent_tools import ctx as tool_ctx


def descriptor(path, base):
    return {'version': 1, 'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(),
            'seed_sha256': digest(base), 'source_ids': ['a' * 24]}


@pytest.fixture
def graph_import(graph_tree):
    create, freeze = graph_tree
    leaf, source, top = create(3)
    bundle = leaf.parent / 'capture-import.json'
    bundle.write_bytes(b'{}')

    def checkpoint(root):
        value = shape()
        value.update(version=5, **freeze(root), dependency_graph=compact._graph(root))
        return value

    base = checkpoint(leaf)
    plan = read_record(source.with_suffix('.json'))
    plan['goal']['comparison'].update(seed=base, capture_import=descriptor(bundle, base))
    source.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    source_checkpoint = checkpoint(source)
    plan = read_record(top.with_suffix('.json'))
    plan['goal']['comparison']['seed'] = source_checkpoint
    top.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    return leaf, source, top, bundle, base, source_checkpoint


def test_external_bundle_is_one_bounded_file_and_closed_subset_member(graph_import):
    leaf, source, top, bundle, _, source_checkpoint = graph_import
    graph = compact._graph(top)
    imported_node = next(node for node in graph['nodes'] if node['snapshot']['run'] == str(source))
    assert imported_node['capture_import'] == {'path': str(bundle), 'sha256': sha256(b'{}').hexdigest(), 'size': 2}
    assert graph['file_count'] == 19  # Three plans, fifteen run files, one external bundle.
    files = [r.with_suffix('.json') for r in (leaf, source, top)]
    files += [p for r in (leaf, source, top) for p in r.rglob('*') if p.is_file()]
    assert graph['byte_count'] == sum(p.stat().st_size for p in files) + 2
    assert source_checkpoint['dependency_graph']['file_count'] == 13
    assert len(graph['nodes']) == 3


def test_same_external_path_in_two_nodes_counts_once(graph_import):
    _, source, top, bundle, _, source_checkpoint = graph_import
    plan = read_record(top.with_suffix('.json'))
    plan['goal']['comparison']['capture_import'] = descriptor(bundle, source_checkpoint)
    top.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    graph = compact._graph(top)
    assert graph['file_count'] == 19
    assert len([n for n in graph['nodes'] if 'capture_import' in n]) == 2


@pytest.mark.parametrize('case', ['same_mtime', 'missing', 'oversized', 'seed_binding'])
def test_nested_graph_rejects_changed_external_dependency(graph_import, case):
    _, source, top, bundle, _, _ = graph_import
    if case == 'same_mtime':
        stat = bundle.stat()
        bundle.write_bytes(b'[]')
        os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif case == 'missing':
        bundle.unlink()
    elif case == 'oversized':
        bundle.write_bytes(b'x' * (imports.MAX_BYTES + 1))
    else:
        plan = read_record(source.with_suffix('.json'))
        plan['goal']['comparison']['capture_import']['seed_sha256'] = '0' * 64
        source.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ComputerError):
        compact._graph(top)


@pytest.mark.parametrize('limit', ['MAX_FILES', 'MAX_BYTES'])
def test_external_bundle_keeps_existing_aggregate_limits(graph_import, monkeypatch, limit):
    _, _, top, _, _, _ = graph_import
    graph = compact._graph(top)
    monkeypatch.setattr(compact, limit, graph['file_count' if limit == 'MAX_FILES' else 'byte_count'] - 1)
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(top)


def test_optional_fingerprint_shape_is_pure_and_old_nodes_stay_exact(graph_import, monkeypatch):
    leaf, _, top, _, _, _ = graph_import
    graph = compact._graph(top)
    assert set(next(n for n in graph['nodes'] if n['snapshot']['run'] == str(leaf))) == {'role', 'snapshot', 'directories'}
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'stat', lambda *_: pytest.fail('shape touched a file'))
        compact._graph_shape(graph)
    wrong = deepcopy(graph)
    node = next(n for n in wrong['nodes'] if 'capture_import' in n)
    node['capture_import']['size'] = True
    wrong['sha256'] = digest({k: v for k, v in wrong.items() if k != 'sha256'})
    with pytest.raises(ComputerError):
        compact._graph_shape(wrong)


@pytest.fixture
def imported_parent(compact_parent, monkeypatch):
    from agent_service import computer_agent as engine
    from test_comparison_loop import run
    c, _, _, base = compact_parent
    bundle = c['session'].root / 'capture-import.json'
    bundle.write_bytes(b'{}')
    goal = deepcopy(c['goal'])
    goal['comparison'].update(evidence_version=4, seed=base,
        completion_policy='dimension_first_v4', dimension_routing=6,
        capture_import=descriptor(bundle, base))
    monkeypatch.setattr(imports, '_derive', lambda value, seed, raw, base: deepcopy(base))
    imports._CACHE.clear()
    plan = engine.prepare(c['session'], 'dependency-only offline import inspection', goal)
    stopped = run(c, plan, halt=0)
    root = Path(stopped['run'])
    recovery._VERIFIED.clear()
    return c, root, bundle


def test_recovery_warm_hit_rehashes_external_bundle_twice_without_replay(imported_parent, monkeypatch):
    c, root, bundle = imported_parent
    before = recovery._inspect(c['session'], root)
    original = imports.fingerprint
    checked = []
    def tracked(value, base):
        checked.append(value['path'])
        return original(value, base)
    monkeypatch.setattr(imports, 'fingerprint', tracked)
    monkeypatch.setattr(recovery, '_inspect_verified', lambda *a, **k: pytest.fail('warm hit replayed'))
    assert recovery._inspect(c['session'], root) == before
    assert checked == [str(bundle), str(bundle)]


@pytest.mark.parametrize('case', ['same_mtime', 'missing', 'oversized'])
def test_recovery_warm_hit_never_returns_changed_external_bytes(imported_parent, case, monkeypatch):
    c, root, bundle = imported_parent
    recovery._inspect(c['session'], root)
    if case == 'same_mtime':
        stat = bundle.stat()
        bundle.write_bytes(b'[]')
        os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif case == 'missing':
        bundle.unlink()
    else:
        bundle.write_bytes(b'x' * (imports.MAX_BYTES + 1))
    monkeypatch.setattr(recovery, '_inspect_verified', lambda *a, **k: pytest.fail('known import error fell back to replay'))
    with pytest.raises(ComputerError):
        recovery._inspect(c['session'], root)


def test_recovery_cached_exit_rechecks_dependency_after_entry(imported_parent, monkeypatch):
    c, root, bundle = imported_parent
    recovery._inspect(c['session'], root)
    original = imports.fingerprint
    calls = []
    def raced(value, base):
        result = original(value, base)
        calls.append(1)
        if len(calls) == 1:
            bundle.write_bytes(b'[]')
        return result
    monkeypatch.setattr(imports, 'fingerprint', raced)
    with pytest.raises(ComputerError):
        recovery._inspect(c['session'], root)
    assert calls == [1]


@pytest.fixture
def completed_cache(graph_import, monkeypatch):
    # Prime only the completed producer's already-qualified cache boundary;
    # eligibility and navigation derivation are covered by production fixtures.
    leaf, _, top, bundle, base, _ = graph_import
    imported = descriptor(bundle, base)
    plan = {'goal': {'comparison': {'evidence_version': 4, 'seed': base, 'capture_import': imported}}}
    monkeypatch.setattr(recovery, '_plan', lambda *a: deepcopy(plan))
    original_read = seed.read_record
    monkeypatch.setattr(seed, 'read_record', lambda path: {'goal': {'comparison': {'evidence_version': 3}}}
                        if Path(path) == leaf.with_suffix('.json') else original_read(path))
    monkeypatch.setattr(seed, 'load_seed', lambda *_: [])
    frozen = {'run': str(top)}
    key = digest({'run': frozen, 'capture_import': imports.fingerprint(imported, base)})
    expected = {'previously_verified': ['capture']}
    monkeypatch.setattr(seed, '_CACHE', OrderedDict([(key, deepcopy(expected))]))
    return top, bundle, frozen, expected


def test_completed_seed_warm_hit_checks_external_bytes_at_entry_and_exit(completed_cache, monkeypatch):
    root, bundle, frozen, expected = completed_cache
    original, calls = imports.fingerprint, []
    def tracked(value, base):
        calls.append(value['path'])
        return original(value, base)
    monkeypatch.setattr(imports, 'fingerprint', tracked)
    result = seed._verified(root, frozen)
    assert result == expected and calls == [str(bundle), str(bundle)]
    result['previously_verified'].clear()
    assert seed._verified(root, frozen) == expected


@pytest.mark.parametrize('phase', ['entry', 'exit'])
def test_completed_seed_cache_cannot_mask_external_change(completed_cache, monkeypatch, phase):
    root, bundle, frozen, _ = completed_cache
    if phase == 'entry':
        stat = bundle.stat()
        bundle.write_bytes(b'[]')
        os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    else:
        monkeypatch.setattr(seed, 'load_seed', lambda *_: bundle.write_bytes(b'[]'))
    with pytest.raises(ComputerError):
        seed._verified(root, frozen)
