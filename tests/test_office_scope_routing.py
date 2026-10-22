"""Offline GoalPanel routing checks for Office edit-scope selection."""
import threading
from test_office_intent_panel import panel_stub


def _scope_result():
    return {'files': ['a.docx'], 'artifact_refs': [{'artifact': 'a.docx', 'sha256': 'a' * 64}],
            'scope': [{'path': 'a.docx', 'locations': [{'at': {'paragraph_index': 0}, 'value': 'old'}]}]}


def test_accept_scope_deepcopies_installs_selection_and_invalidates_plan():
    panel = panel_stub()
    panel.closed = False
    panel.stopped = threading.Event()
    panel.plan = {'mode': 'agent'}
    shown = []
    panel.show_text = shown.append
    result = _scope_result()

    panel.accept_scope(result)
    result['scope'][0]['locations'][0]['value'] = 'mutated'
    result['artifact_refs'][0]['sha256'] = 'b' * 64

    assert panel.office_planning_files == ('a.docx',)
    assert panel.office_artifact_refs[0]['sha256'] == 'a' * 64
    assert panel.office_scope[0]['locations'][0]['value'] == 'old'
    assert panel.plan is None and panel.execute_button.options['state'] == 'disabled'
    assert '未调用模型' in panel.status.get()
    assert 'a.docx' in shown[-1]


def test_accept_scope_ignores_stopped_or_closed_panel():
    for closed, stopped in ((True, False), (False, True)):
        panel = panel_stub()
        panel.closed = closed
        panel.stopped = threading.Event()
        if stopped:
            panel.stopped.set()
        panel.accept_scope(_scope_result())
        assert panel.office_scope is None
        assert panel.office_planning_files == ()


def test_configure_scope_invalidates_and_schedules_local_inspection(monkeypatch):
    from agent_service import office_scope
    panel = panel_stub()
    panel.office_planning_files = ('a.docx',)
    panel.office_artifact_refs = [{'artifact': 'a.docx'}]
    panel.office_scope = [{'old': True}]
    panel.stopped = threading.Event()
    scheduled = []
    panel.start = lambda kind, operation: scheduled.append((kind, operation))
    calls = []
    monkeypatch.setattr(office_scope, 'inspect_files', lambda *args, **kwargs: calls.append((args, kwargs)) or {'ok': True})

    panel.configure_scope()
    result = scheduled[0][1](object())

    assert scheduled[0][0] == 'office_scope' and result == {'ok': True}
    assert calls[0][0][1] == ('a.docx',)
    assert calls[0][1]['artifact_refs'] == [{'artifact': 'a.docx'}]
    assert calls[0][1]['stop']() is False
    assert panel.office_scope == [{'old': True}]
    assert panel.plan is None
