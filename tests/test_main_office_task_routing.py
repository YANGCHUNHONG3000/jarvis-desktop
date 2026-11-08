"""Main Start reuses Office routing without opening apps or calling models."""

import pytest

import test_history_resume_gui as history_gui
from test_history_resume_gui import _button
from test_office_task_entry_gui import desktop, _closure, _controller


def _setup_batch(tmp_path, *, count=2):
    entry = _controller()
    context = _closure(entry.context)
    context['task_text'].insert('1.0', '把预算说明和审批文字更新为本轮确认值')
    for index in range(count):
        label = f'output-{index}'
        suffix = '.docx' if index % 2 else '.xlsx'
        context['agent_outputs'][label] = tmp_path / (label + suffix)
        context['agent_output_hashes'][label] = str(index + 1) * 64
    return entry, context


def _select(context, path, *, sha='a' * 64):
    ref = {'artifact': str(path), 'sha256': sha}
    source = context['source_choice']
    source.update(artifact_ref=ref, pending=False, auto_required=False,
                  retry_ref=None, retry_changed=False, setting=True)
    try:
        context['source_label'].set(str(path))
    finally:
        source['setting'] = False
    return ref


def test_main_start_routes_entire_empty_selection_batch_through_existing_entry(
        desktop, monkeypatch, tmp_path):
    entry, context = _setup_batch(tmp_path)
    from agent_service import daily_task_routing, office_handoff
    checked, routed = [], []
    refs = [{'artifact': str(path), 'sha256': context['agent_output_hashes'][label]}
            for label, path in context['agent_outputs'].items()]
    monkeypatch.setattr(office_handoff, 'select_outputs',
                        lambda records, **_kwargs: checked.append(records) or refs)
    monkeypatch.setattr(daily_task_routing, 'propose',
                        lambda session, instruction, selected, **_kwargs:
                        routed.append((instruction, selected)) or {'state': 'needs_input',
                                                                   'reason': '需要补充要求'})
    entry.session_factory = lambda: object()

    _button('开始任务').invoke()

    assert context['route_scope'].get() == '当前成果批次'
    assert entry.pending is not None
    history_gui._Thread.run('office-task-planning')
    entry.poll()
    assert checked == [[{'path': ref['artifact'], 'sha256': ref['sha256']} for ref in refs]]
    assert routed == [(context['task_text'].get(), refs)]
    assert history_gui._Process.created == []
    assert '需要补充要求' in entry.status.__self__.get()


def test_main_start_keeps_explicit_single_source_instead_of_expanding_to_batch(
        desktop, tmp_path):
    entry, context = _setup_batch(tmp_path)
    selected = _select(context, tmp_path / 'explicit.docx')

    _button('开始任务').invoke()

    assert context['route_scope'].get() == '当前选中资料'
    assert entry.pending['context']['records'] == [
        {'path': selected['artifact'], 'sha256': selected['sha256']}]
    assert entry.pending['context']['expected_refs'] == [selected]
    assert history_gui._Process.created == []


def test_main_start_honors_explicit_batch_despite_different_selected_source(desktop, tmp_path):
    entry, context = _setup_batch(tmp_path)
    _select(context, tmp_path / 'different.docx')
    context['route_scope'].set('当前成果批次')

    _button('开始任务').invoke()

    assert len(entry.pending['context']['records']) == 2
    assert entry.pending['context']['expected_refs'] is None


@pytest.mark.parametrize('reason', ['pending', 'auto_required', 'busy', 'draft', 'hash', 'drift'])
def test_main_start_cannot_bypass_unready_source_busy_or_draft(desktop, tmp_path, reason):
    entry, context = _setup_batch(tmp_path)
    if reason in {'pending', 'auto_required'}:
        context['source_choice'][reason] = True
    elif reason == 'busy':
        _closure(_button('开始任务').options['command'])['active']['kind'] = 'computer_goal'
    elif reason == 'draft':
        context['goal_panel'].text.insert('1.0', '另一个尚未完成的草稿')
    elif reason == 'hash':
        context['agent_output_hashes'].clear()
    else:
        _select(context, tmp_path / 'selected.docx')
        context['source_choice']['artifact_ref']['artifact'] = str(tmp_path / 'changed.docx')

    _button('开始任务').invoke()

    assert entry.pending is None
    assert not any(t.name == 'office-task-planning' for t in history_gui._Thread.all)
    assert history_gui._Process.created == []
    if reason == 'draft':
        assert context['goal_panel'].text.get() == '另一个尚未完成的草稿'


@pytest.mark.parametrize('explicit', [False, True])
def test_main_start_refuses_oversize_batch_without_falling_back_to_old_agent(
        desktop, tmp_path, explicit):
    entry, context = _setup_batch(tmp_path, count=5)
    if explicit:
        context['route_scope'].set('当前成果批次')

    _button('开始任务').invoke()

    assert entry.pending is None
    assert history_gui._Process.created == []
    assert not any(t.name == 'jarvis-agent' for t in history_gui._Thread.all)


def test_main_start_leaves_unverified_external_path_on_existing_validation_path(
        desktop, tmp_path):
    entry, context = _setup_batch(tmp_path)
    external = tmp_path / 'user-selected.docx'
    external.write_bytes(b'existing local source')
    context['source_label'].set(str(external))

    _button('开始任务').invoke()

    assert entry.pending is None
    assert context['route_scope'].get() == '当前选中资料'
    history_gui._Thread.run('jarvis-agent')
    assert history_gui._Process.created[0].command[-2:] == ['--source-file', str(external)]


def test_main_start_empty_explicit_batch_is_not_a_no_source_task(desktop):
    entry = _controller()
    context = _closure(entry.context)
    context['task_text'].insert('1.0', '更新文档标题')
    context['route_scope'].set('当前成果批次')

    _button('开始任务').invoke()

    assert entry.pending is None
    assert history_gui._Process.created == []
    assert not any(t.name == 'jarvis-agent' for t in history_gui._Thread.all)
