"""Offline semantic gate integration; injected Office evidence is not live acceptance."""
from copy import deepcopy
from pathlib import Path
import time
import json

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.artifact_source import prepare_artifact_source
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_agent_tools import AgentEnvironment
from agent_service.computer_goals import _save_goal
from test_comparison_loop import ctx, run, semantic_review, saved_v2_plan
from test_computer_agent_tools import ctx as tool_ctx


def change_review(ctx, transform):
    original = ctx['client'].select
    ctx['client'].select = lambda data: transform(data, semantic_review(data)) if 'selection' in data else original(data)


def comparison_record(result):
    return next(read_record(path)['data']['comparison']
                for path in Path(result['run']).glob('step-*/environment-result.json')
                if 'comparison' in read_record(path)['data'])


def test_saved_v2_completed_review_recovery_never_replays_model(ctx):
    plan = saved_v2_plan(ctx, '语义核验后暂停', ctx['goal'])
    assert plan['goal']['comparison']['evidence_version'] == 2
    assert 'evidence_version' not in ctx['goal']['comparison']
    first = run(ctx, plan, 7)
    proof = comparison_record(first)
    assert proof['version'] == 2 and proof['assessment']['independent_count'] == 2
    assert proof['assessment']['page_count'] == 4 and proof['assessment']['group_count'] == 2
    calls = deepcopy(ctx['client'].calls)
    resumed = recovery.prepare(ctx['session'], first['run'])['plan']
    assert resumed['goal'] == plan['goal']
    final = run(ctx, resumed)
    assert final['ok'] and ctx['client'].calls == calls
    assert [s['kind'] for s, _ in ctx['effects']] == ['spreadsheet', 'document']


def test_unrelated_literal_quote_is_suppressed_in_both_office_tables(ctx):
    def unrelated(data, review):
        review['sources'][0]['cells'][0] = {'status': 'irrelevant', 'reason': 'This text does not answer this dimension.'}
        return review
    change_review(ctx, unrelated)
    result = run(ctx, saved_v2_plan(ctx, '抑制无关引句', ctx['goal']))
    assert result['ok']
    quote = comparison_record(result)['response']['sources'][0]['spans'][0]['quote']
    sheet, word = [spec for spec, _ in ctx['effects']]
    cell = sheet['rows'][0][2]
    assert '待补证' in cell and '与维度不相关' in cell and quote not in cell
    assert word['table']['rows'][0][2] == cell
    assert '原文[' in sheet['rows'][1][2]


@pytest.mark.parametrize('condition', ['uncertain_pairs', 'unknown_publishers', 'all_irrelevant'])
def test_insufficient_independence_or_semantics_blocks_office(ctx, condition):
    def insufficient(data, review):
        if condition == 'uncertain_pairs':
            for pair in review['pairs']:
                if pair['relation'] == 'independent':
                    pair['relation'] = 'uncertain'
        elif condition == 'unknown_publishers':
            for source in review['sources']:
                source['publisher'] = None
        else:
            for source in review['sources']:
                source['cells'][0]['status'] = 'irrelevant'
        return review
    change_review(ctx, insufficient)
    result = run(ctx, saved_v2_plan(ctx, '证据不足禁止生成', ctx['goal']))
    expected = 'comparison_semantic_insufficient' if condition == 'all_irrelevant' else 'comparison_independence_insufficient'
    assert not result['ok'] and not ctx['effects']
    assert result['attempts'][-1]['result']['code'] == expected
    assert not recovery.inspect(ctx['session'], result['run'])['eligible']


@pytest.mark.parametrize('condition', ['missing_callback', 'empty_review', 'forged_publisher', 'second_call_failure'])
def test_missing_invalid_or_failed_second_review_never_dispatches_office(ctx, condition):
    def invalid(data, review):
        if condition == 'second_call_failure':
            raise ComputerError('model_budget_exceeded')
        if condition == 'forged_publisher':
            review['sources'][0]['publisher']['quote'] = 'Invented Publisher Alpha evidence'
            return review
        return {}
    change_review(ctx, invalid)
    extra = {}
    if condition == 'missing_callback':
        def missing(*args, **kwargs):
            kwargs['comparison_review'] = None
            return AgentEnvironment(*args, **kwargs)
        extra['environment_factory'] = missing
    result = run(ctx, saved_v2_plan(ctx, '复核失败禁止生成', ctx['goal']), **extra)
    assert not result['ok'] and not ctx['effects']
    expected = {'missing_callback': 'comparison_review_missing', 'second_call_failure': 'model_budget_exceeded'}.get(condition, 'comparison_review_invalid')
    assert result['attempts'][-1]['result']['code'] == expected
    assert not recovery.inspect(ctx['session'], result['run'])['eligible']
    assert sum('selection' in data for data in ctx['client'].calls) == (0 if condition == 'missing_callback' else 1)


def test_existing_six_session_claims_still_block_v2_without_seventh_call(ctx):
    for number in range(6):
        ctx['session'].claim_model({'previous': number})
    result = run(ctx, saved_v2_plan(ctx, '共享六次调用上限', ctx['goal']))
    assert result['code'] == 'session_model_call_limit'
    assert not ctx['client'].calls and not ctx['effects']
    assert len(list((ctx['session'].root / 'model_calls').glob('*.json'))) == 6


def test_second_review_uses_existing_six_claim_limit_and_stops_before_office(ctx):
    first = run(ctx, saved_v2_plan(ctx, '复核也共用六次上限', ctx['goal']), 6)
    assert not first['ok'] and not ctx['effects']
    claims = list((ctx['session'].root / 'model_calls').glob('*.json'))
    claimed = len([path for path in claims if not path.name.endswith('.outcome.json')])
    for number in range(claimed, 5):
        ctx['session'].claim_model({'previous': number})
    result = run(ctx, recovery.prepare(ctx['session'], first['run'])['plan'])
    assert not result['ok'] and not ctx['effects']
    assert result['attempts'][-1]['result']['code'] == 'session_model_call_limit'
    assert sum('dimensions' in call and 'selection' not in call for call in ctx['client'].calls) == 1
    assert not any('selection' in call for call in ctx['client'].calls)
    claims = list((ctx['session'].root / 'model_calls').glob('*.json'))
    assert len([path for path in claims if not path.name.endswith('.outcome.json')]) == 6
    assert not recovery.inspect(ctx['session'], result['run'])['eligible']


def test_tampered_v2_review_blocks_recovery_before_office(ctx):
    first = run(ctx, saved_v2_plan(ctx, '持久复核证据校验', ctx['goal']), 7)
    assert not ctx['effects']
    path = Path(first['run']) / 'step-07' / 'environment-result.json'
    record = read_record(path)
    review = record['data']['comparison']['review']
    review['sources'][0]['cells'][0]['status'] = 'irrelevant'
    record['data']['comparison']['review_sha256'] = digest(review)
    record['sha256'] = digest(record['data'])
    path.write_text(json.dumps(record), encoding='utf-8')
    assert not recovery.inspect(ctx['session'], first['run'])['eligible']
    assert not ctx['effects']


def test_exact_saved_v1_goal_and_office_origin_survive_recovery(ctx):
    # Saved historical goals retain their exact contract; new prepare always pins v3.
    old_goal = deepcopy(ctx['goal'])
    plan = _save_goal(ctx['session'], {'schema': 3, 'mode': 'agent',
        'instruction': '旧版原文对照恢复', 'goal': old_goal,
        'max_actions': engine.MAX_ACTIONS, 'max_seconds': engine.MAX_SECONDS,
        'prepared_at': time.time()})
    first = run(ctx, plan, 8)
    assert not first['ok'] and len(ctx['effects']) == 1
    assert 'version' not in comparison_record(first)
    sheet = first['artifacts'][0]
    origin = Path(sheet['task_root']) / 'goal-origin.json'
    origin_bytes = origin.read_bytes()
    assert read_record(origin)['data']['goal_sha256'] == digest(old_goal)
    saved_calls = deepcopy(ctx['client'].calls)
    resumed = recovery.prepare(ctx['session'], first['run'])['plan']
    assert resumed['goal'] == old_goal and 'evidence_version' not in resumed['goal']['comparison']
    final = run(ctx, resumed)
    assert final['ok'] and ctx['client'].calls == saved_calls
    assert origin.read_bytes() == origin_bytes
    assert [spec['kind'] for spec, _ in ctx['effects']] == ['spreadsheet', 'document']
    assert prepare_artifact_source(sheet['path'], sheet['sha256'], ctx['options']['task_base'])
