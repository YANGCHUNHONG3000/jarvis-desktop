import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import daily_task_routing as routing
from agent_service.computer import ComputerError, digest, read_record, write_new


class Client:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, system, user, **kwargs):
        self.calls.append(json.loads(user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'computer'; root.mkdir()
    (root / 'goals').mkdir()
    refs = [{'artifact': str(tmp_path / f'{i}.{ext}'), 'sha256': str(i) * 64,
             'task_root': str(tmp_path / 'private-task')} for i, ext in ((1, 'xlsx'), (2, 'docx'))]
    verified, created, claims = [], [], []
    monkeypatch.setattr(routing, '_verify', lambda r, b: verified.append((r, b)))

    def claim(context):
        path = root / f'claim-{len(claims)}.json'
        write_new(path, context); claims.append(context)
        return path

    session = SimpleNamespace(root=root, claim_model=claim)

    def child(session, req, decision):
        plan = {'schema': 1, 'instruction': req['instruction'], 'task_base': req['task_base']}
        if decision['route'] == 'compare_tables':
            plan.update(mode='office_bundle', source_refs=req['source_refs'])
        else:
            plan.update(mode='capability', source_ref=routing._selected(decision, req['source_refs']))
        plan['sha256'] = digest(plan)
        write_new(root / 'goals' / (plan['sha256'] + '.json'), plan)
        created.append(plan)
        return plan

    monkeypatch.setattr(routing, '_child', child)
    return SimpleNamespace(session=session, refs=refs, base=tmp_path, created=created, verified=verified, claims=claims)


def call(s, answer, refs=None, **kwargs):
    client = Client(answer)
    result = routing.propose(s.session, '逐格核对输入并生成 Word 和 Excel', s.refs if refs is None else refs,
                             task_base=s.base, client=client, **kwargs)
    return result, client


@pytest.mark.parametrize(('route', 'ids', 'mode'), [
    ('compare_tables', ['S1', 'S2'], 'office_bundle'),
    ('single_source', ['S2'], 'capability'),
    ('no_source', [], 'capability')])
def test_routes_only_prepare_bound_child_without_execute(setup, route, ids, mode):
    s = setup
    result, client = call(s, {'route': route, 'source_ids': ids, 'reason': '按完整任务选择'})
    assert result['state'] == 'ready' and result['plan']['mode'] == mode
    assert len(s.created) == len(s.claims) == len(client.calls) == 1
    assert len(s.verified) >= 2
    payload = client.calls[0]
    assert set(payload) == {'instruction', 'sources'}
    assert all(set(r) == {'id', 'filename', 'format', 'sha256'} for r in payload['sources'])
    assert str(s.base) not in json.dumps(payload)
    if route == 'single_source':
        assert result['plan']['source_ref'] == s.refs[1]


def test_cached_plan_rechecks_sources_without_second_model_call(setup):
    answer = {'route': 'compare_tables', 'source_ids': ['S1', 'S2'], 'reason': '整批逐格核对'}
    first, _ = call(setup, answer)
    second, client = call(setup, RuntimeError('must not be called'))
    assert second == first and not client.calls and len(setup.created) == 1
    assert len(setup.claims) == 1 and len(setup.verified) >= 3


def test_ambiguity_is_persisted_without_child_or_repeated_call(setup):
    result, _ = call(setup, {'route': 'clarify', 'source_ids': [], 'reason': '请明确哪份输入'})
    assert result['state'] == 'needs_input' and result['plan'] is None
    assert not setup.created
    again, client = call(setup, RuntimeError('no retry'))
    assert again == result and not client.calls


@pytest.mark.parametrize('answer', [
    {'route': 'single_source', 'source_ids': ['S3'], 'reason': 'bad'},
    {'route': 'single_source', 'source_ids': ['S1', 'S2'], 'reason': 'bad'},
    {'route': 'compare_tables', 'source_ids': ['S2', 'S1'], 'reason': 'bad'},
    {'route': 'compare_tables', 'source_ids': ['S1'], 'reason': 'bad'},
    {'route': 'no_source', 'source_ids': ['S1'], 'reason': 'bad'},
    {'route': 'shell', 'source_ids': [], 'reason': 'bad'},
    {'route': 'clarify', 'source_ids': [], 'reason': ''},
    {'route': 'clarify', 'source_ids': [], 'reason': 'ok', 'command': 'bad'},
])
def test_invalid_decision_blocks_without_fallback(setup, answer):
    result, _ = call(setup, answer)
    assert result['state'] == 'blocked' and not setup.created
    again, client = call(setup, RuntimeError('no retry'))
    assert again['state'] == 'blocked' and not client.calls


def test_unknown_inflight_request_is_not_recharged(setup):
    req = {'schema': routing.POLICY, 'instruction': '逐格核对输入并生成 Word 和 Excel',
           'source_refs': setup.refs, 'task_base': str(setup.base)}
    run = setup.session.root / 'task_routes' / digest(req); run.mkdir(parents=True)
    write_new(run / 'request.json', req)
    result, client = call(setup, RuntimeError('no retry'))
    assert result['state'] == 'blocked' and '未知' in result['reason'] and not client.calls


def test_provider_failure_not_persisted_or_retried(setup):
    result, _ = call(setup, RuntimeError('SECRET-API-KEY'))
    assert 'SECRET' not in (Path(result['routing']) / 'outcome.json').read_text()
    again, client = call(setup, RuntimeError('no retry'))
    assert again['state'] == 'blocked' and not client.calls


def test_stop_before_model_does_not_claim(setup):
    with pytest.raises(ComputerError, match='daily_route_cancelled'):
        call(setup, {}, stop=lambda: True)
    assert not setup.claims


def test_source_changed_after_model_blocks_child(setup, monkeypatch):
    count = []
    def verify(refs, base):
        count.append(True)
        if len(count) > 1:
            raise ComputerError('source_changed')
    monkeypatch.setattr(routing, '_verify', verify)
    result, _ = call(setup, {'route': 'single_source', 'source_ids': ['S1'], 'reason': '选第一份'})
    assert result['state'] == 'blocked' and not setup.created


def test_cached_child_tamper_rejected(setup):
    result, _ = call(setup, {'route': 'single_source', 'source_ids': ['S1'], 'reason': '选第一份'})
    path = setup.session.root / 'goals' / (result['plan']['sha256'] + '.json')
    path.write_text('{}', encoding='utf8')
    with pytest.raises(ComputerError, match='daily_route_plan_changed'):
        call(setup, RuntimeError('no retry'))


def test_empty_sources_no_source_plan(setup):
    result, _ = call(setup, {'route': 'no_source', 'source_ids': [], 'reason': '新建文档'}, refs=[])
    assert result['state'] == 'ready' and result['plan']['source_ref'] is None


def test_actual_ref_contract_rejects_oversize_and_nonlist():
    for refs in (None, (), [{}] * 5):
        with pytest.raises(ComputerError, match='daily_route_sources_invalid'):
            routing._verify(refs, Path('.'))


@pytest.mark.parametrize('contents', [None, '{'])
def test_concurrent_or_incomplete_request_does_not_recharge(setup, contents):
    req = {'schema': routing.POLICY, 'instruction': '逐格核对输入并生成 Word 和 Excel',
           'source_refs': setup.refs, 'task_base': str(setup.base)}
    run = setup.session.root / 'task_routes' / digest(req); run.mkdir(parents=True)
    if contents is not None:
        (run / 'request.json').write_text(contents, encoding='utf8')
    result, client = call(setup, RuntimeError('must not call'))
    assert result['state'] == 'blocked' and '记录' in result['reason'] and not client.calls


def test_budget_rejection_has_clear_reason_and_no_retry(setup):
    from agent_service.budget import BudgetError
    result, _ = call(setup, BudgetError('model budget exceeded'))
    assert result['state'] == 'blocked' and '预算' in result['reason'] and not setup.created
    again, client = call(setup, RuntimeError('must not call'))
    assert again == result and not client.calls
