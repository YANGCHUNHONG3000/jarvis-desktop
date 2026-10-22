"""Typed, zero-model browser -> Excel -> Word goals above existing adapters."""
from copy import deepcopy
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, TTL, digest, read_record, safe_root, write_new
from .policy import source_url

TASK_BASE = Path(__file__).resolve().parents[1] / 'data' / 'assistant_tasks'
OPS = frozenset({'public_read', 'public_search', 'sources_sheet', 'sheet_document'})
HEADERS = ['标题', '来源 URL', '抓取时间（UTC）', '正文摘录（最多 1200 字）', '截取说明']
SECONDS = 240


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ComputerError('adapter_text_invalid')
    return value


def validate(steps):
    if type(steps) is not list or not 1 <= len(steps) <= 8:
        raise ComputerError('goal_step_limit')
    known = {}
    for step in steps:
        if type(step) is not dict:
            raise ComputerError('adapter_schema_invalid')
        op = step.get('op')
        fields = ({'op', 'urls', 'query', 'as'} if op == 'public_read' else
                  {'op', 'query', 'max_sources', 'as'} if op == 'public_search' else {'op', 'from', 'title', 'as'})
        if type(op) is not str or op not in OPS or set(step) != fields:
            raise ComputerError('adapter_schema_invalid')
        name = step['as']
        if type(name) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', name) or name in known:
            raise ComputerError('goal_output_invalid')
        if op == 'public_search':
            from .computer_search import validate_query
            validate_query(step['query'], step['max_sources'])
            known[name] = 'sources'
        elif op == 'public_read':
            _text(step['query'], 200)
            urls = step['urls']
            if type(urls) is not list or not 1 <= len(urls) <= 2:
                raise ComputerError('adapter_urls_invalid')
            try:
                canonical = [source_url(url) for url in urls]
            except ValueError:
                raise ComputerError('adapter_urls_invalid') from None
            if canonical != urls or len(set(urls)) != len(urls) or any(len(url) > 2000 for url in urls):
                raise ComputerError('adapter_urls_invalid')
            known[name] = 'sources'
        else:
            _text(step['title'], 200)
            if any(ord(c) < 32 for c in step['title']):
                raise ComputerError('adapter_title_invalid')
            if type(step['from']) is not str or known.get(step['from']) != ('sources' if op == 'sources_sheet' else 'spreadsheet'):
                raise ComputerError('adapter_reference_invalid')
            known[name] = 'spreadsheet' if op == 'sources_sheet' else 'document'
    return len(steps)


def template(urls, title):
    """No content is guessed in this preview: dependencies resolve at execution."""
    steps = [{'op': 'public_read', 'urls': urls, 'query': title, 'as': 'sources'},
             {'op': 'sources_sheet', 'from': 'sources', 'title': title, 'as': 'sheet'},
             {'op': 'sheet_document', 'from': 'sheet', 'title': title, 'as': 'document'}]
    validate(steps)
    return steps


def search_template(query, title, max_sources=2):
    steps = [{'op': 'public_search', 'query': query, 'max_sources': max_sources, 'as': 'sources'},
             {'op': 'sources_sheet', 'from': 'sources', 'title': title, 'as': 'sheet'},
             {'op': 'sheet_document', 'from': 'sheet', 'title': title, 'as': 'document'}]
    validate(steps)
    return steps


def propose_research_goal(session, instruction, *, client=None, stop=lambda: False):
    """One optional planning call, never authority to search or create artifacts."""
    import json
    from .model import CloudClient, ModelError
    from .budget import BudgetError
    from .computer_goals import prepare_goal
    _text(instruction, 1000)
    if stop():
        raise ComputerError('cancelled_before_model')
    claim = session.claim_model({'research_goal': instruction})
    try:
        client = client if client is not None else CloudClient(deadline=time.monotonic() + 60, should_stop=stop)
        value = client.complete(
            'Return exactly a JSON object. Plan ONLY public web research that produces a new Excel source table '
            'and a new Word source-excerpt document. Never search or supply results yourself. '
            'Supported schema: {"kind":"public_research","query":"search topic <=500 chars","title":"document title <=200 chars"}. '
            'Unsupported schema: {"kind":"unsupported","reason":"short reason <=500 chars"}. '
            'Use unsupported if the request requires logged-in sites, sending, deleting, purchases, existing-file edits, '
            'other apps, comprehensive research conclusions, or full-page copying. '
            'A supported plan reads at most 2 publicly accessible pages and copies at most 1200 characters per page '
            'with URL and capture time. It does not synthesize a research conclusion. '
            'Keep the user topic; query/title are plain single-line data, never code or file paths. '
            'A separate local review and approval is required before ANY execution.',
            json.dumps({'instruction': instruction}, ensure_ascii=False), max_tokens=512)
        if stop():
            raise ComputerError('cancelled_after_model')
        if type(value) is not dict:
            raise ComputerError('model_goal_invalid')
        if set(value) == {'kind', 'reason'} and value['kind'] == 'unsupported':
            return {'plan': None, 'reason': _text(value['reason'], 500)}
        if set(value) != {'kind', 'query', 'title'} or value['kind'] != 'public_research':
            raise ComputerError('model_goal_invalid')
        steps = search_template(value['query'], value['title'])
        return {'plan': prepare_goal(session, instruction, {}, steps),
                'reason': '仅生成待批准的公开检索与来源摘录计划；尚未检索或写入文件。'}
    except BudgetError:
        raise ComputerError('model_budget_exceeded') from None
    except ModelError as exc:
        raise ComputerError(str(exc)) from None
    finally:
        write_new(claim.with_suffix('.outcome.json'), {'call': getattr(client, 'last_call', None)})


def prepare(instruction, snapshots, steps):
    if snapshots != {}:
        raise ComputerError('adapter_windows_not_allowed')
    count = validate(steps)
    return {'schema': 2, 'mode': 'adapters', 'instruction': instruction, 'snapshots': {},
            'steps': deepcopy(steps), 'max_actions': count, 'max_seconds': SECONDS,
            'prepared_at': time.time()}


def _sources(value, run):
    path = safe_root(value['path'])
    if path.parent != run or not re.fullmatch(r'sources-[1-8]\.json', path.name):
        raise ComputerError('source_scope_invalid')
    content = read_record(path)
    if digest(content) != value['sha256']:
        raise ComputerError('source_observation_changed')
    return content


def _search_excerpt(text, query):
    pattern = r'\s+'.join(re.escape(part) for part in query.split())
    match = re.search(pattern, text, re.IGNORECASE)
    start = match.start() if match else 0
    end = min(len(text), start + 1200)
    return {'start': start, 'end': end, 'method': 'query_phrase' if match else 'page_start',
            'text': text[start:end]}


def execute(session, plan, *, approval, stop, progress, clock=time.monotonic,
            browser=None, office_execute=None, task_base=TASK_BASE, search=None):
    # Exact approval and saved plan hash are checked by execute_goal before entry.
    if (set(plan) != {'schema', 'mode', 'instruction', 'snapshots', 'steps', 'max_actions',
                     'max_seconds', 'prepared_at', 'sha256'} or plan['schema'] != 2
            or plan['mode'] != 'adapters' or plan['snapshots'] != {}
            or validate(plan['steps']) != plan['max_actions'] or plan['max_seconds'] != SECONDS):
        raise ComputerError('goal_plan_invalid')
    if type(plan['prepared_at']) not in (int, float) or not 0 <= time.time() - plan['prepared_at'] <= TTL:
        raise ComputerError('goal_preview_expired')
    run = safe_root(session.root / 'goals' / (approval + '.run'))
    try:
        run.mkdir(exist_ok=False)
    except FileExistsError:
        raise ComputerError('goal_already_claimed') from None
    write_new(run / 'claim.json', {'approval': approval, 'mode': 'adapters', 'at': time.time()})
    until = clock() + SECONDS
    outputs, completed, artifacts = {}, [], []
    source_reads, failures = 0, []
    uncertainty = False

    def reason():
        return 'goal_cancelled' if stop() else 'goal_deadline' if clock() >= until else None

    def check():
        why = reason()
        if why:
            raise ComputerError(why)

    try:
        for index, step in enumerate(plan['steps'], 1):
            check()
            op = step['op']
            progress({'step': index, 'total': len(plan['steps']), 'op': op})
            check()
            write_new(run / f'step-{index}.intent.json', step)
            if op in {'public_read', 'public_search'}:
                from .browser import BrowserProcess
                from .contracts import BrowserReadError, utc_now
                discovery = None
                urls = step.get('urls')
                if op == 'public_search':
                    from .computer_search import SearchProcess, SEARCH_TIMEOUT, validate_result
                    discovery = (search or SearchProcess()).search(step['query'], step['max_sources'],
                                should_stop=lambda: reason() is not None,
                                timeout=min(SEARCH_TIMEOUT, until - clock()))
                    check()
                    discovery = validate_result(discovery, step['max_sources'])
                    write_new(run / f'discovery-{index}.json', {'query': step['query'],
                              'max_sources': step['max_sources'], **discovery, 'sha256': digest(discovery)})
                    urls = discovery['urls']
                pages, read_failures = [], []
                for candidate, url in enumerate(urls, 1):
                    check()
                    try:
                        observation = (browser or BrowserProcess(query=step['query'])).read(
                            url, should_stop=lambda: reason() is not None)
                    except BrowserReadError as exc:
                        failure = {'url': url, 'code': exc.safe_code, 'captured_at': utc_now()}
                        write_new(run / f'read-{index}-{candidate}.json', {'ok': False, **failure})
                        failures.append(failure)
                        check()
                        if discovery is None:
                            raise
                        read_failures.append(failure)
                        continue
                    check()
                    data = observation.to_dict()
                    if (data['requested_url'] != url or data['final_url'] != url
                            or type(data['title']) is not str or len(data['title']) > 300):
                        raise ComputerError('browser_observation_invalid')
                    _text(data['text'], 12000)
                    _text(data['captured_at'], 80)
                    write_new(run / f'read-{index}-{candidate}.json', {'ok': True, 'observation': data})
                    pages.append(data)
                    source_reads += 1
                if not pages:
                    raise BrowserReadError(read_failures[0]['code'])
                value = {'kind': 'sources', 'query': step['query'], 'pages': pages}
                if discovery is not None:
                    value['discovery'] = discovery
                    value['read_failures'] = read_failures
                    value['excerpts'] = [_search_excerpt(page['text'], step['query']) for page in pages]
                path = run / f'sources-{index}.json'
                write_new(path, value)
                output = {'kind': 'sources', 'path': str(path), 'sha256': digest(value)}
            else:
                from .artifact_source import (prepare_artifact_source, prepare_artifact_task_root,
                                              snapshot_artifact_source)
                from .office_workflow import run_office_workflow
                base = safe_root(task_base)
                base.mkdir(parents=True, exist_ok=True)
                task = base / ('task-' + uuid.uuid4().hex)
                prepare_artifact_task_root(task, base)
                source = outputs[step['from']]
                write_new(task / 'goal-origin.json', {'approval': approval, 'run': str(run),
                          'step': index, 'source': source})
                with (task / 'request.txt').open('x', encoding='utf-8') as f:
                    f.write(plan['instruction'])
                write_new(task / 'events.jsonl', {'event': 'goal_step_started', 'at': time.time(), 'op': op})
                if op == 'sources_sheet':
                    content = _sources(source, run)
                    flat = lambda value: re.sub(r'[\r\n\t]', ' ', value)
                    rows = [[flat(p['title']), p['final_url'], p['captured_at'], flat(p['text'][:1200]),
                             f"浏览器摘录 {len(p['text'])} 字；此表保留前 {min(len(p['text']), 1200)} 字，换行/制表符转空格；不代表完整网页"]
                            for p in content['pages']]
                    headers = HEADERS
                    if 'discovery' in content:
                        headers = HEADERS + ['读取状态']
                        for row, excerpt in zip(rows, content['excerpts']):
                            row[3] = flat(excerpt['text'])
                            method = '完整检索词定位' if excerpt['method'] == 'query_phrase' else '未命中完整检索词，使用页首'
                            row[4] = (f"{method}；原文零基区间 [{excerpt['start']}, {excerpt['end']})；"
                                      '最多 1200 字，换行/制表符转空格；不代表完整网页或语义相关性保证')
                        rows = [row + ['已读取'] for row in rows]
                        rows.extend([['未读取的候选', item['url'], item['captured_at'],
                                      '', '未获得正文；未用于内容；读取失败：' + item['code'], '读取失败']
                                     for item in content['read_failures']])
                    spec = {'kind': 'spreadsheet', 'title': step['title'],
                            'headers': headers, 'rows': rows, 'sum_columns': []}
                else:
                    # Copy and revalidate actual saved Excel bytes, not a proposed spec.
                    snapshot = snapshot_artifact_source(source['ref'], task, base)
                    if snapshot['kind'] != 'table' or snapshot['coverage']['derived_totals']:
                        raise ComputerError('adapter_source_kind_invalid')
                    spec = {'kind': 'document', 'title': step['title'],
                            'paragraphs': ['以下为公开网页来源摘录，来自已保存并核验的 Excel。未调用模型归纳，不代表完整网页。'],
                            'table': snapshot['observed']}
                check()
                from .office import validate_spec
                spec = validate_spec(spec)  # Reject malformed data before any native intent.
                workflow = task / ('workflow-' + uuid.uuid4().hex)
                # The adapter owns these new documents; no selected user window is edited.
                uncertainty = True
                result = run_office_workflow([{'id': 's1', 'action': spec['kind'], 'data': spec}],
                          workflow, execute=office_execute, deadline=until,
                          cancel_requested=lambda: reason() is not None)
                records = [{'step': key, 'path': receipt['artifact'], 'sha256': receipt['sha256']}
                           for key, receipt in result.get('receipts', {}).items()]
                parent = {'root': str(task), 'kind': 'office', 'ok': result['ok'], 'state': result['state'],
                          'workflow_root': str(workflow), 'completed': result['completed'], 'artifacts': records,
                          'artifact': records[-1]['path'] if records else None,
                          'readback_sha256': records[-1]['sha256'] if records else None, 'error': result.get('error')}
                write_new(task / 'result.json', parent)
                if not result['ok']:
                    uncertainty = result['state'] == 'unknown'
                    raise ComputerError(reason() or 'office_' + result['state'])
                record = records[0]
                ref = prepare_artifact_source(record['path'], record['sha256'], base)
                from .task_history import inspect_task
                inspection = inspect_task(task, base)
                if inspection['state'] != 'succeeded' or not any(
                        item['path'] == record['path'] and item['sha256'] == record['sha256']
                        for item in inspection['artifacts']):
                    raise ComputerError('adapter_artifact_verification_failed')
                output = {'kind': spec['kind'], 'ref': ref}
                artifacts.append({**record, 'task_root': str(task), 'kind': spec['kind']})
                uncertainty = False
            outputs[step['as']] = output
            entry = {'step': index, 'op': op, 'state': 'verified', 'output': output}
            write_new(run / f'step-{index}.json', entry)
            completed.append(entry)
            check()
        outcome = {'ok': True, 'state': 'verified', 'code': 'approved_plan_verified'}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ComputerError) else 'adapter_failed'
        from .contracts import BrowserReadError
        if isinstance(exc, BrowserReadError):
            code = exc.safe_code
        outcome = {'ok': False, 'state': 'unknown' if uncertainty else 'partial' if completed else 'stopped',
                   'code': reason() or code}
    outcome.update(mode='adapters', completed=completed, outputs=outputs, artifacts=artifacts,
                   actions=len(completed), run=str(run), source_reads=source_reads, failures=failures)
    write_new(run / 'outcome.json', outcome)
    return outcome
