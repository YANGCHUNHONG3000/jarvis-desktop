"""Independent local task-chain UI guards; no API or native Office execution."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import office_task_chain_panel as module


class Thread:
    all = []

    def __init__(self, *, target, name, daemon):
        self.target, self.name = target, name

    def start(self):
        self.all.append(self)

    def run(self):
        self.target()


def harness(tmp_path, monkeypatch):
    Thread.all = []
    monkeypatch.setattr(module.threading, 'Thread', Thread)
    record = {'path': str(tmp_path / 'first.docx'), 'sha256': 'a' * 64}
    context = {'scope': '当前选中资料', 'workspace': {
        'payload': {'draft': '', 'records': [record], 'source': record},
        'revision': {'revision': 0, 'text': ''},
        'binding': {'source': {'generation': 0}, 'label': record['path'],
                    'outputs': ['first'], 'selected': 'first'}, 'unavailable': False},
        'goal': {'draft': {'revision': 0, 'text': ''}, 'plan': None,
                 'mode': 'original', 'mode_generation': 0, 'source_generation': 0, 'stopped': False}}
    log = SimpleNamespace(applied=[], prepared=[], statuses=[], busy=[], scopes=[], external=False,
                          now=100.0, advanced=[])
    def apply(verified):
        log.applied.append(deepcopy(verified))
        context['workspace']['payload'] = deepcopy(verified['payload'])
        context['workspace']['revision']['revision'] += 1
        context['workspace']['revision']['text'] = verified['payload']['draft']
        return True
    def prepare(*args):
        log.prepared.append(args)
        return True
    panel = module.OfficeTaskChainPanel(
        root=None, context=lambda: deepcopy(context), busy=lambda: log.external,
        on_busy=log.busy.append, apply=apply, prepare=prepare, set_scope=log.scopes.append,
        status=log.statuses.append, store=tmp_path / 'chains', task_base=tmp_path / 'tasks',
        computer_base=tmp_path / 'computer', clock=lambda: log.now)
    monkeypatch.setattr(module.workspace_state, 'verify',
                        lambda payload, base: {'payload': deepcopy(payload), 'refs': [], 'source_ref': None})
    state = {'id': 'a' * 32, 'index': 0, 'status': 'ready',
             'payload': {**deepcopy(context['workspace']['payload']), 'draft': 'first instruction'},
             'binding': None, 'scope': 'selected', 'reason': 'not ready'}
    def advance(store, ident):
        log.advanced.append((store, ident))
        return deepcopy(state)
    monkeypatch.setattr(module.core, 'advance', advance)
    return panel, context, log, state


def finish(panel):
    Thread.all[-1].run()
    panel.poll()


def mutate(context, kind):
    workspace, goal = context['workspace'], context['goal']
    if kind == 'main_new':
        workspace['payload']['draft'] = 'new main instruction'
    elif kind == 'main_revert':
        workspace['revision']['revision'] += 2
    elif kind == 'goal_new':
        goal['draft']['text'] = 'new continuous instruction'
    elif kind == 'goal_revert':
        goal['draft']['revision'] += 2
    elif kind == 'source_revert':
        workspace['binding']['source']['generation'] += 2
    elif kind == 'source':
        workspace['payload']['source'] = None
    elif kind == 'records':
        workspace['payload']['records'].append({'path': 'extra.xlsx', 'sha256': 'b' * 64})
    elif kind == 'selected':
        workspace['binding']['selected'] = 'other'
    elif kind == 'unavailable':
        workspace['unavailable'] = True
    elif kind == 'mode':
        goal['mode'] = 'changed'
    elif kind == 'mode_revert':
        goal['mode_generation'] += 2
    elif kind == 'goal_source_revert':
        goal['source_generation'] += 2
    elif kind == 'plan':
        goal['plan'] = {'new': 'plan'}
    elif kind == 'stop':
        goal['stopped'] = True
    elif kind == 'scope':
        context['scope'] = '当前成果批次'
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'goal_revert',
                                  'source', 'source_revert', 'records', 'selected',
                                  'unavailable', 'mode', 'mode_revert', 'goal_source_revert',
                                  'plan', 'stop', 'scope'])
@pytest.mark.parametrize('poll_first', [False, True])
def test_complete_context_changes_discard_worker_without_overwrite(tmp_path, monkeypatch, change, poll_first):
    panel, context, log, _state = harness(tmp_path, monkeypatch)
    assert panel.restore('a' * 32)
    mutate(context, change)
    before = deepcopy(context)
    if poll_first:
        panel.poll()
        assert panel.pending['discard']
    finish(panel)
    assert context == before and not log.applied and not log.prepared
    assert panel.pending is None and log.busy == [True, False]


@pytest.mark.parametrize('interruption', ['cancel', 'close', 'timeout'])
def test_interruption_keeps_busy_until_worker_exits(tmp_path, monkeypatch, interruption):
    panel, _context, log, _state = harness(tmp_path, monkeypatch)
    assert panel.restore('a' * 32)
    if interruption == 'timeout':
        log.now += 41
        panel.poll()
    else:
        getattr(panel, interruption)()
    assert panel.pending['discard'] and log.busy == [True]
    assert not panel.restore('b' * 32) and len(Thread.all) == 1
    finish(panel)
    assert not log.applied and not log.prepared and panel.pending is None
    assert log.busy == [True, False]


def test_thread_failure_and_verification_failure_release_busy(tmp_path, monkeypatch):
    panel, _context, log, _state = harness(tmp_path, monkeypatch)
    monkeypatch.setattr(Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('fail')))
    assert not panel.restore('a' * 32)
    assert panel.pending is None and log.busy == [True, False]
    monkeypatch.setattr(Thread, 'start', lambda self: Thread.all.append(self))
    monkeypatch.setattr(module.workspace_state, 'verify',
                        lambda *_: (_ for _ in ()).throw(ValueError('local secret path')))
    assert panel.restore('a' * 32)
    finish(panel)
    assert panel.pending is None and log.busy == [True, False, True, False]
    assert not log.applied and 'local secret path' not in log.statuses[-1]


def test_create_binds_confirmed_snapshot_and_preserves_verbatim_instructions(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module.core, 'create', lambda *args, **kw: calls.append((args, kw)) or state['id'])
    monkeypatch.setattr(module.core, 'inspect', lambda *_: deepcopy(state))
    snapshot = deepcopy(context)
    instructions = ['first instruction', '  second authored instruction\n']
    assert panel.create(instructions, snapshot)
    finish(panel)
    assert calls[0][0][1] == instructions
    assert calls[0][0][2] == snapshot['workspace']['payload']
    assert len(log.applied) == 1 and log.scopes == ['当前选中资料'] and not log.prepared
    assert panel.issued == context['workspace']['revision']
    mutate(context, 'main_revert')
    assert not panel.create(instructions, snapshot)
    assert len(Thread.all) == 1


@pytest.mark.parametrize('changed', [None, 'main_revert', 'goal_new', 'source_revert'])
def test_new_dialog_confirmation_is_single_use_and_bound_to_opened_context(tmp_path, monkeypatch, changed):
    import tkinter as tk
    from tkinter import ttk
    from agent_service import office_scope_panel
    panel, context, log, state = harness(tmp_path, monkeypatch)
    widgets, closed = [], []
    class Widget:
        def __init__(self, *args, **options):
            self.options, self.value = options, ''
            widgets.append(self)
        def pack(self, **options):
            pass
        def insert(self, where, text):
            self.value = text
        def get(self, *args):
            return self.value
    monkeypatch.setattr(tk, 'Text', Widget)
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    monkeypatch.setattr(office_scope_panel, '_modal',
                        lambda *args: (object(), lambda: closed.append(True)))
    monkeypatch.setattr(module.core, 'create', lambda *args, **kwargs: state['id'])
    monkeypatch.setattr(module.core, 'inspect', lambda *_: deepcopy(state))
    assert panel.new_dialog()
    texts = [widget for widget in widgets if 'height' in widget.options]
    texts[0].value, texts[1].value = 'first instruction', 'second authored instruction'
    accept = next(widget.options['command'] for widget in widgets if 'command' in widget.options)
    if changed:
        mutate(context, changed)
    before = deepcopy(context)
    accept()
    accept()
    if changed:
        assert not Thread.all and not closed and not log.applied and context == before
    else:
        assert len(Thread.all) == 1 and closed == [True]
        assert panel.pending['creating'] is True
        finish(panel)
        assert len(log.applied) == 1


@pytest.mark.parametrize('blocked', ['goal_new', 'unavailable'])
def test_restore_does_not_start_on_unready_inputs(tmp_path, monkeypatch, blocked):
    panel, context, log, _state = harness(tmp_path, monkeypatch)
    mutate(context, blocked)
    assert not panel.restore('a' * 32)
    assert not Thread.all and not log.applied


@pytest.mark.parametrize('kind', ['main_new', 'records'])
def test_history_restore_does_not_replace_different_main_inputs(tmp_path, monkeypatch, kind):
    panel, context, log, _state = harness(tmp_path, monkeypatch)
    mutate(context, kind)
    before = deepcopy(context)
    assert panel.restore('a' * 32)
    finish(panel)
    assert context == before and not log.applied and not log.prepared


def test_preview_restore_only_prepares_original_plan_with_live_guard(tmp_path, monkeypatch):
    from agent_service.computer import ComputerSession
    panel, context, log, state = harness(tmp_path, monkeypatch)
    session = ComputerSession.create(tmp_path / 'sessions')
    state.update(status='preview', binding={'session_root': str(session.root),
                 'routed': {'plan': {'sha256': 'b' * 64, 'mode': 'capability'}}, 'refs': ['original ref']})
    assert panel.restore(state['id'])
    finish(panel)
    assert len(log.applied) == len(log.prepared) == 1
    session, instruction, routed, refs, current = log.prepared[0]
    assert session.root == Path(state['binding']['session_root'])
    assert instruction == state['payload']['draft'] and refs == ['original ref']
    assert routed == state['binding']['routed'] and current()
    mutate(context, 'main_revert')
    assert not current()
    assert panel.issued['text'] == state['payload']['draft']
    assert not log.advanced[1:]


def test_preview_session_disappears_after_worker_keeps_inputs_and_poll_alive(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    state.update(status='preview', binding={'session_root': str(tmp_path / 'missing-session'),
                 'routed': {'plan': {'sha256': 'b' * 64, 'mode': 'capability'}}, 'refs': []})
    assert panel.restore(state['id'])
    finish(panel)
    assert panel.pending is None and log.busy == [True, False]
    assert len(log.applied) == 1 and not log.prepared
    assert context['workspace']['payload'] == state['payload']
    assert '未接入' in log.statuses[-1]


def test_bind_calls_core_only_for_exact_main_ticket_and_authored_instruction(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    panel.active = deepcopy(state)
    panel.issued = {'revision': 1, 'text': 'first instruction'}
    calls = []
    monkeypatch.setattr(module.core, 'bind_preview', lambda *args: calls.append(args) or deepcopy(state))
    session = SimpleNamespace(root=tmp_path)
    assert panel.bind(session, 'unrelated', {}, [], panel.issued)
    assert panel.bind(session, 'first instruction', {}, [], {'revision': 2, 'text': 'first instruction'})
    assert not calls
    assert panel.active is panel.issued is None
    panel.active = deepcopy(state)
    panel.issued = {'revision': 1, 'text': 'first instruction'}
    assert panel.bind(session, 'first instruction', {'plan': 'exact'}, ['exact ref'], deepcopy(panel.issued))
    assert len(calls) == 1 and calls[0][1:4] == (state['id'], 0, session)
    monkeypatch.setattr(module.core, 'bind_preview', lambda *_: (_ for _ in ()).throw(ValueError('unsupported')))
    assert not panel.bind(session, 'first instruction', {}, [], panel.issued)
    assert not log.applied and not log.prepared


def automatic(tmp_path, monkeypatch):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    source = deepcopy(state)
    binding = {'session_root': str(tmp_path / 'computer'),
               'routed': {'plan': {'sha256': 'b' * 64, 'mode': 'capability'}}, 'refs': []}
    source.update(status='preview', binding=binding)
    panel.active = source
    panel.issued = {'revision': 1, 'text': 'first instruction'}
    output = {'path': str(tmp_path / 'new.xlsx'), 'sha256': 'c' * 64}
    state.update(index=1, scope='batch', payload={'draft': 'second instruction',
                                                'records': [output], 'source': None})
    context['workspace']['payload'] = {'draft': '', 'records': [output], 'source': output}
    context['workspace']['revision'] = {'revision': 2, 'text': ''}
    panel.instruction_cleared(deepcopy(panel.issued), True)
    panel.artifacts_announced([output])
    outcome = Path(binding['session_root']) / 'goals' / ('b' * 64 + '.capability') / 'outcome.json'
    outcome.parent.mkdir(parents=True)
    outcome.write_text('{}')  # Core proof verification is isolated in separate core tests.
    return panel, context, log, state, outcome


def test_exact_completed_stage_only_adopts_second_authored_input_once(tmp_path, monkeypatch):
    panel, context, log, state, _outcome = automatic(tmp_path, monkeypatch)
    panel.poll()
    assert panel.pending and not log.applied
    finish(panel)
    assert len(log.applied) == 1 and context['workspace']['payload'] == state['payload']
    assert log.scopes == ['当前成果批次'] and not log.prepared
    panel.poll()
    assert len(log.advanced) == 1 and len(Thread.all) == 1


@pytest.mark.parametrize('kind', ['wrong_ticket', 'not_cleared', 'main_new', 'main_revert',
                                  'goal_new', 'goal_revert', 'records', 'stop', 'mode',
                                  'mode_revert', 'goal_source_revert', 'plan', 'scope',
                                  'source', 'source_revert', 'selected'])
def test_auto_next_input_needs_bound_clear_same_empty_revision_and_exact_batch(tmp_path, monkeypatch, kind):
    panel, context, log, _state, _outcome = automatic(tmp_path, monkeypatch)
    if kind == 'wrong_ticket':
        panel.cleared = None
        panel.instruction_cleared({'revision': 99, 'text': 'first instruction'}, True)
    elif kind == 'not_cleared':
        panel.instruction_cleared(panel.issued, False)
    else:
        mutate(context, kind)
    before = deepcopy(context)
    panel.poll()
    if panel.pending:
        finish(panel)
    assert not log.applied and not log.prepared and context == before


@pytest.mark.parametrize('missing', ['clear', 'announcement', 'wrong_announcement'])
def test_auto_completion_without_exact_main_handoff_keeps_input(tmp_path, monkeypatch, missing):
    panel, context, log, _state, _outcome = automatic(tmp_path, monkeypatch)
    if missing == 'clear':
        panel.cleared = None
    elif missing == 'announcement':
        panel.handoff = None
    else:
        panel.artifacts_announced([{'path': 'unrelated.docx', 'sha256': 'd' * 64}])
    before = deepcopy(context)
    panel.poll()
    if panel.pending:
        finish(panel)
    assert context == before and not log.applied and not log.prepared


def test_unrelated_outcome_is_never_observed(tmp_path, monkeypatch):
    panel, _context, log, _state, outcome = automatic(tmp_path, monkeypatch)
    outcome.unlink()
    unrelated = outcome.parent.parent / ('d' * 64 + '.capability') / 'outcome.json'
    unrelated.parent.mkdir()
    unrelated.write_text('{}')
    panel.poll()
    assert not panel.pending and not log.advanced and not Thread.all


def main_functions(names, environment):
    if 'accept_goal_artifacts' in names:
        # The production closure also owns the explicit recovery completion ticket.
        environment.setdefault('recovery_submission', {'binding': None})
        environment.setdefault('goal_panel', None)
    if 'load_history' in names:
        environment.setdefault('mail_review_panel', SimpleNamespace(invalidate=lambda: None))
        environment.setdefault('mail_navigation_panel', SimpleNamespace(invalidate=lambda: None))
    tree = ast.parse((Path(__file__).parents[1] / 'run_jarvis_assistant.py').read_text(encoding='utf-8'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    chosen = [node for node in main.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in chosen} == set(names)
    exec(compile(ast.Module(body=chosen, type_ignores=[]), '<production-main-closures>', 'exec'), environment)
    return environment


def test_production_route_prepare_binds_before_adoption_and_rejects_failed_chain():
    from agent_service.office_task_entry import DEFERRED_ROUTE
    calls = []
    ticket = {'revision': 4, 'text': 'authored'}
    route = {'main': None, 'goal': None}
    panel = SimpleNamespace(instruction_draft=SimpleNamespace(snapshot=lambda: {'revision': 2, 'text': 'authored'}),
                            use_task_route=lambda *args: calls.append(('adopt', args)) or True, tab='goal-tab')
    accepted = {'result': False}
    bridge = SimpleNamespace(prepare=lambda *args: calls.append(('prepare', args)) or accepted['result'])
    env = main_functions(['office_route_prepare', 'office_route_adopt'], {
        'office_chain_route_binding': bridge,
        'goal_panel': panel, 'routed_instruction': route,
        'tabs': SimpleNamespace(select=lambda tab: calls.append(('select', tab)))})
    current = lambda: True
    args = ('session', 'authored', 'plan', ['ref'], current)
    assert env['office_route_prepare'](*args) is False
    assert [item[0] for item in calls] == ['prepare'] and route['main'] is None
    calls.clear()
    accepted['result'] = DEFERRED_ROUTE
    assert env['office_route_prepare'](*args) is DEFERRED_ROUTE
    assert calls == [('prepare', args)] and route['main'] is None
    calls.clear()
    panel.use_task_route = lambda *args: calls.append(('adopt', args)) or False
    assert env['office_route_adopt'](*args, ticket) is False
    assert [item[0] for item in calls] == ['adopt'] and route['main'] is None
    calls.clear()
    panel.use_task_route = lambda *args: calls.append(('adopt', args)) or True
    assert env['office_route_adopt'](*args, ticket) is True
    assert [item[0] for item in calls] == ['adopt', 'select']
    assert route['main'] == ticket and route['goal'] == panel.instruction_draft.snapshot()


@pytest.mark.parametrize('clear_success', [False, True])
def test_production_completion_notifies_chain_only_for_exact_routed_goal_ticket(clear_success):
    goal_ticket = {'revision': 2, 'text': 'authored'}
    main_ticket = {'revision': 4, 'text': 'authored'}
    routed = {'main': main_ticket, 'goal': goal_ticket}
    completed, notified = [], []
    env = main_functions(['goal_instruction_completed'], {
        'routed_instruction': routed,
        'complete_main_instruction': lambda ticket: completed.append(ticket) or clear_success,
        'office_chain': SimpleNamespace(instruction_cleared=lambda *args: notified.append(args))})
    env['goal_instruction_completed']({'revision': 3, 'text': 'authored'})
    assert not completed and not notified
    env['goal_instruction_completed'](deepcopy(goal_ticket))
    assert completed == [main_ticket] and notified == [(main_ticket, clear_success)]
    assert routed == {'main': None, 'goal': None}
    env['goal_instruction_completed'](goal_ticket)
    assert len(notified) == 1


def test_production_artifact_announcement_happens_after_same_batch_source_selection(tmp_path):
    calls = []
    outputs, hashes = {}, {}
    choice = {'selected': None}
    widget = SimpleNamespace(configure=lambda **kw: None)
    records = [{'path': str(tmp_path / 'one.docx'), 'sha256': 'a' * 64},
               {'path': str(tmp_path / 'two.xlsx'), 'sha256': 'b' * 64}]
    def select(origin):
        calls.append(('select', origin, choice['selected'], list(outputs.values())))
    env = main_functions(['accept_goal_artifacts'], {
        'active': {'kind': None}, 'source_choice': {'pending': False},
        '_verified_history_path': lambda path, digest, base: Path(path), 'TASK_ROOT': tmp_path,
        'agent_outputs': outputs, 'agent_output_hashes': hashes,
        'agent_output_list': widget,
        'agent_output_choice': SimpleNamespace(set=lambda value: choice.update(selected=value)),
        'last_paths': {}, 'task_open': widget, 'task_dir': widget,
        'select_generated_source': select, 'load_history': lambda: None,
        'office_chain_context': lambda: {'goal': {'source_generation': 7}},
        'office_chain': SimpleNamespace(artifacts_announced=lambda value, **kwargs:
                                        calls.append(('announced', value, kwargs)))})
    env['accept_goal_artifacts'](records)
    assert calls == [('select', 'current', '连续任务 2 · Excel', [Path(r['path']) for r in records]),
                     ('announced', records, {'history_before': {'source_generation': 7}})]


def test_production_chain_context_contains_complete_workspace_goal_and_route_scope():
    workspace = {'payload': {'records': []}, 'revision': {'revision': 1, 'text': ''},
                 'binding': {'source': {'generation': 2}}, 'unavailable': False}
    goal = SimpleNamespace(instruction_draft=SimpleNamespace(snapshot=lambda: {'revision': 3, 'text': ''}),
                           plan={'sha256': 'a' * 64}, mode=SimpleNamespace(get=lambda: 'original'),
                           daily_task_route_generation=4, capability_source_generation=5,
                           stopped=SimpleNamespace(is_set=lambda: False))
    env = main_functions(['office_chain_context'], {
        'workspace_context': lambda: workspace, 'goal_panel': goal,
        'office_workspace_panel': SimpleNamespace(context=lambda: {'payload': {'draft': '', 'records': [], 'scope': None}}),
        'route_scope': SimpleNamespace(get=lambda: '当前成果批次')})
    result = env['office_chain_context']()
    assert result == {'workspace': workspace, 'scope': '当前成果批次',
                      'goal': {'draft': {'revision': 3, 'text': ''}, 'plan': {'sha256': 'a' * 64},
                               'mode': 'original', 'mode_generation': 4, 'source_generation': 5,
                               'office': {'draft': '', 'records': [], 'scope': None},
                               'stopped': False}}
    result['workspace']['revision']['revision'] = 99
    assert workspace['revision']['revision'] == 1


@pytest.mark.parametrize('prior_user_change,legacy_lifecycle', [(False, False), (True, False), (False, True)])
def test_real_goal_completion_and_main_history_refresh_preserve_auto_guard(
        tmp_path, monkeypatch, prior_user_change, legacy_lifecycle,
        execution_kind='capability_execute', after_handoff=None, terminal=False,
        history_setup=None, expect_adopt=None):
    """Exercise real poll/complete/invalidate and main completion/artifact/history closures.

    Only rendering, native artifact verification and background source work are
    isolated. History invalidation and its generation mutation are production.
    """
    import queue
    import threading
    from agent_service.computer_goal_panel import GoalPanel, MODES
    from test_history_resume_gui import _Widget, _Variable, _FakeDraft
    panel, context, log, state, _outcome = automatic(tmp_path, monkeypatch)
    prior_apply_count = len(log.applied)
    if terminal:
        state.update(index=2, status='complete')
        state['payload']['draft'] = ''
        panel.active['index'] = 1
        panel.active['binding']['routed']['plan']['mode'] = 'office_bundle'
        final_outcome = _outcome.parent.with_suffix('.bundle') / 'outcome.json'
        final_outcome.parent.mkdir()
        final_outcome.write_text('{}')
    if legacy_lifecycle:
        announce = panel.artifacts_announced
        # Reproduce the original lifecycle: no bracket around load_history.
        panel.artifacts_announced = lambda records, **kwargs: announce(records)
    panel.cleared = panel.handoff = None
    goal = GoalPanel.__new__(GoalPanel)
    goal.text = _Widget(wrap='word')
    goal.text.edit_modified = lambda *args: False
    goal.text.insert('1.0', 'first instruction')
    goal.instruction_draft = _FakeDraft(goal.text)
    main_text = _Widget(wrap='word')
    main_text.insert('1.0', 'first instruction')
    main_draft = _FakeDraft(main_text)
    panel.issued = main_draft.snapshot()
    goal.mode = _Variable(MODES[11])
    goal.plan = {'mode': 'capability', 'sha256': 'b' * 64}
    goal.daily_task_route_generation = 0
    goal.capability_source_generation = 0
    goal.capability_history_seed = ({'task_id': '本次任务规划'}
                                    if execution_kind in ('capability_execute', 'office_bundle_execute') else None)
    goal.capability_sources = []
    goal.stopped = threading.Event()
    goal.closed, goal.running, goal.external_activity_seen = False, True, False
    goal.poll_id = None
    goal.root = SimpleNamespace(after=lambda *args: None)
    goal.external_busy = lambda: False
    goal.set_busy = lambda value: setattr(goal, 'running', value)
    goal.show_text = lambda text: None
    goal._update_pause_checkbox = lambda: None
    goal.status = _Variable('')
    goal.execute_button = goal.office_bundle_button = _Widget()
    goal.control = SimpleNamespace(configure=lambda **kw: None, current=lambda *args: None, set=lambda *args: None)
    goal.messages = queue.Queue()
    def workspace_context():
        value = deepcopy(context['workspace'])
        value['revision'] = main_draft.snapshot()
        value['payload']['draft'] = main_text.get('1.0', 'end-1c')
        return value
    common = {'workspace_context': workspace_context, 'goal_panel': goal,
              'office_workspace_panel': SimpleNamespace(context=lambda: {'payload': {'draft': '', 'records': [], 'scope': None}}),
              'route_scope': _Variable('当前选中资料')}
    env = main_functions(['office_chain_context'], common)
    panel.context = env['office_chain_context']
    completion_env = main_functions(['complete_main_instruction', 'goal_instruction_completed'], {
        'task_draft': main_draft, 'instruction_state': _Variable(), 'office_chain': panel,
        'routed_instruction': {'main': main_draft.snapshot(), 'goal': goal.instruction_draft.snapshot()},
    })
    goal.on_instruction_complete = completion_env['goal_instruction_completed']
    widget = SimpleNamespace(configure=lambda **kwargs: None, set=lambda *args: None)
    history_environment = {
        'invalidate_history_batch_source': goal.invalidate_history_batch_source,
        'mail_followup_state': {'generation': 0}, 'history_followup_button': widget,
        'history_scan': {'generation': 0}, 'gap_only': SimpleNamespace(get=lambda: False),
        'history_older': SimpleNamespace(get=lambda: False),
        'active': {'kind': None}, 'history_selected': {'generation': 0},
        'gap_state': {'generation': 0}, 'history_resume': widget, 'history_skill_resume': widget,
        'history_bundle_resume': widget, 'history_batch_button': widget, 'history_prepare': widget,
        'history_gap_cancel': widget, 'history_detail': widget, 'history_candidate_detail': widget,
        'threading': SimpleNamespace(Thread=Thread), 'TASK_ROOT': tmp_path,
        'history_messages': queue.Queue()}
    if history_setup is not None:
        history_setup(history_environment)
    history_env = main_functions(['load_history'], history_environment)
    source = context['workspace']['binding']['source']
    outputs, hashes = {}, {}
    selected = {'value': None}
    def select_source(origin):
        # Model only the completed, matching asynchronous source result. Its
        # production select path increments the main source generation once.
        source['generation'] += 1
        context['workspace']['binding']['selected'] = selected['value']
        context['workspace']['payload']['records'] = deepcopy(state['payload']['records'])
        context['workspace']['payload']['source'] = deepcopy(state['payload']['records'][-1])
        if prior_user_change:
            goal.capability_source_generation += 2
    artifact_env = main_functions(['accept_goal_artifacts'], {
        'active': {'kind': None}, 'source_choice': {'pending': False},
        '_verified_history_path': lambda path, digest, base: Path(path), 'TASK_ROOT': tmp_path,
        'agent_outputs': outputs, 'agent_output_hashes': hashes, 'agent_output_list': widget,
        'agent_output_choice': SimpleNamespace(set=lambda value: selected.update(value=value)),
        'last_paths': {}, 'task_open': widget, 'task_dir': widget,
        'select_generated_source': select_source, 'load_history': history_env['load_history'],
        'office_chain_context': env['office_chain_context'], 'office_chain': panel})
    goal.on_artifacts = artifact_env['accept_goal_artifacts']
    result = {'ok': True, 'state': 'succeeded', 'code': 'capability_verified',
              'run': 'saved-run', 'artifacts': deepcopy(state['payload']['records'])}
    if execution_kind == 'execute':
        result.update(state='verified', code='objective_verified', mode='adapters',
                      engine='observed_agent', goal_kind='interactive', completed=[], failures=[])
        result['artifacts'] = [{**record, 'kind': 'spreadsheet', 'provenance': 'office_edit'}
                               for record in result['artifacts']]
    elif execution_kind == 'office_bundle_execute':
        result.update(positions=30, differences=0)
    goal.messages.put((execution_kind, result, None, goal.instruction_draft.snapshot()))
    goal.poll()
    assert main_draft.snapshot()['text'] == goal.instruction_draft.snapshot()['text'] == ''
    assert goal.plan is None
    assert (panel.handoff is None) is prior_user_change
    history_increment = int(execution_kind in ('capability_execute', 'office_bundle_execute'))
    assert goal.capability_source_generation == history_increment + (2 if prior_user_change else 0)
    assert panel.cleared['goal']['source_generation'] == (0 if prior_user_change or legacy_lifecycle else history_increment)
    if after_handoff is not None:
        after_handoff(panel, context, goal, main_draft)
    before_adoption = deepcopy(panel.context())
    panel.poll()
    finish(panel)
    adopted = not prior_user_change and not legacy_lifecycle and after_handoff is None
    if expect_adopt is not None:
        adopted = expect_adopt
    assert (len(log.applied) > prior_apply_count) is adopted
    if adopted:
        assert log.applied[-1]['payload'] == state['payload']
        if terminal:
            assert panel.active['status'] == 'complete' and not panel.active.get('binding')
            assert '全部步骤已完成并核验' in log.statuses[-1]
    else:
        assert panel.context() == before_adoption


@pytest.mark.parametrize('delta', [-1, 0, 1, 2])
@pytest.mark.parametrize('changed_before_history', [False, True])
def test_history_refresh_absorbs_only_its_own_zero_or_one_source_invalidation(
        tmp_path, monkeypatch, delta, changed_before_history):
    panel, context, log, state, _outcome = automatic(tmp_path, monkeypatch)
    if changed_before_history:
        context['goal']['source_generation'] += 1
    before = deepcopy(context['goal'])
    context['goal']['source_generation'] += delta
    panel.artifacts_announced(state['payload']['records'], history_before=before)
    panel.poll()
    finish(panel)
    assert bool(log.applied) is (not changed_before_history and delta in (0, 1))
