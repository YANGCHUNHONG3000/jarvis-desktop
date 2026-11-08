from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest

from agent_service import windows_access as access, windows_content_find as find, windows_task as wt
from agent_service import windows_panel as wp, windows_content_query as query
from agent_service.computer import ComputerError, digest
from test_windows_panel import panel, hidden_root  # noqa: F401
from test_windows_handoff import saved, prepared, native  # noqa: F401
from test_windows_handoff_panel import setup_parent
from test_windows_task import WINDOW, goal


def test_panel_uses_existing_copy_snapshot_and_does_not_request_new_file(panel, saved, monkeypatch):
    p = setup_parent(panel, saved, monkeypatch)
    p.next_copy();p.poll()
    sources = deepcopy(p.sources)
    p.value.insert(0, 'Nested')
    p.content_occurrence.set('1')
    p.add_content_find()
    assert len(p.conditions) == 2 and p.sources == sources
    assert p.conditions[1]['selector'] == find.SELECTOR
    assert query.decode(p.conditions[1]['value'])['file'] == saved[2]['final']
    assert not any(c['retain'] for c in p.conditions)
    assert 'Nested' in p.targets.get(1) and p.task_root is None


@pytest.mark.parametrize('case', ['missing_source', 'empty_query', 'bad_occurrence', 'word_formula'])
def test_panel_invalid_content_request_keeps_previous_goals(panel, saved, monkeypatch, case):
    p = setup_parent(panel, saved, monkeypatch)
    if case != 'missing_source':
        p.next_copy();p.poll()
    if case != 'empty_query':
        p.value.insert(0, 'test')
    if case == 'bad_occurrence':
        p.content_occurrence.set('0')
    if case == 'word_formula':
        p.content_scope.set('公式')
    before = deepcopy((p.conditions, p.sources))
    p.add_content_find()
    assert (p.conditions, p.sources) == before and '无法添加内容定位' in p.status.get()


def target(token):
    return {'id': token, 'window': deepcopy(WINDOW), 'selector': deepcopy(find.SELECTOR),
            'state': 'value', 'value': token, 'retain': False}


class ContentBackend:
    def __init__(self):
        self.selected = None
        self.conditions = {}
        self.calls = []
        self.fail_before = self.fail_after = False

    def observed(self, token):
        return {'window':deepcopy(WINDOW), 'controls':[dict(find.SELECTOR, id='content',
            visible=True,enabled=True,value=token if self.selected==token else '')],
            'target_selector':deepcopy(find.SELECTOR),'application':'wps','dialogs':[], 'truncated':False,
            'content_token':token,'content_match':{'selected':self.selected==token,'hit':{'text':token},
                'total':1,'content_sha256':'a'*64}}

    def observe_condition(self, condition, **kwargs):
        token = condition['value']
        self.conditions[token] = condition
        return self.observed(token)

    candidates = staticmethod(access.candidates)

    def act(self, window, control, action, value, before_sha, operation_root, **kwargs):
        request={'op':'act','window':window,'selector':find.SELECTOR,'document_token':value,
                 'control':control,'action':action,'value':value,'before_sha':before_sha}
        try:
            return access._perform(request,operation_root,lambda:None,[0])
        except ComputerError as exc:
            state='unknown' if (operation_root/'native-intent.json').exists() else 'failed'
            return {'ok':False,'state':state,'effects':'unknown' if state=='unknown' else 'none','code':str(exc)}


@pytest.fixture
def content_native(monkeypatch):
    backend = ContentBackend()
    monkeypatch.setitem(sys.modules,'win32gui',SimpleNamespace(GetForegroundWindow=lambda:WINDOW['hwnd']))
    monkeypatch.setattr(access.uia,'_native',nullcontext)
    monkeypatch.setattr(access.uia,'_recheck',lambda window:window)
    monkeypatch.setattr(access,'_request_observation',lambda request,check:(backend.observed(request['value']),{}))
    def preflight(window,token,expected,check):
        if backend.fail_before:
            raise ComputerError('windows_content_changed')
        assert backend.observed(token)['content_match']==expected
    def select(window,token,expected,check):
        backend.calls.append(token)
        if backend.fail_after:
            raise ComputerError('windows_content_native_unavailable')
        backend.selected=token
    monkeypatch.setattr(find,'preflight',preflight)
    monkeypatch.setattr(find,'select_match',select)
    return backend


def test_content_engine_resume_does_not_select_completed_hit_again(tmp_path, content_native):
    b=content_native
    root=wt.create(tmp_path/'tasks',goal(target('first'),target('second')))
    assert wt.run(root,b,pause_after_step=True)['state']=='paused'
    first=wt._inventory(root/'attempts/0001')
    assert wt.run(root,b)['ok']
    assert b.calls==['first','second'] and wt._inventory(root/'attempts/0001')==first


def test_unknown_native_selection_never_replayed(tmp_path, content_native):
    b=content_native;b.fail_after=True
    root=wt.create(tmp_path/'tasks',goal(target('first')))
    assert wt.run(root,b)['state']=='unknown'
    assert list(root.glob('attempts/*/operations/*/native-intent.json'))
    with pytest.raises(ComputerError,match='windows_unknown_requires_review'):
        wt.run(root,b)
    assert b.calls==['first']


def test_preflight_failure_has_no_native_intent_or_selection(tmp_path, content_native):
    b=content_native;b.fail_before=True
    root=wt.create(tmp_path/'tasks',goal(target('first')))
    result=wt.run(root,b)
    assert result['state']=='failed' and not b.calls
    assert not list(root.glob('attempts/*/operations/*/native-intent.json'))


def test_changed_completed_last_selection_stops_without_reselect(tmp_path, content_native):
    b=content_native
    root=wt.create(tmp_path/'tasks',goal(target('first')))
    assert wt.run(root,b,pause_after_step=True)['state']=='paused'
    b.selected='manually moved'
    result=wt.run(root,b)
    assert not result['ok'] and result['code']=='windows_final_state_changed'
    assert b.calls==['first']
