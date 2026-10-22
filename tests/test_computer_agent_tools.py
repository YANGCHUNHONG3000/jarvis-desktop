"""Offline observed tools with genuine OOXML and receipt/source revalidation."""
from copy import deepcopy
import json
from pathlib import Path
import time

import pytest

from agent_service.computer import ComputerError, read_record
from agent_service.computer_agent_tools import AgentEnvironment, _selection
from agent_service.contracts import BrowserReadError, Observation
from agent_service.artifact_source import prepare_artifact_source
from test_artifact_source import fake as native_fake


A, B = 'https://example.com/', 'https://example.org/'
STAMP = '2026-10-17T12:00:00+00:00'


class Search:
    def __init__(self):
        self.calls, self.results = [], {'first': [A], 'second': [B]}
    def search(self, query, max_sources, should_stop, timeout):
        assert not should_stop() and 0 < timeout <= 25
        self.calls.append(query)
        value = self.results[query]
        if isinstance(value, Exception):
            raise value
        return {'urls': value, 'searched_at': STAMP}


class Browser:
    def __init__(self):
        self.calls, self.text = [], {A: 'prefix alpha actual OMEGA evidence', B: 'alpha second omega'}
    def read(self, url, should_stop):
        assert not should_stop()
        self.calls.append(url)
        value = self.text[url]
        if isinstance(value, Exception):
            raise value
        return Observation(url, url, 'Actual title', value, STAMP)


@pytest.fixture
def ctx(tmp_path):
    goal = {'queries': ['first', 'second'], 'title': '核验资料', 'min_sources': 1,
            'required_terms': ['alpha', 'omega'], 'outputs': ['spreadsheet', 'document']}
    run = tmp_path / 'run'
    run.mkdir()
    search, browser, effects = Search(), Browser(), []
    stopped = [False]
    def execute(spec, root, stop):
        effects.append((deepcopy(spec), root))
        return native_fake(spec, root, stop)
    options = dict(stop=lambda: stopped[0], deadline=time.monotonic() + 360,
                   search=search, browser=browser, office_execute=execute, task_base=tmp_path / 'tasks')
    return dict(goal=goal, run=run, search=search, browser=browser, effects=effects,
                options=options, stopped=stopped, count=0)


def environment(ctx):
    return AgentEnvironment(ctx['goal'], ctx['run'], **ctx['options'])


def act(ctx, env, kind, **params):
    observation = env.observe()
    choices = env.actions(observation)
    action = next(a for a in choices if a['kind'] == kind and all(a.get(k) == v for k, v in params.items()))
    ctx['count'] += 1
    step = ctx['run'] / f"step-{ctx['count']:02}"
    step.mkdir()
    return env.execute(action, step)


def ready(ctx, env):
    assert act(ctx, env, 'search', query_index=0)['ok']
    assert act(ctx, env, 'read')['ok']


def test_observe_is_stable_short_and_rebuilt_from_actual_evidence(ctx):
    env = environment(ctx)
    assert env.observe() == env.observe()
    ready(ctx, env)
    obs = env.observe()
    assert obs['source_reads'] == 1 and obs['checks']['sources_ready']
    assert obs['sources'][0]['terms_match'] is True
    assert len(obs['sources'][0]['excerpt_preview']) <= 180
    assert 'observation' not in obs and 'text' not in obs
    assert environment(ctx).observe() == obs


def test_real_excel_word_receipts_and_existing_next_task_handoff(ctx):
    env = environment(ctx)
    ready(ctx, env)
    assert env.actions(env.observe()) == [{'kind': 'spreadsheet'}]
    assert act(ctx, env, 'spreadsheet')['ok']
    assert env.actions(env.observe()) == [{'kind': 'document'}]
    assert act(ctx, env, 'document')['ok']
    obs = env.observe()
    assert obs['checks']['goal_met'] and not env.actions(obs)
    assert len(obs['artifacts']) == 2
    sheet, document = [spec for spec, _ in ctx['effects']]
    assert document['table'] == {'headers': sheet['headers'], 'rows': sheet['rows']}
    for item in obs['artifacts']:
        assert prepare_artifact_source(item['path'], item['sha256'], ctx['options']['task_base'])
        assert read_record(Path(item['task_root']) / 'result.json')['state'] == 'succeeded'
    word = next(item for item in obs['artifacts'] if item['kind'] == 'document')
    assert read_record(Path(word['task_root']) / 'sources' / 'provenance.json')['artifact'] == obs['artifacts'][0]['path']


def test_word_only_uses_actual_sources_without_creating_excel(ctx):
    ctx['goal']['outputs'] = ['document']
    env = environment(ctx)
    ready(ctx, env)
    assert act(ctx, env, 'document')['ok']
    assert env.observe()['checks']['goal_met']
    assert [spec['kind'] for spec, _ in ctx['effects']] == ['document']


@pytest.mark.parametrize('failure', [ComputerError('search_timeout'), ComputerError('search_unavailable')])
def test_known_search_failure_leaves_alternative_without_office(ctx, failure):
    ctx['search'].results['first'] = failure
    env = environment(ctx)
    assert act(ctx, env, 'search', query_index=0)['state'] == 'failed'
    assert env.actions(env.observe()) == [{'kind': 'search', 'query_index': 1}]
    assert act(ctx, env, 'search', query_index=1)['ok']
    assert act(ctx, env, 'read')['ok']
    assert env.observe()['source_reads'] == 1 and not ctx['effects']


def test_browser_failure_then_real_alternative_is_disclosed_as_empty_body_row(ctx):
    ctx['browser'].text[A] = BrowserReadError('browser_failed')
    env = environment(ctx)
    act(ctx, env, 'search', query_index=0)
    assert not act(ctx, env, 'read')['ok']
    act(ctx, env, 'search', query_index=1)
    act(ctx, env, 'read')
    act(ctx, env, 'spreadsheet')
    rows = ctx['effects'][0][0]['rows']
    failed = next(row for row in rows if row[1] == A)
    assert failed[3] == '' and '未用于内容' in failed[4]
    assert env.observe()['source_reads'] == 1 and len(env.observe()['failures']) == 1


@pytest.mark.parametrize('body', ['alpha missing last term', 'alpha' + 'x' * 1200 + 'omega'])
def test_terms_missing_or_span_too_long_never_make_sources_ready(ctx, body):
    ctx['browser'].text[A] = body
    env = environment(ctx)
    act(ctx, env, 'search', query_index=0)
    result = act(ctx, env, 'read')
    assert result['code'] == 'quality_rejected' and not result['ok']
    assert env.observe()['source_reads'] == 0
    assert not any(a['kind'] in {'spreadsheet', 'document'} for a in env.actions(env.observe()))
    assert not ctx['effects']


def test_exact_term_excerpt_is_original_case_insensitive_and_within_limit():
    body = 'x' * 2000 + 'AlPhA' + ' y ' * 100 + 'OMEGA' + 'tail' * 500
    excerpt = _selection(body, 'ignored', ['alpha', 'omega'])
    assert excerpt['start'] == 2000 and excerpt['end'] == 3200
    assert excerpt['text'] == body[2000:3200]
    assert _selection('prefix first exact text', 'first exact', [])['start'] == 7


def test_distinct_url_deduplication_cannot_fake_minimum_source_count(ctx):
    ctx['goal']['min_sources'] = 2
    ctx['search'].results['second'] = [A]
    env = environment(ctx)
    ready(ctx, env)
    act(ctx, env, 'search', query_index=1)
    obs = env.observe()
    assert obs['source_reads'] == 1 and len(obs['candidates']) == 1
    assert not obs['checks']['sources_ready'] and not env.actions(obs)
    assert ctx['browser'].calls == [A] and not ctx['effects']


@pytest.mark.parametrize('target', ['source', 'file', 'receipt'])
def test_changed_source_or_artifact_evidence_stops_before_next_write(ctx, target):
    env = environment(ctx)
    ready(ctx, env)
    if target == 'source':
        path = ctx['run'] / 'step-02' / 'environment-result.json'
        value = read_record(path)
        value['data']['observation']['text'] = 'forged content'
        path.write_text(json.dumps(value), encoding='utf-8')
    else:
        act(ctx, env, 'spreadsheet')
        root = ctx['effects'][0][1]
        if target == 'file':
            with (root / 'spreadsheet.xlsx').open('ab') as f:
                f.write(b'changed')
        else:
            path = root / 'receipt.json'
            value = read_record(path)
            value['observed']['rows'][0][0] = 'forged title'
            path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ComputerError, match='agent_evidence_changed|agent_artifact_changed'):
        env.observe()
    assert len(ctx['effects']) == (0 if target == 'source' else 1)


def test_unknown_office_cannot_replay_or_read_new_sources(ctx):
    def lost_reply(spec, root, stop):
        ctx['effects'].append((deepcopy(spec), root))
        native_fake(spec, root, stop)
        raise RuntimeError('saved but reply lost')
    ctx['options']['office_execute'] = lost_reply
    env = environment(ctx)
    ready(ctx, env)
    outcome = act(ctx, env, 'spreadsheet')
    assert outcome['state'] == 'unknown'
    obs = env.observe()
    assert obs['office_started'] and obs['office_blocked'] and not env.actions(obs)
    assert not environment(ctx).actions(environment(ctx).observe())
    assert len(ctx['effects']) == 1


def test_illegal_action_and_scope_reject_before_effect(ctx):
    env = environment(ctx)
    step = ctx['run'] / 'step-01'
    step.mkdir()
    for action in [{'kind': 'read', 'candidate_id': 'invented'}, {'kind': 'spreadsheet'},
                   {'kind': 'search', 'query_index': True}, {'kind': 'search', 'query_index': 0, 'url': A}]:
        with pytest.raises(ComputerError, match='agent_action_not_available'):
            env.execute(action, step)
    with pytest.raises(ComputerError, match='agent_step_scope_invalid'):
        env.execute({'kind': 'search', 'query_index': 0}, ctx['run'])
    assert not ctx['search'].calls and not ctx['effects']


def test_cancel_or_deadline_prevents_search_and_office(ctx):
    env = environment(ctx)
    ctx['stopped'][0] = True
    with pytest.raises(ComputerError, match='goal_cancelled'):
        env.observe()
    ctx['stopped'][0] = False
    ctx['options']['deadline'] = time.monotonic() - 1
    with pytest.raises(ComputerError, match='goal_deadline'):
        environment(ctx).observe()
    assert not ctx['search'].calls and not ctx['effects']


def test_changed_goal_and_stale_observation_are_rejected(ctx):
    env = environment(ctx)
    stale = env.observe()
    act(ctx, env, 'search', query_index=0)
    with pytest.raises(ComputerError, match='agent_observation_changed'):
        env.actions(stale)
    ctx['goal']['title'] = 'different'
    with pytest.raises(ComputerError, match='agent_goal_changed'):
        environment(ctx)


def test_source_minimum_requires_two_actual_distinct_eligible_pages(ctx):
    ctx['goal']['min_sources'] = 2
    env = environment(ctx)
    ready(ctx, env)
    assert not env.observe()['checks']['sources_ready']
    act(ctx, env, 'search', query_index=1)
    act(ctx, env, 'read')
    assert env.observe()['source_reads'] == 2
    assert env.actions(env.observe()) == [{'kind': 'spreadsheet'}]


def test_bad_search_receipt_is_terminal_and_never_yields_read_candidates(ctx):
    ctx['search'].results['first'] = ['https://127.0.0.1/']
    env = environment(ctx)
    with pytest.raises(ComputerError, match='search_receipt_invalid'):
        act(ctx, env, 'search', query_index=0)
    with pytest.raises(ComputerError, match='agent_interrupted_action'):
        env.observe()
    assert not ctx['browser'].calls and not ctx['effects']


def test_cancel_during_failed_read_preserves_failure_before_halting(ctx):
    env = environment(ctx)
    act(ctx, env, 'search', query_index=0)
    def cancelled_read(url, should_stop):
        ctx['stopped'][0] = True
        raise BrowserReadError('network_timeout')
    ctx['browser'].read = cancelled_read
    with pytest.raises(ComputerError, match='goal_cancelled'):
        act(ctx, env, 'read')
    result = read_record(ctx['run'] / 'step-02' / 'environment-result.json')['data']
    assert result['result']['code'] == 'network_timeout'
    ctx['stopped'][0] = False
    assert len(env.observe()['failures']) == 1 and not ctx['effects']


def test_cancel_after_native_receipt_preserves_success_without_word_replay(ctx):
    def saved_then_cancelled(spec, root, stop):
        ctx['effects'].append((deepcopy(spec), root))
        result = native_fake(spec, root, stop)
        ctx['stopped'][0] = True
        return result
    ctx['options']['office_execute'] = saved_then_cancelled
    env = environment(ctx)
    ready(ctx, env)
    result = act(ctx, env, 'spreadsheet')
    assert result == {'ok': True, 'state': 'verified', 'code': 'office_verified'}
    assert ctx['stopped'][0]
    with pytest.raises(ComputerError, match='goal_cancelled'):
        env.observe()
    obs = env.observe_final()
    assert obs['outputs_done'] == ['spreadsheet'] and len(obs['artifacts']) == 1
    assert len(ctx['effects']) == 1
    with pytest.raises(ComputerError, match='goal_cancelled'):
        env.actions(obs)
    assert len(ctx['effects']) == 1


def test_successful_observation_contract_error_is_not_safe_retry(ctx):
    env = environment(ctx)
    act(ctx, env, 'search', query_index=0)
    def wrong_url(url, should_stop):
        return Observation(url, B, 'Actual', 'alpha omega', STAMP)
    ctx['browser'].read = wrong_url
    with pytest.raises(ComputerError, match='browser_observation_invalid'):
        act(ctx, env, 'read')
    assert not ctx['effects']


def test_deleted_source_result_is_detected_as_interrupted_not_replayed(ctx):
    env = environment(ctx)
    ready(ctx, env)
    (ctx['run'] / 'step-02' / 'environment-result.json').unlink()
    with pytest.raises(ComputerError, match='agent_interrupted_action'):
        env.observe()
    assert ctx['browser'].calls == [A] and not ctx['effects']


@pytest.mark.parametrize('halt', ['cancel', 'deadline'])
def test_final_observation_rechecks_verified_office_despite_halt(ctx, halt):
    env = environment(ctx)
    ready(ctx, env)
    act(ctx, env, 'spreadsheet')
    before = env.observe()
    if halt == 'cancel':
        ctx['stopped'][0] = True
    else:
        env.deadline = time.monotonic() - 1
    with pytest.raises(ComputerError, match='goal_cancelled|goal_deadline'):
        env.observe()
    assert env.observe_final() == before
    assert len(ctx['effects']) == 1


@pytest.mark.parametrize('target', ['source', 'file', 'receipt'])
def test_final_observation_never_skips_tamper_checks_after_stop(ctx, target):
    env = environment(ctx)
    ready(ctx, env)
    act(ctx, env, 'spreadsheet')
    ctx['stopped'][0] = True
    if target == 'source':
        path = ctx['run'] / 'step-02' / 'environment-result.json'
        data = read_record(path)
        data['data']['observation']['text'] = 'FORGED'
        path.write_text(json.dumps(data), encoding='utf-8')
    elif target == 'file':
        with (ctx['effects'][0][1] / 'spreadsheet.xlsx').open('ab') as f:
            f.write(b'tampered')
    else:
        path = ctx['effects'][0][1] / 'receipt.json'
        data = read_record(path)
        data['observed']['rows'][0][0] = 'FORGED'
        path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ComputerError, match='agent_evidence_changed|agent_artifact_changed'):
        env.observe_final()
    assert len(ctx['effects']) == 1


def test_final_observation_refuses_unresolved_effect_intent(ctx):
    env = environment(ctx)
    ready(ctx, env)
    act(ctx, env, 'spreadsheet')
    # Simulate losing the environment acknowledgement after a later action.
    act(ctx, env, 'document')
    (ctx['run'] / 'step-04' / 'environment-result.json').unlink()
    ctx['stopped'][0] = True
    with pytest.raises(ComputerError, match='agent_interrupted_action'):
        env.observe_final()
    assert len(ctx['effects']) == 2
