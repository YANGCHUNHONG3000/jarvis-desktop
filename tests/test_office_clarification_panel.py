"""Offline clarification UI handoff with real workspace/context methods."""
from copy import deepcopy
import uuid

import pytest

from agent_service import computer_goal_panel, office_clarification_panel, office_workspace
from agent_service import office_edit_clarification as records
from agent_service.computer import ComputerSession, write_new
from test_office_workspace_panel import _Goal, _Thread, _make_panel, _material


@pytest.fixture
def case(tmp_path, monkeypatch):
    material, scope, refs = _material(tmp_path)
    goal = _Goal(files=[row['path'] for row in material], refs=refs,
                 scope=scope, draft='修改那个标题')
    goal.stopped.clear()
    goal.session = ComputerSession.create(tmp_path / 'sessions')
    panel = _make_panel(goal, tmp_path / 'workspaces', tmp_path, monkeypatch)
    clar = panel.clarifications
    clar.store = tmp_path / 'clarifications'
    folder = goal.session.root / 'office-planning' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    result = {'plan': None, 'reason': '请明确哪个标题？',
              'office_result': {'kind': 'clarify', 'record': str(folder)}}
    payload = panel.context()['payload']
    for name, value in [('request', {'instruction': payload['draft'],
            'files': [r['path'] for r in material], 'artifact_refs': refs, 'scope': scope}),
            ('outcome', result), ('response', {'kind': 'clarify', 'reason': result['reason']})]:
        write_new(folder / (name + '.json'), value)
    dialogs, calls = [], []
    monkeypatch.setattr(office_clarification_panel, 'open_question',
                        lambda *args: dialogs.append(args))
    def verify(payload, _base):
        calls.append(deepcopy(payload))
        selected = [deepcopy(ref) for ref in refs
                    if ref['artifact'] in [r['path'] for r in payload['records']]]
        return {'payload': deepcopy(payload), 'refs': selected, 'source_ref': None}
    monkeypatch.setattr(office_workspace, 'verify', verify)
    panel.controller.expected_id = 'original-workspace'
    return panel, goal, result, dialogs, calls


def question(case):
    panel, goal, result, dialogs, _ = case
    panel.clarifications.on_result(result, None, panel.context())
    assert len(dialogs) == 1
    return panel.clarifications.active, dialogs[-1][3]


def complete(panel):
    _Thread.all[-1].run()
    panel.poll()


def mutate(panel, goal, change):
    if change == 'draft_revert':
        text = goal.instruction_draft.text
        goal.instruction_draft.adopt('new draft')
        goal.instruction_draft.adopt(text)
    elif change == 'draft':
        goal.instruction_draft.adopt('new draft')
    elif change == 'scope':
        goal.office_scope[0]['locations'] = [{'paragraph_index': 19}]
    elif change == 'refs':
        goal.office_artifact_refs[0]['task_root'] = 'new-proof'
    elif change == 'mode':
        goal.mode.set(computer_goal_panel.MODES[0])
    elif change == 'plan':
        goal.plan = {'new': 'preview'}
    elif change == 'stop':
        goal.stopped.set()
    elif change == 'generation':
        goal.capability_source_generation += 2
    elif change == 'route_generation':
        goal.daily_task_route_generation += 2
    elif change == 'busy':
        goal.running = True
    elif change == 'external_busy':
        goal.external['busy'] = True
        panel.controller.busy = lambda: True
    elif change == 'closed':
        goal.closed = True
    elif change == 'control':
        goal.source.set('new-source')


def test_actual_goal_poll_show_plan_none_preserves_capture_guard(case):
    panel, goal, result, dialogs, calls = case
    guard = panel.context()
    goal.complete_instruction = lambda *args: computer_goal_panel.GoalPanel.complete_instruction(goal, *args)
    goal.show_plan = lambda plan, **kw: computer_goal_panel.GoalPanel.show_plan(goal, plan, **kw)
    goal.messages.put(('propose', {**result, '_office_guard': guard}, None))
    goal.poll()
    assert panel.context() == guard
    assert len(dialogs) == 1 and panel.clarifications.active['kind'] == 'question'
    assert not calls and not _Thread.all
    assert goal.plan is None and goal.executions == 0
    assert goal.shown == ('需要补充编辑要求：' + result['reason']
                          + '\n原任务、文件及范围已保存；补充后重新规划。')
    assert '当前目标不在已支持范围内' not in goal.shown


@pytest.mark.parametrize('change', ['draft_revert', 'draft', 'scope', 'refs', 'mode',
    'plan', 'stop', 'generation', 'route_generation', 'busy', 'external_busy', 'closed', 'control'])
def test_stale_planning_question_never_saved_or_opened(case, change):
    panel, goal, result, dialogs, calls = case
    guard = panel.context()
    goal.shown = 'current preview'
    mutate(panel, goal, change)
    panel.clarifications.on_result(result, None, guard)
    assert panel.clarifications.active is None and not dialogs
    assert not panel.clarifications.store.exists()
    assert not calls and not _Thread.all
    assert goal.shown == 'current preview'


def test_failed_question_capture_does_not_show_saved_claim(case, monkeypatch):
    panel, goal, result, dialogs, calls = case
    goal.shown = 'current preview'
    def fail(*args):
        raise OSError('save failed')
    monkeypatch.setattr(records, 'capture', fail)
    panel.clarifications.on_result(result, None, panel.context())
    assert panel.clarifications.active is None and not dialogs
    assert goal.shown == 'current preview'
    assert '未能完整保存' in goal.status.get()
    assert not calls and not _Thread.all


@pytest.mark.parametrize('kind', ['unsupported', 'edits'])
def test_nonclarification_result_keeps_existing_preview(case, kind):
    panel, goal, _, dialogs, calls = case
    goal.shown = 'current preview'
    panel.clarifications.on_result({'office_result': {'kind': kind}}, None, panel.context())
    assert goal.shown == 'current preview'
    assert panel.clarifications.active is None and not dialogs
    assert not calls and not _Thread.all


def test_answer_reverified_saved_and_adopted_without_planning_permissions(case):
    panel, goal, _, _, calls = case
    old, accept = question(case)
    assert accept('把第三段标题改为待办事项')
    assert not accept('重复点击')
    assert goal.instruction_draft.text == old['original']['draft']
    assert panel.preparation.pending['ticket']['purpose'] == 'office_answer'
    complete(panel)
    saved = records.history(panel.clarifications.store)
    answers = [r for r in saved if r['kind'] == 'answer']
    assert len(answers) == 1 and len(calls) == 1
    assert goal.instruction_draft.text == answers[0]['payload']['draft']
    assert goal.office_scope == old['original']['scope']
    assert goal.plan is None and goal.executions == 0
    assert panel.clarifications.active is None
    assert panel.controller.expected_id == 'original-workspace'
    assert records.load(panel.clarifications.store, old['id']) == old
    assert not accept('迟到重复点击')


@pytest.mark.parametrize('change', ['draft_revert', 'draft', 'scope', 'refs', 'mode',
    'plan', 'stop', 'generation', 'route_generation', 'busy', 'external_busy', 'closed', 'control'])
@pytest.mark.parametrize('stage', ['before_submit', 'during_verify'])
def test_full_context_changes_reject_old_answer(case, change, stage):
    panel, goal, _, _, _ = case
    _, accept = question(case)
    if stage == 'during_verify':
        assert accept('明确第三段')
    mutate(panel, goal, change)
    after_change = deepcopy(panel.context())
    if stage == 'before_submit':
        assert not accept('明确第三段')
        assert not _Thread.all
    else:
        complete(panel)
        assert panel.preparation.pending is None
    assert panel.context() == after_change
    assert all(r['kind'] == 'question' for r in records.history(panel.clarifications.store))
    assert goal.executions == 0


@pytest.mark.parametrize('failure', ['timeout', 'verify_failure', 'close_preparation'])
def test_failed_preparation_preserves_original_and_question(case, monkeypatch, failure):
    panel, goal, _, _, _ = case
    old, accept = question(case)
    before = panel.context()
    assert accept('明确第三段')
    if failure == 'timeout':
        panel.preparation.pending['deadline'] = -1
    elif failure == 'close_preparation':
        panel.close()
    else:
        def failed(*args):
            raise office_workspace.WorkspaceVerificationError('changed', 0)
        monkeypatch.setattr(office_workspace, 'verify', failed)
    complete(panel)
    assert panel.context() == before
    assert panel.clarifications.active == old
    assert records.history(panel.clarifications.store) == [old]
    assert goal.plan is None and goal.executions == 0


@pytest.mark.parametrize('interrupt', ['stop', 'timeout'])
def test_busy_retained_until_interrupted_worker_returns(case, interrupt):
    panel, goal, _, _, _ = case
    _, accept = question(case)
    events = []
    def on_busy(value):
        goal.set_busy(value)
        goal.external['busy'] = value
        events.append(value)
    panel.preparation.on_busy = on_busy
    assert accept('明确第三段')
    assert goal.running and goal.external['busy']
    if interrupt == 'stop':
        goal.stopped.set()
    else:
        panel.preparation.pending['deadline'] = -1
    panel.poll()
    assert panel.preparation.pending['discard']
    assert goal.running and goal.external['busy'] and events == [True]
    complete(panel)
    assert panel.preparation.pending is None
    assert not goal.running and not goal.external['busy']
    assert events == [True, False]
    assert goal.instruction_draft.text == '修改那个标题'


def test_restore_answer_into_empty_workspace_reverifies_without_restoring_plan(case):
    panel, goal, _, _, calls = case
    _, accept = question(case)
    assert accept('明确第三段')
    complete(panel)
    answer = next(r for r in records.history(panel.clarifications.store) if r['kind'] == 'answer')
    goal.instruction_draft.adopt('')
    goal.office_planning_files, goal.office_artifact_refs, goal.office_scope = (), [], None
    goal.mode.set(computer_goal_panel.MODES[0])
    goal.stopped.set()
    assert panel.clarifications.restore(answer)
    assert not goal.stopped.is_set()
    assert goal.instruction_draft.text == ''
    complete(panel)
    assert len(calls) == 2
    assert goal.instruction_draft.text == answer['payload']['draft']
    assert goal.office_scope == answer['payload']['scope']
    assert goal.plan is None and goal.executions == 0
    assert panel.clarifications.active is None
    assert panel.controller.expected_id == 'original-workspace'


def test_restore_question_sets_active_then_explicitly_opens_answer(case):
    panel, goal, _, dialogs, calls = case
    old, _ = question(case)
    assert panel.clarifications.restore(old)
    complete(panel)
    assert panel.clarifications.active == old
    assert len(dialogs) == 1 and len(calls) == 1
    assert panel.clarifications.open_answer()
    assert len(dialogs) == 2 and goal.plan is None


@pytest.mark.parametrize('change', ['draft', 'scope', 'refs', 'mode', 'plan',
    'busy', 'external_busy', 'closed', 'control'])
def test_restore_refuses_existing_new_input(case, change):
    panel, goal, _, _, calls = case
    old, _ = question(case)
    mutate(panel, goal, change)
    if change == 'refs':
        goal.office_artifact_refs[0]['sha256'] = 'c' * 64
    before = panel.context()
    assert not panel.clarifications.restore(old)
    assert panel.context() == before and not calls and not _Thread.all


@pytest.mark.parametrize('change', ['draft_revert', 'draft', 'scope', 'refs', 'mode',
    'plan', 'stop', 'generation', 'busy', 'external_busy', 'closed', 'control'])
def test_restore_rejects_late_result_after_new_input(case, change):
    panel, goal, _, _, _ = case
    old, _ = question(case)
    assert panel.clarifications.restore(old)
    mutate(panel, goal, change)
    before = panel.context()
    complete(panel)
    assert panel.context() == before and goal.executions == 0


def test_answer_accepts_explicit_subset_then_reverifies_only_that_scope(case):
    panel, goal, _, dialogs, calls = case
    old, _ = question(case)
    goal.office_planning_files = (goal.office_planning_files[0],)
    goal.office_artifact_refs = goal.office_artifact_refs[:1]
    goal.office_scope = goal.office_scope[:1]
    assert panel.clarifications.open_answer()
    assert dialogs[-1][3]('明确该段')
    complete(panel)
    assert calls[0]['records'] == old['original']['records'][:1]
    assert goal.office_scope == old['original']['scope'][:1]
    assert goal.plan is None and goal.executions == 0


def test_cancel_answer_keeps_exact_original_and_no_worker(case):
    panel, goal, _, _, calls = case
    before = panel.context()
    question(case)
    assert panel.context() == before and not calls and not _Thread.all
    assert goal.executions == 0


@pytest.mark.parametrize('answer', ['', 'line\nline', 'x' * 601])
def test_invalid_answer_does_not_start_preparation(case, answer):
    panel, _, _, _, calls = case
    _, accept = question(case)
    assert not accept(answer)
    assert not calls and not _Thread.all


def test_save_answer_failure_does_not_install_verified_draft(case, monkeypatch):
    panel, goal, _, _, _ = case
    old, accept = question(case)
    before = panel.context()
    assert accept('明确第三段')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(records, 'save_answer', fail)
    complete(panel)
    assert panel.context() == before and panel.clarifications.active == old
    assert goal.plan is None and goal.executions == 0
