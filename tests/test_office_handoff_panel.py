"""GUI routing checks for reusing verified Office artifacts."""
import queue
import threading
from copy import deepcopy

from test_office_intent_panel import Value, panel_stub, install_dialog
from agent_service import computer_goal_panel as ui


def _async_panel(monkeypatch, artifacts):
    panel = panel_stub()
    panel.closed = False
    panel.poll_id = None
    panel.messages = queue.Queue()
    panel.stopped = threading.Event()
    panel.set_busy = lambda _value: None
    panel.get_artifacts = lambda: artifacts
    scheduled = []
    panel.start = lambda kind, operation: scheduled.append((kind, operation))
    return panel, scheduled


def test_use_generated_artifacts_opens_selector_and_prepares_scope_without_model(monkeypatch):
    records = [{'path': 'a.docx', 'sha256': 'a' * 64}]
    panel, scheduled = _async_panel(monkeypatch, records)
    panel.office_planning_files = ('old.xlsx',)
    panel.office_artifact_refs = [{'artifact': 'old.xlsx'}]
    panel.office_scope = [{'path': 'old.xlsx'}]
    model_calls = []
    monkeypatch.setattr(ui.GoalPanel, 'propose', lambda *_: model_calls.append(True))
    selected = []
    opened = []
    monkeypatch.setattr('agent_service.office_scope_panel.open_file_selector',
                        lambda root, value, callback: opened.append((value, callback)))
    from agent_service import office_handoff
    monkeypatch.setattr(office_handoff, 'select_outputs',
                        lambda value: selected.append(value) or [{'artifact': 'a.docx', 'sha256': 'a' * 64}])
    from agent_service import office_scope
    monkeypatch.setattr(office_scope, 'inspect_files', lambda *args, **kwargs: {'files': ['a.docx']})

    panel.use_generated_artifacts()
    records[0]['path'] = 'mutated.docx'
    records.append({'path': 'b.xlsx'})

    assert panel.office_planning_files == () and panel.office_artifact_refs is None and panel.office_scope is None
    assert panel.mode.get() == ui.MODES[9]
    assert opened[0][0] == [{'path': 'a.docx', 'sha256': 'a' * 64}]
    opened[0][1](opened[0][0])
    assert scheduled[0][0] == 'office_scope'
    assert scheduled[0][1](object()) == {'files': ['a.docx']}
    assert selected == [[{'path': 'a.docx', 'sha256': 'a' * 64}]]
    assert model_calls == []


def test_use_generated_artifacts_busy_guard():
    panel = panel_stub()
    panel.running = True
    panel.get_artifacts = lambda: (_ for _ in ()).throw(AssertionError('must not read artifacts'))
    panel.use_generated_artifacts()
    assert panel.office_planning_files == ()


def test_poll_scope_result_opens_scope_selector(monkeypatch):
    result = {'files': ['a.docx'], 'artifact_refs': [{'artifact': 'a.docx'}], 'catalog': []}
    panel, _ = _async_panel(monkeypatch, [])
    opened = []
    monkeypatch.setattr('agent_service.office_scope_panel.open_scope_selector',
                        lambda root, value, callback: opened.append((value, callback)))
    panel.messages.put(('office_scope', result, None))

    panel.poll()

    assert opened[0][0] == result
    assert panel.office_artifact_refs is None and panel.office_scope is None
    assert '尚未调用模型' in panel.status.get()


def test_stopped_late_office_artifact_result_does_not_restore_refs(monkeypatch):
    panel, _ = _async_panel(monkeypatch, [])
    panel.stopped.set()
    panel.messages.put(('office_scope', {'files': ['late.docx']}, None))

    panel.poll()

    assert panel.office_artifact_refs is None and panel.office_planning_files == ()


def test_manual_file_selection_clears_artifact_refs(monkeypatch):
    panel = panel_stub()
    panel.office_artifact_refs = [{'artifact': 'generated.docx'}]
    install_dialog(monkeypatch, ('manual.xlsx',))

    panel.configure_office_intent()

    assert panel.office_artifact_refs is None
    assert panel.office_planning_files == ('manual.xlsx',)
    assert panel.office_scope is None


def test_propose_passes_deepcopied_artifact_refs(monkeypatch):
    panel = panel_stub()
    panel.office_planning_files = ('a.docx',)
    panel.office_artifact_refs = [{'artifact': 'a.docx', 'sha256': 'a' * 64}]
    panel.office_scope = [{'path': 'a.docx', 'locations': [{'at': 1}]}]
    panel.text = Value('更新标题')
    panel.stopped = threading.Event()
    scheduled = []
    panel.start = lambda kind, operation: scheduled.append((kind, operation))
    calls = []
    from agent_service import office_intent
    monkeypatch.setattr(office_intent, 'propose', lambda *args, **kwargs: calls.append((args, kwargs)) or {})

    panel.propose()
    panel.office_artifact_refs[0]['sha256'] = 'b' * 64
    panel.office_scope[0]['locations'][0]['at'] = 2
    scheduled[0][1](object())

    assert calls[0][0][1:3] == ('更新标题', ('a.docx',))
    assert calls[0][1]['artifact_refs'] == [{'artifact': 'a.docx', 'sha256': 'a' * 64}]
    assert calls[0][1]['scope'] == [{'path': 'a.docx', 'locations': [{'at': 1}]}]


def test_restart_empty_memory_discovers_batch_on_worker_then_opens_picker(monkeypatch):
    panel,scheduled=_async_panel(monkeypatch,[])
    records=[{'path':'recent.docx','sha256':'a'*64},{'path':'recent.xlsx','sha256':'b'*64}]
    calls=[];opened=[]
    monkeypatch.setattr('agent_service.office_handoff.latest_generated_outputs',lambda **kw: calls.append('worker') or records)
    monkeypatch.setattr('agent_service.office_scope_panel.open_file_selector',lambda root,value,callback: opened.append(value))
    panel.use_generated_artifacts()
    assert not calls and not opened and scheduled[0][0]=='generated_artifacts'
    result=scheduled[0][1](None)
    panel.messages.put(('generated_artifacts',result,None))
    panel.poll()
    assert calls==['worker'] and opened==[records]


def test_explicit_empty_batch_never_uses_history_fallback(monkeypatch):
    panel,scheduled=_async_panel(monkeypatch,[])
    monkeypatch.setattr('agent_service.office_handoff.latest_generated_outputs',lambda **kw: (_ for _ in ()).throw(AssertionError('no fallback')))
    def selector(root,records,callback):
        assert records==[]
        raise ValueError('没有可接入的 Office 文件。')
    monkeypatch.setattr('agent_service.office_scope_panel.open_file_selector',selector)
    panel.use_generated_artifacts([])
    assert not scheduled and '没有可接入' in panel.status.get()
