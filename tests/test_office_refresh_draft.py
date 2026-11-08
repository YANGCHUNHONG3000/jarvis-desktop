"""Local adapter refresh preserves only the original explicit draft binding."""
from copy import deepcopy

import pytest

from test_computer_goal_panel import hidden_root, panel
from test_instruction_draft_panel import DeferredThread, execution
from agent_service import computer_goal_panel as ui
from agent_service import office_edit_adapter_panel as adapter_panel


@pytest.fixture
def refresh(execution, monkeypatch, tmp_path):
    import tkinter
    from tkinter import ttk

    p, ticket, _plan, _result, seen = execution
    buttons = {}

    class Widget:
        def __init__(self, *_args, **kwargs):
            if 'command' in kwargs:
                buttons[kwargs['text']] = kwargs['command']

        def pack(self, **_kwargs):
            pass

        def title(self, *_args):
            pass

        def destroy(self):
            pass

    monkeypatch.setattr(tkinter, 'Toplevel', Widget)
    monkeypatch.setattr(ttk, 'Label', Widget)
    monkeypatch.setattr(ttk, 'Button', Widget)
    monkeypatch.setattr(ui, 'describe', lambda _: 'literal edit preview')
    plan = {'mode': 'agent', 'instruction': ticket['text'], 'sha256': 'b' * 64,
            'goal': {'kind': 'interactive', 'browser': None, 'edits': [{}]}}
    p.show_plan(plan, instruction_ticket=ticket)
    state = {'candidate': 'c' * 64, 'offline': True, 'reviewed': True,
             'trial_passed': False, 'remaining': 0}
    monkeypatch.setattr(adapter_panel.adapter, 'DEFAULT_STORE', tmp_path)
    monkeypatch.setattr(adapter_panel.adapter, 'current_candidate', lambda: 'c' * 64)
    monkeypatch.setattr(adapter_panel.adapter, 'ensure_candidate', lambda *_a, **_k: state)
    fresh = dict(deepcopy(plan), sha256='d' * 64)
    calls = []
    monkeypatch.setattr(adapter_panel.adapter, 'refresh_plan',
                        lambda *_a: calls.append('refresh') or deepcopy(fresh))
    monkeypatch.setattr(adapter_panel.adapter, 'trial', lambda *_a, **_k: {'outcome': {
        'ok': True, 'state': 'verified', 'code': 'objective_verified', 'mode': 'adapters',
        'artifacts': [], 'completed': [], 'run': 'verified-refresh-trial'}})
    return p, ticket, plan, fresh, seen, buttons, calls


def _open_and_refresh(p, buttons):
    adapter_panel.open_panel(p)
    buttons['刷新原差异预览（不调用模型）']()


@pytest.mark.parametrize('bound', [True, False])
def test_refresh_and_trial_only_clear_original_explicit_binding(refresh, bound):
    p, ticket, plan, fresh, seen, buttons, calls = refresh
    if not bound:
        p.show_plan(plan)
    _open_and_refresh(p, buttons)
    DeferredThread.finish()
    p.poll()
    assert calls == ['refresh'] and p.plan == fresh
    assert p.instruction_ticket(fresh) == (ticket if bound else None)
    assert p.instruction_draft.unchanged(ticket) and seen == []
    adapter_panel.open_panel(p)
    buttons['批准当前差异并试运行']()
    DeferredThread.finish()
    p.poll()
    if bound:
        assert p.text.get('1.0', 'end-1c') == '' and seen == [ticket]
    else:
        assert p.instruction_draft.unchanged(ticket) and seen == []


@pytest.mark.parametrize('change', ['draft', 'roundtrip', 'plan', 'binding',
                                    'closed', 'running', 'external_busy'])
def test_old_refresh_window_rejects_changed_or_unavailable_parent(refresh, change):
    p, ticket, plan, _fresh, seen, buttons, calls = refresh
    adapter_panel.open_panel(p)
    if change in {'draft', 'roundtrip'}:
        p.text.replace('1.0', 'end-1c', 'next draft')
        if change == 'roundtrip':
            p.text.replace('1.0', 'end-1c', ticket['text'])
    elif change == 'plan':
        p.show_plan(dict(plan, sha256='e' * 64), instruction_ticket=ticket)
    elif change == 'binding':
        p.plan_instruction_binding = None
    elif change == 'external_busy':
        p.external_busy = lambda: True
    else:
        setattr(p, change, True)
    current = p.instruction_draft.snapshot()
    buttons['刷新原差异预览（不调用模型）']()
    assert DeferredThread.pending == [] and calls == [] and seen == []
    assert p.instruction_draft.unchanged(current)


@pytest.mark.parametrize('roundtrip', [False, True])
def test_refresh_result_after_draft_revision_is_discarded(refresh, roundtrip):
    p, ticket, _plan, _fresh, seen, buttons, calls = refresh
    _open_and_refresh(p, buttons)
    p.text.configure(state='normal')
    p.text.replace('1.0', 'end-1c', 'new while refreshing')
    if roundtrip:
        p.text.replace('1.0', 'end-1c', ticket['text'])
    current = p.instruction_draft.snapshot()
    DeferredThread.finish()
    p.poll()
    assert calls == ['refresh'] and p.plan is None and seen == []
    assert p.instruction_draft.unchanged(current)


@pytest.fixture
def historical_refresh(refresh, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_service import computer

    p, _ticket, plan, _fresh, _seen, _buttons, _calls = refresh
    candidate_root = tmp_path / ('c' * 64)
    candidate_root.mkdir()
    (candidate_root / 'trial-intent.json').touch()
    state = {'candidate': 'c' * 64, 'offline': True, 'reviewed': True,
             'trial_passed': True, 'remaining': 0}
    monkeypatch.setattr(adapter_panel.adapter, 'status', lambda *_a: state)
    monkeypatch.setattr(adapter_panel.adapter, 'reconcile', lambda *_a: None)
    monkeypatch.setattr(computer, 'ComputerSession',
                        lambda *_a, **_k: SimpleNamespace(root=tmp_path / 'history-session'))
    monkeypatch.setattr(computer, 'read_record', lambda path:
        {'session_root': 'historical-session', 'plan_sha256': plan['sha256']}
        if path.name == 'trial-intent.json' else deepcopy(plan))
    return refresh


@pytest.mark.parametrize('parent_plan', [None, {'mode': 'other', 'sha256': 'f' * 64}])
def test_historical_refresh_previews_without_binding_or_consuming_current_draft(
        historical_refresh, parent_plan):
    p, ticket, _plan, fresh, seen, buttons, calls = historical_refresh
    p.show_plan(parent_plan)
    # Even a mistakenly retained same-text binding must not be inherited from history.
    original_binding = p.instruction_ticket
    p.instruction_ticket = lambda _plan: dict(ticket)
    adapter_panel.open_panel(p)
    p.instruction_ticket = original_binding
    buttons['刷新原差异预览（不调用模型）']()
    DeferredThread.finish()
    p.poll()
    assert calls == ['refresh'] and p.plan == fresh
    assert p.instruction_ticket(fresh) is None
    adapter_panel.open_panel(p)
    buttons['批准当前差异并试运行']()
    DeferredThread.finish()
    p.poll()
    assert p.instruction_draft.unchanged(ticket) and seen == []


@pytest.mark.parametrize('change', ['plan', 'draft', 'roundtrip'])
def test_historical_refresh_rejects_revision_since_open(historical_refresh, change):
    p, ticket, _plan, _fresh, seen, buttons, calls = historical_refresh
    p.show_plan(None)
    adapter_panel.open_panel(p)
    if change == 'plan':
        p.show_plan({'mode': 'other'})
    else:
        p.text.replace('1.0', 'end-1c', 'changed')
        if change == 'roundtrip':
            p.text.replace('1.0', 'end-1c', ticket['text'])
    current = p.instruction_draft.snapshot()
    buttons['刷新原差异预览（不调用模型）']()
    assert not DeferredThread.pending and calls == [] and seen == []
    assert p.instruction_draft.unchanged(current)


def test_historical_window_cannot_directly_execute_loaded_plan(historical_refresh):
    p, ticket, _plan, _fresh, seen, buttons, calls = historical_refresh
    p.show_plan(None)
    adapter_panel.open_panel(p)
    buttons['批准当前差异并试运行']()
    assert not DeferredThread.pending and calls == [] and seen == []
    assert p.instruction_draft.unchanged(ticket)
    assert '失效' in p.status.get()
