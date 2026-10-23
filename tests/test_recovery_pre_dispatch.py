"""Only a fully bound final selection without an action dispatch may continue."""
import json
from pathlib import Path

import pytest

from agent_service import computer_agent as engine, research_recovery as recovery
from agent_service.computer import ComputerError, read_record
from agent_service.computer_agent_tools import AgentEnvironment
from test_computer_agent_tools import ctx as tool_ctx, A
from test_research_recovery import ctx, plan, run


@pytest.fixture(autouse=True)
def clear_recovery_cache():
    recovery._VERIFIED.clear()
    yield
    recovery._VERIFIED.clear()


def before_dispatch(kind):
    def factory(goal, root, **options):
        env = AgentEnvironment(goal, root, **options)
        original = env.execute
        def execute(action, step):
            if action['kind'] == kind:
                raise ComputerError('goal_deadline')
            return original(action, step)
        env.execute = execute
        return env
    return factory


@pytest.fixture
def pending(ctx):
    outcome = run(ctx, plan(ctx), environment_factory=before_dispatch('read'))
    assert outcome['code'] == 'goal_deadline' and outcome['actions'] == 1
    root = Path(outcome['run'])
    tail = root / 'step-02'
    assert {p.name for p in tail.iterdir()} == {'choices.json', 'decision.json', 'intent.json'}
    return ctx, root, tail, outcome


@pytest.mark.parametrize('kind', ['search', 'read'])
def test_actual_engine_pending_selection_preserved_and_only_undispatched_action_runs_once(ctx, kind):
    outcome = run(ctx, plan(ctx), environment_factory=before_dispatch(kind))
    root = Path(outcome['run'])
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    value = recovery._inspect(ctx['session'], root)
    assert len(value['records']) == outcome['actions'] == (0 if kind == 'search' else 1)
    assert len(value['done']) == outcome['actions']
    assert value['remaining'][0].startswith(kind + ':')
    assert all(p.read_bytes() == raw for p, raw in before.items())
    child = recovery.prepare(ctx['session'], root)['plan']
    result = run(ctx, child, halt=1)
    assert result['actions'] == 1 and result['attempts'][0]['action']['kind'] == kind
    assert ctx['search'].calls == ['first']
    assert ctx['browser'].calls == ([] if kind == 'search' else [A])
    assert not ctx['effects']
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert read_record(root / 'outcome.json') == outcome


@pytest.mark.parametrize('case', ['missing_choices', 'missing_decision', 'extra_file', 'extra_dir',
    'environment_intent', 'environment_result', 'verification', 'not_last', 'gap', 'multiple_gaps',
    'state', 'choice_id', 'decision_method', 'decision_choice', 'extra_choice_field', 'plan'])
def test_partial_or_changed_selection_never_grants_recovery(pending, case):
    c, root, tail, _ = pending
    if case.startswith('missing_'):
        (tail / (case.removeprefix('missing_') + '.json')).unlink()
    elif case == 'extra_file':
        (tail / 'unexpected.json').write_text('{}', encoding='utf-8')
    elif case == 'extra_dir':
        (tail / 'unexpected').mkdir()
    elif case in {'environment_intent', 'environment_result', 'verification'}:
        (tail / (case.replace('_', '-') + '.json')).write_text('{}', encoding='utf-8')
    elif case == 'not_last':
        (root / 'step-03').mkdir()
    elif case == 'gap':
        tail.rename(root / 'step-03')
    elif case == 'multiple_gaps':
        tail.rename(root / 'step-04')
        (root / 'step-02').mkdir()
        (root / 'step-03').mkdir()
    elif case == 'plan':
        value = read_record(root.with_suffix('.json'))
        value['instruction'] += ' changed objective'
        root.with_suffix('.json').write_text(json.dumps(value), encoding='utf-8')
    else:
        choices = read_record(tail / 'choices.json')
        decision = read_record(tail / 'decision.json')
        chosen = read_record(tail / 'intent.json')
        if case == 'state':
            choices['state_sha256'] = decision['state_sha256'] = '0' * 64
        elif case == 'choice_id':
            choices['choices'][0]['id'] = decision['choice']['id'] = chosen['id'] = '0' * 24
        elif case == 'decision_method':
            decision['method'] = 'model_choice'
        elif case == 'decision_choice':
            decision['choice']['action']['candidate_id'] = '0' * 24
        else:
            choices['unexpected'] = True
        for name, value in [('choices', choices), ('decision', decision), ('intent', chosen)]:
            (tail / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises((ComputerError, ValueError)):
        recovery._inspect(c['session'], root)
    assert not c['browser'].calls and not c['effects']


@pytest.mark.parametrize('kind', ['search', 'read', 'supplement', 'compare'])
def test_bound_read_only_kinds_share_engine_choice_contract(tmp_path, kind):
    observation = {'captured': 'completed prefix'}
    actions = [{'kind': kind}]
    state, choices = engine.choices_for(tmp_path, observation, actions)
    step = tmp_path / 'step-01'
    step.mkdir()
    values = {'choices': {'state_sha256': state, 'choices': choices},
              'decision': {'method': 'single_legal_action', 'choice': choices[0], 'state_sha256': state},
              'intent': choices[0]}
    for name, value in values.items():
        (step / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    recovery._validate_pre_dispatch(tmp_path, step, observation, actions)
    with pytest.raises(ComputerError, match='recovery_pre_dispatch_changed'):
        recovery._validate_pre_dispatch(tmp_path, step, {'captured': 'changed prefix'}, actions)


@pytest.mark.parametrize('kind', ['spreadsheet', 'document', 'office_edit', 'browser_click', 'unknown'])
def test_non_read_only_action_rejected_even_with_consistent_choice_hashes(tmp_path, kind):
    observation, actions = {}, [{'kind': kind}]
    state, choices = engine.choices_for(tmp_path, observation, actions)
    step = tmp_path / 'step-01'
    step.mkdir()
    for name, value in {'choices': {'state_sha256': state, 'choices': choices},
            'decision': {'method': 'single_legal_action', 'choice': choices[0], 'state_sha256': state},
            'intent': choices[0]}.items():
        (step / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ComputerError, match='recovery_pre_dispatch_changed'):
        recovery._validate_pre_dispatch(tmp_path, step, observation, actions)


def test_warm_parent_cache_rechecks_pending_decision(pending):
    c, root, tail, _ = pending
    value = recovery._inspect(c['session'], root)
    assert recovery._inspect(c['session'], root) == value
    changed = read_record(tail / 'intent.json')
    changed['id'] = '0' * 24
    (tail / 'intent.json').write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ComputerError, match='recovery_pre_dispatch_changed'):
        recovery._inspect(c['session'], root)


@pytest.mark.parametrize('decision', [False, True])
def test_legacy_preselection_without_chosen_intent_keeps_old_recovery_rules(pending, decision):
    c, root, tail, prior = pending
    (tail / 'intent.json').unlink()
    if not decision:
        (tail / 'decision.json').unlink()
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    value = recovery._inspect(c['session'], root)
    assert len(value['records']) == prior['actions'] == 1
    assert value['remaining'][0].startswith('read:')
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not c['browser'].calls and not c['effects']


def test_structurally_legal_read_outside_rebuilt_frontier_is_rejected(pending):
    c, root, tail, _ = pending
    # The current observation is rebuilt from completed production receipts;
    # changing a saved choice to another structurally legal read still fails.
    changed = read_record(tail / 'choices.json')
    changed['choices'][0]['action']['candidate_id'] = '0' * 24
    (tail / 'choices.json').write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ComputerError, match='recovery_pre_dispatch_changed'):
        recovery._inspect(c['session'], root)


def test_multiple_read_only_choices_require_exact_model_selection_contract(tmp_path):
    observation = {'captured': 'same completed prefix'}
    actions = [{'kind': 'read', 'candidate_id': 'first'}, {'kind': 'read', 'candidate_id': 'second'}]
    state, choices = engine.choices_for(tmp_path, observation, actions)
    step = tmp_path / 'step-01'
    step.mkdir()
    decision = {'method': 'model_choice', 'choice': choices[1], 'state_sha256': state}
    for name, value in {'choices': {'state_sha256': state, 'choices': choices},
                        'decision': decision, 'intent': choices[1]}.items():
        (step / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
    recovery._validate_pre_dispatch(tmp_path, step, observation, actions)
    decision['method'] = 'single_legal_action'
    (step / 'decision.json').write_text(json.dumps(decision), encoding='utf-8')
    with pytest.raises(ComputerError, match='recovery_pre_dispatch_changed'):
        recovery._validate_pre_dispatch(tmp_path, step, observation, actions)
