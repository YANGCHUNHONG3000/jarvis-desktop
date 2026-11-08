"""Headless mixed-task controller checks; workers never use Tk or paid APIs."""
from copy import deepcopy
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import mixed_tasks_panel as module
from test_office_task_entry import DeferredThread


class Root:
    def __init__(self, worker):
        self.worker, self.scheduled = worker, []

    def after(self, delay, callback):
        assert not self.worker[0], 'worker touched the Tk scheduler'
        self.scheduled.append((delay, callback))


class Label:
    def __init__(self, worker):
        self.worker, self.values = worker, []

    def set(self, value):
        assert not self.worker[0], 'worker touched a Tk variable'
        self.values.append(value)


class Dialog:
    alive = True
    hidden = False

    def winfo_exists(self):
        return self.alive

    def withdraw(self):
        self.hidden = True

    def destroy(self):
        self.alive = False


def view(root, state='ready', *, kind='office_edit', resumable=False):
    return {'root': str(root), 'state': state, 'plan_sha256': 's'*64,
        'current_index': 0, 'steps': [], 'records': [], 'remaining_steps': 2,
        'current': {'kind': kind, 'plan_sha256': 'p'*64, 'plan': {'exact': 'child'},
                    'summary': {'resumable': resumable}}}


@pytest.fixture
def panel(monkeypatch, tmp_path):
    DeferredThread.pending = []
    worker = [False]
    monkeypatch.setattr(module.threading, 'Thread', DeferredThread)
    context = {'instruction': '先改Word再选择Explorer',
               'records': [{'path': str(tmp_path/'old.docx'), 'sha256': 'a'*64}], 'unavailable': False}
    workspace = {'payload': {'draft': context['instruction'], 'records': deepcopy(context['records']), 'source': None},
                 'revision': {'generation': 1, 'draft': 1}, 'unavailable': False}
    calls = {'busy': [], 'status': [], 'apply': [], 'windows': [], 'render': []}
    external_busy = [False]
    def show(self):
        assert not worker[0]
        if self.dialog is None:
            self.dialog = Dialog()
            self.label = Label(worker)
    def render(self):
        assert not worker[0], 'worker rendered Tk widgets'
        calls['render'].append(self.view)
    monkeypatch.setattr(module.MixedTasksPanel, '_show', show)
    monkeypatch.setattr(module.MixedTasksPanel, 'render', render)
    instance = module.MixedTasksPanel(
        root=Root(worker), context=lambda: context, workspace_context=lambda: workspace,
        windows=lambda: deepcopy(calls['windows']), busy=lambda: external_busy[0],
        on_busy=calls['busy'].append, apply=lambda item: calls['apply'].append(item) or True,
        status=calls['status'].append, task_base=tmp_path)
    instance._test = {'worker': worker, 'context': context, 'workspace': workspace,
                      'calls': calls, 'external_busy': external_busy, 'tmp': tmp_path}
    return instance


def run_worker(panel):
    panel._test['worker'][0] = True
    try:
        DeferredThread.run('mixed-task')
    finally:
        panel._test['worker'][0] = False


def finish(panel):
    run_worker(panel)
    panel.poll()


def test_launch_uses_queue_and_rejects_reentry_until_ui_polls(panel):
    result = view(panel._test['tmp'])
    calls = []
    assert panel._launch(lambda: calls.append('one') or result)
    assert not panel._launch(lambda: calls.append('duplicate'))
    assert panel.running and panel._test['calls']['busy'] == [True]
    run_worker(panel)
    assert calls == ['one'] and panel.view is None and panel.running
    assert panel._test['calls']['busy'] == [True]
    panel.poll()
    assert panel.view == result and not panel.running
    assert panel._test['calls']['busy'] == [True, False]


def test_external_busy_or_closed_panel_does_not_launch(panel):
    panel._test['external_busy'][0] = True
    assert not panel._launch(lambda: pytest.fail('must not execute'))
    panel._test['external_busy'][0] = False
    panel.close()
    assert not panel._launch(lambda: pytest.fail('must not execute'))
    assert not DeferredThread.pending and panel._test['calls']['busy'] == []


def test_worker_error_invalidates_visible_approval_and_sanitizes_provider_text(panel):
    panel.view = view(panel._test['tmp'], 'preview')
    panel.task_root = str(panel._test['tmp']/'preserved-task')
    def error():
        raise RuntimeError('private path C:/private key and document text 中文')
    assert panel._launch(error)
    finish(panel)
    assert panel.view is None and panel.task_root.endswith('preserved-task')
    assert 'RuntimeError' in panel.label.values[-1]
    assert 'private' not in panel.label.values[-1]
    assert not panel.execute()


def test_stable_application_error_is_displayed_without_reactivating_preview(panel):
    assert panel._launch(lambda: (_ for _ in ()).throw(ValueError('mixed_unknown_no_replay')))
    finish(panel)
    assert 'mixed_unknown_no_replay' in panel.label.values[-1] and panel.view is None


def test_thread_start_failure_releases_busy_without_worker(panel, monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError('thread unavailable')
    monkeypatch.setattr(module.threading, 'Thread', BrokenThread)
    assert not panel._launch(lambda: pytest.fail('must not run'))
    assert not panel.running and panel._test['calls']['busy'] == [True, False]


def test_hide_requests_stop_and_retains_exact_task_while_work_settles(panel):
    root = panel._test['tmp']/'original'
    panel.task_root = str(root)
    assert panel._launch(lambda: view(root, 'paused', resumable=True))
    panel.hide()
    assert panel.stopped.is_set() and panel.running and panel.dialog.alive
    finish(panel)
    assert not panel.running and panel.task_root == str(root)
    assert not DeferredThread.pending
    assert panel.dialog.hidden and not panel.hide_pending


def test_close_waits_for_worker_then_destroys_without_adopting_result(panel):
    assert panel._launch(lambda: view(panel._test['tmp'], 'complete'))
    panel.close()
    assert panel.closing and not panel.closed and panel.stopped.is_set()
    finish(panel)
    assert panel.closed and not panel.dialog.alive and panel.view is None
    assert panel._test['calls']['busy'] == [True, False]
    assert panel._test['calls']['apply'] == []


def test_idle_close_is_terminal_and_poll_does_not_schedule_again(panel):
    panel._show()
    panel.close()
    count = len(panel.root.scheduled)
    panel.poll()
    assert panel.closed and panel.stopped.is_set() and len(panel.root.scheduled) == count


def test_stale_epoch_messages_cannot_release_current_busy_or_replace_root(panel):
    current_root = panel._test['tmp']/'new'
    panel.task_root = str(current_root)
    assert panel._launch(lambda: view(current_root, 'preview'))
    epoch = panel.epoch
    panel.messages.put(('created', epoch-1, 'wrong-old-root', None, None))
    panel.messages.put(('finished', epoch-1, view('wrong-old-root'), None, None))
    panel.poll()
    assert panel.running and panel.task_root == str(current_root) and panel.view is None
    assert panel._test['calls']['busy'] == [True]
    finish(panel)
    assert panel.view['root'] == str(current_root)


def test_restore_uses_only_readonly_inspect_and_no_client(panel, monkeypatch):
    from agent_service import model
    def forbidden(*args, **kwargs):
        pytest.fail('restore must not construct a model or dispatch work')
    monkeypatch.setattr(model, 'CloudClient', forbidden)
    monkeypatch.setattr(module.core, 'split', forbidden)
    monkeypatch.setattr(module.core, 'prepare', forbidden)
    monkeypatch.setattr(module.core, 'execute', forbidden)
    root = panel._test['tmp']/'existing'
    reads = []
    monkeypatch.setattr(module.core, 'inspect', lambda path: reads.append(path) or view(root, 'unknown'))
    assert panel.restore(root)
    finish(panel)
    assert reads == [str(root)] and panel.view['state'] == 'unknown' and panel.submitted is None
    assert not panel.execute()


def test_exact_current_token_and_existing_root_reach_executor(panel, monkeypatch):
    panel.task_root = 'existing-root'
    panel.view = view(panel.task_root, 'preview')
    calls = []
    monkeypatch.setattr(module.core, 'execute', lambda root, sha, stop:
                        calls.append((root, sha, stop())) or view(root, 'ready'))
    assert panel.execute()
    finish(panel)
    assert calls == [('existing-root', 'p'*64, False)]


def test_office_resume_does_not_construct_model_or_replace_task(panel, monkeypatch):
    from agent_service import model
    monkeypatch.setattr(model, 'CloudClient', lambda **kwargs: pytest.fail('resume is local'))
    panel.task_root = 'same-task'
    panel.view = view('same-task', 'paused', resumable=True)
    calls = []
    monkeypatch.setattr(module.core, 'resume_preview', lambda root:
                        calls.append(root) or view(root, 'preview'))
    assert panel.resume()
    finish(panel)
    assert calls == ['same-task'] and panel.task_root == 'same-task'


@pytest.mark.parametrize('state,kind,resumable,enabled', [
    ('review', 'office_edit', False, 'approve'), ('ready', 'office_edit', False, 'prepare'),
    ('preview', 'office_edit', False, 'execute'), ('child_verified', 'windows', False, 'execute'),
    ('paused', 'windows', True, 'execute'), ('paused', 'office_edit', True, 'resume'),
    ('complete', 'office_edit', False, 'deliver'),
])
def test_action_gate_exposes_only_current_authorized_operation(state, kind, resumable, enabled):
    available = module.actions(view('root', state, kind=kind, resumable=resumable))
    assert [key for key, value in available.items() if value] == [enabled]


@pytest.mark.parametrize('state', ['unknown', 'planning_pending', 'split_pending', None])
def test_uncertain_states_have_no_execute_or_paid_replan_button(state):
    assert not any(module.actions(view('root', state)).values())


def test_delivery_payload_clears_only_unchanged_submission_and_copies_records():
    records = [{'path': 'current.xlsx', 'sha256': 'a'*64}]
    workspace = {'payload': {'draft': 'submitted'}, 'revision': {'draft': 1}}
    assert module.delivery_payload(records, workspace, {'draft': 1})['draft'] == ''
    for submitted in [None, {'draft': 0}]:
        result = module.delivery_payload(records, workspace, submitted)
        assert result['draft'] == 'submitted' and result['source'] is None
        result['records'].clear()
        assert records


def prepare_delivery(panel, monkeypatch):
    panel.task_root = 'completed-root'
    panel.view = view(panel.task_root, 'complete')
    panel.submitted = deepcopy(panel._test['workspace']['revision'])
    records = [{'path': 'verified.xlsx', 'sha256': 'a'*64}]
    captured = []
    monkeypatch.setattr(module.core, 'outputs', lambda root: deepcopy(records))
    monkeypatch.setattr(module.workspace_state, 'verify', lambda payload, base:
                        captured.append(deepcopy(payload)) or {'verified': payload})
    return captured


def test_unchanged_delivery_is_verified_in_worker_then_applied_on_ui(panel, monkeypatch):
    captured = prepare_delivery(panel, monkeypatch)
    assert panel.deliver()
    run_worker(panel)
    assert captured[0]['draft'] == '' and panel._test['calls']['apply'] == []
    panel.poll()
    assert panel._test['calls']['apply'] == [{'verified': captured[0]}]


def test_new_draft_before_delivery_is_retained_in_verified_payload(panel, monkeypatch):
    captured = prepare_delivery(panel, monkeypatch)
    workspace = panel._test['workspace']
    workspace['payload']['draft'] = 'new instruction to retain'
    workspace['revision']['draft'] = 2
    assert panel.deliver()
    finish(panel)
    assert captured[0]['draft'] == 'new instruction to retain'
    assert panel._test['calls']['apply'][0]['verified']['draft'] == 'new instruction to retain'


@pytest.mark.parametrize('change', ['draft', 'source', 'stop'])
def test_worker_completion_does_not_overwrite_later_changes_or_stop(panel, monkeypatch, change):
    prepare_delivery(panel, monkeypatch)
    assert panel.deliver()
    run_worker(panel)
    workspace = panel._test['workspace']
    if change == 'draft':
        workspace['payload']['draft'] = 'typed while worker was running'
        workspace['revision']['draft'] += 1
    elif change == 'source':
        workspace['payload']['source'] = {'path': 'new-user-source'}
        workspace['revision']['generation'] += 1
    else:
        panel.stop()
    before = deepcopy(workspace)
    panel.poll()
    assert panel._test['calls']['apply'] == [] and workspace == before
    assert panel.view['state'] == 'complete'


def test_unavailable_workspace_does_not_start_delivery(panel, monkeypatch):
    prepare_delivery(panel, monkeypatch)
    panel._test['workspace']['unavailable'] = True
    assert not panel.deliver() and not DeferredThread.pending


def test_new_root_is_queued_before_split_failure_and_source_snapshot_is_stable(panel, monkeypatch):
    from agent_service import model
    from agent_service import windows_access
    root = panel._test['tmp']/'already-persisted-root'
    events = []
    panel._test['calls']['windows'] = [{'hwnd': 1, 'title': 'selected'}]
    def create(instruction, records, windows, **kwargs):
        events.append(('create', instruction, deepcopy(records), deepcopy(windows)))
        root.mkdir()
        return root
    def split(path, client, stop):
        assert path.exists()
        queued = list(panel.messages.queue)
        assert queued[0] == ('created', panel.epoch, str(root), None, None)
        events.append(('split', str(path)))
        raise ValueError('model_budget_exceeded')
    monkeypatch.setattr(module.core, 'create', create)
    monkeypatch.setattr(module.core, 'split', split)
    monkeypatch.setattr(model, 'CloudClient', lambda **kwargs: object())
    monkeypatch.setattr(windows_access, 'AutoWindowsBackend', lambda: pytest.fail('explicit windows must be used'))
    original = deepcopy(panel._test['context'])
    assert panel.new()
    panel._test['context']['instruction'] = 'new draft entered during worker'
    panel._test['context']['records'].clear()
    finish(panel)
    assert events[0][1:3] == (original['instruction'], original['records'])
    assert panel.task_root == str(root) and panel.view is None and root.exists()
    assert 'model_budget_exceeded' in panel.label.values[-1]


def test_new_uses_existing_windows_backend_inventory_only_when_no_selection(panel, monkeypatch):
    from agent_service import model, windows_access
    inventory = [{'hwnd': 3}]
    calls = []
    class Backend:
        def list_windows(self, *, stop):
            calls.append(stop())
            return inventory
    monkeypatch.setattr(windows_access, 'AutoWindowsBackend', Backend)
    monkeypatch.setattr(model, 'CloudClient', lambda **kwargs: object())
    monkeypatch.setattr(module.core, 'create', lambda instruction, records, windows, **kwargs:
                        calls.append(windows) or panel._test['tmp'])
    monkeypatch.setattr(module.core, 'split', lambda root, client, stop: view(root, 'review'))
    assert panel.new()
    finish(panel)
    assert calls == [False, inventory] and panel.view['state'] == 'review'


@pytest.mark.parametrize('missing', ['instruction', 'records', 'unavailable'])
def test_new_requires_explicit_instruction_and_verified_source_context(panel, missing):
    panel._test['context'][missing] = True if missing == 'unavailable' else [] if missing == 'records' else ''
    assert not panel.new() and not DeferredThread.pending and panel._test['calls']['busy'] == []


@pytest.mark.parametrize('terminal', ['closed', 'closing'])
def test_terminal_new_and_restore_preserve_existing_task_identity(panel, terminal):
    panel.task_root = 'preserved-original'
    panel.view = view(panel.task_root, 'unknown')
    panel.submitted = {'original': 'ticket'}
    before = (panel.task_root, deepcopy(panel.view), deepcopy(panel.submitted))
    setattr(panel, terminal, True)
    assert not panel.new() and not panel.restore('different-root')
    assert (panel.task_root, panel.view, panel.submitted) == before
    assert not DeferredThread.pending


@pytest.fixture(scope='module')
def main_functions():
    """Compile only the new pure GUI closures; never import/start the desktop."""
    source = Path(__file__).resolve().parents[1]/'run_jarvis_assistant.py'
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    return {node.name: node for node in main.body if isinstance(node, ast.FunctionDef)
            and node.name in {'mixed_context', 'mixed_busy', 'mixed_apply', 'close'}}


def compile_closure(main_functions, name, environment):
    exec(compile(ast.Module(body=[main_functions[name]], type_ignores=[]), '<mixed-main-closure>', 'exec'), environment)
    return environment[name]


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.mark.parametrize('blocker', ['none', 'source', 'reference', 'pending', 'auto_required', 'empty', 'oversize'])
def test_main_mixed_context_adopts_only_ready_unselected_batch(main_functions, blocker):
    scope = Variable('当前选中资料')
    label = Variable('explicit.docx' if blocker == 'source' else '')
    source = {'artifact_ref': {'artifact': 'explicit.docx'} if blocker == 'reference' else None,
              'pending': blocker == 'pending', 'auto_required': blocker == 'auto_required'}
    count = 0 if blocker == 'empty' else 5 if blocker == 'oversize' else 2
    reads = []
    def office_context():
        reads.append(scope.get())
        return {'binding': {'batch': list(range(count))}, 'scope': scope.get()}
    context = compile_closure(main_functions, 'mixed_context', {
        'office_route_context': office_context, 'source_label': label,
        'source_choice': source, 'route_scope': scope})()
    if blocker == 'none':
        assert scope.get() == context['scope'] == '当前成果批次' and len(reads) == 2
    else:
        assert scope.get() == context['scope'] == '当前选中资料' and len(reads) == 1


def test_main_mixed_busy_owns_only_its_activity_and_notifies_goal(main_functions):
    active, calls = {'kind': None}, []
    change = compile_closure(main_functions, 'mixed_busy', {
        'active': active, 'notify_goal_external_activity': lambda *args, **kwargs: calls.append((args, kwargs))})
    change(True)
    assert active == {'kind': 'mixed_task'} and calls == [((True,), {'force': True})]
    change(False)
    assert active == {'kind': None}
    active['kind'] = 'another-task'
    change(False)
    assert active['kind'] == 'another-task'


@pytest.mark.parametrize('accepted', [False, True])
def test_main_delivery_adoption_changes_scope_only_after_workspace_accepts(main_functions, accepted):
    scope, status, selected, applied = Variable('当前选中资料'), Variable('untouched'), [], []
    apply = compile_closure(main_functions, 'mixed_apply', {
        'apply_workspace': lambda verified: applied.append(verified) or accepted,
        'route_scope': scope, 'tabs': SimpleNamespace(select=selected.append),
        'task_tab': 'main-task-tab', 'task_status': status})
    evidence = {'exact': 'verified-workspace'}
    assert apply(evidence) is accepted and applied == [evidence]
    assert scope.get() == ('当前成果批次' if accepted else '当前选中资料')
    assert selected == (['main-task-tab'] if accepted else [])
    assert status.get() != 'untouched' if accepted else status.get() == 'untouched'


def test_main_close_defers_destruction_until_mixed_worker_finishes(main_functions):
    calls, scheduled = [], []
    environment = {'active': {'kind': 'mixed_task'},
        'mail_navigation_panel': SimpleNamespace(invalidate=lambda: calls.append('invalidate')),
        'mixed_panel': SimpleNamespace(close=lambda: calls.append('mixed-close')),
        'root': SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))}
    close = compile_closure(main_functions, 'close', environment)
    close()
    assert calls == ['invalidate', 'mixed-close'] and scheduled == [(200, close)]
def test_human_preview_keeps_exact_office_range_and_values():
    from agent_service.mixed_tasks_panel import preview_lines
    lines = preview_lines({'kind': 'office_edit', 'source_ids': ['s1', 's2'],
        'input_records': [{'path': 'word.docx'}, {'path': 'excel.xlsx'}], 'plan': {'goal': {'edits': [
        {'id': 'e1', 'spec': {'kind': 'document', 'changes': [
            {'paragraph_index': 1, 'before': 'old', 'after': 'new'}]}},
        {'id': 'e2', 'spec': {'kind': 'spreadsheet', 'changes': [
            {'sheet': '预算', 'cell': 'B2', 'before': 28, 'after': 30}]}}]}}})
    assert '  正文段落 2：old → new' in lines
    assert '  预算!B2：28 → 30' in lines


def test_human_preview_maps_sparse_reverse_edit_id_to_logical_source():
    from agent_service.mixed_tasks_panel import preview_lines
    lines = preview_lines({'kind': 'office_edit', 'source_ids': ['s2', 's1'],
        'input_records': [{'path': 'first.docx'}, {'path': 'second.xlsx'}],
        'plan': {'goal': {'edits': [{'id': 'e2', 'spec': {'kind': 'document',
            'changes': [{'paragraph_index': 1, 'before': 'old', 'after': 'new'}]}}]}}})
    assert 's1 · Word · first.docx' in lines
    assert not any('second.xlsx' in line for line in lines)


def test_human_preview_maps_subset_e1_to_second_source():
    from agent_service.mixed_tasks_panel import preview_lines
    lines = preview_lines({'kind': 'office_edit', 'source_ids': ['s2'],
        'input_records': [{'path': 'first.docx'}, {'path': 'second.xlsx'}],
        'plan': {'goal': {'edits': [{'id': 'e1', 'spec': {'kind': 'spreadsheet',
            'changes': [{'sheet': '保留页', 'cell': 'B2', 'before': 789, 'after': 790}]}}]}}})
    assert 's2 · Excel · second.xlsx' in lines


def test_human_preview_keeps_windows_target_and_postcondition():
    from agent_service.mixed_tasks_panel import preview_lines
    lines = preview_lines({'kind': 'windows', 'plan': {'conditions': [
        {'window': {'title': '此电脑'}, 'selector': {'name': '本地磁盘 (C:)'},
         'state': 'selected', 'value': True, 'retain': True}]}})
    assert '此电脑 / 本地磁盘 (C:)：selected → True；后续保持=True' in lines
