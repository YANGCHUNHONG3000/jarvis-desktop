"""Offline contracts; actual WPS evidence is separate from these fixtures."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import struct

import pytest

from agent_service.computer import ComputerError, digest, read_record
from agent_service import windows_visual_forms as forms, windows_visual_memory as memory
from agent_service import windows_task as task, windows_visual_proposal as proposal, windows_intent as intent
from agent_service import windows_visual_form_native as native

WINDOW={'hwnd':100,'pid':200,'created':'offline','image':'C:/wps.exe','class_name':'OpusApp','title':'offline','profile':'observe_only'}
DIALOG={**WINDOW,'hwnd':101,'class_name':'Qt5QWindow','title':'查找'}


@pytest.fixture
def snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(memory,'OBSERVATION_BASE',tmp_path/'observations')
    monkeypatch.setattr(forms,'BASE',tmp_path/'forms')
    from agent_service import windows_visual_binding as binding
    monkeypatch.setattr(binding,'verify',lambda value,**kw:value)
    width,height=560,240; pixels=b'\xff'*width*height*4
    raw=struct.pack('<2sIHHI',b'BM',54+len(pixels),0,0,54)+struct.pack('<IiiHHIIiiII',40,width,-height,1,32,0,len(pixels),0,0,0,0)+pixels
    checksum=sha256(raw).hexdigest();cache=memory.OBSERVATION_BASE/'ocr-cache';cache.mkdir(parents=True)
    (cache/(checksum+'.bmp')).write_bytes(raw)
    def control(identifier,kind,name,bounds,**extra):
        return {'id':identifier,'type':kind,'name':name,'automation_id':'','bounds':bounds,
                'visible':True,'enabled':True,'owner_hwnd':101,'patterns':[],'ancestors':[],'value':'',**extra}
    combo=control('combo','ComboBox','查找内容(N)',[220,180,620,210])
    edit=control('edit','Edit','',[222,182,600,208],patterns=['set_value'],
        ancestors=[{k:combo[k] for k in ('type','name','automation_id')}])
    close=control('close','Button','关闭',[580,300,650,330],patterns=['invoke'])
    snap={'window':deepcopy(WINDOW),'dialog':deepcopy(DIALOG),'document':{'path':'offline'},
          'frame':{'schema':1,'sha256':checksum,'width':width,'height':height,'words':[
              {'text':'查找内容','box':[20,87,70,16]},{'text':'关闭','box':[500,207,28,16]}]},
          'rect':[100,100,660,340],'controls':[combo,edit,close]}
    return attest(snap,tmp_path/'native')


def attest(snapshot,root):
    snap=deepcopy(snapshot);snap.pop('native_proof',None);root.mkdir(parents=True,exist_ok=True)
    request={'op':'visual_form_snapshot','window':snap['window']};outcome={'ok':True,'snapshot':snap}
    for name,data in [('native-request.json',request),('native-outcome.json',outcome)]:
        (root/name).write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return {**snap,'native_proof':{'root':str(root),'request_sha256':sha256((root/'native-request.json').read_bytes()).hexdigest(),
                                 'outcome_sha256':sha256((root/'native-outcome.json').read_bytes()).hexdigest()}}


def candidates(snapshot,tmp_path):return forms.candidates_for(snapshot,parent=tmp_path/'forms')


def observed(snapshot):
    return {'window':deepcopy(WINDOW),'application':'wps','controls':[],'dialogs':[{**DIALOG,'dialog_kind':'reviewed_find'}],
            'truncated':False,'scope':'visible_controls','form_snapshot':deepcopy(snapshot)}


class Client:
    def __init__(self):self.calls=0
    def complete(self,system,context,*,max_tokens):
        self.calls+=1; data=json.loads(context);rows=data['windows'][0]['candidates']
        assert 'VisualSearchInput' in context and 'VisualCloseButton' in context
        assert 'source' not in context and 'offline' not in context and 'native_proof' not in context
        return {'conditions':[{'id':c['id'],'state':'fill' if c['type']=='VisualSearchInput' else 'dismiss',
                              'value':'纸张' if c['type']=='VisualSearchInput' else True,'retain':False} for c in rows]}


def planned(snapshot,tmp_path):
    options=candidates(snapshot,tmp_path);client=Client()
    goal=intent.propose('设置查找词纸张然后关闭，不执行查找',[observed(snapshot)],client,visual_options=options)
    ref=proposal.save(goal,options,[observed(snapshot)],parent=tmp_path/'proposals')
    return task.create(tmp_path/'tasks',goal,visual_proposal=ref),goal


def test_model_only_selects_observed_roles_and_requested_value(snapshot,tmp_path):
    root,goal=planned(snapshot,tmp_path)
    assert task.load(root)['summary']['approval_required']
    assert [forms.decode(c['selector'],c['value'])[0]['kind'] for c in goal['conditions']]==['search_text','close_find']
    assert '纸张' in forms.review_text(goal['conditions'][0]['selector'],goal['conditions'][0]['value'])
    assert '原查找窗口' in forms.review_text(goal['conditions'][1]['selector'],goal['conditions'][1]['value'])


@pytest.mark.parametrize('change', ['duplicate_field','duplicate_close','title_close','readonly','password','offscreen','bad_bounds','no_ocr','foreign','wrong_role'])
def test_unproven_roles_and_geometry_are_not_candidates(snapshot,change):
    snap=deepcopy(snapshot)
    if change=='duplicate_field':snap['controls'].append(deepcopy(snap['controls'][0]))
    elif change=='duplicate_close':snap['controls'].append(deepcopy(snap['controls'][2]))
    elif change=='title_close':snap['controls'][2]['ancestors']=[{'type':'TitleBar'}]
    elif change=='readonly':snap['controls'][1]['patterns']=[]
    elif change=='password':snap['controls'][1]['password']=True
    elif change=='offscreen':snap['controls'][1]['visible']=False
    elif change=='bad_bounds':snap['controls'][2]['bounds'][0]=99
    elif change=='no_ocr':snap['frame']['words']=[]
    elif change=='foreign':snap['controls'][1]['owner_hwnd']=999
    elif change=='wrong_role':snap['controls'][2]['name']='删除'
    with pytest.raises(ComputerError):forms.controls(snap)


@pytest.mark.parametrize('text',[None,True,12,['纸张'],'x'*301,'a\nb'])
def test_input_is_plain_bounded_string(snapshot,tmp_path,text):
    item=candidates(snapshot,tmp_path)[0];item['expected']=[text]
    with pytest.raises(ComputerError):forms.condition(item)


def test_native_source_tampering_stops_approved_task(snapshot,tmp_path):
    root,_=planned(snapshot,tmp_path);proposal.approve(root)
    Path(snapshot['native_proof']['root'],'native-outcome.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ComputerError,match='source_changed'):task.load(root)


def test_form_cannot_bypass_review_with_schema1(snapshot,tmp_path):
    _,goal=planned(snapshot,tmp_path)
    with pytest.raises(ComputerError,match='approval_required'):task.create(tmp_path/'unreviewed',goal)


def fake_native(monkeypatch,snapshot):
    state={'text':'','closed':False,'popup':False}
    def observe(window,source,kind,text,check):
        if state['popup']:raise ComputerError('windows_visual_form_popup_requires_attention')
        snap=deepcopy(snapshot);snap.pop('native_proof')
        snap['controls'][1]['value']=state['text']
        return {'snapshot':None if state['closed'] else snap,'document':source['document'],
                'dialog_open':not state['closed'],'dialog_presence':'destroyed' if state['closed'] else 'visible','field_value':state['text']}
    monkeypatch.setattr(native,'observe',observe)
    return state


def test_approval_pause_popup_and_restart_only_remaining_button(snapshot,tmp_path,monkeypatch):
    root,goal=planned(snapshot,tmp_path);state=fake_native(monkeypatch,snapshot)
    class Backend:
        def __init__(self):self.acts=[];self.reads=0
        def observe_condition(self,c,*,stop):
            self.reads+=1;return forms.observation(c['window'],c['selector'],c['value'],lambda:None)
        candidates=staticmethod(forms.actions)
        def act(self,window,control,action,value,before,operation,*,stop):
            data=json.loads(value);self.acts.append(data['kind'])
            if data['kind']=='search_text':state['text']=data['text']
            else:state['closed']=True
            c=next(c for c in goal['conditions'] if c['value']==value)
            return {'ok':True,'state':'verified','effects':'verified','observation':self.observe_condition(c,stop=stop)}
    b=Backend()
    with pytest.raises(ComputerError,match='approval_required'):task.run(root,b)
    assert b.reads==0
    proposal.approve(root);first=task.run(root,b,pause_after_step=True)
    assert first['completed']==['target_1'] and b.acts==['search_text']
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    state['popup']=True;blocked=task.run(root,b)
    assert blocked['completed']==['target_1'] and b.acts==['search_text']
    state['popup']=False;assert task.run(root,b)['ok'] and b.acts==['search_text','close_find']
    assert all(Path(p).read_bytes()==raw for p,raw in before.items())


def test_hidden_dialog_is_not_success_or_replay(snapshot,tmp_path,monkeypatch):
    item=candidates(snapshot,tmp_path)[1];c=forms.condition(item)
    monkeypatch.setattr(native,'observe',lambda *a:{'dialog_presence':'hidden','snapshot':None})
    with pytest.raises(ComputerError,match='close_unproven'):forms.observation(WINDOW,c['selector'],c['value'],lambda:None)


def test_changed_field_runtime_identity_cannot_reuse_label(snapshot,tmp_path,monkeypatch):
    item=candidates(snapshot,tmp_path)[1];c=forms.condition(item);state=fake_native(monkeypatch,snapshot)
    original=native.observe
    def swapped(*a):
        value=original(*a);value['snapshot']['controls'][1]['id']='replacement';return value
    monkeypatch.setattr(native,'observe',swapped)
    with pytest.raises(ComputerError,match='control_changed'):forms.observation(WINDOW,c['selector'],c['value'],lambda:None)


def test_full_proposal_routes_forms_to_review_without_navigation_capture(snapshot,tmp_path,monkeypatch):
    original=forms.candidates_for
    monkeypatch.setattr(forms,'candidates_for',lambda snap:original(snap,parent=tmp_path/'form-propose'))
    class B:
        def observe(self,window,*,stop):return observed(snapshot)
        def visual_snapshot(self,*a,**kw):pytest.fail('known form must not capture parent navigation')
        def visual_document_identity(self,*a,**kw):pytest.fail('form already binds native document')
    client=Client()
    result=proposal.propose('设置查找词纸张然后关闭，不执行查找',[observed(snapshot)],B(),client,
        task_base=tmp_path/'tasks',proposal_base=tmp_path/'proposals')
    assert result['visual'] and task.load(result['root'])['summary']['approval_required']
    assert client.calls==1 and list((result['root']/'attempts').iterdir())==[]


def test_dismiss_cannot_be_replaced_by_arbitrary_invoke(snapshot,tmp_path):
    options=candidates(snapshot,tmp_path);_,targets=intent._context('关闭查找窗口',[observed(snapshot)],options)
    key=next(k for k,t in targets.items() if t[2]==['dismiss'])
    for value in ('execute',False,0,['close']):
        with pytest.raises(ComputerError):
            intent._goal('关闭查找窗口',{'conditions':[{'id':key,'state':'dismiss','value':value,'retain':False}]},targets)


def changed_pixels(snapshot):
    """A second intact bitmap with identical OCR/native semantics, no masking."""
    current=deepcopy(snapshot)
    current.pop('native_proof',None)
    cache=memory.OBSERVATION_BASE/'ocr-cache'
    raw=bytearray((cache/(snapshot['frame']['sha256']+'.bmp')).read_bytes())
    raw[-4]^=1
    checksum=sha256(raw).hexdigest()
    (cache/(checksum+'.bmp')).write_bytes(raw)
    current['frame']['sha256']=checksum
    return current


def native_state(snapshot):
    return {'snapshot':snapshot,'document':snapshot['document'],'dialog_open':True,
            'dialog_presence':'visible','field_value':snapshot['controls'][1]['value']}


def test_input_caret_pixels_do_not_change_semantic_digest_and_raw_receipts_remain_exact(snapshot,tmp_path,monkeypatch):
    _,goal=planned(snapshot,tmp_path);c=goal['conditions'][0]
    live=deepcopy(snapshot);live.pop('native_proof')
    second=changed_pixels(snapshot)
    second['frame']['words'][0]['box'][0]+=.5
    states=iter([native_state(live),native_state(second)])
    monkeypatch.setattr(native,'observe',lambda *args:next(states))
    roots=[tmp_path/'first',tmp_path/'second']
    for root in roots:root.mkdir()
    observations=[forms.observation(WINDOW,c['selector'],c['value'],lambda:None,evidence_root=root) for root in roots]
    assert observations[0]==observations[1]
    semantic=observations[0]['form']['readback']['semantic_snapshot']
    assert semantic['controls']==live['controls'] and semantic['dialog']==DIALOG
    assert semantic['ocr']['candidates']['search_text']['label']=='查找内容(N)'
    assert 'snapshot' not in observations[0]['form']['readback']
    receipts=[read_record(root/'form-observe.json') for root in roots]
    assert receipts[0]['readback']['snapshot']['frame']['sha256'] != receipts[1]['readback']['snapshot']['frame']['sha256']
    assert all(r['observation_sha256']==digest(observations[0]) for r in receipts)


@pytest.mark.parametrize('change',['value','bounds','owner','dialog','document','pattern','label_geometry'])
def test_input_semantic_drift_is_never_equivalent(snapshot,tmp_path,monkeypatch,change):
    _,goal=planned(snapshot,tmp_path);c=goal['conditions'][0]
    before=deepcopy(snapshot);before.pop('native_proof')
    after=deepcopy(before)
    if change=='value':after['controls'][1]['value']='different'
    elif change=='bounds':after['controls'][1]['bounds'][0]+=1
    elif change=='owner':after['controls'][1]['owner_hwnd']=999
    elif change=='dialog':after['dialog']['pid']+=1
    elif change=='document':after['document']['path']='other'
    elif change=='pattern':after['controls'][1]['patterns']=[]
    elif change=='label_geometry':after['frame']['words'][0]['box'][0]+=150
    states=iter([native_state(before),native_state(after)])
    monkeypatch.setattr(native,'observe',lambda *args:next(states))
    first=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    try:
        second=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    except ComputerError:
        return
    assert digest(first)!=digest(second)


def test_close_still_compares_full_raw_frame(snapshot,tmp_path,monkeypatch):
    _,goal=planned(snapshot,tmp_path);c=goal['conditions'][1]
    first=deepcopy(snapshot);first.pop('native_proof')
    states=iter([native_state(first),native_state(changed_pixels(snapshot))])
    monkeypatch.setattr(native,'observe',lambda *args:next(states))
    a=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    b=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    assert digest(a)!=digest(b) and 'snapshot' in a['form']['readback']


def test_input_perform_accepts_only_semantically_equal_refresh_and_archives_raw_before_after(snapshot,tmp_path,monkeypatch):
    _,goal=planned(snapshot,tmp_path);c=goal['conditions'][0]
    first=deepcopy(snapshot);first.pop('native_proof')
    refresh=changed_pixels(snapshot)
    after=deepcopy(refresh);after['controls'][1]['value']='纸张'
    states=iter([native_state(first),native_state(refresh),native_state(after)])
    monkeypatch.setattr(native,'observe',lambda *args:next(states))
    observed=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    calls=[]
    def set_value(snap,identifier,text,check):
        assert snap['frame']['sha256']==refresh['frame']['sha256']
        calls.append((identifier,text))
    monkeypatch.setattr(native,'set_field',set_value)
    operation=tmp_path/'operation';operation.mkdir()
    request={'window':WINDOW,'selector':c['selector'],'value':c['value'],
             'control':observed['controls'][0],'before_sha':digest(observed)}
    result=forms.perform(request,operation,lambda:None,[None])
    assert result['ok'] and calls==[('edit','纸张')]
    before_raw=read_record(operation/'form-before.json')['readback']['snapshot']
    after_raw=read_record(operation/'form-after.json')['readback']['snapshot']
    assert before_raw==refresh and after_raw==after
    original=(operation/'form-before.json').read_bytes()
    monkeypatch.setattr(native,'observe',lambda *args:native_state(after))
    with pytest.raises(FileExistsError):
        forms._observation(WINDOW,c['selector'],c['value'],lambda:None,evidence_root=operation,receipt='form-before.json')
    assert (operation/'form-before.json').read_bytes()==original


def close_request(snapshot,tmp_path,monkeypatch):
    _,goal=planned(snapshot,tmp_path);c=goal['conditions'][1]
    state=fake_native(monkeypatch,snapshot)
    before=forms.observation(WINDOW,c['selector'],c['value'],lambda:None)
    state.update(clicks=0,after_reads=0)
    def click(*args,**kwargs):
        state['clicks']+=1;state['closed']=True
    monkeypatch.setattr(native,'click_close',click)
    monkeypatch.setattr(native,'_guard',lambda *args,**kwargs:(WINDOW,None,None))
    monkeypatch.setattr(forms.time,'sleep',lambda delay:None)
    operation=tmp_path/'operation';operation.mkdir()
    request={'window':WINDOW,'selector':c['selector'],'value':c['value'],
             'control':before['controls'][0],'before_sha':digest(before)}
    return state,operation,request


@pytest.mark.parametrize('code',['windows_visual_document_changed','windows_visual_form_dialog_changed',
                               'windows_visual_document_identity_unavailable'])
def test_close_postcondition_retries_only_reads_and_keeps_each_failure(snapshot,tmp_path,monkeypatch,code):
    state,operation,request=close_request(snapshot,tmp_path,monkeypatch)
    observe=native.observe
    def delayed(*args):
        if state['closed']:
            state['after_reads']+=1
            if state['after_reads']<=2:raise ComputerError(code)
        return observe(*args)
    monkeypatch.setattr(native,'observe',delayed)
    assert forms.perform(request,operation,lambda:None,[None])['ok']
    assert state['clicks']==1 and state['after_reads']==3
    assert not (operation/'form-after.json').exists()
    assert (operation/'form-after-3.json').exists()
    failures=[read_record(operation/f'form-after-retry-{n}.json') for n in (1,2)]
    assert all(f['code']==code and f['action']=='read_only_postcondition' for f in failures)
    assert not (operation/'form-after-retry-3.json').exists()


def test_close_postcondition_remains_unknown_after_three_failed_reads(snapshot,tmp_path,monkeypatch):
    state,operation,request=close_request(snapshot,tmp_path,monkeypatch)
    observe=native.observe
    def unavailable(*args):
        if state['closed']:
            state['after_reads']+=1
            raise ComputerError('windows_visual_document_identity_unavailable')
        return observe(*args)
    monkeypatch.setattr(native,'observe',unavailable)
    with pytest.raises(ComputerError,match='identity_unavailable'):
        forms.perform(request,operation,lambda:None,[None])
    assert state['clicks']==1 and state['after_reads']==3
    assert len(list(operation.glob('form-after-retry-*.json')))==3
    assert not list(operation.glob('form-after-[123].json'))


@pytest.mark.parametrize('code',['user_takeover','windows_cancelled_or_timeout',
                               'windows_visual_form_popup_requires_attention','windows_form_source_changed'])
def test_close_postcondition_does_not_retry_takeover_popup_or_other_errors(snapshot,tmp_path,monkeypatch,code):
    state,operation,request=close_request(snapshot,tmp_path,monkeypatch)
    observe=native.observe
    def blocked(*args):
        if state['closed']:
            state['after_reads']+=1;raise ComputerError(code)
        return observe(*args)
    monkeypatch.setattr(native,'observe',blocked)
    with pytest.raises(ComputerError,match=code):forms.perform(request,operation,lambda:None,[None])
    assert state['clicks']==1 and state['after_reads']==1
    assert not list(operation.glob('form-after-retry-*.json'))


def test_true_source_document_change_stops_before_postcondition_retry(snapshot,tmp_path,monkeypatch):
    from agent_service import windows_visual_binding as binding
    state,operation,request=close_request(snapshot,tmp_path,monkeypatch)
    def verify(value,**kwargs):
        if state['closed']:raise ComputerError('windows_visual_document_changed')
        return value
    monkeypatch.setattr(binding,'verify',verify)
    with pytest.raises(ComputerError,match='document_changed'):
        forms.perform(request,operation,lambda:None,[None])
    assert state['clicks']==1 and len(list(operation.glob('form-after-retry-*.json')))==1


def test_replacement_find_gate_failure_is_not_caught_as_transient(snapshot,tmp_path,monkeypatch):
    state,operation,request=close_request(snapshot,tmp_path,monkeypatch)
    observe=native.observe
    def unavailable(*args):
        if state['closed']:
            state['after_reads']+=1;raise ComputerError('windows_visual_form_dialog_changed')
        return observe(*args)
    def guard(*args,**kwargs):raise ComputerError('windows_visual_form_dialog_changed')
    monkeypatch.setattr(native,'observe',unavailable);monkeypatch.setattr(native,'_guard',guard)
    with pytest.raises(ComputerError,match='dialog_changed'):
        forms.perform(request,operation,lambda:None,[None])
    assert state['clicks']==1 and state['after_reads']==1
    assert len(list(operation.glob('form-after-retry-*.json')))==1
