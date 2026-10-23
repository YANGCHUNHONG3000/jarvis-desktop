"""Read-only, hash-bound imports of historical v3/v4 source captures.

A seed carries source receipts, never a previous comparison verdict or execution
authority. Shape validation is pure; loading verifies the frozen local evidence
before returning any text to the new task.
"""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import time

from . import computer
from .computer import ComputerError, digest, read_record, safe_root
from .computer_goal_adapters import TASK_BASE

MAX_FILES = 256
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 100_000
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{24}\Z')
_CACHE = OrderedDict()


def _fail():
    raise ComputerError('comparison_seed_invalid')


def _plain(value, limit):
    return (type(value) is str and 0 < len(value) <= limit
            and bool(value.strip()) and not any(
                ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _hash_value(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _inventory_shape(value):
    if type(value) is not dict or not 1 <= len(value) <= MAX_FILES:
        _fail()
    for name, hashed in value.items():
        if (not _plain(name, 1000) or '\\' in name or ':' in name
                or PurePosixPath(name).is_absolute()
                or any(part in {'', '.', '..'} for part in name.split('/'))
                or not _hash_value(hashed)):
            _fail()


def validate_manifest(value):
    """Validate only bounded JSON shape; do not resolve or read any path."""
    if type(value) is dict and type(value.get('version')) is int and value['version'] == 5:
        from .comparison_compact_seed import validate_manifest as validate_compact
        return validate_compact(value)
    if type(value) is dict and type(value.get('version')) is int and value['version'] == 2:
        from .comparison_research_seed import validate_manifest as validate_research
        return validate_research(value)
    if type(value) is dict and type(value.get('version')) is int and value['version'] == 3:
        from .comparison_recovered_seed import validate_manifest as validate_recovered
        return validate_recovered(value)
    if type(value) is dict and type(value.get('version')) is int and value['version'] == 4:
        from .comparison_failed_seed import validate_manifest as validate_failed
        return validate_failed(value)
    return _validate_shape(value, version=1, require_lifecycle=True)


def _validate_shape(value, *, version, require_lifecycle, empty_outputs=False):
    """Shared pure shape checks; producer-specific ownership is checked on load."""
    fields = {'version', 'run', 'queries', 'plan_sha256', 'outcome_sha256',
              'inventory', 'outputs', 'source_ids', 'sources'}
    if (type(value) is not dict or set(value) != fields
            or type(value['version']) is not int or value['version'] != version
            or not _plain(value['run'], 2000)
            or not _hash_value(value['plan_sha256'])
            or not _hash_value(value['outcome_sha256'])):
        _fail()
    queries = value['queries']
    if (type(queries) is not list or not 1 <= len(queries) <= 2
            or any(not _plain(query, 500) for query in queries)
            or len({q.strip().casefold() for q in queries}) != len(queries)):
        _fail()
    _inventory_shape(value['inventory'])
    required = {'claim.json', 'environment-goal.json'}
    if require_lifecycle:
        required.update({'owner.json', 'owner.lease'})
    if (value['inventory'].get('outcome.json') != value['outcome_sha256']
            or not required <= value['inventory'].keys()):
        _fail()
    if (type(value['outputs']) is not dict or
            (value['outputs'] != {} if empty_outputs else not 1 <= len(value['outputs']) <= 2)):
        _fail()
    for path, inventory in value['outputs'].items():
        if not _plain(path, 2000):
            _fail()
        _inventory_shape(inventory)
    ids, sources = value['source_ids'], value['sources']
    if (type(ids) is not list or not 1 <= len(ids) <= 6
            or any(type(sid) is not str or not _ID.fullmatch(sid) for sid in ids)
            or len(set(ids)) != len(ids) or type(sources) is not list or len(sources) != len(ids)):
        _fail()
    for sid, source in zip(ids, sources):
        if (type(source) is not dict or set(source) != {
                'id', 'query_index', 'record_sha256', 'text_sha256'}
                or source['id'] != sid or type(source['query_index']) is not int
                or not 0 <= source['query_index'] < len(queries)
                or not _hash_value(source['record_sha256'])
                or not _hash_value(source['text_sha256'])):
            _fail()
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_MANIFEST_BYTES:
        _fail()
    return deepcopy(value)


def _run_root(value):
    root, base = safe_root(value), safe_root(computer.BASE)
    if (root.parent.name != 'goals' or root.parent.parent.parent != base
            or not root.is_dir() or not root.name.endswith('.run')
            or not _SHA.fullmatch(root.name[:-4])):
        _fail()
    return root


def _hash(path):
    path = safe_root(path)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        _fail()
    hashed, size = sha256(), 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(block)
            if size > MAX_FILE_BYTES:
                _fail()
            hashed.update(block)
    return hashed.hexdigest()


def _inventory(root, *, exclude=()):
    root = safe_root(root)
    files, total, entries = {}, 0, 0
    # Bound directories as well as files; links never become a traversal escape.
    for path in root.rglob('*'):
        entries += 1
        if entries > MAX_FILES * 2:
            _fail()
        safe_root(path)
        if not path.is_file() or path.relative_to(root).as_posix() in exclude:
            continue
        total += path.stat().st_size
        if len(files) >= MAX_FILES or total > MAX_TREE_BYTES:
            _fail()
        files[path.relative_to(root).as_posix()] = _hash(path)
    return files


def _freeze(root):
    """Rehash every run and native-output byte, including cache-hit loads."""
    from .run_lifecycle import assert_inactive
    assert_inactive(root)
    return _snapshot(root)


def _snapshot(root):
    """Bounded byte snapshot; caller must first check its producer ownership."""
    inventory = _inventory(root)
    plan_hash = _hash(root.with_suffix('.json'))
    outcome = read_record(root / 'outcome.json')
    artifacts = outcome.get('artifacts')
    if type(artifacts) is not list or not 1 <= len(artifacts) <= 2:
        _fail()
    outputs = {}
    for artifact in artifacts:
        if type(artifact) is not dict:
            _fail()
        task = safe_root(artifact['task_root'])
        output = safe_root(artifact['path'])
        if task.parent != safe_root(TASK_BASE) or not output.is_relative_to(task):
            _fail()
        # Match existing recovery's exact native Office sibling-lock exception.
        lock = output.with_name('~$' + output.name).relative_to(task).as_posix()
        outputs[str(task)] = _inventory(task, exclude=(lock,))
    return {'run': str(root), 'plan_sha256': plan_hash,
            'outcome_sha256': inventory.get('outcome.json'),
            'inventory': inventory, 'outputs': outputs}


def _verified(root, frozen):
    """Run the original production receipt validators once per frozen snapshot."""
    from .computer_agent_tools import _load
    from .comparison_completion import CompletionEnvironment
    from .research_recovery import _plan
    plan = _plan(None, root)
    version = plan['goal'].get('comparison', {}).get('evidence_version')
    if 'recovery' in plan or version not in {3, 4}:
        _fail()
    comparison = plan['goal'].get('comparison', {})
    imported = None
    if 'capture_import' in comparison:
        from .comparison_capture_import import fingerprint
        imported = fingerprint(comparison['capture_import'], comparison.get('seed'))

    def check_import():
        if imported is not None and fingerprint(comparison['capture_import'], comparison.get('seed')) != imported:
            raise ComputerError('comparison_seed_changed')
    environment_type = CompletionEnvironment
    if version == 4:
        from .comparison_crosssite import CrossSiteEnvironment
        environment_type = CrossSiteEnvironment
        ancestor = plan['goal']['comparison']['seed']
        ancestor_root = _run_root(ancestor['run'])
        ancestor_plan = read_record(ancestor_root.with_suffix('.json'))
        # Only one v4 import layer is supported. Inspect the parent's declared
        # version before loading, so self/cyclic/deeper imports cannot recurse.
        if (ancestor_root == root or 'recovery' in ancestor_plan
                or ancestor_plan.get('goal', {}).get('comparison', {}).get('evidence_version') != 3):
            _fail()
        # A v4 cache entry depends on its v3 ancestor too. Rehash and validate
        # that original seed before returning any cached child source records.
        load_seed(ancestor)
    key = digest(frozen) if imported is None else digest({'run': frozen, 'capture_import': imported})
    if key in _CACHE:
        result = deepcopy(_CACHE[key])
        check_import()
        _CACHE.move_to_end(key)
        return result
    # AgentEnvironment.__init__ can create environment-goal.json when absent.
    # Reconstruct its read-only fields instead, so even concurrent removal can
    # only fail verification and can never cause a write to the historical run.
    env = object.__new__(environment_type)
    env.__dict__.update(goal=deepcopy(plan['goal']), run=root,
                        stop=lambda: False, deadline=float('inf'), clock=time.monotonic,
                        search=None, browser=None, office_execute=None,
                        task_base=safe_root(TASK_BASE), recovery_validator=None,
                        comparison_select=None, comparison_review=None, record_origins={})
    facts = env._facts()
    # Formatting the final observation reuses this exact full verification.
    env._facts = lambda: facts
    observation = env.observe_final()
    if not observation['checks']['goal_met'] or facts['office_blocked']:
        _fail()
    attempts = []
    directories = sorted(root.glob('step-*'))
    if not 1 <= len(directories) <= 12:
        _fail()
    for number, step in enumerate(directories, 1):
        if step.name != f'step-{number:02}' or not step.is_dir():
            _fail()
        intent, intent_hash = _load(step / 'environment-intent.json')
        record, record_hash = _load(step / 'environment-result.json')
        chosen, verification = read_record(step / 'intent.json'), read_record(step / 'verification.json')
        if (chosen.get('action') != intent.get('action')
                or record.get('action') != intent.get('action')
                or record.get('intent_sha256') != intent_hash
                or record.get('result') != verification
                or type(verification.get('ok')) is not bool
                or verification.get('state') not in {'verified', 'failed'}
                or verification['ok'] != (verification['state'] == 'verified')
                or facts['records'][number - 1] != (record, record_hash)):
            _fail()
        attempts.append({'step': number, 'action': record['action'], 'result': verification})
    outcome = read_record(root / 'outcome.json')
    if (outcome.get('ok') is not True or outcome.get('state') != 'verified'
            or outcome.get('code') != 'objective_verified' or outcome.get('engine') != 'observed_agent'
            or outcome.get('mode') != 'adapters' or outcome.get('run') != str(root)
            or type(outcome.get('actions')) is not int or outcome['actions'] != len(attempts)
            or len(facts['records']) != len(attempts) or outcome.get('attempts') != attempts
            or outcome.get('completed') != [row for row in attempts if row['result']['ok']]
            or outcome.get('artifacts') != observation['artifacts']
            or type(outcome.get('source_reads')) is not int
            or outcome['source_reads'] != observation['source_reads']
            or outcome.get('failures') != observation['failures'] or outcome.get('outputs') != {}
            or 'recovery' in outcome):
        _fail()
    sources = {item['id']: (item, facts['reads'][item['id']]) for item in facts['candidates']
               if item['id'] in facts['reads'] and facts['reads'][item['id']]['result']['ok']}
    result = {'queries': plan['goal']['queries'], 'active_ids': facts['active_ids'],
              'sources': sources, 'evidence_version': version}
    if _freeze(root) != frozen:
        raise ComputerError('comparison_seed_changed')
    check_import()
    _CACHE[key] = deepcopy(result)
    while len(_CACHE) > 4:
        _CACHE.popitem(last=False)
    return result


def _selected(verified, ids):
    if len(ids) > (6 if verified['evidence_version'] == 4 else 4):
        _fail()
    selected = []
    for sid in ids:
        if sid not in verified['sources']:
            _fail()
        candidate, record = verified['sources'][sid]
        selected.append({'id': sid, 'query_index': candidate['query_index'],
                         'record_sha256': digest(record), 'text_sha256': record['observation']['sha256']})
    return selected


def create_manifest(run, source_ids=None):
    """Freeze selected captures from a completed v3/v4 run, without claiming it."""
    try:
        root = _run_root(run)
        plan = read_record(root.with_suffix('.json'))
        if type(plan.get('goal')) is dict and 'comparison' not in plan['goal']:
            from .comparison_research_seed import create_manifest as create_research
            return create_research(root, source_ids)
        if read_record(root / 'outcome.json').get('ok') is False:
            from .comparison_failed_seed import create_manifest as create_failed
            return create_failed(root, source_ids)
        if 'recovery' in plan:
            from .comparison_recovered_seed import create_manifest as create_recovered
            return create_recovered(root, source_ids)
        frozen = _freeze(root)
        verified = _verified(root, frozen)
        ids = deepcopy(verified['active_ids'] if source_ids is None else source_ids)
        if (type(ids) is not list or not 1 <= len(ids) <= 6
                or any(type(sid) is not str for sid in ids) or len(set(ids)) != len(ids)):
            _fail()
        manifest = validate_manifest({'version': 1, **frozen, 'queries': verified['queries'],
                                      'source_ids': ids, 'sources': _selected(verified, ids)})
        if _freeze(root) != frozen:
            raise ComputerError('comparison_seed_changed')
        return manifest
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()


def load_seed(manifest):
    """Return original candidate/read pairs only after local immutable replay."""
    try:
        manifest = validate_manifest(manifest)
        if manifest['version'] == 5:
            from .comparison_compact_seed import load_seed as load_compact
            return load_compact(manifest)
        if manifest['version'] == 4:
            from .comparison_failed_seed import load_seed as load_failed
            return load_failed(manifest)
        if manifest['version'] == 2:
            from .comparison_research_seed import load_seed as load_research
            return load_research(manifest)
        if manifest['version'] == 3:
            from .comparison_recovered_seed import load_seed as load_recovered
            return load_recovered(manifest)
        root = _run_root(manifest['run'])
        frozen = _freeze(root)
        if any(manifest[name] != value for name, value in frozen.items()):
            raise ComputerError('comparison_seed_changed')
        verified = _verified(root, frozen)
        if (manifest['queries'] != verified['queries']
                or manifest['sources'] != _selected(verified, manifest['source_ids'])):
            raise ComputerError('comparison_seed_changed')
        if _freeze(root) != frozen:
            raise ComputerError('comparison_seed_changed')
        return deepcopy([verified['sources'][sid] for sid in manifest['source_ids']])
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        _fail()
