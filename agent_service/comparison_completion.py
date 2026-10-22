"""Version-three deterministic, replayable bounded comparison completion."""
from copy import deepcopy
import re
from urllib.parse import urlsplit

from .computer import ComputerError, digest, safe_root
from .computer_agent_tools import AgentEnvironment, _save, _selection
from .computer_search import SEARCH_TIMEOUT, SearchProcess, validate_result
from .contracts import BrowserReadError, utc_now


REPAIRABLE = frozenset({'comparison_coverage_insufficient',
                       'comparison_independence_insufficient',
                       'comparison_semantic_insufficient'})
HINTS = ('about', 'copyright', 'legal', 'company', 'format', 'compatib', 'feature')


def _host(url):
    return urlsplit(url).hostname.lower().rstrip('.')


class CompletionEnvironment(AgentEnvironment):
    active_limit = 4

    def _need_satisfied(self, facts, need):
        return any(facts['reads'][sid]['result']['ok']
                   for sid in facts['need_reads'].get(need['need_id'], []))

    def _matches_need(self, item, need):
        return _host(item['url']) == need['host']

    def _discover_links(self, source, action, excluded):
        from .comparison_link_discovery import LinkDiscoveryProcess
        return LinkDiscoveryProcess().discover(source, action['link_query'], excluded,
            should_stop=lambda: self.stop() or self.clock() >= self.deadline,
            timeout=min(20, self.deadline-self.clock()))

    def _validate_links(self, linked, source, action, excluded):
        from .comparison_link_discovery import validate_link_result
        return validate_link_result(linked, source, action['link_query'], excluded)

    def _supplement_allowed(self, record, action, urls):
        return all(_host(url) == action['host'] for url in urls)

    def _reader(self, query):
        from .browser import BrowserProcess
        return self.browser or BrowserProcess(query=query)

    def _empty(self):
        return dict(records=[], queries=[], candidates=[], reads={}, failures=[], artifacts=[],
                    done=[], office_started=False, office_blocked=False, comparison=None,
                    active_ids=[], replaced_ids=[], comparisons=[], needs=[], supplements=[],
                    need_reads={}, accepted=[])

    def _accepted_active(self, facts):
        by_id = {item['id']: item for item in facts['candidates']}
        return [(by_id[sid], facts['reads'][sid]) for sid in facts['active_ids']]

    def _raw_ready(self, facts):
        from .source_comparison import sources_ready
        return sources_ready(self.goal, self._accepted_active(facts))

    def _needs(self, facts, evidence):
        assessment = evidence['assessment']
        details = assessment['group_details']
        by_id = {item['id']: item for item in facts['candidates']}
        dimensions = self.goal['comparison']['dimensions']
        targets = [g for g in details if not g['complete']]
        if not targets:
            targets = details
        needs = []
        for group in targets:
            support = assessment.get('publisher_support', {})
            sid = next((sid for sid in group['ids'] if not support.get(sid)), group['ids'][0])
            host = _host(by_id[sid]['url'])
            if any(need['host'] == host for need in needs):
                continue
            covered = group['covered_dimensions']
            missing = [d for i, d in enumerate(dimensions) if not covered[i]]
            suffix = ('about company' if not group['publisher_supported']
                      else ' '.join(missing) if missing else 'copyright publisher')
            approved = self.goal['queries'][by_id[sid]['query_index']]
            topic = re.sub(r'\bsite:\S+', '', approved, flags=re.IGNORECASE).strip()[:80]
            fixed = f'site:{host} {suffix}'
            query = f'site:{host} {topic[:max(0, 499 - len(fixed))]} {suffix}'
            need = {'host': host, 'query': query, 'link_query': fixed,
                    'review_sha256': digest(evidence)}
            need['need_id'] = digest(need)[:24]
            needs.append(need)
            if len(needs) == 2:
                break
        return needs

    def _hinted(self, item):
        return any(hint in urlsplit(item['url']).path.casefold() for hint in HINTS)

    def _link_inputs(self, facts, action):
        source = next(item['url'] for item, _ in self._accepted_active(facts)
                      if _host(item['url']) == action['host'])
        excluded = [item['url'] for item in facts['candidates'] if item['id'] in facts['reads']]
        return source, excluded

    def _retain(self, facts, response):
        if not facts['comparisons']:
            return {'response': deepcopy(response), 'retained': []}
        from .comparison_retention import retain_selection
        first = facts['comparisons'][0]
        by_id = {item['id']: item for item in facts['candidates']}
        previous = [(by_id[sid], facts['reads'][sid]) for sid in first['active_ids']]
        return retain_selection(self.goal, self._accepted_active(facts), response,
                                self.goal, previous, first['comparison'])

    def _frontier(self, facts):
        if len(facts['records']) >= 12 or facts['office_blocked']:
            return []
        if facts['comparison'] is not None:
            for kind in self.goal['outputs']:
                if kind not in facts['done']:
                    return [{'kind': kind}]
            return []
        if facts['office_started']:
            return []
        if facts['comparisons']:
            if len(facts['comparisons']) >= 2:
                return []
            # Reserve a final comparison and every requested native output.
            remaining = 12 - len(facts['records'])
            finish = 1 + len(self.goal['outputs'])
            for need in facts['needs']:
                attempts = facts['need_reads'].get(need['need_id'], [])
                if self._need_satisfied(facts, need):
                    continue
                searched = need['need_id'] in facts['supplements']
                unread = [item for item in facts['candidates']
                          if item['id'] not in facts['reads'] and self._matches_need(item, need)
                          and (self._hinted(item) or item.get('need_id') == need['need_id'])]
                if unread:
                    if remaining < finish + 1:
                        break
                    return [{'kind': 'read', 'candidate_id': unread[0]['id']}]
                if not searched:
                    if remaining < finish + 2:
                        break
                    return [{'kind': 'supplement', **need}]
            first = facts['comparisons'][0]
            if facts['active_ids'] != first['active_ids'] and remaining >= finish:
                return [{'kind': 'compare'}]
            return []
        if self._raw_ready(facts):
            return [{'kind': 'compare'}]
        unread = [item for item in facts['candidates'] if item['id'] not in facts['reads']]
        hosts = {_host(item['url']) for item, _ in self._accepted_active(facts)}
        novel = [item for item in unread if _host(item['url']) not in hosts]
        pending = [i for i in range(len(self.goal['queries'])) if i not in facts['queries']]
        if len(hosts) < self.goal['comparison']['min_domains'] and not novel and pending:
            return [{'kind': 'search', 'query_index': pending[0]}]
        if unread:
            return [{'kind': 'read', 'candidate_id': (novel or unread)[0]['id']}]
        return [{'kind': 'search', 'query_index': pending[0]}] if pending else []

    def _current_need(self, facts, candidate_id):
        frontier = self._frontier(facts)
        if not facts['comparisons'] or not frontier or frontier[0] != {'kind': 'read', 'candidate_id': candidate_id}:
            return None
        for need in facts['needs']:
            attempts = facts['need_reads'].get(need['need_id'], [])
            if not self._need_satisfied(facts, need):
                item = next(item for item in facts['candidates'] if item['id'] == candidate_id)
                if self._matches_need(item, need):
                    return need
        return None

    def _add_active(self, facts, sid):
        active = [*facts['active_ids'], sid]
        if len(active) > 4:
            by_id = {item['id']: item for item in facts['candidates']}
            # Never discard the only page on an existing distinct exact host.
            eligible = [old for old in active[:-1] if
                        sum(_host(by_id[v]['url']) == _host(by_id[old]['url']) for v in active) > 1]
            if not eligible:
                raise ComputerError('comparison_active_limit')
            review = {'sources': [{'id': sid, 'cells': cells} for sid, cells in
                      facts['comparisons'][0]['comparison']['assessment']['cells'].items()]}
            scores = {row['id']: sum(cell['status'] == 'relevant' for cell in row['cells'])
                      for row in review['sources']}
            supported = facts['comparisons'][0]['comparison']['assessment']['publisher_evidence']
            publishers = {row['id']: supported[row['id']] == 'supported' for row in review['sources']}
            rows = {row['id']: row for row in review['sources']}
            groups = facts['comparisons'][0]['comparison']['assessment']['groups']
            def lost_coverage(old):
                group = next((group for group in groups if old in group), [old])
                return sum(cell['status'] == 'relevant' and not any(
                    other != old and other in active and other in rows
                    and rows[other]['cells'][i]['status'] == 'relevant' for other in group)
                    for i, cell in enumerate(rows.get(old, {'cells': []})['cells']))
            victim = min(eligible, key=lambda old: (
                lost_coverage(old),
                _host(by_id[old]['url']) != _host(by_id[sid]['url']),
                scores.get(old, 0), publishers.get(old, False), active.index(old)))
            active.remove(victim)
            facts['replaced_ids'].append(victim)
        facts['active_ids'] = active

    def _replay(self, records, *, verify_office=True):
        from .source_comparison import verify_evidence, review_failure
        facts = self._empty()
        for position, (record, record_hash) in enumerate(records):
            action, result = record['action'], record['result']
            if (self._frontier(facts) != [action] or type(result.get('ok')) is not bool
                    or result.get('state') not in {'verified', 'failed', 'unknown'}
                    or result['ok'] != (result['state'] == 'verified') or type(result.get('code')) is not str):
                raise ComputerError('agent_evidence_changed')
            kind = action['kind']
            if kind in {'search', 'supplement'}:
                if kind == 'search':
                    index = action['query_index']
                    facts['queries'].append(index)
                else:
                    facts['supplements'].append(action['need_id'])
                    index = next(item['query_index'] for item in facts['candidates']
                                 if _host(item['url']) == action['host'])
                if 'link_result' in record:
                    if kind != 'supplement':
                        raise ComputerError('agent_evidence_changed')
                    source, excluded = self._link_inputs(facts, action)
                    link_result = self._validate_links(record['link_result'], source, action, excluded)
                    if link_result['discovery'] is not None and (record.get('discovery') != link_result['discovery'] or not result['ok']):
                        raise ComputerError('agent_evidence_changed')
                if 'link_failure' in record and (kind != 'supplement' or record['link_failure'] not in {
                        'supplement_links_unavailable', 'supplement_links_timeout'} or 'link_result' in record):
                    raise ComputerError('agent_evidence_changed')
                if result['ok']:
                    discovery = validate_result(record['discovery'], 2)
                    if kind == 'supplement' and not self._supplement_allowed(record, action, discovery['urls']):
                        raise ComputerError('agent_evidence_changed')
                    for url in discovery['urls']:
                        existing = next((item for item in facts['candidates'] if item['url'] == url), None)
                        if existing is None:
                            item = {'id': digest(url)[:24], 'url': url, 'query_index': index}
                            if kind == 'supplement':
                                item['need_id'] = action['need_id']
                            facts['candidates'].append(item)
                        elif kind == 'supplement' and existing['id'] not in facts['reads']:
                            existing['need_id'] = action['need_id']
                else:
                    if result['code'] == 'supplement_host_mismatch':
                        if kind != 'supplement':
                            raise ComputerError('agent_evidence_changed')
                        discovery = validate_result(record['discovery'], 2)
                        if self._supplement_allowed(record, action, discovery['urls']):
                            raise ComputerError('agent_evidence_changed')
                    facts['failures'].append({'kind': kind, 'code': result['code'],
                                              'captured_at': record['at'], **action})
            elif kind == 'read':
                sid = action['candidate_id']
                item = next(item for item in facts['candidates'] if item['id'] == sid)
                need = self._current_need(facts, sid)
                purpose = {'purpose': 'supplement', 'need_id': need['need_id']} if need else {'purpose': 'initial'}
                if record.get('read_purpose') != purpose:
                    raise ComputerError('agent_evidence_changed')
                if result['ok'] and 'observation' not in record:
                    raise ComputerError('agent_evidence_changed')
                if 'observation' in record:
                    data = record['observation']
                    self._validate_page(data, item['url'])
                    excerpt = _selection(data['text'], need['query'] if need else self.goal['queries'][item['query_index']],
                                         [] if need else self.goal['required_terms'])
                    if record.get('excerpt') != excerpt or result['ok'] != (excerpt is not None):
                        raise ComputerError('agent_evidence_changed')
                if need:
                    facts['need_reads'].setdefault(need['need_id'], []).append(sid)
                facts['reads'][sid] = record
                if result['ok']:
                    self._add_active(facts, sid)
                else:
                    facts['failures'].append({'kind': kind, 'url': item['url'], 'code': result['code'],
                                              'captured_at': record['at']})
            elif kind == 'compare':
                if (record.get('active_ids') != facts['active_ids']
                        or record.get('replaced_ids') != facts['replaced_ids']):
                    raise ComputerError('agent_evidence_changed')
                checked = None
                if 'comparison' in record:
                    kept = self._retain(facts, record.get('selection_response'))
                    if (record.get('retained_selection') != kept['retained']
                            or record['comparison'].get('raw_response') != kept['response']):
                        raise ComputerError('agent_evidence_changed')
                    checked = verify_evidence(self.goal, self._accepted_active(facts), record['comparison'])
                    failure = review_failure(checked)
                    if result['ok'] != (failure is None) or result['code'] != (failure or 'comparison_verified'):
                        raise ComputerError('agent_evidence_changed')
                elif result['ok']:
                    raise ComputerError('agent_evidence_changed')
                facts['comparisons'].append(record)
                if result['ok']:
                    facts['comparison'] = checked
                else:
                    facts['failures'].append({'kind': kind, 'code': result['code'], 'captured_at': record['at']})
                    if checked is not None and len(facts['comparisons']) == 1 and result['code'] in REPAIRABLE:
                        facts['needs'] = self._needs(facts, checked)
                    else:
                        facts['office_blocked'] = True
            else:
                facts['office_started'] = True
                if not result['ok']:
                    facts['office_blocked'] = True
                else:
                    if verify_office:
                        self._verify_artifact(record)
                        self._verify_origin(record, records[:position], self.record_origins[record_hash])
                    facts['artifacts'].append(record['artifact'])
                    facts['done'].append(kind)
            facts['records'].append((record, record_hash))
            if len(facts['candidates']) > 8 or len(facts['active_ids']) > self.active_limit:
                raise ComputerError('agent_evidence_changed')
        facts['accepted'] = self._accepted_active(facts)
        return facts

    def _facts(self):
        return self._replay(self._records())

    def _origin_facts(self, prefix):
        return self._replay(prefix, verify_office=False)

    def observe_final(self):
        facts = self._facts()
        ready = self._raw_ready(facts)
        met = facts['comparison'] is not None and not facts['office_blocked'] and all(
            kind in facts['done'] for kind in self.goal['outputs'])
        return {'artifacts': deepcopy(facts['artifacts']), 'source_reads': len(facts['accepted']),
                'failures': deepcopy(facts['failures']), 'searched_queries': facts['queries'],
                'candidates': [{**item, 'status': 'unread' if item['id'] not in facts['reads'] else
                    'verified' if facts['reads'][item['id']]['result']['ok'] else 'failed'} for item in facts['candidates']],
                'sources': [{'url': item['url'], 'title': row['observation']['title'],
                             'captured_at': row['observation']['captured_at'], 'freshness': 'historical_capture',
                             'excerpt_preview': row['excerpt']['text'][:180], 'start': row['excerpt']['start'],
                             'end': row['excerpt']['end'], 'terms_match': row['read_purpose']['purpose'] == 'initial',
                             'read_purpose': deepcopy(row['read_purpose'])} for item, row in facts['accepted']],
                'active_ids': facts['active_ids'], 'replaced_ids': facts['replaced_ids'],
                'comparison_rounds': len(facts['comparisons']), 'supplement_count': len(facts['supplements']),
                'office_started': facts['office_started'], 'office_blocked': facts['office_blocked'],
                'outputs_done': facts['done'], 'comparison_done': facts['comparison'] is not None,
                'comparison_review': 'bounded_completion_v3',
                'evidence_sha256': digest([value for _, value in facts['records']]),
                'checks': {'sources_ready': ready, 'goal_met': met}}

    def actions(self, observation):
        if observation != self.observe():
            raise ComputerError('agent_observation_changed')
        return deepcopy(self._frontier(self._facts()))

    def execute(self, action, step_root):
        self._check()
        observation = self.observe()
        if self.actions(observation) != [action]:
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
        if kind in {'search', 'supplement'}:
            query = action['query'] if kind == 'supplement' else self.goal['queries'][action['query_index']]
            try:
                discovery = None
                if kind == 'supplement' and self.search is None:
                    source, excluded = self._link_inputs(facts, action)
                    try:
                        linked = self._discover_links(source, action, excluded)
                        record['link_result'] = self._validate_links(linked, source, action, excluded)
                        discovery = record['link_result']['discovery']
                    except ComputerError as exc:
                        if str(exc) not in {'supplement_links_unavailable', 'supplement_links_timeout'}:
                            raise
                        record['link_failure'] = str(exc)
                    self._check()
                if discovery is None:
                    discovery = (self.search or SearchProcess(provider='bing', comparison=True)).search(
                        query, 2, should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                        timeout=min(SEARCH_TIMEOUT, self.deadline - self.clock()))
                self._check()
                discovery = validate_result(discovery, 2)
                record['discovery'] = discovery
                if kind == 'supplement' and not self._supplement_allowed(record, action, discovery['urls']):
                    result = {'ok': False, 'state': 'failed', 'code': 'supplement_host_mismatch'}
                else:
                    result = {'ok': True, 'state': 'verified', 'code': 'search_discovered'}
            except ComputerError as exc:
                if str(exc) not in {'search_timeout', 'search_unavailable'}:
                    self._check()
                    raise
                result = {'ok': False, 'state': 'failed', 'code': str(exc)}
        elif kind == 'read':
            from .browser import BrowserProcess
            item = next(item for item in facts['candidates'] if item['id'] == action['candidate_id'])
            need = self._current_need(facts, item['id'])
            query = need['query'] if need else self.goal['queries'][item['query_index']]
            record['read_purpose'] = ({'purpose': 'supplement', 'need_id': need['need_id']}
                                      if need else {'purpose': 'initial'})
            try:
                value = self._reader(query).read(
                    item['url'], should_stop=lambda: self.stop() or self.clock() >= self.deadline)
                self._check()
                data = value.to_dict()
                self._validate_page(data, item['url'])
                excerpt = _selection(data['text'], query, [] if need else self.goal['required_terms'])
                record.update(observation=data, excerpt=excerpt)
                result = {'ok': excerpt is not None, 'state': 'verified' if excerpt is not None else 'failed',
                          'code': 'source_verified' if excerpt is not None else 'quality_rejected'}
            except BrowserReadError as exc:
                result = {'ok': False, 'state': 'failed', 'code': exc.safe_code}
        elif kind == 'compare':
            from .source_comparison import build_context, create_evidence, review_context, review_failure
            record.update(active_ids=deepcopy(facts['active_ids']), replaced_ids=deepcopy(facts['replaced_ids']))
            try:
                if self.comparison_select is None or self.comparison_review is None:
                    raise ComputerError('comparison_model_missing')
                response = self.comparison_select(build_context(self.goal, facts['accepted']))
                record['selection_response'] = deepcopy(response)
                kept = self._retain(facts, response)
                record['retained_selection'] = kept['retained']
                response = kept['response']
                review = self.comparison_review(review_context(self.goal, facts['accepted'], response))
                record['review_response'] = deepcopy(review)
                record['comparison'] = create_evidence(self.goal, facts['accepted'], response, review)
                failure = review_failure(record['comparison'])
                result = {'ok': failure is None, 'state': 'failed' if failure else 'verified',
                          'code': failure or 'comparison_verified'}
            except ComputerError as exc:
                result = {'ok': False, 'state': 'failed', 'code': str(exc)}
        else:
            result = self._office(action, step_root, facts, record)
        record['result'] = result
        _save(step_root / 'environment-result.json', record)
        if kind not in {'spreadsheet', 'document', 'compare'}:
            self._check()
        return result
