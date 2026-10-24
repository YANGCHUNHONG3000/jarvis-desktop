"""Version-four follow-up: verified captures plus bounded cross-site evidence."""
from copy import deepcopy
import re

from .computer import ComputerError, digest
from .comparison_completion import CompletionEnvironment, _host


class CrossSiteEnvironment(CompletionEnvironment):
    active_limit = 6

    def _dimension_policy(self):
        return self.goal['comparison'].get('completion_policy') in ('dimension_repair_v3', 'dimension_first_v4')

    def _repair_input(self):
        from .comparison_failed_seed import load_bundle
        value = getattr(self, '_repair_batch', None)
        if value is not None:
            return deepcopy(value)
        comparison = self.goal['comparison']
        base = load_bundle(comparison['seed'])
        if 'capture_import' in comparison:
            from .comparison_capture_import import load_bundle as load_import
            return load_import(comparison['capture_import'], comparison['seed'], base)
        return base

    def _replay(self, records, *, verify_office=True):
        if self.goal['comparison'].get('dimension_routing') not in (3, 4, 5, 6) or hasattr(self, '_repair_batch'):
            return super()._replay(records, verify_office=verify_office)
        # Reuse only within this synchronous replay. Entry and exit fully rehash
        # the imported graph and recheck ownership; no cross-action/mtime cache.
        before = self._repair_input()
        self._repair_batch = before
        self._navigation_replay_records = deepcopy(records)
        self._navigation_source_batch = {}
        try:
            result = super()._replay(records, verify_office=verify_office)
        finally:
            del self._repair_batch
            del self._navigation_replay_records
            del self._navigation_source_batch
            if self._repair_input() != before:
                raise ComputerError('comparison_seed_changed')
        return result

    def _prior_discoveries(self):
        if self.goal['comparison'].get('dimension_routing') in (3, 4, 5, 6):
            values = self._repair_input()[1].get('discoveries', [])
            if self.goal['comparison'].get('dimension_routing') in (5, 6):
                # Cache-only attribution is a verified metadata receipt, not an
                # HTML navigation inventory. Keep it in history, not this view.
                values = [v for v in values if v.get('proof', {}).get('version') != 12]
            return values
        from .comparison_failed_seed import load_repair_history
        return load_repair_history(self.goal['comparison']['seed']).get('discoveries', [])

    def _historical_limit(self):
        route = self.goal['comparison'].get('dimension_routing')
        return 16 if route in (5, 6) else 10 if route in (3, 4) else 8

    def execute(self, action, step_root):
        if self.goal['comparison'].get('dimension_routing') not in (5, 6):
            return super().execute(action, step_root)
        self._navigation_execution_records = self._records()
        try:
            return super().execute(action, step_root)
        finally:
            del self._navigation_execution_records

    def _navigation_records(self):
        value = getattr(self, '_navigation_execution_records', None)
        if value is None:
            value = getattr(self, '_navigation_replay_records', None)
        return deepcopy(value) if value is not None else self._records()

    def _navigation_inputs(self, before=None):
        values = deepcopy(self._prior_discoveries())
        for record, _ in self._navigation_records():
            if before is not None and record.get('intent_sha256') == before.get('intent_sha256'):
                break
            if ('link_result' in record and record['action'].get('purpose') != 'cached_attribution_v1'):
                values.append(deepcopy(record['link_result']))
        return values

    def _consumed_urls(self, before=None):
        captures, history = self._repair_input()
        consumed = set(history['read_urls'])
        known = {item['id']: item['url'] for item, _ in captures}
        known.update({item['id']: item['url'] for item in history.get('candidates', [])})
        for record, _ in self._navigation_records():
            if before is not None and record.get('intent_sha256') == before.get('intent_sha256'):
                break
            for url in record.get('discovery', {}).get('urls', []):
                known[digest(url)[:24]] = url
            if record['action']['kind'] == 'read':
                sid = record['action']['candidate_id']
                if sid not in known:
                    raise ComputerError('agent_evidence_changed')
                consumed.add(known[sid])
        return consumed

    def _navigation_sources(self, action):
        from .comparison_compact_seed import load_navigation_observations
        from collections import OrderedDict
        # The bundle performs full current byte/owner checks; within a replay it
        # is protected by the surrounding entry/exit checks instead.
        self._repair_input()
        cache = getattr(self, '_navigation_verified_sources', None)
        if cache is None:
            cache = self._navigation_verified_sources = OrderedDict()
        key = (digest(self.goal['comparison']['seed']), action['topic'])
        if key in cache:
            cache.move_to_end(key)
            return deepcopy(cache[key])
        result = load_navigation_observations(self.goal['comparison']['seed'], action['topic'])
        cache[key] = deepcopy(result)
        while len(cache) > 4:
            cache.popitem(last=False)
        return result

    def _validate_project_document(self, record, url, query):
        if self.goal['comparison'].get('dimension_routing') in (5, 6):
            from .comparison_cached_reader import validate_record
            if validate_record(record, url, query, self._navigation_inputs(record)):
                return
        return super()._validate_project_document(record, url, query)

    def _reader(self, query):
        fallback = self._uncached_reader(query)
        if self.goal['comparison'].get('dimension_routing') in (5, 6):
            from .comparison_cached_reader import CachedNavigationReader
            return CachedNavigationReader(fallback, query, self._navigation_inputs())
        return fallback

    def _uncached_reader(self, query):
        from .project_documents import enabled as dynamic_policy
        if dynamic_policy(self.goal):
            from .project_reader import ProjectReaderProcess
            return self.browser or ProjectReaderProcess(query, include_attribution=True)
        from .browser import BrowserProcess
        return self.browser or BrowserProcess(query=query, include_attribution=True)

    def _empty(self):
        from .comparison_seed import load_seed
        facts = super()._empty()
        captured = (self._repair_input()[0] if self.goal['comparison'].get('dimension_routing') in (3, 4, 5, 6)
                    else load_seed(self.goal['comparison']['seed']))
        if self.goal['queries'] != self.goal['comparison']['seed']['queries']:
            raise ComputerError('comparison_seed_queries_changed')
        for item, record in captured:
            facts['candidates'].append(deepcopy(item))
            imported = deepcopy(record)
            if self.goal['comparison']['seed']['version'] == 2:
                # Ordinary research receipts predate the comparison purpose
                # field. Normalize only the verified in-memory import.
                imported['read_purpose'] = {'purpose': 'initial'}
            facts['reads'][item['id']] = imported
            facts['active_ids'].append(item['id'])
        facts['queries'] = sorted({item['query_index'] for item, _ in captured})
        from .comparison_repair import enabled, import_history
        if enabled(self.goal):
            from .comparison_failed_seed import load_repair_history
            history = (self._repair_input()[1] if self.goal['comparison'].get('dimension_routing') in (3, 4, 5, 6)
                       else load_repair_history(self.goal['comparison']['seed']))
            import_history(facts, history,
                           limit=self._historical_limit())
        return facts

    def _need_satisfied(self, facts, need):
        if need.get('purpose') == 'cached_attribution_v1':
            return any(facts['reads'][sid]['result']['ok']
                       for sid in facts['need_reads'].get(need['need_id'], []))
        if need.get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6'):
            candidates = [item for item in facts['candidates'] if self._matches_need(item, need)]
            return bool(candidates) and all(item['id'] in facts['reads'] for item in candidates)
        if need.get('purpose') in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            return any(facts['reads'][sid]['result']['ok']
                       for sid in facts['need_reads'].get(need['need_id'], []))
        return sum(facts['reads'][sid]['result']['ok']
                   for sid in facts['need_reads'].get(need['need_id'], [])) >= 2

    def _matches_need(self, item, need):
        # A cross-host read is permitted only after validated actual-anchor discovery.
        from .comparison_repair import enabled, supplement_key
        matches = item.get('need_id') == need['need_id']
        if enabled(self.goal):
            matches = matches or item.get('prior_need_key') == supplement_key(need)
        if need.get('purpose') in {'project_attribution', 'dynamic_project_attribution'}:
            from .project_scope import same_project
            return matches and same_project(need['scope_url'], item['url'])
        if enabled(self.goal) and need.get('purpose') == 'repository_attribution':
            from .publisher_scope import same_repository
            return matches and same_repository(need['scope_url'], item['url'])
        return matches

    def _frontier(self, facts):
        from .comparison_repair import enabled, frontier_view, candidate_count
        self._extend_cached_attribution(facts)
        if enabled(self.goal):
            facts = frontier_view(facts)
        actions = super()._frontier(facts)
        if self.goal['comparison'].get('dimension_routing') in (5, 6) and actions:
            remaining = 12 - len(facts['records'])
            finish = 1 + len(self.goal['outputs'])
            needed = 2 if actions[0].get('purpose') == 'cached_attribution_v1' else 3
            if actions[0]['kind'] == 'supplement' and remaining < finish + needed:
                return []
            if actions[0]['kind'] == 'compare' and facts['comparisons']:
                if any(need.get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6')
                       and any(self._matches_need(item, need) and item['id'] not in facts['reads']
                               for item in facts['candidates']) for need in facts['needs']):
                    return []
        size = candidate_count(facts) if enabled(self.goal) else len(facts['candidates'])
        reserve = (2 if actions and actions[0].get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6') else 1 if actions and actions[0].get('purpose') in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6', 'cached_attribution_v1') else 2)
        if actions and actions[0]['kind'] == 'supplement' and (
                size > self._historical_limit() - reserve
                or len(facts['candidates']) > 8 - reserve):
            # Dimension requests discover one URL; old requests can discover two.
            # Bound both live candidates and the versioned consumed history.
            changed = facts['active_ids'] != facts['comparisons'][0]['active_ids']
            remaining = 12 - len(facts['records'])
            actions = [{'kind': 'compare'}] if changed and remaining >= 1 + len(self.goal['outputs']) else []
        if (self.goal['comparison'].get('completion_policy') in {'original_query_fallback_v1', 'capture_repair_v2', 'dimension_repair_v3', 'dimension_first_v4'}
                and len(facts['comparisons']) == 1 and not facts['office_blocked']
                and 'comparison' in facts['comparisons'][0]
                and not facts['office_started'] and facts['comparison'] is None
                and (not actions or actions[0]['kind'] == 'compare')):
            return self._original_fallback(facts) or actions
        return actions

    def _extend_cached_attribution(self, facts):
        """Append optional cached identity work without changing the old prefix."""
        if (self.goal['comparison'].get('dimension_routing') != 6
                or len(facts['comparisons']) != 1 or facts['office_started']
                or facts['office_blocked'] or facts['comparison'] is not None
                or 'comparison' not in facts['comparisons'][0]):
            return
        from .comparison_evidence_discovery import _inventory, _passages, cached_page
        from .project_scope import requires_project, same_project
        prior = deepcopy(self._prior_discoveries())
        prior += [deepcopy(row['link_result']) for row, _ in facts['records']
                  if 'link_result' in row and row['action'].get('purpose') != 'cached_attribution_v1']
        pages, _ = _inventory(prior)
        used = {item['url'] for item in facts['candidates']}
        used.update(facts.get('repair_history', {}).get('read_urls', []))
        queries = {need['query_index'] for need in facts['needs']
                   if need.get('purpose') == 'cached_attribution_v1'}
        for item in facts['candidates']:
            sid, index = item['id'], item['query_index']
            if sid not in facts['active_ids'] or index in queries or sid not in facts['reads']:
                continue
            read = facts['reads'][sid]
            if not read['result']['ok'] or 'observation' not in read:
                continue
            direct, attributed = _passages({'mime': 'text/plain',
                'content': read['observation']['text'].encode('utf-8')})
            if not direct or attributed:
                continue
            query = self.goal['queries'][index] + ' authors copyright'
            if len(query) > 500:
                continue
            for target in pages:
                url = target['url']
                if (url in used or url == item['url'] or _host(url) != _host(item['url'])
                        or not _passages(target)[1]
                        or requires_project(url) and requires_project(item['url'])
                        and not same_project(item['url'], url)):
                    continue
                try:
                    retained = cached_page(url, prior)
                except ComputerError:
                    continue  # Conflicting retained versions cannot supply a need.
                need = {'purpose': 'cached_attribution_v1', 'source_id': sid,
                    'source_url': item['url'], 'capture_sha256': digest(read['observation']),
                    'query_index': index, 'host': _host(item['url']), 'query': query,
                    'link_query': query, 'target_url': url, 'target_ref': retained['ref'],
                    'source_sha256': retained['source_sha256']}
                need['need_id'] = digest(need)[:24]
                facts['needs'].append(need)
                queries.add(index)
                used.add(url)
                break

    def _cached_attribution_result(self, source, action, excluded, prior):
        from .comparison_evidence_discovery import cached_page, _passages
        from .project_scope import requires_project, same_project
        target = action['target_url']
        page = cached_page(target, prior)
        index = action['query_index']
        if (self.goal['comparison'].get('dimension_routing') != 6
                or source != action['source_url'] or target == source or target in excluded
                or _host(source) != _host(target) or page is None or not _passages(page)[1]
                or requires_project(source) and requires_project(target) and not same_project(source, target)
                or type(index) is not int or not 0 <= index < len(self.goal['queries'])
                or action['query'] != self.goal['queries'][index] + ' authors copyright'
                or action['link_query'] != action['query'] or action['target_ref'] != page['ref']
                or action['source_sha256'] != page['source_sha256']):
            raise ComputerError('supplement_dimension_proof_invalid')
        return {'discovery': {'urls': [target], 'searched_at': page['captured_at']},
            'proof': {'version': 12, 'kind': 'cached_attribution_v1',
                'action_sha256': digest(action), 'prior_sha256': digest(prior),
                'ref': deepcopy(page['ref']), 'source_sha256': page['source_sha256']}}

    def _original_fallback(self, facts):
        """Spend only the unused original-query frontier after bounded repair."""
        from .comparison_repair import enabled, candidate_count
        remaining = 12 - len(facts['records'])
        finish = 1 + len(self.goal['outputs'])
        if remaining < finish + 1:
            return []
        by_id = {item['id']: item for item in facts['candidates']}
        assessment = facts['comparisons'][0]['comparison']['assessment']
        complete_queries = {by_id[sid]['query_index']
                            for group in assessment['group_details'] if group['complete']
                            for sid in group['ids']}
        unread = [item for item in facts['candidates']
                  if 'need_id' not in item and item['id'] not in facts['reads']]
        if unread:
            preferred = [item for item in unread if item['query_index'] not in complete_queries]
            return [{'kind': 'read', 'candidate_id': (preferred or unread)[0]['id']}]
        pending = [i for i in range(len(self.goal['queries'])) if i not in facts['queries']]
        size = candidate_count(facts) if enabled(self.goal) else len(facts['candidates'])
        if pending and remaining >= finish + 2 and size <= 6:
            return [{'kind': 'search', 'query_index': pending[0]}]
        return []

    def _needs(self, facts, evidence):
        needs = []
        details = evidence['assessment']['group_details']
        by_id = {item['id']: item for item in facts['candidates']}
        for index in range(len(self.goal['queries'])):
            originals = [item for item in facts['candidates']
                         if item['query_index'] == index and item['id'] in facts['active_ids']]
            if not originals:
                continue
            groups = [g for g in details if any(by_id[s]['query_index'] == index for s in g['ids'])]
            if groups and all(g['complete'] for g in groups):
                continue
            root = originals[0]
            missing = [label for d, label in enumerate(self.goal['comparison']['dimensions'])
                       if not any(g['covered_dimensions'][d] for g in groups)]
            from .publisher_evidence import enabled
            attribution = False
            if enabled(self.goal):
                support = evidence['assessment']['publisher_support']
                unsupported = [item for item in originals if support.get(item['id']) is None]
                if unsupported:
                    root = unsupported[0]
                    attribution = True
            if self.goal['comparison'].get('completion_policy') == 'dimension_first_v4' and missing:
                # A new direct source may supply both dimension and identity.
                # Never spend every remaining slot on attribution-only pages
                # when the original publication also lacks the requested fact.
                attribution = False
                if self.goal['comparison'].get('dimension_routing') in (4, 5, 6):
                    root = originals[0]
            suffix = ('authors maintainers copyright publisher' if attribution else
                      ' '.join(missing) or 'about company publisher')
            from .publisher_scope import enabled as repository_policy, repository_scope
            repo = repository_scope(root['url']) if repository_policy(self.goal) and attribution else None
            if (repository_policy(self.goal) and attribution
                    and _host(root['url']).rstrip('.') == 'github.com' and repo is None):
                continue  # Never broaden an ambiguous repository to all GitHub.
            scope_url = f'https://github.com/{repo[1]}/{repo[2]}' if repo else None
            query = (f'site:github.com/{repo[1]}/{repo[2]} {suffix}' if repo else
                     f"site:{_host(root['url'])} {suffix}")[:500]
            need = {'host': _host(root['url']), 'query': query, 'link_query': query,
                    'source_id': root['id'],
                    'capture_sha256': digest(facts['reads'][root['id']]['observation']),
                    'review_sha256': digest(evidence)}
            if attribution:
                need['purpose'] = 'repository_attribution' if repo else 'attribution'
                if repo:
                    need['scope_url'] = scope_url
            from .project_scope import enabled as project_policy, project_site, requires_project
            if project_policy(self.goal) and attribution:
                site = project_site(root['url'])
                if requires_project(root['url']) and site is None:
                    continue
                if site:
                    query = f'site:{site} {suffix}'
                    if len(query) > 500:
                        continue
                    from .project_documents import enabled as dynamic_policy
                    need.update(purpose='dynamic_project_attribution' if dynamic_policy(self.goal) else 'project_attribution', scope_url=root['url'],
                                query=query, link_query=query)
            if self._dimension_policy() and not attribution and missing:
                topic = re.sub(r'\bsite:\S+', '', self.goal['queries'][index], flags=re.I).strip()
                # Routing vocabulary is only a navigation hint, never an answer.
                terms = (' formula calculate calculation evaluate evaluation' if any(
                    '公式' in d or re.search(r'\bformulas?\b', d, re.I) for d in missing) else '')
                query = topic + terms + ' ' + ' '.join(missing)
                if len(query) > 500 and self.goal['comparison'].get('dimension_routing') not in (3, 4, 5, 6):
                    continue  # Never silently drop an approved topic or dimension.
                need.update(purpose='dimension_evidence_v1', dimensions=list(missing),
                            query=query, link_query=' '.join(missing) + terms)
                if self.goal['comparison'].get('dimension_routing') in (2, 3, 4, 5, 6):
                    route = self.goal['comparison']['dimension_routing']
                    need.update(purpose=f'dimension_evidence_v{route}', topic=self.goal['queries'][index])
                    if route in (3, 4, 5, 6):
                        need['source_url'] = root['url']
                        if terms:
                            need['query'] = topic + ' formula results calculation'
                            if route in (4, 5, 6):
                                from .comparison_dimension_discovery import _TOPIC_STOP
                                products = [word for word in re.findall(r'[a-z][a-z0-9]*', topic, re.I)
                                            if word.casefold() not in _TOPIC_STOP]
                                need['query'] = (' '.join(dict.fromkeys(products)) or topic) + ' formula calculation'
                        if route == 6 and terms:
                            need['query'] = topic + ' evaluates formulae results'
                        if len(need['query']) > 500:
                            continue
            need['need_id'] = digest(need)[:24]
            needs.append(need)
        return needs[:2]

    def _link_inputs(self, facts, action):
        item = next((item for item in facts['candidates'] if item['id'] == action['source_id']), None)
        if item is None or digest(facts['reads'][item['id']]['observation']) != action['capture_sha256']:
            raise ComputerError('comparison_seed_capture_changed')
        if action.get('purpose') in ('dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6', 'cached_attribution_v1') and action.get('source_url') != item['url']:
            raise ComputerError('comparison_seed_capture_changed')
        if action.get('purpose') == 'cached_attribution_v1' and action.get('query_index') != item['query_index']:
            raise ComputerError('comparison_seed_capture_changed')
        excluded = [i['url'] for i in facts['candidates'] if i['id'] in facts['reads']]
        from .comparison_repair import enabled
        if enabled(self.goal):
            excluded = list(dict.fromkeys([*excluded, *facts['repair_history']['read_urls']]))
        return item['url'], excluded

    def _discover_links(self, source, action, excluded):
        if action.get('purpose') == 'cached_attribution_v1':
            return self._cached_attribution_result(source, action, excluded, self._navigation_inputs())
        if action.get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_evidence_discovery import EvidenceDiscoveryProcess
            return EvidenceDiscoveryProcess().discover(source, action['link_query'], excluded,
                topic=action['topic'], prior_discoveries=self._navigation_inputs(),
                source_observations=self._navigation_sources(action), storage_dir=self.run,
                **({'navigation_policy': 'formula_search_index_v4'}
                   if action['purpose'] == 'dimension_evidence_v6' else {}),
                should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                timeout=min(35, self.deadline-self.clock()))
        if action.get('purpose') in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_dimension_discovery import DimensionDiscoveryProcess
            try:
                return DimensionDiscoveryProcess().discover(source, action['link_query'], excluded,
                    prior_discoveries=self._prior_discoveries(), storage_dir=self.run,
                    **({'routing_version': int(action['purpose'][-1]), 'topic': action['topic']}
                       if action['purpose'] in ('dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6') else {}),
                    should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                    timeout=min(35, self.deadline-self.clock()))
            except ComputerError as exc:
                mapped = {'supplement_dimension_unavailable': 'supplement_links_unavailable',
                          'supplement_dimension_timeout': 'supplement_links_timeout'}.get(str(exc))
                if mapped:
                    raise ComputerError(mapped) from None
                raise
        from .comparison_crosssite_discovery import CrossSiteDiscoveryProcess
        try:
            return CrossSiteDiscoveryProcess().discover(source, action['link_query'], excluded,
                should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                timeout=min(35, self.deadline-self.clock()),
                **({'purpose': action['purpose']} if 'purpose' in action else {}))
        except ComputerError as exc:
            mapped = {'supplement_crosssite_timeout': 'supplement_links_timeout',
                      'supplement_crosssite_unavailable': 'supplement_links_unavailable'}.get(str(exc))
            if mapped:
                raise ComputerError(mapped) from None
            raise

    def _validate_links(self, linked, source, action, excluded):
        if action.get('purpose') == 'cached_attribution_v1':
            before = next((record for record, _ in self._navigation_records()
                           if record['action'] == action), None)
            expected = self._cached_attribution_result(source, action, excluded, self._navigation_inputs(before))
            if linked != expected:
                raise ComputerError('supplement_dimension_proof_invalid')
            return deepcopy(expected)
        if action.get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_evidence_discovery import validate
            before = next((record for record, _ in self._navigation_records()
                           if record['action'] == action), None)
            return validate(linked, source, action['link_query'], excluded,
                topic=action['topic'], prior_discoveries=self._navigation_inputs(before),
                source_observations=self._navigation_sources(action))
        if action.get('purpose') in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_dimension_discovery import validate
            return validate(linked, source, action['link_query'], excluded,
                            prior_discoveries=self._prior_discoveries(),
                            **({'routing_version': int(action['purpose'][-1]), 'topic': action['topic']}
                               if action['purpose'] in ('dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6') else {}))
        from .comparison_crosssite_discovery import validate_crosssite_result
        return validate_crosssite_result(linked, source, action['link_query'], excluded,
            **({'purpose': action['purpose']} if 'purpose' in action else {}))

    def _supplement_allowed(self, record, action, urls):
        if action.get('purpose') == 'cached_attribution_v1':
            return urls == [action['target_url']] == record.get('link_result', {}).get('discovery', {}).get('urls')
        if action.get('purpose') == 'dimension_evidence_v6' and record.get('link_result', {}).get('discovery') is None:
            from .comparison_evidence_discovery import _matches, _inventory, _passages
            from .project_scope import requires_project, same_project
            before = next((r for r, _ in self._navigation_records() if r['action'] == action), None)
            consumed = self._consumed_urls(before)
            if not 1 <= len(urls) <= 2 or any(url in consumed for url in urls):
                return False
            request = {'source_url': action['source_url'], 'topic': action['topic']}
            if not all(_matches(request, url) and (not requires_project(url)
                       or not requires_project(action['source_url']) or same_project(action['source_url'], url)) for url in urls):
                return False
            if len(urls) == 2:
                before = next((r for r, _ in self._navigation_records() if r['action'] == action), None)
                pages, _ = _inventory(self._navigation_inputs(before))
                return any(p['url'] == urls[1] and _host(p['url']) == _host(urls[0]) and _passages(p)[1] for p in pages)
            return True
        if action.get('purpose') in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_failed_seed import load_repair_history
            from .project_scope import requires_project, same_project
            history = (self._repair_input()[1] if action['purpose'] in ('dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6') else
                       load_repair_history(self.goal['comparison']['seed']))
            source = action['source_id']
            from .comparison_seed import load_seed
            original = (action['source_url'] if action['purpose'] in ('dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6') else
                        next((item['url'] for item, _ in load_seed(self.goal['comparison']['seed'])
                              if item['id'] == source), None))
            return ((1 <= len(urls) <= 2 if action['purpose'] in ('dimension_evidence_v5', 'dimension_evidence_v6') else len(urls) == 1)
                    and (action['purpose'] not in ('dimension_evidence_v5', 'dimension_evidence_v6') or urls == record.get('link_result', {}).get('discovery', {}).get('urls'))
                    and all(url not in history['read_urls'] for url in urls)
                    and original is not None and all(not requires_project(url)
                        or not requires_project(original) or same_project(original, url) for url in urls))
        if action.get('purpose') in {'project_attribution', 'dynamic_project_attribution'}:
            from .project_scope import same_project
            if not all(same_project(action['scope_url'], url) for url in urls):
                return False
        if action.get('purpose') == 'repository_attribution':
            from .publisher_scope import same_repository
            if not all(same_repository(action['scope_url'], url) for url in urls):
                return False
        linked = record.get('link_result', {}).get('discovery')
        if linked is not None:
            return urls == linked['urls']
        return super()._supplement_allowed(record, action, urls)

    def _search_candidates(self, action, query):
        if action.get('purpose') == 'cached_attribution_v1':
            raise ComputerError('search_unavailable')
        if action.get('purpose') == 'dimension_evidence_v6':
            result = super()._search_candidates(action, query)
            from .comparison_evidence_discovery import _inventory, _passages, _matches
            consumed = self._consumed_urls()
            request = {'source_url': action['source_url'], 'topic': action['topic']}
            from .project_scope import requires_project, same_project
            urls = [url for url in result['urls'] if url not in consumed and _matches(request, url)
                    and (not requires_project(url) or not requires_project(action['source_url'])
                         or same_project(action['source_url'], url))]
            if not urls:
                raise ComputerError('search_unavailable')
            chosen = [urls[0]]
            pages, _ = _inventory(self._navigation_inputs())
            author = next((page['url'] for page in pages if page['url'] not in consumed
                           and page['url'] != chosen[0] and _host(page['url']) == _host(chosen[0])
                           and _passages(page)[1]), None)
            if author:
                chosen.append(author)
            return {**result, 'urls': chosen}
        if action.get('purpose') in ('dimension_evidence_v5', 'dimension_evidence_v6'):
            # This checkpoint already consumed the product formula searches.
            # New navigation is bounded to captured links and literal URLs.
            raise ComputerError('search_unavailable')
        result = super()._search_candidates(action, query)
        if action.get('purpose') not in ('dimension_evidence_v1', 'dimension_evidence_v2', 'dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            return result
        from .computer_search import validate_result
        from .comparison_failed_seed import load_repair_history
        validate_result(result, 2)
        history = (self._repair_input()[1] if action['purpose'] in ('dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6') else
                   load_repair_history(self.goal['comparison']['seed']))
        consumed = set(history['read_urls'])
        urls = [url for url in result['urls'] if url not in consumed]
        if action['purpose'] in ('dimension_evidence_v3', 'dimension_evidence_v4', 'dimension_evidence_v5', 'dimension_evidence_v6'):
            from .comparison_dimension_discovery import _TOPIC_STOP
            topic = re.sub(r'\bsite:\S+', '', action['topic'], flags=re.I)
            terms = set(re.findall(r'[a-z][a-z0-9]*', topic.casefold())) - _TOPIC_STOP
            from urllib.parse import unquote
            urls = [url for url in urls if not terms or any(
                term in unquote(url).casefold() for term in terms)]
            from .project_scope import requires_project, same_project
            urls = [url for url in urls if not requires_project(url)
                    or not requires_project(action['source_url'])
                    or same_project(action['source_url'], url)]
        if not urls:
            raise ComputerError('search_unavailable')
        return {**result, 'urls': urls[:1]}

    def _add_active(self, facts, sid):
        active = [*facts['active_ids'], sid]
        if len(active) > self.active_limit:
            by_id = {item['id']: item for item in facts['candidates']}
            assessment = facts['comparisons'][0]['comparison']['assessment']
            cells = assessment['cells']
            support = assessment['publisher_evidence']
            groups = assessment['groups']
            def contribution(old):
                group = next((g for g in groups if old in g), [old])
                lost = sum(cell['status'] == 'relevant' and not any(
                    other != old and other in active and other in cells
                    and cells[other][i]['status'] == 'relevant' for other in group)
                    for i, cell in enumerate(cells.get(old, [])))
                # New, not-yet-reviewed evidence is retained until final review.
                dependents = assessment['publisher_support']
                lost_identity = sum(anchor == old and other != old and other in active
                    for other, anchor in dependents.items())
                return (old not in cells, lost_identity, lost,
                        support.get(old) == 'supported', active.index(old))
            eligible = [old for old in active[:-1] if sum(
                by_id[v]['query_index'] == by_id[old]['query_index'] for v in active) > 1]
            if not eligible:
                raise ComputerError('comparison_active_limit')
            victim = min(eligible, key=contribution)
            active.remove(victim)
            facts['replaced_ids'].append(victim)
        facts['active_ids'] = active

    def observe_final(self):
        value = super().observe_final()
        value['comparison_review'] = 'bounded_crosssite_v4'
        value['imported_source_ids'] = self.goal['comparison']['seed']['source_ids']
        return value


def followup_manifest(previous_run, source_ids=None):
    from .comparison_seed import create_manifest, load_seed
    try:
        return create_manifest(previous_run) if source_ids is None else create_manifest(previous_run, source_ids)
    except ComputerError as exc:
        from .verification_progress import checkpoint
        checkpoint()  # A stop request must not enter another import fallback.
        if str(exc) != 'comparison_failed_seed_invalid':
            raise
    from .comparison_compact_seed import create_manifest as compact_manifest
    manifest = compact_manifest(previous_run, source_ids)
    if source_ids is None:
        # Retain the latest two captures per query: a direct answer may need a
        # separate attribution page. Older bytes/work remain in the full graph;
        # selection does not inherit any previous semantic verdict.
        chosen = _recent_source_ids(load_seed(manifest))
        manifest = compact_manifest(previous_run, chosen)
    return manifest


def _recent_source_ids(captures):
    selected, counts = set(), {}
    for item, _ in reversed(captures):
        query = item['query_index']
        if counts.get(query, 0) < 2:
            selected.add(item['id'])
            counts[query] = counts.get(query, 0) + 1
    return [item['id'] for item, _ in captures if item['id'] in selected]


def prepare_followup(session, instruction, goal, previous_run, source_ids=None):
    """New explicit objective; reuse proven captures, never old review conclusions."""
    from .computer_agent import prepare
    manifest = followup_manifest(previous_run, source_ids)
    goal = deepcopy(goal)
    if goal.get('queries') != manifest['queries'] or 'comparison' not in goal:
        raise ComputerError('comparison_seed_queries_changed')
    goal['comparison'].update(evidence_version=4, seed=manifest,
                              completion_policy=('dimension_first_v4' if manifest['version'] in (4, 5)
                                                 else 'original_query_fallback_v1'))
    if manifest['version'] in (4, 5):
        goal['comparison']['dimension_routing'] = 6 if manifest['version'] == 5 else 4
    return prepare(session, instruction, goal)
