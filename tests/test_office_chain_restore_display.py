"""Independent restore-display permit guards; no API or native operation."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import office_task_chain_panel as module
from agent_service.computer import ComputerSession
from test_office_chain_edit_panel import edit_harness
from test_office_task_chain_panel import main_functions


def restore_display(tmp_path, monkeypatch):
    chain, context, log, state, goal, office, plan, _result, calls = edit_harness(tmp_path, monkeypatch)
    plan['prepared_at'] = 1.0  # Displaying the old plan must not renew its TTL.
    saved_office = deepcopy(office)
    state.update(status='edit_preview', edit_plan=deepcopy(plan), edit_payload=saved_office,
                 run=str(goal.session.root / 'goals' / (plan['sha256'] + '.run')))
    chain.active = state
    saved_plan = goal.session.root / 'goals' / (plan['sha256'] + '.json')
    saved_plan.parent.mkdir(parents=True, exist_ok=True)
    saved_plan.write_text(json.dumps(plan), encoding='utf-8')
    goal.instruction_draft.adopt('')
    office['draft'] = ''

    def apply_office(verified):
        office.clear()
        office.update(deepcopy(verified['payload']))
        goal.instruction_draft.adopt(office['draft'])
        return True

    chain.apply_office = apply_office
    goal.tab = 'goal-tab'
    routed = {'main': None, 'goal': None}
    env = main_functions(['office_chain_prepare_edit'], {
        'Path': Path, 'goal_panel': goal,
        'task_draft': SimpleNamespace(snapshot=lambda: deepcopy(context['workspace']['revision'])),
        'routed_instruction': routed, 'tabs': SimpleNamespace(select=lambda tab: None)})
    chain.prepare_edit = env['office_chain_prepare_edit']
    verified = {'payload': deepcopy(state['payload']),
                'office_verified': {'payload': saved_office, 'refs': ['worker verified']}}
    return chain, context, log, state, goal, office, plan, calls, verified, saved_plan, routed


def adopt(chain, state, verified):
    return chain._adopt(state, verified, auto=False, before={})


def deny_refresh(monkeypatch, calls):
    def denied(*args):
        calls.append(('refresh-denied', deepcopy(args[4:])))
        raise ValueError('normal full binding rejected changed evidence')
    monkeypatch.setattr(module.core, 'bind_edit_refresh', denied)


def test_restore_shows_exact_worker_plan_once_without_rebinding_or_renewing_ttl(tmp_path, monkeypatch):
    chain, context, log, state, goal, _office, plan, calls, verified, saved_plan, routed = restore_display(tmp_path, monkeypatch)
    assert chain._restore_display is None
    original = saved_plan.read_bytes()
    deny_refresh(monkeypatch, calls)
    assert adopt(chain, state, verified)
    assert not calls and chain._restore_display is None
    assert goal.plan == plan and goal.plan['prepared_at'] == 1.0
    assert saved_plan.read_bytes() == original and not Path(state['run']).exists()
    assert goal.instruction_ticket(plan) == goal.instruction_draft.snapshot()
    assert goal.execute_button.options['state'] == 'normal' and not goal.running
    assert not goal.messages.qsize() and len(log.applied) == 1
    assert routed == {'main': context['workspace']['revision'], 'goal': goal.instruction_draft.snapshot()}


@pytest.mark.parametrize('change', ['stop', 'goal_stop', 'close', 'source', 'source_revert',
                                  'main_draft', 'main_revert', 'goal_draft', 'goal_revert',
                                  'office_scope', 'office_records', 'mode_revert', 'wrong_ticket',
                                  'wrong_plan', 'wrong_session', 'active_state', 'generation'])
def test_restore_permit_cannot_cross_changed_context_or_exact_binding(tmp_path, monkeypatch, change):
    chain, context, _log, state, goal, office, plan, calls, verified, _path, _routed = restore_display(tmp_path, monkeypatch)
    prepare = chain.prepare_edit
    deny_refresh(monkeypatch, calls)

    def changed(saved):
        assert chain._restore_display is not None
        if change == 'stop':
            chain.cancel()
        elif change == 'goal_stop':
            goal.stopped.set()
        elif change == 'close':
            chain.close()
        elif change == 'source':
            context['workspace']['payload']['source'] = None
        elif change == 'source_revert':
            context['workspace']['binding']['source']['generation'] += 2
        elif change == 'main_draft':
            context['workspace']['payload']['draft'] = 'new main task'
        elif change == 'main_revert':
            context['workspace']['revision']['revision'] += 2
        elif change in {'goal_draft', 'goal_revert'}:
            goal.instruction_draft.adopt('new task')
            if change == 'goal_revert':
                goal.instruction_draft.adopt(plan['instruction'])
        elif change == 'office_scope':
            office['scope'][0]['locations'] = [{'paragraph_index': 9}]
        elif change == 'office_records':
            office['records'][0]['sha256'] = 'b' * 64
        elif change == 'mode_revert':
            context['goal']['mode_generation'] += 2
        elif change == 'active_state':
            chain.active['edit_revision'] = 99
        elif change == 'generation':
            chain.generation += 1
        if change == 'wrong_ticket':
            ticket = goal.instruction_draft.snapshot()
            ticket['revision'] += 1
            return goal.show_plan(deepcopy(plan), instruction_ticket=ticket)
        if change == 'wrong_plan':
            altered = deepcopy(plan)
            altered['goal']['edits'][0]['id'] = 'altered'
            return goal.show_plan(altered, instruction_ticket=goal.instruction_draft.snapshot())
        if change == 'wrong_session':
            goal.session = ComputerSession.create(tmp_path / 'other-computers')
            return goal.show_plan(deepcopy(plan), instruction_ticket=goal.instruction_draft.snapshot())
        return prepare(saved)

    chain.prepare_edit = changed
    assert not adopt(chain, state, verified)
    assert chain._restore_display is None and goal.plan is None
    assert goal.execute_button.options['state'] == 'disabled'
    assert not Path(state['run']).exists()


@pytest.mark.parametrize('change', ['plan_changed', 'plan_missing', 'plan_malformed', 'run_directory', 'run_file',
                                  'plan_link_guard', 'run_link_guard'])
def test_changed_plan_bytes_or_started_run_cannot_use_restore_permit(tmp_path, monkeypatch, change):
    chain, _context, _log, state, goal, _office, plan, calls, verified, path, _routed = restore_display(tmp_path, monkeypatch)
    prepare = chain.prepare_edit
    deny_refresh(monkeypatch, calls)

    def changed(saved):
        if change == 'plan_changed':
            path.write_text(json.dumps({**plan, 'prepared_at': 2.0}), encoding='utf-8')
        elif change == 'plan_missing':
            # Rename only this test-created fixture; preserve the old bytes.
            path.rename(path.with_suffix('.preserved'))
        elif change == 'plan_malformed':
            path.write_text('{broken json', encoding='utf-8')
        elif change == 'run_directory':
            Path(state['run']).mkdir()
        elif change == 'run_file':
            Path(state['run']).write_text('dispatch marker', encoding='utf-8')
        else:
            original = module.workspace_state._safe_components
            rejected = path if change == 'plan_link_guard' else Path(state['run'])
            def reject(target):
                if Path(target) == rejected:
                    raise ValueError('linked test fixture')
                return original(target)
            monkeypatch.setattr(module.workspace_state, '_safe_components', reject)
        return prepare(saved)

    chain.prepare_edit = changed
    assert not adopt(chain, state, verified)
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    assert chain._restore_display is None


def test_second_show_callback_in_same_adopt_cannot_reuse_restore_permit(tmp_path, monkeypatch):
    chain, _context, _log, state, goal, _office, plan, calls, verified, _path, _routed = restore_display(tmp_path, monkeypatch)
    prepare = chain.prepare_edit
    outcomes = []
    deny_refresh(monkeypatch, calls)
    def twice(saved):
        outcomes.append(prepare(saved))
        assert chain._restore_display is None
        outcomes.append(goal.show_plan(deepcopy(plan), instruction_ticket=goal.instruction_draft.snapshot()))
        return outcomes[-1]
    chain.prepare_edit = twice
    assert not adopt(chain, state, verified)
    assert outcomes == [True, False] and [call[0] for call in calls] == ['refresh-denied']
    assert chain._restore_display is None and goal.plan is None


@pytest.mark.parametrize('failure', [False, 'return_false', ValueError, RuntimeError])
def test_apply_or_prepare_failure_always_clears_local_restore_permit(tmp_path, monkeypatch, failure):
    chain, _context, _log, state, _goal, _office, _plan, _calls, verified, _path, _routed = restore_display(tmp_path, monkeypatch)
    entered = []
    def prepare(saved):
        entered.append(deepcopy(chain._restore_display))
        if failure == 'return_false':
            return False
        raise failure('preparation failed')
    chain.prepare_edit = prepare
    if failure is False:
        chain.apply_office = lambda verified: False
        assert not adopt(chain, state, verified)
        assert not entered
    elif failure is RuntimeError:
        with pytest.raises(RuntimeError):
            adopt(chain, state, verified)
        assert len(entered) == 1 and entered[0] is not None
    else:
        assert not adopt(chain, state, verified)
        assert len(entered) == 1 and entered[0] is not None
    assert chain._restore_display is None


@pytest.mark.parametrize('supplied', [{}, {'office_result': {'kind': 'edits'}}])
def test_restore_display_requires_absent_planning_result_not_merely_falsey(tmp_path, monkeypatch, supplied):
    chain, _context, _log, state, goal, _office, plan, calls, verified, _path, _routed = restore_display(tmp_path, monkeypatch)
    chain.prepare_edit = lambda saved: goal.show_plan(deepcopy(plan),
        instruction_ticket=goal.instruction_draft.snapshot(), planning_result=supplied)
    assert not adopt(chain, state, verified)
    assert not calls and goal.plan is None and chain._restore_display is None


def test_restore_display_rejects_run_that_is_not_exact_session_plan_run(tmp_path, monkeypatch):
    chain, _context, _log, state, goal, _office, _plan, calls, verified, _path, _routed = restore_display(tmp_path, monkeypatch)
    state['run'] = str(goal.session.root / 'goals' / ('f' * 64 + '.run'))
    assert not adopt(chain, state, verified)
    assert not calls and goal.plan is None and chain._restore_display is None


def test_dispatch_created_while_saved_plan_is_read_blocks_restore_display(tmp_path, monkeypatch):
    chain, _context, _log, state, goal, _office, _plan, calls, verified, path, _routed = restore_display(tmp_path, monkeypatch)
    original_read = module.workspace_state._read
    observed = []
    def read_then_dispatch(target, limit):
        result = original_read(target, limit)
        if Path(target) == path:
            Path(state['run']).mkdir()
            observed.append('dispatched during plan read')
        return result
    monkeypatch.setattr(module.workspace_state, '_read', read_then_dispatch)
    assert not adopt(chain, state, verified)
    assert observed == ['dispatched during plan read']
    assert not calls and goal.plan is None and chain._restore_display is None
    assert Path(state['run']).is_dir()


@pytest.mark.parametrize('planning_result', ['fresh', 'empty', 'refresh'])
def test_nonrestore_planning_paths_still_use_original_binding(tmp_path, monkeypatch, planning_result):
    chain, _context, _log, _state, goal, _office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    assert chain._restore_display is None
    if planning_result != 'fresh':
        assert goal.show_plan(deepcopy(plan), instruction_ticket=goal.instruction_draft.snapshot(), planning_result=result)
        calls.clear()
    supplied = result if planning_result == 'fresh' else {} if planning_result == 'empty' else None
    assert goal.show_plan(deepcopy(plan), instruction_ticket=goal.instruction_draft.snapshot(), planning_result=supplied)
    assert [call[0] for call in calls] == (['initial'] if planning_result == 'fresh' else ['refresh'])
    assert chain._restore_display is None
