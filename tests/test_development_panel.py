import tkinter as tk
from types import SimpleNamespace
import pytest
from agent_service import development_panel as ui, adapter_development as dev


@pytest.fixture(scope='module')
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def panel(monkeypatch, tmp_path, tk_root):
    root = tk_root
    owner = SimpleNamespace(root=root, running=False, external_busy=lambda: False,
                            on_artifacts=lambda value: None)
    owner.set_busy = lambda value: setattr(owner, 'running', value)
    monkeypatch.setattr(ui.DevelopmentPanel, 'refresh', lambda self: None)
    p = ui.DevelopmentPanel(owner, base=tmp_path/'tasks')
    yield p
    p.running = False
    p.close()


def _show(p, actions=('test',)):
    proposal = {'digest': 'a'*64, 'manifest': {'name': '表格转存', 'version': 1},
                'state': 'candidate', 'effect': '保留全部行列', 'actions': list(actions),
                'source_artifact': {'artifact': 'C:/owned/document.docx'}, 'stages': {}}
    p.row = {'task_root': 'task', 'instruction': '原表转存', 'state': 'proposed', 'proposals': [proposal]}
    p.proposals = [proposal]
    p.versions.configure(values=['v1'])
    p.versions.current(0)
    p.show()
    return proposal


def test_detail_separates_request_proposal_and_gates(panel):
    _show(panel)
    text = panel.detail.get('1.0', 'end')
    assert '原任务：原表转存' in text and '作用范围：保留全部行列' in text
    assert '固定来源：C:/owned/document.docx' in text and '独立代码审查由开发者记录' in text
    assert str(panel.buttons['test']['state']) == 'normal'
    assert str(panel.buttons['activate_one']['state']) == 'disabled'
    panel.proposals[0]['stages'] = {'activation': True}
    panel.show()
    assert str(panel.buttons['suspend']['state']) == 'normal'


@pytest.mark.parametrize('recipe,expected', [
    ('workbook_audit_report', '成果中的公式作为文字记录'),
    ('document_structure_report', '成果不保留原版式'),
])
def test_complex_recipe_explains_complete_scope_without_enabling(panel, recipe, expected):
    proposal = _show(panel)
    proposal['manifest']['recipe'] = recipe
    panel.show()
    assert expected in panel.detail.get('1.0', 'end')
    assert str(panel.buttons['activate_one']['state']) == 'disabled'
    assert str(panel.buttons['activate_three']['state']) == 'disabled'


@pytest.mark.parametrize('action', ['test', 'trial', 'resume', 'verify_live', 'activate_one', 'activate_three', 'suspend'])
def test_dispatch_pins_version_and_never_exposes_review(panel, monkeypatch, action):
    _show(panel, [action, 'review_cli'])
    pending, calls = [], []
    monkeypatch.setattr(panel, 'start', lambda kind, work: pending.append((kind, work)))
    target = 'activate' if action.startswith('activate') else action
    monkeypatch.setattr(dev, target, lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(dev, 'inspect', lambda *a, **k: {'inspected': (a, k)})
    panel.run(action)
    pending[0][1]()
    assert calls[0][0] == ('task', panel.base, panel.store, 'a'*64)
    if action in {'trial', 'resume'}:
        assert calls[0][1]['stop']() is False
        panel.stop_event.set()
        assert calls[0][1]['stop']() is True
    if action.startswith('activate'):
        assert calls[0][1]['runs'] == (1 if action == 'activate_one' else 3)
    assert 'review_cli' not in panel.buttons


def test_denied_or_busy_action_does_not_start(panel, monkeypatch):
    _show(panel)
    calls = []
    monkeypatch.setattr(panel, 'start', lambda *a: calls.append(a))
    panel.run('trial')
    assert calls == []


def test_failure_disables_stale_approvals(panel):
    _show(panel)
    panel.running = panel.owner.running = True
    panel.messages.put(('test', None, 'development_evidence_changed'))
    panel.poll()
    assert not panel.running and not panel.owner.running
    assert all(str(b['state']) == 'disabled' for b in panel.buttons.values())
    assert 'development_evidence_changed' in panel.status.get()


def test_verified_trial_binds_next_task_and_callback_failure_is_honest(panel):
    p = _show(panel)
    p['artifacts'] = [{'path': 'owned.xlsx', 'sha256': 'b'*64}]
    captured = []
    panel.owner.on_artifacts = captured.append
    panel.messages.put(('trial', panel.row, None))
    panel.poll()
    assert captured == [p['artifacts']]
    panel.owner.on_artifacts = lambda _: (_ for _ in ()).throw(ValueError())
    panel.messages.put(('resume', panel.row, None))
    panel.poll()
    assert '已保存' in panel.status.get() and '未完成' in panel.status.get()


def test_close_running_signals_stop_without_destroy(panel):
    panel.running = True
    panel.close()
    assert panel.stop_event.is_set() and not panel.closed and panel.window.winfo_exists()


def test_corrupt_proposal_is_readable_and_cannot_run(panel):
    row = {'task_root': 'task', 'instruction': '读取文件', 'state': 'queued',
           'proposals': [{'digest': 'c'*64, 'state': 'needs_review', 'actions': [], 'stages': {}}]}
    panel.messages.put(('inspect', row, None))
    panel.poll()
    assert '方案需审查' in panel.versions.get()
    assert '证据变化' in panel.detail.get('1.0', 'end')
    assert all(str(b['state']) == 'disabled' for b in panel.buttons.values())


def test_refresh_keeps_chosen_version(panel):
    first = _show(panel)
    second = {**first, 'digest': 'b'*64, 'manifest': {'name': '新版', 'version': 2}}
    panel.messages.put(('test', {**panel.row, 'proposals': [first, second]}, None))
    panel.poll()
    assert panel.selected()['digest'] == first['digest']


def test_refresh_keeps_selected_request(panel, monkeypatch):
    _show(panel)
    selected = []
    monkeypatch.setattr(panel, 'select_request', lambda: selected.append(panel.requests.current()))
    rows = [{'task_root': 'another', 'instruction': '另一请求', 'state': 'queued', 'versions': []},
            {'task_root': 'task', 'instruction': '原表转存', 'state': 'queued', 'versions': ['a'*64]}]
    panel.messages.put(('list', rows, None))
    panel.poll()
    assert selected == [1] and '已有开发方案' in panel.requests.get()
