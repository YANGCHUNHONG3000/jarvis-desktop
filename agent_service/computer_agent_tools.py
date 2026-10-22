"""Durable observations and bounded tools for the approved research agent."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new
from .computer_goal_adapters import HEADERS, TASK_BASE, _search_excerpt
from .computer_search import SEARCH_TIMEOUT, SearchProcess, validate_result
from .contracts import BrowserReadError, utc_now


def _save(path, data):
    write_new(path, {'data': data, 'sha256': digest(data)})


def _load(path):
    try:
        value = read_record(path)
        if set(value) != {'data', 'sha256'} or digest(value['data']) != value['sha256']:
            raise ValueError()
        return value['data'], value['sha256']
    except (OSError, ValueError, TypeError, KeyError):
        raise ComputerError('agent_evidence_changed') from None


def _selection(text, query, terms):
    if not terms:
        return _search_excerpt(text, query)
    matches = [re.search(re.escape(term), text, re.IGNORECASE) for term in terms]
    if any(match is None for match in matches):
        return None
    start = min(match.start() for match in matches)
    if max(match.end() for match in matches) - start > 1200:
        return None
    end = min(len(text), start + 1200)
    return {'start': start, 'end': end, 'method': 'required_terms', 'text': text[start:end]}


class AgentEnvironment:
    def __new__(cls, goal, *args, **kwargs):
        if (cls is AgentEnvironment and type(goal) is dict
                and goal.get('comparison', {}).get('evidence_version') == 4):
            from .comparison_crosssite import CrossSiteEnvironment
            return object.__new__(CrossSiteEnvironment)
        if (cls is AgentEnvironment and type(goal) is dict
                and goal.get('comparison', {}).get('evidence_version') == 3):
            from .comparison_completion import CompletionEnvironment
            return object.__new__(CompletionEnvironment)
        return object.__new__(cls)

    def __init__(self, goal, run, *, stop, deadline, clock=time.monotonic,
                 search=None, browser=None, office_execute=None, task_base=TASK_BASE,
                 recovery_validator=None, comparison_select=None, comparison_review=None):
        self.goal, self.run = deepcopy(goal), safe_root(run)
        self.stop, self.deadline, self.clock = stop, deadline, clock
        self.search, self.browser, self.office_execute = search, browser, office_execute
        self.task_base = safe_root(task_base)
        self.recovery_validator = recovery_validator
        self.comparison_select = comparison_select
        self.comparison_review = comparison_review
        self.record_origins = {}
        if not self.run.is_dir():
            raise ComputerError('agent_run_invalid')
        path = self.run / 'environment-goal.json'
        if path.exists():
            if _load(path)[0] != self.goal:
                raise ComputerError('agent_goal_changed')
        else:
            _save(path, self.goal)

    def _check(self):
        if self.stop():
            raise ComputerError('goal_cancelled')
        if self.clock() >= self.deadline:
            raise ComputerError('goal_deadline')

    def _records(self):
        seed = self.recovery_validator() if self.recovery_validator else {}
        if _load(self.run / 'environment-goal.json')[0] != self.goal:
            raise ComputerError('agent_goal_changed')
        records = deepcopy(seed.get('records', []))
        self.record_origins = deepcopy(seed.get('origins', {}))
        for directory in sorted(self.run.iterdir()):
            if not directory.is_dir():
                continue
            intent_path = directory / 'environment-intent.json'
            result_path = directory / 'environment-result.json'
            if not intent_path.exists() and not result_path.exists():
                continue
            if safe_root(directory).parent != self.run:
                raise ComputerError('agent_evidence_changed')
            intent, intent_hash = _load(intent_path)
            if not result_path.exists():
                raise ComputerError('agent_interrupted_action')
            record, record_hash = _load(result_path)
            if record.get('intent_sha256') != intent_hash or record.get('action') != intent['action']:
                raise ComputerError('agent_evidence_changed')
            records.append((record, record_hash))
            self.record_origins[record_hash] = {'run': str(self.run), 'step': str(directory)}
        return records

    @staticmethod
    def _accepted(candidates, reads):
        return [(item, reads[item['id']]) for item in candidates
                if item['id'] in reads and reads[item['id']]['result']['ok']]

    def _ready(self, candidates, reads):
        from .source_comparison import sources_ready
        return sources_ready(self.goal, self._accepted(candidates, reads))

    def _facts(self):
        records = self._records()
        queries, candidates, reads, failures, artifacts, done = [], [], {}, [], [], []
        office_started = office_blocked = False
        comparison = None
        for position, (record, record_hash) in enumerate(records):
            action, result = record['action'], record['result']
            kind = action['kind']
            ready = self._ready(candidates, reads)
            if (type(result.get('ok')) is not bool or result.get('state') not in {'verified', 'failed', 'unknown'}
                    or result['ok'] != (result['state'] == 'verified')
                    or type(result.get('code')) is not str
                    or kind in {'search', 'read'} and (office_started or ready)):
                raise ComputerError('agent_evidence_changed')
            if kind == 'search':
                index = action['query_index']
                if (set(action) != {'kind', 'query_index'} or type(index) is not int
                        or not 0 <= index < len(self.goal['queries']) or index in queries):
                    raise ComputerError('agent_evidence_changed')
                queries.append(index)
                if result['ok']:
                    discovery = validate_result(record['discovery'], 4 if 'comparison' in self.goal else 2)
                    for url in discovery['urls']:
                        if not any(item['url'] == url for item in candidates):
                            candidates.append({'id': digest(url)[:24], 'url': url, 'query_index': index})
                else:
                    failures.append({'kind': kind, 'query_index': index, 'code': result['code'], 'captured_at': record['at']})
            elif kind == 'read':
                if set(action) != {'kind', 'candidate_id'}:
                    raise ComputerError('agent_evidence_changed')
                matches = [item for item in candidates if item['id'] == action['candidate_id']]
                if len(matches) != 1 or action['candidate_id'] in reads:
                    raise ComputerError('agent_evidence_changed')
                candidate = matches[0]
                if result['ok'] and 'observation' not in record:
                    raise ComputerError('agent_evidence_changed')
                if 'observation' in record:
                    data = record['observation']
                    self._validate_page(data, candidate['url'])
                    expected = _selection(data['text'], self.goal['queries'][candidate['query_index']], self.goal['required_terms'])
                    if expected != record.get('excerpt') or result['ok'] != (expected is not None):
                        raise ComputerError('agent_evidence_changed')
                reads[action['candidate_id']] = record
                if not result['ok']:
                    failures.append({'kind': kind, 'url': candidate['url'], 'code': result['code'], 'captured_at': record['at']})
            elif kind == 'compare':
                from .source_comparison import verify_evidence, review_failure
                if (set(action) != {'kind'} or 'comparison' not in self.goal or not ready
                        or comparison is not None or office_started or office_blocked):
                    raise ComputerError('agent_evidence_changed')
                if not result['ok']:
                    if 'comparison' in record:
                        rejected = verify_evidence(self.goal, self._accepted(candidates, reads), record['comparison'])
                        if review_failure(rejected) != result['code']:
                            raise ComputerError('agent_evidence_changed')
                    office_blocked = True
                    failures.append({'kind':kind,'code':result['code'],'captured_at':record['at']})
                    continue
                checked = verify_evidence(self.goal, self._accepted(candidates, reads), record['comparison'])
                if review_failure(checked):
                    raise ComputerError('agent_evidence_changed')
                comparison = record['comparison']
            elif kind in {'spreadsheet', 'document'}:
                if (set(action) != {'kind'} or kind not in self.goal['outputs'] or not ready
                        or kind in done or office_blocked or 'comparison' in self.goal and comparison is None
                        or kind == 'document' and 'spreadsheet' in self.goal['outputs'] and 'spreadsheet' not in done):
                    raise ComputerError('agent_evidence_changed')
                office_started = True
                if not result['ok']:
                    office_blocked = True
                    continue
                self._verify_artifact(record)
                self._verify_origin(record, records[:position], self.record_origins[record_hash])
                artifacts.append(record['artifact'])
                done.append(kind)
            else:
                raise ComputerError('agent_evidence_changed')
        if len(candidates) > (8 if 'comparison' in self.goal else 4) or len(queries) > 2:
            raise ComputerError('agent_evidence_changed')
        accepted = self._accepted(candidates, reads)
        return dict(records=records, queries=queries, candidates=candidates, reads=reads,
                    accepted=accepted, failures=failures, artifacts=artifacts, done=done,
                    office_started=office_started, office_blocked=office_blocked, comparison=comparison)

    @staticmethod
    def _validate_page(data, url):
        try:
            if (set(data) != {'requested_url', 'final_url', 'title', 'text', 'captured_at', 'sha256'}
                    or data['requested_url'] != url or data['final_url'] != url
                    or type(data['title']) is not str or len(data['title']) > 300
                    or type(data['text']) is not str or not data['text'].strip() or len(data['text']) > 12000
                    or data['sha256'] != sha256(data['text'].encode('utf-8')).hexdigest()
                    or type(data['captured_at']) is not str or len(data['captured_at']) > 80
                    or datetime.fromisoformat(data['captured_at']).utcoffset() is None
                    or any(ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127
                           or 0xD800 <= ord(c) <= 0xDFFF for c in data['text'])):
                raise ValueError()
        except (TypeError, ValueError, KeyError):
            raise ComputerError('browser_observation_invalid') from None

    def observe(self):
        self._check()
        return self.observe_final()

    def observe_final(self):
        """Revalidate durable facts after stopping; grants no execution authority."""
        facts = self._facts()
        ready = self._ready(facts['candidates'], facts['reads'])
        return {'artifacts': deepcopy(facts['artifacts']), 'source_reads': len(facts['accepted']),
                'failures': deepcopy(facts['failures']), 'searched_queries': facts['queries'],
                'candidates': [{**item, 'status': 'unread' if item['id'] not in facts['reads'] else
                                'verified' if facts['reads'][item['id']]['result']['ok'] else 'failed'}
                               for item in facts['candidates']],
                'sources': [{'url': item['url'], 'title': record['observation']['title'],
                             'captured_at': record['observation']['captured_at'],
                             'freshness': 'historical_capture',
                             'excerpt_preview': record['excerpt']['text'][:180],
                             'start': record['excerpt']['start'], 'end': record['excerpt']['end'],
                             'terms_match': True} for item, record in facts['accepted']],
                'office_started': facts['office_started'], 'office_blocked': facts['office_blocked'],
                'outputs_done': facts['done'],
                **({'comparison_done':facts['comparison'] is not None,
                    'comparison_review':('semantic_and_provenance_v2' if self.goal['comparison'].get('evidence_version') == 2
                                         else 'legacy_literal_only_unreviewed')}
                   if 'comparison' in self.goal else {}),
                'evidence_sha256': digest([value for _, value in facts['records']]),
                'checks': {'sources_ready': ready, 'goal_met': ready and not facts['office_blocked']
                           and ('comparison' not in self.goal or facts['comparison'] is not None)
                           and all(kind in facts['done'] for kind in self.goal['outputs'])}}

    def actions(self, observation):
        current = self.observe()
        if observation != current:
            raise ComputerError('agent_observation_changed')
        if current['checks']['goal_met'] or current['office_blocked']:
            return []
        if current['checks']['sources_ready']:
            if 'comparison' in self.goal and not current['comparison_done']:
                return [{'kind':'compare'}]
            result = []
            for kind in self.goal['outputs']:
                if kind in current['outputs_done']:
                    continue
                if kind == 'document' and 'spreadsheet' in self.goal['outputs'] and 'spreadsheet' not in current['outputs_done']:
                    continue
                result.append({'kind': kind})
            return result
        if current['office_started']:
            return []
        if 'comparison' in self.goal:
            from .source_comparison import site_key
            pending = [{'kind':'search','query_index':i} for i in range(len(self.goal['queries']))
                       if i not in current['searched_queries']]
            unread = [item for item in current['candidates'] if item['status']=='unread']
            hosts = {site_key(item['url']) for item in current['sources']}
            if len(hosts) < self.goal['comparison']['min_domains']:
                novel = [item for item in unread if site_key(item['url']) not in hosts]
                if novel:
                    unread = novel
                elif pending:
                    return pending[:1]
            if unread:
                return [{'kind':'read','candidate_id':item['id']} for item in unread]
            return pending[:1]
        return ([{'kind': 'search', 'query_index': i} for i in range(len(self.goal['queries']))
                 if i not in current['searched_queries']] +
                [{'kind': 'read', 'candidate_id': item['id']} for item in current['candidates'] if item['status'] == 'unread'])

    def _verify_artifact(self, record):
        from .artifact_source import prepare_artifact_source
        from .task_history import inspect_task
        from . import office
        artifact, ref, spec = record['artifact'], record['ref'], record['spec']
        try:
            fresh = prepare_artifact_source(artifact['path'], artifact['sha256'], self.task_base)
            inspection = inspect_task(artifact['task_root'], self.task_base)
            actual = office.verify_office_file(Path(artifact['path']), spec)
            if fresh != ref or inspection['state'] != 'succeeded' or not office._compare(actual, office._expected(spec)):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise ComputerError('agent_artifact_changed') from None

    def _verify_origin(self, record, prefix, origin):
        """Bind Office bytes and snapshots to the actual captured-source prefix."""
        from . import artifact_source, office
        task = safe_root(record['artifact']['task_root'])
        try:
            if _load(task / 'goal-origin.json')[0] != {
                    'goal_sha256': digest(self.goal), **origin,
                    'evidence_sha256': digest([h for _, h in prefix])}:
                raise ValueError()
            # Rebuild the input table without opening or refreshing any webpage.
            table = self._table(self._origin_facts(prefix))
            kind = record['action']['kind']
            if kind == 'spreadsheet':
                expected = {'kind': kind, 'title': self.goal['title'], **table, 'sum_columns': []}
            else:
                expected = {'kind': kind, 'title': self.goal['title'],
                            'paragraphs': self._paragraphs(), 'table': table}
                if 'spreadsheet' in self.goal['outputs']:
                    prior = next(row for row, _ in prefix if row['action']['kind'] == 'spreadsheet')
                    sources = task / 'sources'
                    ref, snapshot, receipt, index = artifact_source._verified(
                        prior['artifact']['path'], prior['artifact']['sha256'], self.task_base)
                    source_file = sources / 'source.xlsx'
                    if (ref != prior['ref'] or sha256(source_file.read_bytes()).hexdigest() != ref['sha256']
                            or read_record(sources / 'provenance.json') != {
                                'kind': 'verified_generated_office_artifact', **ref}):
                        raise ValueError()
                    source_kind, observed, coverage = artifact_source._extract(snapshot, receipt, index, source_file)
                    if (source_kind != 'table' or observed != table or coverage['derived_totals']
                            or read_record(sources / 'source.json') != {
                                'source_id': 'selected', 'kind': source_kind, 'sha256': ref['sha256'],
                                'bytes': source_file.stat().st_size, 'display_name': source_file.name,
                                'coverage': coverage, 'observed': observed}):
                        raise ValueError()
            if record['spec'] != office.validate_spec(expected):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, StopIteration):
            raise ComputerError('agent_artifact_changed') from None

    def _origin_facts(self, prefix):
        candidates, reads, failures, comparison = [], {}, [], None
        for prior, _ in prefix:
            action, result = prior['action'], prior['result']
            if action['kind'] == 'search' and result['ok']:
                for url in prior['discovery']['urls']:
                    if not any(item['url'] == url for item in candidates):
                        candidates.append({'url': url, 'id': digest(url)[:24]})
            elif action['kind'] == 'read':
                reads[action['candidate_id']] = prior
                if not result['ok']:
                    candidate = next(item for item in candidates if item['id'] == action['candidate_id'])
                    failures.append({'kind': 'read', 'url': candidate['url'],
                                     'captured_at': prior['at'], 'code': result['code']})
            elif action['kind'] == 'compare' and result['ok']:
                comparison = prior['comparison']
        return {'accepted': [(item, reads[item['id']]) for item in candidates
                             if item['id'] in reads and reads[item['id']]['result']['ok']],
                'failures': failures, 'comparison': comparison}

    def _paragraphs(self):
        if 'comparison' in self.goal:
            from .source_comparison import introduction
            return [introduction(self.goal)]
        return ['公开来源实际摘录；不代表完整网页或研究结论。']

    def _table(self, facts):
        if 'comparison' in self.goal:
            from .source_comparison import table
            return table(self.goal, facts['accepted'], facts['comparison'])
        flat = lambda value: re.sub(r'[\r\n\t]', ' ', value)
        rows = [[flat(record['observation']['title']), item['url'], record['observation']['captured_at'],
                 flat(record['excerpt']['text']),
                 f"原文[{record['excerpt']['start']},{record['excerpt']['end']})；换行/制表符转空格；非完整网页", '已读取']
                for item, record in facts['accepted']]
        rows += [['未用于内容的候选', item['url'], item['captured_at'], '',
                  '未用于内容：' + item['code'], '读取失败'] for item in facts['failures'] if item['kind'] == 'read']
        return {'headers': HEADERS + ['读取状态'], 'rows': rows}

    def execute(self, action, step_root):
        self._check()
        observation = self.observe()
        if not any(digest(action) == digest(candidate) for candidate in self.actions(observation)):
            raise ComputerError('agent_action_not_available')
        step_root = safe_root(step_root)
        if step_root.parent != self.run or not step_root.is_dir():
            raise ComputerError('agent_step_scope_invalid')
        facts = self._facts()
        intent = {'action': deepcopy(action), 'observation_sha256': digest(observation)}
        try:
            _save(step_root / 'environment-intent.json', intent)
        except FileExistsError:
            raise ComputerError('agent_action_already_claimed') from None
        record = {'action': deepcopy(action), 'intent_sha256': digest(intent), 'at': utc_now()}
        kind = action['kind']
        self._check()
        if kind == 'search':
            try:
                value = (self.search or SearchProcess(provider='bing' if 'comparison' in self.goal else None,
                                                        comparison='comparison' in self.goal)).search(
                    self.goal['queries'][action['query_index']], 4 if 'comparison' in self.goal else 2,
                    should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                    timeout=min(SEARCH_TIMEOUT, self.deadline - self.clock()))
                self._check()
                record['discovery'] = validate_result(value, 4 if 'comparison' in self.goal else 2)
                result = {'ok': True, 'state': 'verified', 'code': 'search_discovered'}
            except ComputerError as exc:
                if str(exc) not in {'search_timeout', 'search_unavailable'}:
                    self._check()
                    raise
                result = {'ok': False, 'state': 'failed', 'code': str(exc)}
        elif kind == 'read':
            candidate = next(item for item in facts['candidates'] if item['id'] == action['candidate_id'])
            from .browser import BrowserProcess
            try:
                value = (self.browser or BrowserProcess(query=self.goal['queries'][candidate['query_index']])).read(
                    candidate['url'], should_stop=lambda: self.stop() or self.clock() >= self.deadline)
                self._check()
                data = value.to_dict()
                self._validate_page(data, candidate['url'])
                excerpt = _selection(data['text'], self.goal['queries'][candidate['query_index']], self.goal['required_terms'])
                record.update(observation=data, excerpt=excerpt)
                result = {'ok': excerpt is not None, 'state': 'verified' if excerpt is not None else 'failed',
                          'code': 'source_verified' if excerpt is not None else 'quality_rejected'}
            except BrowserReadError as exc:
                result = {'ok': False, 'state': 'failed', 'code': exc.safe_code}
        elif kind == 'compare':
            from .source_comparison import build_context, create_evidence, review_context, review_failure
            try:
                if self.comparison_select is None:
                    raise ComputerError('comparison_model_missing')
                response = self.comparison_select(build_context(self.goal, facts['accepted']))
                review = None
                if self.goal['comparison'].get('evidence_version') == 2:
                    if self.comparison_review is None:
                        raise ComputerError('comparison_review_missing')
                    review = self.comparison_review(review_context(self.goal, facts['accepted'], response))
                record['comparison'] = create_evidence(self.goal, facts['accepted'], response, review)
                failure = review_failure(record['comparison'])
                result = {'ok':failure is None,'state':'failed' if failure else 'verified',
                          'code':failure or 'comparison_verified'}
            except ComputerError as exc:
                result = {'ok':False,'state':'failed','code':str(exc)}
        else:
            result = self._office(action, step_root, facts, record)
        record['result'] = result
        _save(step_root / 'environment-result.json', record)
        # Let the engine durably acknowledge known Office results before it
        # classifies a simultaneous stop. New actions still check at entry.
        if kind not in {'spreadsheet', 'document', 'compare'}:
            self._check()
        return result

    def _office(self, action, step_root, facts, record):
        from .artifact_source import (prepare_artifact_task_root, prepare_artifact_source,
                                      snapshot_artifact_source)
        from .office_workflow import run_office_workflow
        from .office import validate_spec
        kind = action['kind']
        table = self._table(facts)
        self.task_base.mkdir(parents=True, exist_ok=True)
        task = self.task_base / ('task-' + uuid.uuid4().hex)
        prepare_artifact_task_root(task, self.task_base)
        _save(task / 'goal-origin.json', {'goal_sha256': digest(self.goal), 'run': str(self.run),
                                         'step': str(step_root), 'evidence_sha256': digest([h for _, h in facts['records']])})
        (task / 'request.txt').write_text(self.goal['title'], encoding='utf-8')
        write_new(task / 'events.jsonl', {'event': 'agent_office_started', 'at': utc_now()})
        if kind == 'spreadsheet':
            spec = {'kind': kind, 'title': self.goal['title'], **table, 'sum_columns': []}
        else:
            if 'spreadsheet' in self.goal['outputs']:
                prior = next(row for row, _ in facts['records'] if row['action']['kind'] == 'spreadsheet')
                source = snapshot_artifact_source(prior['ref'], task, self.task_base)
                if source['kind'] != 'table' or source['observed'] != table or source['coverage']['derived_totals']:
                    raise ComputerError('agent_artifact_changed')
                table = source['observed']
            spec = {'kind': kind, 'title': self.goal['title'],
                    'paragraphs': self._paragraphs(), 'table': table}
        spec = validate_spec(spec)
        self._check()
        workflow = task / ('workflow-' + uuid.uuid4().hex)
        try:
            value = run_office_workflow([{'id': 's1', 'action': kind, 'data': spec}], workflow,
                execute=self.office_execute, deadline=self.deadline,
                cancel_requested=lambda: self.stop() or self.clock() >= self.deadline)
        except Exception:
            return {'ok': False, 'state': 'unknown', 'code': 'office_unknown'}
        artifacts = [{'step': key, 'path': receipt['artifact'], 'sha256': receipt['sha256']}
                     for key, receipt in value.get('receipts', {}).items()]
        write_new(task / 'result.json', {'root': str(task), 'kind': 'office', 'ok': value['ok'],
            'state': value['state'], 'workflow_root': str(workflow), 'completed': value['completed'],
            'artifacts': artifacts, 'artifact': artifacts[-1]['path'] if artifacts else None,
            'readback_sha256': artifacts[-1]['sha256'] if artifacts else None, 'error': value.get('error')})
        if not value['ok']:
            return {'ok': False, 'state': 'unknown' if value['state'] == 'unknown' else 'failed', 'code': 'office_' + value['state']}
        artifact = {**artifacts[0], 'task_root': str(task), 'kind': kind}
        record.update(artifact=artifact, ref=prepare_artifact_source(artifact['path'], artifact['sha256'], self.task_base), spec=spec)
        self._verify_artifact(record)
        return {'ok': True, 'state': 'verified', 'code': 'office_verified'}
