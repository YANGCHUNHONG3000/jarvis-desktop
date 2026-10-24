"""Hidden-Tk checks for the explicit verified Office bundle route."""

import tkinter as tk
import sys
from tkinter import ttk
from types import SimpleNamespace

import pytest

from agent_service import computer_goal_panel as ui


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


def _setup(p, sources, monkeypatch):
    monkeypatch.setattr(p, 'refresh_capability_sources', lambda: None)
    p.mode.set(ui.MODES[11])
    p.capability_sources = sources
    p.mode_changed()
    p.text.insert('1.0', '逐份核对数据并生成汇总')
    p.text.edit_modified(False)


def _bundle_module(monkeypatch, **functions):
    module = SimpleNamespace(**functions)
    monkeypatch.setitem(sys.modules, 'agent_service.office_bundle', module)
    return module


def test_bundle_preview_is_local_and_does_not_execute(panel, monkeypatch):
    p = panel
    refs = [{'artifact': f's{i}.xlsx', 'sha256': str(i) * 64} for i in range(2, 4)]
    _setup(p, refs, monkeypatch)
    pending, calls = [], []
    plan = {'mode': 'office_bundle', 'sha256': 'a' * 64, 'source_refs': refs}
    _bundle_module(monkeypatch,
        prepare=lambda s, text, source_refs: calls.append(('prepare', s, text, source_refs)) or plan,
        describe=lambda _: '全部来源已绑定',
        execute=lambda *a, **k: pytest.fail('executed before approval'))
    monkeypatch.setattr(p, 'start', lambda kind, work: pending.append((kind, work)))

    p.preview_office_bundle()

    assert pending[0][0] == 'office_bundle_preview'
    refs[0]['sha256'] = 'f' * 64
    assert pending[0][1]('session') is plan
    assert calls[0] == ('prepare', 'session', '逐份核对数据并生成汇总', [
        {'artifact': 's2.xlsx', 'sha256': '2' * 64},
        {'artifact': 's3.xlsx', 'sha256': '3' * 64}])
    p.messages.put(('office_bundle_preview', plan, None))
    p.poll()
    assert p.plan is plan


def test_bundle_execute_uses_approved_plan_and_hands_off_success(panel, monkeypatch):
    p = panel
    records = [{'path': 'bundle.xlsx', 'sha256': 'e' * 64}]
    plan = {'mode': 'office_bundle', 'sha256': 'b' * 64, 'source_refs': []}
    p.plan = plan
    pending, calls, handed = [], [], []
    result = {'ok': True, 'state': 'succeeded', 'run': 'bundle-run', 'artifacts': records}
    _bundle_module(monkeypatch,
        execute=lambda s, chosen, *, approval, stop: calls.append((s, chosen, approval, stop)) or result,
        describe_result=lambda value: '已核验联合成果')
    monkeypatch.setattr(p, 'start', lambda kind, work: pending.append((kind, work)))
    p.on_artifacts = handed.append

    p.execute()

    assert pending[0][0] == 'office_bundle_execute'
    assert pending[0][1]('session') is result
    assert calls[0][:3] == ('session', plan, plan['sha256'])
    assert calls[0][3]() is False
    p.messages.put(('office_bundle_execute', result, None))
    p.poll()
    assert handed == [records]
    assert '自动接入下一任务' in p.preview_box.get('1.0', 'end')


def test_bundle_success_after_stop_is_kept_without_handoff(panel, monkeypatch):
    p = panel
    records = [{'path': 'bundle.xlsx', 'sha256': 'f' * 64}]
    result = {'ok': True, 'state': 'succeeded', 'run': 'bundle-run', 'artifacts': records}
    _bundle_module(monkeypatch, describe_result=lambda value: '已核验联合成果')
    handed = []
    p.on_artifacts = handed.append
    p.stopped.set()

    p.messages.put(('office_bundle_execute', result, None))
    p.poll()

    assert handed == []
    assert 'bundle-run' in p.preview_box.get('1.0', 'end')
    assert '保留' in p.status.get() and '未自动接力' in p.status.get()


def test_bundle_button_requires_idle_and_two_to_four_sources(panel, monkeypatch):
    p = panel
    _setup(p, [{'artifact': 'one.xlsx', 'sha256': 'c' * 64},
               {'artifact': 'two.docx', 'sha256': 'd' * 64}], monkeypatch)
    assert str(p.office_bundle_button['state']) == 'normal'
    p.set_busy(True)
    assert str(p.office_bundle_button['state']) == 'disabled'
    p.set_busy(False)
    p.capability_sources = p.capability_sources[:1]
    p.update_office_bundle_button()
    assert str(p.office_bundle_button['state']) == 'disabled'
    p.capability_sources *= 5
    p.update_office_bundle_button()
    assert str(p.office_bundle_button['state']) == 'disabled'


@pytest.mark.parametrize('sources', [[], [{'artifact': 'one.xlsx', 'sha256': 'a' * 64}]])
def test_bundle_preview_rejects_zero_or_one_source(panel, monkeypatch, sources):
    p = panel
    _setup(p, sources, monkeypatch)
    monkeypatch.setattr(p, 'start', lambda *a: pytest.fail('invalid source count started preview'))

    p.preview_office_bundle()

    assert '2 至 4' in p.status.get()
