"""Persisted route handoff only: no model, native Office, or background work."""
from copy import deepcopy
import pytest

from test_computer_goal_panel import hidden_root, panel
from agent_service import daily_task_routing as routing
from agent_service.computer import ComputerSession, digest, write_new
from agent_service.computer_goal_panel import MODES


@pytest.fixture
def saved_route(tmp_path):
    session = ComputerSession.create(tmp_path)
    refs = [{'artifact': str(tmp_path / ('source' + suffix)), 'sha256': char * 64,
             'task_root': str(tmp_path / ('task-' + char * 32))}
            for suffix, char in [('.docx', 'a'), ('.xlsx', 'b')]]
    instruction = '请处理当前两份办公成果'

    def create(route='single_source', ids=None, sources=None):
        values = deepcopy(refs if sources is None else sources)
        selected = ids if ids is not None else (['S1', 'S2'] if route == 'compare_tables' else ['S2'])
        request = {'schema': routing.POLICY, 'instruction': instruction,
                   'source_refs': values, 'task_base': str(tmp_path)}
        run = session.root / 'task_routes' / digest(request)
        run.mkdir(parents=True)
        decision = {'route': route, 'source_ids': selected, 'reason': '按明确任务选择'}
        plan = None
        if route != 'office_edit':
            plan = {'schema': 1, 'mode': 'office_bundle' if route == 'compare_tables' else 'capability',
                    'instruction': instruction, 'task_base': str(tmp_path),
                    'request_id': 'c' * 32}
            if route == 'compare_tables':
                plan['source_refs'] = values
            else:
                plan.update(source_ref=values[int(selected[0][1:]) - 1] if selected else None, candidates=[])
            plan['sha256'] = digest(plan)
            (session.root / 'goals').mkdir(exist_ok=True)
            write_new(session.root / 'goals' / (plan['sha256'] + '.json'), plan)
        outcome = {'schema': 1, 'request_sha256': digest(request),
                   'state': 'needs_scope' if route == 'office_edit' else 'ready',
                   'code': 'planned', 'plan': plan}
        if route == 'office_edit':
            outcome['decision_sha256'] = digest(decision)
        write_new(run / 'request.json', request)
        write_new(run / 'decision.json', {'request_sha256': digest(request), 'decision': decision})
        write_new(run / 'outcome.json', outcome)
        return session, instruction, routing._cached(session, run, request), values

    return create


def forbid_work(p, monkeypatch):
    monkeypatch.setattr(p, 'start', lambda *a: pytest.fail('handoff must not start a worker'))
    monkeypatch.setattr(p, 'refresh_capability_sources', lambda: pytest.fail('must keep exact routed sources'))
    monkeypatch.setattr(routing, 'propose', lambda *a, **k: pytest.fail('must not replan'))


@pytest.mark.parametrize('route', ['single_source', 'compare_tables', 'no_source'])
def test_ready_pins_sources_session_and_plan_without_work(panel, saved_route, monkeypatch, route):
    p, _ = panel
    args = saved_route(route, ids=[] if route == 'no_source' else None,
                       sources=[] if route == 'no_source' else None)
    session, instruction, routed, refs = args
    forbid_work(p, monkeypatch)
    assert p.use_task_route(*args, lambda: True)
    assert p.session is session and p.plan == routed['plan']
    assert p.mode.get() == MODES[11] and p.capability_sources == refs
    assert p.capability_sources_validated_generation == p.capability_source_generation
    assert p.capability_history_seed['records'] == [
        {'path': ref['artifact'], 'sha256': ref['sha256']} for ref in refs]
    assert p.text.get('1.0', 'end-1c') == instruction and not p.text.edit_modified()
    assert str(p.execute_button['state']) == 'normal'
    refs.clear()
    assert len(p.capability_sources) == (0 if route == 'no_source' else 2)


def test_edit_opens_existing_selector_for_exact_selected_subset(panel, saved_route, monkeypatch):
    p, _ = panel
    args = saved_route('office_edit', ids=['S2'])
    forbid_work(p, monkeypatch)
    opened = []
    monkeypatch.setattr('agent_service.office_scope_panel.open_file_selector',
                        lambda root, records, callback: opened.append((deepcopy(records), callback)))
    assert p.use_task_route(*args, lambda: True)
    assert p.session is args[0] and p.mode.get() == MODES[9] and p.plan is None
    assert opened[0][0] == [{'path': args[3][1]['artifact'], 'sha256': args[3][1]['sha256']}]
    assert opened[0][1] == p.prepare_generated_scope
    assert p.text.get('1.0', 'end-1c') == args[1]
    assert str(p.execute_button['state']) == 'disabled'


@pytest.mark.parametrize('guard', ['running', 'external', 'closed', 'stale'])
def test_guards_reject_before_changing_draft_or_plan(panel, saved_route, guard):
    p, _ = panel
    args = saved_route()
    p.text.insert('1.0', '保留草稿')
    old = p.plan = {'old': True}
    if guard == 'running':
        p.running = True
    elif guard == 'external':
        p.external_busy = lambda: True
    elif guard == 'closed':
        p.closed = True
    assert not p.use_task_route(*args, lambda: guard != 'stale')
    assert p.text.get('1.0', 'end-1c') == '保留草稿' and p.plan is old
    p.running = p.closed = False


def test_different_nonempty_draft_is_preserved(panel, saved_route):
    p, _ = panel
    p.text.insert('1.0', '另一个任务')
    assert not p.use_task_route(*saved_route(), lambda: True)
    assert p.text.get('1.0', 'end-1c') == '另一个任务' and p.session is None
    assert '草稿' in p.status.get()


def test_same_draft_can_be_reused_and_old_approval_replaced(panel, saved_route):
    p, _ = panel
    args = saved_route()
    p.text.insert('1.0', args[1])
    p.plan = {'old': True}
    assert p.use_task_route(*args, lambda: True)
    assert p.plan == args[2]['plan'] and p.plan != {'old': True}


@pytest.mark.parametrize('padding', ['', '  ', '\n'])
def test_same_normalized_route_preserves_exact_existing_draft_ticket(panel, saved_route, padding):
    p, _ = panel
    args = saved_route()
    ticket = p.instruction_draft.adopt(padding + args[1] + padding)
    p.text.edit_modified(False)
    assert p.use_task_route(*args, lambda: True)
    assert p.instruction_draft.unchanged(ticket)
    assert p.instruction_ticket(args[2]['plan']) == ticket


def test_whitespace_only_target_is_preserved_as_user_draft(panel, saved_route):
    p, _ = panel
    ticket = p.instruction_draft.adopt(' \n ')
    assert not p.use_task_route(*saved_route(), lambda: True)
    assert p.instruction_draft.unchanged(ticket)


def test_later_source_invalidation_prevents_approval_dispatch(panel, saved_route, monkeypatch):
    p, _ = panel
    current = [True]
    assert p.use_task_route(*saved_route(), lambda: current[0])
    current[0] = False
    monkeypatch.setattr(p, 'start', lambda *a: pytest.fail('stale route must not execute'))
    p.execute()
    assert p.plan is None and '失效' in p.status.get()


@pytest.mark.parametrize('change', ['instruction', 'refs', 'plan', 'routing', 'state', 'edit'])
def test_changed_route_inputs_are_rejected_without_draft_write(panel, saved_route, change):
    p, _ = panel
    session, instruction, routed, refs = saved_route('office_edit' if change == 'edit' else 'single_source')
    if change == 'instruction':
        instruction += '新任务'
    elif change == 'refs':
        refs[0]['sha256'] = 'd' * 64
    elif change == 'plan':
        routed['plan']['mode'] = 'agent'
    elif change == 'routing':
        routed['routing'] = str(session.root.parent)
    elif change == 'state':
        routed['state'] = 'blocked'
    else:
        routed['edit']['source_refs'] = refs
    assert not p.use_task_route(session, instruction, routed, refs, lambda: True)
    assert p.session is None and p.plan is None and p.text.get('1.0', 'end-1c') == ''


def test_saved_child_plan_change_is_rejected(panel, saved_route):
    p, _ = panel
    args = saved_route()
    plan_path = args[0].root / 'goals' / (args[2]['plan']['sha256'] + '.json')
    plan_path.write_text('{}', encoding='utf-8')
    assert not p.use_task_route(*args, lambda: True)
    assert p.session is None and p.plan is None


def test_callback_becomes_stale_during_record_check(panel, saved_route):
    p, _ = panel
    calls = []
    def current():
        calls.append(True)
        return len(calls) == 1
    assert not p.use_task_route(*saved_route(), current)
    assert p.session is None and p.plan is None


@pytest.mark.parametrize('single_selected', [False, True])
def test_existing_capability_route_poll_hands_edit_to_original_picker(
        panel, saved_route, monkeypatch, tmp_path, single_selected):
    p, _ = panel
    refs = [{'artifact': str(tmp_path / 'selected.docx'), 'sha256': 'd' * 64}]
    args = saved_route('office_edit', ids=['S1'], sources=refs) if single_selected else saved_route('office_edit')
    p.session = args[0]
    p.mode.set(MODES[11])
    p.mode_changed(refresh_sources=False)
    p.capability_sources = deepcopy(args[3])
    if single_selected:
        p.capability_sources.insert(0, {'artifact': str(tmp_path / 'unselected.xlsx'), 'sha256': 'e' * 64})
    p.capability_sources_validated_generation = p.capability_source_generation
    p.control.configure(values=['自动', '无来源'] + [ref['artifact'] for ref in p.capability_sources])
    p.control.current(3 if single_selected else 0)
    p.text.insert('1.0', args[1])
    p.text.edit_modified(False)
    token = {'generation': p.daily_task_route_generation,
             'source_generation': p.capability_source_generation,
             'control_index': p.control.current(), 'preview_generation': p.capability_preview_generation,
             'instruction': args[1], 'history_task_id': None}
    opened = []
    monkeypatch.setattr('agent_service.office_scope_panel.open_file_selector',
                        lambda root, records, callback: opened.append((deepcopy(records), callback)))
    forbid_work(p, monkeypatch)
    p.messages.put(('daily_task_route', {'token': token, 'result': args[2]}, None))
    p.poll()
    assert p.mode.get() == MODES[9] and p.plan is None
    expected = args[2]['edit']['source_refs']
    assert opened[0][0] == [{'path': ref['artifact'], 'sha256': ref['sha256']} for ref in expected]
    assert p.text.get('1.0', 'end-1c') == args[1]
    # A scope selection remains usable after switching away from mode 11.
    inspected = {'files': [ref['artifact'] for ref in expected], 'artifact_refs': expected,
                 'scope': [{'path': ref['artifact'], 'locations': []} for ref in expected]}
    p.accept_scope(inspected)
    assert p.office_planning_files == tuple(inspected['files'])
