"""Grouped exact scopes: real snapshots and synthetic location evidence, no API/UI effects."""
from copy import deepcopy
from pathlib import Path

import pytest
from openpyxl import Workbook

from agent_service import located_edit_workflow as flow, computer_interaction as interaction
from agent_service import office_edit_adapter as adapter, computer_agent, windows_task as wt
from agent_service import windows_located_edit as located, windows_content_find as find
from agent_service.computer import ComputerSession, ComputerError, read_record
from test_windows_task import WINDOW, Backend, control, goal
from test_windows_located_edit import _word, _excel
from test_office_edit import word_fixture


@pytest.fixture
def grouped(tmp_path, monkeypatch):
    create = ComputerSession.create
    monkeypatch.setattr(flow.ComputerSession, 'create', lambda: create(tmp_path / 'sessions'))
    monkeypatch.setattr(adapter, 'DEFAULT_STORE', tmp_path / 'gates')
    word, excel = tmp_path/'original.docx', tmp_path/'original.xlsx'
    word_fixture(word)
    book=Workbook(); sheet=book.active; sheet.title='预算'
    sheet['B2']=5; sheet['B3']=7; sheet['B4']='=SUM(B2:B3)'
    book.save(excel); book.close()
    pairs=[_word(word,'预算'),_word(word,'待确认'),_excel(excel,'5'),_excel(excel,'7')]
    conditions=[{'id':f'hit{i}', 'window':deepcopy(WINDOW), 'selector':deepcopy(find.SELECTOR),
                 'state':'value','value':token,'retain':False} for i,(token,match) in enumerate(pairs)]
    by_token=dict(pairs)
    class Observed(Backend):
        def observe_condition(self, condition, *, stop):
            token=condition['value']
            return {'window':deepcopy(WINDOW),'application':'wps','target_selector':deepcopy(find.SELECTOR),
                    'dialogs':[],'content_token':token,'content_match':deepcopy(by_token[token]),
                    'controls':[control(**find.SELECTOR,value=token)],'truncated':False}
    parent=wt.create(tmp_path/'parents',goal(*conditions))
    result=wt.run(parent,Observed())
    assert result['ok']
    refs=[located.capture(parent,c['id']) for c in conditions]
    selections=[{'ref':ref,'after':after} for ref,after in zip(refs,['年度预算','已确认',9,11])]
    state=flow.prepare(parent,selections,'只改四处，保留公式和原格式')
    return state,parent,selections


def test_one_snapshot_and_step_per_file_with_all_original_scopes(grouped):
    state,parent,selections=grouped
    assert flow.load(state['session_root'])==state
    edits=state['plan']['goal']['edits']
    assert len(edits)==2 and [len(e['locations']) for e in edits]==[2,2]
    assert edits[0]['spec']['changes']==[{'paragraph_index':1,'before':'预算待确认','after':'年度预算已确认'}]
    assert [c['cell'] for c in edits[1]['spec']['changes']]==['B2','B3']
    assert not list(Path(state['session_root']).glob('goals/*.run'))
    assert len(list(Path(state['session_root']).glob('edit-inputs/*/source.json')))==2
    refreshed=flow.preview(state['session_root'])
    assert refreshed['plan']['goal']==state['plan']['goal']


@pytest.mark.parametrize('change',['drop_one','add_one','changed_after','drop_binding','both_shapes','broaden','other_file'])
def test_grouped_plan_cannot_reduce_or_expand_scope(grouped,change):
    state,parent,selections=grouped
    session=ComputerSession(state['session_root'])
    value=deepcopy(state['plan']['goal']); item=value['edits'][0]
    if change=='drop_one': item['locations'].pop()
    elif change=='add_one': item['locations'].append(deepcopy(item['locations'][0]))
    elif change=='changed_after': item['locations'][0]['after']='不同文字'
    elif change=='drop_binding': item.pop('locations')
    elif change=='both_shapes': item['location']=deepcopy(item['locations'][0])
    elif change=='broaden': item['spec']['changes'][0]['after']='另一整段'
    elif change=='other_file': item['locations'][0]=deepcopy(selections[2])
    with pytest.raises((ComputerError,ValueError)):
        plan=computer_agent.prepare(session,state['plan']['instruction'],value)
        flow.execute(session.root,plan)
    assert not list(session.root.glob('goals/*.run'))


def test_parent_change_blocks_grouped_refresh(grouped):
    state,parent,_=grouped
    (parent/'unexpected.json').write_text('{}')
    with pytest.raises(ComputerError,match='source_changed'):
        flow.preview(state['session_root'])


def test_generic_interaction_also_limits_total_locations(grouped):
    state,_,_=grouped
    value=deepcopy(state['plan']['goal'])
    for item in value['edits']:
        item['locations']=item['locations']*6
    with pytest.raises(ComputerError,match='interaction_location_limit'):
        interaction.validate_goal(value)


def test_grouped_order_continues_only_unstarted_file(grouped):
    state,_,_=grouped
    env=interaction.InteractionEnvironment.__new__(interaction.InteractionEnvironment)
    env.goal=state['plan']['goal']
    obs={'browser':None,'started_edits':[],'checks':{'sources_ready':True}}
    env._check=lambda:None
    env.observe=lambda:deepcopy(obs)
    assert env.actions(obs)==[{'kind':'office_edit','edit_id':'e1'}]
    obs['started_edits']=['e1']
    assert env.actions(obs)==[{'kind':'office_edit','edit_id':'e2'}]


def test_formula_stays_readonly_in_grouped_source(grouped):
    state,_,_=grouped
    item=state['plan']['goal']['edits'][1]
    public=read_record(Path(item['source_root'])/'source.json')['public']
    cell=next(c for s in public['observed']['sheets'] for c in s['cells'] if c['cell']=='B4')
    assert cell['editable'] is False and cell['value']=={'formula':'=SUM(B2:B3)'}
    with pytest.raises((ComputerError, FileNotFoundError)):
        flow.execute(state['session_root'],state['plan'])
    assert not list(Path(state['session_root']).glob('goals/*.run'))
