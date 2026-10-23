"""Single-use research continuations from acknowledged captured evidence."""
from copy import deepcopy
from collections import OrderedDict
from pathlib import Path
import re
import time

from .computer import ComputerError, digest, read_record, write_new, safe_root
from .computer_agent_tools import AgentEnvironment, _load
from .computer_goal_adapters import TASK_BASE

MAX_DEPTH = 4
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_VERIFIED = OrderedDict()
_CACHE_LIMIT = 4


def _verification_snapshot(session, root, *, child, allow_unclaimed):
    """Rehash every bounded dependency; snapshots never establish eligibility.

    Only a successful production replay can populate the cache. In particular,
    neither timestamps nor the seed loaders' own caches substitute for these
    bytes, current leases, continuation claims, or native output inventories.
    """
    from .computer import ComputerSession
    from .interaction_recovery import _run_root, _claim, _hash, _inventory
    from .run_lifecycle import assert_inactive
    from .comparison_seed import _run_root as seed_root
    snapshots, visiting, node_count = {}, set(), 0

    def tree(target, *, exclude=()):
        files = _inventory(target, exclude=exclude)
        directories = []
        for path in target.rglob('*'):
            safe_root(path)
            if path.is_dir():
                directories.append(path.relative_to(target).as_posix())
                if len(directories) > 256:
                    raise ComputerError('recovery_evidence_limit')
        return {'files': files, 'directories': sorted(directories)}

    def visit(target, owner, *, research, expected_child=None, unclaimed=False):
        nonlocal node_count
        target = _run_root(owner, target) if research else seed_root(target)
        if research:
            _claim(target, expected_child, unclaimed)
        name = str(target) + ('|research' if research else '|seed')
        if str(target) in visiting:
            raise ComputerError('recovery_lineage_depth')
        if name in snapshots:
            return
        if node_count >= 32:
            raise ComputerError('recovery_evidence_limit')
        node_count += 1
        claim = read_record(target / 'claim.json')
        if ('lifecycle' in claim or (target / 'owner.json').exists()
                or (target / 'owner.lease').exists()):
            assert_inactive(target)
        plan = read_record(target.with_suffix('.json'))
        outcome = read_record(target / 'outcome.json')
        artifacts = outcome.get('artifacts')
        if type(artifacts) is not list or len(artifacts) > 2:
            raise ComputerError('recovery_evidence_changed')
        outputs = {}
        for artifact in artifacts:
            task, output = safe_root(artifact['task_root']), safe_root(artifact['path'])
            lock = output.with_name('~$' + output.name).relative_to(task).as_posix()
            outputs[str(task)] = tree(task, exclude=(lock,))
        continuation = target / 'continuation-claim.json'
        snapshots[name] = {'plan': _hash(target.with_suffix('.json')),
            'run': tree(target, exclude=('continuation-claim.json',) if research else ()),
            'claim': read_record(continuation) if research and continuation.exists() else None,
            'outputs': outputs}
        comparison = plan['goal'].get('comparison', {})
        if 'capture_import' in comparison:
            from .comparison_capture_import import fingerprint
            snapshots[name]['capture_import'] = fingerprint(comparison['capture_import'], comparison.get('seed'))
        visiting.add(str(target))
        meta = plan.get('recovery')
        if 'recovery' in plan:
            if type(meta) is not dict:
                raise ComputerError('recovery_metadata_invalid')
            visit(meta['parent_run'], owner, research=True,
                  expected_child=plan['sha256'])
        ancestor = plan['goal'].get('comparison', {}).get('seed')
        if ancestor is not None:
            if type(ancestor) is not dict or ancestor.get('version') not in {1, 2, 3, 4, 5}:
                raise ComputerError('recovery_evidence_changed')
            imported = seed_root(ancestor['run'])
            if ancestor['version'] == 5:
                from .comparison_compact_seed import validate_manifest, _graph
                manifest = validate_manifest(ancestor)
                name = str(imported) + '|compact'
                if name not in snapshots:
                    graph = _graph(imported)
                    if node_count + len(graph['nodes']) > 32:
                        raise ComputerError('recovery_evidence_limit')
                    node_count += len(graph['nodes'])
                    snapshots[name] = graph
                if snapshots[name] != manifest['dependency_graph']:
                    raise ComputerError('recovery_evidence_changed')
            else:
                visit(imported, ComputerSession(imported.parent.parent), research=False)
        visiting.remove(str(target))

    visit(root, session, research=True, expected_child=child, unclaimed=allow_unclaimed)
    return snapshots


def _plan(session, run):
    from .computer_agent import MAX_ACTIONS, MAX_SECONDS, validate_goal
    plan = read_record(run.with_suffix('.json'))
    fields = {'schema', 'mode', 'instruction', 'goal', 'max_actions', 'max_seconds', 'prepared_at', 'sha256'}
    if (type(plan) is not dict or set(plan) not in (fields, fields | {'recovery'})
            or plan['schema'] != 3 or plan['mode'] != 'agent'
            or plan['sha256'] != run.name[:-4]
            or digest({k: v for k, v in plan.items() if k != 'sha256'}) != plan['sha256']
            or plan['max_actions'] != MAX_ACTIONS or plan['max_seconds'] != MAX_SECONDS):
        raise ComputerError('recovery_plan_changed')
    goal = validate_goal(plan['goal'])
    if goal.get('kind') == 'interactive':
        raise ComputerError('recovery_goal_changed')
    if read_record(run / 'claim.json').get('approval') != plan['sha256']:
        raise ComputerError('recovery_claim_changed')
    if _load(run / 'environment-goal.json')[0] != goal:
        raise ComputerError('recovery_goal_changed')
    return plan


def _lineage(session, plan, *, claimed, seen=()):
    if 'recovery' not in plan:
        return {'records': [], 'origins': {}, 'depth': 0, 'hashes': []}
    meta = plan['recovery']
    if (type(meta) is not dict or set(meta) != {
            'version', 'kind', 'parent_run', 'parent_evidence_sha256', 'depth',
            'completed_edits', 'remaining_edits'} or meta['version'] != 2 or meta['kind'] != 'research'
            or type(meta['depth']) is not int or not 1 <= meta['depth'] <= MAX_DEPTH
            or not _SHA.fullmatch(str(meta['parent_evidence_sha256']))):
        raise ComputerError('recovery_metadata_invalid')
    parent = _inspect(session, meta['parent_run'], child=plan['sha256'],
                      allow_unclaimed=not claimed, seen=seen)
    if (parent['evidence_sha256'] != meta['parent_evidence_sha256']
            or parent['goal'] != plan['goal'] or parent['instruction'] != plan['instruction']
            or parent['depth'] + 1 != meta['depth']
            or parent['done'] != meta['completed_edits'] or parent['remaining'] != meta['remaining_edits']):
        raise ComputerError('recovery_lineage_changed')
    return {'records': parent['records'], 'origins': parent['origins'],
            'depth': meta['depth'], 'hashes': [parent['evidence_sha256']]}


def _inspect(session, run, *, child=None, allow_unclaimed=False, seen=()):
    from .interaction_recovery import _run_root
    root = _run_root(session, run)
    if str(root) in seen or len(seen) > MAX_DEPTH:
        raise ComputerError('recovery_lineage_depth')
    try:
        before = _verification_snapshot(session, root, child=child, allow_unclaimed=allow_unclaimed)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        if isinstance(exc, ComputerError) and str(exc) in {
                'comparison_capture_import_changed', 'comparison_capture_import_invalid'}:
            raise
        # Unknown graph shapes retain the original full verification path and
        # its diagnostic; they never gain authority through this optimization.
        before = None
    key = (str(root), child, allow_unclaimed, tuple(seen), digest(before))
    if before is not None and key in _VERIFIED:
        result = deepcopy(_VERIFIED[key])
        if _verification_snapshot(session, root, child=child, allow_unclaimed=allow_unclaimed) != before:
            raise ComputerError('recovery_evidence_changed')
        _VERIFIED.move_to_end(key)
        return result
    result = _inspect_verified(session, root, child=child, allow_unclaimed=allow_unclaimed,
                               seen=seen, reuse_facts=before is not None)
    if before is not None:
        if _verification_snapshot(session, root, child=child, allow_unclaimed=allow_unclaimed) != before:
            raise ComputerError('recovery_evidence_changed')
        _VERIFIED[key] = deepcopy(result)
        _VERIFIED.move_to_end(key)
        while len(_VERIFIED) > _CACHE_LIMIT:
            _VERIFIED.popitem(last=False)
    return result


def _inspect_verified(session, run, *, child=None, allow_unclaimed=False, seen=(), reuse_facts=False):
    from .interaction_recovery import _run_root, _claim, _hash, _inventory
    root = _run_root(session, run)
    if str(root) in seen or len(seen) > MAX_DEPTH:
        raise ComputerError('recovery_lineage_depth')
    _claim(root, child, allow_unclaimed)
    plan = _plan(session, root)
    if ('lifecycle' in read_record(root/'claim.json')
            or (root/'owner.json').exists() or (root/'owner.lease').exists()):
        from .run_lifecycle import assert_inactive
        assert_inactive(root)
    outcome = read_record(root / 'outcome.json')
    if (outcome.get('ok') is not False or outcome.get('state') not in {'partial', 'stopped'}
            or outcome.get('engine') != 'observed_agent' or outcome.get('run') != str(root)):
        raise ComputerError('recovery_terminal_state_not_safe')
    seed = _lineage(session, plan, claimed=True, seen=(*seen, str(root)))
    records, origins, attempts = list(seed['records']), deepcopy(seed['origins']), []
    steps, pending, gaps = sorted(root.glob('step-*')), None, []
    for step in steps:
        if not re.fullmatch(r'step-(0[1-9]|1[0-2])', step.name) or not step.is_dir():
            raise ComputerError('recovery_step_invalid')
        if not (step / 'environment-intent.json').exists():
            if not (step / 'intent.json').exists():
                # Preserve the old pre-selection contract: an empty directory,
                # choices alone, or an uncommitted decision never dispatched.
                if any((step / name).exists() for name in ('environment-result.json', 'verification.json')):
                    raise ComputerError('recovery_evidence_incomplete')
                gaps.append(step)
                continue
            entries = list(step.iterdir())
            if (pending is not None or gaps or step != steps[-1]
                    or int(step.name[5:]) != len(attempts) + 1
                    or {entry.name for entry in entries} != {'choices.json', 'decision.json', 'intent.json'}
                    or any(not entry.is_file() for entry in entries)):
                raise ComputerError('recovery_evidence_incomplete')
            pending = step
            continue
        if not all((step / name).is_file() for name in ('environment-result.json', 'intent.json', 'verification.json')):
            raise ComputerError('recovery_dispatched_action_not_verified')
        intent, intent_sha = _load(step / 'environment-intent.json')
        record, record_sha = _load(step / 'environment-result.json')
        chosen = read_record(step / 'intent.json')
        verification = read_record(step / 'verification.json')
        action = intent['action']
        if (chosen.get('action') != action or record.get('action') != action
                or record.get('intent_sha256') != intent_sha or record.get('result') != verification
                or type(verification.get('ok')) is not bool
                or verification.get('state') not in {'verified', 'failed'}
                or verification['ok'] != (verification['state'] == 'verified')
                or action.get('kind') in {'spreadsheet', 'document'} and not verification['ok']):
            raise ComputerError('recovery_dispatched_action_not_verified')
        records.append((record, record_sha))
        origins[record_sha] = {'run': str(root), 'step': str(step)}
        attempts.append({'step': int(step.name[5:]), 'action': action, 'result': verification})
    bases = {str(safe_root(record['artifact']['task_root']).parent)
             for record, _ in records if record['action']['kind'] in {'spreadsheet', 'document'}}
    if len(bases) > 1:
        raise ComputerError('recovery_source_owner_invalid')
    # Reconstruction is read-only even if environment-goal.json disappears
    # concurrently; the ordinary constructor would recreate that receipt.
    env = AgentEnvironment.__new__(AgentEnvironment, plan['goal'])
    env.__dict__.update(goal=deepcopy(plan['goal']), run=root, stop=lambda: False,
        deadline=float('inf'), clock=time.monotonic, search=None, browser=None,
        office_execute=None, task_base=safe_root(next(iter(bases), TASK_BASE)),
        recovery_validator=lambda: seed, comparison_select=None, comparison_review=None,
        record_origins={})
    facts = env._facts()
    if reuse_facts:
        # This instance exists only for this synchronous inspection; entry and
        # exit snapshots still bind every dependency around the full replay.
        env._facts = lambda: facts
    if facts['records'] != records or env.record_origins != origins or facts['office_blocked']:
        raise ComputerError('recovery_evidence_changed')
    if (outcome.get('attempts') != attempts or outcome.get('completed') != [a for a in attempts if a['result']['ok']]
            or outcome.get('actions') != len(attempts) or outcome.get('artifacts') != facts['artifacts']
            or outcome.get('source_reads') != len(facts['accepted']) or outcome.get('failures') != facts['failures']):
        raise ComputerError('recovery_outcome_changed')
    v3 = plan['goal'].get('comparison', {}).get('evidence_version') in {3,4}
    ready = env._ready(facts['candidates'], facts['reads'])
    if v3:
        actions = env.actions(env.observe_final())
    elif ready:
        actions = ([{'kind':'compare'}] if 'comparison' in plan['goal'] and facts['comparison'] is None
                   else [{'kind': kind} for kind in plan['goal']['outputs'] if kind not in facts['done']])
    elif not facts['office_started']:
        actions = ([{'kind': 'search', 'query_index': i} for i in range(len(plan['goal']['queries']))
                    if i not in facts['queries']] +
                   [{'kind': 'read', 'candidate_id': item['id']} for item in facts['candidates']
                    if item['id'] not in facts['reads']])
    else:
        actions = []
    if pending is not None:
        from .computer_agent import checked_observation
        observation = checked_observation(env.observe_final())
        actions = env.actions(observation)
        _validate_pre_dispatch(root, pending, observation, actions)
    if not actions:
        raise ComputerError('recovery_nothing_remaining')
    done = [_label(row['action']) for row, _ in records]
    remaining = [_label(action) for action in actions]
    # Pending later outputs also appear in approval even when sources are not ready.
    if 'comparison' in plan['goal'] and facts['comparison'] is None and 'compare' not in remaining:
        remaining.append('compare')
    remaining += [kind for kind in plan['goal']['outputs'] if kind not in facts['done'] and kind not in remaining]
    evidence = {'plan': _hash(root.with_suffix('.json')),
                'run': _inventory(root, exclude=('continuation-claim.json',)),
                'outputs': {record['artifact']['task_root']: _output_inventory(record)
                            for record, _ in records if record['action']['kind'] in {'spreadsheet', 'document'}},
                'ancestors': seed['hashes']}
    return {'run': str(root), 'instruction': plan['instruction'], 'goal': plan['goal'],
            'done': done, 'remaining': remaining, 'artifacts': facts['artifacts'],
            'records': records, 'origins': origins, 'depth': seed['depth'],
            'evidence_sha256': digest(evidence)}


def _validate_pre_dispatch(root, step, observation, actions):
    """Validate an engine selection which never reached an effect dispatcher."""
    from .computer_agent import choices_for
    state_hash, choices = choices_for(root, observation, actions)
    recorded = read_record(step / 'choices.json')
    decision = read_record(step / 'decision.json')
    chosen = read_record(step / 'intent.json')
    if (recorded != {'state_sha256': state_hash, 'choices': choices}
            or type(decision) is not dict or set(decision) != {'method', 'choice', 'state_sha256'}
            or decision['state_sha256'] != state_hash or decision['choice'] != chosen
            or chosen not in choices or chosen['action'].get('kind') not in {'search', 'read', 'supplement', 'compare'}
            or decision['method'] != ('single_legal_action' if len(choices) == 1 else 'model_choice')):
        raise ComputerError('recovery_pre_dispatch_changed')


def _output_inventory(record):
    from .interaction_recovery import _inventory
    task = safe_root(record['artifact']['task_root'])
    artifact = safe_root(record['artifact']['path'])
    # Native Office may keep this exact sibling locked while its verified
    # document stays open. It carries no input, output, receipt or lineage data.
    lock = artifact.with_name('~$' + artifact.name)
    return _inventory(task, exclude=(lock.relative_to(task).as_posix(),))


def _label(action):
    if action['kind'] == 'search':
        return 'search:' + str(action['query_index'])
    if action['kind'] == 'read':
        return 'read:' + action['candidate_id']
    if action['kind'] == 'supplement':
        return 'supplement:' + action['need_id']
    return action['kind']


def inspect(session, run):
    result = {'run': str(run), 'eligible': False, 'code': 'recovery_evidence_invalid',
              'instruction': '', 'completed_edits': [], 'remaining_edits': [], 'artifacts': [], 'depth': 0}
    try:
        value = _inspect(session, run)
        if value['depth'] >= MAX_DEPTH:
            raise ComputerError('recovery_lineage_depth')
        result.update(eligible=True, code='recovery_ready', instruction=value['instruction'],
                      completed_edits=value['done'], remaining_edits=value['remaining'],
                      artifacts=value['artifacts'], depth=value['depth'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        if isinstance(exc, ComputerError):
            result['code'] = str(exc)
    return result


def prepare(session, run):
    from .computer_agent import MAX_ACTIONS, MAX_SECONDS
    from .computer_goals import _save_goal
    before = _inspect(session, run)
    if before['depth'] >= MAX_DEPTH:
        raise ComputerError('recovery_lineage_depth')
    value = {'schema': 3, 'mode': 'agent', 'instruction': before['instruction'],
             'goal': deepcopy(before['goal']), 'max_actions': MAX_ACTIONS, 'max_seconds': MAX_SECONDS,
             'prepared_at': time.time(), 'recovery': {
                 'version': 2, 'kind': 'research', 'parent_run': before['run'],
                 'parent_evidence_sha256': before['evidence_sha256'], 'depth': before['depth'] + 1,
                 'completed_edits': before['done'], 'remaining_edits': before['remaining']}}
    if _inspect(session, run) != before:
        raise ComputerError('recovery_evidence_changed')
    return {'plan': _save_goal(session, value),
            'reason': '已核验历史采集来源与成果；请批准仅执行尚未开始的工作。'}


def validate(session, plan, *, claimed=False):
    try:
        return _lineage(session, plan, claimed=claimed)
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise ComputerError('recovery_evidence_invalid') from None


def claim(session, plan):
    validate(session, plan)
    try:
        write_new(Path(plan['recovery']['parent_run']) / 'continuation-claim.json', {'child': plan['sha256']})
    except FileExistsError:
        raise ComputerError('recovery_already_claimed') from None
    validate(session, plan, claimed=True)


def describe(plan):
    meta = plan['recovery']
    return '\n'.join([
        '原会话研究任务的新批准续跑（原失败记录保留）：',
        '已核验历史步骤，仅带入记录与成果：' + ('、'.join(meta['completed_edits']) or '无'),
        '尚未开始，本次允许执行：' + '、'.join(meta['remaining_edits']),
        '网页沿用原采集时间，不代表网站当前状态；不重复已执行的搜索、读取或Office操作。',
        f"续跑层数：{meta['depth']}/{MAX_DEPTH}；沿用原会话6次模型调用与共享预算。",
        '逐步重验祖先、来源、回执与文件；未知或已失败的Office操作拒绝恢复。'])
