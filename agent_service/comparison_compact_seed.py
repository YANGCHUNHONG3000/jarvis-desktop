"""Bounded, flat failed-capture checkpoints; historical receipts stay immutable.

The graph is an inventory, never a certificate of eligibility. A new fingerprint
must pass the original production replay before its result may enter the cache.
"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from . import comparison_seed as seed
from . import comparison_failed_seed as failed
from .computer import ComputerError, digest, read_record, safe_root

MAX_NODES = 10
MAX_FAILED_ANCESTORS = 4
MAX_COMPACT_ANCESTORS = 2
MAX_FILES = 2560
MAX_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024
_CACHE = OrderedDict()


def _fail(code='comparison_compact_seed_invalid'):
    raise ComputerError(code)


def _graph_shape(graph):
    if (type(graph) is not dict or set(graph) != {'nodes', 'file_count', 'byte_count', 'sha256'}
            or not seed._hash_value(graph['sha256'])
            or type(graph['file_count']) is not int or not 1 <= graph['file_count'] <= MAX_FILES
            or type(graph['byte_count']) is not int or not 0 <= graph['byte_count'] <= MAX_BYTES
            or type(graph['nodes']) is not list or not 1 <= len(graph['nodes']) <= MAX_NODES):
        _fail()
    runs = []
    for node in graph['nodes']:
        fields = {'role', 'snapshot', 'directories'}
        if (type(node) is not dict or set(node) not in (fields, fields | {'capture_import'})
                or type(node['role']) is not str or node['role'] not in {'failed', 'research', 'parent'}):
            _fail()
        if 'capture_import' in node:
            from .comparison_capture_import import MAX_BYTES as max_import_bytes
            imported = node['capture_import']
            if (type(imported) is not dict or set(imported) != {'path', 'sha256', 'size'}
                    or not seed._plain(imported['path'], 2000) or not Path(imported['path']).is_absolute()
                    or not seed._hash_value(imported['sha256'])
                    or type(imported['size']) is not int or not 0 <= imported['size'] <= max_import_bytes):
                _fail()
        snap = node['snapshot']
        if (type(snap) is not dict or set(snap) != {'run', 'plan_sha256', 'outcome_sha256', 'inventory', 'outputs'}
                or not seed._plain(snap['run'], 2000) or not seed._hash_value(snap['plan_sha256'])
                or not seed._hash_value(snap['outcome_sha256'])
                or type(snap['outputs']) is not dict or len(snap['outputs']) > 2):
            _fail()
        seed._inventory_shape(snap['inventory'])
        if snap['inventory'].get('outcome.json') != snap['outcome_sha256']:
            _fail()
        for folder, inventory in snap['outputs'].items():
            if not seed._plain(folder, 2000):
                _fail()
            seed._inventory_shape(inventory)
        dirs = node['directories']
        if (type(dirs) is not list or len(dirs) > seed.MAX_FILES * 2
                or any(not seed._plain(name, 1000) or '\\' in name or ':' in name
                       or any(p in {'', '.', '..'} for p in name.split('/')) for name in dirs)
                or dirs != sorted(set(dirs))):
            _fail()
        runs.append(snap['run'])
    if runs != sorted(set(runs)) or digest({k: v for k, v in graph.items() if k != 'sha256'}) != graph['sha256']:
        _fail()


def validate_manifest(value):
    """Pure bounded shape validation; graph equality is established on loading."""
    if type(value) is not dict or 'dependency_graph' not in value:
        seed._fail()
    base = {k: v for k, v in value.items() if k != 'dependency_graph'}
    seed._validate_shape(base, version=5, require_lifecycle=True, empty_outputs=True)
    _graph_shape(value['dependency_graph'])
    roots = [n for n in value['dependency_graph']['nodes'] if n['snapshot']['run'] == value['run']]
    if (len(roots) != 1 or roots[0]['role'] != 'failed'
            or any(roots[0]['snapshot'][k] != value[k] for k in roots[0]['snapshot'])
            or len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_MANIFEST_BYTES):
        _fail()
    return deepcopy(value)


def _graph(root):
    """Freeze the complete finite graph without invoking any recursive loader."""
    from .comparison_research_seed import _freeze as freeze_research
    from .run_lifecycle import assert_inactive

    nodes, seen, parents, paths = [], set(), [], {}
    compact_ancestors = []
    edges, node_paths = {}, {}

    def reserve(value):
        target = seed._run_root(value)
        if target in seen:
            _fail()
        if len(seen) >= MAX_NODES:
            _fail('comparison_compact_seed_limit')
        seen.add(target)
        return target

    def append(target, role, snapshot, plan):
        own_paths = node_paths.setdefault(str(target), set())
        directories = []
        for path in target.rglob('*'):
            safe_root(path)
            if path.is_dir():
                directories.append(path.relative_to(target).as_posix())
                if len(directories) > seed.MAX_FILES * 2:
                    _fail('comparison_compact_seed_limit')
        node = {'role': role, 'snapshot': snapshot, 'directories': sorted(directories)}
        comparison = plan.get('goal', {}).get('comparison', {})
        if 'capture_import' in comparison:
            from .comparison_capture_import import fingerprint
            node['capture_import'] = fingerprint(comparison['capture_import'], comparison.get('seed'))
        nodes.append(node)
        files = [(target.with_suffix('.json'), snapshot['plan_sha256'])]
        files.extend((target / name, hashed) for name, hashed in snapshot['inventory'].items())
        for folder, inventory in snapshot['outputs'].items():
            files.extend((Path(folder) / name, hashed) for name, hashed in inventory.items())
        if 'capture_import' in node:
            imported = node['capture_import']
            files.append((Path(imported['path']), imported['sha256']))
        for path, hashed in files:
            key = str(safe_root(path))
            entry = (hashed, path.stat().st_size)
            if key in paths and paths[key] != entry:
                _fail('comparison_seed_changed')
            paths[key] = entry
            own_paths.add(key)
            if len(paths) > MAX_FILES or sum(size for _, size in paths.values()) > MAX_BYTES:
                _fail('comparison_compact_seed_limit')

    def visit(target, role, layers):
        target = reserve(target)
        edges[str(target)] = []
        plan = read_record(target.with_suffix('.json'))
        if 'recovery' in plan:
            meta = plan['recovery']
            if type(meta) is not dict or 'parent_run' not in meta:
                _fail()
            parents.append(meta['parent_run'])
            edges[str(target)].append(str(seed._run_root(meta['parent_run'])))
            if len(parents) > 4:
                _fail('comparison_compact_seed_limit')
        ancestor = plan.get('goal', {}).get('comparison', {}).get('seed')
        if role == 'research':
            if 'comparison' in plan.get('goal', {}):
                _fail()
            snapshot = freeze_research(target)
        else:
            if ancestor is not None:
                if type(ancestor) is not dict or type(ancestor.get('version')) is not int:
                    _fail()
                version = ancestor['version']
                if version not in {2, 4, 5}:
                    _fail()
                seed.validate_manifest(ancestor)
                if version == 5:
                    if len(compact_ancestors) >= MAX_COMPACT_ANCESTORS:
                        _fail('comparison_compact_seed_limit')
                    compact_ancestors.append(ancestor)
                failed_limit = MAX_FAILED_ANCESTORS + (len(compact_ancestors) == 2)
                if version in {4, 5} and layers >= failed_limit:
                    _fail('comparison_compact_seed_limit')
                child = visit(ancestor['run'], 'research' if version == 2 else 'failed',
                              layers + (version in {4, 5}))
                edges[str(target)].append(child['run'])
                if any(ancestor.get(k) != v for k, v in child.items()):
                    _fail('comparison_seed_changed')
            snapshot = failed._freeze(target)
        append(target, role, snapshot, plan)
        return snapshot

    visit(root, 'failed', 0)
    for value in parents:
        target = reserve(value)
        assert_inactive(target)
        plan = read_record(target.with_suffix('.json'))
        if 'recovery' in plan:
            _fail()
        outcome = read_record(target / 'outcome.json')
        if outcome.get('artifacts'):
            snapshot = seed._snapshot(target)
        else:
            inventory = seed._inventory(target)
            snapshot = {'run': str(target), 'plan_sha256': seed._hash(target.with_suffix('.json')),
                        'outcome_sha256': inventory.get('outcome.json'), 'inventory': inventory, 'outputs': {}}
        append(target, 'parent', snapshot, plan)
    # All hashes, ownership checks and file sizes above belong to this one
    # synchronous scan. Derive the closed ancestor subset from these exact
    # nodes instead of hashing that same subtree a second time.
    actual = {node['snapshot']['run']: node for node in nodes}
    for ancestor in compact_ancestors:
        closed, pending = set(), [str(seed._run_root(ancestor['run']))]
        while pending:
            name = pending.pop()
            if name in closed:
                continue
            if name not in actual:
                _fail()
            closed.add(name)
            pending.extend(edges.get(name, ()))
        subset_paths = set().union(*(node_paths[name] for name in closed))
        subset_body = {'nodes': [actual[name] for name in sorted(closed)],
                       'file_count': len(subset_paths),
                       'byte_count': sum(paths[name][1] for name in subset_paths)}
        subset = {**subset_body, 'sha256': digest(subset_body)}
        if subset != ancestor['dependency_graph']:
            _fail('comparison_seed_changed')
    body = {'nodes': sorted(nodes, key=lambda n: n['snapshot']['run']),
            'file_count': len(paths), 'byte_count': sum(size for _, size in paths.values())}
    graph = {**body, 'sha256': digest(body)}
    _graph_shape(graph)
    return graph


def _verified(root, frozen, graph):
    key = digest({'root': frozen, 'dependency_graph': graph})
    if key in _CACHE:
        result = deepcopy(_CACHE[key])
        # Both callers own complete entry/exit graph scans. Lookup is pure;
        # another identical middle scan cannot establish any new eligibility.
        _CACHE.move_to_end(key)
        return result

    def dependencies(plan, target):
        if target != root:
            _fail()
        current = _graph(root)
        if current != graph:
            _fail('comparison_seed_changed')
        return current

    token = failed._REPLAY_MEMO.set({})
    try:
        result = failed._verified(root, frozen, dependency_validator=dependencies)
    finally:
        failed._REPLAY_MEMO.reset(token)
    if _graph(root) != graph:
        _fail('comparison_seed_changed')
    _CACHE[key] = deepcopy(result)
    while len(_CACHE) > 4:
        _CACHE.popitem(last=False)
    return result


def _guard(operation):
    try:
        return operation()
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def create_manifest(run, source_ids=None):
    def create():
        root = seed._run_root(run)
        graph = _graph(root)
        frozen = next(n['snapshot'] for n in graph['nodes'] if n['snapshot']['run'] == str(root))
        verified = _verified(root, frozen, graph)
        ids = deepcopy(verified['active_ids'] if source_ids is None else source_ids)
        if (type(ids) is not list or not ids or any(type(sid) is not str for sid in ids)
                or len(set(ids)) != len(ids)):
            _fail()
        value = validate_manifest({'version': 5, **frozen, 'queries': verified['queries'],
            'source_ids': ids, 'sources': seed._selected(verified, ids), 'dependency_graph': graph})
        if _graph(root) != graph:
            _fail('comparison_seed_changed')
        return value
    return _guard(create)


def _load_verified(value):
    manifest = validate_manifest(value)
    root = seed._run_root(manifest['run'])
    graph = _graph(root)
    if graph != manifest['dependency_graph']:
        _fail('comparison_seed_changed')
    frozen = next(n['snapshot'] for n in graph['nodes'] if n['snapshot']['run'] == str(root))
    verified = _verified(root, frozen, graph)
    if (manifest['queries'] != verified['queries']
            or manifest['sources'] != seed._selected(verified, manifest['source_ids'])
            or _graph(root) != graph):
        _fail('comparison_seed_changed')
    return manifest, verified


def load_bundle(manifest):
    def load():
        value, verified = _load_verified(manifest)
        return deepcopy(([verified['sources'][sid] for sid in value['source_ids']], verified['repair_history']))
    return _guard(load)


def load_seed(manifest):
    return load_bundle(manifest)[0]


def load_repair_history(manifest):
    return load_bundle(manifest)[1]


def _navigation_record(path, expected):
    """Bind the bytes actually parsed to the replayed graph, including ABA races."""
    with safe_root(path).open('rb') as stream:
        raw = stream.read(300_001)
    if len(raw) > 300_000 or sha256(raw).hexdigest() != expected:
        _fail('comparison_seed_changed')
    value = json.loads(raw.decode('utf-8'))
    if (type(value) is not dict or set(value) != {'data', 'sha256'}
            or digest(value['data']) != value['sha256']):
        _fail('agent_evidence_changed')
    return value['data']


def load_navigation_observations(manifest, topic):
    """Return historical literal navigation text, including evicted reads.

    Eligibility comes from the same complete producer replay as load_bundle.
    Matching a product word grants no publisher or semantic evidence verdict.
    """
    def load():
        from .comparison_dimension_discovery import _TOPIC_STOP
        from .computer_agent_tools import AgentEnvironment

        if not seed._plain(topic, 500):
            _fail()
        value = validate_manifest(manifest)
        load_bundle(value)
        topic_text = re.sub(r'\bsite:\S+', '', topic.casefold())
        products = set(re.findall(r'[a-z][a-z0-9]*', topic_text)) - _TOPIC_STOP - {'api'}
        rows = []
        for node in value['dependency_graph']['nodes']:
            snapshot = node['snapshot']
            root = seed._run_root(snapshot['run'])
            for name in sorted(snapshot['inventory']):
                if re.fullmatch(r'step-\d{2}/environment-result\.json', name) is None:
                    continue
                record = _navigation_record(root / name, snapshot['inventory'][name])
                if (record.get('action', {}).get('kind') != 'read'
                        or record.get('result', {}).get('ok') is not True
                        or record['result'].get('state') != 'verified'):
                    continue
                observation = record.get('observation')
                if type(observation) is not dict:
                    _fail()
                AgentEnvironment._validate_page(observation, observation['requested_url'])
                words = set(re.findall(r'[a-z][a-z0-9]*', ' '.join(
                    observation[k] for k in ('requested_url', 'final_url', 'title', 'text')).casefold()))
                if products & words:
                    rows.append((datetime.fromisoformat(observation['captured_at']).timestamp(),
                                 snapshot['run'], name, deepcopy(observation)))
        if _graph(seed._run_root(value['run'])) != value['dependency_graph']:
            _fail('comparison_seed_changed')
        observations, seen = [], set()
        for _, _, _, observation in sorted(rows, key=lambda row: row[:3]):
            key = digest(observation)
            if key not in seen:
                seen.add(key)
                observations.append(observation)
        # Preserve recent product navigation when the older graph has grown;
        # the returned bounded selection still has chronological order.
        return observations[-6:]
    return _guard(load)
