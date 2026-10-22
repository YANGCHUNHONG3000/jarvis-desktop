"""Version-four follow-up: verified captures plus bounded cross-site evidence."""
from copy import deepcopy

from .computer import ComputerError, digest
from .comparison_completion import CompletionEnvironment, _host


class CrossSiteEnvironment(CompletionEnvironment):
    active_limit = 6

    def _reader(self, query):
        from .browser import BrowserProcess
        return self.browser or BrowserProcess(query=query, include_attribution=True)

    def _empty(self):
        from .comparison_seed import load_seed
        facts = super()._empty()
        captured = load_seed(self.goal['comparison']['seed'])
        if self.goal['queries'] != self.goal['comparison']['seed']['queries']:
            raise ComputerError('comparison_seed_queries_changed')
        for item, record in captured:
            facts['candidates'].append(deepcopy(item))
            facts['reads'][item['id']] = deepcopy(record)
            facts['active_ids'].append(item['id'])
        facts['queries'] = sorted({item['query_index'] for item, _ in captured})
        return facts

    def _need_satisfied(self, facts, need):
        return sum(facts['reads'][sid]['result']['ok']
                   for sid in facts['need_reads'].get(need['need_id'], [])) >= 2

    def _matches_need(self, item, need):
        # A cross-host read is permitted only after validated actual-anchor discovery.
        return item.get('need_id') == need['need_id']

    def _frontier(self, facts):
        actions = super()._frontier(facts)
        if actions and actions[0]['kind'] == 'supplement' and len(facts['candidates']) > 6:
            # Each supplement can discover two candidates. Never dispatch a
            # request that could overflow the immutable eight-candidate bound.
            changed = facts['active_ids'] != facts['comparisons'][0]['active_ids']
            remaining = 12 - len(facts['records'])
            return [{'kind': 'compare'}] if changed and remaining >= 1 + len(self.goal['outputs']) else []
        return actions

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
            suffix = ' '.join(missing) or 'about company publisher'
            query = f"site:{_host(root['url'])} {suffix}"[:500]
            need = {'host': _host(root['url']), 'query': query, 'link_query': query,
                    'source_id': root['id'],
                    'capture_sha256': digest(facts['reads'][root['id']]['observation']),
                    'review_sha256': digest(evidence)}
            need['need_id'] = digest(need)[:24]
            needs.append(need)
        return needs[:2]

    def _link_inputs(self, facts, action):
        item = next((item for item in facts['candidates'] if item['id'] == action['source_id']), None)
        if item is None or digest(facts['reads'][item['id']]['observation']) != action['capture_sha256']:
            raise ComputerError('comparison_seed_capture_changed')
        return item['url'], [i['url'] for i in facts['candidates'] if i['id'] in facts['reads']]

    def _discover_links(self, source, action, excluded):
        from .comparison_crosssite_discovery import CrossSiteDiscoveryProcess
        try:
            return CrossSiteDiscoveryProcess().discover(source, action['link_query'], excluded,
                should_stop=lambda: self.stop() or self.clock() >= self.deadline,
                timeout=min(35, self.deadline-self.clock()))
        except ComputerError as exc:
            mapped = {'supplement_crosssite_timeout': 'supplement_links_timeout',
                      'supplement_crosssite_unavailable': 'supplement_links_unavailable'}.get(str(exc))
            if mapped:
                raise ComputerError(mapped) from None
            raise

    def _validate_links(self, linked, source, action, excluded):
        from .comparison_crosssite_discovery import validate_crosssite_result
        return validate_crosssite_result(linked, source, action['link_query'], excluded)

    def _supplement_allowed(self, record, action, urls):
        linked = record.get('link_result', {}).get('discovery')
        if linked is not None:
            return urls == linked['urls']
        return super()._supplement_allowed(record, action, urls)

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


def prepare_followup(session, instruction, goal, previous_run, source_ids=None):
    """New explicit objective; reuse proven captures, never old review conclusions."""
    from .comparison_seed import create_manifest
    from .computer_agent import prepare
    manifest = create_manifest(previous_run, source_ids)
    goal = deepcopy(goal)
    if goal.get('queries') != manifest['queries'] or 'comparison' not in goal:
        raise ComputerError('comparison_seed_queries_changed')
    goal['comparison'].update(evidence_version=4, seed=manifest)
    return prepare(session, instruction, goal)
