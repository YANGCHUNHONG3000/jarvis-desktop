"""Offline Explorer contracts; no shell window, native input or network use."""
from copy import deepcopy
from types import SimpleNamespace
import ntpath
import sys
import threading

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_explorer_native as native

SHELL_READ=native._shell_read
SEND_ENTER=native._send_enter

WINDOW={'hwnd':100,'pid':200,'created':'offline','image':'C:\\Windows\\explorer.exe',
        'class_name':'CabinetWClass','title':'child','profile':'explorer'}
SOURCE='C:\\scope\\child';PARENT='C:\\scope';TARGET='C:\\scope\\target'


def folder(path,inode):return {'path':path,'device':1,'inode':inode}


def source():
    parent={'type':'Group','name':'','automation_id':'PART_AutoSuggestBox','runtime_id':[1,2]}
    address={'id':'address','runtime_id':[1,3],'type':'Edit','name':'地址栏','automation_id':'TextBox',
             'value':SOURCE,'enabled':True,'visible':True,'focused':False,'patterns':['focus','set_value'],
             'ancestors':[{k:parent[k] for k in ('type','name','automation_id')}],
             'bounds':[100,50,500,80],'direct_parent':parent}
    up={'id':'up','runtime_id':[1,4],'type':'Button','name':'上移到“scope”(Alt + 向上键)',
        'automation_id':'upButton','value':None,'enabled':True,'visible':True,'focused':False,
        'patterns':['invoke'],'ancestors':[{'type':'ToolBar','name':'','automation_id':'NavigationCommands'}],
        'bounds':[20,50,50,80],'direct_parent':{'type':'ToolBar','name':'','automation_id':'NavigationCommands','runtime_id':[1,5]}}
    tab={'id':'tab','runtime_id':[1,6],'type':'TabItem','name':'child','automation_id':'',
         'selected':True,'ancestors':[{'type':'List','name':'','automation_id':'TabListView'}]}
    return {'window':deepcopy(WINDOW),'application':'explorer','kind':'explorer',
            'binding':{'path':SOURCE,'directory':folder(SOURCE,2),'parent':folder(PARENT,1),
                       'shell_hwnd':100,'shell_tab_hwnd':101,'tab':tab},
            'controls':[{'role':'directory','label':'地址栏','control':address},{'role':'up','label':up['name'],'control':up}]}


@pytest.fixture
def environment(monkeypatch):
    initial=source();state={'current':deepcopy(initial),'sets':[],'invokes':0,'enters':0}
    records={ntpath.normcase(SOURCE):folder(SOURCE,2),ntpath.normcase(PARENT):folder(PARENT,1),
             ntpath.normcase(TARGET):folder(TARGET,3)}
    def directory(value):
        key=ntpath.normcase(native._lexical(value))
        if key not in records:raise ComputerError('windows_explorer_directory_invalid')
        return deepcopy(records[key])
    monkeypatch.setattr(native,'_directory',directory)
    monkeypatch.setattr(native,'snapshot',lambda window,check:deepcopy(state['current']))
    monkeypatch.setattr(native,'_guard',lambda window,check,**kwargs:(check(),deepcopy(WINDOW))[1])
    class Value:
        CurrentIsReadOnly=False
        @property
        def CurrentValue(self):return state['current']['controls'][0]['control']['value']
        def SetValue(self,value):
            state['sets'].append(value);state['current']['controls'][0]['control']['value']=value
    class Element:
        CurrentHasKeyboardFocus=False
        def SetFocus(self):self.CurrentHasKeyboardFocus=True
    element=Element()
    def invoke():state['invokes']+=1
    wrappers={'address':SimpleNamespace(iface_value=Value(),element_info=SimpleNamespace(element=element)),
              'up':SimpleNamespace(iface_invoke=SimpleNamespace(Invoke=invoke))}
    monkeypatch.setattr(native,'_roles',lambda window,check:
        (deepcopy(state['current']['controls']),deepcopy(state['current']['binding']['tab']),wrappers))
    monkeypatch.setattr(native,'_shell_read',lambda window,check:
        {k:deepcopy(v) for k,v in initial['binding'].items() if k!='tab'})
    def enter(window,check,epoch):
        check();state['enters']+=1;epoch[0]=42
    monkeypatch.setattr(native,'_send_enter',enter)
    return initial,state,records,wrappers


@pytest.mark.parametrize('path',[r'\\server\share',r'\\?\C:\scope',r'shell:Downloads',r'http://example.com',
                               r'C:scope',r'%USERPROFILE%\Downloads',r'C:\scope:stream',r'C:\scope\..\target',
                               ' C:\\scope',r'C:\scope & calc.exe',r'C:\scope\bad.',r'C:\scope\bad ',
                               'C:\\scope\nnext',r'C:\scope|calc.exe'])
def test_nonlocal_command_alias_and_traversal_paths_are_rejected(path):
    with pytest.raises(ComputerError,match='directory_invalid'):native._lexical(path)


def test_directory_export_uses_existing_folder_authorization(environment):
    assert native.local_directory(TARGET)==TARGET
    with pytest.raises(ComputerError):native.local_directory(r'C:\missing')


@pytest.mark.parametrize('change',['search','parent','button','duplicate','tab','binding','bounds'])
def test_static_validator_rejects_unproven_address_or_up_roles(change):
    snap=source()
    if change=='search':snap['controls'][0]['control']['ancestors'].append({'automation_id':'FileExplorerSearchBox'})
    elif change=='parent':snap['controls'][0]['control']['direct_parent']['automation_id']='other'
    elif change=='button':snap['controls'][1]['control']['automation_id']='deleteButton'
    elif change=='duplicate':snap['controls'].append(deepcopy(snap['controls'][0]))
    elif change=='tab':snap['binding']['tab']['selected']=False
    elif change=='binding':snap['binding']['shell_hwnd']=999
    elif change=='bounds':snap['controls'][0]['control']['bounds'][2]=0
    with pytest.raises(ComputerError):native.validate(snap)


def test_static_validation_does_not_read_live_files_or_shell(monkeypatch):
    monkeypatch.setattr(native,'_directory',lambda *args:pytest.fail('static validation touched disk'))
    monkeypatch.setattr(native,'_shell_read',lambda *args:pytest.fail('static validation touched shell'))
    assert native.validate(source())['kind']=='explorer'


def test_directory_goal_verified_by_native_folder_not_address_text(environment):
    initial,state,_,_=environment
    state['current']['controls'][0]['control']['value']=TARGET
    assert not native.observe(WINDOW,initial,'directory',TARGET,lambda:None)['verified']
    state['current']['binding'].update(path=TARGET,directory=folder(TARGET,3))
    assert native.observe(WINDOW,initial,'directory',TARGET,lambda:None)['verified']
    assert state['sets']==[] and state['invokes']==0


def test_up_has_fixed_source_parent_and_does_not_follow_changed_source(environment):
    initial,state,_,_=environment
    state['current']['binding'].update(path=PARENT,directory=folder(PARENT,1))
    assert native.observe(WINDOW,initial,'up','',lambda:None)['verified']
    with pytest.raises(ComputerError,match='source_changed'):native.act(WINDOW,initial,'up','',lambda:None,[None])
    state['current']['binding'].update(path=TARGET,directory=folder(TARGET,3))
    with pytest.raises(ComputerError,match='source_changed'):native.observe(WINDOW,initial,'up','',lambda:None)


def test_directory_sets_exact_value_and_sends_one_enter_without_com_navigation(environment):
    initial,state,_,_=environment;epoch=[4]
    native.act(WINDOW,initial,'directory',TARGET,lambda:None,epoch)
    assert state['sets']==[TARGET] and state['enters']==1 and state['invokes']==0 and epoch==[42]


def test_up_invokes_only_bound_up_button_once(environment):
    initial,state,_,_=environment
    native.act(WINDOW,initial,'up','',lambda:None,[None])
    assert state['invokes']==1 and state['sets']==[] and state['enters']==0


@pytest.mark.parametrize('change',['folder','inode','control','value','tab','popup'])
def test_act_rejects_stale_source_before_any_write_or_invoke(environment,monkeypatch,change):
    initial,state,records,_=environment
    if change=='folder':state['current']['binding']['directory']=folder(TARGET,3)
    elif change=='inode':records[ntpath.normcase(SOURCE)]['inode']=99
    elif change=='control':state['current']['controls'][0]['control']['runtime_id']=[99]
    elif change=='value':state['current']['controls'][0]['control']['value']='changed'
    elif change=='tab':state['current']['binding']['tab']['runtime_id']=[99]
    elif change=='popup':monkeypatch.setattr(native,'snapshot',lambda *args:
        (_ for _ in ()).throw(ComputerError('windows_explorer_popup_requires_attention')))
    with pytest.raises(ComputerError):native.act(WINDOW,initial,'directory',TARGET,lambda:None,[4])
    assert state['sets']==[] and state['enters']==0 and state['invokes']==0


@pytest.mark.parametrize('change',['readonly','focus','readback'])
def test_address_provider_failures_never_send_enter(environment,change):
    initial,state,_,wrappers=environment
    if change=='readonly':wrappers['address'].iface_value.CurrentIsReadOnly=True
    elif change=='focus':wrappers['address'].element_info.element.SetFocus=lambda:None
    else:wrappers['address'].iface_value.SetValue=lambda value:state['sets'].append(value)
    with pytest.raises(ComputerError):native.act(WINDOW,initial,'directory',TARGET,lambda:None,[4])
    assert state['enters']==0 and state['invokes']==0


def test_fresh_up_control_drift_after_snapshot_is_rejected_before_invoke(environment,monkeypatch):
    initial,state,_,wrappers=environment
    def roles(*args):
        changed=deepcopy(state['current']['controls']);changed[1]['control']['name']='上移到“different”'
        return changed,deepcopy(initial['binding']['tab']),wrappers
    monkeypatch.setattr(native,'_roles',roles)
    with pytest.raises(ComputerError,match='control_changed'):
        native.act(WINDOW,initial,'up','',lambda:None,[4])
    assert state['invokes']==0


def test_shell_binding_requires_unique_hwnd_filesystem_and_one_native_tab(monkeypatch,environment):
    initial,_,_,_=environment
    folder_item=SimpleNamespace(Path=SOURCE,IsFileSystem=True)
    item=SimpleNamespace(HWND=100,Busy=False,Document=SimpleNamespace(Folder=SimpleNamespace(Self=folder_item)))
    collection=SimpleNamespace(Count=1,Item=lambda index:item)
    monkeypatch.setattr(native,'_shell_collection',lambda:collection)
    gui=SimpleNamespace(GetClassName=lambda hwnd:'ShellTabWindowClass',GetAncestor=lambda hwnd,flag:100,
                        IsWindowVisible=lambda hwnd:True,EnumChildWindows=lambda hwnd,fn,unused:fn(101,None))
    monkeypatch.setitem(sys.modules,'win32gui',gui)
    monkeypatch.setitem(sys.modules,'win32process',SimpleNamespace(GetWindowThreadProcessId=lambda hwnd:(0,200)))
    assert SHELL_READ(WINDOW,lambda:None)['directory']==initial['binding']['directory']
    collection.Count=2
    with pytest.raises(ComputerError,match='shell_ambiguous'):SHELL_READ(WINDOW,lambda:None)
    collection.Count=1;folder_item.IsFileSystem=False
    with pytest.raises(ComputerError,match='virtual_folder'):SHELL_READ(WINDOW,lambda:None)


@pytest.mark.parametrize('effect',['ok','partial','takeover','held','timeout'])
def test_only_fixed_enter_is_hook_confirmed_and_never_retried(monkeypatch,effect):
    state={'injections':[],'closed':False,'error':False,'now':0.0}
    class Monitor:
        def __init__(self,*args):self.seen=threading.Event();state['monitor']=self
        def start(self,timeout):pass
        def check(self):
            if state['error']:raise ComputerError('user_takeover')
        def close(self):state['closed']=True
        def diagnostic(self):return {'matched_strokes':2 if self.seen.is_set() else 0}
    user=SimpleNamespace(GetForegroundWindow=lambda:100,
                         GetAsyncKeyState=lambda key:0x8000 if effect=='held' and key==0x11 else 0)
    monkeypatch.setattr(native.inputs,'_api',lambda:(user,None,None))
    monkeypatch.setattr(native.inputs,'_Monitor',Monitor)
    monkeypatch.setattr(native.inputs,'_epoch',lambda user:42)
    def inject(user,chord,marker):
        state['injections'].append(chord)
        assert chord in (((13,False),(13,True)),((13,True),))
        if effect=='partial':return 1
        if effect=='takeover':state['error']=True
        if effect!='timeout':state['monitor'].seen.set()
        return 2
    monkeypatch.setattr(native.inputs,'_inject',inject)
    def clock():state['now']+=.2;return state['now']
    monkeypatch.setattr(native.time,'monotonic',clock)
    epoch=[4]
    if effect=='ok':
        SEND_ENTER(WINDOW,lambda:None,epoch);assert epoch==[42]
    else:
        with pytest.raises(ComputerError):SEND_ENTER(WINDOW,lambda:None,epoch)
        assert epoch==[4]
    expected=0 if effect=='held' else 2 if effect=='partial' else 1
    assert len(state['injections'])==expected and state['closed']


@pytest.fixture
def tooltip(monkeypatch):
    bridge='Microsoft.UI.Content.PopupWindowSiteBridge'
    expected=[(50033,bridge,2,False),(50033,'InputSiteWindowClass',3,False),
              (50032,'Popup',0,True),(50022,'ToolTip',0,False),(50020,'TextBlock',0,False)]
    class Node:
        def __init__(self,role,cls,hwnd,offscreen):
            self.CurrentControlType=role;self.CurrentClassName=cls;self.CurrentNativeWindowHandle=hwnd
            self.CurrentIsOffscreen=offscreen;self.CurrentProcessId=200;self.CurrentAutomationId=''
            self.CurrentIsPassword=False;self.CurrentIsKeyboardFocusable=False
            self.CurrentHasKeyboardFocus=False
        def __getattr__(self,name):
            if name in {'CurrentName','CurrentValue','CurrentHelpText'}:
                pytest.fail('tooltip classifier must not read text')
            raise AttributeError(name)
    nodes=[Node(*item) for item in expected]
    state={'nodes':nodes,'children':{id(n):nodes[i+1] if i<4 else None for i,n in enumerate(nodes)},
           'siblings':{},'owner':100,'root_owner':100,'exstyle':0x08200000,'style':0x94000000,
           'native_pid':200,'child_root':2,'child_member':True,'visible':True,'native_error':False}
    walker=SimpleNamespace(GetFirstChildElement=lambda n:state['children'].get(id(n)),
                           GetNextSiblingElement=lambda n:state['siblings'].get(id(n)))
    def element(hwnd):
        if state['native_error']:raise RuntimeError('provider unavailable')
        return state['nodes'][0]
    monkeypatch.setitem(sys.modules,'pywinauto.uia_defines',SimpleNamespace(
        IUIA=lambda:SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker,ElementFromHandle=element))))
    gui=SimpleNamespace(IsWindow=lambda hwnd:hwnd in (2,3,100),
        IsWindowVisible=lambda hwnd:state['visible'],GetClassName=lambda hwnd:bridge if hwnd==2 else 'InputSiteWindowClass',
        GetWindow=lambda hwnd,flag:state['owner'],GetAncestor=lambda hwnd,flag:
            state['root_owner'] if flag==3 else state['child_root'],
        GetWindowLong=lambda hwnd,index:state['exstyle'] if index==-20 else state['style'],
        IsChild=lambda root,child:state['child_member'] and (root,child)==(2,3),
        IsWindowEnabled=lambda hwnd:True,GetForegroundWindow=lambda:100,
        EnumWindows=lambda fn,arg:fn(2,None))
    monkeypatch.setitem(sys.modules,'win32gui',gui)
    monkeypatch.setitem(sys.modules,'win32process',SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd:(0,state['native_pid'])))
    return state


def test_exact_informational_tooltip_is_classified_without_reading_text(tooltip):
    assert native.is_tooltip(2,WINDOW,lambda:None) is True


def test_tooltip_style_matches_native_dword_bits_when_win32_returns_signed_long(tooltip):
    tooltip['style']=0x94000000-(1<<32)
    assert native.is_tooltip(2,WINDOW,lambda:None) is True


def test_nonactivating_tooltip_bridge_may_advertise_focusability_without_holding_focus(tooltip):
    tooltip['nodes'][0].CurrentIsKeyboardFocusable=True
    assert native.is_tooltip(2,WINDOW,lambda:None) is True


@pytest.mark.parametrize('depth',range(5))
def test_tooltip_with_actual_keyboard_focus_is_rejected_at_every_depth(tooltip,depth):
    tooltip['nodes'][depth].CurrentHasKeyboardFocus=True
    assert native.is_tooltip(2,WINDOW,lambda:None) is False


@pytest.mark.parametrize('depth',range(1,5))
def test_tooltip_focusable_descendant_is_rejected(tooltip,depth):
    tooltip['nodes'][depth].CurrentIsKeyboardFocusable=True
    assert native.is_tooltip(2,WINDOW,lambda:None) is False


@pytest.mark.parametrize('change',['owner','root','pid','noactivate','style','password','focusable','input_role',
                               'menu_class','automation_id','uia_pid','offscreen','root_hwnd','child_hwnd',
                               'child_root','child_member','truncated','extra','branch','provider'])
def test_tooltip_lookalikes_or_incomplete_trees_fail_closed(tooltip,change):
    s=tooltip;n=s['nodes']
    if change=='owner':s['owner']=999
    elif change=='root':s['root_owner']=999
    elif change=='pid':s['native_pid']=999
    elif change=='noactivate':s['exstyle']&=~0x08000000
    elif change=='style':s['style']|=0x10000
    elif change=='password':n[4].CurrentIsPassword=True
    elif change=='focusable':n[4].CurrentIsKeyboardFocusable=True
    elif change=='input_role':n[4].CurrentControlType=50000
    elif change=='menu_class':n[3].CurrentClassName='Menu'
    elif change=='automation_id':n[3].CurrentAutomationId='interactive'
    elif change=='uia_pid':n[2].CurrentProcessId=999
    elif change=='offscreen':n[2].CurrentIsOffscreen=False
    elif change=='root_hwnd':n[0].CurrentNativeWindowHandle=999
    elif change=='child_hwnd':n[1].CurrentNativeWindowHandle=0
    elif change=='child_root':s['child_root']=999
    elif change=='child_member':s['child_member']=False
    elif change=='truncated':s['children'][id(n[3])]=None
    elif change=='extra':s['children'][id(n[4])]=n[4]
    elif change=='branch':s['siblings'][id(n[1])]=n[4]
    elif change=='provider':s['native_error']=True
    assert native.is_tooltip(2,WINDOW,lambda:None) is False


def test_tooltip_classifier_preserves_caller_cancellation(tooltip):
    def cancel():raise ComputerError('user_takeover')
    with pytest.raises(ComputerError,match='user_takeover'):native.is_tooltip(2,WINDOW,cancel)


def test_explorer_guard_ignores_only_the_complete_tooltip(tooltip,monkeypatch):
    monkeypatch.setattr(native.uia,'_recheck',lambda window:deepcopy(window))
    monkeypatch.setattr(native.access,'_kind',lambda window:'explorer')
    monkeypatch.setattr(native.access,'_owned',lambda hwnd,root:True)
    assert native._guard(WINDOW,lambda:None)==WINDOW
    tooltip['nodes'][3].CurrentControlType=50009
    with pytest.raises(ComputerError,match='popup_requires_attention'):native._guard(WINDOW,lambda:None)
