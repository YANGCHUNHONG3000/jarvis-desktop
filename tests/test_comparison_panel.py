"""Comparison objective validation and hidden Tk dispatch; no network or API."""
import tkinter as tk
from tkinter import ttk

import pytest

from agent_service import computer_agent, computer_goal_panel as ui
from agent_service.comparison_panel import build_goal, fields_from_goal


def values(**extra):
    return {'query1': 'site:adafruit.com keyboard', 'query2': 'site:logitech.com keyboard',
            'title': '办公键盘对比', 'dimensions': '连接方式 | 价格 | 尺寸',
            'min_sources': '4', 'min_domains': '2', **extra}


def test_build_goal_uses_shared_validation_and_roundtrip():
    goal = build_goal(values())
    assert goal['comparison'] == {'dimensions': ['连接方式', '价格', '尺寸'], 'min_domains': 2,
                                  'evidence_version': 3}
    assert goal['min_sources'] == 4 and goal['required_terms'] == []
    assert goal['outputs'] == ['spreadsheet', 'document']
    assert build_goal(fields_from_goal(goal)) == goal


@pytest.mark.parametrize('extra', [
    {'query1': ''}, {'query2': 'SITE:ADAFRUIT.COM KEYBOARD'}, {'query1': 'x' * 501},
    {'title': ''}, {'title': 'x' * 201}, {'dimensions': ''},
    {'dimensions': '价格 | 价格'}, {'dimensions': 'a|b|c|d'}, {'dimensions': 'x' * 61},
    {'min_sources': '1'}, {'min_sources': '5'}, {'min_sources': 'two'},
    {'min_domains': '0'}, {'min_domains': '3'}, {'query1': 'keyboard\ncommands'},
])
def test_invalid_fields_fail_closed(extra):
    with pytest.raises((ValueError, TypeError)):
        build_goal(values(**extra))


def test_one_query_two_sources_allowed():
    goal = build_goal(values(query2='', min_sources='2', min_domains='1'))
    assert len(goal['queries']) == 1 and goal['min_sources'] == 2


@pytest.fixture(scope='module')
def hidden_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()
    import gc
    gc.collect()


@pytest.fixture
def panel(hidden_root):
    tabs = ttk.Notebook(hidden_root)
    panel = ui.attach_goal_tab(tabs, hidden_root)
    panel.mode.set(ui.MODES[10])
    panel.mode_changed()
    yield panel
    panel.close()
    tabs.destroy()


def test_mode_appended_and_scope_button_enabled(panel):
    assert len(ui.MODES) == 11 and ui.MODES[9] == '自然语言编辑 Word/Excel'
    assert str(panel.interaction_button['state']) == 'normal'
    assert '对比' in str(panel.interaction_button['text'])
    assert str(panel.propose_button['state']) == 'disabled'
    assert str(panel.source['state']) == 'disabled'
    panel.set_busy(True)
    assert str(panel.interaction_button['state']) == 'disabled'
    panel.set_busy(False)
    assert str(panel.interaction_button['state']) == 'normal'


def test_preview_requires_saved_scope_and_cloud_propose_is_local(panel, monkeypatch):
    monkeypatch.setattr(computer_agent, 'propose', lambda *a, **kw: pytest.fail('API'))
    panel.preview()
    assert panel.plan is None and '先设置' in panel.status.get()
    panel.propose()
    assert not panel.running and panel.session is None and '本地预览' in panel.status.get()


def test_configure_saves_validated_copy_and_revokes_plan(panel, monkeypatch):
    from agent_service import comparison_panel
    goal, fields = build_goal(values()), values()
    panel.plan = {'stale': True}
    def editor(parent, saved, initial):
        assert initial is None
        saved(goal, fields)
    monkeypatch.setattr(comparison_panel, 'open_editor', editor)
    panel.configure_comparison()
    assert panel.plan is None and panel.comparison_goal == goal
    goal['queries'][0] = 'changed'
    fields['title'] = 'changed'
    assert panel.comparison_goal['queries'][0] != 'changed'
    assert panel.comparison_fields['title'] != 'changed'


def test_configure_busy_does_not_open_editor(panel, monkeypatch):
    from agent_service import comparison_panel
    monkeypatch.setattr(comparison_panel, 'open_editor', lambda *a: pytest.fail('busy'))
    panel.external_busy = lambda: True
    panel.configure_comparison()
    assert panel.comparison_goal is None


def test_preview_is_existing_agent_plan_and_no_model(panel, tmp_path, monkeypatch):
    from agent_service.computer import ComputerSession
    session = ComputerSession.create(tmp_path / 'sessions')
    monkeypatch.setattr(ui.ComputerSession, 'create', lambda **kw: session)
    monkeypatch.setattr(computer_agent, 'model_json', lambda *a, **kw: pytest.fail('API'))
    panel.comparison_goal = build_goal(values())
    panel.preview()
    assert panel.session is session and panel.plan['mode'] == 'agent'
    assert panel.plan['goal'] == panel.comparison_goal
    assert panel.plan['instruction'] == '办公键盘对比'
    assert str(panel.execute_button['state']) == 'normal'
    panel.comparison_goal['queries'][0] = 'changed'
    assert panel.plan['goal']['queries'][0] != 'changed'


def test_preview_keeps_instruction(panel, monkeypatch):
    panel.session = object()
    panel.comparison_goal = build_goal(values())
    panel.text.insert('1.0', '比较办公键盘的公开来源')
    calls = []
    monkeypatch.setattr(computer_agent, 'prepare', lambda s, instruction, goal: calls.append((s, instruction, goal)) or {'fixture': True})
    monkeypatch.setattr(panel, 'show_plan', lambda plan: None)
    panel.preview()
    assert calls == [(panel.session, '比较办公键盘的公开来源', panel.comparison_goal)]


def test_recovery_uses_comparison_mode_and_restores_scope(panel, tmp_path):
    from agent_service.computer import ComputerSession
    session = ComputerSession.create(tmp_path / 'sessions')
    goal = build_goal(values())
    plan = computer_agent.prepare(session, '继续对比', goal)
    panel.messages.put(('recovery_preview', {'session_root': str(session.root),
        'plan': plan, 'reason': '已恢复预览'}, None))
    panel.poll()
    assert panel.mode.get() == ui.MODES[10]
    assert panel.comparison_goal == goal and panel.plan == plan
    assert build_goal(panel.comparison_fields) == goal
    assert panel.text.get('1.0', 'end-1c') == '继续对比'


def selection():
    goal = build_goal(values())
    return {'manifest': {'queries': goal['queries'], 'run': 'source.run', 'source_ids': ['s1', 's2']},
            'goal': goal, 'fields': values(), 'pages': [{'url': 'https://example.org/', 'query_index': 0}]}


def test_source_event_revokes_old_plan_and_locks_queries(panel, monkeypatch):
    from agent_service import comparison_panel
    chosen = selection()
    panel.plan = {'old': True}
    panel.text.insert('1.0', 'old instruction')
    panel.messages.put(('comparison_source', chosen, None))
    panel.poll()
    assert panel.plan is None and panel.comparison_source == chosen
    assert panel.text.get('1.0', 'end-1c') == ''
    assert 'https://example.org/' in panel.preview_box.get('1.0', 'end-1c')
    def editor(parent, saved, initial, *, locked_queries):
        assert locked_queries is True
        changed = values(dimensions='材质')
        saved(build_goal(changed), changed)
    monkeypatch.setattr(comparison_panel, 'open_editor', editor)
    panel.configure_comparison()
    assert panel.comparison_goal['comparison']['dimensions'] == ['材质']
    assert panel.comparison_source['manifest'] == chosen['manifest']


def test_source_preview_uses_worker_and_copied_scope(panel, monkeypatch):
    from agent_service import comparison_history
    panel.comparison_source = selection()
    panel.comparison_goal = build_goal(values())
    panel.session = object()
    calls = []
    monkeypatch.setattr(panel, 'start', lambda kind, operation: calls.append((kind, operation)))
    monkeypatch.setattr(computer_agent, 'prepare', lambda *a: pytest.fail('wrong preview'))
    captured = []
    monkeypatch.setattr(comparison_history, 'prepare_selected',
                        lambda *args, **kw: captured.append(args))
    panel.preview()
    assert calls[0][0] == 'comparison_preview'
    panel.comparison_goal['title'] = 'changed later'
    calls[0][1](panel.session)
    assert captured[0][1:3] == ('办公键盘对比', build_goal(values()))


def test_changed_queries_are_rejected_by_saved_callback(panel, monkeypatch):
    from agent_service import comparison_panel
    panel.comparison_source = selection()
    panel.comparison_goal = build_goal(values())
    def editor(parent, saved, initial, **kw):
        with pytest.raises(ValueError, match='检索主题不能改变'):
            saved(build_goal(values(query1='different')), values(query1='different'))
    monkeypatch.setattr(comparison_panel, 'open_editor', editor)
    panel.configure_comparison()
    assert panel.comparison_goal == build_goal(values())


def test_recovery_cannot_silently_become_fresh_comparison(panel, tmp_path, monkeypatch):
    from agent_service import comparison_panel
    from agent_service.computer import ComputerSession
    session = ComputerSession.create(tmp_path / 'sessions')
    plan = computer_agent.prepare(session, '恢复范围', build_goal(values()))
    panel.comparison_source = selection()
    panel.messages.put(('recovery_preview', {'session_root': str(session.root),
                        'plan': plan, 'reason': '继续剩余步骤'}, None))
    panel.poll()
    assert panel.comparison_source is None and panel.comparison_recovery
    monkeypatch.setattr(comparison_panel, 'open_editor', lambda *a: pytest.fail('immutable recovery'))
    panel.configure_comparison()
    panel.preview()
    assert panel.plan == plan
    panel.reset_comparison_source()
    assert panel.plan is None and panel.comparison_goal is None and not panel.comparison_recovery


@pytest.mark.parametrize('kind', ['comparison_source', 'comparison_preview'])
def test_stopped_worker_cannot_restore_approval(panel, kind):
    panel.stop()
    panel.messages.put((kind, selection(), None))
    panel.poll()
    assert panel.plan is None and panel.comparison_source is None
    assert str(panel.execute_button['state']) == 'disabled'


def test_source_controls_busy_and_mode_gated(panel):
    assert str(panel.comparison_source_button['state']) == 'normal'
    panel.set_busy(True)
    assert str(panel.comparison_source_button['state']) == 'disabled'
    panel.set_busy(False)
    panel.mode.set(ui.MODES[9])
    panel.mode_changed()
    assert str(panel.comparison_source_button['state']) == 'disabled'


def test_invalid_history_clears_source_without_fallback(panel):
    panel.comparison_source = selection()
    panel.comparison_goal = build_goal(values())
    panel.messages.put(('comparison_preview', None, 'comparison_seed_changed'))
    panel.poll()
    assert panel.plan is None and panel.comparison_source is None
    assert panel.comparison_goal is None and '另选来源' in panel.status.get()
