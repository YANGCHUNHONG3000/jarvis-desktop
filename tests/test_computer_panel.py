"""Focused widget/callback tests. No native targets, network or model calls."""
import queue
import tkinter as tk
from tkinter import ttk
import pytest
from agent_service.computer_panel import attach_computer_tab


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
    root = hidden_root
    notebook = ttk.Notebook(root)
    changes = []
    value = attach_computer_tab(notebook, root, on_busy=changes.append)
    yield value, changes
    value.close()
    notebook.destroy()


def test_panel_starts_local_without_session_or_network(panel):
    p, _ = panel
    assert p.session is None and p.snapshot is None and p.plan is None
    assert str(p.execute_button.cget('state')) == 'disabled'
    assert '上传' in next(w.cget('text') for w in p.tab.winfo_children()
                       if isinstance(w, ttk.Button) and '云端' in w.cget('text'))


def test_cancel_discards_late_proposal_and_releases_shared_busy(panel):
    p, changes = panel
    p.running = True
    p.stop()
    p.messages.put(('propose', {'proposal': {'allowed':True}, 'reason':'late'}, None))
    p.poll()
    assert p.plan is None and p.snapshot is None
    assert not p.running and changes == [False]
    assert str(p.execute_button.cget('state')) == 'disabled'


def test_external_busy_blocks_start_without_allocating_session(panel):
    p, changes = panel
    p.external_busy = lambda: True
    p.start('windows', lambda _: pytest.fail('must not run'))
    assert not p.running and not changes and p.session is None


def test_failed_thread_start_restores_controls_and_busy(panel, monkeypatch):
    from agent_service import computer_panel
    p, changes = panel
    class NoThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('test')
    monkeypatch.setattr(computer_panel.threading, 'Thread', NoThread)
    p.start('windows', lambda _: None)
    assert changes == [True, False] and not p.running
    assert str(p.window.cget('state')) == 'readonly'


def test_execute_requires_current_visible_plan(panel):
    p, changes = panel
    p.plan = None
    p.execute()
    assert not changes and p.session is None
