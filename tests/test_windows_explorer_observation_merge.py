from copy import deepcopy
import pytest
from agent_service.windows_access import WindowsBackend
from agent_service.computer import ComputerError

WINDOW={'hwnd':1,'pid':2,'created':'fixture','image':'C:/Windows/explorer.exe','class_name':'CabinetWClass','title':'Explorer','profile':'explorer'}
TREE={'id':'tree','type':'TreeItem','name':'Documents','enabled':True,'visible':True,'patterns':['select']}

@pytest.mark.parametrize('mode',['complete','truncated','different_window'])
def test_reviewed_explorer_keeps_only_complete_same_window_tree(monkeypatch,mode):
    backend=WindowsBackend()
    observed={'window':deepcopy(WINDOW),'application':'explorer','controls':[TREE],'dialogs':[],
              'scope':'visible_controls','truncated':mode=='truncated'}
    snapshot={'window':deepcopy(WINDOW),'application':'explorer','kind':'explorer_view'}
    if mode=='different_window':snapshot['window']['title']='Changed'
    monkeypatch.setattr(backend,'_call',lambda *a,**kw:{'ok':True,'observation':deepcopy(observed)})
    monkeypatch.setattr(backend,'reviewed_snapshot',lambda *a,**kw:deepcopy(snapshot))
    result=backend.observe(WINDOW)
    assert result['controls']==([TREE] if mode=='complete' else [])
    assert result['reviewed_snapshot']==snapshot

def test_blocked_explorer_does_not_gain_specialised_candidates(monkeypatch):
    backend=WindowsBackend()
    observed={'window':WINDOW,'application':'explorer','controls':[],'dialogs':[],
              'scope':'visible_controls','truncated':False,'blocked_reason':'modal'}
    monkeypatch.setattr(backend,'_call',lambda *a,**kw:{'ok':True,'observation':observed})
    monkeypatch.setattr(backend,'reviewed_snapshot',lambda *a,**kw:pytest.fail('blocked'))
    assert backend.observe(WINDOW)==observed

def test_directory_change_uses_fresh_same_native_window_title(monkeypatch):
    backend=WindowsBackend();current={**WINDOW,'title':'本地磁盘 (C:) - 文件资源管理器'}
    observed={'window':current,'application':'explorer','controls':[TREE],'dialogs':[],
              'scope':'visible_controls','truncated':False}
    seen=[]
    monkeypatch.setattr(backend,'_call',lambda *a,**kw:{'ok':True,'observation':deepcopy(observed)})
    def snapshot(window,kind,**kw):
        seen.append(deepcopy(window))
        assert window==current
        return {'window':window,'application':'explorer','kind':kind}
    monkeypatch.setattr(backend,'reviewed_snapshot',snapshot)
    result=backend.observe(WINDOW)
    assert seen==[current] and result['controls']==[TREE]
    assert result['reviewed_snapshot']['window']==current

@pytest.mark.parametrize('field,value',[('hwnd',3),('pid',4),('created','new'),('image','D:/other.exe'),
                                      ('class_name','other'),('profile','observe_only')])
def test_new_title_never_allows_changed_native_identity(monkeypatch,field,value):
    backend=WindowsBackend();current={**WINDOW,'title':'changed',field:value}
    observed={'window':current,'application':'explorer','controls':[],'dialogs':[],
              'scope':'visible_controls','truncated':False}
    monkeypatch.setattr(backend,'_call',lambda *a,**kw:{'ok':True,'observation':observed})
    monkeypatch.setattr(backend,'reviewed_snapshot',lambda *a,**kw:pytest.fail('foreign window'))
    with pytest.raises(ComputerError,match='identity_mismatch'):
        backend.observe(WINDOW)
