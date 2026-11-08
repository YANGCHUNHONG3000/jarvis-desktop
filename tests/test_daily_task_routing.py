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
    (tmp_path / 'private-task').mkdir()
    write_new(tmp_path / 'private-task' / 'result.json', {})
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


def call(s, answer, refs=None, instruction='逐格核对输入并生成 Word 和 Excel', **kwargs):
    client = Client(answer)
    result = routing.propose(s.session, instruction, s.refs if refs is None else refs,
                             task_base=s.base, client=client, **kwargs)
    return result, client


def edit_call(s, answer, **kwargs):
    return call(s, answer, instruction='仅将指定副本的表头改为待确认', **kwargs)


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
    req = {'schema': routing.POLICY, 'routing_rules_sha256': digest(routing.SYSTEM),
           'instruction': '逐格核对输入并生成 Word 和 Excel',
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
    req = {'schema': routing.POLICY, 'routing_rules_sha256': digest(routing.SYSTEM),
           'instruction': '逐格核对输入并生成 Word 和 Excel',
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


@pytest.mark.parametrize('ids', [['S1'], ['S2'], ['S1', 'S2']])
def test_office_edit_only_returns_bound_scope_inputs_without_child(setup, monkeypatch, ids):
    from agent_service import office_intent, office_workflow
    monkeypatch.setattr(routing, '_child', lambda *a: pytest.fail('edit must not create child'))
    monkeypatch.setattr(office_intent, 'propose', lambda *a, **k: pytest.fail('must select scope first'))
    monkeypatch.setattr(office_workflow, 'run_office_workflow', lambda *a, **k: pytest.fail('no execution'))
    result, client = edit_call(setup, {'route': 'office_edit', 'source_ids': ids, 'reason': '只修改指定副本'})
    assert result['state'] == 'needs_scope' and result['plan'] is None
    assert result['edit'] == {'instruction': '仅将指定副本的表头改为待确认',
                              'source_refs': [setup.refs[int(item[1:]) - 1] for item in ids]}
    assert not setup.created and not list((setup.session.root / 'goals').iterdir())
    assert len(client.calls) == 1
    payload = client.calls[0]
    assert set(payload) == {'instruction', 'sources'}
    assert all(set(source) == {'id', 'filename', 'format', 'sha256'} for source in payload['sources'])
    assert str(setup.base) not in json.dumps(payload)


PRESERVE_TWO_FILES = ('请在所选Word和Excel副本中，把表头“后续事项”替换为“待办事项”；'
                      '原样保留其他内容、原格式和所有公式，不计算、不修改公式。')


def test_two_file_literal_edit_with_format_and_formula_preservation_only_requests_scope(setup, monkeypatch):
    from agent_service import office_intent, office_workflow
    systems = []
    class CaptureClient(Client):
        def complete(self, system, user, **kwargs):
            systems.append(system)
            return super().complete(system, user, **kwargs)
    monkeypatch.setattr(routing, '_child', lambda *a: pytest.fail('range selection must precede child planning'))
    monkeypatch.setattr(office_intent, 'propose', lambda *a, **k: pytest.fail('must not plan actual edits'))
    monkeypatch.setattr(office_workflow, 'run_office_workflow', lambda *a, **k: pytest.fail('must not execute'))
    client = CaptureClient({'route': 'office_edit', 'source_ids': ['S1', 'S2'],
                            'reason': '两份副本字面替换，保持原格式与公式'})
    result = routing.propose(setup.session, PRESERVE_TWO_FILES, setup.refs,
        task_base=setup.base, client=client)
    assert result['state'] == 'needs_scope' and result['plan'] is None
    assert result['edit'] == {'instruction': PRESERVE_TWO_FILES, 'source_refs': setup.refs}
    assert len(client.calls) == len(setup.claims) == 1 and not setup.created
    assert not list((setup.session.root / 'goals').iterdir())
    prompt = ' '.join(systems[0].split())
    office_clause = prompt.split('office_edit:', 1)[1].split('single_source:', 1)[0]
    assert '1..4' in office_clause and 'both a Word and an Excel' in office_clause
    assert 'Preserving existing formatting and formulas' in office_clause
    assert 'not a request to style, calculate, or change formulas' in office_clause
    assert 'Those changes remain forbidden' in office_clause
    assert 'If the MAIL task requires several inputs' in prompt
    assert client.calls[0]['instruction'] == PRESERVE_TWO_FILES
    assert all(set(item) == {'id', 'filename', 'format', 'sha256'}
               for item in client.calls[0]['sources'])


def test_preservation_rules_change_request_digest_but_keep_old_clarification_readable(setup, monkeypatch):
    current_rules = routing.SYSTEM
    monkeypatch.setattr(routing, 'SYSTEM', 'Prior bounded Office route rules without preservation clarification.')
    old, _ = call(setup, {'route': 'clarify', 'source_ids': [], 'reason': '旧规则误判联合编辑'},
                  instruction=PRESERVE_TWO_FILES)
    old_root = Path(old['routing'])
    old_request = read_record(old_root / 'request.json')
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old_root.iterdir()}
    monkeypatch.setattr(routing, 'SYSTEM', current_rules)
    current, client = call(setup, {'route': 'office_edit', 'source_ids': ['S1', 'S2'],
                                  'reason': '字面修改并保留格式公式'}, instruction=PRESERVE_TWO_FILES)
    request = read_record(Path(current['routing']) / 'request.json')
    assert request['schema'] == old_request['schema'] == routing.POLICY == 3
    assert request['routing_rules_sha256'] == digest(current_rules)
    assert request['routing_rules_sha256'] != old_request['routing_rules_sha256']
    assert Path(current['routing']).name == digest(request) and current['routing'] != old['routing']
    assert current['state'] == 'needs_scope' and current['plan'] is None and len(client.calls) == 1
    assert routing._cached(setup.session, old_root, old_request) == old
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old_root.iterdir()} == before
    cached, retry = call(setup, RuntimeError('new request must use saved outcome'), instruction=PRESERVE_TWO_FILES)
    assert cached == current and not retry.calls and not setup.created


def test_new_rules_preserve_existing_office_edit_scope_without_replanning(setup, monkeypatch):
    current_rules = routing.SYSTEM
    monkeypatch.setattr(routing, 'SYSTEM', 'Earlier bounded Office edit routing rules.')
    old, _ = call(setup, {'route': 'office_edit', 'source_ids': ['S1', 'S2'], 'reason': '已选择编辑范围入口'},
                  instruction=PRESERVE_TWO_FILES)
    old_root = Path(old['routing'])
    request = read_record(old_root / 'request.json')
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old_root.iterdir()}
    monkeypatch.setattr(routing, 'SYSTEM', current_rules)
    assert request['routing_rules_sha256'] != digest(current_rules)
    assert routing._cached(setup.session, old_root, request) == old
    again, client = call(setup, RuntimeError('existing scope must not trigger a paid retry'),
                         instruction=PRESERVE_TWO_FILES)
    assert again == old and not client.calls and not setup.created and len(setup.claims) == 1
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old_root.iterdir()} == before


def test_office_edit_allows_four_ordered_sources_and_noncontiguous_subset(setup):
    refs = setup.refs + [{**setup.refs[0], 'artifact': str(setup.base / f'{i}.docx')}
                         for i in (3, 4)]
    result, _ = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1', 'S3', 'S4'],
                            'reason': '编辑三份指定副本'}, refs=refs)
    assert result['edit']['source_refs'] == [refs[0], refs[2], refs[3]]
    # Four selected files are also valid under the same existing edit limit.
    value = {'route': 'office_edit', 'source_ids': ['S1', 'S2', 'S3', 'S4'], 'reason': '全部修改'}
    assert routing._decision(value, refs) == value


@pytest.mark.parametrize('ids', [[], ['S3'], ['S1', 'S1'], ['S2', 'S1'], ['S0'],
                              ['S01'], [1], ['S1', 'S2', 'S3', 'S4', 'S5']])
def test_invalid_edit_selection_blocks_without_child_or_fallback(setup, ids):
    result, client = edit_call(setup, {'route': 'office_edit', 'source_ids': ids, 'reason': 'invalid selection'})
    assert result['state'] == 'blocked' and 'edit' not in result
    assert not setup.created and len(client.calls) == 1
    again, retry = edit_call(setup, RuntimeError('no retry'))
    assert again == result and not retry.calls


def test_edit_cache_is_read_only_and_reconstructs_refs_from_request(setup):
    result, _ = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S2'], 'reason': '只改Word'})
    route = Path(result['routing'])
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in route.iterdir()}
    saved = read_record(route / 'outcome.json')
    assert saved['state'] == 'needs_scope' and 'edit' not in saved
    result['edit']['source_refs'][0]['artifact'] = 'untrusted-bare-path.docx'
    again, retry = edit_call(setup, RuntimeError('no retry'))
    assert again['edit']['source_refs'] == [setup.refs[1]] and not retry.calls
    assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in route.iterdir()} == before


@pytest.mark.parametrize('record', ['request', 'decision', 'outcome'])
def test_edit_cache_rejects_tampered_binding_or_stored_edit_paths(setup, record):
    result, _ = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '只改首份'})
    path = Path(result['routing']) / (record + '.json')
    value = read_record(path)
    if record == 'request':
        value['source_refs'][0]['artifact'] = 'untrusted.docx'
    elif record == 'decision':
        value['request_sha256'] = 'a' * 64
    else:
        value['edit'] = {'instruction': 'run', 'source_refs': [{'artifact': 'untrusted.docx'}]}
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ComputerError, match='daily_route_(request|decision|outcome)_changed'):
        edit_call(setup, RuntimeError('no retry'))


def test_edit_cache_rechecks_sources_before_returning_inputs(setup, monkeypatch):
    edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '修改'})
    monkeypatch.setattr(routing, '_verify', lambda *_: (_ for _ in ()).throw(ComputerError('source_changed')))
    with pytest.raises(ComputerError, match='source_changed'):
        edit_call(setup, RuntimeError('no retry'))


def test_edit_cache_cannot_redirect_to_another_valid_source_id(setup):
    result, _ = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '只改首份'})
    path = Path(result['routing']) / 'decision.json'
    value = read_record(path)
    value['decision']['source_ids'] = ['S2']
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ComputerError, match='daily_route_decision_changed'):
        edit_call(setup, RuntimeError('no retry'))


def test_edit_without_sources_is_blocked(setup):
    result, _ = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '无来源'}, refs=[])
    assert result['state'] == 'blocked' and not setup.created and 'edit' not in result


def test_mixed_edit_and_report_request_can_clarify_without_partial_execution(setup):
    result, client = call(setup, {'route': 'clarify', 'source_ids': [], 'reason': '请拆分编辑与新报告要求'},
                          instruction='修改两个文件的表头，再新增一份汇总报告')
    assert client.calls[0]['instruction'] == '修改两个文件的表头，再新增一份汇总报告'
    assert set(result) == {'state', 'reason', 'plan', 'routing'}
    assert result['state'] == 'needs_input' and result['plan'] is None and not setup.created


@pytest.mark.parametrize('length,expected', [(1000, 'needs_scope'), (1001, 'blocked')])
def test_edit_instruction_length_is_enforced_without_generation_fallback(setup, length, expected):
    client = Client({'route': 'office_edit', 'source_ids': ['S1'], 'reason': '文字修改'})
    instruction = '改' * length
    result = routing.propose(setup.session, instruction, setup.refs, task_base=setup.base, client=client)
    assert result['state'] == expected and result['plan'] is None and not setup.created
    retry = Client(RuntimeError('no retry'))
    assert routing.propose(setup.session, instruction, setup.refs,
                           task_base=setup.base, client=retry) == result
    assert not retry.calls
    if expected == 'blocked':
        assert '1000' in result['reason'] and 'edit' not in result


def test_edit_cancellation_after_model_is_terminal_and_not_retried(setup):
    stopped = [False]
    class CancellingClient(Client):
        def complete(self, *args, **kwargs):
            result = super().complete(*args, **kwargs)
            stopped[0] = True
            return result
    client = CancellingClient({'route': 'office_edit', 'source_ids': ['S1'], 'reason': '修改'})
    result = routing.propose(setup.session, '修改表头', setup.refs, task_base=setup.base,
                             client=client, stop=lambda: stopped[0])
    assert result['state'] == 'blocked' and '停止' in result['reason'] and not setup.created
    retry = Client(RuntimeError('no retry'))
    again = routing.propose(setup.session, '修改表头', setup.refs, task_base=setup.base, client=retry)
    assert again == result and not retry.calls


def test_edit_cancellation_during_source_recheck_does_not_return_scope(setup, monkeypatch):
    stopped, verifications = [False], []
    def verify(*_args):
        verifications.append(True)
        if len(verifications) == 2:
            stopped[0] = True
    monkeypatch.setattr(routing, '_verify', verify)
    result, client = edit_call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '修改'},
                               stop=lambda: stopped[0])
    assert len(client.calls) == 1 and result['state'] == 'blocked'
    assert '停止' in result['reason'] and 'edit' not in result and not setup.created


def test_policy3_uses_distinct_cache_namespace_from_policy2(setup):
    req = {'schema': 2, 'instruction': '逐格核对输入并生成 Word 和 Excel',
           'source_refs': setup.refs, 'task_base': str(setup.base)}
    old = setup.session.root / 'task_routes' / digest(req)
    old.mkdir(parents=True)
    write_new(old / 'request.json', req)
    result, client = call(setup, {'route': 'office_edit', 'source_ids': ['S1'], 'reason': '新路由'})
    assert routing.POLICY == 3 and Path(result['routing']) != old and len(client.calls) == 1
    assert read_record(old / 'request.json') == req and not (old / 'outcome.json').exists()


def _old_rules_record(s, *, rules=True, state=None, instruction=None):
    req = {'schema': routing.POLICY,
           'instruction': instruction or '逐格核对输入并生成 Word 和 Excel',
           'source_refs': s.refs, 'task_base': str(s.base)}
    if rules:
        req['routing_rules_sha256'] = digest('old routing rules')
    run = s.session.root / 'task_routes' / digest(req)
    run.mkdir(parents=True)
    write_new(run / 'request.json', req)
    if state is not None:
        outcome = {'schema': 1, 'request_sha256': digest(req), 'state': state,
                   'code': 'planned' if state == 'needs_input' else 'planning_failed', 'plan': None}
        write_new(run / 'outcome.json', outcome)
        if state == 'needs_input':
            write_new(run / 'decision.json', {'request_sha256': digest(req), 'decision': {
                'route': 'clarify', 'source_ids': [], 'reason': '旧规则无法判定'}})
    return run, req


@pytest.mark.parametrize('rules', [False, True])
@pytest.mark.parametrize('state', [None, 'blocked'])
def test_rules_hash_cannot_bypass_old_unresolved_or_failed_call(setup, rules, state):
    old, _ = _old_rules_record(setup, rules=rules, state=state)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old.iterdir()}
    result, client = call(setup, RuntimeError('must not charge after rules update'))
    assert result['state'] == 'blocked' and '未知' in result['reason']
    assert result['routing'] == str(old) and not client.calls and not setup.claims
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old.iterdir()} == before


@pytest.mark.parametrize('rules', [False, True])
def test_explicit_new_rules_can_replan_old_verified_terminal_clarification(setup, rules):
    old, _ = _old_rules_record(setup, rules=rules, state='needs_input')
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old.iterdir()}
    result, client = call(setup, {'route': 'single_source', 'source_ids': ['S2'], 'reason': '单输入可产两成果'})
    assert result['state'] == 'ready' and Path(result['routing']) != old and len(client.calls) == 1
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in old.iterdir()} == before
    again, retry = call(setup, RuntimeError('new rules result must be cached'))
    assert again == result and not retry.calls


def test_rules_update_reuses_existing_valid_ready_plan_without_paid_replanning(setup, monkeypatch):
    current = routing.SYSTEM
    monkeypatch.setattr(routing, 'SYSTEM', current + '\nPrior rules revision.')
    first, _ = call(setup, {'route': 'single_source', 'source_ids': ['S2'], 'reason': '完整生成计划'})
    monkeypatch.setattr(routing, 'SYSTEM', current)
    again, client = call(setup, RuntimeError('must use existing plan'))
    assert again == first and not client.calls and len(setup.created) == 1


@pytest.mark.parametrize('record', ['request.json', 'outcome.json', 'decision.json'])
def test_corrupt_old_terminal_record_never_allows_new_rules_charge(setup, record):
    old, _ = _old_rules_record(setup, state='needs_input')
    (old / record).write_text('{', encoding='utf-8')
    result, client = call(setup, RuntimeError('no unknown retry'))
    assert result['state'] == 'blocked' and not client.calls and not setup.claims


def test_old_unknown_takes_priority_over_another_terminal_clarification(setup):
    _old_rules_record(setup, state='needs_input')
    _old_rules_record(setup, rules=False)
    result, client = call(setup, RuntimeError('unknown call must remain blocked'))
    assert result['state'] == 'blocked' and not client.calls and not setup.created


def test_different_instruction_does_not_inherit_known_unrelated_old_rules_call(setup):
    old, _ = _old_rules_record(setup, instruction='另一项任务')
    result, client = call(setup, {'route': 'single_source', 'source_ids': ['S1'], 'reason': '新任务'})
    assert result['state'] == 'ready' and len(client.calls) == 1
    assert not (old / 'outcome.json').exists()


def test_rules_admission_owner_blocks_parallel_paid_call(setup):
    from agent_service.office_workflow import _owner, WorkflowError
    routes = setup.session.root / 'task_routes'
    routes.mkdir()
    with _owner(routes):
        with pytest.raises(WorkflowError, match='workflow_busy'):
            call(setup, RuntimeError('locked admission must not charge'))
    assert not setup.claims
