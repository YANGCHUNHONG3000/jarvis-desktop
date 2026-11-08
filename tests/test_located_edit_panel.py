"""Real Tk state tests with synthetic workflow results, never native Office or API."""
from copy import deepcopy
import json
import threading
import tkinter as tk
from types import SimpleNamespace

import pytest

from agent_service import located_edit_panel as ui


def choice(path='C:/synthetic/source.docx', *, editable=True, ident='word'):
    hit = {'text': 'Nested final', 'start': 52, 'end': 64}
    if path.endswith('.xlsx'):
        hit = {'text': '7', 'sheet': '预算', 'address': 'A1'}
    return {'condition_id': ident, 'editable': editable,
            'reason': '' if editable else 'windows_located_edit_formula_forbidden',
            'ref': {'token': json.dumps({'file': {'path': path}}), 'match': {'hit': hit}}}


def state(*, outcome=None, plan='initial'):
    return {'session_root': 'C:/synthetic/session', 'plan': {'id': plan},
            'candidate': 'synthetic', 'outcome': outcome}


class ImmediateThread:
    def __init__(self, *, target, **kwargs):
        self.target = target
    def start(self):
        self.target()


@pytest.fixture(scope='module')
def root():
    hidden = tk.Tk()
    hidden.withdraw()
    yield hidden
    hidden.destroy()


@pytest.fixture
def env(root, monkeypatch):
    calls, busy, delivered, panels = [], [], [], []
    def prepare(parent, selections, instruction):
        calls.append(('prepare', parent, deepcopy(selections), instruction))
        return state()
    def preview(session):
        calls.append(('preview', session))
        return state(plan='remaining')
    def execute(session, plan, **options):
        calls.append(('execute', session, deepcopy(plan), options))
        return state(outcome={'ok': True, 'state': 'verified'})
    fake = SimpleNamespace(prepare=prepare, preview=preview,
        load=lambda session: state(), execute=execute,
        outputs=lambda session: [{'path': 'copy.docx', 'sha256': 'abc'}])
    monkeypatch.setattr(ui, 'workflow', fake)
    monkeypatch.setattr(ui, 'describe', lambda plan: 'review '+plan['id'])
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [choice()])
    monkeypatch.setattr(ui.threading, 'Thread', ImmediateThread)
    def open_(**kwargs):
        p = ui.open_panel(root, on_busy=busy.append, on_artifacts=delivered.append, **kwargs)
        panels.append(p)
        p.poll()
        return p
    yield SimpleNamespace(open=open_, calls=calls, busy=busy, delivered=delivered, fake=fake,
                          root=root, panels=panels)
    for p in panels:
        if not p.closed:
            if p.running:
                p.poll()
            p.close()


def author(p, text='42'):
    p.rows[0]['selected'].set(True)
    p.rows[0]['value'].insert('1.0', text)
    p.rows[0]['value'].edit_modified(False)
    p.instruction.set('Replace the explicitly selected text')


def test_open_reads_choices_without_creating_session(env):
    p = env.open(task_root='parent')
    assert p.rows and not env.calls and p.session_root is None
    assert str(p.execute_button['state']) == 'disabled'
    assert p.rows[0]['value'].get('1.0', 'end-1c') == ''


def test_preview_is_explicit_and_word_new_value_remains_literal(env):
    p = env.open(task_root='parent')
    author(p, '42\nsecond line')
    assert not env.calls
    assert p.preview()
    p.poll()
    assert env.calls[0] == ('prepare', 'parent', [
        {'ref': p.rows[0]['choice']['ref'], 'after': '42\nsecond line'}], p.instruction.get())
    assert p.reviewed and p.session_root == 'C:/synthetic/session'
    assert p.preview_text.get('1.0', 'end-1c') == 'review initial'
    assert str(p.rows[0]['value']['state']) == 'disabled'


def test_excel_scalar_and_formula_refusal(env, monkeypatch):
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [
        choice('C:/synthetic/book.xlsx'), choice('C:/synthetic/formula.xlsx', editable=False, ident='formula')])
    p = env.open(task_root='parent')
    author(p, '42')
    p.preview()
    p.poll()
    assert env.calls[0][2][0]['after'] == 42
    assert str(p.rows[1]['check']['state']) == 'disabled'


def test_formula_programmatic_selection_cannot_prepare(env, monkeypatch):
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [choice(editable=False)])
    p = env.open(task_root='parent')
    p.rows[0]['selected'].set(True)
    p.instruction.set('attempt')
    assert not p.preview() and not env.calls
    assert '公式' in p.status.get()


def test_invalid_excel_object_preserves_draft(env, monkeypatch):
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [choice('C:/synthetic/book.xlsx')])
    p = env.open(task_root='parent')
    author(p, '{"x": 1}')
    assert not p.preview() and not env.calls
    assert p.rows[0]['value'].get('1.0', 'end-1c') == '{"x": 1}'


def test_more_than_four_distinct_files_refused(env, monkeypatch):
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [choice(f'C:/synthetic/{n}.docx') for n in range(5)])
    p = env.open(task_root='parent')
    for row in p.rows:
        row['selected'].set(True)
    p.instruction.set('explicit')
    assert not p.preview() and not env.calls


@pytest.mark.parametrize('count,allowed', [(2, True), (20, True), (21, False)])
def test_same_file_multiple_positions_and_total_limit(env, monkeypatch, count, allowed):
    monkeypatch.setattr(ui.windows_located_edit, 'choices', lambda _: [
        choice('C:/synthetic/one.docx',ident=f'hit{n}') for n in range(count)])
    p=env.open(task_root='parent')
    for n,row in enumerate(p.rows):
        row['selected'].set(True)
        row['value'].insert('1.0',f'new {n}')
    p.instruction.set('同文件多处精确修改')
    assert p.preview() is allowed
    p.poll()
    if allowed:
        assert len(env.calls[0][2])==count
        assert [part['after'] for part in env.calls[0][2]]==[f'new {n}' for n in range(count)]
        assert p.pause_button['text']=='每文件完成后暂停'
    else:
        assert not env.calls and '20' in p.status.get()


def test_readback_preview_dispatches_no_edit_and_requires_its_approval(env):
    value=state(outcome={'ok':False,'state':'unknown'})
    value['readback']={'records':[{'path':'kept.xlsx','sha256':'source'}]}
    value['readback_preview']={'sha256':'readback-approval'}
    env.fake.preview=lambda _:deepcopy(value)
    def recheck(session,**options):
        env.calls.append(('recheck',session,options))
        done=deepcopy(value); done['outcome']={'ok':True,'state':'verified'}
        return done
    env.fake.recheck=recheck
    p=env.open(session_root='saved')
    assert not p.execute()
    p.preview(); p.poll()
    assert '不重做编辑' in p.preview_text.get('1.0','end-1c')
    p.execute(); p.poll()
    assert env.calls[-1][0]=='recheck' and env.calls[-1][2]['approval']=='readback-approval'
    assert not any(c[0]=='execute' for c in env.calls)
    assert str(p.next_button['state'])=='normal'


def test_failure_preserves_values_and_allows_retry(env):
    p = env.open(task_root='parent')
    author(p, 'replacement')
    env.fake.prepare = lambda *args: (_ for _ in ()).throw(ValueError('source changed'))
    p.preview()
    p.poll()
    assert 'source changed' in p.status.get() and not p.reviewed
    assert p.rows[0]['value'].get('1.0', 'end-1c') == 'replacement'
    assert env.busy[-2:] == [True, False]


def test_resume_load_requires_fresh_preview_and_never_reprepares(env):
    p = env.open(session_root='saved-session')
    assert not p.execute() and not env.calls
    p.preview()
    p.poll()
    assert env.calls == [('preview', 'C:/synthetic/session')]
    p.execute()
    p.poll()
    assert env.calls[-1][0:3] == ('execute', 'C:/synthetic/session', {'id': 'remaining'})
    assert env.calls[-1][3]['pause_after_step'] is True
    assert not p.reviewed


def test_success_handoff_reverifies_outputs_before_callback(env):
    p = env.open(session_root='saved')
    p.state = state(outcome={'ok': True, 'state': 'verified'})
    assert p.next_task() and not env.delivered
    p.poll()
    assert env.delivered == [[{'path': 'copy.docx', 'sha256': 'abc'}]]


@pytest.mark.parametrize('outcome', [None, {'ok': False, 'state': 'verified'},
    {'ok': True, 'state': 'unknown'}, {'ok': True, 'state': 'paused'}, {'state': 'verified'}])
def test_only_verified_ok_allows_handoff(env, outcome):
    p = env.open(session_root='saved')
    p.state = state(outcome=outcome)
    p._refresh()
    assert not p.next_task() and str(p.next_button['state']) == 'disabled'


def test_duplicate_start_and_stale_generation_do_not_replace_current(env, monkeypatch):
    pending = []
    class HeldThread(ImmediateThread):
        def start(self):
            pending.append(self.target)
    monkeypatch.setattr(ui.threading, 'Thread', HeldThread)
    p = env.open(task_root='parent')
    assert p.running and not p.preview()
    p.messages.put((p.generation-1, 'choices', p.revision, [], None))
    p.poll()
    assert p.running and not p.rows
    pending.pop()()
    p.poll()
    assert p.rows and not p.running


def test_changed_draft_invalidates_inflight_preview_approval(env):
    p = env.open(task_root='parent')
    author(p)
    p.preview()
    p.instruction.set('changed while pending')
    p.poll()
    assert not p.reviewed and not p.execute()
    assert p.instruction.get() == 'changed while pending'
    assert p.session_root == 'C:/synthetic/session'
    p.preview()
    p.poll()
    assert env.calls[-1][0] == 'preview'


def test_close_running_sets_stop_and_waits_for_completion(env, monkeypatch):
    pending = []
    class HeldThread(ImmediateThread):
        def start(self):
            pending.append(self.target)
    monkeypatch.setattr(ui.threading, 'Thread', HeldThread)
    p = env.open(session_root='saved')
    p.close()
    assert p.closing and not p.closed and p.stop_event.is_set()
    assert p.top.winfo_exists()
    pending.pop()()
    p.poll()
    assert p.closed and not p.top.winfo_exists()
    assert env.busy[-2:] == [True, False]


def test_shared_busy_prevents_thread_and_backend(env):
    p = env.open(task_root='parent', busy=lambda: True)
    assert not p.running and not p.preview() and not env.busy and not env.calls


def test_thread_creation_failure_releases_busy(env, monkeypatch):
    class BrokenThread(ImmediateThread):
        def start(self):
            raise RuntimeError('synthetic thread failure')
    monkeypatch.setattr(ui.threading, 'Thread', BrokenThread)
    p = env.open(task_root='parent')
    assert not p.running and env.busy == [True, False]
    assert 'synthetic thread failure' in p.status.get()


def test_failed_outputs_verification_never_calls_handoff(env):
    p = env.open(session_root='saved')
    p.state = state(outcome={'ok': True, 'state': 'verified'})
    env.fake.outputs = lambda _: (_ for _ in ()).throw(ValueError('copy digest changed'))
    p.next_task()
    p.poll()
    assert not env.delivered and 'copy digest changed' in p.status.get()


def test_without_callback_goal_panel_binds_outputs_directly(env, monkeypatch):
    from agent_service import computer_goal_panel as goal_ui
    calls = []
    class Variable:
        def set(self, value):
            calls.append(('mode', value))
    class FakeGoalPanel:
        def __init__(self, tab, top, **kwargs):
            self.mode = Variable()
            self.top = top
        def mode_changed(self):
            calls.append(('mode_changed',))
        def prepare_generated_scope(self, records):
            calls.append(('scope', deepcopy(records)))
        def close(self):
            pass
    monkeypatch.setattr(goal_ui, 'GoalPanel', FakeGoalPanel)
    p = env.open(session_root='saved')
    p.on_artifacts = None
    p.state = state(outcome={'ok': True, 'state': 'verified'})
    p.next_task()
    p.poll()
    assert calls == [('mode', goal_ui.MODES[9]), ('mode_changed',),
        ('scope', [{'path': 'copy.docx', 'sha256': 'abc'}])]
    p.next_panel.top.destroy()
