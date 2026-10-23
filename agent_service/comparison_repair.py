"""Versioned capture-only failure repair; consumed work never grants a verdict."""
from copy import deepcopy

from .computer import ComputerError, digest, read_record

POLICY = 'capture_repair_v2'
DIMENSION_POLICY = 'dimension_repair_v3'
DIMENSION_FIRST_POLICY = 'dimension_first_v4'
FORMAT_FAILURES = frozenset({'comparison_transport_invalid', 'comparison_review_invalid',
                             'comparison_evidence_invalid'})


def enabled(goal):
    policy = goal.get('comparison', {}).get('completion_policy')
    return type(policy) is str and policy in {POLICY, DIMENSION_POLICY, DIMENSION_FIRST_POLICY}


def supplement_key(action):
    return digest({key: action.get(key) for key in ('query', 'link_query', 'purpose')})


def format_retry(facts):
    if len(facts['comparisons']) != 1:
        return False
    first = facts['comparisons'][0]
    return ('comparison' not in first and first['result']['ok'] is False
            and first['result']['state'] == 'failed' and first['result']['code'] in FORMAT_FAILURES)


def history(goal, facts):
    """Called only within the failed loader's full replay and byte checks."""
    previous = facts.get('repair_history', {})
    ancestor = goal['comparison'].get('seed')
    if ancestor is not None and ancestor['version'] in {4, 5} and not previous:
        from .comparison_failed_seed import load_repair_history
        previous = load_repair_history(ancestor)
    queries = set(facts['queries']) | set(previous.get('queries', []))
    reads = ({item['url'] for item in facts['candidates'] if item['id'] in facts['reads']}
             | set(previous.get('read_urls', [])))
    keys = {r['action']['need_id']: supplement_key(r['action'])
            for r, _ in facts['records'] if r['action']['kind'] == 'supplement'}
    candidates = deepcopy(facts['candidates'])
    known = {item['id']: item for item in candidates}
    for item in previous.get('candidates', []):
        if item['id'] not in known:
            copied = deepcopy(item)
            candidates.append(copied); known[item['id']] = copied
    if ancestor is not None and ancestor['version'] == 2:
        # _verified already validates this ordinary research ancestor and its
        # at-most-one parent. A failed ancestor contributes its verified history.
        from .comparison_seed import _run_root
        root = _run_root(ancestor['run'])
        plan = read_record(root.with_suffix('.json'))
        roots = [root]
        if 'recovery' in plan:
            roots.insert(0, _run_root(plan['recovery']['parent_run']))
        for origin in roots:
            if read_record(origin.with_suffix('.json'))['goal']['queries'] != goal['queries']:
                raise ComputerError('comparison_seed_queries_changed')
            for path in sorted(origin.glob('step-*/environment-result.json')):
                record = read_record(path)['data']; action = record['action']
                if action['kind'] == 'search':
                    queries.add(action['query_index'])
                    if record['result']['ok']:
                        for url in record['discovery']['urls']:
                            sid = digest(url)[:24]
                            if sid not in known:
                                item = {'id': sid, 'url': url, 'query_index': action['query_index']}
                                candidates.append(item); known[sid] = item
                elif action['kind'] == 'read':
                    reads.add(known[action['candidate_id']]['url'])
    pending = []
    for item in candidates:
        if item['url'] not in reads:
            if 'need_id' in item:
                if item['need_id'] in keys:
                    item['prior_need_key'] = keys[item['need_id']]
                elif 'prior_need_key' not in item:
                    raise ComputerError('comparison_failed_seed_invalid')
            pending.append(item)
    result = {'queries': sorted(queries), 'read_urls': sorted(reads),
              'candidates': pending, 'supplement_keys': sorted(
                  set(keys.values()) | set(previous.get('supplement_keys', [])))}
    # Keep the whole replay-verified link result: even no selected URLs may
    # contain fetched documents useful for a later, different evidence need.
    discoveries = deepcopy(previous.get('discoveries', []))
    seen = {digest(value) for value in discoveries}
    for record, _ in facts['records']:
        value = record.get('link_result')
        if value is not None and digest(value) not in seen:
            discoveries.append(deepcopy(value)); seen.add(digest(value))
    if discoveries:
        result['discoveries'] = discoveries
    return result


def import_history(facts, value, *, limit=8):
    facts['repair_history'] = deepcopy(value)
    facts['queries'] = sorted(set(facts['queries']) | set(value['queries']))
    ids = {item['id'] for item in facts['candidates']}
    for item in value['candidates']:
        if item['id'] not in ids:
            facts['candidates'].append(deepcopy(item)); ids.add(item['id'])
    if type(limit) is not int or limit not in (8, 10, 16) or candidate_count(facts) > limit:
        raise ComputerError('comparison_seed_candidate_limit')


def candidate_count(facts):
    return len({item['url'] for item in facts['candidates']}
               | set(facts.get('repair_history', {}).get('read_urls', [])))


def frontier_view(facts):
    """Do not invent receipts; project consumed work only into decision inputs."""
    result = dict(facts)
    history = facts['repair_history']
    result['reads'] = dict(facts['reads'])
    for item in facts['candidates']:
        if item['url'] in history['read_urls'] and item['id'] not in result['reads']:
            result['reads'][item['id']] = {'result': {'ok': False}}
    consumed = {need['need_id'] for need in facts['needs']
                if supplement_key(need) in history['supplement_keys']}
    result['supplements'] = [*facts['supplements'], *sorted(consumed)]
    return result
