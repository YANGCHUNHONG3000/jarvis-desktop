"""Read-only import of a completed first v4 comparison recovery.

The recovery's original research seed and interrupted parent are replayed on
every load. Its former comparison decision is never imported into the new goal.
"""
from copy import deepcopy
import time

from .computer import ComputerError, read_record, safe_root
from .computer_goal_adapters import TASK_BASE
from . import comparison_seed as seed


def _fail(code='comparison_seed_invalid'):
    raise ComputerError(code)


def validate_manifest(value):
    return seed._validate_shape(value, version=3, require_lifecycle=True)


def _verified(root, frozen):
    from .computer import ComputerSession
    from .computer_agent_tools import _load
    from .comparison_crosssite import CrossSiteEnvironment
    from .research_recovery import _lineage, _plan

    plan = _plan(None, root)
    goal, meta = plan['goal'], plan.get('recovery')
    comparison = goal.get('comparison', {})
    if (type(meta) is not dict or meta.get('version') != 2
            or meta.get('kind') != 'research' or type(meta.get('depth')) is not int
            or meta['depth'] != 1 or comparison.get('evidence_version') != 4
            or type(comparison.get('seed')) is not dict
            or comparison['seed'].get('version') != 2
            or (root / 'continuation-claim.json').exists()):
        _fail()
    parent = seed._run_root(meta['parent_run'])
    if parent == root or parent.parent != root.parent:
        _fail()
    parent_plan = _plan(None, parent)
    if ('recovery' in parent_plan or parent_plan['goal'] != goal
            or parent_plan['goal'].get('comparison', {}).get('evidence_version') != 4
            or parent_plan['goal']['comparison']['seed']['version'] != 2):
        _fail()
    # The production loader verifies the original research receipts, including
    # every byte of its native artifacts. No cache can skip its frozen checks.
    seed.load_seed(comparison['seed'])
    # An interrupted parent has no required Office artifacts, so the completed
    # run snapshot helper cannot represent it. Lineage validates its native
    # outputs; this bounded byte snapshot catches concurrent parent changes.
    parent_before = (seed._hash(parent.with_suffix('.json')), seed._inventory(parent))
    session = ComputerSession(root.parent.parent)
    lineage = _lineage(session, plan, claimed=True)
    if lineage['depth'] != 1 or not lineage['records']:
        _fail()
    env = object.__new__(CrossSiteEnvironment)
    env.__dict__.update(goal=deepcopy(goal), run=root,
                        stop=lambda: False, deadline=float('inf'), clock=time.monotonic,
                        search=None, browser=None, office_execute=None,
                        task_base=safe_root(TASK_BASE), recovery_validator=lambda: lineage,
                        comparison_select=None, comparison_review=None, record_origins={})
    facts = env._facts()
    env._facts = lambda: facts
    observed = env.observe_final()
    offset = len(lineage['records'])
    directories = sorted(root.glob('step-*'))
    if (not observed['checks']['goal_met'] or facts['office_blocked']
            or not 1 <= len(directories) <= 12
            or offset + len(directories) > 12
            or len(facts['records']) != offset + len(directories)
            or facts['records'][:offset] != lineage['records']):
        _fail()
    origins = {**lineage['origins'], **{
        record_hash: {'run': str(root), 'step': str(root / f'step-{number:02}')}
        for number, (_, record_hash) in enumerate(facts['records'][offset:], 1)}}
    if env.record_origins != origins:
        _fail()
    attempts = []
    for number, step in enumerate(directories, 1):
        if step.name != f'step-{number:02}' or not step.is_dir():
            _fail()
        intent, intent_hash = _load(step / 'environment-intent.json')
        record, record_hash = _load(step / 'environment-result.json')
        chosen = read_record(step / 'intent.json')
        verification = read_record(step / 'verification.json')
        if (chosen.get('action') != intent.get('action')
                or record.get('action') != intent.get('action')
                or record.get('intent_sha256') != intent_hash
                or record.get('result') != verification
                or type(verification.get('ok')) is not bool
                or verification.get('state') not in {'verified', 'failed'}
                or verification['ok'] != (verification['state'] == 'verified')
                or facts['records'][offset + number - 1] != (record, record_hash)):
            _fail()
        attempts.append({'step': number, 'action': record['action'], 'result': verification})
    outcome = read_record(root / 'outcome.json')
    if (outcome.get('ok') is not True or outcome.get('state') != 'verified'
            or outcome.get('code') != 'objective_verified'
            or outcome.get('engine') != 'observed_agent'
            or outcome.get('mode') != 'adapters' or outcome.get('run') != str(root)
            or type(outcome.get('actions')) is not int or outcome['actions'] != len(attempts)
            or outcome.get('attempts') != attempts
            or outcome.get('completed') != [row for row in attempts if row['result']['ok']]
            or outcome.get('artifacts') != observed['artifacts']
            or type(outcome.get('source_reads')) is not int
            or outcome['source_reads'] != observed['source_reads']
            or outcome.get('failures') != observed['failures']
            or outcome.get('outputs') != {}
            or outcome.get('recovery') != meta):
        _fail()
    sources = {item['id']: (item, facts['reads'][item['id']])
               for item in facts['candidates'] if item['id'] in facts['active_ids']
               and item['id'] in facts['reads'] and facts['reads'][item['id']]['result']['ok']}
    if (seed._freeze(root) != frozen
            or (seed._hash(parent.with_suffix('.json')), seed._inventory(parent)) != parent_before
            or _lineage(session, plan, claimed=True) != lineage):
        _fail('comparison_seed_changed')
    return {'queries': deepcopy(goal['queries']), 'active_ids': list(facts['active_ids']),
            'sources': sources, 'evidence_version': 4}


def create_manifest(run, source_ids=None):
    try:
        root = seed._run_root(run)
        frozen = seed._freeze(root)
        verified = _verified(root, frozen)
        ids = deepcopy(verified['active_ids'] if source_ids is None else source_ids)
        if (type(ids) is not list or not 1 <= len(ids) <= 6
                or any(type(sid) is not str for sid in ids) or len(set(ids)) != len(ids)):
            _fail()
        manifest = validate_manifest({'version': 3, **frozen,
                                      'queries': verified['queries'],
                                      'source_ids': ids,
                                      'sources': seed._selected(verified, ids)})
        if seed._freeze(root) != frozen:
            _fail('comparison_seed_changed')
        return manifest
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def load_seed(manifest):
    try:
        manifest = validate_manifest(manifest)
        root = seed._run_root(manifest['run'])
        frozen = seed._freeze(root)
        if any(manifest[name] != value for name, value in frozen.items()):
            _fail('comparison_seed_changed')
        verified = _verified(root, frozen)
        if (manifest['queries'] != verified['queries']
                or manifest['sources'] != seed._selected(verified, manifest['source_ids'])
                or seed._freeze(root) != frozen):
            _fail('comparison_seed_changed')
        return deepcopy([verified['sources'][sid] for sid in manifest['source_ids']])
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()
