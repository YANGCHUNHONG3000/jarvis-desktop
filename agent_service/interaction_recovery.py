"""Fresh, single-use continuations of independently proven inactive goals.

Inspection is local and read-only. A continuation retains its original goal and
session; verified artifacts are references, never instructions to replay edits.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import time

from .computer import ComputerError, digest, read_record, safe_root, write_new
from .computer_agent_tools import _load

MAX_DEPTH = 4
MAX_RUN_FILES = 256
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _hash(path):
    path = safe_root(path)
    value = sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _inventory(root, *, exclude=()):
    root = safe_root(root)
    files = {}
    for path in root.rglob('*'):
        safe_root(path)
        if path.is_file() and path.relative_to(root).as_posix() not in exclude:
            if len(files) >= MAX_RUN_FILES:
                raise ComputerError('recovery_evidence_limit')
            files[path.relative_to(root).as_posix()] = _hash(path)
    return files


def _run_root(session, value):
    root = safe_root(value)
    if (root.parent != session.root/'goals' or not root.is_dir()
            or not root.name.endswith('.run') or not _SHA.fullmatch(root.name[:-4])):
        raise ComputerError('recovery_run_owner_invalid')
    return root


def _plan(session, run):
    from .computer_agent import MAX_ACTIONS, MAX_SECONDS
    from .computer_interaction import validate_goal
    plan = read_record(run.with_suffix('.json'))
    fields = {'schema','mode','instruction','goal','max_actions','max_seconds','prepared_at','sha256'}
    if (type(plan) is not dict or set(plan) not in (fields, fields|{'recovery'})
            or plan['schema'] != 3 or plan['mode'] != 'agent'
            or plan['sha256'] != run.name[:-4]
            or digest({k:v for k,v in plan.items() if k != 'sha256'}) != plan['sha256']
            or plan['max_actions'] != MAX_ACTIONS or plan['max_seconds'] != MAX_SECONDS):
        raise ComputerError('recovery_plan_changed')
    goal = validate_goal(plan['goal'])
    if read_record(run/'claim.json').get('approval') != plan['sha256']:
        raise ComputerError('recovery_claim_changed')
    if _load(run/'environment-goal.json')[0] != goal:
        raise ComputerError('recovery_goal_changed')
    return plan


def _claim(run, child, allow_unclaimed):
    path = run/'continuation-claim.json'
    if path.exists():
        if child is None or read_record(path) != {'child':child}:
            raise ComputerError('recovery_already_claimed')
    elif child is not None and not allow_unclaimed:
        raise ComputerError('recovery_lineage_claim_missing')


def _lineage(session, plan, *, claimed, seen=()):
    """Validate all ancestors, including their immutable byte inventories."""
    if 'recovery' not in plan:
        return {'done':[], 'artifacts':[], 'depth':0, 'hashes':[]}
    metadata = plan['recovery']
    fields = {'version','parent_run','parent_evidence_sha256','depth','completed_edits','remaining_edits'}
    if (type(metadata) is not dict or set(metadata) not in (fields,
            fields|{'kind','browser_checkpoint_sha256'}, fields|{'kind','browser_resume_sha256'})
            or metadata['version'] != 1 or type(metadata['depth']) is not int
            or not 1 <= metadata['depth'] <= MAX_DEPTH
            or not _SHA.fullmatch(str(metadata['parent_evidence_sha256']))):
        raise ComputerError('recovery_metadata_invalid')
    if 'kind' in metadata:
        key = {'interactive_browser':'browser_checkpoint_sha256',
               'interactive_browser_pending':'browser_resume_sha256'}.get(metadata['kind'])
        if key is None or not _SHA.fullmatch(str(metadata.get(key))):
            raise ComputerError('recovery_metadata_invalid')
    parent = _inspect(session, metadata['parent_run'], child=plan['sha256'],
                      allow_unclaimed=not claimed, seen=seen)
    if (parent['evidence_sha256'] != metadata['parent_evidence_sha256']
            or parent['goal'] != plan['goal'] or parent['instruction'] != plan['instruction']
            or parent['depth']+1 != metadata['depth']
            or parent['done'] != metadata['completed_edits']
            or parent['remaining'] != metadata['remaining_edits']):
        raise ComputerError('recovery_lineage_changed')
    checkpoint = parent.get('browser_checkpoint')
    resume = parent.get('browser_resume')
    if (checkpoint is not None and (metadata.get('kind') != 'interactive_browser'
            or digest(checkpoint) != metadata.get('browser_checkpoint_sha256'))
            or resume is not None and (metadata.get('kind') != 'interactive_browser_pending'
            or digest(resume) != metadata.get('browser_resume_sha256'))
            or checkpoint is None and resume is None and 'kind' in metadata):
        raise ComputerError('recovery_browser_checkpoint_changed')
    return {'done':parent['done'], 'artifacts':parent['artifacts'],
            'depth':metadata['depth'], 'hashes':[parent['evidence_sha256']],
            'browser_checkpoint':checkpoint, 'browser_resume':resume,
            'browser_seen_sessions':parent.get('browser_seen_sessions', []),
            'browser_history':parent.get('browser_history', [])}


def _undispatched(root, step):
    """Check any persisted selection while preserving proof of no dispatch."""
    from .computer_agent import choices_for
    paths = {name:step/(name+'.json') for name in ('choices','decision','intent')}
    if not paths['choices'].exists():
        if paths['decision'].exists() or paths['intent'].exists():
            raise ComputerError('recovery_evidence_incomplete')
        return  # Exit between creating the directory and writing choices.
    choices = read_record(paths['choices'])
    before = read_record(root/('observation-'+step.name[-2:]+'.json'))
    state, expected = choices_for(root, before, [v['action'] for v in choices['choices']])
    if choices != {'state_sha256':state,'choices':expected}:
        raise ComputerError('recovery_dispatched_action_not_verified')
    if not paths['decision'].exists():
        if paths['intent'].exists():
            raise ComputerError('recovery_evidence_incomplete')
        return
    decision = read_record(paths['decision'])
    chosen = decision.get('choice')
    if (chosen not in expected or decision != {'method':
            'single_legal_action' if len(expected) == 1 else 'model_choice',
            'choice':chosen, 'state_sha256':state}
            or paths['intent'].exists() and read_record(paths['intent']) != chosen):
        raise ComputerError('recovery_dispatched_action_not_verified')


def _rejected_initial_read(root, plan, outcome, seed, attempts, checkpoint, observation):
    """Only an inactive, action-free bootstrap denial retains its old cursor."""
    return bool(outcome is not None and plan.get('recovery', {}).get('kind') == 'interactive_browser_pending'
        and plan['goal']['browser'].get('mode') == 'public_search'
        and seed.get('browser_resume') is not None and checkpoint is None
        and outcome.get('ok') is False and outcome.get('state') == 'stopped'
        and outcome.get('code') == 'agent_no_safe_next_action'
        and outcome.get('attempts') == [] and outcome.get('completed') == []
        and outcome.get('actions') == 0 and outcome.get('source_reads') == 0
        and outcome.get('outputs') == {} and outcome.get('failures') == []
        and outcome.get('reobservations') == [] and attempts == []
        and (root/'owner.json').is_file() and (root/'owner.lease').is_file()
        and not any(root.glob('step-*'))
        and not (root/'browser-checkpoint.json').exists()
        and not (root.parent.parent/'page-deliveries'/root.stem).exists()
        and list(root.glob('observation-*.json')) == [root/'observation-01.json']
        and observation.get('checks') == {'sources_ready':False,'goal_met':False}
        and observation.get('source_reads') == 0 and observation.get('failures') == []
        and observation.get('done_edits') == seed['done']
        and observation.get('started_edits') == seed['done']
        and observation.get('artifacts') == seed['artifacts'])


def _inspect(session, run, *, child=None, allow_unclaimed=False, seen=(), successful=False):
    from .office_edit import _bind, verify_edit_result
    root = _run_root(session, run)
    if str(root) in seen or len(seen) > MAX_DEPTH:
        raise ComputerError('recovery_lineage_depth')
    _claim(root, child, allow_unclaimed)
    plan = _plan(session, root)
    abandoned = not (root/'outcome.json').exists()
    # An OS lease, never an absent outcome or an old PID, proves inactivity.
    # Legacy terminal runs retain their original validation contract.
    if (abandoned or 'lifecycle' in read_record(root/'claim.json')
            or (root/'owner.json').exists() or (root/'owner.lease').exists()):
        from .run_lifecycle import assert_inactive
        assert_inactive(root)
    if abandoned:
        if successful or plan['goal'].get('kind') != 'interactive':
            raise ComputerError('recovery_terminal_state_not_safe')
        outcome = None
    else:
        outcome = read_record(root/'outcome.json')
        terminal = (outcome.get('ok') is True and outcome.get('state') == 'verified'
                    and outcome.get('code') == 'objective_verified') if successful else (
                    outcome.get('ok') is False and outcome.get('state') in {'partial','stopped'})
        if not terminal or outcome.get('engine') != 'observed_agent' or outcome.get('run') != str(root):
            raise ComputerError('recovery_terminal_state_not_safe')
        if outcome.get('recovery') != plan.get('recovery'):
            raise ComputerError('recovery_outcome_changed')
    seed = _lineage(session, plan, claimed=True, seen=(*seen, str(root)))
    done, artifacts = list(seed['done']), deepcopy(seed['artifacts'])
    checkpoint = seed.get('browser_checkpoint')
    resume = seed.get('browser_resume')
    browser_config = plan['goal']['browser']
    if browser_config is not None and checkpoint is None and (root/'browser-checkpoint.json').exists():
        from .browser_checkpoint import load as load_checkpoint
        checkpoint = load_checkpoint(root, browser_config)
    edits = {item['id']:item for item in plan['goal']['edits']}
    excluded = ('continuation-claim.json',)
    if successful:
        excluded += ('repository-document.claim.json','repository-document.result.json','repository-delivery.json')
    evidence = {'plan':_hash(root.with_suffix('.json')),
                'run':_inventory(root, exclude=excluded),
                'sources':{}, 'outputs':{}, 'ancestors':seed['hashes']}
    if abandoned:
        evidence['outcome'] = {'present':False}
        # Exit may interrupt persistence. Parse every bounded JSON record;
        # hashing truncated bytes cannot prove an undispatched action safe.
        for name in evidence['run']:
            if name.endswith('.json'):
                read_record(root/name)
    for item in edits.values():
        source_root = safe_root(item['source_root'])
        if source_root.parent != session.root/'edit-inputs':
            raise ComputerError('recovery_source_owner_invalid')
        _bind(item['spec'], source_root)
        evidence['sources'][str(source_root)] = _inventory(source_root)
    attempts, completed, browser_sessions, browser_verified = [], [], set(), False
    browser_befores = {}
    for step in sorted(root.glob('step-*')):
        if not re.fullmatch(r'step-(0[1-9]|1[0-2])', step.name) or not step.is_dir():
            raise ComputerError('recovery_step_invalid')
        if not (step/'environment-intent.json').exists():
            if (step/'environment-result.json').exists() or (step/'verification.json').exists():
                raise ComputerError('recovery_evidence_incomplete')
            if abandoned:
                _undispatched(root, step)
            continue  # No adapter dispatch was recorded.
        intent, intent_sha = _load(step/'environment-intent.json')
        record, _ = _load(step/'environment-result.json')
        chosen = read_record(step/'intent.json')
        verification = read_record(step/'verification.json')
        action = intent.get('action')
        if (type(action) is not dict or chosen.get('action') != action or record.get('action') != action
                or record.get('intent_sha256') != intent_sha or record.get('result') != verification
                or (action.get('kind') == 'office_edit' and (set(action) != {'kind','edit_id'}
                    or action['edit_id'] not in edits or verification.get('ok') is not True
                    or verification.get('state') != 'verified'))):
            raise ComputerError('recovery_dispatched_action_not_verified')
        attempt = {'step':int(step.name[5:]), 'action':action, 'result':verification}
        attempts.append(attempt)
        if verification.get('ok') is True:
            completed.append(attempt)
        if action.get('kind') != 'office_edit':
            if browser_config is None or seed.get('browser_checkpoint') is not None or action.get('kind') not in {
                    'browser_fill','browser_click','browser_wait','browser_search_open','browser_read'}:
                raise ComputerError('recovery_dispatched_action_not_verified')
            from .browser_checkpoint import verify_action
            before = verify_action(root, step, browser_config, action, verification, include_before=True)
            browser_sessions.add(before['session_id'])
            if browser_config.get('mode') == 'public_search':
                browser_befores[attempt['step']] = before
            browser_verified = browser_verified or verification.get('ok') is True
            continue
        item = edits[action['edit_id']]
        if item['id'] in done:
            raise ComputerError('recovery_duplicate_edit')
        actual = verify_edit_result(record['native'], item['source_root'], item['spec'])
        artifact = safe_root(actual['artifact'])
        manifest, _ = _load(artifact.parent/'edit-manifest.json')
        if manifest != {'source_root':item['source_root'], 'spec':item['spec'],
                        'run':str(root), 'step':str(step)}:
            raise ComputerError('recovery_manifest_changed')
        evidence['outputs'][str(artifact.parent)] = _inventory(
            artifact.parent, exclude=('~$'+artifact.name,))
        done.append(item['id'])
        artifacts.append({'kind':item['spec']['kind'], 'path':actual['artifact'],
                          'sha256':actual['sha256'], 'provenance':'office_edit'})
    history = deepcopy(seed.get('browser_history', []))
    history.extend({**deepcopy(a), **({'browser_before':browser_befores[a['step']]}
                    if a['step'] in browser_befores else {})}
                   for a in attempts if a['action']['kind'].startswith('browser_'))
    seen_sessions = set(seed.get('browser_seen_sessions', []))
    rejected_initial_read = False
    if resume is not None and (root/'observation-01.json').exists():
        from .browser_checkpoint import verify_browser
        observation = read_record(root/'observation-01.json')
        rejected_initial_read = bool(observation['browser'].get('locked') is True
            and _rejected_initial_read(root, plan, outcome, seed, attempts, checkpoint, observation))
        initial = verify_browser(browser_config,
            observation['browser'], complete=False, rejected_initial=rejected_initial_read)
        ancestral_sessions = {a['result']['observation']['session_id']
                              for a in seed.get('browser_history', [])}
        ancestral_sessions.update(seen_sessions)
        if (initial.get('discovery', {}).get('resume') != resume
                or initial['session_id'] in ancestral_sessions):
            raise ComputerError('recovery_browser_resume_changed')
        if browser_sessions and browser_sessions != {initial['session_id']}:
            raise ComputerError('recovery_browser_session_changed')
        seen_sessions.add(initial['session_id'])
        if checkpoint is not None:
            from .browser_resume import derive
            options = {'completed_browser':checkpoint['browser']} if browser_config.get('mode') == 'public_search' else {}
            current_cursor = derive(browser_config, history, **options)
            # A manual handoff can retain an advanced, independently proven
            # phase. It cannot authorize a different target or browser session.
            proof = 'content_proof' if browser_config.get('mode') == 'public_search' else 'repository_proof'
            if (checkpoint['browser']['session_id'] != initial['session_id']
                    or checkpoint['browser'].get('discovery', {}).get(proof) != 'fresh'
                    or checkpoint['browser'].get('discovery', {}).get('resume')
                       not in (resume, current_cursor)):
                raise ComputerError('recovery_browser_session_changed')
    elif resume is not None and checkpoint is not None:
        raise ComputerError('recovery_browser_observation_missing')
    if browser_config is not None and seed.get('browser_checkpoint') is None:
        if len(browser_sessions) > 1:
            raise ComputerError('recovery_browser_action_evidence_missing')
        if checkpoint is not None:
            if ((not resume and not browser_verified)
                    or browser_sessions and browser_sessions != {checkpoint['browser']['session_id']}):
                raise ComputerError('recovery_browser_action_evidence_missing')
            from .browser_checkpoint import verify_history
            verify_history(browser_config, checkpoint, history, resumed=resume is not None)
            resume = None
        else:
            from .browser_resume import derive
            resume = derive(browser_config, history)
    seen_sessions.update(browser_sessions)
    if checkpoint is not None:
        seen_sessions.add(checkpoint['browser']['session_id'])
    artifacts.sort(key=lambda a:a['kind']=='document')
    if outcome is not None and (outcome.get('attempts') != attempts or outcome.get('completed') != completed
            or outcome.get('actions') != len(attempts) or outcome.get('artifacts') != artifacts):
        raise ComputerError('recovery_outcome_changed')
    remaining = [item for item in edits if item not in done]
    verification_only = bool(abandoned and not remaining and resume is None
                             and (artifacts or checkpoint is not None))
    if not remaining and resume is None and not successful and not verification_only:
        raise ComputerError('recovery_nothing_remaining')
    if (remaining or resume is not None) and successful:
        raise ComputerError('recovery_goal_not_completed')
    if abandoned:
        assert_inactive(root)
        if (root/'outcome.json').exists():
            raise ComputerError('recovery_evidence_changed')
    return {'run':str(root), 'instruction':plan['instruction'], 'goal':plan['goal'],
            'done':done, 'remaining':remaining, 'artifacts':artifacts,
            'abandoned':abandoned, 'verification_only':verification_only,
            'depth':seed['depth'], 'evidence_sha256':digest(evidence),
            'browser_checkpoint':checkpoint, 'browser_resume':resume,
            'rejected_initial_read':rejected_initial_read,
            'browser_seen_sessions':sorted(seen_sessions),
            'browser_history':history}


def verify_reading(session, run):
    """Verify an inactive successful reading, including its independent capture."""
    value = _inspect(session, run, successful=True)
    if value['browser_checkpoint'] is None:
        raise ComputerError('recovery_browser_checkpoint_missing')
    return value


def verify_completed(session, run):
    """Validate a successful child for separately claimed repository delivery."""
    value = _inspect(session, run, successful=True)
    if value['browser_checkpoint'] is None or not _plan(session, _run_root(session, run)).get('recovery'):
        raise ComputerError('recovery_browser_checkpoint_missing')
    return value


def inspect(session, run):
    """Return a safe candidate or a stable refusal, without any side effects."""
    result = {'run':str(run), 'eligible':False, 'code':'recovery_evidence_invalid',
              'instruction':'', 'completed_edits':[], 'remaining_edits':[], 'artifacts':[], 'depth':0,
              'interrupted_without_outcome':False}
    try:
        if _research_plan(read_record(_run_root(session, run).with_suffix('.json'))):
            from .research_recovery import inspect as inspect_research
            return inspect_research(session, run)
        value = _inspect(session, run)
        if value['depth'] >= MAX_DEPTH:
            raise ComputerError('recovery_lineage_depth')
        result.update(eligible=True, code='recovery_ready', instruction=value['instruction'],
                      completed_edits=value['done'], remaining_edits=value['remaining'],
                      artifacts=value['artifacts'], depth=value['depth'],
                      interrupted_without_outcome=value['abandoned'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, ComputerError):
            result['code'] = str(exc)
    return result


def list_candidates(session, *, limit=30):
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ComputerError('recovery_candidate_limit_invalid')
    roots = sorted((session.root/'goals').glob('*.run'),
                   key=lambda p:(p.stat().st_mtime, p.name), reverse=True)
    rows = []
    for root in roots[:128]:
        try:
            plan = read_record(root.with_suffix('.json'))
            outcome = read_record(root/'outcome.json') if (root/'outcome.json').exists() else {}
            if outcome.get('ok') is True or plan.get('mode') != 'agent':
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        row = inspect(session, root)
        if not row['instruction'] and type(plan.get('instruction')) is str:
            row['instruction'] = plan['instruction'][:1000]
        row['session_root'] = str(session.root)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def prepare(session, run):
    from .computer_agent import MAX_ACTIONS, MAX_SECONDS
    from .computer_goals import _save_goal
    if _research_plan(read_record(_run_root(session, run).with_suffix('.json'))):
        from .research_recovery import prepare as prepare_research
        return prepare_research(session, run)
    before = _inspect(session, run)
    if before['depth'] >= MAX_DEPTH:
        raise ComputerError('recovery_lineage_depth')
    value = {'schema':3, 'mode':'agent', 'instruction':before['instruction'],
             'goal':deepcopy(before['goal']), 'max_actions':MAX_ACTIONS,
             'max_seconds':MAX_SECONDS, 'prepared_at':time.time(),
             'recovery':{'version':1, 'parent_run':before['run'],
                         'parent_evidence_sha256':before['evidence_sha256'],
                         'depth':before['depth']+1, 'completed_edits':before['done'],
                         'remaining_edits':before['remaining']}}
    if before['browser_checkpoint'] is not None:
        value['recovery'].update(kind='interactive_browser',
                                browser_checkpoint_sha256=digest(before['browser_checkpoint']))
    elif before['browser_resume'] is not None:
        value['recovery'].update(kind='interactive_browser_pending',
                                browser_resume_sha256=digest(before['browser_resume']))
    # Close the inspection/snapshot race before making a reviewable preview.
    if _inspect(session, run) != before:
        raise ComputerError('recovery_evidence_changed')
    plan = _save_goal(session, value)
    reason = ('原任务已无活动执行且未写入结束记录；已从逐步回执重新核验历史动作与成果。'
              if before['abandoned'] else '已重新核验历史动作与成果。')
    if before['verification_only']:
        reason += '全部批准动作均已有独立完成证明；新任务只重新核验并记录结果，不重放动作。'
    if before.get('rejected_initial_read'):
        reason += '上次新浏览器的初始读取被明确拒绝，未派发任何动作；本次仅从已核验的祖先位置新建只读页面。'
    prompt = ('请批准新建核验任务并记录结果。' if before['verification_only']
              else '请批准从安全检查点继续未完成部分。')
    return {'plan':plan, 'reason':reason+prompt}


def validate(session, plan, *, claimed=False):
    """Called before dispatch and on every subsequent observation/action/read."""
    if _research_plan(plan):
        from .research_recovery import validate as validate_research
        return validate_research(session, plan, claimed=claimed)
    try:
        return _lineage(session, plan, claimed=claimed)
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ComputerError('recovery_evidence_invalid') from None


def claim(session, plan):
    if _research_plan(plan):
        from .research_recovery import claim as claim_research
        return claim_research(session, plan)
    validate(session, plan)
    try:
        write_new(Path(plan['recovery']['parent_run'])/'continuation-claim.json', {'child':plan['sha256']})
    except FileExistsError:
        raise ComputerError('recovery_already_claimed') from None
    validate(session, plan, claimed=True)


def describe(plan):
    if _research_plan(plan):
        from .research_recovery import describe as describe_research
        return describe_research(plan)
    metadata = plan['recovery']
    lines = ['历史任务的新批准续跑（原中断记录保留）：']
    if metadata.get('kind') == 'interactive_browser':
        lines.append('复用已核验网页历史捕获（非当前实时页面），不启动浏览器；本次只执行未开始的Office编辑。')
    elif metadata.get('kind') == 'interactive_browser_pending':
        public = plan['goal']['browser'].get('mode') == 'public_search'
        location = '公开站点搜索结果或已选页面' if public else 'GitHub搜索结果或仓库打开位置'
        lines.append('从已核验的'+location+'新建浏览器，重新观察当前页面后继续；不重放历史输入与点击。')
    if not metadata['remaining_edits'] and metadata.get('kind') != 'interactive_browser_pending':
        lines.append('全部批准动作已有完成证明；新任务只重新核验并记录结果，不重放动作。')
    return '\n'.join(lines + [
        '已完成并重新核验，仅带入成果：'+('、'.join(metadata['completed_edits']) or '无'),
        '尚未开始，本次允许执行：'+('、'.join(metadata['remaining_edits']) or '无'),
        f"续跑层数：{metadata['depth']}/{MAX_DEPTH}；沿用原会话模型调用上限与共享账本。",
        '历史证据、原文件或成果变化即停止；未知或已失败的原生操作不恢复。'])


def _research_plan(plan):
    return (type(plan) is dict and plan.get('mode') == 'agent'
            and type(plan.get('goal')) is dict and plan['goal'].get('kind') != 'interactive')
