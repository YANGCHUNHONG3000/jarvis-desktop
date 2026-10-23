"""Offline routing checks for the natural-language Office panel mode."""
from types import SimpleNamespace
import sys
from types import ModuleType
import queue
import threading

from agent_service import computer_goal_panel as ui


class Value:
    def __init__(self, value=''):
        self.value = value
        self.options = {}

    def get(self, *args):
        return self.value

    def set(self, value):
        self.value = value

    def configure(self, **kwargs):
        self.options.update(kwargs)


class PreviewBox:
    def __init__(self):
        self.value = ''

    def configure(self, **kwargs):
        pass

    def delete(self, *_):
        self.value = ''

    def insert(self, _index, value):
        self.value = value


def panel_stub():
    panel = ui.GoalPanel.__new__(ui.GoalPanel)
    panel.mode = Value(ui.MODES[9])
    panel.running = False
    panel.external_busy = lambda: False
    panel.root = SimpleNamespace(after=lambda *_: 1)
    panel.status = Value()
    panel.plan = {'mode': 'agent'}
    panel.office_planning_files = ()
    panel.office_artifact_refs = None
    panel.office_scope = None
    panel.output_edit_button = Value()
    panel.execute_button = Value()
    panel.preview_box = PreviewBox()
    panel.snapshots, panel.controls = {'old': 1}, ['old']
    panel.control = Value('old')
    panel.source = Value()
    panel.destination = Value()
    panel.expression_label = Value()
    panel.text_label = Value()
    panel.control_label = Value()
    panel.interaction_button = Value()
    panel.propose_button = Value()
    panel.scope_button = Value()
    panel.recovery_button = Value()
    panel.pause_checkbox = Value()
    panel.comparison_source_button = Value()
    panel.comparison_reset_button = Value()
    panel.show_text = lambda text: ui.GoalPanel.show_text(panel, text)
    panel.invalidate = lambda clear_snapshots=False: ui.GoalPanel.invalidate(panel, clear_snapshots)
    return panel


def install_dialog(monkeypatch, result):
    from tkinter import filedialog
    monkeypatch.setattr(filedialog, 'askopenfilenames', lambda **_: result)


def test_propose_routes_selected_files_and_instruction(monkeypatch):
    panel = ui.GoalPanel.__new__(ui.GoalPanel)
    panel.running = False
    panel.mode = Value(ui.MODES[9])
    panel.office_planning_files = ('a.docx', 'b.xlsx')
    panel.office_artifact_refs = None
    panel.office_scope = None
    panel.text = Value('把预算状态改为已确认')
    panel.stopped = SimpleNamespace(is_set=lambda: False)
    panel.invalidate = lambda: None
    scheduled = []
    panel.start = lambda kind, operation: scheduled.append((kind, operation))
    calls = []
    office_intent = ModuleType('agent_service.office_intent')
    office_intent.propose = lambda session, instruction, files, **kw: (
        calls.append((session, instruction, files, kw)) or
        {'plan': {'mode': 'agent', 'kind': 'interactive'}, 'reason': 'review'})
    monkeypatch.setitem(sys.modules, 'agent_service.office_intent', office_intent)

    panel.propose()

    assert len(scheduled) == 1 and scheduled[0][0] == 'propose'
    result = scheduled[0][1](object())
    assert result['plan']['kind'] == 'interactive'
    assert calls[0][1:3] == ('把预算状态改为已确认', ('a.docx', 'b.xlsx'))
    assert calls[0][3]['stop']() is False


def test_local_preview_says_cloud_planning_without_api():
    panel = ui.GoalPanel.__new__(ui.GoalPanel)
    panel.running = False
    panel.external_busy = lambda: False
    panel.mode = Value(ui.MODES[9])
    panel.text = Value('更新预算')
    panel.expression = Value('')
    panel.invalidate = lambda: None
    shown, statuses = [], []
    panel.show_text = shown.append
    panel.status = Value()

    panel.preview()

    assert '不会调用 API' in panel.status.get()
    assert '云端规划' in shown[0]


def test_valid_reselection_invalidates_plan_and_cancel_keeps_selection(monkeypatch):
    panel = panel_stub()
    panel.office_planning_files = ('old.docx',)
    install_dialog(monkeypatch, ('new.xlsx',))
    panel.configure_office_intent()
    assert panel.office_planning_files == ('new.xlsx',)
    assert panel.plan is None and panel.execute_button.options['state'] == 'disabled'

    panel.plan = {'mode': 'agent'}
    panel.office_planning_files = ('new.xlsx',)
    install_dialog(monkeypatch, ())
    panel.configure_office_intent()
    assert panel.office_planning_files == ('new.xlsx',)
    assert panel.plan == {'mode': 'agent'}


def test_invalid_selection_clears_old_files_and_plan(monkeypatch):
    panel = panel_stub()
    panel.office_planning_files = ('old.docx',)
    install_dialog(monkeypatch, ('a.docx', 'b.xlsx', 'c.docx', 'd.xlsx', 'e.docx'))
    panel.configure_office_intent()
    assert panel.office_planning_files == () and panel.plan is None
    panel.plan = {'mode': 'agent'}
    panel.office_planning_files = ('old.docx',)
    install_dialog(monkeypatch, ('wrong.pdf',))
    panel.configure_office_intent()
    assert panel.office_planning_files == () and panel.plan is None


def test_mode_and_instruction_changes_invalidate_plan():
    panel = panel_stub()
    panel.mode_changed()
    assert panel.plan is None and panel.execute_button.options['state'] == 'disabled'

    panel.plan = {'mode': 'agent'}
    panel.text = SimpleNamespace(edit_modified=lambda: True, edit_modified_reset=False)
    panel.text.edit_modified = lambda value=None: False if value is False else True
    panel.modified()
    assert panel.plan is None and panel.execute_button.options['state'] == 'disabled'


def test_stop_then_late_proposal_does_not_restore_approval():
    panel = panel_stub()
    panel.closed = False
    panel.poll_id = None
    panel.messages = queue.Queue()
    panel.stopped = threading.Event()
    panel.stopped.set()
    panel.set_busy = lambda _value: None
    panel.messages.put(('propose', {'plan': {'mode': 'agent'}, 'reason': 'late'}, None))

    panel.poll()

    assert panel.plan is None and panel.execute_button.options['state'] == 'disabled'
