"""Model plans cannot invent cells or hide output coverage in workbook handoff."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from agent_service import assistant, workbook_source, workbook_source_host
from test_workbook_source import fixture, receipt
from test_assistant_source import FakeClient, _fake_office


@pytest.fixture
def workbook(tmp_path,monkeypatch):
    path = tmp_path/'input.xlsx'
    fixture(path)
    def native(path,directory,stop,deadline):
        assert not stop()
        value = receipt(path)
        workbook_source.office_source._write_json_new(directory/'source-native.json',value)
        return value
    monkeypatch.setattr(workbook_source_host,'native_snapshot_supervised',native)
    return path


def plan(sheets=None):
    return {'kind':'source_office','steps':[{'id':'s1','action':'document_from_workbook',
        'source_id':'selected','title':'全部工作表记录','sheets':sheets or ['预算 明细','汇总','保留页','空白页']}]}


def test_all_sheets_formula_context_then_independent_coverage_review(workbook,tmp_path,monkeypatch):
    specs = _fake_office(monkeypatch)
    client = FakeClient(plan(),{'approved':True,'reason':'all sheets, one Word requested'})
    result = assistant.run_task('全部工作表逐单元格记录成Word，保留坐标、公式原文和计算值',tmp_path/'tasks',
                                source_file=workbook,client=client)
    assert result['ok'],result
    context = json.loads(client.requests[0]['user'])['selected_source']
    assert context['observed']['sheets'][1]['values'][2][1] == 65
    assert '99999' not in json.dumps(context)
    review = json.loads(client.requests[1]['user'])
    assert review['workbook_selections'][0]['all_sheets'] is True
    assert review['proposed_outputs'] == [{'id':'s1','action':'document','title':'全部工作表记录','from':None}]
    assert result['workbook_selections'] == review['workbook_selections']
    assert ['汇总','B3','65',"='预算 明细'!B4*2+5"] in specs[0]['table']['rows']
    events = (Path(result['root'])/'events.jsonl').read_text(encoding='utf-8')
    assert 'workbook_source_reverified' in events


def test_range_values_chain_lists_every_output_for_review(workbook,tmp_path,monkeypatch):
    specs = _fake_office(monkeypatch)
    request = {'kind':'source_office','steps':[
        {'id':'s1','action':'spreadsheet_from_workbook_range','source_id':'selected','title':'提取',
         'sheet':'汇总','range':'A1:B4'},
        {'id':'s2','action':'report_from_sheet','from':'s1','title':'提取报告'}]}
    client = FakeClient(request,{'approved':True,'reason':'two requested outputs'})
    result = assistant.run_task('提取汇总 A1:B4 为值表Excel，再做Word报告',tmp_path/'tasks',
                                source_file=workbook,client=client)
    assert result['ok'],result
    assert specs[0]['rows'] == [['原合计',30],['调整后',65],['跨页求和',60]]
    outputs = json.loads(client.requests[1]['user'])['proposed_outputs']
    assert len(outputs)==2 and outputs[1]['action']=='document_from_sheet' and outputs[1]['from']=='s1'


def test_omitted_sheet_rejected_without_any_office_effect(workbook,tmp_path,monkeypatch):
    specs = _fake_office(monkeypatch)
    client = FakeClient(plan(['预算 明细']),{'approved':False,'reason':'omits three requested sheets'})
    result = assistant.run_task('全部四个工作表都要Word记录',tmp_path/'tasks',source_file=workbook,client=client)
    assert result['error']=='intent_review_rejected' and not specs
    audit = json.loads(client.requests[1]['user'])['workbook_selections'][0]
    assert not audit['all_sheets'] and len(audit['available_sheets'])==4


def test_extra_report_exposed_and_rejected(workbook,tmp_path,monkeypatch):
    specs = _fake_office(monkeypatch)
    request = {'kind':'source_office','steps':[
        {'id':'s1','action':'spreadsheet_from_workbook_range','source_id':'selected','title':'提取',
         'sheet':'汇总','range':'A1:B4'},
        {'id':'s2','action':'report_from_sheet','from':'s1','title':'多余Word'}]}
    client = FakeClient(request,{'approved':False,'reason':'extra Word not requested'})
    result = assistant.run_task('只要汇总 A1:B4 的值表Excel',tmp_path/'tasks',source_file=workbook,client=client)
    assert result['error']=='intent_review_rejected' and not specs
    assert len(json.loads(client.requests[1]['user'])['proposed_outputs'])==2


@pytest.mark.parametrize('target',['source.xlsx','source-native.json','source.json'])
def test_source_tampered_during_model_review_cannot_execute(workbook,tmp_path,monkeypatch,target):
    specs = _fake_office(monkeypatch)
    base = tmp_path/'tasks'
    class Client(FakeClient):
        def complete(self,**kwargs):
            answer = super().complete(**kwargs)
            if len(self.requests)==2:
                path = next(base.glob('task-*/sources/'+target))
                if target=='source.json':
                    value=json.loads(path.read_text(encoding='utf-8'))
                    value['display_name']='replacement.xlsx'
                    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
                else:
                    path.write_bytes(path.read_bytes()+b'changed')
            return answer
    result = assistant.run_task('全部表成Word',base,source_file=workbook,
        client=Client(plan(),{'approved':True,'reason':'all requested'}))
    assert result['error']=='workbook_source_changed' and not specs


def test_workbook_cannot_fall_back_to_invented_office_plan(workbook,tmp_path,monkeypatch):
    specs = _fake_office(monkeypatch)
    invented={'kind':'office','steps':[{'id':'s1','action':'document','data':{'kind':'document',
        'title':'invented','paragraphs':['99999']}}]}
    result=assistant.run_task('全部表成Word',tmp_path/'tasks',source_file=workbook,
                              client=FakeClient(invented,deepcopy(invented)))
    assert result['error']=='source_plan_required' and not specs
