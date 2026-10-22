"""Offline scope binding and multi-file/table planner boundaries."""
from copy import deepcopy
import json

import pytest

from agent_service import office_intent as intent
from agent_service import office_scope as scope
from agent_service.computer import ComputerSession, ComputerError
from agent_service.computer_interaction import validate_goal, describe
from test_office_edit import word_fixture, sheet_fixture
from test_office_table_edit import table_fixture
from test_office_intent import Client


@pytest.fixture
def catalog(tmp_path):
    files = [tmp_path/'table.docx',tmp_path/'sheet.xlsx',tmp_path/'plain.docx',tmp_path/'spare.docx']
    table_fixture(files[0]); sheet_fixture(files[1]); word_fixture(files[2]); word_fixture(files[3])
    session = ComputerSession.create(tmp_path/'sessions')
    return session,files,scope.inspect_files(session,files)


def test_selected_table_sheet_and_body_bind_three_files(catalog):
    session, files, observed = catalog
    indices = [(0,i) for i,r in enumerate(observed['files'][0]['entries']) if r['at'].get('table_index')==0]
    indices += [(1,i) for i,r in enumerate(observed['files'][1]['entries']) if r['at'].get('sheet')=='预算']
    indices += [(2,1)]
    selected = scope.selection_from_indices(observed,indices)
    assert selected['files']==tuple(str(p) for p in files[:3])
    client = Client(lambda ctx:{'kind':'edits','changes':[
        {'target':next(r['target'] for r in f['content'] if r['editable']), 'after':'范围已确认'}
        for f in ctx['files']]})
    result = intent.propose(session,'修改选中位置',selected['files'],client=client,scope=selected['scope'])
    assert len(validate_goal(result['plan']['goal'])['edits'])==3
    assert '表格1 行1 列1 第1段' in describe(result['plan'])
    ctx = client.contexts[0]
    assert '开头' not in json.dumps(ctx,ensure_ascii=False) and len(ctx['files'])==3
    assert all(r['at'].get('table_index')==0 for r in ctx['files'][0]['content'])
    assert len(ctx['files'][2]['content'])==1 and ctx['files'][2]['content'][0]['at']=={'paragraph_index':1}


def test_model_cannot_pick_a_real_but_unselected_target(catalog):
    session, files, observed = catalog
    selected = scope.selection_from_indices(observed,[(0,0)])
    from agent_service.computer import digest
    other = observed['files'][0]['entries'][1]['at']
    target = digest({'file':'e1','sha256':observed['files'][0]['sha256'],'at':other})[:24]
    client = Client(lambda _: {'kind':'edits','changes':[{'target':target,'after':'越界'}]})
    with pytest.raises(ComputerError,match='office_planning_target_invalid'):
        intent.propose(session,'改选中范围',selected['files'],client=client,scope=selected['scope'])


@pytest.mark.parametrize('damage',['hash','path','location','duplicate','missing'])
def test_stale_or_invalid_scope_never_calls_model(catalog, damage):
    session, files, observed = catalog
    selected=scope.selection_from_indices(observed,[(0,0)])
    damaged=deepcopy(selected['scope'])
    if damage=='hash': damaged[0]['sha256']='f'*64
    if damage=='path': damaged[0]['path']=str(files[1])
    if damage=='location': damaged[0]['locations']=[{'paragraph_index':99}]
    if damage=='duplicate': damaged[0]['locations']*=2
    if damage=='missing': damaged=[]
    client=Client()
    with pytest.raises(ComputerError):
        intent.propose(session,'编辑',selected['files'],scope=damaged,client=client)
    assert not client.contexts


def test_scope_selects_matching_reference_subset(catalog):
    _,_,observed=catalog
    observed['artifact_refs']=[{'artifact':f['path'],'sha256':f['sha256']} for f in observed['files']]
    result=scope.selection_from_indices(observed,[(2,1),(0,0),(2,1)])
    assert result['artifact_refs']==[observed['artifact_refs'][0],observed['artifact_refs'][2]]
    assert len(result['scope'][1]['locations'])==1


@pytest.mark.parametrize('indices',[[],[(-1,0)],[(0,-1)],[(4,0)],[(0,1000)],[(True,0)],[(0,)]])
def test_invalid_selection_rejected(catalog,indices):
    with pytest.raises(ComputerError): scope.selection_from_indices(catalog[2],indices)


def test_readonly_formula_alone_is_not_editable(catalog):
    _,_,observed=catalog
    ix=next(i for i,r in enumerate(observed['files'][1]['entries']) if not r['editable'])
    with pytest.raises(ComputerError): scope.selection_from_indices(observed,[(1,ix)])


def test_four_files_allowed_fifth_refused(catalog):
    session,files,observed=catalog
    selected=scope.selection_from_indices(observed,[(i,0) for i in range(4)])
    client=Client(lambda ctx:{'kind':'edits','changes':[{'target':f['content'][0]['target'],'after':'四文件'} for f in ctx['files']]})
    plan=intent.propose(session,'编辑四文件',selected['files'],scope=selected['scope'],client=client)['plan']
    assert len(validate_goal(plan['goal'])['edits'])==4
    bad=deepcopy(plan['goal']); bad['edits'].append(deepcopy(bad['edits'][0]))
    bad['edits'][-1]['id']='e5'
    with pytest.raises(ComputerError): validate_goal(bad)


def test_inspection_cancelled_and_duplicate_do_not_produce_catalog(catalog):
    session,files,_=catalog
    with pytest.raises(ComputerError,match='cancelled'):
        scope.inspect_files(session,files,stop=lambda:True)
    with pytest.raises(ComputerError,match='duplicate'):
        scope.inspect_files(session,[files[0],files[0]])
