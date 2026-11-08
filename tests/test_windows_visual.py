from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_visual as visual, windows_access as access, windows_task as task


def selector(label='视图'):
    return {'type':visual.TYPE,'name':label,'automation_id':visual.ID}


VALUE=json.dumps({'all':['显示比例','冻结窗格']},ensure_ascii=False,separators=(',',':'))


def frame(*extra):
    return {'schema':1,'sha256':'a'*64,'width':900,'height':240,'words':[
        {'text':'开始','box':[50,50,30,16]}, {'text':'插入','box':[110,50,30,16]},
        {'text':'视图','box':[350,50,30,16]}, *extra]}


def word(text,x=100,y=100):
    return {'text':text,'box':[x,y,60,16]}


def observation(f=None):
    c,d=visual.presentation(f or frame(),selector(),VALUE)
    return {'window':{},'application':'wps','controls':[c],'dialogs':[],
            'truncated':False,'visual':d}


@pytest.mark.parametrize('label',['删除','保存','发送','购买','登录','用户自定按钮'])
def test_navigation_is_not_arbitrary_click(label):
    with pytest.raises(ComputerError,match='goal_invalid'):
        visual.decode(selector(label),VALUE)


@pytest.mark.parametrize('items', [['视图','冻结窗格'],['开始','审阅'],['显示比例'],
    ['比例','显示比例'],['冻结窗格','冻结窗格'],['显示\n比例','冻结窗格']])
def test_completion_requires_distinct_non_navigation_labels(items):
    with pytest.raises(ComputerError,match='goal_invalid'):
        visual.decode(selector(),json.dumps({'all':items}))


def test_split_chinese_characters_are_joined_only_on_same_line():
    f=frame(word('冻结',100),word('窗格',162))
    assert visual.spans(f,'冻结窗格') == [[100,100,122,16]]
    f['words'][-1]['box'][1]=130
    assert visual.spans(f,'冻结窗格') == []


def test_word_formula_command_is_not_a_navigation_tab():
    f=frame(word('公式',200,105),word('显示比例',400,130),word('冻结窗格',600,130))
    c,d=visual.presentation(f,selector('公式'),VALUE)
    assert d['label_boxes']==[] and not d['verified']


def test_single_familiar_label_is_insufficient_navigation_evidence():
    f=frame();f['words']=f['words'][2:]
    assert visual.presentation(f,selector(),VALUE)[1]['label_boxes']==[]


def test_navigation_evidence_has_canonical_order_across_worker_processes():
    _,d=visual.presentation(frame(),selector(),VALUE)
    assert [b[0] for _,b in d['navigation_row']]==[50,110,350]


@pytest.mark.parametrize('y',[0,25,190,220])
def test_document_or_title_text_does_not_prove_navigation(y):
    c,d=visual.presentation(frame(word('显示比例',100,y),word('冻结窗格',200,y)),selector(),VALUE)
    assert c['value']=='' and not d['verified']


def test_independent_visible_command_labels_complete():
    o=observation(frame(word('显示比例'),word('冻结窗格',200)))
    c=o['controls'][0]
    assert c['value']==VALUE and o['visual']['verified']
    assert not visual.allowed(o,c,visual.ACTION,VALUE)


def test_duplicate_result_or_target_never_proves_or_clicks_first_match():
    f=frame(word('显示比例'),word('显示比例',300),word('冻结窗格',200))
    assert not observation(f)['visual']['verified']
    o=observation(frame(word('视图',500,50)))
    assert not visual.allowed(o,o['controls'][0],visual.ACTION,VALUE)


@pytest.mark.parametrize('change',[{'blocked_reason':'popup'},{'dialogs':[{'hwnd':42}]},
    {'truncated':True},{'application':'unknown'}])
def test_popup_and_unsupported_windows_stop_before_click(change):
    o=observation();o.update(change)
    assert not visual.allowed(o,o['controls'][0],visual.ACTION,VALUE)


def test_current_unique_navigation_can_offer_one_action():
    o=observation()
    goal={'selector':selector(),'state':'value','value':VALUE}
    assert visual.candidates(o,goal)==[{'control_id':o['controls'][0]['id'],
                                     'action':visual.ACTION,'value':VALUE}]
    o['visual']['label_boxes']=[[0,0,10,10]]
    assert visual.candidates(o,goal)==[]


@pytest.mark.parametrize('bad',[float('nan'),-1,5000])
def test_invalid_ocr_geometry_denied(bad):
    f=frame();f['words'][0]['box'][0]=bad
    with pytest.raises(ComputerError,match='frame_invalid'):
        visual.validate_frame(f)


def test_observation_retries_only_pixels_never_a_click(monkeypatch):
    calls=[]
    def capture(window,check):
        calls.append(True)
        return (b'first' if len(calls)==1 else b'new'),window,'wps',[0,0,900,800]
    monkeypatch.setattr(visual,'capture',capture)
    monkeypatch.setattr(visual,'recognize',lambda *_:frame(word('显示比例'),word('冻结窗格',200)))
    o,_=visual.observe({'hwnd':1},selector(),VALUE,None,lambda:None)
    assert len(calls)==4 and o['visual']['verified']


def test_continuously_changing_frame_is_not_success(monkeypatch):
    calls=[]
    def capture(window,check):
        calls.append(1)
        return str(len(calls)).encode(),window,'wps',[0,0,900,800]
    monkeypatch.setattr(visual,'capture',capture)
    monkeypatch.setattr(visual,'recognize',lambda *_:frame(word('显示比例'),word('冻结窗格',200)))
    with pytest.raises(ComputerError,match='screen_changed_reobserve'):
        visual.observe({},selector(),VALUE,None,lambda:None)
    assert len(calls)==6


def test_changed_frame_before_input_has_no_intent(monkeypatch,tmp_path):
    o=observation()
    o['visual']['rect']=[0,0,900,800]
    monkeypatch.setattr(visual,'observe',lambda *_:(o,{}))
    monkeypatch.setattr(visual,'capture',lambda *_:(b'changed',{},'wps',[0,0,900,800]))
    from agent_service.computer import digest
    with pytest.raises(ComputerError,match='screen_changed_reobserve'):
        visual.perform({'window':{},'selector':selector(),'value':VALUE,
            'control':o['controls'][0],'before_sha':digest(o)},tmp_path,lambda:None,[None])
    assert not (tmp_path/'native-intent.json').exists()


def test_cache_is_bound_to_actual_bitmap(monkeypatch,tmp_path):
    monkeypatch.setattr(access,'BASE',tmp_path)
    monkeypatch.setattr(visual,'ocr_runtime',lambda:('test-v1',[]))
    cache=tmp_path/'ocr-cache'/'test-v1';cache.mkdir(parents=True)
    raw=b'bitmap';key=sha256(raw).hexdigest()
    f=frame();f['sha256']=key;f['recipe']='test-v1'
    (cache/(key+'.bmp')).write_bytes(raw)
    (cache/(key+'.json')).write_text(json.dumps(f))
    monkeypatch.setattr(visual.subprocess,'Popen',lambda *a,**k:pytest.fail('cache relaunched OCR'))
    assert visual.recognize(raw,None,lambda:None)==f
    (cache/(key+'.bmp')).write_bytes(b'corrupt')
    with pytest.raises(ComputerError,match='cache_changed'):
        visual.recognize(raw,None,lambda:None)


def test_unchecked_condition_is_binary_and_recoverable():
    window={'hwnd':100,'pid':200,'created':'start','image':'C:/Windows/explorer.exe',
            'class_name':'CabinetWClass','title':'Files','profile':'explorer'}
    g={'schema':1,'instruction':'Set checkbox state','max_steps':2,'conditions':[
        {'id':'c1','window':window,'selector':{'type':'CheckBox','name':'Preview','automation_id':'cb'},
         'state':'checked','value':True,'retain':True}]}
    g['conditions'][0].update(state='checked',value=False)
    assert task.validate_goal(g)['conditions'][0]['value'] is False
    g['conditions'][0]['value']=2
    with pytest.raises(ComputerError):
        task.validate_goal(g)
