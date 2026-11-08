"""Persisted handoff and real Tk draft protection, with no app/API calls."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest

from agent_service import windows_task as wt, windows_handoff as handoff, windows_panel as wp
from agent_service import windows_file_dialog as files
from agent_service.computer import ComputerError
from test_windows_panel import panel, hidden_root  # noqa: F401
from test_windows_handoff import saved, prepared, native, child_goal  # noqa: F401
from test_windows_task import Backend, control


def setup_parent(panel, saved, monkeypatch):
    p, _ = panel
    root, _, _ = saved
    p.messages.put(('restore', wt.load(root), None))
    p.poll()
    from agent_service import windows_access
    monkeypatch.setattr(windows_access, '_kind', lambda window: 'wps')
    monkeypatch.setattr(windows_access, '_file_application', lambda window, path: True)
    backend = SimpleNamespace(list_windows=lambda: [deepcopy(p.saved_goal['conditions'][0]['window'])])
    monkeypatch.setattr(p, 'start', lambda kind, fn: p.messages.put((kind, fn(backend), None)))
    return p


def test_handoff_clears_completed_conditions_and_preserves_parent(panel, saved, monkeypatch):
    p = setup_parent(panel, saved, monkeypatch)
    before = wt._inventory(saved[0])
    p.next_copy(); p.poll()
    assert len(p.conditions) == len(p.sources) == 1 and p.task_root is None
    assert files.decode_token(p.conditions[0]['value']) == saved[2]['final']
    assert p.conditions[0]['selector'] == files.OPEN_DOCUMENT_SELECTOR
    assert p.instruction.get() != wt.load_goal(saved[0])['instruction']
    assert str(p.execute_button.cget('state')) == 'disabled'
    assert wt._inventory(saved[0]) == before


@pytest.mark.parametrize('same_text', [False, True])
def test_user_new_or_retyped_instruction_survives_handoff(panel, saved, monkeypatch, same_text):
    p = setup_parent(panel, saved, monkeypatch)
    draft = p.instruction.get() if same_text else 'Keep my new cross-app instructions'
    p.instruction_var.set(draft)
    p.next_copy();p.poll()
    assert p.instruction.get() == draft and len(p.sources) == 1


def test_late_result_does_not_overwrite_new_instruction(panel, saved, monkeypatch):
    p = setup_parent(panel, saved, monkeypatch)
    old = deepcopy(p.conditions)
    p.next_copy()
    p.instruction_var.set('Written while receipt verification finished')
    p.poll()
    assert p.conditions == old and not p.sources
    assert p.instruction.get() == 'Written while receipt verification finished'
    assert '迟到' in p.status.get()


def test_existing_draft_goal_is_kept_and_duplicate_handoff_refused(panel, saved, monkeypatch):
    p = setup_parent(panel, saved, monkeypatch)
    from test_windows_task import condition
    p.conditions = [condition('New_draft')]
    p.instruction_var.set('new draft')
    p.next_copy();p.poll()
    assert len(p.conditions) == 2 and p.conditions[0]['id'] == 'new_draft'
    before = deepcopy(p.conditions)
    p.next_copy();p.poll()
    assert p.conditions == before and '已在当前草稿' in p.status.get()


def test_appended_new_goal_does_not_replay_any_parent_condition(panel, saved, monkeypatch):
    p = setup_parent(panel, saved, monkeypatch)
    from test_windows_task import condition
    old = deepcopy(p.conditions)
    p.conditions.extend([condition('New_draft')])
    p.conditions[0]['retain'] = not p.conditions[0]['retain']
    p.instruction_var.set('New cross-app task')
    p.next_copy();p.poll()
    assert len(p.conditions)==2 and p.conditions[0]['id']=='new_draft'
    assert all(c['selector']!=old[0]['selector'] for c in p.conditions)
    assert p.instruction.get()=='New cross-app task'


def test_bound_preview_round_trips_and_missing_source_blocks_before_native_effect(saved, tmp_path):
    root, _, receipt = saved
    goal = child_goal(receipt['final'])
    ref = handoff.capture(root, 'save', target_id='open')
    child = wt.create(tmp_path/'next', goal, sources=[ref])
    loaded = wt.load(child)
    assert loaded['sources'] == [ref] and loaded['goal'] == goal
    assert wt._load(child/'task.json')[0]['schema'] == 2
    p = Path(receipt['final']['path']);p.write_bytes(p.read_bytes()+b'changed')
    backend = Backend()
    with pytest.raises(ComputerError):wt.run(child, backend)
    assert backend.observations == 0 and not backend.calls and not list((child/'attempts').iterdir())


def test_bound_child_resume_only_executes_remaining_goals(saved, tmp_path):
    parent, _, receipt = saved
    goal = child_goal(receipt['final'])
    ref = handoff.capture(parent, 'save', target_id='open')
    child = wt.create(tmp_path/'next', goal, sources=[ref])
    backend = Backend(control(**files.OPEN_DOCUMENT_SELECTOR, value=goal['conditions'][0]['value']),
                      control('Other_application'))
    before = wt._inventory(parent)
    result = wt.run(child, backend, pause_after_step=True)
    assert result['completed'] == ['open'] and not backend.calls
    first = wt._inventory(child/'attempts/0001')
    assert wt.run(child, backend)['ok']
    assert len(backend.calls)==1 and backend.calls[0][0]['name']=='Other_application'
    assert wt._inventory(child/'attempts/0001')==first and wt._inventory(parent)==before


def test_restored_handoff_retains_sources(panel, saved, monkeypatch, tmp_path):
    p = setup_parent(panel, saved, monkeypatch)
    p.next_copy();p.poll()
    p.task_base=tmp_path/'next'
    p.preview();p.poll()
    loaded = wt.load(p.task_root)
    p.sources=[]
    p.messages.put(('restore', loaded, None));p.poll()
    assert p.sources == loaded['sources'] and p.sources
    p.remove_last();assert not p.sources
