"""History navigation must not execute tasks or overwrite main inputs."""
from pathlib import Path
from types import ModuleType

import pytest

from test_history_resume_gui import (
    _Process, _Thread, _Widget, _button, _pump, desktop, _replace_package_module,
)
from test_mail_review_panel import selected
from test_mail_followup_gui import _task_text


@pytest.fixture
def linked(desktop, monkeypatch):
    root, parent, tree, review, _preview = selected(desktop, monkeypatch)
    state = desktop[2]
    child = parent.parent / ('task-' + 'd' * 32)
    child.mkdir()
    state['export_inspection'] = {
        'root': str(child), 'state': 'succeeded', 'kind': 'office',
        'artifacts': [{'label': 's1 · 表格 (xlsx)', 'path': str(child / 'spreadsheet.xlsx'), 'sha256': 'a' * 64},
                      {'label': 's2 · 文档 (docx)', 'path': str(child / 'document.docx'), 'sha256': 'b' * 64}],
        'resume': {'eligible': False, 'reason': 'already_completed'}}
    module = ModuleType('agent_service.mail_review_navigation')
    result = {'parent_root': str(parent), 'child_root': str(child), 'token': 'e' * 64}
    module.resolve_continuation = lambda *_: dict(result)
    module.link_unchanged = lambda *_: True
    module.describe_error = lambda _: '关联记录无法核验；原选择保留，未执行任务。'
    _replace_package_module(monkeypatch, 'agent_service.mail_review_navigation', module)
    monkeypatch.setattr(_Widget, 'see', lambda self, item: setattr(self, 'seen', item), raising=False)
    review.inspect_resume = lambda *_: {'eligible': False, 'code': 'mail_review_resume_already_claimed'}
    tree.select(parent.name)
    _Thread.run('jarvis-history-inspect')
    _pump(root)
    return root, parent, child, tree, state, module, result


def test_direct_link_inserts_child_outside_recent_list_and_reuses_history_verifier(linked):
    root, parent, child, tree, state, _module, _result = linked
    draft = _task_text()
    draft.insert('1.0', '保留这份新草稿')
    source = next(w for w in _Widget.all if w.options.get('width') == 38).options['textvariable']
    source.set(str(parent.parent / 'existing-user-source.xlsx'))
    before = source.get()
    assert child.name not in tree.get_children()
    button = _button('查看续接任务和成果')
    assert button.cget('state') == 'normal'
    button.invoke()
    button.invoke()
    assert sum(t.name == 'jarvis-mail-review-navigate' for t in _Thread.all) == 1
    _Thread.run('jarvis-mail-review-navigate')
    _pump(root)
    assert tree.selection() == (child.name,) and tree.seen == child.name
    assert child.name in tree.get_children()
    # Real Tk queues this selection event; the deterministic fake does not.
    before_inspections = len(state['inspected'])
    tree.callbacks['<<TreeviewSelect>>']()
    _Thread.run('jarvis-history-inspect')
    _pump(root)
    assert len(state['inspected']) == before_inspections + 1
    assert _button('打开已核验成果').cget('state') == 'normal'
    assert _button('用于下一任务').cget('state') == 'normal'
    assert any(w.options.get('values') == ('s1 · 表格 (xlsx)', 's2 · 文档 (docx)') for w in _Widget.all)
    assert draft.get('1.0', 'end-1c') == '保留这份新草稿' and source.get() == before
    assert not _Process.created
    assert not any(t.name in ('jarvis-agent', 'jarvis-mail-review-prepare') for t in _Thread.all)


@pytest.mark.parametrize('change', ['draft', 'source', 'selection', 'refresh', 'goal_cycle', 'close'])
def test_late_navigation_does_not_steal_selection_after_context_change(linked, change):
    root, parent, child, tree, state, _module, _result = linked
    _button('查看续接任务和成果').invoke()
    if change == 'draft':
        _task_text().insert('1.0', '后来写的新稿')
    elif change == 'source':
        source = next(w for w in _Widget.all if w.options.get('width') == 38)
        source.options['textvariable'].set('later-source.xlsx')
    elif change == 'selection':
        tree.select(parent.name)
    elif change == 'refresh':
        _button('刷新').invoke()
    elif change == 'goal_cycle':
        state['goal_panel'].on_busy(True)
        state['goal_panel'].on_busy(False)
    elif change == 'close':
        root.callbacks['WM_DELETE_WINDOW']()
    _Thread.run('jarvis-mail-review-navigate')
    _pump(root)
    assert tree.selection() != (child.name,) and child.name not in tree.get_children()
    assert not _Process.created


@pytest.mark.parametrize('failure', ['exception', 'guard', 'parent_changed'])
def test_bad_link_keeps_parent_and_never_exposes_raw_exception(linked, failure):
    root, parent, child, tree, _state, module, result = linked
    if failure == 'exception':
        def fail(*_):
            raise ValueError('PRIVATE_MAIL_AND_PROVIDER_ERROR')
        module.resolve_continuation = fail
    elif failure == 'guard':
        module.link_unchanged = lambda *_: False
    else:
        result['parent_root'] = str(child)
    _button('查看续接任务和成果').invoke()
    _Thread.run('jarvis-mail-review-navigate')
    _pump(root)
    assert tree.selection() == (parent.name,) and not _Process.created
    assert not any('PRIVATE_MAIL' in str(w.options['textvariable'].get())
                   for w in _Widget.all if w.options.get('textvariable'))


def test_navigation_timeout_ignores_late_result(linked, monkeypatch):
    from agent_service import mail_review_navigation_panel
    root, parent, child, tree, *_ = linked
    tick = [0]
    monkeypatch.setattr(mail_review_navigation_panel.time, 'monotonic', lambda: tick[0])
    _button('查看续接任务和成果').invoke()
    tick[0] = 31
    _pump(root)
    _Thread.run('jarvis-mail-review-navigate')
    _pump(root)
    assert tree.selection() == (parent.name,) and child.name not in tree.get_children()
    assert not _Process.created


def test_unclaimed_task_has_no_navigation_button(desktop):
    assert _button('查看续接任务和成果').cget('state') == 'disabled'
