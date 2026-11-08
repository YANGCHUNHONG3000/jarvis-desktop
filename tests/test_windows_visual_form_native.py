"""Offline native boundary tests; these do not substitute for WPS acceptance."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
import sys

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_visual_form_native as native

PARENT = {'hwnd': 10, 'pid': 20, 'created': 'now', 'image': 'C:/wps.exe',
          'class_name': 'XLMAIN', 'title': 'original.xlsx', 'profile': 'observe_only'}
DIALOG = {**PARENT, 'hwnd': 11, 'class_name': 'Qt5QWindow', 'title': '查找'}
DOC = {'stable': 'document'}


@pytest.fixture
def windows(monkeypatch):
    state = {'visible': [11], 'exists': {10, 11}, 'foreground': 11, 'owner': {11: 10}, 'raises': [],
             'children': set(), 'roots': {10: 10, 11: 11},
             'identities': {10: deepcopy(PARENT), 11: deepcopy(DIALOG)}, 'rect': [10, 20, 575, 262]}
    gui = SimpleNamespace(
        EnumWindows=lambda callback, unused: [callback(hwnd, None) for hwnd in list(state['visible'])],
        IsWindow=lambda hwnd: hwnd in state['exists'],
        IsWindowVisible=lambda hwnd: hwnd in state['visible'],
        IsWindowEnabled=lambda hwnd: True,
        GetClassName=lambda hwnd: state['identities'][hwnd]['class_name'],
        GetWindowText=lambda hwnd: state['identities'][hwnd]['title'],
        GetWindow=lambda hwnd, flag: state['owner'].get(hwnd, 0),
        IsChild=lambda parent, child: (parent, child) in state['children'],
        GetAncestor=lambda hwnd, flag: state['roots'].get(hwnd, hwnd),
        GetWindowLong=lambda hwnd, flag: 0,
        GetForegroundWindow=lambda: state['foreground'],
        SetForegroundWindow=lambda hwnd: (state['raises'].append(hwnd), state.update(foreground=hwnd)),
        GetWindowRect=lambda hwnd: tuple(state['rect']))
    monkeypatch.setitem(sys.modules, 'win32gui', gui)
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (0, state['identities'][hwnd]['pid'])))
    monkeypatch.setattr(native.uia, '_recheck', lambda value: deepcopy(state['identities'][value['hwnd']]))
    monkeypatch.setattr(native.uia, '_identity', lambda hwnd: deepcopy(state['identities'][hwnd]))
    monkeypatch.setattr(native.access, '_kind', lambda value: 'wps')
    monkeypatch.setattr(native.access, '_process_owned', lambda child, parent:
        all(child[key] == parent[key] for key in ('pid', 'created', 'image')))
    monkeypatch.setattr(native.access, '_owned', lambda hwnd, parent: hwnd != parent)
    return state


def source():
    return {'window': deepcopy(PARENT), 'dialog': deepcopy(DIALOG), 'document': deepcopy(DOC),
            'rect': [10, 20, 575, 262], 'controls': [], 'frame': {'sha256': sha256(b'frame').hexdigest()}}


def test_guard_unique_find(windows):
    assert native._guard(PARENT, lambda: None, foreground=True) == (PARENT, DIALOG, windows['rect'])


def test_expanded_office_height_is_opt_in_and_bounded(windows):
    windows['rect'][3] = windows['rect'][1] + 327
    with pytest.raises(ComputerError, match='windows_visual_form_frame_limit'):
        native._guard(PARENT, lambda: None)
    assert native._guard(PARENT, lambda: None, max_dialog_height=720)[1] == DIALOG
    windows['rect'][3] = windows['rect'][1] + 721
    with pytest.raises(ComputerError, match='windows_visual_form_frame_limit'):
        native._guard(PARENT, lambda: None, max_dialog_height=720)
    with pytest.raises(ComputerError, match='windows_visual_form_frame_limit'):
        native._guard(PARENT, lambda: None, max_dialog_height=10000)


@pytest.mark.parametrize('registered', [True, False])
def test_find_companion_uses_existing_registered_process_lineage_guard(windows, monkeypatch, registered):
    windows['identities'][11].update(pid=21, created='later', image='C:/et.exe')
    calls = []
    def process_owned(child, parent):
        calls.append((deepcopy(child), deepcopy(parent)))
        return registered
    monkeypatch.setattr(native.access, '_process_owned', process_owned)
    if registered:
        assert native._guard(PARENT, lambda: None)[1] == windows['identities'][11]
    else:
        with pytest.raises(ComputerError, match='popup_requires_attention') as error:
            native._guard(PARENT, lambda: None)
        assert error.value.content_diagnostic['predicate'] == 'process_binding'
    assert calls == [(windows['identities'][11], PARENT)]


@pytest.mark.parametrize('case', ['valid', 'foreign_process', 'wrong_root', 'not_child'])
def test_find_owner_must_be_same_process_native_child_of_exact_parent(windows, case):
    windows['identities'][12] = {**PARENT, 'hwnd': 12, 'title': '', 'class_name': 'native editor'}
    windows['owner'][11] = 12
    windows['children'].add((10, 12))
    windows['roots'][12] = 10
    if case == 'foreign_process':
        windows['identities'][12]['pid'] = 999
    elif case == 'wrong_root':
        windows['roots'][12] = 99
    elif case == 'not_child':
        windows['children'].clear()
    if case == 'valid':
        assert native._guard(PARENT, lambda: None)[1] == DIALOG
    else:
        with pytest.raises(ComputerError, match='popup_requires_attention') as error:
            native._guard(PARENT, lambda: None)
        proof = error.value.content_diagnostic
        assert proof['predicate'] == 'owner_binding' and proof['owner_hwnd'] == 12
        assert set(proof) == {'predicate','hwnd','class_name','title','pid','owner_hwnd',
                              'owner_class','owner_pid','owner_root_hwnd'}


@pytest.mark.parametrize('change', ['unknown', 'foreign', 'nested', 'duplicate', 'title', 'foreground', 'size'])
def test_guard_rejects_popup_or_identity_change(windows, change):
    if change in {'unknown', 'foreign', 'nested', 'duplicate'}:
        windows['visible'].append(12)
        windows['exists'].add(12)
        windows['identities'][12] = {**DIALOG, 'hwnd': 12}
        windows['owner'][12] = 10
    if change == 'unknown':
        windows['identities'][12]['title'] = '付款'
    elif change == 'foreign':
        windows['identities'][12]['pid'] = 21
    elif change == 'nested':
        windows['owner'][12] = 11
    elif change == 'title':
        windows['identities'][10]['title'] = 'other.xlsx'
    elif change == 'foreground':
        windows['foreground'] = 10
    elif change == 'size':
        windows['rect'][3] = 281
    with pytest.raises(ComputerError):
        native._guard(PARENT, lambda: None, foreground=True)


@pytest.mark.parametrize('destroyed', [True, False])
def test_closed_distinguishes_hidden_from_destroyed(windows, monkeypatch, destroyed):
    windows['visible'].clear()
    if destroyed:
        windows['exists'].remove(11)
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: deepcopy(DOC))
    result = native.observe(PARENT, source(), 'close_find', '', lambda: None)
    assert result == {'snapshot': None, 'document': DOC, 'dialog_open': False,
                      'dialog_presence': 'destroyed' if destroyed else 'hidden', 'field_value': None}


def test_closed_rejects_hidden_reused_handle(windows):
    windows['visible'].clear()
    windows['identities'][11]['title'] = 'new dialog'
    with pytest.raises(ComputerError, match='dialog_changed'):
        native._guard(PARENT, lambda: None, expected=DIALOG, allow_closed=True)


def test_closed_rejects_changed_document(windows, monkeypatch):
    windows['visible'].clear()
    windows['exists'].remove(11)
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: {'stable': 'changed'})
    with pytest.raises(ComputerError, match='document_changed'):
        native.closed(source(), lambda: None)


def test_snapshot_race_stops_before_return(windows, monkeypatch):
    monkeypatch.setattr(native, '_controls', lambda *args: ([], {}))
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: DOC)
    captures = iter([(b'before', windows['rect']), (b'after', windows['rect'])]*3)
    monkeypatch.setattr(native, '_capture', lambda *args: next(captures))
    monkeypatch.setattr(native.visual, 'recognize', lambda *args,**kw: {'frame': 'read'})
    with pytest.raises(ComputerError, match='screen_changed'):
        native.snapshot(PARENT, lambda: None)


def test_field_changed_value_never_set(windows, monkeypatch):
    field = {'id': 'edit', 'type': 'Edit', 'enabled': True, 'visible': True,
             'patterns': ['set_value'], 'value': 'before'}
    src = source()
    src['controls'] = [deepcopy(field)]
    pattern = SimpleNamespace(CurrentIsReadOnly=False, CurrentValue='changed',
                              SetValue=lambda text: pytest.fail('must not dispatch'))
    monkeypatch.setattr(native, '_controls', lambda *args: ([field], {'edit': SimpleNamespace(iface_value=pattern)}))
    with pytest.raises(ComputerError, match='screen_changed'):
        native.set_field(src, 'edit', 'query', lambda: None)


def test_set_field_single_dispatch_and_exact_readback(monkeypatch):
    calls = []
    pattern = SimpleNamespace(CurrentValue='')
    def set_value(text):
        calls.append(text)
        pattern.CurrentValue = text
    pattern.SetValue = set_value
    monkeypatch.setattr(native, '_field', lambda *args: pattern)
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (PARENT, DIALOG, source()['rect']))
    assert native.set_field(source(), 'edit', 'query', lambda: None) == 'query'
    assert calls == ['query']


@pytest.mark.parametrize('text', ['bad\nquery', 7, 'x'*301])
def test_invalid_text_rejected_before_native(text, monkeypatch):
    monkeypatch.setattr(native, '_field', lambda *args: pytest.fail('no native dispatch'))
    with pytest.raises(ComputerError, match='text_invalid'):
        native.set_field(source(), 'edit', text, lambda: None)


def test_close_click_once_preserves_epoch_and_checks_geometry(windows, monkeypatch):
    from agent_service import windows_pointer
    calls = []
    def click(hwnd, x, y, **kwargs):
        kwargs['check']()
        calls.append((hwnd, x, y))
        windows['exists'].remove(hwnd);windows['visible'].clear()
        return 42
    monkeypatch.setattr(windows_pointer, 'guarded_click', click)
    monkeypatch.setattr(native, '_capture', lambda *args: (b'frame', windows['rect']))
    epoch = [4]
    native.click_close(source(), [400, 220], lambda: None, epoch)
    assert calls == [(11, 400, 220)] and epoch == [42]
    windows['exists'].add(11);windows['visible'].append(11)
    windows['rect'][0] += 1
    with pytest.raises(ComputerError, match='screen_changed'):
        native.click_close(source(), [400, 220], lambda: None, epoch)
    assert len(calls) == 1


def test_prepare_raises_only_already_guarded_find_without_input(windows):
    windows['foreground'] = 99
    assert native.prepare(PARENT, lambda: None) == DIALOG
    assert windows['foreground'] == 11 and windows['raises'] == [11]


def test_prepare_rejects_unknown_popup_before_foreground_request(windows):
    windows['foreground'] = 99
    windows['identities'][11]['title'] = 'unknown'
    with pytest.raises(ComputerError, match='popup_requires_attention'):
        native.prepare(PARENT, lambda: None)
    assert windows['foreground'] == 99 and windows['raises'] == []


def test_prepare_foreground_denied_captures_nothing(windows, monkeypatch):
    windows['foreground'] = 99
    gui = sys.modules['win32gui']
    monkeypatch.setattr(gui, 'SetForegroundWindow', lambda hwnd: windows['raises'].append(hwnd))
    clock = iter([0, 2])
    monkeypatch.setattr(native.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(native, '_capture', lambda *args: pytest.fail('no capture without foreground grant'))
    with pytest.raises(ComputerError, match='foreground_required'):
        native.snapshot(PARENT, lambda: None)
    assert windows['raises'] == [11]


def test_snapshot_reobserves_caret_drift_at_most_three_times(windows, monkeypatch):
    monkeypatch.setattr(native, '_controls', lambda *args: ([], {}))
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: DOC)
    images = iter([b'caret', b'no-caret', b'stable', b'stable'])
    calls = []
    def capture(*args):
        image = next(images)
        calls.append(image)
        return image, windows['rect']
    monkeypatch.setattr(native, '_capture', capture)
    monkeypatch.setattr(native.visual, 'recognize', lambda pixels, *args,**kw: {'sha256': sha256(pixels).hexdigest()})
    result = native.snapshot(PARENT, lambda: None)
    assert result['frame']['sha256'] == sha256(b'stable').hexdigest() and len(calls) == 4


def test_close_screen_drift_refuses_click(windows, monkeypatch):
    from agent_service import windows_pointer
    monkeypatch.setattr(native, '_capture', lambda *args: (b'changed', windows['rect']))
    monkeypatch.setattr(windows_pointer, 'guarded_click', lambda *args, **kwargs: pytest.fail('no stale click'))
    epoch = [4]
    with pytest.raises(ComputerError, match='screen_changed'):
        native.click_close(source(), [400, 220], lambda: None, epoch)
    assert epoch == [4]


@pytest.mark.parametrize('effect',['delayed_destroy','hidden','cancel','takeover'])
def test_close_waits_once_for_native_destruction_with_updated_epoch_and_bounded_cancel(windows,monkeypatch,effect):
    from agent_service import windows_pointer
    state={'now':0.0,'clicks':0,'waiting_checks':0}
    epoch=[4]
    monkeypatch.setattr(native,'_capture',lambda *args:(b'frame',windows['rect']))
    monkeypatch.setattr(native.time,'monotonic',lambda:state['now'])
    def sleep(delay):
        state['now']+=delay
        if effect=='delayed_destroy' and state['now']>=.09:
            windows['exists'].discard(11);windows['visible'].clear()
    monkeypatch.setattr(native.time,'sleep',sleep)
    def click(*args,**kwargs):
        kwargs['check']()
        state['clicks']+=1
        return 42
    monkeypatch.setattr(windows_pointer,'guarded_click',click)
    def check():
        if state['clicks']:
            assert epoch==[42]
            state['waiting_checks']+=1
            if effect=='hidden':windows['visible'].clear()
            if effect=='takeover' and state['now']>=.03:raise ComputerError('user_takeover')
    stop=lambda:effect=='cancel' and state['now']>=.03
    if effect=='delayed_destroy':
        native.click_close(source(),[400,220],check,epoch,should_stop=stop)
        assert .09<=state['now']<1.5 and 11 not in windows['exists']
    else:
        code={'hidden':'close_unproven','cancel':'input_cancelled','takeover':'user_takeover'}[effect]
        with pytest.raises(ComputerError,match=code):
            native.click_close(source(),[400,220],check,epoch,should_stop=stop)
        assert 11 in windows['exists'] and state['now']<=1.54
    assert state['clicks']==1 and state['waiting_checks']>=2 and epoch==[42]


@pytest.mark.parametrize('effect', ['destroyed', 'hidden', 'foreign_foreground', 'unknown_popup'])
def test_close_post_target_only_accepts_destroyed_original_with_original_parent_foreground(windows, monkeypatch, effect):
    from agent_service import windows_pointer
    monkeypatch.setattr(native, '_capture', lambda *args: (b'frame', windows['rect']))
    monkeypatch.setattr(windows_pointer, '_target', lambda *args:
        (_ for _ in ()).throw(ComputerError('original_target_required')))
    injected = []
    def click(hwnd, x, y, **kwargs):
        kwargs['check']()
        injected.append(True)
        windows['visible'].clear()
        windows['foreground'] = 10
        if effect != 'hidden':
            windows['exists'].remove(11)
        if effect == 'foreign_foreground':
            windows['foreground'] = 99
        elif effect == 'unknown_popup':
            windows['visible'].append(12)
            windows['identities'][12] = {**DIALOG, 'hwnd': 12, 'title': 'unknown'}
            windows['owner'][12] = 10
        kwargs['after_target'](SimpleNamespace(GetForegroundWindow=lambda: windows['foreground']), hwnd, x, y)
        return 42
    monkeypatch.setattr(windows_pointer, 'guarded_click', click)
    def check():
        if injected and epoch!=[42]:
            pytest.fail('stale worker epoch must not be checked after own injection')
    epoch = [4]
    if effect == 'destroyed':
        native.click_close(source(), [400, 220], check, epoch)
        assert epoch == [42]
    else:
        with pytest.raises(ComputerError):
            native.click_close(source(), [400, 220], check, epoch)
        assert epoch == [4]


def test_missing_find_never_recreated_for_input(windows, monkeypatch):
    windows['visible'].clear()
    windows['exists'].remove(11)
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: deepcopy(DOC))
    with pytest.raises(ComputerError, match='dialog_missing'):
        native.observe(PARENT, source(), 'search_text', 'query', lambda: None)


@pytest.fixture
def close_focus(windows,monkeypatch):
    combo={'id':'combo','type':'ComboBox','name':'查找内容(N)','automation_id':'','value':'query',
           'visible':True,'enabled':True,'owner_hwnd':11,'patterns':['set_value'],'ancestors':[]}
    edit={'id':'edit','type':'Edit','name':'','automation_id':'','value':'query','focused':True,
          'visible':True,'enabled':True,'owner_hwnd':11,'patterns':['set_value'],
          'ancestors':[{k:combo[k] for k in ('type','name','automation_id')}]}
    button={'id':'close','runtime_id':[42],'type':'Button','name':'关闭','automation_id':'',
            'value':'','focused':False,'visible':True,'enabled':True,'owner_hwnd':11,
            'patterns':['focus','invoke'],'bounds':[400,210,460,240],'ancestors':[]}
    title={**button,'id':'title','runtime_id':[43],'ancestors':[{'type':'TitleBar'}]}
    state={'items':[combo,edit,button,title],'focuses':0,'noop':False}
    def focus():
        state['focuses']+=1
        if not state['noop']:
            state['items'][2]['focused']=True
            state['items'][1]['focused']=False
    wrapper=SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(SetFocus=focus)),
                            iface_invoke=SimpleNamespace(Invoke=lambda:pytest.fail('must not Invoke')))
    class FieldPattern:
        CurrentIsReadOnly=False
        @property
        def CurrentValue(self):return state['items'][1]['value']
        def SetValue(self,*args):pytest.fail('observation must never set text')
    editor_wrapper=SimpleNamespace(iface_value=FieldPattern())
    monkeypatch.setattr(native,'_controls',lambda *args:
        (deepcopy(state['items']),{'close':wrapper,'edit':editor_wrapper}))
    state['source']={**source(),'controls':deepcopy(state['items'])}
    return state


def test_close_focus_only_footer_and_preserves_query(close_focus):
    before=close_focus['items'][1]['value']
    native.prepare_close_focus(close_focus['source'],lambda:None)
    assert close_focus['focuses']==1 and close_focus['items'][2]['focused'] is True
    assert close_focus['items'][1]['focused'] is False and close_focus['items'][1]['value']==before
    native.prepare_close_focus(close_focus['source'],lambda:None)
    assert close_focus['focuses']==1


def test_input_observation_stabilizes_focus_without_invocation_or_text_write(close_focus,monkeypatch):
    monkeypatch.setattr(native,'document',lambda *args,**kwargs:deepcopy(DOC))
    snapshots=[]
    def snapshot(window,check):
        assert close_focus['items'][2]['focused'] is True
        assert close_focus['items'][1]['focused'] is False
        snapshots.append(True)
        return {**deepcopy(close_focus['source']),'controls':deepcopy(close_focus['items'])}
    monkeypatch.setattr(native,'snapshot',snapshot)
    result=native.observe(PARENT,close_focus['source'],'search_text','query',lambda:None)
    assert result['field_value']=='query' and result['snapshot']['controls'][1]['value']=='query'
    assert close_focus['focuses']==1 and len(snapshots)==1
    assert close_focus['items'][1]['value']=='query'


@pytest.mark.parametrize('change',['popup','identity','bounds','name','title_only','no_focus_pattern','duplicate'])
def test_unreviewed_or_changed_close_cannot_be_focused(windows,close_focus,change):
    if change=='popup':
        windows['visible'].append(12);windows['identities'][12]={**DIALOG,'hwnd':12,'title':'unknown'}
        windows['owner'][12]=10
    elif change=='identity':close_focus['items'][2]['runtime_id']=[99]
    elif change=='bounds':close_focus['items'][2]['bounds'][0]+=1
    elif change=='name':close_focus['items'][2]['name']='删除'
    elif change=='title_only':close_focus['items'][2]['ancestors']=[{'type':'TitleBar'}]
    elif change=='no_focus_pattern':close_focus['items'][2]['patterns']=['invoke']
    elif change=='duplicate':close_focus['items'].append(deepcopy(close_focus['items'][2]))
    with pytest.raises(ComputerError):native.prepare_close_focus(close_focus['source'],lambda:None)
    assert close_focus['focuses']==0


def test_close_focus_requires_actual_readback_not_method_return(close_focus,monkeypatch):
    close_focus['noop']=True
    ticks=iter([0,2]);monkeypatch.setattr(native.time,'monotonic',lambda:next(ticks))
    with pytest.raises(ComputerError,match='focus_unverified'):
        native.prepare_close_focus(close_focus['source'],lambda:None)
    assert close_focus['focuses']==1 and close_focus['items'][1]['value']=='query'


def test_close_focus_rejects_unexpected_text_change(close_focus,monkeypatch):
    original=native._controls;reads=[]
    def controls(*args):
        reads.append(True)
        if len(reads)>1:close_focus['items'][1]['value']='changed'
        return original(*args)
    monkeypatch.setattr(native,'_controls',controls)
    with pytest.raises(ComputerError,match='close_changed'):
        native.prepare_close_focus(close_focus['source'],lambda:None)
    assert close_focus['focuses']==1


def test_password_descendant_rejected_before_text_or_capture(monkeypatch):
    root = SimpleNamespace(CurrentIsPassword=False, CurrentProcessId=20)
    # No label/value/process properties are supplied on the protected node.
    protected = SimpleNamespace(CurrentIsPassword=True)
    walker = SimpleNamespace(GetFirstChildElement=lambda element: protected if element is root else None,
                             GetNextSiblingElement=lambda element: None)
    desktop = SimpleNamespace(window=lambda **kwargs: SimpleNamespace(wrapper_object=lambda:
        SimpleNamespace(element_info=SimpleNamespace(element=root))))
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Desktop=lambda **kwargs: desktop))
    monkeypatch.setitem(sys.modules, 'pywinauto.uia_defines', SimpleNamespace(
        IUIA=lambda: SimpleNamespace(iuia=SimpleNamespace(ControlViewWalker=walker))))
    with pytest.raises(ComputerError, match='protected_control'):
        native._password_guard(DIALOG, lambda: None)


@pytest.mark.parametrize('failure', [None, 'unsaved', 'bytes'])
def test_document_uses_exact_existing_application_and_rejects_change(tmp_path, monkeypatch, failure):
    from agent_service import windows_workbook_binding as books
    path = tmp_path/'fixture.xlsx'
    facts = {'path': str(path), 'sha256': 'a'*64, 'size': 10}
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (PARENT, DIALOG, source()['rect']))
    def no_word(*args):
        raise ComputerError('windows_document_anchor_unavailable')
    monkeypatch.setattr(native.files, '_document_application', no_word)
    monkeypatch.setattr(books, '_workbook_application', lambda *args:
        (None, SimpleNamespace(ActiveWorkbook=SimpleNamespace(FullName=str(path))), None))
    monkeypatch.setattr(native.files, '_local_path', lambda value: __import__('pathlib').Path(value))
    monkeypatch.setattr(native.files, 'document_status', lambda *args:
        {'active': True, 'saved': failure != 'unsaved', 'read_only': False})
    reads = []
    def file_facts(value):
        reads.append(value)
        return {**facts, 'sha256': 'b'*64} if failure == 'bytes' and len(reads) > 1 else deepcopy(facts)
    monkeypatch.setattr(native.binding, '_file', file_facts)
    monkeypatch.setattr(native.binding, 'verify', lambda result, **kwargs: result)
    monkeypatch.setattr(native.binding, 'capture', lambda *args: pytest.fail('must not weaken global popup gate'))
    if failure:
        with pytest.raises(ComputerError, match='unsaved|document_changed'):
            native.document(PARENT, lambda: None, expected_dialog=DIALOG)
    else:
        result = native.document(PARENT, lambda: None, expected_dialog=DIALOG)
        assert result['path'] == str(path) and result['saved'] is True and len(reads) == 2
