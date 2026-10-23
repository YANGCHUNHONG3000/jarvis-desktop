"""Failed capture follow-ups repair locally, with bounded offline review retries.

The old failed producers and import verification are real. Format loop tests
cache verified import copies during execution; history, byte-race, ancestor, and
backup reuse contracts retain real loaders. Browser, model, and Office executors
are existing offline fixtures; no external calls.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from agent_service import comparison_seed as seed, computer_agent as engine, research_recovery as recovery
from agent_service.comparison_crosssite import CrossSiteEnvironment, prepare_followup
from agent_service.comparison_failed_seed import load_repair_history
from agent_service.comparison_repair import (
    candidate_count, format_retry, history as repair_history, import_history, supplement_key,
)
from agent_service.computer import ComputerError
from agent_service.source_comparison import validate_comparison
from agent_service import publisher_evidence
from agent_service.comparison_review import assess
from test_comparison_failed_seed import (
    failed, failed_model, install_roots, records, select_model, snapshot,
)
from test_comparison_completion import configure
from test_comparison_loop import ctx, run
from test_comparison_query_fallback import _frontier_facts
from test_comparison_seed import shape
from test_comparison_review import fixture as review_fixture
from test_computer_agent import Client
from test_computer_agent_tools import ctx as tool_ctx, A, B


def followup(c, root, original_plan, *, legacy=False, outputs=None, current_policy=False):
    goal = deepcopy(original_plan['goal'])
    if outputs is not None:
        goal['outputs'] = outputs
    plan = prepare_followup(c['session'], 'bounded failed capture repair',
                            goal, root)
    if legacy or not current_policy:
        # Pin the earlier policy cases as the new follow-up default advances.
        from agent_service.computer_goals import _save_goal
        body = {key: deepcopy(value) for key, value in plan.items() if key != 'sha256'}
        body['goal']['comparison']['completion_policy'] = (
            'original_query_fallback_v1' if legacy else 'capture_repair_v2')
        body['goal']['comparison'].pop('dimension_routing', None)
        plan = _save_goal(c['session'], body)
    return plan


def flaky_model(c, *, failure='format', always=False, stage='selection'):
    selections = []

    def select(data):
        if ('selection' in data) == (stage == 'review'):
            selections.append(data)
            if always or len(selections) == 1:
                if failure == 'format':
                    return {'sources': []}
                raise ComputerError(failure)
        return select_model(data)

    c['client'] = Client(select)
    return selections


def run_cached_imports(c, plan, monkeypatch, **options):
    """Isolate loop decisions after real validation; byte-race tests stay uncached."""
    from agent_service import comparison_failed_seed
    manifest = plan['goal']['comparison']['seed']
    real_load, real_history = seed.load_seed, comparison_failed_seed.load_repair_history
    captured, history = real_load(manifest), real_history(manifest)
    with monkeypatch.context() as loop:
        loop.setattr(seed, 'load_seed', lambda value: deepcopy(captured)
                     if value == manifest else real_load(value))
        loop.setattr(comparison_failed_seed, 'load_repair_history', lambda value: deepcopy(history)
                     if value == manifest else real_history(value))
        return run(c, plan, **options)


def test_failed_followup_alone_selects_capture_repair_policy(failed):
    c, root, original, _ = failed
    plan = followup(c, root, original, current_policy=True)
    assert plan['goal']['comparison']['evidence_version'] == 4
    assert plan['goal']['comparison']['seed']['version'] == 4
    assert plan['goal']['comparison']['completion_policy'] == 'dimension_first_v4'
    assert plan['goal']['comparison']['dimension_routing'] == 4
    assert original['goal']['comparison'].get('completion_policy') != 'dimension_first_v4'


@pytest.mark.parametrize('pause', [None, 'before-retry', 'after-retry'])
@pytest.mark.parametrize('stage', ['selection', 'review'])
def test_one_format_retry_uses_same_captures_and_survives_pause(failed, pause, stage, monkeypatch):
    c, root, original, _ = failed
    old_bytes = snapshot(root)
    searches, reads = list(c['search'].calls), list(c['browser'].calls)
    selections = flaky_model(c, stage=stage)
    plan = followup(c, root, original, outputs=['document'])
    result = run_cached_imports(c, plan, monkeypatch, halt=1 if pause == 'before-retry' else None,
                               pause_after_step=(lambda: True) if pause == 'after-retry' else (lambda: False))
    saved = records(Path(result['run']))
    if pause:
        assert not result['ok'], result
        if pause == 'after-retry':
            assert result['code'] == 'agent_paused', result
        assert [row['action']['kind'] for row in saved] == ['compare'] * (2 if pause == 'after-retry' else 1)
        assert not saved[0]['result']['ok'] and 'comparison' not in saved[0]
        preserved = snapshot(Path(result['run']))
        plan = recovery.prepare(c['session'], result['run'])['plan']
        result = run_cached_imports(c, plan, monkeypatch)
        assert all(Path(path).read_bytes() == content for path, content in preserved.items())
        saved += records(Path(result['run']))
    assert result['ok'], result
    assert [row['action']['kind'] for row in saved] == ['compare', 'compare', 'document']
    comparisons = [row for row in saved if row['action']['kind'] == 'compare']
    assert comparisons[0]['active_ids'] == comparisons[1]['active_ids']
    assert [row['result']['ok'] for row in comparisons] == [False, True]
    assert len(selections) == 2 and len(c['client'].calls) == (4 if stage == 'review' else 3)
    assert (c['search'].calls, c['browser'].calls) == (searches, reads)
    assert snapshot(root) == old_bytes


def test_second_format_failure_stops_with_two_reviews(failed, monkeypatch):
    c, root, original, _ = failed
    searches, reads = list(c['search'].calls), list(c['browser'].calls)
    selections = flaky_model(c, always=True)
    result = run_cached_imports(c, followup(c, root, original), monkeypatch)
    assert not result['ok'] and not c['effects'], result
    saved = records(Path(result['run']))
    assert [row['action']['kind'] for row in saved] == ['compare', 'compare']
    assert len(selections) == len(c['client'].calls) == 2
    assert (c['search'].calls, c['browser'].calls) == (searches, reads)


@pytest.mark.parametrize('failure', ['model_budget_exceeded', 'unknown_model_failure'])
def test_budget_or_unknown_failure_never_requests_format_retry(failed, failure, monkeypatch):
    c, root, original, _ = failed
    selections = flaky_model(c, failure=failure)
    result = run_cached_imports(c, followup(c, root, original), monkeypatch)
    assert not result['ok'] and not c['effects'], result
    assert [row['action']['kind'] for row in records(Path(result['run']))] == ['compare']
    assert len(selections) == len(c['client'].calls) == 1


def test_historical_policy_keeps_format_failure_terminal(failed, monkeypatch):
    c, root, original, _ = failed
    selections = flaky_model(c)
    result = run_cached_imports(c, followup(c, root, original, legacy=True), monkeypatch)
    assert not result['ok'] and not c['effects'], result
    assert [row['action']['kind'] for row in records(Path(result['run']))] == ['compare']
    assert len(selections) == len(c['client'].calls) == 1


def test_history_is_verified_copied_and_rechecked_on_cache_hit(failed):
    c, root, original, _ = failed
    manifest = seed.create_manifest(root)
    before = snapshot(root)
    value = load_repair_history(manifest)
    assert value == {'queries': [0, 1], 'read_urls': sorted(c['browser'].calls),
                     'candidates': [], 'supplement_keys': []}
    value['queries'].clear()
    value['read_urls'].append('https://caller.example/')
    assert load_repair_history(manifest)['queries'] == [0, 1]
    assert load_repair_history(manifest)['read_urls'] == sorted(c['browser'].calls)
    assert snapshot(root) == before
    target = root / 'step-01' / 'environment-result.json'
    content = target.read_bytes()
    try:
        target.write_bytes(content + b' ')
        with pytest.raises(ComputerError):
            load_repair_history(manifest)
    finally:
        target.write_bytes(content)
    assert load_repair_history(manifest)['queries'] == [0, 1]


def test_history_preserves_attempted_supplement_key_without_binding_old_review(ctx, monkeypatch):
    c = configure(ctx)
    install_roots(c, monkeypatch)
    original_search = c['search'].search

    def unavailable(query, *args, **kwargs):
        if query not in c['goal']['queries']:
            c['search'].calls.append(query)
            raise ComputerError('search_unavailable')
        return original_search(query, *args, **kwargs)

    c['search'].search = unavailable
    plan = engine.prepare(c['session'], 'retain exhausted supplementary search', c['goal'])
    result = run(c, plan)
    assert not result['ok'] and not c['effects'], result
    root = Path(result['run'])
    attempted = [row['action'] for row in records(root) if row['action']['kind'] == 'supplement']
    assert len(attempted) == 1
    history = load_repair_history(seed.create_manifest(root))
    assert history['supplement_keys'] == [supplement_key(attempted[0])]
    changed_review = {**attempted[0], 'review_sha256': 'new model response', 'need_id': 'new id'}
    assert supplement_key(changed_review) == history['supplement_keys'][0]
    assert history['read_urls'] == sorted(c['browser'].calls)


def test_actual_new_need_uses_unread_history_without_researching_old_queries(ctx, monkeypatch):
    c = configure(ctx, cached=True)
    # This is an original-query backup, so its later read still satisfies the
    # approved terms rather than inheriting a supplementary-read exemption.
    c['browser'].text[A + 'about'] += '; alpha fact omega'
    install_roots(c, monkeypatch)
    valid_model = c['client'].select
    c['client'] = Client(failed_model)
    original = engine.prepare(c['session'], 'retain unused about candidate', c['goal'])
    previous = run(c, original)
    assert not previous['ok'] and not c['effects'], previous
    root = Path(previous['run'])
    old_bytes = snapshot(root)
    manifest = seed.create_manifest(root)
    history = load_repair_history(manifest)
    assert history['read_urls'] == sorted([A, B])
    assert [item['url'] for item in history['candidates']] == [A + 'about']
    assert history['queries'] == [0, 1]
    original_calls = list(c['search'].calls)

    def unavailable(query, *args, **kwargs):
        assert query not in original['goal']['queries'], 'consumed original query repeated'
        c['search'].calls.append(query)
        raise ComputerError('search_unavailable')

    c['search'].search = unavailable
    c['client'] = Client(valid_model)
    result = run(c, followup(c, root, original))
    assert result['ok'], result
    rows = records(Path(result['run']))
    assert [row['action']['kind'] for row in rows] == [
        'compare', 'supplement', 'read', 'compare', 'spreadsheet', 'document']
    assert len(c['search'].calls) == len(original_calls) + 1
    assert c['search'].calls[:len(original_calls)] == original_calls
    assert c['browser'].calls == [A, B, A + 'about']
    assert len(c['browser'].calls) == len(set(c['browser'].calls))
    assert rows[1]['result']['code'] == 'search_unavailable'
    assert snapshot(root) == old_bytes


def test_failed_read_and_unselected_successes_are_consumed_without_fabricating_imports(ctx, monkeypatch):
    c = ctx
    install_roots(c, monkeypatch)
    rejected = A + 'rejected'
    c['goal']['min_sources'] = 3
    c['search'].results['first'] = [A, rejected]
    c['browser'].text[rejected] = 'irrelevant text without required terms'
    c['client'] = Client(failed_model)
    original = engine.prepare(c['session'], 'preserve unsuccessful read attempts', c['goal'])
    previous = run(c, original)
    assert not previous['ok'] and rejected in c['browser'].calls, previous
    root = Path(previous['run'])
    manifest = seed.create_manifest(root)
    history = load_repair_history(manifest)
    assert rejected in history['read_urls']
    assert len(history['read_urls']) == 4 and history['candidates'] == []
    pairs = seed.load_seed(manifest)
    selected = [next(item['id'] for item, _ in pairs if item['query_index'] == index)
                for index in (0, 1)]
    plan = prepare_followup(c['session'], 'only selected captures may count',
                            deepcopy(original['goal']), root, selected)
    calls = list(c['search'].calls), list(c['browser'].calls)
    c['client'] = Client(select_model)
    result = run(c, plan)
    assert not result['ok'] and result['attempts'] == [], result
    assert result['source_reads'] == 2  # Consumed attempts are not accepted imports.
    assert (c['search'].calls, c['browser'].calls) == calls
    assert not c['client'].calls and not c['effects']


def test_verified_research_ancestor_contributes_failed_reads_and_unread_candidates(ctx, monkeypatch):
    from agent_service import comparison_research_seed
    c = ctx
    install_roots(c, monkeypatch)
    monkeypatch.setattr(comparison_research_seed, 'TASK_BASE', c['options']['task_base'])
    rejected, unused = A + 'rejected', B + 'unread'
    c['search'].results = {'first': [rejected, A], 'second': [B, unused]}
    c['browser'].text[rejected] = 'irrelevant text without required terms'
    c['goal'].pop('comparison')
    c['goal']['min_sources'] = 2
    c['client'] = Client()
    research_plan = engine.prepare(c['session'], 'research ancestor with consumed attempts', c['goal'])
    research = run(c, research_plan)
    assert research['ok'], research
    ancestor = Path(research['run'])
    before = snapshot(ancestor)
    objective = deepcopy(research_plan['goal'])
    objective['comparison'] = {'dimensions': ['fact'], 'min_domains': 2}
    plan = prepare_followup(c['session'], 'failed comparison after verified research', objective, ancestor)
    assert plan['goal']['comparison']['completion_policy'] == 'original_query_fallback_v1'
    manifest = plan['goal']['comparison']['seed']
    real_load = seed.load_seed
    verified = real_load(manifest)
    c['client'] = Client(failed_model)
    # Cache only the already verified producer input. The history assertion below
    # uses the full real verifier, including ancestor bytes and native receipts.
    with monkeypatch.context() as producer:
        producer.setattr(seed, 'load_seed', lambda value: deepcopy(verified)
                         if value == manifest else real_load(value))
        result = run(c, plan)
    assert not result['ok'], result
    failed_root = Path(result['run'])
    value = load_repair_history(seed.create_manifest(failed_root))
    assert value['queries'] == [0, 1]
    assert value['read_urls'] == sorted([rejected, A, B])
    assert [item['url'] for item in value['candidates']] == [unused]
    assert value['supplement_keys'] == []
    assert snapshot(ancestor) == before


def test_stable_supplement_key_blocks_same_need_and_allows_different_missing_item():
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['spreadsheet', 'document'],
                'comparison': {'completion_policy': 'capture_repair_v2'}}
    facts = _frontier_facts(pending=False)
    need = facts['needs'][0]
    need.update(query='site:alpha.example format', link_query='site:alpha.example format',
                purpose='attribution', review_sha256='old review')
    key = supplement_key(need)
    facts['supplements'] = []
    facts['repair_history'] = {'queries': [0, 1], 'read_urls': [], 'candidates': [],
                               'supplement_keys': [key]}
    need['review_sha256'] = 'new independent review'
    need['need_id'] = 'new evidence bound need'
    assert supplement_key(need) == key
    assert env._frontier(facts) == []
    need['query'] = need['link_query'] = 'site:alpha.example price'
    assert supplement_key(need) != key
    assert env._frontier(facts) == [{'kind': 'supplement', **need}]


def test_consumed_reads_and_queries_are_only_projected_into_new_policy_decisions():
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['document'],
                'comparison': {'completion_policy': 'capture_repair_v2'}}
    facts = _frontier_facts(candidates=3, pending=False)
    facts['repair_history'] = {'queries': [0, 1], 'read_urls': ['https://gamma.test/'],
                               'candidates': [], 'supplement_keys': []}
    before = deepcopy(facts)
    assert env._frontier(facts) == []
    assert facts == before  # No fabricated receipts enter evidence or persistence.
    env.goal['comparison']['completion_policy'] = 'original_query_fallback_v1'
    assert env._frontier(facts) == [{'kind': 'read', 'candidate_id': 'unused'}]


@pytest.mark.parametrize('version,seed_version,policy', [
    (3, 4, 'capture_repair_v2'), (4, 1, 'capture_repair_v2'),
    (4, 2, 'capture_repair_v2'), (4, 3, 'capture_repair_v2'),
    (4, 4, []), (4, 4, {}), (4, 4, True), (4, 4, 'unknown'),
])
def test_new_policy_rejects_other_evidence_seeds_and_malformed_policy(version, seed_version, policy):
    manifest = shape()
    manifest.update(version=seed_version, outputs={} if seed_version == 4 else manifest['outputs'])
    objective = {'dimensions': ['fact'], 'min_domains': 2, 'evidence_version': version,
                 'seed': manifest, 'completion_policy': policy}
    with pytest.raises(ComputerError, match='comparison_invalid'):
        validate_comparison(objective)


@pytest.mark.parametrize('boundary', ['unknown_effect', 'second_comparison', 'has_evidence', 'budget'])
def test_retry_guard_refuses_uncertain_second_semantic_or_budget_failure(boundary):
    row = {'result': {'ok': False, 'state': 'failed', 'code': 'comparison_evidence_invalid'}}
    facts = {'comparisons': [row]}
    if boundary == 'unknown_effect':
        row['result']['state'] = 'unknown'
    elif boundary == 'second_comparison':
        facts['comparisons'].append(deepcopy(row))
    elif boundary == 'has_evidence':
        row['comparison'] = {'assessment': {'eligible': False}}
    else:
        row['result']['code'] = 'model_budget_exceeded'
    assert format_retry(facts) is False


@pytest.mark.parametrize('pending', [False, True], ids=['supplement', 'original-query'])
def test_eight_consumed_urls_reserve_no_room_for_further_discovery(pending):
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'queries': ['first', 'second'], 'outputs': ['document'],
                'comparison': {'completion_policy': 'capture_repair_v2'}}
    facts = _frontier_facts(pending=pending)
    facts['repair_history'] = {
        'queries': [0], 'read_urls': [item['url'] for item in facts['candidates']] +
        [f'https://consumed{i}.example/' for i in range(6)],
        'candidates': [], 'supplement_keys': []}
    facts['needs'][0].update(query='site:alpha.example format', link_query='site:alpha.example format')
    if not pending:
        facts['supplements'] = []
    assert candidate_count(facts) == 8
    assert env._frontier(facts) == []


def test_ninth_consumed_url_is_rejected_before_a_frontier_can_be_created():
    facts = {'candidates': [], 'queries': []}
    history = {'queries': [0], 'read_urls': [f'https://consumed{i}.example/' for i in range(9)],
               'candidates': [], 'supplement_keys': []}
    with pytest.raises(ComputerError, match='comparison_seed_candidate_limit'):
        import_history(facts, history)


def test_unread_supplement_candidate_rebinds_stable_need_with_repository_scope_preserved():
    action = {'kind': 'supplement', 'need_id': 'old need', 'query': 'site:github.com/acme/project authors',
              'link_query': 'site:github.com/acme/project authors', 'purpose': 'repository_attribution',
              'scope_url': 'https://github.com/acme/project/blob/main/README.md',
              'review_sha256': 'old review'}
    item = {'id': 'candidate', 'url': 'https://github.com/acme/project/blob/main/AUTHORS',
            'query_index': 0, 'need_id': action['need_id']}
    value = repair_history({'comparison': {}}, {'queries': [0], 'reads': {}, 'candidates': [item],
                           'records': [({'action': action}, 'verified record hash')]})
    pending = value['candidates'][0]
    assert pending['prior_need_key'] == supplement_key(action)
    assert item == {key: val for key, val in pending.items() if key != 'prior_need_key'}
    need = {**action, 'need_id': 'new need', 'review_sha256': 'new review'}
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'completion_policy': 'capture_repair_v2'}}
    assert env._matches_need(pending, need)
    pending['url'] = 'https://github.com/acme/other/blob/main/AUTHORS'
    assert not env._matches_need(pending, need)
    pending['url'] = item['url']
    env.goal['comparison']['completion_policy'] = 'original_query_fallback_v1'
    assert not env._matches_need(pending, need)


# UTF-8 prompt hashes captured and compared with the pre-repair HEAD function.
# These protect historical replay without requiring Git or executing old source.
PREVIOUS_PUBLISHER_PROMPTS = {
    ('explicit_attribution_v1', 3): 'ff055fe4e3b8616897e53623bccb9cd30b6e3cc6019e36a1b27c9d2463dc8e36',
    ('explicit_attribution_v1', 4): 'ff055fe4e3b8616897e53623bccb9cd30b6e3cc6019e36a1b27c9d2463dc8e36',
    ('repository_attribution_v2', 3): '9c9fad761b112be1e966b608f65a9cda9c7ea4daf900441004e162e5c6207974',
    ('repository_attribution_v2', 4): '9c9fad761b112be1e966b608f65a9cda9c7ea4daf900441004e162e5c6207974',
    ('project_attribution_v3', 3): 'fcd0c8a10f51beb4c783e0c39216ef50239653e90ad889d7fb6d8fb431bbd605',
    ('project_attribution_v3', 4): 'fcd0c8a10f51beb4c783e0c39216ef50239653e90ad889d7fb6d8fb431bbd605',
    ('project_attribution_v4', 3): 'fcd0c8a10f51beb4c783e0c39216ef50239653e90ad889d7fb6d8fb431bbd605',
    ('project_attribution_v4', 4): 'fcd0c8a10f51beb4c783e0c39216ef50239653e90ad889d7fb6d8fb431bbd605',
    ('project_attribution_v5', 3): 'da096f2ef730fca0690cbc92cb0b4023992a1d6b16f598688ca6e824a97306a8',
    ('project_attribution_v5', 4): 'f517d5e225c1bd9d8dd1c37fd68044dd69dcdc21e951793dc61cef2d09d77d10',
}


@pytest.mark.parametrize('policy', publisher_evidence.POLICIES)
@pytest.mark.parametrize('version', [3, 4])
def test_copyright_prompt_keeps_every_old_policy_instruction_exact(policy, version):
    for completion in (None, 'original_query_fallback_v1'):
        goal = {'comparison': {'publisher_policy': policy, 'evidence_version': version}}
        if completion is not None:
            goal['comparison']['completion_policy'] = completion
        actual = publisher_evidence.review_instructions(goal)
        assert sha256(actual.encode('utf-8')).hexdigest() == PREVIOUS_PUBLISHER_PROMPTS[(policy, version)]


@pytest.mark.parametrize('policy', publisher_evidence.POLICIES)
def test_copyright_prompt_adds_only_generic_repair_instructions(policy):
    goal = {'comparison': {'publisher_policy': policy, 'evidence_version': 4,
                            'completion_policy': 'capture_repair_v2'}}
    old_goal = deepcopy(goal)
    del old_goal['comparison']['completion_policy']
    original = publisher_evidence.review_instructions(old_goal)
    updated = publisher_evidence.review_instructions(goal)
    assert updated.startswith(original) and len(updated) > len(original)
    suffix = updated[len(original):]
    assert 'Atlas' in suffix and 'All rights reserved' in suffix
    assert 'GeeksforGeeks' not in suffix and 'Sanchhaya' not in suffix
    assert 'project author' in suffix and 'copyright' in suffix
    assert 'null' in suffix
    assert 'candidate_index protocol' in suffix and 'nonempty project_role_candidates' in suffix
    assert ('name immediately after @ and before the first comma' in suffix
            and 'do not substitute another name occurring later in the line' in suffix)


@pytest.mark.parametrize('quote', [
    'Copyright 2026 Atlas',
    'Atlas, All rights reserved',
    '@Atlas, Example Education Private Limited, All rights reserved',
])
def test_copyright_prompt_does_not_fill_null_publisher_or_change_local_rules(quote):
    context, selected, review = review_fixture()
    context['goal']['comparison'].update(evidence_version=4, publisher_policy=publisher_evidence.POLICY,
                                          completion_policy='capture_repair_v2')
    source = context['sources'][0]
    source['text'] = 'Atlas format supported.\n' + quote
    source['sha256'] = sha256(source['text'].encode()).hexdigest()
    selected['sources'][0]['spans'] = [{'quote': 'Atlas format supported.'}]
    review['sources'][0]['publisher'] = None
    before = deepcopy((context, selected, review))
    publisher = {'name': 'Atlas', 'quote': quote}
    assert publisher_evidence.attribution_status(source, publisher,
        policy=publisher_evidence.POLICY) == 'supported'
    assessment = assess(context, selected, review)
    assert assessment['review']['sources'][0]['publisher'] is None
    assert assessment['publisher_support']['s0'] is None and not assessment['eligible']
    assert (context, selected, review) == before
    old_context = deepcopy(context)
    old_context['goal']['comparison']['completion_policy'] = 'original_query_fallback_v1'
    assert assess(old_context, selected, review) == assessment
