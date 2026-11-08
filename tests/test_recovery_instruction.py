"""Recovery draft consumption checks local plans/receipts, without UI/API I/O."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from agent_service import recovery_instruction as helper
from agent_service.computer import digest, write_new


class Draft:
    def __init__(self, text):
        self.text, self.revision = text, 1

    def snapshot(self):
        return {'text': self.text, 'revision': self.revision}

    def unchanged(self, ticket):
        return ticket == self.snapshot()

    def change(self, text):
        self.text, self.revision = text, self.revision + 1

    def complete(self, ticket):
        if not self.unchanged(ticket):
            return False
        self.change('')
        return True


@pytest.fixture
def state(tmp_path):
    session = tmp_path / 'session'
    goals = session / 'goals'
    goals.mkdir(parents=True)
    instruction = '只续剩余Excel，保留已完成Word。'
    edits = [{'id': f'e{n}', 'source_root': str(session / 'edit-inputs' / f'source{n}'),
              'after_browser': False, 'spec': {
                  'kind': 'spreadsheet', 'source_id': 'selected-edit', 'source_sha256': str(n) * 64,
                  'changes': [{'sheet': '预算', 'cell': 'B2', 'before': n, 'after': n + 10}]}}
             for n in (1, 2)]
    plan = {'schema': 3, 'mode': 'agent', 'instruction': instruction,
            'goal': {'kind': 'interactive', 'browser': None, 'edits': edits},
            'max_actions': 12, 'max_seconds': 360, 'prepared_at': 1,
            'recovery': {'version': 1, 'parent_run': str(goals / ('a' * 64 + '.run')),
                         'parent_evidence_sha256': 'b' * 64, 'depth': 1,
                         'completed_edits': ['e1'], 'remaining_edits': ['e2']}}
    plan['sha256'] = digest(plan)
    plan_file = goals / (plan['sha256'] + '.json')
    write_new(plan_file, plan)
    run = goals / (plan['sha256'] + '.run')
    run.mkdir()
    records = [{'path': str(tmp_path / 'first.xlsx'), 'sha256': 'c' * 64},
               {'path': str(tmp_path / 'second.xlsx'), 'sha256': 'd' * 64}]
    claim = {'approval': plan['sha256'], 'mode': 'agent'}
    outcome = {'ok': True, 'state': 'verified', 'code': 'objective_verified',
               'engine': 'observed_agent', 'run': str(run), 'artifacts': deepcopy(records),
               'recovery': deepcopy(plan['recovery'])}
    write_new(run / 'claim.json', claim)
    write_new(run / 'outcome.json', outcome)
    parent = SimpleNamespace(plan=deepcopy(plan), session=SimpleNamespace(root=session),
        instruction_draft=Draft(instruction), closed=False, running=False,
        stopped=threading.Event(), external_busy=lambda: False, completions=[])
    def complete(ticket, kind, result):
        parent.completions.append((ticket, kind, result))
        parent.instruction_draft.complete(ticket)
    parent.complete_instruction = complete
    return SimpleNamespace(parent=parent, records=records, run=run, plan_file=plan_file,
                           plan=plan, claim=claim, outcome=outcome)


def rewrite(path, value):
    import json
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def test_exact_completed_recovery_consumes_once_after_panel_plan_invalidated(state):
    binding = helper.capture(state.parent)
    assert binding is not None
    state.parent.plan = None  # GoalPanel.poll invalidates before on_artifacts.
    assert helper.complete(state.parent, binding, list(reversed(state.records)))
    assert state.parent.instruction_draft.snapshot()['text'] == ''
    assert len(state.parent.completions) == 1
    assert state.parent.completions[0][1:] == ('execute', state.outcome)
    assert not helper.complete(state.parent, binding, state.records)
    assert len(state.parent.completions) == 1


@pytest.mark.parametrize('change', ['no_plan', 'not_recovery', 'browser', 'draft', 'missing_plan',
    'changed_plan', 'session', 'stop', 'closed', 'running', 'busy', 'invalid_ticket'])
def test_capture_refuses_unbound_or_inactive_draft(state, change):
    parent = state.parent
    if change == 'no_plan': parent.plan = None
    elif change == 'not_recovery': parent.plan.pop('recovery')
    elif change == 'browser': parent.plan['goal']['browser'] = {'start_url': 'https://example.org/'}
    elif change == 'draft': parent.instruction_draft.change('新草稿')
    elif change == 'missing_plan': state.plan_file.unlink()
    elif change == 'changed_plan': rewrite(state.plan_file, {'different': True})
    elif change == 'session': parent.session = None
    elif change == 'stop': parent.stopped.set()
    elif change == 'closed': parent.closed = True
    elif change == 'running': parent.running = True
    elif change == 'busy': parent.external_busy = lambda: True
    else: parent.instruction_draft.revision = True
    before = parent.instruction_draft.snapshot()
    assert helper.capture(parent) is None
    assert parent.instruction_draft.snapshot() == before and not parent.completions


@pytest.mark.parametrize('change', ['new_draft', 'changed_back', 'different_tracker', 'different_session',
    'different_parent', 'new_plan', 'stop', 'closed', 'running', 'busy', 'disk_plan', 'claim', 'wrong_claim_mode',
    'missing_outcome', 'failed', 'paused', 'wrong_code', 'wrong_engine', 'wrong_run', 'wrong_recovery',
    'different_output', 'wrong_sha', 'partial_outputs', 'duplicate_outputs', 'outcome_outputs'])
def test_completion_refuses_every_unmatched_success_without_touching_draft(state, change):
    parent = state.parent
    binding = helper.capture(parent)
    assert binding is not None
    records = deepcopy(state.records)
    if change == 'new_draft': parent.instruction_draft.change('新草稿')
    elif change == 'changed_back':
        parent.instruction_draft.change('新草稿')
        parent.instruction_draft.change(state.plan['instruction'])
    elif change == 'different_tracker': parent.instruction_draft = Draft(state.plan['instruction'])
    elif change == 'different_session': parent.session = SimpleNamespace(root=state.run.parent.parent / 'other')
    elif change == 'different_parent': parent = SimpleNamespace(**vars(parent))
    elif change == 'new_plan': parent.plan = {'new': True}
    elif change == 'stop': parent.stopped.set()
    elif change == 'closed': parent.closed = True
    elif change == 'running': parent.running = True
    elif change == 'busy': parent.external_busy = lambda: True
    elif change == 'disk_plan': rewrite(state.plan_file, {'different': True})
    elif change == 'claim': rewrite(state.run / 'claim.json', {'approval': 'f' * 64})
    elif change == 'wrong_claim_mode':
        rewrite(state.run / 'claim.json', {**state.claim, 'mode': 'capability'})
    elif change == 'missing_outcome': (state.run / 'outcome.json').unlink()
    elif change in ('failed', 'paused', 'wrong_code', 'wrong_engine', 'wrong_run', 'wrong_recovery', 'outcome_outputs'):
        outcome = deepcopy(state.outcome)
        if change == 'failed': outcome.update(ok=False, state='unknown')
        elif change == 'paused': outcome.update(ok=False, state='partial', code='office_paused_after_verified_step')
        elif change == 'wrong_code': outcome['code'] = 'approved_plan_verified'
        elif change == 'wrong_engine': outcome['engine'] = 'other'
        elif change == 'wrong_run': outcome['run'] = str(state.run.parent / 'other.run')
        elif change == 'wrong_recovery': outcome['recovery']['remaining_edits'] = []
        else: outcome['artifacts'] = outcome['artifacts'][:1]
        rewrite(state.run / 'outcome.json', outcome)
    elif change == 'different_output': records[0]['path'] = str(state.run / 'other.xlsx')
    elif change == 'wrong_sha': records[0]['sha256'] = 'f' * 64
    elif change == 'partial_outputs': records = records[:1]
    else: records = [records[0], records[0]]
    before = parent.instruction_draft.snapshot()
    assert not helper.complete(parent, binding, records)
    assert parent.instruction_draft.snapshot() == before and not parent.completions


def test_invalid_binding_and_missing_success_do_not_consume_ticket(state):
    before = state.parent.instruction_draft.snapshot()
    assert not helper.complete(state.parent, None, state.records)
    binding = helper.capture(state.parent)
    assert not helper.complete(state.parent, binding, [])
    assert state.parent.instruction_draft.snapshot() == before
    assert helper.complete(state.parent, binding, state.records)


@pytest.mark.parametrize('edit', ['unchanged', 'new_draft', 'changed_back', 'undo_back'])
def test_real_tk_instruction_draft_retains_revision_identity(state, edit):
    import tkinter as tk
    from agent_service.computer_goal_panel import GoalPanel
    from agent_service.instruction_draft import InstructionDraft
    root = tk.Tk()
    root.withdraw()
    widget = tk.Text(root, undo=True, autoseparators=False)
    tracker = InstructionDraft(widget)
    try:
        tracker.adopt(state.plan['instruction'])
        widget.edit_reset()
        parent = state.parent
        parent.text, parent.instruction_draft = widget, tracker
        notifications = []
        parent.on_instruction_complete = notifications.append
        parent.complete_instruction = lambda ticket, kind, result: GoalPanel.complete_instruction(
            parent, ticket, kind, result)
        binding = helper.capture(parent)
        assert binding is not None
        if edit != 'unchanged':
            widget.replace('1.0', 'end-1c', 'new draft')
            if edit == 'changed_back':
                widget.replace('1.0', 'end-1c', state.plan['instruction'])
            elif edit == 'undo_back':
                widget.edit_undo()
        before = tracker.snapshot()
        parent.plan = None
        assert helper.complete(parent, binding, state.records) is (edit == 'unchanged')
        if edit == 'unchanged':
            assert widget.get('1.0', 'end-1c') == '' and notifications == [binding.ticket]
        else:
            assert tracker.snapshot() == before and not notifications
    finally:
        tracker.close()
        root.destroy()
