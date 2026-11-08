"""Deferred chain binding through the real GoalPanel display/poll contract."""
from copy import deepcopy
import json
from pathlib import Path
from threading import Thread as NativeThread, get_ident
from types import SimpleNamespace

import pytest

from agent_service import office_task_chain_panel as chain_ui
from agent_service.computer import ComputerSession
from test_office_chain_edit_panel import edit_harness
from test_office_task_chain_panel import Thread, finish


def binding_harness(tmp_path, monkeypatch):
    from agent_service.office_chain_plan_binding import OfficeChainPlanBinding
    from agent_service import office_workspace

    chain, context, log, state, goal, office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    goal.office_planning_context = chain.context
    completed = []
    goal.on_office_planning_result = lambda *args: completed.append(deepcopy(args))
    chain.on_busy = lambda value: (log.busy.append(value), goal.set_busy(value))
    monkeypatch.setattr(office_workspace, 'verify',
                        lambda payload, _base: {'payload': deepcopy(payload), 'refs': []})

    def bind(*args, initial):
        calls.append(('initial' if initial else 'refresh', deepcopy(args[4:])))
        bound = deepcopy(state)
        bound.update(status='edit_preview', edit_plan=deepcopy(args[4]), edit_payload=deepcopy(office),
                     run=str(args[3].root / 'goals' / (args[4]['sha256'] + '.run')))
        saved = Path(bound['run']).with_suffix('.json')
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(args[4]), encoding='utf-8')
        return bound

    monkeypatch.setattr(chain_ui.core, 'bind_edit_preview', lambda *args: bind(*args, initial=True))
    monkeypatch.setattr(chain_ui.core, 'bind_edit_refresh', lambda *args: bind(*args, initial=False))
    original_show = goal.show_plan
    bridge = OfficeChainPlanBinding(goal, chain)
    return (chain, context, log, state, goal, office, plan, result, calls,
            completed, bridge, original_show)


def request(h):
    chain, _context, _log, _state, goal, _office, plan, result, *_rest = h
    return goal.show_plan(plan, instruction_ticket=goal.draft_ticket(), planning_result=result)


def test_initial_binding_returns_false_then_displays_exact_saved_diff_once(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, log, _state, goal, _office, plan, result, calls, completed, *_ = h
    previous = deepcopy(chain.active)
    assert request(h) is False
    assert chain.pending is not None and chain.active == previous
    assert not calls and not completed and goal.plan is None
    assert goal.execute_button.options['state'] == 'disabled'
    finish(chain)
    assert goal.plan == plan and chain.active['status'] == 'edit_preview'
    assert [item[0] for item in calls] == ['initial']
    assert completed == [(result, goal.draft_ticket(), result['_office_guard'])]
    assert goal.instruction_ticket(plan) == goal.draft_ticket()
    assert chain._restore_display is None and log.busy == [True, False]
    assert not log.applied and not log.prepared
    chain.poll()
    assert len(calls) == len(completed) == 1


def test_answered_draft_defers_for_backend_proof_then_uses_new_recovery_callback(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, state, goal, office, plan, result, calls, completed, *_ = h
    original = state['payload']['draft']
    composed = original + '；用户补充：明确修改已有表头'
    goal.instruction_draft.adopt(composed)
    office['draft'] = composed
    plan['instruction'] = composed
    result['plan'] = deepcopy(plan)
    result['_office_guard'] = deepcopy(goal.office_planning_context())
    result['_office_continuation'] = {'state':'resolved', 'original_instruction':original,
                                    'instruction':composed, 'answers':['明确修改已有表头']}
    results = []
    goal.intent_recovery = SimpleNamespace(on_result=lambda value: results.append(deepcopy(value)))
    assert request(h) is False
    assert chain.pending is not None and not calls
    finish(chain)
    assert goal.plan == plan and chain.active['edit_payload']['draft'] == composed
    assert len(results) == 1 and not completed
    assert state['payload']['draft'] == original


@pytest.mark.parametrize('field', ['original_instruction','instruction','answers'])
def test_unbound_answer_marker_never_enters_backend(tmp_path, monkeypatch, field):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, state, goal, office, plan, result, calls, *_ = h
    composed = state['payload']['draft'] + '；用户补充：明确'
    goal.instruction_draft.adopt(composed)
    office['draft'] = composed
    plan['instruction'] = composed
    result['plan'] = deepcopy(plan)
    result['_office_continuation'] = {'state':'resolved', 'original_instruction':state['payload']['draft'],
                                    'instruction':composed, 'answers':['明确']}
    result['_office_continuation'][field] = [] if field == 'answers' else '其他内容'
    result['_office_guard'] = deepcopy(goal.office_planning_context())
    assert request(h) is False
    assert chain.pending is None and not calls


def test_local_refresh_validates_guard_with_old_plan_then_uses_postclear_guard(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, _result, calls, completed, *_ = h
    request(h)
    finish(chain)
    fresh = {**deepcopy(plan), 'sha256': 'f' * 64}
    old_guard = deepcopy(goal.office_planning_context())
    result = {'plan': fresh, 'reason': 'local refresh', '_office_guard': old_guard}
    assert old_guard['goal']['plan'] == plan
    assert goal.show_plan(fresh, instruction_ticket=goal.draft_ticket(), planning_result=result) is False
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    finish(chain)
    assert goal.plan == fresh and [c[0] for c in calls] == ['initial', 'refresh']
    assert completed[-1] == (result, goal.draft_ticket(), old_guard)
    assert len(completed) == 2 and chain._restore_display is None


def test_real_goal_poll_does_not_emit_result_or_replace_wait_status_while_deferred(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, result, calls, completed, *_ = h
    goal.messages.put(('propose', deepcopy(result), None, goal.draft_ticket()))
    goal.poll()
    assert chain.pending is not None and not calls and not completed
    assert goal.plan is None and goal.status.get() != result['reason']
    assert '无法核验本次编辑预览' not in goal.status.get()
    finish(chain)
    assert goal.plan == plan and len(completed) == 1
    assert goal.status.get() == result['reason']


@pytest.mark.parametrize('change', ['main_draft', 'main_revert', 'source_revert', 'goal_draft',
                                  'goal_revert', 'office_scope', 'office_records', 'mode_revert',
                                  'route_scope', 'new_plan'])
def test_late_binding_cannot_replace_new_input_or_a_reverted_revision(tmp_path, monkeypatch, change):
    h = binding_harness(tmp_path, monkeypatch)
    chain, context, log, _state, goal, office, _plan, _result, _calls, completed, *_ = h
    request(h)
    previous = deepcopy(chain.active)
    if change == 'main_draft':
        context['workspace']['payload']['draft'] = 'new main task'
    elif change == 'main_revert':
        context['workspace']['revision']['revision'] += 2
    elif change == 'source_revert':
        context['workspace']['binding']['source']['generation'] += 2
    elif change in {'goal_draft', 'goal_revert'}:
        old = goal.draft_ticket()['text']
        goal.instruction_draft.adopt('new goal')
        if change == 'goal_revert':
            goal.instruction_draft.adopt(old)
    elif change == 'office_scope':
        office['scope'][0]['locations'] = [{'paragraph_index': 9}]
    elif change == 'office_records':
        office['records'][0]['sha256'] = 'b' * 64
    elif change == 'mode_revert':
        context['goal']['mode_generation'] += 2
    elif change == 'route_scope':
        context['scope'] = '当前成果批次'
    else:
        goal.plan = {'new': 'plan'}
    current = deepcopy(chain.context())
    finish(chain)
    assert chain.context() == current and chain.active == previous
    assert not completed and not log.applied and not log.prepared
    assert chain._restore_display is None and chain.pending is None


@pytest.mark.parametrize('stop', ['chain_cancel', 'goal_stop', 'chain_close'])
def test_stop_waits_for_worker_without_publishing_a_preview(tmp_path, monkeypatch, stop):
    h = binding_harness(tmp_path, monkeypatch)
    chain, context, log, _state, goal, _office, _plan, _result, _calls, completed, *_ = h
    request(h)
    if stop == 'chain_cancel':
        chain.cancel()
    elif stop == 'chain_close':
        chain.close()
    else:
        # The real stop method also invalidates the preview and increments these epochs.
        goal.capability_source_generation = context['goal']['source_generation']
        goal.daily_task_route_generation = context['goal']['mode_generation']
        goal.development_panel = goal.human_pending = None
        goal.human_continue_button = goal.execute_button
        goal.stop()
    assert log.busy == [True]
    finish(chain)
    assert log.busy == [True, False] and not completed and goal.plan is None
    assert chain._restore_display is None


def test_duplicate_click_cannot_enqueue_or_enable_a_second_preview(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, _plan, _result, calls, completed, *_ = h
    assert request(h) is False
    assert request(h) is False
    assert len(Thread.all) == 1 and goal.plan is None
    finish(chain)
    assert len(calls) == len(completed) == 1


@pytest.mark.parametrize('failure', ['thread', 'bind', 'verify'])
def test_background_failure_keeps_original_active_and_never_opens_execution(tmp_path, monkeypatch, failure):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, log, _state, goal, _office, _plan, _result, _calls, completed, *_ = h
    previous = deepcopy(chain.active)
    def fail(*_args, **_kwargs):
        raise ValueError('private local evidence')
    if failure == 'thread':
        monkeypatch.setattr(Thread, 'start', fail)
    elif failure == 'bind':
        monkeypatch.setattr(chain_ui.core, 'bind_edit_preview', fail)
    else:
        monkeypatch.setattr(chain_ui.workspace_state, 'verify', fail)
    assert request(h) is False
    if failure != 'thread':
        finish(chain)
    assert chain.active == previous and chain.pending is None and not completed
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    assert log.busy == [True, False]
    assert all('private local evidence' not in text for text in log.statuses)


@pytest.mark.parametrize('kind', ['unrelated', 'none_plan', 'restore_ticket'])
def test_nonintercepted_calls_delegate_original_display_unchanged(tmp_path, monkeypatch, kind):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, _result, _calls, _completed, bridge, _original = h
    if kind == 'unrelated':
        chain.active = None
    elif kind == 'none_plan':
        plan = None
    else:
        # A bad restore ticket is rejected by the original gate, not scheduled again.
        chain._restore_display = {'invalid': True}
    expected = True if kind != 'restore_ticket' else False
    assert goal.show_plan(plan, instruction_ticket=goal.draft_ticket()) is expected
    assert chain.pending is None and not Thread.all
    assert goal.show_plan == bridge.show_plan


def test_cancel_after_binding_write_keeps_saved_record_but_never_displays_it(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, _result, calls, completed, *_ = h
    previous = deepcopy(chain.active)
    request(h)
    Thread.all[-1].run()
    saved = goal.session.root / 'goals' / (plan['sha256'] + '.json')
    assert saved.is_file() and calls
    original_bytes = saved.read_bytes()
    chain.cancel()
    chain.poll()
    assert saved.read_bytes() == original_bytes and chain.active == previous
    assert goal.plan is None and not completed


def test_dispatch_appearing_after_worker_verification_blocks_display(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, _result, _calls, completed, *_ = h
    request(h)
    Thread.all[-1].run()
    run = goal.session.root / 'goals' / (plan['sha256'] + '.run')
    run.mkdir()
    chain.poll()
    assert run.is_dir() and goal.plan is None and not completed
    assert goal.execute_button.options['state'] == 'disabled'
    assert chain._restore_display is None


def test_display_exception_clears_one_use_ticket_and_keeps_execution_disabled(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, _plan, _result, _calls, completed, *_ = h
    def fail(_text):
        raise ValueError('render failure')
    # invalidate also renders, so install the failure only after request starts.
    request(h)
    goal.show_text = fail
    finish(chain)
    assert chain._restore_display is None and not completed
    assert goal.execute_button.options['state'] == 'disabled'


@pytest.mark.parametrize('change', ['session', 'active'])
def test_late_result_requires_original_session_and_exact_active_binding(tmp_path, monkeypatch, change):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, _plan, _result, _calls, completed, *_ = h
    request(h)
    if change == 'session':
        goal.session = ComputerSession.create(tmp_path / 'other-computers')
    else:
        chain.active = {**deepcopy(chain.active), 'id': 'b' * 32}
    current = deepcopy(chain.active)
    finish(chain)
    assert chain.active == current and goal.plan is None and not completed
    assert chain._restore_display is None


def test_stale_original_office_guard_is_rejected_before_worker_starts(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, office, _plan, _result, calls, completed, *_ = h
    office['scope'][0]['locations'] = [{'paragraph_index': 9}]
    assert request(h) is False
    assert not Thread.all and chain.pending is None and not calls and not completed
    assert goal.execute_button.options['state'] == 'disabled'


def test_background_arguments_are_copies_of_the_original_planning_proof(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, result, calls, completed, *_ = h
    expected_plan, expected_result = deepcopy(plan), deepcopy(result)
    request(h)
    plan['goal']['edits'][0]['id'] = 'mutated-caller-plan'
    result['office_result']['record'] = 'mutated-caller-proof'
    finish(chain)
    assert calls[0][1][0] == expected_plan
    assert calls[0][1][1] == {k: expected_result[k] for k in ('plan', 'reason', 'office_result')}
    assert goal.plan == expected_plan and completed[0][0] == expected_result


@pytest.mark.parametrize('failure', ['context', 'invalidate', 'postclear_guard'])
def test_preparation_exception_revokes_old_approval_without_escaping_poll(tmp_path, monkeypatch, failure):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, _plan, result, calls, completed, *_ = h
    goal.plan = {'old': 'preview'}
    goal.plan_instruction_binding = {'old': 'binding'}
    goal.execute_button.configure(state='normal')
    result['_office_guard'] = deepcopy(goal.office_planning_context())
    def fail(*_args, **_kwargs):
        raise RuntimeError('private provider or widget error')
    if failure == 'context':
        chain.context = fail
    elif failure == 'invalidate':
        goal.invalidate = fail
    else:
        original_guard = goal.office_planning_context
        def guard():
            if goal.plan is None:
                fail()
            return original_guard()
        goal.office_planning_context = guard
    assert request(h) is False
    assert goal.plan is None and goal.plan_instruction_binding is None
    assert goal.execute_button.options['state'] == 'disabled'
    assert chain.pending is None and not Thread.all and not calls and not completed
    assert 'private provider' not in goal.status.get()


def test_completion_callback_runtime_error_revokes_preview_without_escaping_poll(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, _plan, _result, _calls, _completed, *_ = h
    seen = []
    def fail(*args):
        seen.append(args)
        raise RuntimeError('private callback error')
    goal.on_office_planning_result = fail
    request(h)
    finish(chain)
    assert len(seen) == 1 and chain.pending is None and chain._restore_display is None
    assert goal.plan is None and goal.plan_instruction_binding is None
    assert goal.execute_button.options['state'] == 'disabled'
    assert 'private callback' not in goal.status.get()


@pytest.mark.parametrize('failure', ['route', 'ticket', 'session', 'guard'])
def test_precheck_rejection_revokes_existing_executable_preview(tmp_path, monkeypatch, failure):
    h = binding_harness(tmp_path, monkeypatch)
    chain, context, _log, _state, goal, _office, plan, result, calls, completed, *_ = h
    goal.plan = deepcopy(plan)
    goal.plan_instruction_binding = {'old': 'binding'}
    goal.execute_button.configure(state='normal')
    result['_office_guard'] = deepcopy(goal.office_planning_context())
    ticket = goal.draft_ticket()
    if failure == 'route':
        context['scope'] = '当前成果批次'
    elif failure == 'ticket':
        ticket = {**ticket, 'revision': ticket['revision'] + 1}
    elif failure == 'session':
        goal.session = ComputerSession.create(tmp_path / 'wrong-session')
    else:
        result['_office_guard'] = {'stale': True}
    assert goal.show_plan(plan, instruction_ticket=ticket, planning_result=result) is False
    assert goal.plan is None and goal.plan_instruction_binding is None
    assert goal.execute_button.options['state'] == 'disabled'
    assert chain.pending is None and not Thread.all and not calls and not completed


def test_real_worker_uses_frozen_inputs_without_reading_ui_context(tmp_path, monkeypatch):
    h = binding_harness(tmp_path, monkeypatch)
    chain, _context, _log, _state, goal, _office, plan, _result, _calls, completed, *_ = h
    from agent_service import office_workspace
    ui_thread = get_ident()
    reads, workers, bindings, verifications = [], [], [], []
    original_context, original_office = chain.context, chain.office_context
    original_bind = chain_ui.core.bind_edit_preview
    original_verify = chain_ui.workspace_state.verify
    original_office_verify = office_workspace.verify
    def only_ui(provider, kind):
        def call():
            assert get_ident() == ui_thread, kind
            reads.append(kind)
            return provider()
        return call
    chain.context = only_ui(original_context, 'chain context')
    chain.office_context = only_ui(original_office, 'Office context')
    goal.office_planning_context = only_ui(original_context, 'planning guard')
    def bind(*args):
        assert get_ident() != ui_thread
        bindings.append(get_ident())
        return original_bind(*args)
    def verify(*args):
        assert get_ident() != ui_thread
        verifications.append('workspace')
        return original_verify(*args)
    def office_verify(*args):
        assert get_ident() != ui_thread
        verifications.append('office')
        return original_office_verify(*args)
    class Worker(NativeThread):
        def start(self):
            workers.append(self)
            super().start()
    monkeypatch.setattr(chain_ui.threading, 'Thread', Worker)
    monkeypatch.setattr(chain_ui.core, 'bind_edit_preview', bind)
    monkeypatch.setattr(chain_ui.workspace_state, 'verify', verify)
    monkeypatch.setattr(office_workspace, 'verify', office_verify)
    assert request(h) is False
    workers[0].join(5)
    assert not workers[0].is_alive()
    chain.poll()
    assert goal.plan == plan and len(completed) == 1 and len(bindings) == 1
    assert verifications == ['workspace', 'office']
    assert set(reads) == {'chain context', 'Office context', 'planning guard'}
