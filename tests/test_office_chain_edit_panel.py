"""Independent chain edit UI wiring and actual GoalPanel lifecycle checks."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import queue
import threading

import pytest

from agent_service import computer_goal_panel as ui, office_task_chain_panel as module
from agent_service.computer import ComputerSession
from test_history_resume_gui import _Widget, _Variable, _FakeDraft
from test_office_task_chain_panel import harness, finish, main_functions, Thread
from test_office_task_chain_panel import test_real_goal_completion_and_main_history_refresh_preserve_auto_guard as _real_completion
from test_computer_goal_panel import hidden_root, panel
from test_instruction_draft_panel import execution, DeferredThread
from test_office_refresh_draft import refresh, _open_and_refresh


def edit_harness(tmp_path, monkeypatch):
    chain, context, log, state = harness(tmp_path, monkeypatch)
    goal = ui.GoalPanel.__new__(ui.GoalPanel)
    goal.text = _Widget(wrap='word')
    goal.text.edit_modified = lambda *args: False
    goal.text.insert('1.0', state['payload']['draft'])
    goal.instruction_draft = _FakeDraft(goal.text)
    goal.closed, goal.running, goal.external_activity_seen = False, False, False
    goal.stopped = threading.Event()
    goal.session = ComputerSession.create(tmp_path / 'computers')
    goal.plan = goal.plan_instruction_binding = None
    goal.poll_id = None
    goal.root = SimpleNamespace(after=lambda *args: None)
    goal.external_busy = lambda: False
    goal.set_busy = lambda value: setattr(goal, 'running', value)
    goal.show_text = lambda text: None
    goal._update_pause_checkbox = lambda: None
    goal.status = _Variable('')
    goal.execute_button = _Widget(state='disabled')
    goal.control = SimpleNamespace(configure=lambda **kw: None, current=lambda *args: None, set=lambda *args: None)
    goal.messages = queue.Queue()
    goal.before_show_plan = chain.before_plan
    monkeypatch.setattr(ui, 'describe', lambda plan: 'actual diff')
    context['workspace']['payload']['draft'] = state['payload']['draft']
    context['workspace']['revision'] = {'revision': 1, 'text': state['payload']['draft']}
    chain.issued = deepcopy(context['workspace']['revision'])
    scope = [{'path': state['payload']['records'][0]['path'], 'sha256': 'a' * 64,
              'locations': [{'paragraph_index': 0}]}]
    office = {'draft': state['payload']['draft'], 'records': deepcopy(state['payload']['records']), 'scope': scope}
    context['goal'].update(draft=goal.instruction_draft.snapshot(), office=office)
    chain.office_context = lambda: {'payload': deepcopy(office)}
    original_context = chain.context
    def current():
        value = original_context()
        value['goal']['draft'] = goal.instruction_draft.snapshot()
        value['goal']['plan'] = deepcopy(goal.plan)
        value['goal']['stopped'] = goal.stopped.is_set()
        value['goal']['office'] = deepcopy(office)
        return value
    chain.context = current
    chain.route_context = {key: deepcopy(current()[key]) for key in ('workspace', 'scope')}
    state.update(status='needs_scope', binding={'session_root': str(goal.session.root),
                 'routed': {'state': 'needs_scope', 'plan': None, 'edit': {'instruction': office['draft']}},
                 'refs': []})
    chain.active = deepcopy(state)
    plan = {'mode': 'agent', 'instruction': office['draft'], 'sha256': 'e' * 64,
            'goal': {'kind': 'interactive', 'browser': None, 'edits': [{'id': 'e1'}]}}
    result = {'plan': deepcopy(plan), 'reason': 'literal difference',
              'office_result': {'kind': 'edits', 'record': 'actual-local-record'},
              '_office_guard': deepcopy(current())}
    calls = []
    def bind_preview(*args):
        calls.append(('initial', deepcopy(args[4:])))
        updated = deepcopy(state)
        updated.update(status='edit_preview', edit_plan=deepcopy(plan), edit_payload=deepcopy(office),
                       run=str(goal.session.root / 'goals' / ('e' * 64 + '.run')))
        return updated
    def bind_refresh(*args):
        calls.append(('refresh', deepcopy(args[4:])))
        updated = deepcopy(chain.active)
        updated['edit_plan'] = deepcopy(args[4])
        return updated
    monkeypatch.setattr(module.core, 'bind_edit_preview', bind_preview)
    monkeypatch.setattr(module.core, 'bind_edit_refresh', bind_refresh)
    return chain, context, log, state, goal, office, plan, result, calls


def test_real_show_plan_binds_original_planning_result_before_enabling_execute(tmp_path, monkeypatch):
    chain, _context, _log, _state, goal, office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    assert goal.show_plan(plan, instruction_ticket=goal.instruction_draft.snapshot(), planning_result=result)
    assert calls == [('initial', (plan, {k: result[k] for k in ('plan', 'reason', 'office_result')}, office))]
    assert goal.execute_button.options['state'] == 'normal'
    assert goal.instruction_ticket(plan) == goal.instruction_draft.snapshot()
    assert chain.active['status'] == 'edit_preview'


@pytest.mark.parametrize('failure', [False, None, 'exception'])
def test_real_show_plan_validator_failure_invalidates_old_plan_and_never_enables_execution(tmp_path, monkeypatch, failure):
    _chain, _context, _log, _state, goal, _office, plan, _result, _calls = edit_harness(tmp_path, monkeypatch)
    goal.plan = {'old': 'plan'}
    goal.plan_instruction_binding = {'old': 'binding'}
    goal.execute_button.configure(state='normal')
    if failure == 'exception':
        goal.before_show_plan = lambda *args: (_ for _ in ()).throw(ValueError('private path'))
    else:
        goal.before_show_plan = lambda *args: failure
    assert goal.show_plan(plan, instruction_ticket=goal.instruction_draft.snapshot()) is False
    assert goal.plan is None and goal.plan_instruction_binding is None
    assert goal.execute_button.options['state'] == 'disabled'
    assert '未开放执行' in goal.status.get() and 'private path' not in goal.status.get()


@pytest.mark.parametrize('change', ['main_revision', 'main_revert', 'main_source', 'main_source_revert',
                                  'main_batch', 'main_scope', 'stopped', 'closed', 'goal_draft', 'goal_revert',
                                  'wrong_ticket', 'bind_failure'])
def test_changed_ticket_or_failed_binding_blocks_edit_preview(tmp_path, monkeypatch, change):
    chain, context, _log, _state, goal, _office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    ticket = goal.instruction_draft.snapshot()
    if change in ('main_revision', 'main_revert'):
        context['workspace']['revision']['revision'] += 2
    elif change == 'main_source':
        context['workspace']['payload']['source'] = None
    elif change == 'main_source_revert':
        context['workspace']['binding']['source']['generation'] += 2
    elif change == 'main_batch':
        context['workspace']['payload']['records'][0]['sha256'] = 'b' * 64
    elif change == 'main_scope':
        context['scope'] = '当前成果批次'
    elif change == 'stopped':
        goal.stopped.set()
    elif change == 'closed':
        chain.close()
    elif change in ('goal_draft', 'goal_revert'):
        goal.instruction_draft.adopt('other draft')
        if change == 'goal_revert':
            goal.instruction_draft.adopt(ticket['text'])
    elif change == 'wrong_ticket':
        ticket = {**ticket, 'revision': ticket['revision'] + 1}
    else:
        monkeypatch.setattr(module.core, 'bind_edit_preview',
                            lambda *args: (_ for _ in ()).throw(ValueError('source changed')))
    assert goal.show_plan(plan, instruction_ticket=ticket, planning_result=result) is False
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    assert not calls


def test_real_poll_forwards_planning_proof_and_does_not_publish_failed_preview(tmp_path, monkeypatch):
    chain, _context, _log, _state, goal, _office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    completed = []
    goal.on_office_planning_result = lambda *args: completed.append(args)
    goal.office_planning_context = lambda: result['_office_guard']
    goal.messages.put(('propose', deepcopy(result), None, goal.instruction_draft.snapshot()))
    goal.poll()
    assert len(calls) == len(completed) == 1 and goal.plan == plan
    monkeypatch.setattr(module.core, 'bind_edit_preview',
                        lambda *args: (_ for _ in ()).throw(ValueError('cannot bind')))
    goal.messages.put(('propose', deepcopy(result), None, goal.instruction_draft.snapshot()))
    goal.poll()
    assert len(completed) == 1 and goal.plan is None
    assert goal.execute_button.options['state'] == 'disabled'


def test_refresh_of_exact_current_edit_preserves_ticket_and_uses_refresh_binding(tmp_path, monkeypatch):
    chain, _context, _log, _state, goal, _office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    ticket = goal.instruction_draft.snapshot()
    assert goal.show_plan(plan, instruction_ticket=ticket, planning_result=result)
    fresh = {**deepcopy(plan), 'sha256': 'f' * 64}
    goal.messages.put(('propose', {'plan': fresh, 'reason': 'local refresh'}, None, ticket))
    goal.poll()
    assert [call[0] for call in calls] == ['initial', 'refresh']
    assert goal.plan == fresh and goal.instruction_ticket(fresh) == ticket


@pytest.mark.parametrize('initial_scope', ['当前选中资料', '当前成果批次'])
def test_refresh_rejects_main_route_scope_change_with_identical_workspace_and_office_inputs(
        tmp_path, monkeypatch, initial_scope):
    chain, context, _log, _state, goal, _office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    context['scope'] = initial_scope
    chain.route_context['scope'] = initial_scope
    ticket = goal.instruction_draft.snapshot()
    assert goal.show_plan(plan, instruction_ticket=ticket, planning_result=result)
    unchanged_workspace = deepcopy(context['workspace'])
    context['scope'] = ('当前成果批次' if initial_scope == '当前选中资料' else '当前选中资料')
    goal.messages.put(('propose', {'plan': {**plan, 'sha256': 'f' * 64}, 'reason': 'local refresh'}, None, ticket))
    goal.poll()
    assert context['workspace'] == unchanged_workspace
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    assert [call[0] for call in calls] == ['initial']


@pytest.mark.parametrize('change', ['scope', 'records', 'draft'])
def test_real_poll_refresh_cannot_restore_old_scope_after_office_inputs_change(tmp_path, monkeypatch, change):
    chain, _context, _log, _state, goal, office, plan, result, calls = edit_harness(tmp_path, monkeypatch)
    ticket = goal.instruction_draft.snapshot()
    assert goal.show_plan(plan, instruction_ticket=ticket, planning_result=result)
    fresh = {**deepcopy(plan), 'sha256': 'f' * 64}
    # A local adapter refresh result has no _office_guard. Its UI must compare
    # current Office inputs with the bound edit payload before binding/enabling.
    if change == 'scope':
        office['scope'][0]['locations'] = [{'paragraph_index': 9}]
    elif change == 'records':
        office['records'][0]['sha256'] = 'b' * 64
    else:
        office['draft'] = 'other requirements'
    goal.messages.put(('propose', {'plan': fresh, 'reason': 'local refresh'}, None, ticket))
    goal.poll()
    assert goal.plan is None and goal.execute_button.options['state'] == 'disabled'
    assert [call[0] for call in calls] == ['initial']


def test_unrelated_new_main_route_detaches_old_chain_and_does_not_intercept_its_plan(tmp_path, monkeypatch):
    chain, _context, _log, _state, goal, _office, plan, _result, calls = edit_harness(tmp_path, monkeypatch)
    assert chain.bind(goal.session, 'new unrelated instruction', {'state': 'ready'}, [],
                      {'revision': 9, 'text': 'new unrelated instruction'})
    assert chain.active is chain.issued is chain.cleared is chain.handoff is None
    assert chain.route_context is None
    assert goal.show_plan(plan, instruction_ticket=goal.instruction_draft.snapshot())
    assert not calls and goal.execute_button.options['state'] == 'normal'


def test_batch_first_step_create_keeps_entire_batch_and_removes_single_source(tmp_path, monkeypatch):
    chain, context, log, state = harness(tmp_path, monkeypatch)
    context['scope'] = '当前成果批次'
    context['workspace']['payload']['records'].append({'path': str(tmp_path / 'two.xlsx'), 'sha256': 'b' * 64})
    captured = []
    monkeypatch.setattr(module.core, 'create', lambda *args, **kwargs: captured.append(deepcopy(args[2])) or state['id'])
    monkeypatch.setattr(module.core, 'inspect', lambda *args: deepcopy(state))
    assert chain.create(['first instruction', 'second'], deepcopy(context))
    finish(chain)
    assert captured == [{**context['workspace']['payload'], 'source': None}]
    assert len(captured[0]['records']) == 2


def test_batch_first_step_dialog_needs_no_single_source_and_explicitly_names_all_outputs(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from agent_service import office_scope_panel
    chain, context, _log, _state = harness(tmp_path, monkeypatch)
    context['scope'] = '当前成果批次'
    context['workspace']['payload']['source'] = None
    widgets = []
    class Widget:
        def __init__(self, *args, **options):
            self.options = options
            widgets.append(self)
        def pack(self, **kwargs):
            pass
        def insert(self, *args):
            pass
    monkeypatch.setattr(tk, 'Text', Widget)
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    monkeypatch.setattr(office_scope_panel, '_modal', lambda *args: (object(), lambda: None))
    assert chain.new_dialog()
    labels = [widget.options['text'] for widget in widgets if 'text' in widget.options]
    assert any('第一步使用当前全部成果批次' in text and '后续每步使用上一步生成的全部 Word/Excel' in text for text in labels)
    assert not Thread.all


def test_main_prepare_edit_restores_exact_session_plan_and_both_draft_tickets(tmp_path):
    session = ComputerSession.create(tmp_path / 'computers')
    run = session.root / 'goals' / ('e' * 64 + '.run')
    main_ticket, goal_ticket = {'revision': 1, 'text': 'first'}, {'revision': 2, 'text': 'first'}
    calls = []
    goal = SimpleNamespace(session=None, tab='goal-tab', draft_ticket=lambda: deepcopy(goal_ticket),
                           instruction_draft=SimpleNamespace(snapshot=lambda: deepcopy(goal_ticket)),
                           show_plan=lambda plan, **kw: calls.append(('show', plan, kw)) or True)
    routed = {'main': None, 'goal': None}
    env = main_functions(['office_chain_prepare_edit'], {
        'Path': Path, 'goal_panel': goal, 'task_draft': SimpleNamespace(snapshot=lambda: deepcopy(main_ticket)),
        'routed_instruction': routed, 'tabs': SimpleNamespace(select=lambda tab: calls.append(('select', tab)))})
    plan = {'instruction': 'first', 'goal': 'exact persisted goal'}
    assert env['office_chain_prepare_edit']({'run': str(run), 'edit_plan': plan})
    assert goal.session.root == session.root and calls == [
        ('show', plan, {'instruction_ticket': goal_ticket}), ('select', 'goal-tab')]
    assert routed == {'main': main_ticket, 'goal': goal_ticket}
    goal.show_plan = lambda *args, **kwargs: False
    routed.update(main=None, goal=None)
    assert not env['office_chain_prepare_edit']({'run': str(run), 'edit_plan': plan})
    assert routed == {'main': None, 'goal': None}


@pytest.mark.parametrize('change', [None, 'office_scope', 'office_draft', 'office_records', 'goal_revision', 'verify_failure'])
def test_edit_preview_restore_uses_verified_office_inputs_and_live_tickets(tmp_path, monkeypatch, change):
    import json
    from agent_service import office_workspace
    chain, context, log, state, goal, office, plan, _result, calls = edit_harness(tmp_path, monkeypatch)
    saved_office = deepcopy(office)
    goal.instruction_draft.adopt('')
    office['draft'] = ''
    state.update(status='edit_preview', edit_plan=deepcopy(plan), edit_payload=saved_office,
                 run=str(goal.session.root / 'goals' / ('e' * 64 + '.run')))
    (goal.session.root / 'goals').mkdir(exist_ok=True)
    (goal.session.root / 'goals' / ('e' * 64 + '.json')).write_text(json.dumps(plan), encoding='utf-8')
    verified_calls, office_applications = [], []
    def verify(payload, base):
        verified_calls.append(deepcopy(payload))
        if change == 'verify_failure':
            raise ValueError('changed source')
        return {'payload': deepcopy(payload), 'refs': ['reverified original source']}
    monkeypatch.setattr(office_workspace, 'verify', verify)
    def apply_office(verified):
        office_applications.append(deepcopy(verified))
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
    assert chain.restore(state['id'])
    if change == 'office_scope':
        office['scope'][0]['locations'] = [{'paragraph_index': 9}]
    elif change == 'office_draft':
        office['draft'] = 'new requirements'
    elif change == 'office_records':
        office['records'][0]['sha256'] = 'd' * 64
    elif change == 'goal_revision':
        goal.instruction_draft.adopt('new')
        goal.instruction_draft.adopt('')
    finish(chain)
    assert verified_calls == [saved_office]
    if change:
        assert not office_applications and not log.applied and not calls
        assert goal.plan is None and routed == {'main': None, 'goal': None}
    else:
        assert len(log.applied) == len(office_applications) == 1
        assert office == saved_office and goal.plan == plan
        assert not calls  # Already verified in this restore worker; no Tk rebind.
        assert routed == {'main': context['workspace']['revision'], 'goal': goal.instruction_draft.snapshot()}
        assert goal.instruction_ticket(plan) == goal.instruction_draft.snapshot()


def test_needs_scope_restore_reuses_exact_route_without_planning_or_execution(tmp_path, monkeypatch):
    chain, _context, log, state, goal, _office, _plan, _result, calls = edit_harness(tmp_path, monkeypatch)
    goal.instruction_draft.adopt('')
    assert chain.restore(state['id'])
    finish(chain)
    assert len(log.applied) == len(log.prepared) == 1
    session, instruction, routed, refs, current = log.prepared[0]
    assert session.root == goal.session.root and instruction == state['payload']['draft']
    assert routed == state['binding']['routed'] and refs == state['binding']['refs'] and current()
    assert not calls and goal.plan is None


@pytest.mark.parametrize('change', [None, 'mode', 'mode_revert', 'scope', 'source_revert', 'stop'])
def test_real_adapter_refresh_and_goal_poll_bind_complete_planning_context(refresh, change):
    p, ticket, original, fresh, seen, buttons, calls = refresh
    context = {'mode': ui.MODES[9], 'mode_generation': 1, 'source_generation': 2,
               'draft': ticket, 'scope': [{'paragraph_index': 0}], 'stopped': False}
    p.office_planning_context = lambda: deepcopy(context)
    before = deepcopy(context)
    _open_and_refresh(p, buttons)
    if change == 'mode':
        context['mode'] = ui.MODES[0]
    elif change == 'mode_revert':
        context['mode_generation'] += 2
    elif change == 'scope':
        context['scope'] = [{'paragraph_index': 9}]
    elif change == 'source_revert':
        context['source_generation'] += 2
    elif change == 'stop':
        context['stopped'] = True
    DeferredThread.finish()
    p.poll()
    assert calls == ['refresh'] and seen == [] and p.instruction_draft.unchanged(ticket)
    if change:
        assert p.plan is None and str(p.execute_button.cget('state')) == 'disabled'
        assert '迟到规划' in p.status.get()
    else:
        assert context == before and p.plan == fresh and p.instruction_ticket(fresh) == ticket


def test_real_edit_execute_metadata_adopts_canonical_second_stage_inputs(tmp_path, monkeypatch):
    # Real edit execution emits kind/provenance metadata; persisted chain and
    # main workspace inputs intentionally contain only exact path/SHA pairs.
    _real_completion(tmp_path, monkeypatch, False, False, execution_kind='execute')


@pytest.mark.parametrize('change', ['new_main_draft', 'new_goal_draft', 'main_source_revert', 'stop'])
def test_real_edit_execute_metadata_still_preserves_user_changes(tmp_path, monkeypatch, change):
    def edit_after_handoff(chain, context, goal, main_draft):
        if change == 'new_main_draft':
            main_draft.adopt('new main task')
        elif change == 'new_goal_draft':
            goal.instruction_draft.adopt('new continuous task')
        elif change == 'main_source_revert':
            context['workspace']['binding']['source']['generation'] += 2
        else:
            goal.stopped.set()
    _real_completion(tmp_path, monkeypatch, False, False, execution_kind='execute',
                     after_handoff=edit_after_handoff)


def test_real_final_bundle_completion_adopts_final_batch_and_reports_chain_complete(tmp_path, monkeypatch):
    _real_completion(tmp_path, monkeypatch, False, False, execution_kind='office_bundle_execute', terminal=True)
