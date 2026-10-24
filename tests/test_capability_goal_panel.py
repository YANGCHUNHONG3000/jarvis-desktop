"""Hidden-Tk checks for the approved capability route; no network or desktop effects."""

import tkinter as tk
from tkinter import ttk

import pytest

from agent_service import capability_router, computer_goal_panel as ui
from agent_service.computer import ComputerSession


def test_main_stop_also_stops_development_trial(panel):
    from types import SimpleNamespace
    import threading
    state = SimpleNamespace(running=True, stop_event=threading.Event())
    panel.development_panel = state
    panel.stop()
    assert state.stop_event.is_set()
    panel.development_panel = None


@pytest.fixture(scope='module')
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(hidden_root):
    tabs = ttk.Notebook(hidden_root)
    p = ui.attach_goal_tab(tabs, hidden_root)
    yield p
    p.close()
    tabs.destroy()


def _mode(p, monkeypatch):
    monkeypatch.setattr(p, 'refresh_capability_sources', lambda: None)
    p.mode.set(ui.MODES[11])
    p.mode_changed()


def _text(p):
    return p.preview_box.get('1.0', 'end')


def test_mode11_uses_verified_source_list_and_defaults_to_task_selection(panel, monkeypatch):
    from agent_service import office_handoff

    p = panel
    captured = []
    inputs = [{'path': 'saved.docx', 'sha256': 'a' * 64}]
    refs = [{'artifact': 'saved.docx', 'sha256': 'a' * 64}]
    p.get_artifacts = lambda: inputs
    monkeypatch.setattr(office_handoff, 'latest_generated_outputs',
                        lambda **kw: pytest.fail('ignored caller-selected source'))
    monkeypatch.setattr(office_handoff, 'select_outputs', lambda records: refs if records is inputs
                        else pytest.fail('wrong source list'))
    monkeypatch.setattr(p, 'start', lambda kind, work: captured.append((kind, work)))
    p.mode.set(ui.MODES[11])
    p.mode_changed()
    assert captured[0][0] == 'capability_sources'
    p.messages.put(('capability_sources', captured[0][1](object()), None))
    p.poll()
    assert p.capability_sources == refs
    assert p.control.current() == 0
    assert '自动按任务' in p.control['values'][0]
    assert '无来源文件' in p.control['values'][1]
    assert 'saved.docx' in p.control['values'][2]
    assert str(p.propose_button['state']) == 'disabled'


def test_local_preview_uses_real_session_without_model_or_api(panel, tmp_path, monkeypatch):
    from agent_service import model

    p = panel
    _mode(p, monkeypatch)
    session = ComputerSession.create(tmp_path / 'sessions')
    p.session = session
    p.capability_sources = []
    p.control.configure(values=['自动按任务选择输入', '无来源文件'])
    p.control.current(1)
    monkeypatch.setattr(model, 'CloudClient', lambda **kw: pytest.fail('preview called API'))
    pending = []
    monkeypatch.setattr(p, 'start', lambda kind, work: pending.append((kind, work)))
    p.text.insert('1.0', '整理现有资料')
    p.text.edit_modified(False)
    p.preview()
    assert pending[0][0] == 'capability_preview'
    plan = pending[0][1](session)
    p.messages.put(('capability_preview', plan, None))
    p.poll()
    assert p.plan == plan and plan['source_ref'] is None
    assert str(p.execute_button['state']) == 'normal'
    assert not list((session.root / 'model_calls').iterdir())


def test_preview_binds_selected_source_and_approval_passes_exact_plan(panel, monkeypatch):
    p = panel
    _mode(p, monkeypatch)
    ref = {'artifact': 'selected.xlsx', 'sha256': 'b' * 64}
    p.capability_sources = [ref]
    p.control.configure(values=['自动按任务选择输入', '无来源文件', '1. selected.xlsx'])
    p.control.current(2)
    p.text.insert('1.0', '请按已有表格制作新的报告')
    p.text.edit_modified(False)
    calls = []
    plan = {'mode': 'capability', 'sha256': 'c' * 64, 'source_ref': ref,
            'instruction': '请按已有表格制作新的报告', 'candidates': []}
    monkeypatch.setattr(capability_router, 'prepare',
                        lambda session, instruction, *, source_ref: calls.append(
                            ('preview', session, instruction, source_ref)) or plan)
    monkeypatch.setattr(capability_router, 'execute',
                        lambda session, chosen, *, approval, stop: calls.append(
                            ('execute', session, chosen, approval, stop)) or
                            {'ok': False, 'code': 'capability_not_completed', 'run': 'saved', 'artifacts': []})
    pending = []
    monkeypatch.setattr(p, 'start', lambda kind, work: pending.append((kind, work)))
    p.preview()
    assert pending[0][0] == 'capability_preview'
    assert pending[0][1]('session') is plan
    assert calls[0] == ('preview', 'session', plan['instruction'], ref)
    p.messages.put(('capability_preview', plan, None))
    p.poll()
    p.execute()
    assert pending[1][0] == 'capability_execute'
    pending[1][1]('session')
    assert calls[1][:4] == ('execute', 'session', plan, plan['sha256'])
    assert calls[1][4]() is False


def test_stop_discards_late_capability_preview(panel, monkeypatch):
    p = panel
    _mode(p, monkeypatch)
    p.set_busy(True)
    p.stop()
    late = {'mode': 'capability', 'sha256': 'd' * 64, 'source_ref': None,
            'instruction': 'late', 'candidates': []}
    p.messages.put(('capability_preview', late, None))
    p.poll()
    assert p.plan is None and str(p.execute_button['state']) == 'disabled'
    assert '本地预览完成' not in p.status.get()


def test_auto_preview_passes_snapshot_of_candidate_batch(panel, monkeypatch):
    p = panel
    _mode(p, monkeypatch)
    refs = [{'artifact': 'sheet.xlsx', 'sha256': 'a' * 64},
            {'artifact': 'report.docx', 'sha256': 'b' * 64}]
    p.capability_sources = refs
    p.control.configure(values=['自动按任务选择输入', '无来源文件', 'sheet.xlsx', 'report.docx'])
    p.control.current(0)
    pending, calls = [], []
    monkeypatch.setattr(p, 'start', lambda kind, work: pending.append(work))
    monkeypatch.setattr(capability_router, 'prepare', lambda s, text, **kw: calls.append((text, kw)))
    p.preview_capability('根据Excel生成Word')
    refs[0]['sha256'] = 'c' * 64
    pending[0](object())
    assert calls[0][0] == '根据Excel生成Word'
    assert calls[0][1]['source_refs'][0]['sha256'] == 'a' * 64
    assert 'source_ref' not in calls[0][1]


def test_auto_without_candidates_requires_explicit_no_source(panel):
    panel.capability_sources = []
    panel.control.configure(values=['自动按任务选择输入', '无来源文件'])
    panel.control.current(0)
    with pytest.raises(Exception, match='没有可用的上一任务成果'):
        panel.preview_capability('做表格')


def test_failure_shows_raw_code_and_never_calls_handoff(panel):
    p = panel
    callbacks = []
    p.on_artifacts = callbacks.append
    p.messages.put(('capability_execute', {'ok': False, 'state': 'unknown',
        'code': 'capability_previous_run_unknown', 'run': 'saved-run',
        'artifacts': [{'path': 'unverified.docx'}]}, None))
    p.poll()
    assert callbacks == []
    assert 'capability_previous_run_unknown' in _text(p)
    assert '任务未完成' in p.status.get()
    assert '自动接入下一任务' not in _text(p)


def test_success_claims_handoff_only_after_callback_and_callback_failure_is_clear(panel):
    p = panel
    records = [{'path': 'verified.docx', 'sha256': 'e' * 64}]
    result = {'ok': True, 'state': 'succeeded', 'code': 'capability_verified',
              'run': 'saved-run', 'artifacts': records}
    observations = []

    def accept(value):
        observations.append((_text(p), value, p.running))

    p.on_artifacts = accept
    p.messages.put(('capability_execute', result, None))
    p.poll()
    assert observations[0][1:] == (records, False)
    assert '已接到下一任务' not in observations[0][0]
    assert '已自动接入下一任务' in _text(p)

    def reject(_):
        raise ValueError('handoff callback failed')

    p.on_artifacts = reject
    p.messages.put(('capability_execute', result, None))
    p.poll()
    assert '自动接力未完成' in _text(p) and '自动接力未完成' in p.status.get()
    assert '已接到下一任务' not in _text(p)
    assert '已自动接入下一任务' not in _text(p)


def test_clarification_button_loads_backend_candidates_on_worker(panel, monkeypatch):
    from agent_service import capability_clarification
    rows = [{'run': 'verified-run', 'instruction': '保留原任务',
             'sources': [{'id': 'S1', 'filename': 'source.docx', 'format': '.docx'}]}]
    calls, scheduled = [], []
    monkeypatch.setattr(capability_clarification, 'pending', lambda: calls.append(True) or rows)
    monkeypatch.setattr(panel, 'start', lambda kind, work: scheduled.append((kind, work)))
    panel.mode.set(ui.MODES[11])

    panel.list_capability_clarifications()

    assert calls == [] and scheduled[0][0] == 'capability_clarifications'
    assert scheduled[0][1](object()) == rows and calls == [True]


def test_clarification_modal_lists_verified_source_choices(panel):
    rows = [{'run': 'verified-run', 'instruction': '保留原任务',
             'sources': [{'id': 'S1', 'filename': 'source.docx', 'format': '.docx'},
                         {'id': 'S2', 'filename': 'other.xlsx', 'format': '.xlsx'}]}]
    panel.mode.set(ui.MODES[11])

    panel.open_capability_clarification_selector(rows)

    window = panel.capability_clarification_window
    children = window.winfo_children()
    listbox = next(child for child in children if isinstance(child, tk.Listbox))
    source = next(child for child in children if isinstance(child, ttk.Combobox))
    assert '保留原任务' in listbox.get(0)
    assert str(source.cget('state')) == 'readonly'
    window.destroy()


def test_clarification_source_selection_prepares_new_plan_for_normal_approval(panel, monkeypatch):
    from agent_service import capability_clarification
    scheduled, calls = [], []
    plan = {'mode': 'capability', 'schema': 3, 'instruction': '保留原任务',
            'source_ref': {'artifact': 'other.xlsx'}, 'candidates': []}
    monkeypatch.setattr(capability_clarification, 'prepare',
                        lambda session, run, source_id: calls.append((session, run, source_id)) or plan)
    monkeypatch.setattr(panel, 'start', lambda kind, work: scheduled.append((kind, work)))
    panel.mode.set(ui.MODES[11])

    panel.prepare_capability_clarification('verified-run', 'S2')

    session = object()
    assert calls == []
    assert scheduled[0][0] == 'capability_clarification_prepare'
    assert scheduled[0][1](session) is plan
    assert calls == [(session, 'verified-run', 'S2')]
    panel.messages.put(('capability_clarification_prepare', plan, None))
    panel.poll()
    assert panel.plan is plan and str(panel.execute_button['state']) == 'normal'
    assert '尚未执行办公步骤' in panel.status.get()


def test_ambiguous_outcome_offers_only_explicit_clarification(panel, monkeypatch):
    panel.mode.set(ui.MODES[11])
    panel.messages.put(('capability_execute', {
        'ok': False, 'code': 'capability_source_ambiguous', 'run': 'known-run', 'artifacts': []
    }, None))
    monkeypatch.setattr(capability_router, 'describe_result', lambda _result: '来源不明确。')

    panel.poll()

    assert '明确来源后继续' in _text(panel)
    assert '未知执行不会列入继续列表' in _text(panel)
    assert panel.plan is None
