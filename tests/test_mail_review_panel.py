"""Offline native-panel wiring; cloud and Office acceptance are separate."""
from copy import deepcopy
import json
from pathlib import Path
from types import ModuleType

import pytest

import run_jarvis_assistant as launcher
from test_history_resume_gui import (
    _Process, _Thread, _Widget, _button, _pump, desktop, _replace_package_module,
)
from test_mail_followup_gui import _mail_source, _task_text


def selected(desktop, monkeypatch):
    root, task, _state, tree, _original = desktop
    (task / 'mail-analysis-request.json').write_text('{"schema":4}', encoding='utf-8')
    preview = {'eligible': True, 'task_root': str(task), 'token': 'e' * 64,
               'instruction': '整理旧邮件为Word和Excel，不发信。',
               'source_ref': _mail_source(task), 'code': 'mail_review_resume_ready'}
    module = ModuleType('agent_service.mail_review_resume')
    module.inspect_resume = lambda *_args: deepcopy(preview)
    module.prepare_resume = lambda *_args: deepcopy(preview)
    module.describe = lambda _value: '只重新复核，不重新生成；原失败保留。'
    _replace_package_module(monkeypatch, 'agent_service.mail_review_resume', module)
    tree.select(tree.selection()[0])
    _Thread.run('jarvis-history-inspect')
    _pump(root)
    return root, task, tree, module, preview


def test_explicit_review_launch_reuses_old_request_and_preserves_new_main_draft(desktop, monkeypatch):
    root, task, _tree, _module, preview = selected(desktop, monkeypatch)
    draft = _task_text()
    draft.insert('1.0', '这是另一个尚未提交的新草稿')
    button = _button('继续邮件复核（云端）')
    assert button.cget('state') == 'normal'
    before = (task / 'result.json').read_bytes()
    button.invoke()
    button.invoke()
    assert sum(t.name == 'jarvis-mail-review-prepare' for t in _Thread.all) == 1
    assert not _Process.created
    _Thread.run('jarvis-mail-review-prepare')
    _pump(root)
    _Thread.run('jarvis-agent')
    assert len(_Process.created) == 1
    command = _Process.created[0].command
    assert '--mail-understanding' in command
    assert json.loads(command[command.index('--mail-review-resume-json') + 1]) == {
        'task_root': str(task), 'token': preview['token']}
    request = Path(command[command.index('--text-file') + 1])
    assert request.read_text('utf-8') == preview['instruction']
    assert json.loads(command[command.index('--source-artifact-json') + 1]) == preview['source_ref']
    assert draft.get('1.0', 'end-1c') == '这是另一个尚未提交的新草稿'
    assert (task / 'result.json').read_bytes() == before
    assert button.cget('state') == 'disabled'


@pytest.mark.parametrize('change', ['draft', 'selection', 'refresh', 'running'])
def test_late_preparation_cannot_launch_after_context_changes(desktop, monkeypatch, change):
    root, _task, tree, _module, _preview = selected(desktop, monkeypatch)
    button = _button('继续邮件复核（云端）')
    button.invoke()
    if change == 'draft':
        _task_text().insert('1.0', '新的要求不能被覆盖')
    elif change == 'selection':
        tree.select(tree.selection()[0])
    elif change == 'refresh':
        _button('刷新').invoke()
    else:
        _task_text().insert('1.0', '新任务')
        _button('开始任务').invoke()
    _Thread.run('jarvis-mail-review-prepare')
    _pump(root)
    assert not any('--mail-review-resume-json' in p.command for p in _Process.created)
    assert not any(t.name == 'jarvis-agent' for t in _Thread.all) or change == 'running'


@pytest.mark.parametrize('changed', ['token', 'instruction', 'source_ref', 'task_root', 'exception'])
def test_changed_or_failed_preparation_is_not_paid_or_dispatched(desktop, monkeypatch, changed):
    root, _task, _tree, module, preview = selected(desktop, monkeypatch)
    def prepare(*_args):
        if changed == 'exception':
            raise ValueError('private provider content')
        value = deepcopy(preview)
        value[changed] = {} if changed == 'source_ref' else 'changed'
        return value
    module.prepare_resume = prepare
    _button('继续邮件复核（云端）').invoke()
    _Thread.run('jarvis-mail-review-prepare')
    _pump(root)
    assert not _Process.created
    assert not any(t.name == 'jarvis-agent' for t in _Thread.all)
    assert not any('private provider content' in str(w.options.get('textvariable').get())
                   for w in _Widget.all if w.options.get('textvariable'))


def test_nonmail_history_keeps_review_disabled(desktop):
    assert _button('继续邮件复核（云端）').cget('state') == 'disabled'


def test_preparation_timeout_ignores_late_result(desktop, monkeypatch):
    from agent_service import mail_review_panel
    root, _task, _tree, _module, _preview = selected(desktop, monkeypatch)
    tick = [0]
    monkeypatch.setattr(mail_review_panel.time, 'monotonic', lambda: tick[0])
    _button('继续邮件复核（云端）').invoke()
    tick[0] = 31
    _pump(root)
    _Thread.run('jarvis-mail-review-prepare')
    _pump(root)
    assert not _Process.created
    assert not any(t.name == 'jarvis-agent' for t in _Thread.all)
    assert _button('继续邮件复核（云端）').cget('state') == 'disabled'


def test_claimed_parent_shows_resume_guidance_without_enabling_replay(desktop, monkeypatch):
    root, _task, tree, module, _preview = selected(desktop, monkeypatch)
    module.inspect_resume = lambda *_args: {'eligible': False, 'code': 'mail_review_resume_already_claimed'}
    module.describe = lambda _value: '已经认领，请检查新任务记录；不能重复付费。'
    tree.select(tree.selection()[0])
    _Thread.run('jarvis-history-inspect')
    _pump(root)
    assert _button('继续邮件复核（云端）').cget('state') == 'disabled'
    assert any('已经认领，请检查新任务记录' in str(w.options['textvariable'].get())
               for w in _Widget.all if w.options.get('textvariable'))
    summaries = [str(w.options['textvariable'].get()) for w in _Widget.all
                 if w.options.get('textvariable') and
                 '已创建一次复核续接' in str(w.options['textvariable'].get())]
    assert len(summaries) == 1 and '原任务：失败' in summaries[0]
    assert '若属技能任务' not in summaries[0] and '无法核验' not in summaries[0]
    assert not _Process.created


@pytest.mark.parametrize('options', [
    {'mail_understanding': False, 'mail_review_resume': {'task_root': 'x', 'token': 'a' * 64}},
    {'mail_understanding': True, 'mail_review_resume': []},
    {'mail_understanding': True, 'mail_review_resume': {'task_root': 'x', 'token': 'bad'}},
])
def test_command_rejects_ambiguous_review_launch(options):
    with pytest.raises(ValueError):
        launcher._task_command('request.txt', 'new-task', source_artifact={'version': 1}, **options)
