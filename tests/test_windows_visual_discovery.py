"""Offline contract tests; native acceptance is recorded separately."""
from copy import deepcopy
from hashlib import sha256
import json
import struct

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_visual_discovery as discovery
from agent_service import windows_visual_memory as memory
from agent_service import windows_visual_proposal as proposal
from agent_service import windows_visual as visual, windows_task as task
from agent_service import windows_intent as intent

WINDOW={'hwnd':100,'pid':200,'created':'offline','image':'C:/wps.exe','class_name':'OpusApp',
        'title':'offline.xlsx','profile':'observe_only'}


@pytest.fixture
def snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(memory,'OBSERVATION_BASE',tmp_path/'observations')
    width,height=900,180
    pixels=bytearray(b'\xff'*width*height*4)
    for y in (69,70):
        for x in range(50,90):pixels[(y*width+x)*4:(y*width+x)*4+4]=bytes((100,135,20,255))
    raw=struct.pack('<2sIHHI',b'BM',54+len(pixels),0,0,54)
    raw+=struct.pack('<IiiHHIIiiII',40,width,-height,1,32,0,len(pixels),0,0,0,0)+pixels
    key=sha256(raw).hexdigest()
    p=memory.OBSERVATION_BASE/'ocr-cache'/(key+'.bmp');p.parent.mkdir(parents=True);p.write_bytes(raw)
    words=[{'text':name,'box':[50+i*70,50,40,16]} for i,name in enumerate(['开始','插入','数据','公式'])]
    words+=[{'text':'样式','box':[400,100,30,16]},{'text':'数字格式','box':[500,100,70,16]}]
    return {'window':deepcopy(WINDOW),'application':'wps','rect':[0,0,900,800],
            'frame':{'schema':1,'sha256':key,'width':width,'height':height,'words':words}}


def legacy(snapshot):
    items=discovery.discover(snapshot)
    for item in items:item['source']['kind']='fresh_frame'
    return items


def obs():
    return {'window':deepcopy(WINDOW),'application':'wps','scope':'visible_controls',
            'controls':[],'dialogs':[],'truncated':False}


class Model:
    def __init__(self,value=None):self.calls=0;self.value=value or ['排序','筛选']
    def complete(self,system,context,*,max_tokens):
        self.calls+=1
        assert 'offline.xlsx' not in context and 'sha256' not in context and 'C:/wps.exe' not in context
        rows=json.loads(context)['windows'][0]['candidates']
        row=next(x for x in rows if x['label']=='数据')
        assert row['type']=='UnlearnedVisualNavigation'
        return {'conditions':[{'id':row['id'],'state':'navigate','value':self.value,'retain':False}]}


def test_fresh_observed_navigation_candidates_without_page_claim(snapshot):
    options=discovery.discover(snapshot)
    assert {x['label'] for x in options}=={'开始','插入','数据','公式'}
    assert all(x['expected']==[] for x in options)
    for item in options:discovery.verify(item,pending=True)


def test_body_or_command_label_does_not_become_navigation(snapshot):
    snapshot['frame']['words'].append({'text':'视图','box':[650,100,40,16]})
    assert '视图' not in {x['label'] for x in discovery.discover(snapshot)}


def test_duplicate_top_label_is_not_first_match(snapshot):
    snapshot['frame']['words'].append({'text':'数据','box':[680,50,40,16]})
    assert '数据' not in {x['label'] for x in discovery.discover(snapshot)}


def test_model_predictions_are_unverified_until_real_content_appears(snapshot):
    goal=intent.propose('查看数据工具',[obs()],Model(),visual_options=legacy(snapshot))
    c=goal['conditions'][0]
    control,details=visual.presentation(snapshot['frame'],c['selector'],c['value'])
    assert not details['verified'] and control['value']==''
    after=deepcopy(snapshot['frame'])
    after['words']=[w for w in after['words'] if w['box'][1]<80]
    after['words'] += [{'text':'排序','box':[200,100,40,16]},{'text':'筛选','box':[300,100,40,16]}]
    assert visual.presentation(after,c['selector'],c['value'])[1]['verified']


@pytest.mark.parametrize('value',[True,['排序'],['排序','排序'],['开始','筛选'],['筛选','自动筛选'],['样式','排序'],['数字格式','筛选']])
def test_invalid_or_already_visible_prediction_is_refused(snapshot,value):
    with pytest.raises(ComputerError):
        intent.propose('查看数据工具',[obs()],Model(value),visual_options=legacy(snapshot))


def test_fresh_source_is_immutable(snapshot,tmp_path):
    item=next(x for x in legacy(snapshot) if x['label']=='数据')
    item['expected']=['排序','筛选'];discovery.verify(item)
    path=memory._bitmap(snapshot['frame'])['bitmap']
    from pathlib import Path
    Path(path).write_bytes(b'changed')
    with pytest.raises(ComputerError):discovery.verify(item)


def test_generate_once_save_and_restore_pending_without_manual_labels(snapshot,tmp_path,monkeypatch):
    monkeypatch.setattr(memory,'discover',lambda *a:[])
    class Backend:
        def observe(self,*a,**kw):return obs()
        def visual_snapshot(self,*a,**kw):return deepcopy(snapshot)
        def visual_document_identity(self,*a,**kw):raise ComputerError('binding_unavailable')
    class SelectionModel(Model):
        def complete(self,system,context,*,max_tokens):
            self.calls+=1
            row=next(x for x in json.loads(context)['windows'][0]['candidates'] if x['label']=='数据')
            assert row['type']=='FreshVisualNavigation'
            return {'conditions':[{'id':row['id'],'state':'navigate','value':True,'retain':False}]}
    model=SelectionModel()
    result=proposal.propose('查看数据工具',[obs()],Backend(),model,
                            task_base=tmp_path/'tasks',proposal_base=tmp_path/'proposals')
    loaded=task.load(result['root'])
    assert model.calls==1 and loaded['summary']['approval_required']
    assert loaded['summary']['attempt']==0 and loaded['summary']['completed']==[]
    assert task.load(result['root'])==loaded
    with pytest.raises(ComputerError,match='approval_required'):task.run(result['root'],Backend())


def test_popup_snapshot_failure_stops_before_model(snapshot,tmp_path,monkeypatch):
    monkeypatch.setattr(memory,'discover',lambda *a:[])
    class Backend:
        def observe(self,*a,**kw):return obs()
        def visual_snapshot(self,*a,**kw):raise ComputerError('windows_visual_popup_requires_attention')
    model=Model()
    with pytest.raises(ComputerError,match='popup'):
        proposal.propose('查看数据工具',[obs()],Backend(),model,task_base=tmp_path/'tasks',proposal_base=tmp_path/'proposals')
    assert model.calls==0 and not (tmp_path/'tasks').exists()


def repaint(snapshot,markers):
    """Synthetic BMP only, no user image editing or native claims."""
    from pathlib import Path
    frame=deepcopy(snapshot['frame'])
    width,height=frame['width'],frame['height']
    raw=bytearray(Path(memory._bitmap(frame)['bitmap']).read_bytes())
    raw[54:]=b'\xff'*(width*height*4)
    for x0,y0,w,h,color in markers:
        for y in range(y0,y0+h):
            for x in range(x0,x0+w):raw[54+(y*width+x)*4:58+(y*width+x)*4]=bytes(color)
    key=sha256(raw).hexdigest();frame['sha256']=key
    (memory.OBSERVATION_BASE/'ocr-cache'/(key+'.bmp')).write_bytes(raw)
    return {**deepcopy(snapshot),'frame':frame}


def test_selection_goal_proves_actual_tab_and_live_toolbar(snapshot):
    c=visual.selection_condition(WINDOW,'开始')
    _,details=visual.presentation(snapshot['frame'],c['selector'],c['value'])
    assert details['verified']
    assert details['selection_readback']['selected_labels']==['开始']
    assert set(details['selection_readback']['content_labels'])=={'样式','数字格式'}
    other=visual.selection_condition(WINDOW,'数据')
    assert not visual.presentation(snapshot['frame'],other['selector'],other['value'])[1]['verified']


@pytest.mark.parametrize('markers',[
    [],[(50,69,40,4,(100,135,20,255))],[(50,69,40,2,(130,130,130,255))],
    [(50,69,40,2,(100,135,20,255)),(190,69,40,2,(100,135,20,255))],
    [(50,50,40,2,(100,135,20,255))],[(50,95,40,2,(100,135,20,255))],
    [(50,69,7,2,(100,135,20,255))],[(50,74,40,8,(100,135,20,255))],
    [(50,63,40,5,(100,135,20,255))],[(40,69,70,2,(100,135,20,255))]])
def test_missing_ambiguous_or_nonunderline_marker_never_proves_navigation(snapshot,markers):
    changed=repaint(snapshot,markers)
    assert discovery.discover(changed)==[]
    c=visual.selection_condition(WINDOW,'开始')
    assert not visual.presentation(changed['frame'],c['selector'],c['value'])[1]['verified']


def test_selected_tab_without_toolbar_is_not_complete(snapshot):
    snapshot['frame']['words']=[x for x in snapshot['frame']['words'] if x['box'][1]<80]
    assert discovery.discover(snapshot)==[]


def test_antialiased_underline_ends_need_uniform_core_and_same_hue(snapshot):
    changed=repaint(snapshot,[(50,69,40,2,(82,124,5,255)),
                             (50,69,1,2,(115,147,51,255)),(89,69,1,2,(115,147,51,255))])
    assert discovery.discover(changed)
    bad=repaint(snapshot,[(50,69,40,2,(82,124,5,255)),(50,69,1,2,(5,124,200,255))])
    assert discovery.discover(bad)==[]


def test_formula_bar_and_document_text_do_not_count_as_toolbar(snapshot):
    snapshot['frame']['words']=[x for x in snapshot['frame']['words'] if x['box'][1]<80]
    snapshot['frame']['words'] += [{'text':'A1','box':[50,148,24,16]},
                                  {'text':'项目','box':[200,148,24,16]}]
    assert discovery.discover(snapshot)==[]


def test_v2_does_not_accept_model_authored_verification_text(snapshot):
    class InvalidModel:
        def complete(self,system,context,*,max_tokens):
            row=next(x for x in json.loads(context)['windows'][0]['candidates'] if x['label']=='数据')
            return {'conditions':[{'id':row['id'],'state':'navigate','value':['invented','words'],'retain':False}]}
    with pytest.raises(ComputerError,match='target_denied'):
        intent.propose('查看数据工具',[obs()],InvalidModel(),visual_options=discovery.discover(snapshot))


@pytest.mark.parametrize('length,accepted',[(26,True),(25,False)])
def test_fractional_ocr_coverage_rounds_to_one_pixel(snapshot,length,accepted):
    snapshot['frame']['words'][0]['box']=[50,50,33,16]
    changed=repaint(snapshot,[(51,69,length,2,(82,124,5,255))])
    assert bool(discovery.discover(changed)) is accepted
