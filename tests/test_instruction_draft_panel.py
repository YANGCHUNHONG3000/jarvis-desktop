"""Real Tk draft identity through bound planning and execution; no external effects."""
from copy import deepcopy

import pytest

from test_computer_goal_panel import hidden_root, panel
from agent_service import computer_goal_panel as ui
from agent_service.computer import digest


class DeferredThread:
    pending = []

    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.pending.append(self)

    @classmethod
    def finish(cls):
        cls.pending.pop(0).target()


@pytest.fixture
def execution(panel, monkeypatch):
    p, _ = panel
    DeferredThread.pending = []
    monkeypatch.setattr(ui.threading, 'Thread', DeferredThread)
    p.session = object()
    ticket = p.instruction_draft.adopt('只执行这一条完整指令')
    p.text.edit_modified(False)
    plan = {'schema': 1, 'mode': 'capability', 'instruction': ticket['text'],
            'source_ref': None, 'candidates': [], 'request_id': 'a' * 32, 'task_base': 'unused'}
    plan['sha256'] = digest(plan)
    p.show_plan(plan, instruction_ticket=ticket)
    seen = []
    p.on_instruction_complete = seen.append
    from agent_service import capability_router
    result = {'ok': True, 'state': 'succeeded', 'code': 'capability_verified',
              'artifacts': [], 'run': 'verified-test-result'}
    monkeypatch.setattr(capability_router, 'execute', lambda *_a, **_k: deepcopy(result))
    return p, ticket, plan, result, seen


def test_actual_execute_captures_ticket_before_invalidate_and_clears_only_on_success(execution):
    p, ticket, _plan, _result, seen = execution
    p.execute()
    assert p.plan is None and p.instruction_draft.unchanged(ticket) and p.running
    DeferredThread.finish()
    p.poll()
    assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]
    assert not p.running


@pytest.mark.parametrize('change', ['failed', 'paused', 'prepared', 'needs_input',
                                    'partial_completed', 'unknown', 'missing_state', 'wrong_code'])
def test_nonterminal_or_failed_results_never_consume_draft(execution, change):
    p, ticket, _plan, result, seen = execution
    if change == 'failed':
        result.update(ok=False, state='failed', code='fixture_failed')
    elif change == 'missing_state':
        result.pop('state')
    elif change == 'wrong_code':
        result['code'] = 'office_paused_after_verified_step'
    else:
        result['state'] = change
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


def test_stop_preserves_submitted_draft_even_when_worker_returns_success(execution):
    p, ticket, _plan, _result, seen = execution
    p.execute()
    p.stop()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


@pytest.mark.parametrize('return_to_same', [False, True])
def test_new_draft_survives_late_success_and_still_notifies_original_owner(execution, return_to_same):
    p, ticket, _plan, _result, seen = execution
    p.execute()
    p.text.configure(state='normal')
    p.text.replace('1.0', 'end-1c', '用户新写的下一条指令')
    if return_to_same:
        p.text.replace('1.0', 'end-1c', ticket['text'])
    newer = p.instruction_draft.snapshot()
    p.text.edit_modified(False)
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(newer) and seen == [ticket]


def test_artifact_handoff_failure_does_not_prevent_instruction_completion(execution):
    p, ticket, _plan, result, seen = execution
    result['artifacts'] = [{'path': 'not-opened.docx', 'sha256': 'f' * 64}]
    p.on_artifacts = lambda _: (_ for _ in ()).throw(ValueError('handoff failed'))
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]
    assert '自动接力未完成' in p.status.get()


def test_unknown_show_plan_cannot_bind_current_same_text(execution):
    p, ticket, plan, _result, seen = execution
    p.show_plan(plan)
    assert p.instruction_ticket(plan) is None
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


def test_recovery_never_binds_same_current_instruction(execution):
    p, ticket, plan, _result, seen = execution
    old = dict(plan, recovery={'parent_run': 'old'})
    p.show_plan(old, instruction_ticket=ticket)
    assert p.instruction_ticket(old) is None
    p.execute()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


def test_edited_then_restored_draft_invalidates_approval_without_worker(execution):
    p, ticket, _plan, _result, _seen = execution
    p.text.replace('1.0', 'end-1c', 'other')
    p.text.replace('1.0', 'end-1c', ticket['text'])
    p.execute()
    assert p.plan is None and not DeferredThread.pending
    assert p.text.get('1.0', 'end-1c') == ticket['text']


def test_late_propose_after_same_loop_edit_roundtrip_is_discarded(execution, monkeypatch):
    p, ticket, plan, _result, seen = execution
    p.mode.set(ui.MODES[9])
    p.office_planning_files = ('not-read.docx',)
    from agent_service import office_intent
    monkeypatch.setattr(office_intent, 'propose',
                        lambda *_a, **_k: {'plan': plan, 'reason': 'prepared'})
    p.propose()
    p.text.configure(state='normal')
    p.text.replace('1.0', 'end-1c', 'new')
    p.text.replace('1.0', 'end-1c', ticket['text'])
    p.text.edit_modified(False)
    newer = p.instruction_draft.snapshot()
    DeferredThread.finish()
    p.poll()
    assert p.plan is None and p.instruction_draft.unchanged(newer) and seen == []


def test_matching_async_propose_binds_but_does_not_complete_draft(execution, monkeypatch):
    p, ticket, plan, _result, seen = execution
    p.mode.set(ui.MODES[9])
    p.office_planning_files = ('not-read.docx',)
    from agent_service import office_intent
    monkeypatch.setattr(office_intent, 'propose',
                        lambda *_a, **_k: {'plan': plan, 'reason': 'prepared'})
    p.propose()
    DeferredThread.finish()
    p.poll()
    assert p.plan == plan and p.instruction_ticket(plan) == ticket
    assert p.instruction_draft.unchanged(ticket) and seen == []


def test_closing_panel_preserves_text_and_restores_tracker(execution):
    p, ticket, _plan, _result, seen = execution
    p.execute()
    p.close()
    DeferredThread.finish()
    p.poll()
    assert p.text.get('1.0', 'end-1c') == ticket['text'] and seen == []
    assert not hasattr(p.text, '_instruction_draft_tracker')


@pytest.mark.parametrize('kind,state,code', [
    ('execute', 'verified', 'objective_verified'),
    ('execute', 'verified', 'approved_plan_verified'),
    ('office_bundle_execute', 'succeeded', None),
    ('repository_document', 'verified', 'repository_document_verified'),
])
def test_explicit_other_execution_terminal_contracts_consume_bound_instruction(execution, kind, state, code):
    p, ticket, _plan, _result, seen = execution
    # Worker result identity comes from the submitted operation's local envelope.
    # Backend receipt verification is independently covered by its own tests.
    result = {'ok': True, 'state': state, 'code': code, 'completed': [],
              'mode': 'adapters', 'artifacts': [], 'run': 'verified-result'}
    if kind == 'office_bundle_execute':
        result.update(positions=1, differences=0)
    p.messages.put((kind, result, None, ticket))
    p.poll()
    assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]


@pytest.mark.parametrize('change', ['none', 'plan', 'edit_roundtrip'])
def test_adapter_trial_only_consumes_current_explicitly_bound_plan(
        execution, monkeypatch, tmp_path, change):
    p, ticket, _plan, _result, seen = execution
    from agent_service import office_edit_adapter_panel as adapter_panel
    import tkinter
    from tkinter import ttk
    buttons = []
    class Widget:
        def __init__(self, *_a, **kwargs):
            if 'command' in kwargs:
                buttons.append(kwargs)
        def pack(self, **_kwargs):
            pass
        def title(self, *_args):
            pass
        def destroy(self):
            pass
    monkeypatch.setattr(tkinter, 'Toplevel', Widget)
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    monkeypatch.setattr(ui, 'describe', lambda _: 'approved literal edits')
    edit_plan = {'mode': 'agent', 'instruction': ticket['text'], 'sha256': 'b' * 64,
                 'goal': {'kind': 'interactive', 'browser': None, 'edits': [{}]}}
    p.show_plan(edit_plan, instruction_ticket=ticket)
    state = {'candidate': 'c' * 64, 'offline': True, 'reviewed': True,
             'trial_passed': False, 'remaining': 0}
    monkeypatch.setattr(adapter_panel.adapter, 'DEFAULT_STORE', tmp_path)
    monkeypatch.setattr(adapter_panel.adapter, 'current_candidate', lambda: 'c' * 64)
    monkeypatch.setattr(adapter_panel.adapter, 'ensure_candidate', lambda *_a, **_k: state)
    monkeypatch.setattr(adapter_panel.adapter, 'trial', lambda *_a, **_k: {'outcome': {
        'ok': True, 'state': 'verified', 'code': 'objective_verified', 'mode': 'adapters',
        'artifacts': [], 'completed': [], 'run': 'verified-trial'}})
    adapter_panel.open_panel(p)
    if change == 'plan':
        p.show_plan(dict(edit_plan, sha256='d' * 64), instruction_ticket=ticket)
    elif change == 'edit_roundtrip':
        p.text.replace('1.0', 'end-1c', 'new draft')
        p.text.replace('1.0', 'end-1c', ticket['text'])
    next(button['command'] for button in buttons
         if button['text'] == '批准当前差异并试运行')()
    if change != 'none':
        assert DeferredThread.pending == [] and seen == []
        assert p.text.get('1.0', 'end-1c') == ticket['text']
        assert '失效' in p.status.get()
    else:
        DeferredThread.finish()
        p.poll()
        assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]
