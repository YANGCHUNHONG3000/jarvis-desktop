"""Reviewed Explorer address/Up semantics; never arbitrary shell commands.

Shell automation is read-only. Navigation is performed only through the exact
native address editor or Up button, under the existing worker/input guards.
"""
from copy import deepcopy
import ntpath
from pathlib import Path
import re
import secrets
import stat
import time

from .computer import ComputerError, same_identity, safe_root
from . import computer_uia as uia, windows_access as access, windows_input as inputs

_IDENTITY=('id','runtime_id','type','name','automation_id','ancestors','bounds','direct_parent')
_ADDRESS_NAMES={'地址栏','Address Bar','Address bar'}


def _lexical(value):
    if (type(value) is not str or not 3<=len(value)<=3000 or value!=value.strip()
            or re.search(r'[\x00-\x1f%$`<>"\'|?*;&]',value)
            or value.startswith(('\\\\','//'))):
        raise ComputerError('windows_explorer_directory_invalid')
    text=value.replace('/','\\')
    if not re.match(r'^[A-Za-z]:\\',text) or ':' in text[2:]:
        raise ComputerError('windows_explorer_directory_invalid')
    parts=text[3:].split('\\')
    if any(p in {'.','..'} or p.endswith((' ','.')) for p in parts if p):
        raise ComputerError('windows_explorer_directory_invalid')
    return ntpath.normpath(text)


def _same_path(left,right):
    return ntpath.normcase(_lexical(left))==ntpath.normcase(_lexical(right))


def _directory(value):
    path=Path(_lexical(value))
    try:
        for item in (path,*path.parents):
            info=item.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
                    or not stat.S_ISDIR(info.st_mode)):
                raise ComputerError('windows_explorer_directory_invalid')
        path=safe_root(path);info=path.stat()
        if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
            raise ComputerError('windows_explorer_directory_invalid')
    except (OSError,ValueError) as exc:
        raise ComputerError('windows_explorer_directory_invalid') from exc
    return {'path':str(path),'device':info.st_dev,'inode':info.st_ino}


def local_directory(value):
    """Authorize only a real, non-reparse, absolute local filesystem folder."""
    return _directory(value)['path']


def is_tooltip(hwnd,parent,check):
    """Only the evidenced five-node, nonactivating Explorer ToolTip chain.

    Reads structural UIA/Win32 metadata only. Name, Value, HelpText and document
    content are never read. Any extra node, branch, input role or unknown shape
    fails closed; cancellation/takeover from the caller still propagates.
    """
    import win32gui,win32process
    from pywinauto.uia_defines import IUIA
    bridge='Microsoft.UI.Content.PopupWindowSiteBridge'
    def native_identity():
        return (win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
                and win32gui.GetClassName(hwnd)==bridge
                and win32process.GetWindowThreadProcessId(hwnd)[1]==parent['pid']
                and win32gui.GetWindow(hwnd,4)==parent['hwnd']
                and win32gui.GetAncestor(hwnd,3)==parent['hwnd']
                and win32gui.GetWindowLong(hwnd,-20)&0xffffffff==0x08200000
                and win32gui.GetWindowLong(hwnd,-16)&0xffffffff==0x94000000)
    try:
        check()
        if not native_identity():return False
        automation=IUIA().iuia
        walker=automation.ControlViewWalker
        node=automation.ElementFromHandle(hwnd)
        expected=((50033,bridge,False),(50033,'InputSiteWindowClass',False),
                  (50032,'Popup',True),(50022,'ToolTip',False),(50020,'TextBlock',False))
        for depth,(role,cls,offscreen) in enumerate(expected):
            check()
            # The NOACTIVATE WinUI bridge advertises keyboard focusability on
            # some informational tooltips. It must never actually hold focus;
            # every descendant remains strictly nonfocusable.
            if (not node or node.CurrentIsPassword or node.CurrentHasKeyboardFocus
                    or (depth>0 and node.CurrentIsKeyboardFocusable)):
                return False
            if (node.CurrentProcessId!=parent['pid'] or node.CurrentControlType!=role
                    or node.CurrentClassName!=cls or node.CurrentAutomationId!=''
                    or bool(node.CurrentIsOffscreen)!=offscreen):return False
            native=int(node.CurrentNativeWindowHandle or 0)
            if depth==0:
                if native!=hwnd:return False
            elif depth==1:
                if (not native or not win32gui.IsWindow(native)
                        or win32gui.GetClassName(native)!='InputSiteWindowClass'
                        or win32process.GetWindowThreadProcessId(native)[1]!=parent['pid']
                        or not win32gui.IsChild(hwnd,native) or win32gui.GetAncestor(native,2)!=hwnd):
                    return False
            elif native:return False
            child=walker.GetFirstChildElement(node)
            if child and walker.GetNextSiblingElement(child):return False
            if depth==4:
                if child:return False
            else:
                if not child:return False
                node=child
        check()
        return bool(native_identity())
    except ComputerError:
        raise
    except Exception:
        return False


def _guard(window,check,*,foreground=False):
    import win32gui
    check();fresh=uia._recheck(window)
    if not same_identity(fresh,window) or access._kind(fresh)!='explorer':
        raise ComputerError('windows_explorer_window_changed')
    blocked=[]
    def visit(hwnd,_):
        if (hwnd!=fresh['hwnd'] and win32gui.IsWindowVisible(hwnd) and access._owned(hwnd,fresh['hwnd'])
                and win32gui.GetClassName(hwnd).casefold()!='tooltips_class32'
                and not is_tooltip(hwnd,fresh,check)):
            blocked.append(hwnd)
    win32gui.EnumWindows(visit,None)
    if blocked or not win32gui.IsWindowEnabled(fresh['hwnd']):
        raise ComputerError('windows_explorer_popup_requires_attention')
    if foreground and win32gui.GetForegroundWindow()!=fresh['hwnd']:
        raise ComputerError('windows_explorer_foreground_required')
    check();return fresh


def _prepare(window,check):
    import win32gui
    fresh=_guard(window,check)
    if win32gui.GetForegroundWindow()!=fresh['hwnd']:
        win32gui.SetForegroundWindow(fresh['hwnd']);until=time.monotonic()+1.5
        while win32gui.GetForegroundWindow()!=fresh['hwnd']:
            check()
            if time.monotonic()>until:raise ComputerError('windows_explorer_foreground_required')
            time.sleep(.03)
    return _guard(window,check,foreground=True)


def _shell_collection():
    import win32com.client
    # Fixed read-only collection factory; no Navigate/Open/Execute invocation.
    return win32com.client.Dispatch('Shell.Application').Windows()


def probe(window,check):
    """Read-only factory/token diagnostic; no UIA traversal, focus or input.

    Only PIDs, integrity/session/elevation facts and Shell HWNDs are returned.
    User SIDs are compared locally and are never included in the result.
    """
    import os
    import pythoncom
    import win32api,win32security,win32com.client
    fresh=_guard(window,check)
    def token(pid):
        process=win32api.OpenProcess(0x1000,False,pid)
        handle=None
        try:
            handle=win32security.OpenProcessToken(process,win32security.TOKEN_QUERY)
            integrity=win32security.GetTokenInformation(handle,win32security.TokenIntegrityLevel)
            sid=integrity[0] if isinstance(integrity,tuple) else integrity
            rid=sid.GetSubAuthority(sid.GetSubAuthorityCount()-1)
            user=win32security.GetTokenInformation(handle,win32security.TokenUser)
            user_sid=user[0] if isinstance(user,tuple) else user
            return ({'pid':pid,'integrity_rid':rid,
                     'session_id':win32security.GetTokenInformation(handle,win32security.TokenSessionId),
                     'elevated':bool(win32security.GetTokenInformation(handle,win32security.TokenElevation)),
                     'elevation_type':win32security.GetTokenInformation(handle,win32security.TokenElevationType)},
                    win32security.ConvertSidToStringSid(user_sid))
        finally:
            if handle is not None:handle.Close()
            process.Close()
    worker,worker_user=token(os.getpid());shell,shell_user=token(fresh['pid'])
    result={'window_hwnd':fresh['hwnd'],'worker':worker,'explorer':shell,
            'same_user':worker_user==shell_user,'factories':[]}
    def sample(name,factory):
        check()
        try:
            collection=factory();count=collection.Count
            if type(count) is not int or not 0<=count<=64:raise ComputerError('windows_explorer_shell_ambiguous')
            handles=[]
            for index in range(count):
                check();item=collection.Item(index)
                if item is not None:handles.append(int(item.HWND))
            result['factories'].append({'factory':name,'count':count,'returned_hwnds':handles,
                                        'matching_hwnds':[h for h in handles if h==fresh['hwnd']]})
        except Exception as exc:
            if isinstance(exc,ComputerError):raise
            result['factories'].append({'factory':name,'error_type':type(exc).__name__,
                                        'error_hresult':getattr(exc,'hresult',None)})
    sample('Shell.Application.Windows',_shell_collection)
    for context in (pythoncom.CLSCTX_LOCAL_SERVER,pythoncom.CLSCTX_SERVER):
        def direct(context=context):
            pointer=pythoncom.CoCreateInstance(pythoncom.MakeIID('{9BA05972-F6A8-11CF-A442-00A0C90A8F39}'),
                None,context,pythoncom.IID_IDispatch)
            return win32com.client.Dispatch(pointer)
        sample('CLSID_ShellWindows:'+str(context),direct)
    sample('ROT:Shell.Application.Windows',lambda:win32com.client.GetActiveObject('Shell.Application').Windows())
    check();return result


def probe_navigation(window,check):
    """Read only known navigation/breadcrumb/view UIA properties for diagnosis."""
    from .windows_controls import bounds
    from pywinauto.uia_defines import NoPatternInterfaceError
    fresh=_guard(window,check)
    observed,wrappers=access._tree(fresh,check)
    records=[]
    markers=('address','breadcrumb','autosuggest','folderview','itemsview','fileview','tablistview')
    for c in observed['controls']:
        check();wrapper=wrappers[c['id']]
        cls=wrapper.element_info.class_name or ''
        identities=[c.get('automation_id',''),cls,*[p.get('automation_id','') for p in c.get('ancestors',[])]]
        if not (c['automation_id']=='upButton'
                or (c['type']!='Edit' and re.match(r'^(?:地址|Address)\s*[:：]',c['name']))
                or any(marker in value.casefold() for value in identities for marker in markers)):
            continue
        record={k:deepcopy(c.get(k)) for k in ('id','runtime_id','type','name','automation_id','value','patterns','ancestors','focused')}
        record.update(class_name=cls,bounds=bounds(wrapper))
        element=wrapper.element_info.element
        for name in ('HelpText','ItemStatus','ItemType','NativeWindowHandle','FrameworkId'):
            record[name]=getattr(element,'Current'+name)
        try:record['legacy_value']=wrapper.iface_legacy_iaccessible.CurrentValue
        except (NoPatternInterfaceError,AttributeError):record['legacy_value']=None
        records.append(record)
    _guard(window,check)
    return {'window':fresh,'truncated':observed['truncated'],'navigation':records}


def _shell_read(window,check):
    import win32gui,win32process
    fresh=_guard(window,check,foreground=True)
    collection=_shell_collection();count=collection.Count
    if type(count) is not int or not 1<=count<=64:
        error=ComputerError('windows_explorer_shell_ambiguous')
        error.content_diagnostic={'collection_count':count,'selected_hwnd':fresh['hwnd'],
                                  'returned_hwnds':[],'matching_hwnds':[]}
        raise error
    matches=[];returned=[]
    for index in range(count):
        check();item=collection.Item(index)
        if item is not None:
            hwnd=int(item.HWND);returned.append(hwnd)
            if hwnd==fresh['hwnd']:matches.append(item)
    if len(matches)!=1:
        error=ComputerError('windows_explorer_shell_ambiguous')
        error.content_diagnostic={'collection_count':count,'selected_hwnd':fresh['hwnd'],
                                  'returned_hwnds':returned,
                                  'matching_hwnds':[int(item.HWND) for item in matches]}
        raise error
    item=matches[0]
    if item.Busy is not False:raise ComputerError('windows_explorer_navigation_pending')
    folder=item.Document.Folder.Self
    if folder.IsFileSystem is not True:raise ComputerError('windows_explorer_virtual_folder_denied')
    directory=_directory(folder.Path)
    tabs=[]
    def child(hwnd,_):
        if win32gui.GetClassName(hwnd)=='ShellTabWindowClass':
            if (win32process.GetWindowThreadProcessId(hwnd)[1]!=fresh['pid']
                    or win32gui.GetAncestor(hwnd,2)!=fresh['hwnd'] or not win32gui.IsWindowVisible(hwnd)):
                raise ComputerError('windows_explorer_tab_ambiguous')
            tabs.append(hwnd)
    win32gui.EnumChildWindows(fresh['hwnd'],child,None)
    if len(tabs)!=1:raise ComputerError('windows_explorer_tab_ambiguous')
    check()
    return {'path':directory['path'],'directory':directory,'parent':_directory(ntpath.dirname(directory['path'])),
            'shell_hwnd':fresh['hwnd'],'shell_tab_hwnd':tabs[0]}


def _parent(wrapper,pid):
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    element=IUIA().iuia.ControlViewWalker.GetParentElement(wrapper.element_info.element)
    if not element:raise ComputerError('windows_explorer_control_unproven')
    value=uia._record(UIAWrapper(UIAElementInfo(element)),pid)
    if value is None:raise ComputerError('windows_explorer_control_unproven')
    return {k:deepcopy(value[k]) for k in ('type','name','automation_id','runtime_id')}


def _roles(window,check):
    from .windows_controls import bounds
    observed,wrappers=access._tree(window,check)
    if observed.get('truncated'):raise ComputerError('windows_explorer_tree_limit')
    controls=observed['controls']
    tabs=[c for c in controls if c['type']=='TabItem'
          and any(p.get('automation_id')=='TabListView' for p in c.get('ancestors',[]))]
    if len(tabs)!=1 or tabs[0].get('selected') is not True:
        raise ComputerError('windows_explorer_tab_ambiguous')
    result=[]
    for role in ('directory','up'):
        options=[c for c in controls if c['visible'] and c['enabled']
                 and ((role=='directory' and c['type']=='Edit' and c['name'] in _ADDRESS_NAMES
                       and c['automation_id']=='TextBox' and 'set_value' in c['patterns']
                       and not any(p.get('automation_id')=='FileExplorerSearchBox' for p in c.get('ancestors',[])))
                      or (role=='up' and c['type']=='Button' and c['automation_id']=='upButton'
                          and re.match(r'^(?:上移到|向上|Up\b)',c['name']) and 'invoke' in c['patterns']))]
        if role=='up' and not options:continue
        if len(options)!=1:raise ComputerError('windows_explorer_control_unproven')
        control=deepcopy(options[0]);wrapper=wrappers[control['id']]
        control['bounds']=bounds(wrapper);control['direct_parent']=_parent(wrapper,window['pid'])
        expected='PART_AutoSuggestBox' if role=='directory' else 'NavigationCommands'
        if control['direct_parent']['automation_id']!=expected:
            raise ComputerError('windows_explorer_control_unproven')
        result.append({'role':role,'label':control['name'],'control':control})
    return result,deepcopy(tabs[0]),wrappers


def validate(snapshot):
    """Static source validation only; current disk/native facts are read later."""
    from .windows_visual_memory import _window
    raw={k:v for k,v in snapshot.items() if k!='native_proof'} if type(snapshot) is dict else {}
    if set(raw)!={'window','application','kind','binding','controls'} or raw['application']!='explorer' or raw['kind']!='explorer':
        raise ComputerError('windows_explorer_source_invalid')
    _window(raw['window']);binding=raw['binding']
    if (type(binding) is not dict or set(binding)!={'path','directory','parent','shell_hwnd','shell_tab_hwnd','tab'}
            or binding['shell_hwnd']!=raw['window']['hwnd'] or type(binding['shell_tab_hwnd']) is not int
            or binding['shell_tab_hwnd']<=0):raise ComputerError('windows_explorer_source_invalid')
    for record in (binding['directory'],binding['parent']):
        if (type(record) is not dict or set(record)!={'path','device','inode'}
                or _lexical(record['path'])!=record['path'] or type(record['device']) is not int
                or type(record['inode']) is not int or record['inode']<=0):
            raise ComputerError('windows_explorer_source_invalid')
    if not _same_path(binding['path'],binding['directory']['path']) or not _same_path(ntpath.dirname(binding['path']),binding['parent']['path']):
        raise ComputerError('windows_explorer_source_invalid')
    tab=binding['tab']
    if (type(tab) is not dict or tab.get('type')!='TabItem' or tab.get('selected') is not True
            or not tab.get('runtime_id') or not any(p.get('automation_id')=='TabListView' for p in tab.get('ancestors',[]))):
        raise ComputerError('windows_explorer_source_invalid')
    if type(raw['controls']) is not list or not 1<=len(raw['controls'])<=2:
        raise ComputerError('windows_explorer_source_invalid')
    seen=set()
    for entry in raw['controls']:
        if type(entry) is not dict or set(entry)!={'role','label','control'} or entry['role'] in seen:
            raise ComputerError('windows_explorer_source_invalid')
        role=entry['role'];seen.add(role);c=entry['control']
        if role not in {'directory','up'} or type(c) is not dict or entry['label']!=c.get('name'):
            raise ComputerError('windows_explorer_source_invalid')
        if (c.get('visible') is not True or c.get('enabled') is not True or not c.get('runtime_id')
                or type(c.get('id')) is not str or type(c.get('value')) not in (str,type(None))
                or c.get('password') or c.get('is_password') or type(c.get('bounds')) is not list
                or len(c['bounds'])!=4 or any(type(v) is not int for v in c['bounds'])
                or c['bounds'][0]>=c['bounds'][2] or c['bounds'][1]>=c['bounds'][3]):
            raise ComputerError('windows_explorer_source_invalid')
        parent=c.get('direct_parent',{})
        if not parent.get('runtime_id'):raise ComputerError('windows_explorer_source_invalid')
        if role=='directory':
            if (c.get('type')!='Edit' or c.get('automation_id')!='TextBox' or c.get('name') not in _ADDRESS_NAMES
                    or parent.get('automation_id')!='PART_AutoSuggestBox' or 'set_value' not in c.get('patterns',[])
                    or type(c.get('value')) is not str or len(c['value'])>3000
                    or any(p.get('automation_id')=='FileExplorerSearchBox' for p in c.get('ancestors',[]))):
                raise ComputerError('windows_explorer_source_invalid')
        elif (c.get('type')!='Button' or c.get('automation_id')!='upButton'
              or parent.get('automation_id')!='NavigationCommands' or 'invoke' not in c.get('patterns',[])
              or not re.match(r'^(?:上移到|向上|Up\b)',c.get('name',''))):
            raise ComputerError('windows_explorer_source_invalid')
    if 'directory' not in seen:raise ComputerError('windows_explorer_source_invalid')
    return snapshot


def snapshot(window,check):
    fresh=_prepare(window,check);first=_shell_read(fresh,check)
    controls,tab,_=_roles(fresh,check);second=_shell_read(fresh,check)
    if first!=second or _guard(window,check,foreground=True)!=fresh:
        raise ComputerError('windows_explorer_navigation_pending')
    return validate({'window':fresh,'application':'explorer','kind':'explorer',
                     'binding':{**second,'tab':tab},'controls':controls})


def _entry(source,role):
    options=[c for c in source['controls'] if c['role']==role]
    if len(options)!=1:raise ComputerError('windows_explorer_control_unproven')
    return options[0]['control']


def _source_live(source):
    if _directory(source['binding']['path'])!=source['binding']['directory'] or _directory(source['binding']['parent']['path'])!=source['binding']['parent']:
        raise ComputerError('windows_explorer_source_changed')


def _same_tab(current,source):
    return (current['binding']['shell_tab_hwnd']==source['binding']['shell_tab_hwnd']
            and all(current['binding']['tab'].get(k)==source['binding']['tab'].get(k)
                    for k in ('id','runtime_id','type','automation_id')))


def _target(source,role,text):
    if role=='directory':return _directory(text)
    if role=='up' and text=='':return _directory(source['binding']['parent']['path'])
    raise ComputerError('windows_explorer_goal_invalid')


def observe(window,source,role,text,check):
    validate(source);_source_live(source)
    if not same_identity(window,source['window']):raise ComputerError('windows_explorer_window_changed')
    target=_target(source,role,text);current=snapshot(window,check)
    if not _same_tab(current,source):raise ComputerError('windows_explorer_tab_changed')
    directory=current['binding']['directory']
    if directory!=source['binding']['directory'] and directory!=target:
        raise ComputerError('windows_explorer_source_changed')
    return {'verified':directory==target,'facts':{'window':current['window'],'binding':current['binding'],
            'role':role,'target':target,'controls':current['controls']}}


def _send_enter(window,check,epoch):
    """Only Enter down/up, using the existing exact-marker double-hook monitor."""
    user,kernel,callback=inputs._api();marker=secrets.randbits(31) or 1
    monitor=inputs._Monitor(user,kernel,callback,marker);monitor.chord=((13,False),(13,True))
    sent=0;phase='hook_start';deadline=time.monotonic()+1
    try:
        monitor.start(1);check();monitor.check();phase=monitor.phase='before_injection'
        if int(user.GetForegroundWindow() or 0)!=window['hwnd']:raise ComputerError('windows_input_foreground_changed')
        if any(user.GetAsyncKeyState(k)&0x8000 for k in range(1,256)):raise ComputerError('windows_input_keys_held')
        check();monitor.check()
        if time.monotonic()>=deadline:raise ComputerError('windows_input_hook_timeout')
        phase=monitor.phase='injecting';sent=inputs._inject(user,monitor.chord,marker)
        if sent!=2:raise ComputerError('windows_input_partial')
        phase=monitor.phase='awaiting_confirmation'
        while not monitor.seen.is_set():
            monitor.check()
            if time.monotonic()>=deadline:raise ComputerError('windows_input_hook_timeout')
            monitor.seen.wait(.005)
        monitor.check()
        if int(user.GetForegroundWindow() or 0)!=window['hwnd']:raise ComputerError('user_takeover')
        epoch[0]=inputs._epoch(user);monitor.check()
    except Exception as exc:
        error=exc if isinstance(exc,ComputerError) else ComputerError('windows_explorer_enter_failed')
        error.input_diagnostic={**monitor.diagnostic(),'phase':phase,'sent_events':sent}
        raise error from None
    finally:
        try:
            if sent==1:inputs._inject(user,((13,True),),marker)
        finally:monitor.close()


def act(window,source,role,text,check,epoch):
    validate(source);_source_live(source);target=_target(source,role,text)
    current=snapshot(window,check)
    if not same_identity(window,source['window']) or not _same_tab(current,source):
        raise ComputerError('windows_explorer_window_changed')
    if current['binding']['directory']!=source['binding']['directory']:
        raise ComputerError('windows_explorer_source_changed')
    control,wanted=_entry(current,role),_entry(source,role)
    if any(control.get(k)!=wanted.get(k) for k in (*_IDENTITY,'value','patterns','enabled','visible')):
        raise ComputerError('windows_explorer_control_changed')
    fresh_roles,fresh_tab,wrappers=_roles(current['window'],check)
    refreshed=_entry({'controls':fresh_roles},role)
    if (any(refreshed.get(k)!=control.get(k) for k in (*_IDENTITY,'value','patterns','enabled','visible'))
            or any(fresh_tab.get(k)!=source['binding']['tab'].get(k)
                   for k in ('id','runtime_id','type','automation_id'))):
        raise ComputerError('windows_explorer_control_changed')
    wrapper=wrappers[control['id']]
    _guard(window,check,foreground=True);check()
    if _shell_read(window,check)!={k:v for k,v in source['binding'].items() if k!='tab'}:
        raise ComputerError('windows_explorer_source_changed')
    if role=='up':
        if target!=source['binding']['parent']:raise ComputerError('windows_explorer_source_changed')
        wrapper.iface_invoke.Invoke();return
    if target==source['binding']['directory']:raise ComputerError('windows_explorer_already_satisfied')
    pattern=wrapper.iface_value
    if pattern.CurrentIsReadOnly or pattern.CurrentValue!=control['value']:
        raise ComputerError('windows_explorer_control_changed')
    wrapper.element_info.element.SetFocus();check()
    if not wrapper.element_info.element.CurrentHasKeyboardFocus:
        raise ComputerError('windows_explorer_focus_unverified')
    if pattern.CurrentValue!=control['value'] and not _same_path(pattern.CurrentValue,source['binding']['path']):
        raise ComputerError('windows_explorer_control_changed')
    pattern.SetValue(target['path']);check()
    if not _same_path(pattern.CurrentValue,target['path']):raise ComputerError('windows_explorer_value_unverified')
    def before_enter():
        check();_guard(window,check,foreground=True);_source_live(source)
        if (_shell_read(window,check)!= {k:v for k,v in source['binding'].items() if k!='tab'}
                or not wrapper.element_info.element.CurrentHasKeyboardFocus
                or not _same_path(pattern.CurrentValue,target['path'])):
            raise ComputerError('windows_explorer_source_changed')
    _send_enter(window,before_enter,epoch)
