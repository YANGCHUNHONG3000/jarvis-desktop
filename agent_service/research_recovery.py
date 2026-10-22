"""Single-use research continuations from acknowledged captured evidence."""
from copy import deepcopy
from pathlib import Path
import re
import time

from .computer import ComputerError, digest, read_record, write_new, safe_root
from .computer_agent_tools import AgentEnvironment, _load
from .computer_goal_adapters import TASK_BASE

MAX_DEPTH = 4
_SHA = re.compile(r'[0-9a-f]{64}\Z')


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
    for step in sorted(root.glob('step-*')):
        if not re.fullmatch(r'step-(0[1-9]|1[0-2])', step.name) or not step.is_dir():
            raise ComputerError('recovery_step_invalid')
        if not (step / 'environment-intent.json').exists():
            if any((step / name).exists() for name in ('intent.json', 'environment-result.json', 'verification.json')):
                raise ComputerError('recovery_evidence_incomplete')
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
    env = AgentEnvironment(plan['goal'], root, stop=lambda: False, deadline=float('inf'),
                           task_base=next(iter(bases), TASK_BASE),
                           recovery_validator=lambda: seed)
    facts = env._facts()
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
