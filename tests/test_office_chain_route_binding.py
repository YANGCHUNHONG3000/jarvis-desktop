"""Independent route binding and restore reuse; no model or native Office."""
from copy import deepcopy
import json
from pathlib import Path
from threading import Thread as NativeThread, get_ident
from types import SimpleNamespace

import pytest

from agent_service import office_task_chain_panel as chain_ui, office_task_entry as entry_ui
from agent_service.computer import ComputerSession
from agent_service.computer_goal_panel import MODES
from test_history_resume_gui import _Variable
from test_office_chain_edit_panel import edit_harness
from test_office_task_chain_panel import Thread, finish, mutate, main_functions
from test_task_route_handoff_panel import saved_route


def route_harness(tmp_path, monkeypatch, saved_route, *, editing=False):
    from agent_service.office_chain_route_binding import OfficeChainRouteBinding
    chain, context, log, state, goal, office, _plan, _result, _calls = edit_harness(tmp_path, monkeypatch)
    session, instruction, routed, refs = saved_route('office_edit' if editing else 'compare_tables')
    chain.computer_base, chain.task_base = session.root.parent, tmp_path
    records = [{'path': ref['artifact'], 'sha256': ref['sha256']} for ref in refs]
    state.update(status='ready', binding=None, payload={'draft': instruction, 'records': records,
                 'source': records[0]}, run=None)
    context['workspace']['payload'] = deepcopy(state['payload'])
    context['workspace']['revision'] = {'revision': 2, 'text': instruction}
    goal.instruction_draft.adopt(instruction)
    office.update(draft=instruction, records=deepcopy(records))
    goal.session = session
    goal.mode = _Variable(MODES[0])
    goal.mode_changed = lambda **_kw: None
    goal.capability_source_generation = 0
    goal.capability_source_label = _Variable('')
    goal.update_office_bundle_button = lambda: None
    selected, adopted, bindings, waiting = [], [], [], []
    goal.use_generated_artifacts = lambda records: selected.append(deepcopy(records))
    chain.active = deepcopy(state)
    chain.issued = deepcopy(context['workspace']['revision'])
    chain.route_context = {key: deepcopy(chain.context()[key]) for key in ('workspace', 'scope')}
    chain.on_busy = lambda value: (log.busy.append(value), goal.set_busy(value))
    source = SimpleNamespace(current=True)
    current = lambda: source.current
    ticket = lambda: deepcopy(context['workspace']['revision'])

    def bound_state():
        value = deepcopy(state)
        value.update(status='needs_scope' if editing else 'preview', binding={
            'session_root': str(session.root), 'routed': deepcopy(routed), 'refs': deepcopy(refs)},
            run=None if editing else str(session.root / 'goals' / (routed['plan']['sha256'] + '.bundle')))
        return value

    def bind(*args):
        bindings.append(deepcopy(args[1:3]))
        assert args[3].root == session.root and args[4] == routed and args[5] == refs
        return bound_state()
    monkeypatch.setattr(chain_ui.core, 'bind_preview', bind)

    def adopt(s, text, route, references, is_current, source_ticket):
        adopted.append((s, text, deepcopy(route), deepcopy(references), is_current, deepcopy(source_ticket)))
        return goal.use_task_route(s, text, route, references, is_current)

    bridge = OfficeChainRouteBinding(goal, chain, adopt=adopt, source_ticket=ticket,
                                    set_waiting=waiting.append)
    chain.prepare = bridge.prepare
    return SimpleNamespace(chain=chain, context=context, log=log, state=state, goal=goal,
        session=session, instruction=instruction, routed=routed, refs=refs, office=office,
        selected=selected, adopted=adopted, bindings=bindings, waiting=waiting, source=source,
        current=current, ticket=ticket, bridge=bridge, bound_state=bound_state)


def request(h):
    return h.bridge.prepare(h.session, h.instruction, h.routed, h.refs, h.current)


@pytest.mark.parametrize('editing', [False, True])
def test_fresh_route_binds_in_background_then_uses_original_goal_adoption(
        tmp_path, monkeypatch, saved_route, editing):
    h = route_harness(tmp_path, monkeypatch, saved_route, editing=editing)
    old = deepcopy(h.chain.active)
    assert request(h) is entry_ui.DEFERRED_ROUTE
    assert not h.bindings and not h.adopted and h.chain.active == old
    assert h.goal.plan is None and h.goal.execute_button.options['state'] == 'disabled'
    finish(h.chain)
    assert len(h.bindings) == len(h.adopted) == 1 and h.adopted[0][4] is h.current
    assert h.adopted[0][5] == h.ticket()
    assert h.bridge.pending is None and h.waiting == [True, False]
    assert h.chain._restore_route is None and not h.log.applied
    if editing:
        assert h.selected and h.goal.plan is None
    else:
        assert h.goal.plan == h.routed['plan']
        assert h.goal.capability_history_seed['is_current'] is h.current
        assert h.goal.execute_button.options['state'] == 'normal'
        h.source.current = False
        assert not h.goal.capability_history_seed['is_current']()


def test_real_task_entry_poll_recognizes_deferred_route_without_a_terminal_status(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    statuses = []
    source = {'instruction': h.instruction, 'draft': h.instruction, 'records': [],
              'draft_revision': 0, 'instruction_revision': 2}
    entry = entry_ui.OfficeTaskEntry(context=lambda: deepcopy(source), busy=lambda: False,
        on_busy=lambda _value: None, prepare=h.bridge.prepare, status=statuses.append,
        session_factory=lambda: h.session, task_base=tmp_path, clock=lambda: 100)
    entry.generation = 4
    import threading
    entry.pending = {'context': deepcopy(source), 'generation': 4, 'stop': threading.Event(),
                     'deadline': 195, 'discard': False}
    entry.messages.put((4, (h.session, h.routed, h.refs), None))
    entry.poll()
    assert h.bridge.pending is not None and not h.adopted
    assert not any('已接入' in text or '未接入' in text for text in statuses)
    finish(h.chain)
    assert len(h.adopted) == 1


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'goal_revert',
                                  'source_revert', 'mode_revert', 'scope', 'plan'])
def test_late_bound_route_never_replaces_changed_or_reverted_inputs(
        tmp_path, monkeypatch, saved_route, change):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    request(h)
    previous = deepcopy(h.chain.active)
    if change in {'goal_new', 'goal_revert'}:
        h.goal.instruction_draft.adopt('a new goal')
        if change == 'goal_revert':
            h.goal.instruction_draft.adopt(h.instruction)
    elif change == 'plan':
        h.goal.plan = {'new': 'plan'}
    else:
        mutate(h.context, change)
    current = deepcopy(h.chain.context())
    finish(h.chain)
    assert h.chain.context() == current and h.chain.active == previous
    assert not h.adopted and h.bridge.pending is None and h.waiting == [True, False]


@pytest.mark.parametrize('cancel', ['bridge', 'chain', 'stop', 'close'])
def test_cancellation_keeps_waiting_until_worker_settles_then_never_adopts(
        tmp_path, monkeypatch, saved_route, cancel):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    request(h)
    if cancel == 'bridge':
        h.bridge.cancel()
    elif cancel == 'chain':
        h.chain.cancel()
    elif cancel == 'close':
        h.chain.close()
    else:
        h.goal.stopped.set()
    assert h.waiting == [True]
    finish(h.chain)
    assert not h.adopted and h.goal.plan is None
    assert h.bridge.pending is None and h.waiting == [True, False]


@pytest.mark.parametrize('editing', [False, True])
def test_restored_route_reuses_worker_binding_once_without_another_core_call(
        tmp_path, monkeypatch, saved_route, editing):
    h = route_harness(tmp_path, monkeypatch, saved_route, editing=editing)
    restored = h.bound_state()
    h.chain.active = deepcopy(restored)
    h.goal.instruction_draft.adopt('')
    h.context['workspace']['payload']['draft'] = ''
    verified = {'payload': deepcopy(restored['payload'])}
    assert h.chain._adopt(restored, verified, auto=False, before={})
    assert not h.bindings and len(h.adopted) == 1
    assert h.bridge.pending is h.chain.pending is None and not h.waiting
    assert h.chain._restore_route is None
    assert h.goal.plan == restored['binding']['routed']['plan']


@pytest.mark.parametrize('attack', ['request', 'decision', 'outcome', 'plan', 'run'])
def test_restored_route_keeps_small_record_and_undispatched_run_checks(
        tmp_path, monkeypatch, saved_route, attack):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    restored = h.bound_state()
    h.chain.active = deepcopy(restored)
    h.goal.instruction_draft.adopt('')
    h.context['workspace']['payload']['draft'] = ''
    if attack in {'request', 'decision', 'outcome'}:
        (Path(h.routed['routing']) / (attack + '.json')).write_text('{}', encoding='utf-8')
    elif attack == 'plan':
        (h.session.root / 'goals' / (h.routed['plan']['sha256'] + '.json')).write_text('{}', encoding='utf-8')
    else:
        Path(restored['run']).mkdir()
    assert not h.chain._adopt(restored, {'payload': deepcopy(restored['payload'])}, auto=False, before={})
    assert not h.bindings and h.goal.plan is None and h.chain._restore_route is None
    assert h.goal.execute_button.options['state'] == 'disabled'


@pytest.mark.parametrize('failure', ['bind', 'verify', 'adopt'])
def test_failures_release_bridge_pending_and_never_enable_execution(
        tmp_path, monkeypatch, saved_route, failure):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    def fail(*_args, **_kwargs):
        raise RuntimeError('private evidence detail')
    if failure == 'bind':
        monkeypatch.setattr(chain_ui.core, 'bind_preview', fail)
    elif failure == 'verify':
        monkeypatch.setattr(chain_ui.workspace_state, 'verify', fail)
    else:
        h.bridge.adopt = fail
    request(h)
    finish(h.chain)
    assert h.bridge.pending is None and h.chain.pending is None and h.waiting == [True, False]
    assert h.goal.plan is None and h.goal.execute_button.options['state'] == 'disabled'
    assert h.chain._restore_route is None
    assert 'private evidence detail' not in h.goal.status.get()


@pytest.mark.parametrize('kind', ['unrelated', 'new_ticket'])
def test_unrelated_route_preserves_synchronous_boolean_adoption(
        tmp_path, monkeypatch, saved_route, kind):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    if kind == 'unrelated':
        h.chain.active = None
    else:
        h.chain.issued = {'revision': 99, 'text': 'previous task'}
    assert request(h) is True
    assert not h.bindings and len(h.adopted) == 1 and not h.waiting
    assert h.bridge.pending is h.chain.pending is None
    if kind == 'new_ticket':
        assert h.chain.active is h.chain.issued is None


def test_source_guard_false_at_completion_discards_saved_binding_without_adoption(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    request(h)
    Thread.all[-1].run()
    h.source.current = False
    h.chain.poll()
    assert h.bindings and not h.adopted and h.bridge.pending is None
    assert h.goal.plan is None and h.chain._restore_route is None


def test_late_result_cannot_replace_a_new_goal_session(tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    request(h)
    replacement = ComputerSession.create(tmp_path / 'new-target-session')
    h.goal.session = replacement
    finish(h.chain)
    assert not h.adopted and h.goal.session is replacement and h.goal.plan is None
    assert h.bridge.pending is None and h.chain._restore_route is None


def test_repeated_prepare_cannot_enqueue_a_second_binding(tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    assert request(h) is entry_ui.DEFERRED_ROUTE
    assert request(h) is not True
    assert len(Thread.all) == 1 and not h.adopted
    finish(h.chain)
    assert len(h.bindings) == len(h.adopted) == 1 and h.waiting == [True, False]


def test_real_main_busy_callback_releases_active_before_original_goal_adoption(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    active = {'kind': None}
    previous_set_busy = h.goal.set_busy
    def set_busy(value):
        previous_set_busy(value)
        active['kind'] = 'computer_goal' if value else None
    h.goal.set_busy = set_busy
    env = main_functions(['office_chain_busy'], {'goal_panel': h.goal, 'active': active})
    h.chain.on_busy = env['office_chain_busy']
    h.chain.busy = lambda: active['kind'] is not None or h.goal.running
    h.goal.external_busy = lambda: active['kind'] is not None
    assert request(h) is entry_ui.DEFERRED_ROUTE
    assert active['kind'] == 'office_task_chain' and h.goal.running
    finish(h.chain)
    assert active['kind'] is None and not h.goal.running
    assert len(h.adopted) == 1 and h.goal.plan == h.routed['plan']


def test_main_stop_planning_cancels_both_entry_and_deferred_bridge():
    calls = []
    env = main_functions(['cancel_office_route'], {
        'office_task_entry': SimpleNamespace(cancel=lambda: calls.append('entry')),
        'office_chain_route_binding': SimpleNamespace(cancel=lambda: calls.append('bridge'))})
    env['cancel_office_route']()
    assert calls == ['entry', 'bridge']


def route_busy_callback(goal):
    active, notified, button = {'kind': None}, [], []
    env = main_functions(['office_route_busy'], {
        'active': active, 'goal_panel': goal,
        'notify_goal_external_activity': lambda *args, **kwargs:
            notified.append((args, kwargs, goal.stopped.is_set())),
        'task_route_stop': SimpleNamespace(configure=lambda **kwargs: button.append(kwargs))})
    return env['office_route_busy'], active, notified, button


def test_explicit_new_planning_clears_old_stop_after_notifying_and_can_bind(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    h.goal.stopped.set()
    busy, active, notified, button = route_busy_callback(h.goal)
    busy(True)
    assert notified == [((True,), {'force': True}, True)]
    assert active['kind'] == 'office_task_planning' and not h.goal.stopped.is_set()
    assert button[-1] == {'state': 'normal'}
    busy(False)
    assert active['kind'] is None and button[-1] == {'state': 'disabled'}
    assert request(h) is entry_ui.DEFERRED_ROUTE
    finish(h.chain)
    assert len(h.adopted) == 1 and h.goal.plan == h.routed['plan']


def test_stop_after_new_planning_is_never_cleared_by_release_or_completion(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    busy, _active, _notified, _button = route_busy_callback(h.goal)
    busy(True)
    busy(False)
    assert request(h) is entry_ui.DEFERRED_ROUTE
    h.goal.stopped.set()
    busy(False)
    assert h.goal.stopped.is_set()
    finish(h.chain)
    assert h.goal.stopped.is_set() and not h.adopted and h.goal.plan is None
    assert h.bridge.pending is None and h.waiting == [True, False]


def test_native_binding_worker_never_reads_ui_or_source_guard(
        tmp_path, monkeypatch, saved_route):
    h = route_harness(tmp_path, monkeypatch, saved_route)
    ui_thread = get_ident()
    workers, work = [], []
    original_context, original_ticket = h.chain.context, h.bridge.source_ticket
    original_bind = chain_ui.core.bind_preview
    def ui_only(provider):
        def call():
            assert get_ident() == ui_thread
            return provider()
        return call
    h.chain.context = ui_only(original_context)
    h.bridge.source_ticket = ui_only(original_ticket)
    h.current = ui_only(h.current)
    def bind(*args):
        assert get_ident() != ui_thread
        work.append(get_ident())
        return original_bind(*args)
    class Worker(NativeThread):
        def start(self):
            workers.append(self)
            super().start()
    monkeypatch.setattr(chain_ui.threading, 'Thread', Worker)
    monkeypatch.setattr(chain_ui.core, 'bind_preview', bind)
    assert request(h) is entry_ui.DEFERRED_ROUTE
    workers[0].join(5)
    assert not workers[0].is_alive()
    h.chain.poll()
    assert len(work) == len(h.adopted) == 1 and h.bridge.pending is None
