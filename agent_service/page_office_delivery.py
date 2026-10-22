"""Saved public search evidence to two owned local Office artifacts.

Claims live outside the browser run so its immutable evidence stays unchanged.
Every repeated call rechecks source and output bytes, without repeating effects.
An interrupted delivery remains partial; it is never silently resumed here.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import uuid

from .computer import BASE, ComputerError, ComputerSession, digest, read_record, safe_root, write_new
from .computer_agent_tools import _load, _save
from .computer_goal_adapters import TASK_BASE


def _harmless_failures(outcome):
    """Accept only ordered summaries of independently verified no-effect stales."""
    expected = []
    for attempt in outcome['attempts']:
        action, result = attempt['action'], attempt['result']
        if result.get('ok') is True:
            continue
        if (result.get('ok') is not False
                or action.get('kind') not in {'browser_fill','browser_click','browser_wait'}
                or result.get('code') != 'browser_action_stale'
                or result.get('state') != 'failed'
                or result.get('verification', {}).get('effect_dispatched') is not False):
            return False
        expected.append({'kind':action['kind'], 'code':result['code']})
    return outcome.get('failures') == expected


def verify_page_source(run, *, computer_base=BASE):
    """Return bounded saved text bound to the complete successful evidence chain."""
    from .interaction_recovery import verify_reading
    from .browser_checkpoint import verify_browser
    try:
        root, base = safe_root(run), safe_root(computer_base)
        session = root.parent.parent
        if (not re.fullmatch(r'[0-9a-f]{64}\.run', root.name)
                or root.parent != session/'goals' or session.parent != base
                or not re.fullmatch(r'computer-[0-9a-f]{32}', session.name)
                or read_record(session/'session.json').get('schema') != 1):
            raise ValueError()
        owner = ComputerSession(session)
        verified = verify_reading(owner, root)
        goal = verified['goal']
        config = goal['browser']
        checkpoint = verified['browser_checkpoint']
        browser = checkpoint['browser']
        verify_browser(config, browser)
        discovery = browser['discovery']
        outcome = read_record(root/'outcome.json')
        if (goal.get('kind') != 'interactive' or goal.get('page_office') is not True
                or config.get('mode') != 'public_search'
                or discovery.get('mode') != 'public_search'
                or any(discovery.get(key) is not True for key in ('query_verified','content_verified'))
                or browser.get('url') != discovery.get('target_url')
                or browser.get('actions') != [] or not _harmless_failures(outcome)
                or outcome.get('goal_kind') != 'interactive'):
            raise ValueError()
        text, title = browser['text'], browser['title']
        if (type(text) is not str or not text.strip() or len(text) > 24000
                or type(title) is not str or len(title) > 2000
                or any(term not in text for term in config['required_text'])):
            raise ValueError()
        title = title.strip() or browser['url']
        metadata = {'version':1, 'session':str(session), 'run':str(root),
            'plan_sha256':root.stem, 'plan_file_sha256':sha256(root.with_suffix('.json').read_bytes()).hexdigest(),
            'session_sha256':sha256((session/'session.json').read_bytes()).hexdigest(),
            'evidence_sha256':verified['evidence_sha256'], 'checkpoint_sha256':digest(checkpoint),
            'captured_at':checkpoint['captured_at'], 'captured_run':checkpoint['run'],
            'config':deepcopy(config), 'url':browser['url'], 'title':title,
            'body_chars':len(text), 'body_sha256':sha256(text.encode('utf-8')).hexdigest()}
        if verify_reading(owner, root) != verified:
            raise ValueError()
        return {'metadata':metadata, 'source_sha256':digest(metadata), 'text':text,
                'artifacts':verified['artifacts']}
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        raise ComputerError('page_office_source_invalid') from None


def _steps(source):
    from .office import validate_spec
    from .repository_document import _parts
    meta = source['metadata']
    parts = _parts(source['text'])
    # Extracts preserve order and offsets; they make no semantic claims.
    rows = [['来源', '', '', meta['url']], ['检索词', '', '', meta['config']['query']],
            ['页面标题', '', '', meta['title']],
            ['阅读范围', 0, meta['body_chars'], '本次保存的有界可见文字；分段摘要为原文摘录。']]
    rows.extend(['原文摘录', part['start'], part['end'], part['text']] for part in parts)
    paragraphs = ['来源页面：'+meta['url'], '实际检索词：'+meta['config']['query'],
        '页面标题：'+meta['title'],
        f"阅读范围：本次保存的可见文字 {meta['body_chars']} 字符；页面采集最多保留 24000 字符。",
        '以下按原顺序完整保留本次读取的有界文字；换行和制表符转为空格。']
    procurement = meta['config']['site'] == 'ikea_us'
    if procurement:
        # Source-specific labels, not invented product attributes or a purchase.
        rows.append(['采购核对状态', '', '', '待人工核对'])
        paragraphs += ['采购核对状态：待人工核对',
            '候选来自 IKEA 美国站；价格、税费、运费及库存须按购买时页面核对。本任务未下单。']
    paragraphs.extend(part['text'] for part in parts)
    return [{'id':'s1', 'action':'spreadsheet', 'data':validate_spec({
                'kind':'spreadsheet', 'title':'办公采购候选台账' if procurement else '网页来源与分段摘要',
                'headers':['类别','起始字符','结束字符','内容'], 'rows':rows})},
            {'id':'s2', 'action':'document', 'data':validate_spec({
                'kind':'document', 'title':'办公采购候选资料' if procurement else '网页阅读资料',
                'paragraphs':paragraphs})}]


def _claim_check(claim, source, task_base):
    task = safe_root(claim['task_root'])
    if (set(claim) != {'version','source_sha256','task_base','task_root','workflow_root','steps_sha256'}
            or claim['version'] != 1 or claim['source_sha256'] != source['source_sha256']
            or claim['task_base'] != str(task_base) or task.parent != task_base
            or not re.fullmatch(r'task-[0-9a-f]{32}', task.name)
            or Path(claim['workflow_root']).parent != task
            or not re.fullmatch(r'workflow-[0-9a-f]{32}', Path(claim['workflow_root']).name)
            or claim['steps_sha256'] != digest(_steps(source))):
        raise ComputerError('page_office_claim_changed')
    safe_root(claim['workflow_root'])
    return task


def _verify_result(report, claim, source, task_base):
    """Rebuild the artifact list from original task receipts and actual files."""
    from .artifact_source import prepare_artifact_source
    from .office import verify_office_file, _expected, _compare, _public_observation
    from .office_workflow import _resume_preflight, _hash
    task = _claim_check(claim, source, task_base)
    steps, workflow = _steps(source), safe_root(claim['workflow_root'])
    if (type(report) is not dict or set(report) != {
            'ok','state','code','source_sha256','task_root','artifacts','refs'}
            or type(report['ok']) is not bool or report['source_sha256'] != source['source_sha256']
            or report['task_root'] != str(task)
            or report['state'] not in {'verified','partial','unknown','failed'}
            or report['ok'] != (report['state'] == 'verified')
            or report['code'] != 'page_office_'+report['state']):
        raise ComputerError('page_office_result_changed')
    if not report['artifacts'] and not report['ok']:
        if report['refs'] != []:
            raise ComputerError('page_office_result_changed')
        return deepcopy(report)
    origin, _ = _load(task/'page-origin.json')
    saved, _ = _load(task/'page-source.json')
    if origin != claim or saved != source:
        raise ComputerError('page_office_result_changed')
    if read_record(workflow/'manifest.json') != {'version':1,'steps':steps,'steps_sha256':_hash(steps)}:
        raise ComputerError('page_office_result_changed')
    receipts, unknown = _resume_preflight(steps, workflow)
    if unknown is not None or (report['ok'] and list(receipts) != ['s1','s2']):
        raise ComputerError('page_office_result_changed')
    artifacts, refs = [], []
    for step in steps:
        if step['id'] not in receipts:
            continue
        receipt = receipts[step['id']]
        spec, path = step['data'], safe_root(receipt['artifact'])
        if (receipt['observed'] != _public_observation(spec, _expected(spec))
                or not _compare(verify_office_file(path, spec), _expected(spec))):
            raise ComputerError('page_office_result_changed')
        artifacts.append({'step':step['id'], 'path':str(path), 'sha256':receipt['sha256'],
                          'task_root':str(task), 'kind':step['action']})
        refs.append(prepare_artifact_source(path, receipt['sha256'], task_base))
    if report['artifacts'] != artifacts or report['refs'] != refs:
        raise ComputerError('page_office_result_changed')
    return deepcopy(report)


def _deliver(source, *, computer_base, task_base, stop, office_execute):
    from .artifact_source import prepare_artifact_task_root, prepare_artifact_source
    from .office_workflow import run_office_workflow
    base = safe_root(task_base)
    run = Path(source['metadata']['run'])
    delivery = safe_root(Path(source['metadata']['session'])/'page-deliveries'/run.stem)
    claim_path, result_path = delivery/'claim.json', delivery/'result.json'
    if claim_path.exists():
        claim = read_record(claim_path)
        task = _claim_check(claim, source, base)
        if result_path.exists():
            return _verify_result(_load(result_path)[0], claim, source, base)
        return {'ok':False, 'state':'unknown', 'code':'page_office_unknown',
                'source_sha256':source['source_sha256'], 'task_root':str(task), 'artifacts':[], 'refs':[]}
    if result_path.exists():
        raise ComputerError('page_office_claim_changed')
    if stop():
        return {'ok':False, 'state':'partial', 'code':'page_office_partial',
                'source_sha256':source['source_sha256'], 'task_root':None,'artifacts':[], 'refs':[]}
    steps = _steps(source)
    task = base/('task-'+uuid.uuid4().hex)
    workflow = task/('workflow-'+uuid.uuid4().hex)
    claim = {'version':1, 'source_sha256':source['source_sha256'], 'task_base':str(base),
             'task_root':str(task), 'workflow_root':str(workflow), 'steps_sha256':digest(steps)}
    delivery.mkdir(parents=True, exist_ok=True)
    write_new(claim_path, claim)
    started = False
    try:
        prepare_artifact_task_root(task, base)
        _save(task/'page-origin.json', claim)
        _save(task/'page-source.json', source)
        (task/'request.txt').write_text('将已核验网页资料保存为 Excel 与 Word',encoding='utf-8')
        write_new(task/'events.jsonl', {'event':'page_office_started'})
        def guard():
            if verify_page_source(run, computer_base=computer_base) != source:
                raise ComputerError('page_office_source_changed')
        def execute(spec, step_root, should_stop):
            guard()
            if office_execute is None:
                from .office_host import execute_office_supervised
                executor = execute_office_supervised
            else:
                executor = office_execute
            value = executor(spec, step_root, should_stop)
            guard()
            return value
        guard()
        started = True
        value = run_office_workflow(steps, workflow, execute=execute,
                                   cancel_requested=stop, resume_guard=guard)
        raw = [{'step':key,'path':receipt['artifact'],'sha256':receipt['sha256']}
               for key,receipt in value['receipts'].items()]
        write_new(task/'result.json', {'root':str(task),'kind':'office','ok':value['ok'],
            'state':value['state'],'workflow_root':str(workflow),'completed':value['completed'],
            'artifacts':raw,'artifact':raw[-1]['path'] if raw else None,
            'readback_sha256':raw[-1]['sha256'] if raw else None,'error':value.get('error')})
        state = 'verified' if value['ok'] else 'unknown' if value['state']=='unknown' else 'partial'
        artifacts, refs = [], []
        if state != 'unknown':
            for item, step in zip(raw, steps):
                artifacts.append({**item,'task_root':str(task),'kind':step['action']})
                refs.append(prepare_artifact_source(item['path'],item['sha256'],base))
        report = {'ok':value['ok'],'state':state,'code':'page_office_'+state,
            'source_sha256':source['source_sha256'],'task_root':str(task),'artifacts':artifacts,'refs':refs}
        guard()
        report = _verify_result(report,claim,source,base)
    except Exception:
        state = 'unknown' if started else 'failed'
        report = {'ok':False,'state':state,'code':'page_office_'+state,
            'source_sha256':source['source_sha256'],'task_root':str(task),'artifacts':[],'refs':[]}
    _save(result_path,report)
    return report


def deliver_page_office(reading, *, stop=lambda:False, progress=lambda value:None,
                        computer_base=BASE, task_base=TASK_BASE, office_execute=None):
    """Append verified Excel and Word artifacts; preserve the original browser run."""
    result = deepcopy(reading)
    try:
        source = verify_page_source(reading['run'],computer_base=computer_base)
        if (reading.get('ok') is not True or reading.get('state') != 'verified'
                or reading.get('artifacts') != source['artifacts']):
            raise ComputerError('page_office_reading_changed')
        progress({'op':'page_office','message':'网页已读取，正在本机生成并核验 Excel 与 Word。'})
        report = _deliver(source, computer_base=computer_base, task_base=task_base,
                          stop=stop, office_execute=office_execute)
    except Exception as exc:
        report = {'ok':False,'state':'partial','code':str(exc) if isinstance(exc,ComputerError)
                  else 'page_office_failed','artifacts':[],'refs':[]}
    result['page_office_report'] = report
    result['artifacts'] = deepcopy(reading.get('artifacts',[]))+report['artifacts']
    result['code'] = report['code']
    if not report['ok']:
        result.update(ok=False,state='unknown' if report['state']=='unknown' else 'partial')
    return result
