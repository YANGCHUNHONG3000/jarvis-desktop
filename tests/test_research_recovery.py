"""Research continuation contracts through the actual loop and OOXML verifier."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, ComputerSession, read_record, digest
from agent_service.contracts import BrowserReadError
from test_computer_agent_tools import ctx as tool_ctx, A, B
from test_computer_agent import Client


@pytest.fixture
def ctx(tool_ctx):
    tool_ctx['session'] = ComputerSession.create(tool_ctx['run'].parent.parent / ('rr-' + digest(str(tool_ctx['run']))[:8]))
    tool_ctx['goal']['queries'] = ['first']
    tool_ctx['client'] = Client(lambda data: {'action_id': next(
        (v['id'] for v in data['choices'] if v['action']['kind'] == 'read'), data['choices'][0]['id'])})
    return tool_ctx


def plan(ctx):
    return engine.prepare(ctx['session'], '查资料生成Excel和Word', ctx['goal'])


def run(ctx, value, *, halt=None, **kwargs):
    root = ctx['session'].root / 'goals' / (value['sha256'] + '.run')
    options = {k: v for k, v in ctx['options'].items() if k not in {'stop', 'deadline'}}
    options.update(kwargs)
    return engine.execute(ctx['session'], value, approval=value['sha256'], client=ctx['client'],
                          stop=lambda: halt is not None and len(list(root.glob('step-*/verification.json'))) >= halt,
                          **options)


@pytest.mark.parametrize('halt', [0, 1, 2, 3])
def test_same_session_resumes_only_untouched_research_work(ctx, halt):
    original = plan(ctx)
    prior = run(ctx, original, halt=halt)
    root = Path(prior['run'])
    snapshot = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    before = (list(ctx['search'].calls), list(ctx['browser'].calls), len(ctx['effects']))
    row = recovery.inspect(ctx['session'], root)
    assert row['eligible']
    resumed = recovery.prepare(ctx['session'], root)['plan']
    assert resumed['goal'] == original['goal'] and resumed['recovery']['kind'] == 'research'
    assert '原采集时间' in recovery.describe(resumed)
    result = run(ctx, resumed)
    assert result['ok'] and result['actions'] == 4 - halt and len(result['artifacts']) == 2
    assert ctx['search'].calls == ['first'] and ctx['browser'].calls == [A]
    assert [spec['kind'] for spec, _ in ctx['effects']] == ['spreadsheet', 'document']
    assert all(Path(path).read_bytes() == value for path, value in snapshot.items())
    assert read_record(root / 'outcome.json') == prior
    assert not recovery.inspect(ctx['session'], root)['eligible']
    if halt >= 2:
        observation = read_record(Path(result['run']) / 'observation-01.json')
        assert observation['sources'][0]['freshness'] == 'historical_capture'
        assert observation['sources'][0]['captured_at'] == '2026-10-17T12:00:00+00:00'
    assert before[0] == (['first'] if halt >= 1 else [])


@pytest.mark.parametrize('kind', ['search', 'read'])
def test_known_failed_source_keeps_alternative_without_retry(ctx, kind):
    ctx['goal']['queries'] = ['first', 'second']
    if kind == 'search':
        ctx['search'].results['first'] = ComputerError('search_timeout')
        halt = 1
    else:
        ctx['browser'].text[A] = BrowserReadError('network_timeout')
        halt = 2
    prior = run(ctx, plan(ctx), halt=halt)
    result = run(ctx, recovery.prepare(ctx['session'], prior['run'])['plan'])
    assert result['ok'] and len(result['failures']) == 1
    assert ctx['search'].calls == ['first', 'second']
    assert ctx['browser'].calls == ([B] if kind == 'search' else [A, B])


@pytest.mark.parametrize('target', ['page', 'artifact', 'receipt', 'origin', 'outcome', 'intent', 'result', 'verification'])
def test_changed_ancestor_blocks_before_any_more_effects(ctx, target):
    prior = run(ctx, plan(ctx), halt=3)
    continuation = recovery.prepare(ctx['session'], prior['run'])['plan']
    root = Path(prior['run'])
    task = Path(prior['artifacts'][0]['task_root'])
    artifact = Path(prior['artifacts'][0]['path'])
    paths = {'page': root / 'step-02' / 'environment-result.json',
             'artifact': artifact, 'receipt': artifact.parent / 'receipt.json',
             'origin': task / 'goal-origin.json', 'outcome': root / 'outcome.json',
             'intent': root / 'step-03' / 'environment-intent.json',
             'result': root / 'step-03' / 'environment-result.json',
             'verification': root / 'step-03' / 'verification.json'}
    paths[target].write_bytes(paths[target].read_bytes() + b' ')
    with pytest.raises(ValueError):
        run(ctx, continuation)
    assert len(ctx['effects']) == 1


def test_wrong_origin_refused_before_preview_even_with_valid_wrapper(ctx):
    from agent_service.computer import digest
    prior = run(ctx, plan(ctx), halt=3)
    path = Path(prior['artifacts'][0]['task_root']) / 'goal-origin.json'
    value = read_record(path)
    value['data']['run'] = 'unrelated'
    value['sha256'] = digest(value['data'])
    path.write_text(json.dumps(value), encoding='utf-8')
    assert not recovery.inspect(ctx['session'], prior['run'])['eligible']
    assert len(ctx['effects']) == 1


@pytest.mark.parametrize('kind', ['search', 'read', 'spreadsheet'])
def test_dispatched_without_engine_ack_never_recovers(ctx, kind):
    halt = {'search': 1, 'read': 2, 'spreadsheet': 3}[kind]
    prior = run(ctx, plan(ctx), halt=halt)
    (Path(prior['run']) / f'step-{halt:02}' / 'verification.json').unlink()
    assert not recovery.inspect(ctx['session'], prior['run'])['eligible']
    with pytest.raises(ValueError):
        recovery.prepare(ctx['session'], prior['run'])


@pytest.mark.parametrize('state', ['unknown', 'failed'])
def test_failed_native_office_is_not_retried(ctx, state):
    def bad(*args, **kwargs):
        return {'ok': False, 'state': state, 'code': 'offline_native_failure'}
    prior = run(ctx, plan(ctx), office_execute=bad)
    assert not recovery.inspect(ctx['session'], prior['run'])['eligible']
    assert not ctx['effects']


def test_competing_previews_and_four_hop_limit(ctx):
    prior = run(ctx, plan(ctx), halt=0)
    for depth in range(1, 5):
        first = recovery.prepare(ctx['session'], prior['run'])['plan']
        second = recovery.prepare(ctx['session'], prior['run'])['plan']
        assert first['recovery']['depth'] == depth
        prior = run(ctx, first, halt=0)
        with pytest.raises(ComputerError, match='recovery_already_claimed'):
            run(ctx, second)
    assert recovery.inspect(ctx['session'], prior['run'])['code'] == 'recovery_lineage_depth'
    assert not ctx['search'].calls and not ctx['effects']


def test_ancestor_change_during_document_prevents_stale_handoff(ctx):
    prior = run(ctx, plan(ctx), halt=3)
    continuation = recovery.prepare(ctx['session'], prior['run'])['plan']
    original = ctx['options']['office_execute']
    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        path = Path(prior['run']) / 'outcome.json'
        path.write_bytes(path.read_bytes() + b' ')
        return value
    result = run(ctx, continuation, office_execute=changed)
    assert not result['ok'] and result['artifacts'] == [] and len(ctx['effects']) == 2


def test_unchanged_model_cap_and_cross_session_owner(ctx):
    ctx['goal']['queries'] = ['first', 'second']
    prior = run(ctx, plan(ctx), halt=0)
    other = ComputerSession.create(ctx['run'].parent / 'another-session')
    assert recovery.inspect(other, prior['run'])['code'] == 'recovery_run_owner_invalid'
    for i in range(6):
        ctx['session'].claim_model({'test': i})
    resumed = recovery.prepare(ctx['session'], prior['run'])['plan']
    result = run(ctx, resumed)
    assert result['code'] == 'session_model_call_limit' and not ctx['client'].calls
    assert not ctx['search'].calls and not ctx['effects']


def test_only_verified_office_artifact_sibling_lock_is_excluded(ctx, monkeypatch):
    from agent_service import interaction_recovery
    prior = run(ctx, plan(ctx), halt=3)
    artifact = Path(prior['artifacts'][0]['path'])
    lock = artifact.with_name('~$' + artifact.name)
    lock.write_bytes(b'native Office transient lock')
    original = interaction_recovery._hash
    blocked = [lock]
    def locked(path):
        if Path(path) in blocked:
            raise PermissionError('native transient lock')
        return original(path)
    monkeypatch.setattr(interaction_recovery, '_hash', locked)
    assert recovery.inspect(ctx['session'], prior['run'])['eligible']
    other = lock.with_name('~$unrelated.xlsx')
    other.write_bytes(b'not the verified artifact lock')
    blocked.append(other)
    assert not recovery.inspect(ctx['session'], prior['run'])['eligible']
