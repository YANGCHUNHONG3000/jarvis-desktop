"""Offline recovery routing and preview delivery checks."""
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

import pytest

from agent_service import computer_goal_panel as ui
from agent_service import interaction_recovery_panel as recovery_ui
from agent_service.computer import ComputerError
from test_office_intent_panel import Value, panel_stub


def test_abrupt_exit_display_does_not_claim_original_success():
    row = {'run':'saved-run','eligible':True,'instruction':'继续网页任务',
           'interrupted_without_outcome':True,'completed_edits':['e1'],'remaining_edits':['e2']}
    assert recovery_ui.candidate_label(row).startswith('异常退出 · 可恢复 · 已完成1/待完成1')
    detail = recovery_ui.candidate_detail(row)
    assert '未写最终结果' in detail and '原执行已停止' in detail
    assert '成功' not in detail


def test_active_owner_display_cannot_offer_parallel_recovery():
    row = {'run':'saved-run','eligible':False,'code':'run_owner_active'}
    assert recovery_ui.candidate_label(row).startswith('已阻止')
    assert '暂不能同时恢复' in recovery_ui.candidate_detail(row)
    assert '原执行已停止' not in recovery_ui.candidate_detail(row)
    assert recovery_ui.candidate_detail(None) == '选择任务后核验来源和原生回执。'


def test_list_recoveries_routes_local_candidates_and_guards_busy(monkeypatch):
    panel = panel_stub()
    panel.stopped = threading.Event()
    scheduled = []
    panel.start = lambda kind, fn: scheduled.append((kind, fn))
    seen = []
    monkeypatch.setattr(recovery_ui, 'recent_candidates', lambda **kw: seen.append(kw) or ['candidate'])

    panel.list_recoveries()
    assert scheduled[0][0] == 'recovery_candidates'
    assert scheduled[0][1](None) == ['candidate']
    assert seen[0]['stop']() is False

    for running, external in ((True, False), (False, True)):
        panel.running = running
        panel.external_busy = lambda external=external: external
        panel.list_recoveries()
    assert len(scheduled) == 1


@pytest.mark.parametrize('guard', ['running', 'closed', 'stopped', 'external'])
def test_select_recovery_refuses_running_closed_stopped_or_external_busy(monkeypatch, guard):
    panel = panel_stub()
    panel.running = guard == 'running'
    panel.closed = guard == 'closed'
    panel.stopped = threading.Event()
    if guard == 'stopped':
        panel.stopped.set()
    panel.external_busy = lambda: guard == 'external'
    scheduled = []
    panel.start = lambda *args: scheduled.append(args)
    monkeypatch.setattr(recovery_ui, 'prepare_selected', lambda *a, **kw: {})

    panel.select_recovery({'run': 'run', 'session_root': 'root'})

    assert scheduled == []


def test_select_recovery_routes_prepare_with_host_and_stop(monkeypatch):
    panel = panel_stub()
    panel.closed = False
    panel.stopped = threading.Event()
    panel.host = 'host'
    scheduled = []
    panel.start = lambda kind, fn: scheduled.append((kind, fn))
    called = []
    monkeypatch.setattr(recovery_ui, 'prepare_selected',
                        lambda row, **kw: called.append((row, kw)) or {'preview': 1})
    row = {'run': 'run', 'session_root': 'root'}

    panel.select_recovery(row)

    assert scheduled[0][0] == 'recovery_preview'
    assert scheduled[0][1](None) == {'preview': 1}
    assert called[0][0] is row and called[0][1]['host'] == 'host'
    assert called[0][1]['stop']() is False


def test_prepare_selected_validates_session_path_before_loading(monkeypatch, tmp_path):
    base = tmp_path / 'sessions'
    base.mkdir()
    outside = tmp_path / 'computer-outside'
    outside.mkdir()
    called = []
    monkeypatch.setattr(recovery_ui, 'ComputerSession', lambda *a, **kw: called.append(a) or object())
    monkeypatch.setattr('agent_service.interaction_recovery.prepare', lambda *a: {'plan': {}})

    with pytest.raises(ComputerError, match='recovery_session_scope_invalid'):
        recovery_ui.prepare_selected({'session_root': str(outside), 'run': 'x'}, base=base)
    assert called == []


def test_prepare_selected_reuses_saved_session_and_cancellation(monkeypatch, tmp_path):
    base = tmp_path / 'sessions'
    root = base / 'computer-saved'
    root.mkdir(parents=True)
    opened, prepared = [], []
    session = SimpleNamespace(root=root)
    monkeypatch.setattr(recovery_ui, 'ComputerSession',
                        lambda path, **kw: opened.append((Path(path), kw)) or session)
    monkeypatch.setattr('agent_service.interaction_recovery.prepare',
                        lambda obj, run: prepared.append((obj, run)) or {'plan': {'approved': True}})

    result = recovery_ui.prepare_selected({'session_root': str(root), 'run': 'saved-run'},
                                          host='host', base=base)
    assert result['plan']['approved'] is True and result['session_root'] == str(root)
    assert opened == [(root, {'host': 'host'})]
    assert prepared == [(session, 'saved-run')]

    before = len(opened)
    with pytest.raises(ComputerError, match='recovery_cancelled'):
        recovery_ui.prepare_selected({'session_root': str(root), 'run': 'saved-run'},
                                     base=base, stop=lambda: True)
    assert len(opened) == before


def _poll_panel():
    panel = panel_stub()
    panel.closed = False
    panel.poll_id = None
    panel.messages = queue.Queue()
    panel.stopped = threading.Event()
    panel.set_busy = lambda _value: None
    panel.session = 'old-session'
    panel.host = 'host'
    class Text(Value):
        def delete(self, *_): self.value = ''
        def insert(self, _i, value): self.value = value
        def edit_modified(self, *_): pass
    panel.text = Text()
    panel.mode_changed = lambda: None
    return panel


@pytest.mark.parametrize('kind', ['recovery_candidates', 'recovery_preview'])
def test_poll_ignores_stopped_late_recovery_results(kind):
    panel = _poll_panel()
    panel.stopped.set()
    panel.messages.put((kind, {'session_root': 'late', 'plan': {'instruction': 'late'}}, None))

    ui.GoalPanel.poll(panel)

    assert panel.session == 'old-session'
    assert panel.plan is None
    assert panel.text.get() == ''


@pytest.mark.parametrize('goal,mode_index', [({},9), ({'queries':['topic']},6),
                                           ({'kind':'interactive','browser':{'start_url':'https://example.org'}},7)])
def test_poll_installs_only_fresh_recovery_preview(monkeypatch, goal, mode_index):
    panel = _poll_panel()
    result = {'session_root': 'saved', 'plan': {'mode': 'agent', 'instruction': 'continue','goal':goal}, 'reason': 'review'}
    panel.messages.put(('recovery_preview', result, None))
    created = []
    monkeypatch.setattr(ui, 'ComputerSession', lambda root, **kw: created.append((root, kw)) or 'reused-session')
    monkeypatch.setattr(ui, 'describe', lambda _plan: 'review preview')

    ui.GoalPanel.poll(panel)

    assert created == [('saved', {'host': 'host'})]
    assert panel.session == 'reused-session'
    assert panel.mode.get() == ui.MODES[mode_index]
    assert panel.text.value == 'continue'
    assert panel.plan == result['plan']


def test_recent_candidates_caps_sessions_and_rows(monkeypatch, tmp_path):
    base = tmp_path / 'sessions'
    base.mkdir()
    sessions = [base / f'computer-{i:02d}' for i in range(24)]
    for path in sessions:
        path.mkdir()
    opened = []
    monkeypatch.setattr(recovery_ui, 'BASE', base)
    monkeypatch.setattr(recovery_ui, 'ComputerSession',
                        lambda path: opened.append(Path(path)) or SimpleNamespace(root=Path(path)))
    monkeypatch.setattr('agent_service.interaction_recovery.list_candidates',
                        lambda session: [{'run': str(session.root)}])

    rows = recovery_ui.recent_candidates(base=base)

    assert len(opened) == 20
    assert len(rows) == 20
    assert all(row['session_root'] in {str(path) for path in sessions} for row in rows)

    for i in range(24, 45):
        (base / f'computer-{i:02d}').mkdir()
    monkeypatch.setattr('agent_service.interaction_recovery.list_candidates',
                        lambda session: [{'run': 'a'}, {'run': 'b'}] * 20)
    rows = recovery_ui.recent_candidates(base=base)
    assert len(rows) == 30
