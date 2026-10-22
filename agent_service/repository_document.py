"""A verified local GitHub reading becomes an owned Word task, without a model.

Browser evidence is only read. The single-use claim belongs to that reading;
an interrupted Office operation never causes another execution on retry.
"""
from copy import deepcopy
from hashlib import sha256
import math
from pathlib import Path
import re
import time
import uuid

from .computer import BASE, ComputerError, digest, read_record, safe_root, write_new
from .computer_agent_tools import _load, _save
from .computer_goal_adapters import TASK_BASE


_SESSION = re.compile(r'computer-[0-9a-f]{32}\Z')
_RUN = re.compile(r'[0-9a-f]{64}\.run\Z')
_CLAIM = 'repository-document.claim.json'
_RESULT = 'repository-document.result.json'


def _record(path):
    value = read_record(path)
    if type(value) is not dict:
        raise ComputerError('repository_source_invalid')
    return value


def verify_repository_source(run, *, computer_base=BASE):
    """Revalidate canonical saved evidence. Returned text is local private data."""
    try:
        root, base = safe_root(run), safe_root(computer_base)
        session = root.parent.parent
        if (not _RUN.fullmatch(root.name) or root.parent.name != 'goals'
                or not _SESSION.fullmatch(session.name) or session.parent != base
                or not root.is_dir()):
            raise ValueError()
        session_record = _record(session / 'session.json')
        if session_record.get('schema') != 1:
            raise ValueError()
        plan_path = root.with_suffix('.json')
        plan = _record(plan_path)
        approval = plan.get('sha256')
        if (plan.get('schema') != 3 or plan.get('mode') != 'agent'
                or approval != root.stem
                or digest({k: v for k, v in plan.items() if k != 'sha256'}) != approval):
            raise ValueError()
        goal = plan['goal']
        recovered = None
        if 'recovery' in plan:
            from .computer import ComputerSession
            from .interaction_recovery import verify_completed
            if plan['recovery'].get('kind') not in {'interactive_browser','interactive_browser_pending'}:
                raise ValueError()
            recovered = verify_completed(ComputerSession(session), root)
        from .browser_github import validate_github_config
        config = validate_github_config(goal['browser'])
        if goal.get('kind') != 'interactive' or config != goal['browser']:
            raise ValueError()
        saved_goal, _ = _load(root / 'environment-goal.json')
        claim = _record(root / 'claim.json')
        if saved_goal != goal or claim.get('approval') != approval or claim.get('mode') != 'agent':
            raise ValueError()
        if (type(claim.get('at')) not in (float, int) or not math.isfinite(claim['at'])):
            raise ValueError()
        outcome = _record(root / 'outcome.json')
        if (outcome.get('ok') is not True or outcome.get('state') != 'verified'
                or outcome.get('code') != 'objective_verified'
                or outcome.get('engine') != 'observed_agent'
                or outcome.get('goal_kind') != 'interactive' or outcome.get('run') != str(root)
                or outcome.get('failures') != []):
            raise ValueError()
        observations = sorted(root.glob('observation-*.json'))
        if not 1 <= len(observations) <= 13:
            raise ValueError()
        final_path = observations[-1]
        final = _record(final_path)
        browser = final['browser']
        fresh_recovered = recovered is not None and recovered['browser_checkpoint']['run'] == str(root)
        if recovered is not None:
            from .browser_checkpoint import frozen
            expected_browser = (recovered['browser_checkpoint']['browser'] if fresh_recovered
                                else frozen(recovered['browser_checkpoint']))
            if browser != expected_browser:
                raise ValueError()
            from .browser_checkpoint import verify_browser
            verify_browser(config, browser)
        discovery = browser['discovery']
        if (final['checks'] != {'sources_ready': True, 'goal_met': True}
                or browser.get('url') != config['start_url'] or browser.get('locked') is not False
                or browser.get('human_required') is not False or browser.get('actions') != []
                or browser.get('checks') != {'goal_met': True}
                or discovery.get('mode') != 'github_repo'
                or any(discovery.get(k) is not True for k in (
                    'query_verified', 'repository_verified',
                    'repository_identity_matches', 'main_present'))
                or type(discovery.get('authenticated')) is not bool
                or (config['human_login'] and discovery['authenticated'] is not True)
                or type(discovery.get('repository_status')) is not int or discovery['repository_status'] != 200
                or (recovered is None and (type(discovery.get('repository_search_status')) is not int
                    or discovery['repository_search_status'] != 200))
                or discovery.get('ambiguous_controls') is not False
                or discovery.get('controls_overflow') is not False
                or browser.get('state_id') != digest({k: v for k, v in browser.items()
                                                     if k not in {'state_id', 'actions'}})):
            raise ValueError()
        text = browser['text']
        if (type(text) is not str or not text.strip() or len(text) > 24000
                or any(term not in text for term in config['required_text'])
                or outcome.get('repository_read') != {
                    'url': config['start_url'], 'text': text, 'authenticated': discovery['authenticated']}):
            raise ValueError()
        attempts, record_hashes, record_paths = [], [], []
        if recovered is not None:
            from .interaction_recovery import validate
            seed = validate(ComputerSession(session), plan, claimed=True)
            record_hashes.extend(seed['hashes'])
        for step in sorted(root.glob('step-*')):
            if not (step / 'environment-intent.json').exists():
                # Selection may have been discarded before dispatch.
                if (step / 'intent.json').exists() or (step / 'environment-result.json').exists():
                    raise ValueError()
                continue
            if not re.fullmatch(r'step-(?:0[1-9]|1[0-2])', step.name):
                raise ValueError()
            intent, intent_hash = _load(step / 'environment-intent.json')
            record, record_hash = _load(step / 'environment-result.json')
            selected = _record(step / 'intent.json')
            decision, choices = _record(step / 'decision.json'), _record(step / 'choices.json')
            result = record['result']
            before = _record(root / ('observation-' + step.name[-2:] + '.json'))
            from .computer_agent import choices_for
            state_hash, expected_choices = choices_for(root, before, [record['action']])
            if (record.get('intent_sha256') != intent_hash or record['action'] != intent['action']
                    or selected.get('action') != record['action'] or decision.get('choice') != selected
                    or choices != {'state_sha256': state_hash, 'choices': expected_choices}
                    or selected != expected_choices[0] or decision.get('method') != 'single_legal_action'
                    or decision.get('state_sha256') != state_hash
                    or _record(step / 'verification.json') != result
                    or result.get('ok') is not True or result.get('state') != 'verified'
                    or record['action'].get('kind') not in {
                        'browser_click', 'browser_fill', 'browser_wait', 'browser_read',
                        'browser_search_open', 'office_edit'}):
                raise ValueError()
            attempts.append({'step': int(step.name[-2:]), 'action': record['action'], 'result': result})
            record_hashes.append(record_hash)
            record_paths.extend(step / name for name in ('environment-intent.json',
                'environment-result.json', 'intent.json', 'decision.json', 'choices.json', 'verification.json'))
        if (not attempts and recovered is None or len(attempts) > 12 or outcome.get('attempts') != attempts
                or outcome.get('completed') != attempts or outcome.get('actions') != len(attempts)
                or final.get('evidence_sha256') != digest(record_hashes)):
            raise ValueError()
        browser_results = [a['result']['observation'] for a in attempts
                           if a['action']['kind'].startswith('browser_')]
        browser_attempts = [a for a in attempts if a['action']['kind'].startswith('browser_')]
        codes = [a['result'].get('code') for a in browser_attempts]
        opened = codes.index('browser_github_repository_opened') if 'browser_github_repository_opened' in codes else -1
        if recovered is None and (not browser_results or browser_results[-1] != browser
                or codes.count('browser_github_repository_opened') != 1
                or 'browser_github_search_submitted' not in codes[:opened]
                # Navigation may commit before the completed response/body;
                # verified observation waits can establish final completion.
                or any(code != 'browser_wait_observed' for code in codes[opened + 1:])
                or any(a['result'].get('verification', {}).get('independent_read') is not True
                       for a in browser_attempts)):
            raise ValueError()
        if recovered is not None and not fresh_recovered and (browser_results or any(
                a['action']['kind'] != 'office_edit' for a in attempts)):
            raise ValueError()
        for path in root.glob('reobserve-*.json'):
            if _record(path).get('effect_dispatched') is not False:
                raise ValueError()
        paths = [session / 'session.json', plan_path, root / 'claim.json',
                 root / 'environment-goal.json', root / 'outcome.json', *observations,
                 *record_paths, *sorted(root.glob('human-*.json')), *sorted(root.glob('reobserve-*.json'))]
        # Every path is checked before hashing, including ancestor junctions.
        files = {}
        for path in paths:
            safe_root(path)
            _record(path)
            files[str(path.relative_to(session))] = sha256(path.read_bytes()).hexdigest()
        identity = {'session': str(session), 'run': str(root), 'plan_sha256': approval,
                    'claim_sha256': sha256((root / 'claim.json').read_bytes()).hexdigest(),
                    'final_observation': final_path.name,
                    'final_observation_sha256': sha256(final_path.read_bytes()).hexdigest(),
                    'outcome_sha256': sha256((root / 'outcome.json').read_bytes()).hexdigest(),
                    'files': files, 'url': config['start_url'], 'query': config['query'],
                    'authenticated': discovery['authenticated'],
                    'repository_search_status': discovery['repository_search_status'], 'repository_status': 200,
                    'body_chars': len(text), 'body_sha256': sha256(text.encode('utf-8')).hexdigest(),
                    'approved_at': claim['at']}
        if recovered is not None:
            # This binds the successful child, all ancestor claims/actions,
            # source bytes and native edit receipts without copying credentials.
            identity['recovery'] = {'completed_evidence_sha256':recovered['evidence_sha256'],
                'browser_checkpoint_sha256':digest(recovered['browser_checkpoint']),
                'captured_at':recovered['browser_checkpoint']['captured_at'],
                'captured_run':recovered['browser_checkpoint']['run']}
            if discovery.get('search_proof') == 'historical':
                identity['search_proof'] = 'historical'
            if verify_completed(ComputerSession(session), root) != recovered:
                raise ValueError()
        return {'metadata': identity, 'source_sha256': digest(identity), 'text': text}
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        raise ComputerError('repository_source_invalid') from None


def _parts(text):
    # Character-for-character whitespace replacement preserves original offsets.
    flat = re.sub(r'[\r\n\t]', ' ', text)
    parts, start = [], 0
    while start < len(text):
        end = min(start + 1200, len(text))
        if end < len(text):
            candidates = [flat.rfind(mark, start + 1000, end) for mark in ('。', '！', '？', '. ', '; ', ' ')]
            boundary = max(candidates)
            if boundary >= 0:
                end = boundary + 1
        parts.append({'start': start, 'end': end, 'text': flat[start:end]})
        start = end
    return parts


def _spec(source, title):
    from .office import validate_spec
    meta, parts = source['metadata'], _parts(source['text'])
    reading_state = '已登录' if meta['authenticated'] else '公开读取'
    paragraphs = ['仓库概况', '仓库页面：' + meta['url'], '实际检索词：' + meta['query'],
        '读取结果：' + reading_state + '；仓库搜索、页面身份与页面内容已核验。',
        '来源与阅读范围',
        f"本文件整理已保存的仓库页面可见文字，共 {meta['body_chars']} 字符，分为 {len(parts)} 段，完整保留本次读取结果。",
        '正文包含页面导航及仓库展示内容，是页面资料摘录；未遍历仓库文件，也不是代码审查或语义摘要。网页读取本身最多保留 24000 字符，不代表完整仓库。',
        '以下按页面原顺序分段呈现；换行和制表符转为空格，其余文字不改。']
    if 'recovery' in meta:
        captured = time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(meta['recovery']['captured_at']))
        paragraphs.insert(6, '网页历史捕获时间：'+captured+'；本次续接复用该时刻保存的页面资料。')
    for number, part in enumerate(parts, 1):
        paragraphs.append(f"页面内容 第 {number} 部分：" + part['text'])
    try:
        return validate_spec({'kind': 'document', 'title': title, 'paragraphs': paragraphs})
    except ValueError:
        raise ComputerError('repository_document_text_unsupported') from None


def prepare_repository_document(run, *, computer_base=BASE, title='GitHub 仓库页面资料'):
    """Make a deterministic reviewable plan containing metadata, never body text."""
    source = verify_repository_source(run, computer_base=computer_base)
    spec = _spec(source, title)
    value = {'version': 1, 'mode': 'repository_document', 'run': source['metadata']['run'],
             'source_sha256': source['source_sha256'], 'source': source['metadata'],
             'title': spec['title'], 'spec_sha256': digest(spec),
             'segments': [{'start': p['start'], 'end': p['end']} for p in _parts(source['text'])]}
    return {**value, 'sha256': digest(value)}


def describe_repository_document(plan):
    source = plan['source']
    reading_state = '已登录' if source['authenticated'] else '公开读取'
    return '\n'.join(['将已核验的 GitHub 页面资料保存为新的 Word：',
        '标题：' + plan['title'], '来源：' + source['url'],
        f"{reading_state}；仓库搜索与页面内容已核验。完整保留已读取的 {source['body_chars']} 字符，分为 {len(plan['segments'])} 段。",
        '文档包含仓库概况、来源与阅读范围，以及按原顺序分段的页面内容；未遍历代码，不生成代码审查结论。',
        '只使用本机保存结果，无需重新打开浏览器；正文不上传模型，零 API。',
        '创建独立任务，经 WPS 与文件回读核验后，Word 自动成为下一任务资料。',
        '未知作用立即停止；同一来源成功重复执行只复用核验产物。'])


def list_repository_readings(*, computer_base=BASE, limit=20):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ComputerError('repository_source_limit_invalid')
    base = safe_root(computer_base)
    candidates = sorted(base.glob('computer-*/goals/*.run'),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    found = []
    for run in candidates:
        try:
            source = verify_repository_source(run, computer_base=base)
        except ComputerError:
            continue
        meta = source['metadata']
        found.append({'run': str(run), 'url': meta['url'], 'authenticated': meta['authenticated'],
                      'body_chars': meta['body_chars'], 'completed_at': meta['approved_at'],
                      'source_sha256': source['source_sha256']})
        if len(found) == limit:
            break
    return found


def _verified_result(value, plan, task_base):
    from .artifact_source import prepare_artifact_source
    from .task_history import inspect_task
    from .office import verify_office_file, _compare, _expected, _public_observation
    if (type(value) is not dict or value.get('approval') != plan['sha256']
            or type(value.get('ok')) is not bool):
        raise ComputerError('repository_document_result_changed')
    if value['ok'] is False:
        if (value.get('state') not in {'unknown', 'failed', 'cancelled', 'timed_out'}
                or value.get('artifacts') != [] or 'artifact' in value or 'ref' in value):
            raise ComputerError('repository_document_result_changed')
        return deepcopy(value)
    try:
        artifact = value['artifact']
        if (set(value) != {'ok', 'state', 'code', 'approval', 'task_root', 'artifacts', 'artifact', 'ref'}
                or value['state'] != 'verified' or value['code'] != 'repository_document_verified'
                or type(artifact) is not dict
                or set(artifact) != {'step', 'path', 'sha256', 'task_root', 'kind'}
                or artifact.get('kind') != 'document' or artifact.get('step') != 's1'
                or value['artifacts'] != [artifact]):
            raise ValueError()
        task = safe_root(artifact['task_root'])
        if (value['task_root'] != str(task) or value['ref']['task_root'] != str(task)
                or Path(artifact['path']).parent.parent.parent != task):
            raise ValueError()
        claim = _record(Path(plan['run']) / _CLAIM)
        if claim != {'approval': plan['sha256'], 'task_base': str(task_base), 'task_root': str(task)}:
            raise ValueError()
        origin, _ = _load(task / 'repository-origin.json')
        source, _ = _load(task / 'repository-source.json')
        if (origin != plan or source['metadata'] != plan['source']
                or source['source_sha256'] != plan['source_sha256']
                or sha256(source['text'].encode('utf-8')).hexdigest() != plan['source']['body_sha256']):
            raise ValueError()
        spec = _spec(source, plan['title'])
        receipt = _record(Path(artifact['path']).parent / 'receipt.json')
        manifest = _record(Path(artifact['path']).parent.parent / 'manifest.json')
        if (digest(spec) != plan['spec_sha256']
                or manifest.get('steps') != [{'id': 's1', 'action': 'document', 'data': spec}]
                or receipt.get('observed') != _public_observation(spec, _expected(spec))
                or prepare_artifact_source(artifact['path'], artifact['sha256'], task_base) != value['ref']
                or inspect_task(task, task_base)['state'] != 'succeeded'
                or not _compare(verify_office_file(Path(artifact['path']), spec), _expected(spec))):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise ComputerError('repository_document_result_changed') from None
    return deepcopy(value)


def execute_repository_document(plan, *, approval, task_base=TASK_BASE, computer_base=BASE,
                                stop=lambda: False, deadline=None, office_execute=None):
    """Claim once, run standard supervised Office, return a standard source ref."""
    from .artifact_source import prepare_artifact_task_root, prepare_artifact_source
    from .office_workflow import run_office_workflow
    if (type(plan) is not dict or approval != plan.get('sha256')
            or digest({k: v for k, v in plan.items() if k != 'sha256'}) != approval):
        raise ComputerError('repository_document_approval_invalid')
    fresh = prepare_repository_document(plan.get('run'), computer_base=computer_base, title=plan.get('title'))
    if fresh != plan:
        raise ComputerError('repository_document_source_changed')
    source = verify_repository_source(plan['run'], computer_base=computer_base)
    root, base = safe_root(plan['run']), safe_root(task_base)
    if deadline is None:
        deadline = time.monotonic() + 120
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or not callable(stop):
        raise ComputerError('repository_document_options_invalid')
    claim_path, result_path = root / _CLAIM, root / _RESULT
    if claim_path.exists():
        claim = _record(claim_path)
        if claim.get('approval') != approval or claim.get('task_base') != str(base):
            raise ComputerError('repository_document_already_claimed')
        try:
            claimed_task = safe_root(claim['task_root'])
            if (set(claim) != {'approval', 'task_base', 'task_root'} or claimed_task.parent != base
                    or not re.fullmatch(r'task-[0-9a-f]{32}', claimed_task.name)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ComputerError('repository_document_result_changed') from None
        if result_path.exists():
            return _verified_result(_load(result_path)[0], plan, base)
        return {'ok': False, 'state': 'unknown', 'code': 'repository_document_unknown',
                'approval': approval, 'task_root': claim.get('task_root'), 'artifacts': []}
    if result_path.exists():
        raise ComputerError('repository_document_result_changed')
    if stop() or time.monotonic() >= deadline:
        return {'ok': False, 'state': 'cancelled' if stop() else 'timed_out',
                'code': 'repository_document_cancelled' if stop() else 'repository_document_deadline',
                'approval': approval, 'artifacts': []}
    task = base / ('task-' + uuid.uuid4().hex)
    try:
        write_new(claim_path, {'approval': approval, 'task_base': str(base), 'task_root': str(task)})
    except FileExistsError:
        raise ComputerError('repository_document_already_claimed') from None
    started = False
    try:
        prepare_artifact_task_root(task, base)
        _save(task / 'repository-origin.json', plan)
        _save(task / 'repository-source.json', source)
        (task / 'request.txt').write_text(plan['title'], encoding='utf-8')
        write_new(task / 'events.jsonl', {'event': 'repository_document_started'})
        workflow = task / ('workflow-' + uuid.uuid4().hex)
        spec = _spec(source, plan['title'])
        # Revalidate the source immediately before Office acquires any effect.
        if prepare_repository_document(root, computer_base=computer_base, title=plan['title']) != plan:
            raise ComputerError('repository_document_source_changed')
        started = True
        value = run_office_workflow([{'id': 's1', 'action': 'document', 'data': spec}], workflow,
            execute=office_execute, deadline=deadline, cancel_requested=stop)
        artifacts = [{'step': key, 'path': receipt['artifact'], 'sha256': receipt['sha256']}
                     for key, receipt in value.get('receipts', {}).items()]
        write_new(task / 'result.json', {'root': str(task), 'kind': 'office', 'ok': value['ok'],
            'state': value['state'], 'workflow_root': str(workflow), 'completed': value['completed'],
            'artifacts': artifacts, 'artifact': artifacts[-1]['path'] if artifacts else None,
            'readback_sha256': artifacts[-1]['sha256'] if artifacts else None, 'error': value.get('error')})
        result = {'ok': value['ok'], 'state': 'verified' if value['ok'] else value['state'],
                  'code': 'repository_document_verified' if value['ok'] else 'repository_document_' + value['state'],
                  'approval': approval, 'task_root': str(task), 'artifacts': []}
        if value['ok']:
            artifact = {**artifacts[0], 'task_root': str(task), 'kind': 'document'}
            result.update(artifact=artifact, artifacts=[artifact],
                          ref=prepare_artifact_source(artifact['path'], artifact['sha256'], base))
            if prepare_repository_document(root, computer_base=computer_base, title=plan['title']) != plan:
                raise ComputerError('repository_document_source_changed')
            result = _verified_result(result, plan, base)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ComputerError) else 'repository_document_failed'
        result = {'ok': False, 'state': 'unknown' if started else 'failed', 'code': code,
                  'approval': approval, 'task_root': str(task), 'artifacts': []}
    _save(result_path, result)
    return result
