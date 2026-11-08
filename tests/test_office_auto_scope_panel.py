"""Automatic previous-result scope stays bounded and tied to its UI request."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from agent_service import computer_goal_panel as ui, office_handoff, office_scope, office_scope_panel
from test_office_intent_panel import panel_stub, Value


def ready_panel():
    panel=panel_stub()
    panel.closed=False
    panel.stopped=threading.Event()
    panel.text=Value('把文字改成已核验，并更新预算')
    panel.session=SimpleNamespace(root=Path('session'))
    panel.accepted=[]
    def accept(value):
        panel.accepted.append(deepcopy(value))
        panel.office_planning_files=value['files']
    panel.accept_scope=accept
    return panel


def catalog(count=2, editable=True):
    return {'files':[{'path':'a.docx','sha256':'a'*64,'kind':'document',
        'entries':[{'at':{'paragraph_index':i},'value':'old','editable':editable} for i in range(count)]},
        {'path':'b.xlsx','sha256':'b'*64,'kind':'spreadsheet','entries':[
            {'at':{'sheet':'预算','cell':'B2'},'value':19,'editable':True},
            {'at':{'sheet':'预算','cell':'B4'},'value':{'formula':'=SUM(B2:B3)'},'editable':False}]}],
        'artifact_refs':[{'id':'word-proof'},{'id':'excel-proof'}]}


def pending(panel, value=None):
    panel._generated_scope_pending=1
    return {'catalog':value or catalog(),'generation':1,'context':panel._generated_scope_context(),
            'session_root':str(panel.session.root)}


def test_previous_small_batch_does_not_open_file_picker(monkeypatch):
    panel=ready_panel()
    panel.mode_changed=lambda:None
    scheduled=[]
    panel.prepare_generated_scope=scheduled.append
    monkeypatch.setattr(office_scope_panel,'open_file_selector',lambda *a:pytest.fail('unnecessary picker'))
    records=[{'path':'a.docx'},{'path':'b.xlsx'}]
    panel.use_generated_artifacts(records)
    assert scheduled==[records] and panel.mode.get()==ui.MODES[9]


def test_more_than_four_keeps_explicit_file_selection(monkeypatch):
    panel=ready_panel()
    panel.mode_changed=lambda:None
    dialogs=[]
    monkeypatch.setattr(office_scope_panel,'open_file_selector',lambda *a:dialogs.append(a[1]))
    records=[{'path':f'{n}.docx'} for n in range(5)]
    panel.use_generated_artifacts(records)
    assert dialogs==[records]


def test_prepare_freezes_records_and_uses_verified_source_catalog(monkeypatch):
    panel=ready_panel()
    tasks=[]
    panel.start=lambda kind,work:tasks.append((kind,work))
    seen=[]
    monkeypatch.setattr(office_handoff,'select_outputs',lambda rows:seen.append(deepcopy(rows)) or [{'artifact':'a.docx'}])
    monkeypatch.setattr(office_scope,'inspect_files',lambda session,files,**kw: catalog())
    records=[{'path':'a.docx'}]
    panel.prepare_generated_scope(records)
    records[0]['path']='changed.docx'
    kind,work=tasks[0]
    result=work(panel.session)
    assert kind=='generated_scope' and seen==[[{'path':'a.docx'}]]
    assert result['session_root']==str(panel.session.root)


def test_all_supported_rows_autoselected_formula_stays_context_only(monkeypatch):
    panel=ready_panel()
    result=pending(panel)
    monkeypatch.setattr(office_scope_panel,'open_scope_selector',lambda *a:pytest.fail('unnecessary range picker'))
    panel.accept_generated_scope(result)
    accepted=panel.accepted[0]
    assert accepted['files']==('a.docx','b.xlsx')
    assert [len(f['locations']) for f in accepted['scope']]==[2,2]
    assert accepted['artifact_refs']==result['catalog']['artifact_refs']
    assert result['catalog']['files'][1]['entries'][1]['editable'] is False
    assert panel._generated_scope_pending is None and '尚未调用模型' in panel.status.get()
    panel.accept_generated_scope(result)
    assert len(panel.accepted)==1


@pytest.mark.parametrize('change',['draft','files','scope','mode','plan','session','stop','busy','generation','closed'])
def test_late_intake_does_not_overwrite_current_inputs(change):
    panel=ready_panel()
    result=pending(panel)
    if change=='draft':panel.text.set('new draft')
    elif change=='files':panel.office_planning_files=('new.docx',)
    elif change=='scope':panel.office_scope=[{'new':1}]
    elif change=='mode':panel.mode.set(ui.MODES[0])
    elif change=='plan':panel.plan={'new_plan':True}
    elif change=='session':panel.session=SimpleNamespace(root=Path('new-session'))
    elif change=='stop':panel.stopped.set()
    elif change=='busy':panel.external_busy=lambda:True
    elif change=='generation':panel._generated_scope_pending=2
    else:panel.closed=True
    before=panel._generated_scope_context()
    panel.accept_generated_scope(result)
    assert not panel.accepted and panel._generated_scope_context()==before


def test_oversize_falls_back_without_truncation_and_modal_is_still_guarded(monkeypatch):
    panel=ready_panel()
    result=pending(panel,catalog(121))
    dialogs=[]
    monkeypatch.setattr(office_scope_panel,'open_scope_selector',lambda root,data,accept:dialogs.append((data,accept)))
    panel.accept_generated_scope(result)
    assert not panel.accepted and len(dialogs[0][0]['files'][0]['entries'])==121
    selection=office_scope.selection_from_indices(result['catalog'],[(0,0)])
    panel.text.set('a newer instruction')
    dialogs[0][1](selection)
    assert not panel.accepted


def test_modal_accepts_valid_narrowed_range_once(monkeypatch):
    panel=ready_panel()
    result=pending(panel,catalog(121))
    dialogs=[]
    monkeypatch.setattr(office_scope_panel,'open_scope_selector',lambda root,data,accept:dialogs.append(accept))
    panel.accept_generated_scope(result)
    selection=office_scope.selection_from_indices(result['catalog'],[(0,0)])
    dialogs[0](selection)
    dialogs[0](selection)
    assert panel.accepted==[selection]


def test_draft_revision_catches_changed_then_restored_text():
    panel=ready_panel()
    draft={'text':'same','revision':1}
    panel.instruction_draft=SimpleNamespace(snapshot=lambda:dict(draft))
    result=pending(panel)
    draft['revision']=3
    panel.accept_generated_scope(result)
    assert not panel.accepted
