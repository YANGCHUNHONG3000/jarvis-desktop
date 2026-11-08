from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time

import pytest
from PIL import Image, ImageDraw

from agent_service.computer import ComputerError
from agent_service import windows_vision_context as vision
from agent_service.community_annotations import annotate
from test_windows_stages import observation


class Backend:
    def __init__(self, parent):
        self.parent=parent; self.calls=0
        self.observed=observation()
        self.observed['controls'][0]['bounds']=[30,50,130,80]
        self.rect=[10,20,310,220]
        image=Image.new('RGB',(300,200),'white')
        ImageDraw.Draw(image).rectangle((20,30,120,60),fill='gray')
        stream=BytesIO();image.save(stream,format='BMP')
        self.png=stream.getvalue()
        self.path=parent/'selected-window.bmp';self.path.write_bytes(self.png)
        self.frame={'window':deepcopy(self.observed['window']),'application':'explorer',
                    'rect':self.rect,'path':str(self.path),'sha256':sha256(self.png).hexdigest(),
                    'captured_at':time.time()}
        self.live=deepcopy(self.observed)
    def vision_frame(self,window,*,stop):
        self.calls+=1
        return deepcopy(self.frame)
    def observe(self,window,*,stop):return deepcopy(self.live)


def prepared(backend,**kwargs):
    obs=backend.observed
    selector={k:obs['controls'][0][k] for k in ('type','name','automation_id')}
    return vision.prepare_context(backend,[obs],{'W1C1':(obs,selector,['selected'])},**kwargs)


def test_labeled_window_has_bound_source_and_no_pixels_in_proof(tmp_path):
    b=Backend(tmp_path);images,proof=prepared(b)
    assert len(images)==1 and Image.open(BytesIO(images[0])).size==(300,200)
    p=proof['sources'][0]
    assert p['labels']==[{'id':'W1C1','bounds':[20,30,120,60]}]
    assert p['annotated_sha256']==sha256(images[0]).hexdigest()
    assert images[0]!=b.png and p['sha256']==sha256(b.png).hexdigest()
    assert not any(isinstance(v,bytes) for v in p.values())


@pytest.mark.parametrize('field,value', [('name','changed'),('bounds',[31,50,130,80]),
    ('selected',True),('enabled',False),('visible',False),('id','replaced')])
def test_changed_target_is_not_sent(tmp_path,field,value):
    b=Backend(tmp_path);b.live['controls'][0][field]=value
    with pytest.raises(ComputerError):prepared(b)


@pytest.mark.parametrize('field,value',[('dialogs',[{'title':'unknown'}]),('blocked_reason','blocked'),('truncated',True)])
def test_manual_attention_prevents_capture(tmp_path,field,value):
    b=Backend(tmp_path);b.observed[field]=value
    with pytest.raises(ComputerError,match='manual_attention'):prepared(b)
    assert b.calls==0


@pytest.mark.parametrize('case',['stale','hash','outside','title','dimensions','geometry'])
def test_bad_image_binding_rejected(tmp_path,case):
    b=Backend(tmp_path)
    if case=='stale':b.frame['captured_at']-=60
    if case=='hash':b.frame['sha256']='0'*64
    if case=='outside':b.frame['path']=str(tmp_path.parent/'selected-window.bmp')
    if case=='title':b.frame['window']['title']='different'
    if case=='dimensions':b.frame['rect']=[10,20,311,220]
    if case=='geometry':
        b.observed['controls'][0]['bounds']=[0,50,130,80]
        b.live=deepcopy(b.observed)
    with pytest.raises(ComputerError):prepared(b)


def test_cancelled_before_capture(tmp_path):
    b=Backend(tmp_path)
    with pytest.raises(ComputerError,match='cancelled'):prepared(b,stop=lambda:True)
    assert b.calls==0


def test_annotation_resizes_deterministically():
    image=Image.new('RGB',(2048,1024),'white');s=BytesIO();image.save(s,format='PNG')
    labels=[{'id':'W1C1','bounds':[0,0,100,40]}]
    first=annotate(s.getvalue(),labels)
    assert first==annotate(s.getvalue(),labels)
    assert Image.open(BytesIO(first)).size==(1536,768)


@pytest.mark.parametrize('reason',['password','truncated'])
def test_sensitive_surface_blocks_before_pixels(tmp_path,monkeypatch,reason):
    from agent_service import windows_access as access, windows_visual as visual
    monkeypatch.setattr(visual,'prepare',lambda *args:None)
    def tree(window,check,**kwargs):
        assert kwargs['reject_password'] is True
        if reason=='password':raise ComputerError('windows_vision_password_surface')
        return {'truncated':True},{}
    monkeypatch.setattr(access,'_tree',tree)
    monkeypatch.setattr(visual,'_capture_physical',lambda *args,**kw:pytest.fail('must not capture'))
    with pytest.raises(ComputerError):vision.capture_frame({},tmp_path,lambda:None)
    assert not list(tmp_path.iterdir())
