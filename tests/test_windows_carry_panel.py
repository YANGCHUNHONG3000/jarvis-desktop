"""Completed inherited copies remain usable after GUI restart, without replay."""
from copy import deepcopy
import pytest

from agent_service import windows_handoff as handoff, windows_task as wt
from agent_service import windows_file_dialog as files
from test_windows_panel import panel, hidden_root  # noqa: F401
from test_windows_handoff import saved, prepared, native, child_goal  # noqa: F401
from test_windows_handoff_panel import setup_parent
from test_windows_task import Backend, control, condition
from types import SimpleNamespace
from agent_service.computer import ComputerError
from agent_service.windows_panel import window_for_copy


def restored_child(panel, saved, monkeypatch, tmp_path, *, finish=True):
    p = setup_parent(panel, saved, monkeypatch)
    goal = child_goal(saved[2]['final'])
    ref = handoff.capture(saved[0], 'save', target_id='open')
    child = wt.create(tmp_path/'child', goal, sources=[ref])
    backend = Backend(control(**files.OPEN_DOCUMENT_SELECTOR, value=goal['conditions'][0]['value']),
                      control('Other_application'))
    wt.run(child, backend, pause_after_step=not finish)
    p.messages.put(('restore', wt.load(child), None)); p.poll()
    return p, child


def test_completed_open_is_offered_and_persisted_after_restore(panel, saved, monkeypatch, tmp_path):
    p, child = restored_child(panel, saved, monkeypatch, tmp_path)
    assert len(p.saved_choices) == 1 and p.saved_choices[0]['id'] == 'open'
    assert '已核验接续副本' in p.copy_choice.get()
    from pathlib import Path
    assert Path(saved[2]['final']['path']).name in p.copy_choice.get()
    before = wt._inventory(child)
    p.next_copy(); p.poll()
    assert len(p.conditions) == len(p.sources) == 1
    assert p.sources[0]['kind'] == 'carried' and p.sources[0]['parent_root'] == str(child)
    assert files.decode_token(p.conditions[0]['value']) == saved[2]['final']
    assert p.task_root is None and str(p.execute_button.cget('state')) == 'disabled'
    p.task_base = tmp_path/'next'
    p.preview(); p.poll()
    loaded = wt.load(p.task_root)
    assert loaded['sources'] == p.sources
    assert wt._inventory(child) == before


def test_repeated_identical_open_handoff_keeps_staged_goal_and_refuses_duplicate(panel, saved, monkeypatch, tmp_path):
    p, _ = restored_child(panel, saved, monkeypatch, tmp_path)
    p.next_copy(); p.poll()
    before = deepcopy((p.conditions, p.sources))
    p.next_copy(); p.poll()
    assert (p.conditions, p.sources) == before and '已在当前草稿' in p.status.get()


def test_carried_handoff_keeps_new_instruction_and_new_goals_only(panel, saved, monkeypatch, tmp_path):
    p, _ = restored_child(panel, saved, monkeypatch, tmp_path)
    p.conditions.append(condition('New_work'))
    p.instruction_var.set('My next work')
    p.next_copy(); p.poll()
    assert [c['id'] for c in p.conditions] == ['new_work', 'target_1']
    assert p.instruction.get() == 'My next work'
    assert len(p.sources) == 1


def test_pending_intermediate_does_not_hide_remaining_steps_or_keep_previous_menu(panel, saved, monkeypatch, tmp_path):
    p, child = restored_child(panel, saved, monkeypatch, tmp_path, finish=False)
    assert wt.inspect(child)['state'] == 'paused'
    assert not p.saved_choices and p.saved_task is None and not p.copy_choice.get()
    before = deepcopy((p.conditions, p.sources))
    p.next_copy()
    assert (p.conditions, p.sources) == before


@pytest.mark.parametrize('state', ['unknown', 'ready'])
def test_restoring_unrelated_task_clears_prior_source_menu(panel, saved, monkeypatch, state):
    p = setup_parent(panel, saved, monkeypatch)
    assert p.saved_choices
    p.sources = []
    p.conditions = [condition('Unrelated')]
    p.remember_saved_task({'state': state, 'completed': []})
    assert not p.saved_choices and p.saved_task is None
    assert not p.copy_choice.get() and not p.copy_choice.cget('values')


def test_late_carried_result_cannot_replace_new_draft(panel, saved, monkeypatch, tmp_path):
    p, _ = restored_child(panel, saved, monkeypatch, tmp_path)
    old = deepcopy((p.conditions, p.sources))
    p.next_copy()
    p.instruction_var.set('Keep late new draft')
    p.poll()
    assert (p.conditions, p.sources) == old
    assert p.instruction.get() == 'Keep late new draft' and '迟到' in p.status.get()


@pytest.mark.parametrize('scenario', ['original', 'restarted', 'ambiguous', 'absent', 'chosen', 'stale_chosen', 'wrong_format', 'explorer'])
def test_copy_draft_uses_only_current_compatible_window(monkeypatch, scenario):
    from agent_service import windows_access
    from test_windows_panel import WORD_WINDOW, WINDOW
    old = deepcopy(WORD_WINDOW)
    new = dict(old, hwnd=101, pid=201, created='new', title='new WPS')
    other = dict(new, hwnd=102)
    rows = {'original': [old, new], 'restarted': [new], 'ambiguous': [new, other],
            'absent': [], 'chosen': [new, other], 'stale_chosen': [other], 'wrong_format': [new], 'explorer': [new]}[scenario]
    monkeypatch.setattr(windows_access, '_kind', lambda window: 'wps')
    monkeypatch.setattr(windows_access, '_file_application', lambda window, path: scenario != 'wrong_format')
    backend = SimpleNamespace(list_windows=lambda: rows)
    preferred = new if scenario in {'chosen', 'stale_chosen', 'wrong_format'} else None
    if scenario == 'explorer':
        preferred = WINDOW
    if scenario in {'ambiguous', 'absent', 'stale_chosen', 'wrong_format'}:
        with pytest.raises(ComputerError, match='windows_handoff_window_'):
            window_for_copy(backend, old, 'file.xlsx', preferred=preferred)
    else:
        assert window_for_copy(backend, old, 'file.xlsx', preferred=preferred) == (old if scenario == 'original' else new)
