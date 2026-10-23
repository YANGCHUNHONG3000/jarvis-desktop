"""Read-only import of completed, original public research captures.

Version two keeps the comparison seed's frozen manifest shape, but has its own
producer verifier. It returns source receipts, never an earlier Office result or
an earlier judgment about the new comparison dimensions.
"""
from copy import deepcopy
import math
import time

from .computer import ComputerError, digest, read_record, safe_root
from .computer_goal_adapters import TASK_BASE
from . import comparison_seed as seed


def _fail(code='comparison_seed_invalid'):
    raise ComputerError(code)


def validate_manifest(value):
    """Validate bounded JSON shape without touching the filesystem."""
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 2:
        _fail()
    seed._validate_shape(value, version=2, require_lifecycle=False)
    if len(value['source_ids']) > 2:
        _fail()
    _selection_sufficient(value['queries'], value['sources'])
    return deepcopy(value)


def _selection_sufficient(queries, sources):
    # A lone page can start a comparison only when a second original topic can
    # still be searched by the v4 frontier. One-topic research would stall.
    if len(sources) == 1 and (len(queries) != 2 or sources[0]['query_index'] not in (0, 1)):
        _fail('comparison_seed_insufficient')
    if not 1 <= len(sources) <= 2:
        _fail('comparison_seed_insufficient')


def _freeze(root):
    """Freeze current runs via OS lease; accept only the exact old claim form."""
    from .research_recovery import _plan

    if (root / 'continuation-claim.json').exists():
        _fail()
    claim = read_record(root / 'claim.json')
    if ((root / 'owner.json').exists() or (root / 'owner.lease').exists()
            or type(claim) is dict and 'lifecycle' in claim):
        return seed._freeze(root)
    plan = _plan(None, root)
    approved_at = claim.get('at') if type(claim) is dict else None
    prepared_at = plan.get('prepared_at')
    if (type(claim) is not dict or set(claim) != {'approval', 'mode', 'at'}
            or claim['approval'] != root.name[:-4] or claim['mode'] != 'agent'
            or type(approved_at) not in (int, float) or not math.isfinite(approved_at)
            or type(prepared_at) not in (int, float) or not math.isfinite(prepared_at)
            or approved_at < prepared_at
            or approved_at > prepared_at + plan['max_seconds']
            or 'recovery' in plan):
        _fail()
    return seed._snapshot(root)


def _verified(root, frozen):
    """Replay the original production validators without constructing a writer."""
    from .computer_agent_tools import AgentEnvironment, _load
    from .computer import ComputerSession
    from .research_recovery import _lineage, _plan

    plan = _plan(None, root)
    if 'comparison' in plan['goal'] or plan['goal'].get('kind') == 'interactive':
        _fail()
    lineage = {'records': [], 'origins': {}, 'depth': 0, 'hashes': []}
    session = None
    if 'recovery' in plan:
        metadata = plan['recovery']
        if (type(metadata) is not dict or metadata.get('depth') != 1
                or metadata.get('version') != 2 or metadata.get('kind') != 'research'
                or not {'owner.json', 'owner.lease'} <= frozen['inventory'].keys()):
            _fail()
        parent_root = seed._run_root(metadata['parent_run'])
        if parent_root.parent != root.parent or parent_root == root:
            _fail()
        parent_plan = _plan(None, parent_root)
        if ('recovery' in parent_plan or 'comparison' in parent_plan['goal']
                or parent_plan['goal'].get('kind') == 'interactive'):
            _fail()
        session = ComputerSession(root.parent.parent)
        lineage = _lineage(session, plan, claimed=True)
        if lineage['depth'] != 1:
            _fail()
    env = object.__new__(AgentEnvironment)
    env.__dict__.update(goal=deepcopy(plan['goal']), run=root,
                        stop=lambda: False, deadline=float('inf'), clock=time.monotonic,
                        search=None, browser=None, office_execute=None,
                        task_base=safe_root(TASK_BASE), recovery_validator=lambda: lineage,
                        comparison_select=None, comparison_review=None, record_origins={})
    facts = env._facts()
    env._facts = lambda: facts
    observation = env.observe_final()
    if not observation['checks']['goal_met'] or facts['office_blocked'] or facts['comparison'] is not None:
        _fail()

    directories = sorted(root.glob('step-*'))
    offset = len(lineage['records'])
    if (not 1 <= len(directories) <= 12
            or len(directories) != len(facts['records']) - offset
            or facts['records'][:offset] != lineage['records']
            or env.record_origins != {**lineage['origins'], **{
                record_hash: {'run': str(root), 'step': str(root / f'step-{number:02}')}
                for number, (_, record_hash) in enumerate(facts['records'][offset:], 1)}}):
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
            or len(facts['records']) != offset + len(attempts) or outcome.get('attempts') != attempts
            or outcome.get('completed') != [row for row in attempts if row['result']['ok']]
            or outcome.get('artifacts') != observation['artifacts']
            or type(outcome.get('source_reads')) is not int
            or outcome['source_reads'] != observation['source_reads']
            or outcome.get('failures') != observation['failures']
            or outcome.get('outputs') != {}
            or ('recovery' in plan and outcome.get('recovery') != plan['recovery'])
            or ('recovery' not in plan and 'recovery' in outcome)):
        _fail()
    sources = {item['id']: (item, record) for item, record in facts['accepted']}
    result = {'queries': deepcopy(plan['goal']['queries']), 'sources': sources,
              'active_ids': [item['id'] for item, _ in facts['accepted']], 'evidence_version': 3}
    if _freeze(root) != frozen:
        _fail('comparison_seed_changed')
    if session is not None and _lineage(session, plan, claimed=True) != lineage:
        _fail('comparison_seed_changed')
    return result


def create_manifest(run, source_ids=None):
    """Freeze selected verified research reads; never claim the old run."""
    try:
        root = seed._run_root(run)
        frozen = _freeze(root)
        verified = _verified(root, frozen)
        ids = deepcopy(verified['active_ids'] if source_ids is None else source_ids)
        if (type(ids) is not list or not 1 <= len(ids) <= 2
                or any(type(sid) is not str for sid in ids) or len(set(ids)) != len(ids)):
            _fail()
        sources = seed._selected(verified, ids)
        _selection_sufficient(verified['queries'], sources)
        manifest = validate_manifest({'version': 2, **frozen, 'queries': verified['queries'],
                                      'source_ids': ids, 'sources': sources})
        if _freeze(root) != frozen:
            _fail('comparison_seed_changed')
        return manifest
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def load_seed(manifest):
    """Rehash run and native outputs, replay receipts, then return original pairs."""
    try:
        manifest = validate_manifest(manifest)
        root = seed._run_root(manifest['run'])
        frozen = _freeze(root)
        if any(manifest[name] != value for name, value in frozen.items()):
            _fail('comparison_seed_changed')
        verified = _verified(root, frozen)
        if (manifest['queries'] != verified['queries']
                or manifest['sources'] != seed._selected(verified, manifest['source_ids'])):
            _fail('comparison_seed_changed')
        if _freeze(root) != frozen:
            _fail('comparison_seed_changed')
        return deepcopy([verified['sources'][sid] for sid in manifest['source_ids']])
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()
