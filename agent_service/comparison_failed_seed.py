"""Read-only failed-comparison captures; no old verdict or execution authority."""
from copy import deepcopy
from collections import OrderedDict
from contextvars import ContextVar
import time

from . import comparison_seed as seed
from .computer import ComputerError, ComputerSession, digest, read_record, safe_root
from .computer_goal_adapters import TASK_BASE

FAILURES = frozenset({'comparison_transport_invalid', 'comparison_review_invalid',
    'comparison_evidence_invalid', 'comparison_independence_insufficient',
    'comparison_coverage_insufficient', 'comparison_semantic_insufficient'})
_CACHE = OrderedDict()
_REPLAY_MEMO = ContextVar('comparison_failed_replay_memo', default=None)


def _fail(code='comparison_failed_seed_invalid'):
    raise ComputerError(code)


def validate_manifest(value):
    return seed._validate_shape(value, version=4, require_lifecycle=True, empty_outputs=True)


def _freeze(root):
    from .run_lifecycle import assert_inactive
    assert_inactive(root)
    if (root / 'continuation-claim.json').exists():
        _fail('comparison_failed_seed_claimed')
    inventory = seed._inventory(root)
    outcome = read_record(root / 'outcome.json')
    if outcome.get('artifacts') != [] or outcome.get('outputs') != {}:
        _fail()
    return {'run': str(root), 'plan_sha256': seed._hash(root.with_suffix('.json')),
            'outcome_sha256': inventory.get('outcome.json'), 'inventory': inventory, 'outputs': {}}


def _dependencies(plan, root=None):
    """Rehash the entire bounded dependency graph, including native outputs.

    Cache entries only follow a successful full replay. Ownership and every
    dependency byte are still checked on every hit; no mtime-based trust.
    """
    from .run_lifecycle import assert_inactive
    from .comparison_research_seed import _freeze as freeze_research
    snapshots, parents = {}, []
    seen = {root} if root is not None else set()

    def reserve(value):
        target = seed._run_root(value)
        if target in seen:
            _fail()
        seen.add(target)
        return target

    def visit(value, failed_layers):
        meta = value.get('recovery')
        if meta is not None:
            if type(meta) is not dict:
                _fail()
            parents.append(meta['parent_run'])
        ancestor = value['goal'].get('comparison', {}).get('seed')
        if ancestor is None:
            return
        if type(ancestor) is not dict or ancestor.get('version') not in {2, 4}:
            _fail()
        version = ancestor['version']
        if version == 4 and failed_layers == 0:
            _fail()
        target = reserve(ancestor['run'])
        ancestor_plan = read_record(target.with_suffix('.json'))
        if version == 2:
            if 'comparison' in ancestor_plan.get('goal', {}):
                _fail()
            frozen = freeze_research(target)
            if 'recovery' in ancestor_plan:
                parents.append(ancestor_plan['recovery']['parent_run'])
        else:
            validate_manifest(ancestor)
            # Check the complete shape before any recursive seed loader runs.
            # At most two imported failed runs may precede ordinary research.
            visit(ancestor_plan, failed_layers - 1)
            frozen = _freeze(target)
        if any(ancestor.get(k) != v for k, v in frozen.items()):
            _fail('comparison_seed_changed')
        snapshots[str(target)] = frozen

    visit(plan, 2)
    if len(parents) > 4:
        _fail()
    for value in parents:
        target = reserve(value)
        assert_inactive(target)
        parent_plan = read_record(target.with_suffix('.json'))
        if 'recovery' in parent_plan:
            _fail()
        outcome = read_record(target / 'outcome.json')
        if outcome.get('artifacts'):
            snapshots[str(target)] = seed._snapshot(target)
        else:
            snapshots[str(target)] = {'plan': seed._hash(target.with_suffix('.json')),
                                      'inventory': seed._inventory(target)}
    return snapshots


def _verified(root, frozen, *, dependency_validator=None):
    from .comparison_completion import CompletionEnvironment
    from .comparison_crosssite import CrossSiteEnvironment
    from .computer_agent_tools import _load
    from .research_recovery import _plan, _lineage
    plan = _plan(None, root)
    if 'recovery' in plan and type(plan['recovery']) is not dict:
        _fail()
    dependencies_for = dependency_validator or _dependencies
    dependencies = dependencies_for(plan, root)
    key = digest({'run': frozen, 'dependencies': dependencies})
    memo = _REPLAY_MEMO.get()
    cache = _CACHE if memo is None else memo
    if key in cache:
        result = deepcopy(cache[key])
        if _freeze(root) != frozen or dependencies_for(plan, root) != dependencies:
            _fail('comparison_seed_changed')
        if memo is None:
            _CACHE.move_to_end(key)
        return result
    goal, meta = plan['goal'], plan.get('recovery')
    comparison = goal.get('comparison', {})
    version = comparison.get('evidence_version')
    if version not in {3, 4}:
        _fail()
    ancestor = comparison.get('seed')
    if version == 4:
        supported = {2, 4, 5} if dependency_validator is not None else {2, 4}
        if type(ancestor) is not dict or ancestor.get('version') not in supported:
            _fail()
        ancestor_root = seed._run_root(ancestor['run'])
        ancestor_plan = read_record(ancestor_root.with_suffix('.json'))
        if (ancestor_root == root or ancestor['version'] == 2
                and 'comparison' in ancestor_plan.get('goal', {})):
            _fail()
        seed.load_seed(ancestor)
    lineage = {'records': [], 'origins': {}, 'depth': 0, 'hashes': []}
    parent = parent_before = None
    session = ComputerSession(root.parent.parent)
    if meta is not None:
        if (type(meta) is not dict or meta.get('version') != 2 or meta.get('kind') != 'research'
                or type(meta.get('depth')) is not int or meta['depth'] != 1 or version != 4):
            _fail()
        parent = seed._run_root(meta['parent_run'])
        parent_plan = _plan(None, parent)
        if parent == root or parent.parent != root.parent or 'recovery' in parent_plan or parent_plan['goal'] != goal:
            _fail()
        parent_before = (seed._hash(parent.with_suffix('.json')), seed._inventory(parent))
        lineage = _lineage(session, plan, claimed=True)
        if lineage['depth'] != 1 or not lineage['records']:
            _fail()
    env = object.__new__(CrossSiteEnvironment if version == 4 else CompletionEnvironment)
    env.__dict__.update(goal=deepcopy(goal), run=root, stop=lambda: False,
        deadline=float('inf'), clock=time.monotonic, search=None, browser=None,
        office_execute=None, task_base=safe_root(TASK_BASE),
        recovery_validator=(lambda: lineage) if meta is not None else None,
        comparison_select=None, comparison_review=None, record_origins={})
    facts = env._facts()
    env._facts = lambda: facts
    observed = env.observe_final()
    offset, directories = len(lineage['records']), sorted(root.glob('step-*'))
    if (observed['checks']['goal_met'] or facts['office_started'] or facts['artifacts']
            or not 1 <= len(directories) <= 12 or offset + len(directories) > 12
            or len(facts['records']) != offset + len(directories)
            or facts['records'][:offset] != lineage['records']
            or not facts['comparisons'] or facts['comparisons'][-1]['result']['ok'] is not False
            or facts['comparisons'][-1]['result']['code'] not in FAILURES):
        _fail()
    if any(row['action']['kind'] not in {'search', 'read', 'compare', 'supplement'}
           or row['result']['state'] not in {'verified', 'failed'} for row, _ in facts['records']):
        _fail()
    origins = {**lineage['origins'], **{h: {'run': str(root), 'step': str(root / f'step-{i:02}')}
        for i, (_, h) in enumerate(facts['records'][offset:], 1)}}
    if env.record_origins != origins:
        _fail()
    attempts = []
    for number, step in enumerate(directories, 1):
        if step.name != f'step-{number:02}' or not step.is_dir():
            _fail()
        intent, intent_hash = _load(step / 'environment-intent.json')
        record, record_hash = _load(step / 'environment-result.json')
        chosen, verification = read_record(step / 'intent.json'), read_record(step / 'verification.json')
        if (chosen.get('action') != intent.get('action') or record.get('action') != intent.get('action')
                or record.get('intent_sha256') != intent_hash or record.get('result') != verification
                or type(verification.get('ok')) is not bool or verification.get('state') not in {'verified', 'failed'}
                or verification['ok'] != (verification['state'] == 'verified')
                or facts['records'][offset + number - 1] != (record, record_hash)):
            _fail()
        attempts.append({'step': number, 'action': record['action'], 'result': verification})
    outcome = read_record(root / 'outcome.json')
    if (outcome.get('ok') is not False or outcome.get('state') not in {'partial', 'stopped'}
            or outcome.get('code') != 'agent_no_safe_next_action' or outcome.get('engine') != 'observed_agent'
            or outcome.get('mode') != 'adapters' or outcome.get('run') != str(root)
            or type(outcome.get('actions')) is not int or outcome['actions'] != len(attempts)
            or outcome.get('attempts') != attempts
            or outcome.get('completed') != [row for row in attempts if row['result']['ok']]
            or outcome.get('artifacts') != [] or outcome.get('outputs') != {}
            or type(outcome.get('source_reads')) is not int or outcome['source_reads'] != observed['source_reads']
            or outcome.get('failures') != observed['failures']
            or ('recovery' in outcome) != (meta is not None) or outcome.get('recovery') != meta):
        _fail()
    if ('comparison_assessment' in outcome and outcome['comparison_assessment'] != observed.get('comparison_assessment')
            or 'comparison_dimensions' in outcome and outcome['comparison_dimensions'] != observed.get('comparison_dimensions')):
        _fail()
    sources = {item['id']: (item, facts['reads'][item['id']]) for item in facts['candidates']
        if item['id'] in facts['active_ids'] and item['id'] in facts['reads'] and facts['reads'][item['id']]['result']['ok']}
    if not sources:
        _fail('comparison_seed_insufficient')
    if _freeze(root) != frozen:
        _fail('comparison_seed_changed')
    if ancestor is not None:
        seed.load_seed(ancestor)
    if parent is not None and ((seed._hash(parent.with_suffix('.json')), seed._inventory(parent)) != parent_before
                             or _lineage(session, plan, claimed=True) != lineage):
        _fail('comparison_seed_changed')
    from .comparison_repair import history
    repair_history = history(goal, facts)
    if dependencies_for(plan, root) != dependencies:
        _fail('comparison_seed_changed')
    result = {'queries': deepcopy(goal['queries']), 'active_ids': list(facts['active_ids']),
              'sources': sources, 'evidence_version': version, 'repair_history': repair_history}
    cache[key] = deepcopy(result)
    if memo is None:
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return result


def create_manifest(run, source_ids=None):
    try:
        root = seed._run_root(run)
        frozen = _freeze(root)
        verified = _verified(root, frozen)
        ids = deepcopy(verified['active_ids'] if source_ids is None else source_ids)
        if (type(ids) is not list or not ids or any(type(sid) is not str for sid in ids)
                or len(set(ids)) != len(ids)):
            _fail()
        manifest = validate_manifest({'version': 4, **frozen, 'queries': verified['queries'],
            'source_ids': ids, 'sources': seed._selected(verified, ids)})
        if _freeze(root) != frozen:
            _fail('comparison_seed_changed')
        return manifest
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def _load_verified(manifest):
    try:
        manifest = validate_manifest(manifest)
        root = seed._run_root(manifest['run'])
        frozen = _freeze(root)
        if any(manifest[k] != v for k, v in frozen.items()):
            _fail('comparison_seed_changed')
        verified = _verified(root, frozen)
        if (manifest['queries'] != verified['queries']
                or manifest['sources'] != seed._selected(verified, manifest['source_ids'])
                or _freeze(root) != frozen):
            _fail('comparison_seed_changed')
        return manifest, verified
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def load_seed(manifest):
    if type(manifest) is dict and manifest.get('version') == 5:
        from .comparison_compact_seed import load_seed as load_compact
        return load_compact(manifest)
    manifest, verified = _load_verified(manifest)
    return deepcopy([verified['sources'][sid] for sid in manifest['source_ids']])


def load_repair_history(manifest):
    """Return consumed work only after the same frozen-byte validation as captures."""
    if type(manifest) is dict and manifest.get('version') == 5:
        from .comparison_compact_seed import load_repair_history as load_compact
        return load_compact(manifest)
    _, verified = _load_verified(manifest)
    return deepcopy(verified['repair_history'])


def load_bundle(manifest):
    """One complete byte/lifecycle verification, with two independent copies."""
    if type(manifest) is dict and manifest.get('version') == 5:
        from .comparison_compact_seed import load_bundle as load_compact
        return load_compact(manifest)
    manifest, verified = _load_verified(manifest)
    return deepcopy(([verified['sources'][sid] for sid in manifest['source_ids']],
                     verified['repair_history']))
