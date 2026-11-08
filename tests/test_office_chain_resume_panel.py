"""Independent exact-attempt chain continuation UI and main wiring checks."""
from copy import deepcopy
import ast
import os
from pathlib import Path
import queue
import re
import subprocess
from types import SimpleNamespace
import time
import uuid

import pytest

from agent_service import office_task_chain_panel as module
from test_office_task_chain_panel import harness, finish, mutate, main_functions, Thread
from test_history_resume_gui import _Widget, _Variable, _FakeDraft
from test_computer_goal_panel import hidden_root
from test_office_task_chain_mail import local as real_mail_chain


def resume_harness(tmp_path, monkeypatch, resume_kind='office_resume'):
    from tkinter import ttk
    from agent_service import office_scope_panel
    chain, context, log, state = harness(tmp_path, monkeypatch)
    context['goal']['office'] = {'draft': '', 'records': [], 'scope': None}
    task = tmp_path / 'tasks' / ('task-' + 'b' * 32)
    state.update(status='resume_available', task_root=str(task), resume_original_state='cancelled',
                 resume_preview={'eligible': True, 'reason': 'ready', 'kind': resume_kind,
                     'task_root': str(task), 'workflow_root': str(task / ('workflow-' + 'c' * 32)),
                     'completed': 1, 'total': 2, 'remaining': 1, 'token': 'd' * 64},
                 binding={'session_root': str(tmp_path / 'computer'),
                     'routed': {'plan': {'mode': 'capability', 'sha256': 'e' * 64}}, 'refs': []},
                 reason='只继续剩余步骤')
    log.widgets, log.closed, log.started, log.cleared = [], [], [], []
    log.attempt = 'f' * 32
    class Widget:
        def __init__(self, *args, **options):
            self.options = options
            log.widgets.append(self)
        def pack(self, **kwargs):
            pass
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    monkeypatch.setattr(office_scope_panel, '_modal',
                        lambda *args: (object(), lambda: log.closed.append(True)))
    def start(saved):
        log.started.append(deepcopy(saved))
        # Production launch owns this synchronous invalidation, before return.
        context['goal']['source_generation'] += 1
        return log.attempt
    chain.start_resume = start
    def clear(ticket):
        log.cleared.append(deepcopy(ticket))
        if ticket != context['goal']['draft'] or context['goal']['plan'] is not None:
            return False
        context['goal']['draft'] = {'revision': ticket['revision'] + 1, 'text': ''}
        return True
    chain.clear_resume_goal = clear
    def advance(store, ident, **kwargs):
        log.advanced.append((store, ident, deepcopy(kwargs)))
        return deepcopy(state)
    monkeypatch.setattr(module.core, 'advance', advance)
    return chain, context, log, state


def confirm(log):
    return next(widget.options['command'] for widget in reversed(log.widgets)
                if widget.options.get('text') == '核对后继续剩余步骤')


def open_resume(chain, log, state):
    assert chain.restore(state['id'])
    finish(chain)
    assert chain.pending is None
    assert not log.started
    return confirm(log)


def next_stage(state):
    payload = {'draft': 'second authored requirement', 'source': None,
               'records': [{'path': str(Path(state['task_root']) / 'final.xlsx'), 'sha256': '1' * 64},
                           {'path': str(Path(state['task_root']) / 'final.docx'), 'sha256': '2' * 64}]}
    state.update(index=state['index'] + 1, status='ready', scope='batch', binding=None,
                 payload=payload, reason='全部成果已核验')


def test_restore_resume_preview_requires_explicit_single_confirmation_and_does_not_observe_old_outcome(tmp_path, monkeypatch):
    chain, _context, log, state = resume_harness(tmp_path, monkeypatch)
    outcome = Path(state['binding']['session_root']) / 'goals' / ('e' * 64 + '.capability') / 'outcome.json'
    outcome.parent.mkdir(parents=True)
    outcome.write_text('{}')
    accept = open_resume(chain, log, state)
    assert len(Thread.all) == 1 and not log.prepared
    chain.poll()
    assert len(Thread.all) == 1 and chain.pending is None
    label = next(widget.options['text'] for widget in log.widgets if '已有' in widget.options.get('text', ''))
    assert '1/2' in label and '剩余 1 步' in label and '不再调用模型' in label
    accept(); accept()
    assert log.started == [state] and log.closed == [True]
    assert chain.resume_owned['attempt'] == log.attempt
    assert chain.resume_owned['context']['goal']['source_generation'] == 1


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'goal_revert',
                                  'source', 'source_revert', 'records', 'scope', 'mode',
                                  'mode_revert', 'goal_source_revert', 'stop', 'plan', 'unavailable'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_confirmation_rejects_complete_context_changes_without_launch(tmp_path, monkeypatch, change, resume_kind):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    accept = open_resume(chain, log, state)
    mutate(context, change)
    before = deepcopy(context)
    accept()
    assert context == before and not log.started and not log.closed and chain.resume_owned is None


@pytest.mark.parametrize('change', ['busy', 'pending', 'cancel', 'close'])
def test_confirmation_rejects_busy_cancelled_or_closed_dialog(tmp_path, monkeypatch, change):
    chain, _context, log, state = resume_harness(tmp_path, monkeypatch)
    accept = open_resume(chain, log, state)
    if change == 'busy':
        log.external = True
    elif change == 'pending':
        assert chain._begin(lambda: deepcopy(state))
    else:
        getattr(chain, change)()
    accept()
    assert not log.started


@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_start_failure_is_single_use_and_keeps_original_inputs(tmp_path, monkeypatch, resume_kind):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    chain.start_resume = lambda value: log.started.append(value) or None
    accept = open_resume(chain, log, state)
    before = deepcopy(context)
    accept(); accept()
    assert len(log.started) == 1 and not log.closed and chain.resume_owned is None
    assert context == before


def test_new_resume_dialog_supersedes_old_confirmation_even_with_identical_context(tmp_path, monkeypatch):
    chain, _context, log, state = resume_harness(tmp_path, monkeypatch)
    first = open_resume(chain, log, state)
    assert chain.resume_dialog(state)
    second = confirm(log)
    first()
    assert not log.started
    second()
    assert len(log.started) == 1


def owned_pause(chain, context, state):
    context['workspace']['payload'] = deepcopy(state['payload'])
    context['workspace']['revision'] = {'revision': 4, 'text': state['payload']['draft']}
    context['goal']['draft'] = {'revision': 7, 'text': state['payload']['draft']}
    chain.issued = deepcopy(context['workspace']['revision'])
    chain.route_context = {key: deepcopy(context[key]) for key in ('workspace', 'scope')}
    chain.active = deepcopy(state)


def test_owned_pause_can_reopen_confirmation_without_replacing_existing_original_drafts(tmp_path, monkeypatch):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    owned_pause(chain, context, state)
    # The first confirmation was closed by X: no approval callback occurred.
    assert chain.resume_dialog(state)
    before = deepcopy(context)
    assert chain.restore(state['id'])
    assert chain.pending['auto'] is True
    finish(chain)
    assert context == before and not log.applied and not log.started
    assert len([widget for widget in log.widgets if widget.options.get('text') == '核对后继续剩余步骤']) == 2
    confirm(log)()
    assert len(log.started) == 1 and chain.resume_owned is not None


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'goal_new', 'source_revert',
                                  'source', 'records', 'scope', 'unavailable', 'other_chain', 'not_paused', 'no_binding'])
def test_owned_pause_restore_does_not_bypass_new_source_or_draft_protection(tmp_path, monkeypatch, change):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    owned_pause(chain, context, state)
    ident = state['id']
    if change == 'other_chain':
        ident = '9' * 32
    elif change == 'not_paused':
        chain.active['status'] = 'blocked'
    elif change == 'no_binding':
        chain.route_context = None
    else:
        mutate(context, change)
    before = deepcopy(context)
    assert not chain.restore(ident)
    assert context == before and not Thread.all and not log.applied and not log.started


def test_fixed_attempt_completion_revalidates_core_and_adopts_all_next_inputs(tmp_path, monkeypatch):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    next_stage(state)
    assert chain.resume_finished(owned['task_root'], owned['attempt'])
    assert chain.resume_owned is None and chain.pending['resuming'] == owned
    finish(chain)
    assert log.advanced[-1][2] == {'resume_attempt': owned['attempt'], 'resume_index': owned['index']}
    assert len(log.cleared) == 1 and context['workspace']['payload'] == state['payload']
    assert context['goal']['draft']['text'] == '' and log.scopes[-1] == '当前成果批次'
    assert not log.prepared
    assert not chain.resume_finished(owned['task_root'], owned['attempt'])


@pytest.mark.parametrize('mismatch', ['task', 'attempt'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_unrelated_completion_cannot_consume_owned_attempt(tmp_path, monkeypatch, mismatch, resume_kind):
    chain, _context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    task, attempt = owned['task_root'], owned['attempt']
    if mismatch == 'task':
        task += '-other'
    else:
        attempt = '9' * 32
    assert not chain.resume_finished(task, attempt)
    assert chain.resume_owned == owned and chain.pending is None and not log.cleared


@pytest.mark.parametrize('change', ['main_new', 'main_revert', 'source_revert', 'mode_revert',
                                  'goal_new', 'goal_revert', 'scope', 'stop', 'plan', 'office',
                                  'cancel', 'close', 'stopped_argument'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_completed_resume_preserves_user_changes_and_consumes_exact_attempt(tmp_path, monkeypatch, change, resume_kind):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    if change in ('cancel', 'close'):
        getattr(chain, change)()
    elif change == 'office':
        context['goal']['office']['scope'] = [{'different': True}]
    elif change != 'stopped_argument':
        mutate(context, change)
    before = deepcopy(context)
    assert chain.resume_finished(owned['task_root'], owned['attempt'], stopped=change == 'stopped_argument')
    assert chain.resume_owned is None and chain.pending is None
    assert context == before and not log.cleared
    assert len(log.applied) == 1  # only explicit restore input, no resumed output


@pytest.mark.parametrize('change', ['main_revert', 'goal_revert', 'source_revert', 'scope', 'mode_revert', 'office'])
def test_late_core_resume_verification_never_clears_or_overwrites_changed_context(tmp_path, monkeypatch, change):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    next_stage(state)
    assert chain.resume_finished(owned['task_root'], owned['attempt'])
    if change == 'office':
        context['goal']['office']['records'] = [{'new': 'source'}]
    else:
        mutate(context, change)
    before = deepcopy(context)
    finish(chain)
    assert context == before and not log.cleared and len(log.applied) == 1


@pytest.mark.parametrize('interruption', ['cancel', 'close', 'timeout'])
def test_resume_result_verification_keeps_busy_until_worker_exits_after_interruption(tmp_path, monkeypatch, interruption):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    next_stage(state)
    assert chain.resume_finished(owned['task_root'], owned['attempt'])
    if interruption == 'timeout':
        log.now += 41
        chain.poll()
    else:
        getattr(chain, interruption)()
    assert chain.pending['discard'] and log.busy[-1] is True
    before = deepcopy(context)
    finish(chain)
    assert chain.pending is None and log.busy[-1] is False
    assert context == before and not log.cleared and len(log.applied) == 1


@pytest.mark.parametrize('result', ['blocked', 'wrong_chain', 'wrong_index', 'proof_failure', 'clear_failure'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_noncompletion_or_unverified_attempt_never_clears_original_input(tmp_path, monkeypatch, result, resume_kind):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    next_stage(state)
    if result == 'blocked':
        state['status'] = 'blocked'
    elif result == 'wrong_chain':
        state['id'] = '8' * 32
    elif result == 'wrong_index':
        state['index'] += 1
    elif result == 'proof_failure':
        monkeypatch.setattr(module.core, 'advance', lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('bad proof')))
    else:
        chain.clear_resume_goal = lambda ticket: False
    before = deepcopy(context)
    assert chain.resume_finished(owned['task_root'], owned['attempt'])
    finish(chain)
    assert context == before and len(log.applied) == 1


def test_main_clear_resume_goal_accepts_only_current_unplanned_goal_ticket():
    text = _Widget(wrap='word')
    text.insert('1.0', 'original paused instruction')
    tracker = _FakeDraft(text)
    selected = []
    routed = {'main': {'bound': True}, 'goal': {'bound': True}}
    goal = SimpleNamespace(instruction_draft=tracker, plan=None)
    env = main_functions(['office_chain_clear_resume_goal'], {
        'goal_panel': goal, 'routed_instruction': routed, 'task_tab': 'main-tab',
        'tabs': SimpleNamespace(select=selected.append)})
    clear = env['office_chain_clear_resume_goal']
    ticket = tracker.snapshot()
    assert not clear({**ticket, 'revision': ticket['revision'] + 1})
    goal.plan = {'other': 'preview'}
    assert not clear(ticket)
    goal.plan = None
    assert clear(ticket) and tracker.snapshot()['text'] == ''
    assert routed == {'main': None, 'goal': None} and selected == ['main-tab']
    assert clear(tracker.snapshot())  # restoring a chain preview had an empty goal draft


@pytest.mark.parametrize('original_state', ['cancelled', 'failed', 'timed_out'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_main_resume_launcher_uses_exact_chain_task_preview_and_existing_resume_entry(tmp_path, original_state, resume_kind):
    base = tmp_path / 'tasks'
    base.mkdir()
    task = base / ('task-' + 'a' * 32)
    task.mkdir()
    selected, calls, inserted = [], [], []
    goal = SimpleNamespace(running=False)
    history_records, history_selected = {}, {'id': 'unrelated', 'inspection': {'other': True}}
    env = main_functions(['office_chain_start_resume'], {
        'Path': Path, 'TASK_ROOT': base, 'active': {'kind': None}, 'source_choice': {'pending': False},
        'goal_panel': goal, 'history_records': history_records, 'history_selected': history_selected,
        'history_list': SimpleNamespace(exists=lambda key: False,
            insert=lambda *args, **kwargs: inserted.append((args, kwargs)), selection_set=selected.append),
        'history_tab': 'history-tab', 'tabs': SimpleNamespace(select=lambda tab: calls.append(('tab', tab))),
        'start_resume': lambda **kwargs: calls.append(('start', kwargs)) or 'b' * 32})
    preview = {'task_root': str(task), 'eligible': True, 'remaining': 1, 'token': 'c' * 64, 'kind': resume_kind}
    assert env['office_chain_start_resume']({'task_root': str(task), 'resume_preview': preview,
                                            'resume_original_state': original_state}) == 'b' * 32
    assert history_selected['id'] == task.name and history_selected['inspection']['resume'] == preview
    assert history_records[task.name]['state'] == history_selected['inspection']['original_state'] == original_state
    assert selected == [task.name] and len(inserted) == 1
    assert calls == [('tab', 'history-tab'), ('start', {'resume_kind': resume_kind})]


@pytest.mark.parametrize('blocked', ['active', 'source_pending', 'goal_running', 'outside_task', 'wrong_preview', 'not_eligible', 'no_remaining'])
@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
def test_main_resume_launcher_rejects_unready_or_unrelated_task_without_changing_history(tmp_path, blocked, resume_kind):
    base = tmp_path / 'tasks'
    task = base / ('task-' + 'a' * 32)
    env = {'Path': Path, 'TASK_ROOT': base, 'active': {'kind': None}, 'source_choice': {'pending': False},
           'goal_panel': SimpleNamespace(running=False), 'history_records': {}, 'history_selected': {},
           'start_resume': lambda **kwargs: pytest.fail('must not launch')}
    state = {'task_root': str(task), 'resume_preview': {'task_root': str(task), 'eligible': True, 'remaining': 1, 'kind': resume_kind}}
    if blocked == 'active': env['active']['kind'] = 'another-task'
    elif blocked == 'source_pending': env['source_choice']['pending'] = True
    elif blocked == 'goal_running': env['goal_panel'].running = True
    elif blocked == 'outside_task': state['task_root'] = str(tmp_path / task.name)
    elif blocked == 'wrong_preview': state['resume_preview']['task_root'] += '-other'
    elif blocked == 'not_eligible': state['resume_preview']['eligible'] = False
    else: state['resume_preview']['remaining'] = 0
    launcher = main_functions(['office_chain_start_resume'], env)['office_chain_start_resume']
    assert launcher(state) is None
    assert env['history_selected'] == env['history_records'] == {}


@pytest.mark.parametrize('kind', [None, '', 'skill_resume', 'office_bundle_resume',
                                'bundle_resume_other', [], {}, 1])
def test_chain_resume_rejects_unknown_kind_before_mutating_existing_history(tmp_path, kind):
    task = tmp_path / ('task-' + 'a' * 32)
    records = {'old-task': {'root': 'preserved'}}
    selected = {'id': 'old-task', 'inspection': {'resume': 'preserved'}}
    before = deepcopy((records, selected))
    env = main_functions(['office_chain_start_resume'], {
        'Path': Path, 'TASK_ROOT': tmp_path, 'active': {'kind': None},
        'source_choice': {'pending': False}, 'goal_panel': SimpleNamespace(running=False),
        'history_records': records, 'history_selected': selected,
        'start_resume': lambda **kwargs: pytest.fail('unknown kind must not launch')})
    state = {'task_root': str(task), 'resume_preview': {'task_root': str(task),
             'kind': kind, 'eligible': True, 'remaining': 1, 'token': 'c' * 64}}
    assert env['office_chain_start_resume'](state) is None
    assert (records, selected) == before


@pytest.mark.parametrize('resume_kind', ['office_resume', 'bundle_resume'])
@pytest.mark.parametrize('token_case', ['valid', 'missing', 'malformed'])
def test_real_main_start_resume_and_launch_capture_only_their_own_goal_invalidation(tmp_path, monkeypatch, resume_kind, token_case):
    from agent_service.computer_goal_panel import GoalPanel, MODES
    import run_jarvis_assistant as launcher
    chain, context, log, state = resume_harness(tmp_path, monkeypatch, resume_kind)
    base = Path(state['task_root']).parent
    Path(state['task_root']).mkdir(parents=True)
    goal = GoalPanel.__new__(GoalPanel)
    goal.running = goal.closed = False
    goal.external_activity_seen = False
    goal.capability_source_generation = 0
    goal.capability_sources = ['previous source']
    goal.mode = _Variable(MODES[11])
    goal.control = SimpleNamespace(configure=lambda **kwargs: None, current=lambda value: None)
    goal.execute_button = _Widget(state='normal')
    goal.office_bundle_button = _Widget(state='disabled')
    goal.external_busy = lambda: active['kind'] is not None
    goal.plan = {'old': 'preview'}
    goal.status = _Variable('')
    goal._update_pause_checkbox = lambda: None
    goal.show_text = lambda value: None
    widget = SimpleNamespace(configure=lambda **kwargs: None)
    active = {'kind': None}
    env = {'Path': Path, 'TASK_ROOT': base, 'ROOT': tmp_path, 'active': active,
           'source_choice': {'pending': False}, 'goal_panel': goal,
           'history_records': {}, 'history_selected': {'generation': 0}, 'history_scan': {'generation': 0},
           'history_list': SimpleNamespace(exists=lambda key: True, selection_set=lambda key: None),
           'history_tab': 'history', 'tabs': SimpleNamespace(select=lambda tab: None),
           'history_detail': _Variable(''), 'update_mail_understanding_button': lambda: None,
           'mail_followup_state': {'generation': 0}, 'threading': SimpleNamespace(Thread=Thread),
           'time': time, 'os': os, 'subprocess': subprocess, 'messages': queue.Queue(), 're': re,
           'uuid': uuid, '_resume_command': launcher._resume_command,
           '_skill_resume_command': launcher._skill_resume_command, '_bundle_resume_command': launcher._bundle_resume_command}
    for name in ('note_start', 'research_start', 'task_start', 'source_pick', 'source_entry', 'source_clear',
                 'research_stop', 'task_stop', 'history_resume', 'history_skill_resume', 'history_bundle_resume',
                 'history_followup_button', 'history_prepare', 'history_stop'):
        env[name] = widget
    env = main_functions(['invalidate_mail_followup_pending', 'notify_goal_external_activity', 'launch',
                          'start_resume', 'office_chain_start_resume'], env)
    original_context = chain.context
    def current():
        result = original_context()
        result['goal']['source_generation'] = goal.capability_source_generation
        result['goal']['plan'] = deepcopy(goal.plan)
        return result
    chain.context = current
    chain.start_resume = env['office_chain_start_resume']
    # Explicit history restore starts with an empty goal and no old approval.
    goal.plan = None
    if token_case == 'missing':
        del state['resume_preview']['token']
    elif token_case == 'malformed':
        state['resume_preview']['token'] = 'changed-token'
    open_resume(chain, log, state)()
    if token_case != 'valid':
        assert active['kind'] is None and chain.resume_owned is None
        assert goal.capability_source_generation == 0 and goal.plan is None
        assert not list(Path(state['task_root']).glob('resume-*'))
        return
    assert goal.capability_source_generation == 1 and goal.capability_sources == [] and goal.plan is None
    assert active['kind'] == 'resume' and active['attempt_id'] == chain.resume_owned['attempt']
    assert Path(active['attempt_root']).is_dir() is (resume_kind == 'office_resume')
    assert active['resume_kind'] == resume_kind
    assert chain.resume_owned['context'] == current()
    assert Thread.all[-1].name == 'jarvis-resume'
    # Do not run the child process; verify its exact existing CLI command via
    # the captured active attempt and the production command builder.
    command = (launcher._resume_command if resume_kind == 'office_resume' else launcher._bundle_resume_command)(
        Path(state['task_root']), state['resume_preview']['token'], active['attempt_id'])
    assert Path(command[1]).name == 'run_jarvis_resume.py'
    assert ('--bundle' in command) is (resume_kind == 'bundle_resume')
    assert command[command.index('--attempt-id') + 1] == chain.resume_owned['attempt']
    active['kind'] = None
    next_stage(state)
    assert chain.resume_finished(state['task_root'], active['attempt_id'])
    finish(chain)
    assert context['workspace']['payload'] == state['payload']


def resume_result_branch(environment):
    """Compile the actual resume event body, retaining real continue guards."""
    source = (Path(__file__).parents[1] / 'run_jarvis_assistant.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "event in {'resume_final', 'resume_final_error'}")
    loop = ast.For(target=ast.Name(id='_one_event', ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(value=None)], ctx=ast.Load()), body=node.body, orelse=[])
    program = ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[]))
    exec(compile(program, '<production-main-resume-result>', 'exec'), environment)


@pytest.mark.parametrize('event,interrupted', [('resume_final', None), ('resume_final', 'stopped'),
                                             ('resume_final', 'timed_out'), ('resume_final_error', None)])
def test_real_main_result_branch_consumes_owned_attempt_before_generic_latest_history(tmp_path, monkeypatch, event, interrupted):
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    next_stage(state)
    data = {'inspection': {'continuation': {'attempt_id': '9' * 32, 'state': 'succeeded'}},
            'outcome': {'attempt_id': owned['attempt'], 'ok': True, 'state': 'succeeded'},
            'stopped': interrupted == 'stopped', 'timed_out': interrupted == 'timed_out'}
    if event == 'resume_final_error':
        data = 'reader_error'  # Must not call .get on an error's string payload.
    widget = SimpleNamespace(configure=lambda **kwargs: None)
    active = {'kind': 'resume_verifying', 'history_id': Path(owned['task_root']).name, 'attempt_id': owned['attempt']}
    env = {'active': active, 'task_id': (active['history_id'], active['attempt_id']),
           'data': data, 'event': event, 'TASK_ROOT': Path(owned['task_root']).parent,
           'office_chain': chain, 'source_choice': {'path': ''},
           'show_history_inspection': lambda value: pytest.fail('generic latest continuation must not gate the owned chain'),
           'history_detail': _Variable('')}
    for name in ('note_start', 'research_start', 'task_start', 'source_pick', 'source_entry', 'source_clear',
                 'history_stop', 'history_resume', 'history_skill_resume', 'history_bundle_resume'):
        env[name] = widget
    resume_result_branch(env)
    assert chain.resume_owned is None and active['kind'] is None
    if event == 'resume_final' and interrupted is None:
        assert chain.pending is not None
        finish(chain)
        assert context['workspace']['payload'] == state['payload']
    else:
        assert chain.pending is None and len(log.applied) == 1 and not log.cleared


def test_real_main_result_branch_drops_unrelated_event_without_consuming_ownership(tmp_path, monkeypatch):
    chain, _context, log, state = resume_harness(tmp_path, monkeypatch)
    open_resume(chain, log, state)()
    owned = deepcopy(chain.resume_owned)
    active = {'kind': 'resume_verifying', 'history_id': Path(owned['task_root']).name, 'attempt_id': owned['attempt']}
    resume_result_branch({'active': active, 'task_id': (active['history_id'], '9' * 32)})
    assert chain.resume_owned == owned and chain.pending is None


def history_list_result(environment):
    """Run the production async list branch, including real Treeview deletion."""
    source = (Path(__file__).parents[1] / 'run_jarvis_assistant.py').read_text(encoding='utf-8')
    node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.If)
                and ast.unparse(node.test) == "event == 'list'")
    loop = ast.For(target=ast.Name(id='_one_event', ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(None)], ctx=ast.Load()), body=node.body, orelse=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                 '<production-main-history-list>', 'exec'), environment)


def test_real_mail_chain_projects_plain_office_preview_into_strict_main_dispatch(real_mail_chain):
    """Use real paused mail receipts and production preview, never invent kind."""
    from agent_service import office_resume
    from test_office_task_chain_mail import paused
    import run_jarvis_assistant as launcher
    s = real_mail_chain
    _original, _client, state = paused(s)
    task = Path(state['task_root'])
    raw_preview = office_resume.preview_resume(task, s.base)
    assert raw_preview['eligible'] is True and raw_preview['remaining'] == 1
    assert 'kind' not in raw_preview  # unchanged public Office recovery contract
    protected = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                 for folder in (task, s.store) for p in folder.rglob('*') if p.is_file()}
    calls = []
    widget = _Widget()
    env = {
        'Path': Path, 'TASK_ROOT': s.base, 'active': {'kind': None},
        'source_choice': {'pending': False}, 'goal_panel': SimpleNamespace(running=False),
        'history_records': {}, 'history_selected': {'generation': 0}, 'history_scan': {'generation': 0},
        'history_list': SimpleNamespace(exists=lambda key: False,
            insert=lambda *a, **kw: None, selection_set=lambda value: None),
        'tabs': SimpleNamespace(select=lambda tab: None), 'history_tab': 'history',
        'history_resume': widget, 'history_skill_resume': widget, 'history_bundle_resume': widget,
        'history_detail': _Variable(''), 're': re, 'uuid': uuid,
        '_resume_command': launcher._resume_command,
        '_bundle_resume_command': launcher._bundle_resume_command,
        '_skill_resume_command': launcher._skill_resume_command,
        'launch': lambda *a, **kw: calls.append((a, kw))}
    env = main_functions(['office_chain_start_resume', 'start_resume'], env)
    attempt = env['office_chain_start_resume'](state)
    assert attempt is not None and len(calls) == 1
    assert state['resume_preview'] == {**raw_preview, 'kind': 'office_resume'}
    args, options = calls[0]
    assert args[0] == 'resume' and args[1] == launcher._resume_command(task, raw_preview['token'], attempt)
    assert '--bundle' not in args[1] and options['resume_kind'] == 'office_resume'
    assert options['task_root'] == task and options['attempt_id'] == attempt
    assert env['history_selected']['inspection']['original_state'] == state['resume_original_state'] == 'cancelled'
    assert 'kind' not in office_resume.preview_resume(task, s.base)
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in protected.items())


@pytest.mark.parametrize('change', ['none', 'legacy_empty_event', 'manual_deselect',
                                  'selection', 'selection_return', 'new_draft',
                                  'new_source', 'source_return', 'stop'])
def test_resumed_chain_then_normal_bundle_completion_handles_real_history_selection_events(
        tmp_path, monkeypatch, hidden_root, change):
    """Exact lifecycle: resume selects old task, then bundle refresh deletes it.

    Real GoalPanel poll/completion, main closures, and hidden Tk virtual events
    run locally. Only Office byte verification and worker results are fixtures.
    The legacy arm removes only the new empty-event guard to prove the old bug.
    """
    from tkinter import ttk
    import test_office_task_chain_panel as anchor
    chain, context, log, state = resume_harness(tmp_path, monkeypatch)
    tree = ttk.Treeview(hidden_root)
    active = {'kind': None}
    selected = {'generation': 0, 'id': None, 'inspection': None}
    records = {}
    task_id = Path(state['task_root']).name
    launcher_env = main_functions(['office_chain_start_resume'], {
        'Path': Path, 'TASK_ROOT': Path(state['task_root']).parent, 'active': active,
        'source_choice': {'pending': False}, 'goal_panel': SimpleNamespace(running=False),
        'history_selected': selected, 'history_records': records, 'history_list': tree,
        'tabs': SimpleNamespace(select=lambda tab: None), 'history_tab': 'history',
        'start_resume': lambda **kwargs: log.attempt})
    chain.start_resume = launcher_env['office_chain_start_resume']
    open_resume(chain, log, state)()
    assert tree.selection() == (task_id,) and selected['inspection']['resume']['remaining'] == 1
    hidden_root.update()  # flush original selection before installing the later handler
    next_stage(state)
    assert chain.resume_finished(state['task_root'], log.attempt)
    finish(chain)
    assert chain.active['index'] == 1 and context['workspace']['payload']['draft'] == 'second authored requirement'
    before_count = len(log.applied)
    binding = {'session_root': str(tmp_path / 'computer'),
               'routed': {'plan': {'mode': 'office_bundle', 'sha256': 'e' * 64}}, 'refs': []}
    chain.active['binding'] = binding
    outcome = tmp_path / 'computer' / 'goals' / ('e' * 64 + '.capability') / 'outcome.json'
    outcome.parent.mkdir(parents=True)
    monkeypatch.setattr(anchor, 'automatic', lambda *args: (chain, context, log, state, outcome))
    history = {}
    events = []
    def setup(environment):
        widget = SimpleNamespace(configure=lambda **kwargs: None, set=lambda *args: None)
        environment.update(history_selected=selected, history_records=records, history_list=tree,
                           history_artifacts={}, history_output_list=widget, history_output_choice=widget,
                           history_open=widget, history_dir=widget, Path=Path,
                           _history_state_label=lambda value: value, time=time,
                           _history_list_state_label=lambda value: value)
        history.update(environment)
        env = main_functions(['inspect_history_selection'], history)
        callback = env['inspect_history_selection']
        if change == 'legacy_empty_event':
            # Reconstruct exactly the pre-fix function, without touching source.
            source = (Path(__file__).parents[1] / 'run_jarvis_assistant.py').read_text(encoding='utf-8')
            function = next(n for n in ast.walk(ast.parse(source))
                            if isinstance(n, ast.FunctionDef) and n.name == 'inspect_history_selection')
            empty = next(n for n in function.body if isinstance(n, ast.If)
                         and ast.unparse(n.test) == 'not selected')
            guard = next(n for n in empty.body if isinstance(n, ast.If)
                         and 'inspection' in ast.unparse(n.test))
            empty.body.remove(guard)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                         '<legacy-main-history-empty-event>', 'exec'), env)
            callback = env['inspect_history_selection']
        def selection_event(event):
            events.append(tree.selection())
            callback(event)
        tree.bind('<<TreeviewSelect>>', selection_event)
    def after(panel, current, goal, main_draft):
        assert goal.capability_source_generation == 1
        assert selected['id'] is selected['inspection'] is None
        # Production list completion deletes the previously selected resume row.
        history.update(task_id=history['history_scan']['generation'],
                       data=[{'id': task_id, 'root': state['task_root'], 'state': 'cancelled'},
                             {'id': 'another-task', 'root': str(tmp_path / 'another'), 'state': 'succeeded'}])
        history_list_result(history)
        hidden_root.update()
        assert events == [()]
        assert goal.capability_source_generation == (2 if change == 'legacy_empty_event' else 1)
        if change in ('manual_deselect', 'selection', 'selection_return'):
            tree.selection_set(task_id)
            hidden_root.update()
            assert selected['id'] == task_id and goal.capability_source_generation == 2
            if change == 'manual_deselect':
                tree.selection_remove(task_id)
                hidden_root.update()
                assert selected['id'] is selected['inspection'] is None
                assert goal.capability_source_generation == 3
            elif change == 'selection_return':
                tree.selection_set('another-task')
                hidden_root.update()
                tree.selection_set(task_id)
                hidden_root.update()
                assert goal.capability_source_generation == 4
        elif change == 'new_draft':
            main_draft.widget.insert('1.0', 'new authored instruction')
        elif change in ('new_source', 'source_return'):
            current['workspace']['binding']['source']['generation'] += (2 if change == 'source_return' else 1)
        elif change == 'stop':
            goal.stopped.set()
    try:
        anchor.test_real_goal_completion_and_main_history_refresh_preserve_auto_guard(
            tmp_path, monkeypatch, False, False, execution_kind='office_bundle_execute',
            after_handoff=after, terminal=True, history_setup=setup, expect_adopt=change == 'none')
        assert len(log.applied) == before_count + int(change == 'none')
        if change == 'none':
            assert chain.active['status'] == 'complete' and '均已完成' in log.statuses[-1]
        else:
            assert '当前新草稿或来源保留' in log.statuses[-1]
    finally:
        tree.destroy()
