"""Compact checkpoints keep exact captures and bounded, byte-verified ancestry."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import comparison_compact_seed as compact
from agent_service import comparison_failed_seed as failed
from agent_service import comparison_seed as seed
from agent_service.computer import ComputerError, digest, read_record
from test_comparison_failed_seed import failed as failed_run
from test_comparison_loop import ctx
from test_computer_agent_tools import ctx as tool_ctx
from test_comparison_seed import shape


@pytest.fixture
def graph_tree(tmp_path, monkeypatch):
    """Real local bytes with synthetic frozen producers; no eligibility claim."""
    monkeypatch.setattr(seed, '_run_root', lambda p: Path(p))

    def freeze(root):
        inventory = seed._inventory(root)
        return {'run': str(root), 'plan_sha256': seed._hash(root.with_suffix('.json')),
                'outcome_sha256': inventory['outcome.json'], 'inventory': inventory, 'outputs': {}}

    monkeypatch.setattr(failed, '_freeze', freeze)

    def create(count):
        roots, ancestor = [], None
        for index in range(count):
            root = tmp_path / f'{index}.run'
            root.mkdir(exist_ok=True)
            for name in ('claim.json', 'owner.json', 'owner.lease', 'environment-goal.json', 'outcome.json'):
                (root / name).write_text('{}', encoding='utf-8')
            plan = {'goal': {'comparison': {'evidence_version': 4, **({'seed': ancestor} if ancestor else {})}}}
            root.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
            value = shape()
            value.update(version=4, **freeze(root))
            ancestor = value
            roots.append(root)
        return roots
    return create, freeze


def test_flat_graph_accepts_four_edges_and_rejects_five(graph_tree):
    create, _ = graph_tree
    roots = create(5)
    graph = compact._graph(roots[-1])
    assert len(graph['nodes']) == 5 and graph['file_count'] == 30
    roots = create(6)
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(roots[-1])


@pytest.mark.parametrize('case', ['self', 'cycle'])
def test_graph_cycles_rejected_before_replay(graph_tree, case):
    create, _ = graph_tree
    roots = create(2)
    plan = read_record(roots[0].with_suffix('.json'))
    ancestor = deepcopy(read_record(roots[1].with_suffix('.json'))['goal']['comparison']['seed'])
    ancestor['run'] = str(roots[0] if case == 'self' else roots[1])
    plan['goal']['comparison']['seed'] = ancestor
    roots[0].with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ComputerError, match='comparison_compact_seed_invalid'):
        compact._graph(roots[0])


@pytest.mark.parametrize('limit', ['MAX_NODES', 'MAX_FILES', 'MAX_BYTES'])
def test_graph_aggregate_limits_are_enforced(graph_tree, monkeypatch, limit):
    create, _ = graph_tree
    root = create(2)[-1]
    monkeypatch.setattr(compact, limit, 1)
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(root)


def test_shape_is_pure_hash_bound_and_old_v4_rules_remain(graph_tree, monkeypatch):
    create, freeze = graph_tree
    root = create(1)[0]
    value = shape()
    value.update(version=5, **freeze(root), dependency_graph=compact._graph(root))
    with monkeypatch.context() as patch:
        patch.setattr(seed, 'safe_root', lambda *_: pytest.fail('shape touched filesystem'))
        patch.setattr(Path, 'stat', lambda *_: pytest.fail('shape touched filesystem'))
        assert seed.validate_manifest(value) == value
    bad = deepcopy(value)
    bad['dependency_graph']['byte_count'] += 1
    with pytest.raises(ComputerError):
        seed.validate_manifest(bad)
    old = {k: v for k, v in value.items() if k != 'dependency_graph'}
    old['version'] = 4
    assert failed.validate_manifest(old) == old
    old['version'] = 5
    with pytest.raises(ComputerError):
        failed.validate_manifest(old)


def test_actual_failure_exact_selection_fresh_replay_and_copy_isolation(failed_run, monkeypatch):
    _, root, _, _ = failed_run
    before = compact._graph(root)
    calls = []
    original = failed._verified

    def replay(*args, **kwargs):
        calls.append((args[0], failed._REPLAY_MEMO.get()))
        return original(*args, **kwargs)

    monkeypatch.setattr(failed, '_verified', replay)
    # Legacy successful cache entries cannot replace the first compact replay.
    legacy = failed.create_manifest(root)
    selected = legacy['source_ids'][::-1][:2]
    compact._CACHE.clear()
    value = compact.create_manifest(root, selected)
    assert any(memo is not None for _, memo in calls)
    count = len(calls)
    captured, history = failed.load_bundle(value)
    assert [item['id'] for item, _ in captured] == selected
    assert captured == seed.load_seed(value)
    assert len(calls) == count
    assert all('comparison' not in record for _, record in captured)
    captured[0][1]['observation']['text'] = 'caller mutation'
    history['queries'].clear()
    assert compact.load_bundle(value)[0][0][1]['observation']['text'] != 'caller mutation'
    assert compact._graph(root) == before
    compact._CACHE.clear()
    assert compact.load_seed(value)
    assert len(calls) > count
    for field in ('text_sha256', 'record_sha256', 'query_index'):
        bad = deepcopy(value)
        bad['sources'][0][field] = (1 - bad['sources'][0][field] if field == 'query_index' else '0' * 64)
        with pytest.raises(ComputerError, match='comparison_seed_changed'):
            compact.load_seed(bad)


def test_warm_bundle_has_two_complete_scans_and_rejects_exit_change(failed_run, monkeypatch):
    _, root, _, _ = failed_run
    value = compact.create_manifest(root)
    original = compact._graph
    calls = []
    def counted(target):
        calls.append(target)
        return original(target)
    monkeypatch.setattr(compact, '_graph', counted)
    assert compact.load_bundle(value)
    assert calls == [root, root]
    calls.clear()
    def changed_at_exit(target):
        graph = counted(target)
        if len(calls) == 2:
            graph['byte_count'] += 1
        return graph
    monkeypatch.setattr(compact, '_graph', changed_at_exit)
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        compact.load_bundle(value)
    assert calls == [root, root]


@pytest.fixture(scope='module')
def product_checkpoint():
    entry = Path(__file__).resolve().parents[1] / 'validation/artifacts/dimension-repair-20261023/product-start-outcome.json'
    if not entry.exists():
        pytest.skip('local product evidence unavailable')
    root = Path(read_record(entry)['run'])
    before = compact._graph(root)
    compact._CACHE.clear()
    value = compact.create_manifest(root)
    captured, history = compact.load_bundle(value)
    yield root, value, captured, history
    assert compact._graph(root) == before


def test_product_three_failed_ancestors_exact_captures_history_and_legacy_rejection(product_checkpoint):
    root, value, captured, history = product_checkpoint
    with pytest.raises(ComputerError):
        failed._dependencies(read_record(root.with_suffix('.json')), root)
    assert value['version'] == 5 and len(captured) == len(value['source_ids']) == 6
    assert len(value['dependency_graph']['nodes']) == 7
    assert len(history['read_urls']) == 9
    assert len(history['discoveries']) >= 7
    assert all('comparison' not in record for _, record in captured)
    assert compact.load_bundle(value) == (captured, history)


@pytest.mark.parametrize('case', ['bytes', 'active', 'claim', 'empty_step'])
def test_product_cached_ancestors_still_check_bytes_lifecycle_and_claim(product_checkpoint, monkeypatch, case):
    root, value, _, _ = product_checkpoint
    ancestor = Path(read_record(root.with_suffix('.json'))['goal']['comparison']['seed']['run'])
    if case == 'bytes':
        target = ancestor / 'outcome.json'
        original = seed._hash
        monkeypatch.setattr(seed, '_hash', lambda p: '0' * 64 if Path(p) == target else original(p))
    elif case == 'active':
        from agent_service import run_lifecycle
        original = run_lifecycle.assert_inactive
        def active(p):
            if p == ancestor:
                raise ComputerError('run_owner_active')
            return original(p)
        monkeypatch.setattr(run_lifecycle, 'assert_inactive', active)
    elif case == 'claim':
        original = Path.exists
        monkeypatch.setattr(Path, 'exists', lambda p: True if p == ancestor / 'continuation-claim.json' else original(p))
    else:
        original = Path.rglob
        def entries(p, pattern):
            yield from original(p, pattern)
            if p == ancestor:
                yield ancestor / 'step-99'
        original_dir = Path.is_dir
        monkeypatch.setattr(Path, 'rglob', entries)
        monkeypatch.setattr(Path, 'is_dir', lambda p: True if p == ancestor / 'step-99' else original_dir(p))
    with pytest.raises(ComputerError):
        compact.load_seed(value)


def test_exactly_one_compact_edge_and_fresh_subset_equality(graph_tree, monkeypatch):
    create, freeze = graph_tree
    original, root = create(2)
    checkpoint = shape()
    checkpoint.update(version=5, **freeze(original), dependency_graph=compact._graph(original))
    plan = read_record(root.with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint
    root.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    calls = []
    def counted(target):
        calls.append(target)
        return freeze(target)
    monkeypatch.setattr(failed, '_freeze', counted)
    assert len(compact._graph(root)['nodes']) == 2
    assert sorted(calls) == sorted([original, root])
    altered = deepcopy(checkpoint)
    altered['dependency_graph']['byte_count'] += 1
    altered['dependency_graph']['sha256'] = digest({k: v for k, v in altered['dependency_graph'].items() if k != 'sha256'})
    plan['goal']['comparison']['seed'] = altered
    root.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        compact._graph(root)


@pytest.mark.parametrize('changed', ['native', 'claim', 'active'])
def test_closed_subset_includes_recovery_parent_native_bytes_and_claim_once(graph_tree, monkeypatch, changed):
    from agent_service import run_lifecycle
    create, freeze = graph_tree
    parent, original, root = create(3)
    original.with_suffix('.json').write_text(json.dumps({'goal': {'comparison': {'evidence_version': 3}},
        'recovery': {'parent_run': str(parent)}}), encoding='utf-8')
    (parent / 'outcome.json').write_text(json.dumps({'artifacts': [{}]}), encoding='utf-8')
    (parent / 'continuation-claim.json').write_text('{}', encoding='utf-8')
    output = parent.parent / 'native'
    output.mkdir()
    native = output / 'document.docx'
    native.write_bytes(b'original native output')
    snapshots, leases = [], []
    def parent_snapshot(target):
        assert target == parent
        snapshots.append(target)
        return {**freeze(target), 'outputs': {str(output): seed._inventory(output)}}
    monkeypatch.setattr(seed, '_snapshot', parent_snapshot)
    monkeypatch.setattr(run_lifecycle, 'assert_inactive', lambda target: leases.append(target))
    ancestor_graph = compact._graph(original)
    checkpoint = shape()
    checkpoint.update(version=5, **freeze(original), dependency_graph=ancestor_graph)
    root.with_suffix('.json').write_text(json.dumps({'goal': {'comparison': {
        'evidence_version': 4, 'seed': checkpoint}}}), encoding='utf-8')
    snapshots.clear(); leases.clear()
    graph = compact._graph(root)
    assert len(graph['nodes']) == 3
    assert graph['file_count'] == ancestor_graph['file_count'] + 6
    assert graph['byte_count'] > ancestor_graph['byte_count']
    assert snapshots == leases == [parent]
    if changed == 'native':
        native.write_bytes(b'changed native output')
    elif changed == 'claim':
        (parent / 'continuation-claim.json').write_text('{"changed":true}', encoding='utf-8')
    else:
        def active(target):
            raise ComputerError('run_owner_active')
        monkeypatch.setattr(run_lifecycle, 'assert_inactive', active)
    with pytest.raises(ComputerError):
        compact._graph(root)


def test_two_compact_edges_fit_but_third_rejected_before_recursive_loader(graph_tree, monkeypatch):
    create, freeze = graph_tree
    original, first, second, third = create(4)
    def checkpoint(root):
        value = shape()
        value.update(version=5, **freeze(root), dependency_graph=compact._graph(root))
        return value
    plan = read_record(first.with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(original)
    first.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    plan = read_record(second.with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(first)
    second.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    assert len(compact._graph(second)['nodes']) == 3
    plan = read_record(third.with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(second)
    third.with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    monkeypatch.setattr(seed, 'load_seed', lambda *_: pytest.fail('recursive loader before finite graph walk'))
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(third)


def test_five_failed_edges_allowed_only_with_exactly_two_compact_edges(graph_tree):
    create, freeze = graph_tree
    roots = create(6)
    def checkpoint(root):
        value = shape()
        value.update(version=5, **freeze(root), dependency_graph=compact._graph(root))
        return value
    # One compact edge plus four old failed edges still exceeds the old cap.
    plan = read_record(roots[5].with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(roots[4])
    roots[5].with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(roots[5])
    # Exactly two compact edges may add one failed edge, within all other caps.
    plan = read_record(roots[4].with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(roots[3])
    roots[4].with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    plan = read_record(roots[5].with_suffix('.json'))
    plan['goal']['comparison']['seed'] = checkpoint(roots[4])
    roots[5].with_suffix('.json').write_text(json.dumps(plan), encoding='utf-8')
    assert len(compact._graph(roots[5])['nodes']) == 6
    assert compact.MAX_FAILED_ANCESTORS == 4 and compact.MAX_NODES == 10


def test_two_checkpoints_never_raise_ten_node_limit(graph_tree, monkeypatch):
    from agent_service import comparison_research_seed, run_lifecycle
    create, freeze = graph_tree
    roots = create(11)
    parents, research, failed_roots = roots[:4], roots[4], roots[5:]
    monkeypatch.setattr(run_lifecycle, 'assert_inactive', lambda *_: None)
    research.with_suffix('.json').write_text(json.dumps({'goal': {}}), encoding='utf-8')
    output = research.parent / 'output'
    output.mkdir()
    (output / 'native.docx').write_bytes(b'native')
    def freeze_research(target):
        return {**freeze(target), 'outputs': {str(output): seed._inventory(output)}}
    monkeypatch.setattr(comparison_research_seed, '_freeze', freeze_research)
    ancestor = shape()
    ancestor.update(version=2, **freeze_research(research))
    ancestor['source_ids'] = ancestor['source_ids'][:2]
    ancestor['sources'] = ancestor['sources'][:2]
    for index, root in enumerate(failed_roots):
        value = {'goal': {'comparison': {'evidence_version': 4, 'seed': ancestor}}}
        if index < 4:
            value['recovery'] = {'parent_run': str(parents[index])}
        root.with_suffix('.json').write_text(json.dumps(value), encoding='utf-8')
        if index == 5:
            break
        ancestor = shape()
        ancestor.update(version=5 if index >= 3 else 4, **freeze(root))
        if index >= 3:
            ancestor['dependency_graph'] = compact._graph(root)
    with pytest.raises(ComputerError, match='comparison_compact_seed_limit'):
        compact._graph(failed_roots[-1])


@pytest.fixture
def navigation_tree(graph_tree, monkeypatch):
    """Exercise extraction after a mocked production eligibility boundary."""
    from agent_service.contracts import Observation
    from agent_service.computer_agent_tools import _save
    create, freeze = graph_tree
    root = create(1)[0]
    observations = []
    for index in range(1, 11):
        product = 'openpyxl' if index == 2 else 'XlsxWriter'
        observation = Observation(f'https://example.test/{index}', f'https://example.test/{index}',
            product, f'{product} literal navigation https://example.test/{index}',
            f'2026-10-23T00:00:{10-index:02}+00:00').to_dict()
        if index == 3:
            observation = deepcopy(observations[0])
        observations.append(observation)
        step = root / f'step-{index:02}'
        step.mkdir()
        _save(step / 'environment-result.json', {'action': {'kind': 'read'},
            'result': {'ok': index != 4, 'state': 'verified' if index != 4 else 'failed'},
            'observation': observation})
    # A valid envelope outside replayed step directories cannot supply text.
    _save(root / 'environment-result.json', {'action': {'kind': 'read'},
        'result': {'ok': True, 'state': 'verified'}, 'observation': observations[0]})
    value = shape()
    value.update(version=5, **freeze(root), dependency_graph=compact._graph(root))
    calls = []
    def validated(manifest):
        calls.append(deepcopy(manifest))
        return ([], {})
    monkeypatch.setattr(compact, 'load_bundle', validated)
    return root, value, observations, calls


def test_navigation_only_successful_replayed_steps_old_to_new_unique_and_bounded(navigation_tree):
    _, value, observations, calls = navigation_tree
    result = compact.load_navigation_observations(value, 'XlsxWriter formula calculation')
    assert calls == [value]
    assert result == [observations[i-1] for i in (9, 8, 7, 6, 5, 1)]
    assert all(set(row) == {'requested_url', 'final_url', 'title', 'text', 'captured_at', 'sha256'}
               for row in result)
    assert compact.load_navigation_observations(value, 'openpyxl') == [observations[1]]
    assert compact.load_navigation_observations(value, 'formula calculation API') == []
    result[0]['text'] = 'caller mutation'
    assert compact.load_navigation_observations(value, 'XlsxWriter')[0] == observations[8]


def test_navigation_requires_bundle_validation_before_extracting_any_record(navigation_tree, monkeypatch):
    _, value, _, _ = navigation_tree
    def rejected(*_):
        raise ComputerError('run_owner_active')
    monkeypatch.setattr(compact, 'load_bundle', rejected)
    monkeypatch.setattr(compact, '_navigation_record', lambda *_: pytest.fail('read before eligibility'))
    with pytest.raises(ComputerError, match='run_owner_active'):
        compact.load_navigation_observations(value, 'XlsxWriter')


def test_navigation_rechecks_complete_graph_after_extraction(navigation_tree, monkeypatch):
    _, value, _, _ = navigation_tree
    altered = deepcopy(value['dependency_graph'])
    altered['byte_count'] += 1
    monkeypatch.setattr(compact, '_graph', lambda *_: altered)
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        compact.load_navigation_observations(value, 'XlsxWriter')


def test_navigation_checks_observation_integrity_even_after_bundle_boundary(navigation_tree, monkeypatch):
    root, value, _, _ = navigation_tree
    original = compact._navigation_record
    def altered(path, expected):
        record = original(path, expected)
        record['observation']['sha256'] = '0' * 64
        return record
    monkeypatch.setattr(compact, '_navigation_record', altered)
    with pytest.raises(ComputerError, match='browser_observation_invalid'):
        compact.load_navigation_observations(value, 'XlsxWriter')


def test_navigation_parsed_bytes_must_match_frozen_inventory_even_if_graph_restored(navigation_tree, monkeypatch):
    root, value, _, _ = navigation_tree
    target = root / 'step-01/environment-result.json'
    original = target.read_bytes()
    envelope = json.loads(original)
    envelope['data']['observation']['text'] = 'XlsxWriter transient unverified link https://bad.test/'
    from hashlib import sha256
    envelope['data']['observation']['sha256'] = sha256(envelope['data']['observation']['text'].encode()).hexdigest()
    envelope['sha256'] = digest(envelope['data'])
    target.write_text(json.dumps(envelope), encoding='utf-8')
    try:
        # Simulate restored entry/exit graph bytes: only the parsed-read hash
        # can reject this different, internally valid record before use.
        monkeypatch.setattr(compact, '_graph', lambda *_: value['dependency_graph'])
        with pytest.raises(ComputerError, match='comparison_seed_changed'):
            compact.load_navigation_observations(value, 'XlsxWriter')
    finally:
        target.write_bytes(original)


def test_product_navigation_includes_evicted_original_literal_xlsxwriter_link(product_checkpoint):
    _, value, active, _ = product_checkpoint
    observations = compact.load_navigation_observations(value, 'XlsxWriter formula calculation')
    assert 1 <= len(observations) <= 6
    original = [row for row in observations if row['requested_url'] == 'https://github.com/jmcnamara/XlsxWriter']
    assert len(original) == 1
    assert 'https://xlsxwriter.readthedocs.io' in original[0]['text']
    assert original[0] not in [record['observation'] for _, record in active]
    assert observations == compact.load_navigation_observations(value, 'XlsxWriter formula calculation')


@pytest.fixture(scope='module')
def nested_checkpoint():
    entry = Path(__file__).resolve().parents[1] / 'validation/artifacts/formula-evidence-20261023/start-outcome.json'
    if not entry.exists():
        pytest.skip('local first formula navigation failure unavailable')
    root = Path(read_record(entry)['run'])
    before = compact._graph(root)
    value = compact.create_manifest(root)
    captured, history = compact.load_bundle(value)
    yield root, value, captured, history
    assert compact._graph(root) == before


def test_real_one_nested_checkpoint_preserves_original_three_failures_and_new_navigation(nested_checkpoint):
    root, value, captured, history = nested_checkpoint
    ancestor = read_record(root.with_suffix('.json'))['goal']['comparison']['seed']
    previous, consumed = compact.load_bundle(ancestor)
    assert ancestor['version'] == 5 and value['version'] == 5
    assert len(value['dependency_graph']['nodes']) == 8
    assert captured == previous and len(captured) == 2
    assert len(list(root.glob('step-*'))) == 3
    assert read_record(root / 'outcome.json')['ok'] is False
    assert all(row in history['discoveries'] for row in consumed['discoveries'])
    assert len(history['discoveries']) == len(consumed['discoveries']) + 2
    added = [row for row in history['discoveries'] if row not in consumed['discoveries']]
    assert sum(len(row['proof']['pages']) for row in added) == 8
    assert any(page['url'].startswith('https://xlsxwriter.readthedocs.io')
               for row in added for page in row['proof']['pages'])
    with pytest.raises(ComputerError):
        failed.create_manifest(root)


def test_nested_checkpoint_cache_still_binds_new_navigation_receipt_bytes(nested_checkpoint, monkeypatch):
    root, value, _, _ = nested_checkpoint
    target = root / 'step-02/environment-result.json'
    original = seed._hash
    monkeypatch.setattr(seed, '_hash', lambda p: '0' * 64 if Path(p) == target else original(p))
    with pytest.raises(ComputerError, match='comparison_seed_changed'):
        compact.load_bundle(value)
