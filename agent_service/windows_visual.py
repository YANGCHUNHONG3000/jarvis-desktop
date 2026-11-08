"""Local OCR navigation, bound to current window pixels and text outcomes.

This is a restricted navigation provider, not an arbitrary coordinate tool.
"""
from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

from .computer import ComputerError, digest, read_record, write_new

TYPE = 'VisualNavigation'
ID = 'jarvis.visual.navigation.v1'
SELECTION_ID = 'jarvis.visual.navigation.v2'
LABELS = {'开始', '插入', '页面', '页面布局', '公式', '数据', '审阅', '视图', '工具', '帮助', '查看',
          'Home', 'Insert', 'Layout', 'Formulas', 'Data', 'Review', 'View', 'Help'}
ACTION = 'visual_navigate'


def is_goal(selector):
    return type(selector) is dict and selector.get('type') == TYPE and selector.get('automation_id') in {ID,SELECTION_ID}


def normalize(text):
    return re.sub(r'\s+', '', text).casefold()


def decode(selector, value):
    if (not is_goal(selector) or set(selector) != {'type', 'name', 'automation_id'}
            or selector.get('name') not in LABELS or type(value) is not str or len(value) > 1200):
        raise ComputerError('windows_visual_goal_invalid')
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        raise ComputerError('windows_visual_goal_invalid') from None
    if selector['automation_id']==SELECTION_ID:
        if type(data) is not dict or set(data)!={'selected'} or data['selected'] is not True:
            raise ComputerError('windows_visual_goal_invalid')
        return data
    if (type(data) is not dict or set(data) != {'all'} or type(data['all']) is not list
            or not 2 <= len(data['all']) <= 4 or any(type(x) is not str or not 2 <= len(x.strip()) <= 40
            or any(ord(c) < 32 for c in x) for x in data['all'])):
        raise ComputerError('windows_visual_goal_invalid')
    normalized = [normalize(x) for x in data['all']]
    if (len(set(normalized)) != len(normalized) or any(n in {normalize(x) for x in LABELS} for n in normalized)
            or any(a in b for a in normalized for b in normalized if a != b)):
        raise ComputerError('windows_visual_goal_invalid')
    return data


def condition(window, label, expected, *, number=1):
    selector = {'type': TYPE, 'name': label, 'automation_id': ID}
    value = json.dumps({'all': expected}, ensure_ascii=False, separators=(',', ':'))
    decode(selector, value)
    return {'id': f'target_{number}', 'window': deepcopy(window), 'selector': selector,
            'state': 'value', 'value': value, 'retain': False}


def selection_condition(window,label,*,number=1):
    selector={'type':TYPE,'name':label,'automation_id':SELECTION_ID}
    value='{"selected":true}'
    decode(selector,value)
    return {'id':f'target_{number}','window':deepcopy(window),'selector':selector,
            'state':'value','value':value,'retain':False}


def validate_frame(frame):
    if (type(frame) is not dict or frame.get('schema') != 1
            or not re.fullmatch('[0-9a-f]{64}', str(frame.get('sha256', '')))
            or type(frame.get('width')) is not int or not 200 <= frame['width'] <= 4096
            or type(frame.get('height')) is not int or not 60 <= frame['height'] <= 260
            or type(frame.get('words')) is not list or len(frame['words']) > 300):
        raise ComputerError('windows_visual_frame_invalid')
    for item in frame['words']:
        if (type(item) is not dict or set(item) != {'text', 'box'} or type(item['text']) is not str
                or not item['text'] or len(item['text']) > 300 or type(item['box']) is not list
                or len(item['box']) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in item['box'])):
            raise ComputerError('windows_visual_frame_invalid')
        x,y,w,h = item['box']
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > frame['width']+1 or y+h > frame['height']+1:
            raise ComputerError('windows_visual_frame_invalid')
    return frame


def spans(frame, label):
    """Find exact OCR text spans, including adjacent split Chinese characters."""
    validate_frame(frame)
    wanted = normalize(label)
    matches = []
    words = frame['words']
    for i, first in enumerate(words):
        x,y,w,h = first['box']
        text = ''
        right = x
        bottom = y+h
        for item in words[i:i+16]:
            a,b,c,d = item['box']
            if abs((b+d/2)-(y+h/2)) > max(h,d)*.45 or a < right-1 or a-right > max(h,d)*1.3:
                break
            text += normalize(item['text'])
            right, bottom = a+c, max(bottom,b+d)
            if text == wanted:
                matches.append([x,y,right-x,bottom-y])
                break
            if not wanted.startswith(text):
                break
    # Exact boxes only; duplicated labels never become a first-match click.
    return list({tuple(b): b for b in matches}.values())


def presentation(frame, selector, value):
    data = decode(selector, value)
    validate_frame(frame)
    label = spans(frame, selector['name'])
    # The title row / document body is not a navigation permission surface.
    label = [box for box in label if 32 <= box[1] and box[1]+box[3] <= 125
             and 8 <= box[3] <= 44 and box[2] <= 160]
    # A familiar word in a ribbon COMMAND is not a navigation permission.
    # Require the first actual row of >=3 distinct navigation labels; e.g.
    # Word's Insert/Formula command below that row must never be clicked.
    peers = [(name,b) for name in sorted(LABELS) for b in spans(frame,name)
             if 32<=b[1] and b[1]+b[3]<=125 and 8<=b[3]<=44 and b[2]<=160]
    peers.sort(key=lambda item:(item[1][0],item[1][1],item[0]))
    rows = []
    for _,b in peers:
        row = [(n,p) for n,p in peers if abs((p[1]+p[3]/2)-(b[1]+b[3]/2)) <= min(p[3],b[3])*.4]
        if len({normalize(n) for n,_ in row})>=3:
            rows.append(row)
    row = min(rows,key=lambda entries:min(b[1] for _,b in entries)) if rows else []
    label = [b for b in label if any(n==selector['name'] and b==p for n,p in row)]
    low = label[0][1]+label[0][3]+6 if len(label)==1 else 240
    hits = {text: [b for b in spans(frame, text) if low <= b[1]
                   and b[1]+b[3] <= min(180,low+110)] for text in data.get('all',[])}
    selected=None
    if data.get('selected'):
        from .windows_visual_selection import readback
        selected=readback(frame,row)
        met=(len(label)==1 and selected['selected_labels']==[selector['name']]
             and len(selected['content_labels'])>=2)
    else:
        met = len(label)==1 and all(len(found)==1 for found in hits.values())
    c = {'id': digest(selector)[:24], **selector, 'enabled': True, 'visible': True,
         'value': value if met else '', 'patterns': [ACTION], 'selected': None,
         'expanded': None, 'focused': False}
    details={'frame': frame, 'label_boxes': label, 'navigation_row':row, 'expected_matches': hits,
               'verified': met, 'selector': selector, 'value': value}
    if selected is not None:details['selection_readback']=selected
    return c,details


def allowed(observation, control, action, value):
    if (action != ACTION or type(observation) is not dict
            or observation.get('application') not in {'explorer','wps'}
            or observation.get('blocked_reason') or observation.get('truncated')
            or observation.get('dialogs') or type(control) is not dict):
        return False
    visual = observation.get('visual', {})
    try:
        c, details = presentation(visual['frame'], visual['selector'], value)
    except (KeyError, TypeError, ComputerError):
        return False
    selection=details.get('selection_readback')
    if selection is not None and (len(selection['selected_labels'])!=1 or len(selection['content_labels'])<2):
        return False
    return (control == c and visual.get('value') == value and visual.get('label_boxes') == details['label_boxes']
            and len(details['label_boxes']) == 1 and not details['verified'])


def candidates(observation, goal):
    if goal.get('state') != 'value':
        return []
    controls = [c for c in observation['controls'] if c.get('type') == TYPE and c.get('name') == goal['selector']['name']]
    if len(controls) == 1 and allowed(observation, controls[0], ACTION, goal['value']):
        return [{'control_id': controls[0]['id'], 'action': ACTION, 'value': goal['value']}]
    return []


def _guard(window, check, *, require_foreground=True):
    import win32gui
    from . import computer_uia as uia, windows_access as access
    check()
    identity = uia._recheck(window)
    kind = access._kind(identity)
    if identity['title'] != window['title']:
        raise ComputerError('windows_document_window_changed')
    blocked = []
    def visit(hwnd, _):
        if hwnd == identity['hwnd'] or not win32gui.IsWindowVisible(hwnd) or not access._owned(hwnd, identity['hwnd']):
            return
        cls = win32gui.GetClassName(hwnd)
        if cls.casefold() == 'tooltips_class32':
            return
        if cls == 'KLiteMainWindowShadowBorder' and not win32gui.GetWindowText(hwnd):
            import win32process
            if (win32process.GetWindowThreadProcessId(hwnd)[1] == identity['pid']
                    and win32gui.GetWindowLong(hwnd, -20) & 0x80080 == 0x80080):
                return
        # Never capture popup text/password surfaces. Ownership is diagnostic only.
        blocked.append({'hwnd': hwnd, 'class_name': cls})
    win32gui.EnumWindows(visit, None)
    if blocked:
        exc = ComputerError('windows_visual_popup_requires_attention')
        exc.content_diagnostic = {'blocked_windows': blocked, 'action': 'none', 'resume': 'after_manual_handoff'}
        raise exc
    if not win32gui.IsWindowEnabled(identity['hwnd']):
        raise ComputerError('windows_visual_popup_requires_attention')
    if require_foreground and win32gui.GetForegroundWindow() != identity['hwnd']:
        raise ComputerError('windows_visual_foreground_required')
    rect = list(win32gui.GetWindowRect(identity['hwnd']))
    if rect[2]-rect[0] < 200 or rect[2]-rect[0] > 4096 or rect[3]-rect[1] < 100:
        raise ComputerError('windows_visual_frame_limit')
    check()
    return identity, kind, rect


def prepare(window, check):
    """Explicit local observation raises only its selected, unblocked window."""
    import win32gui
    identity, _, _ = _guard(window, check, require_foreground=False)
    if win32gui.GetForegroundWindow() != identity['hwnd']:
        win32gui.SetForegroundWindow(identity['hwnd'])
        until = time.monotonic()+1.5
        while win32gui.GetForegroundWindow() != identity['hwnd']:
            check()
            if time.monotonic()>until:
                raise ComputerError('windows_visual_foreground_required')
            time.sleep(.03)
    _guard(window, check)


@contextmanager
def _physical_pixels():
    import ctypes as c
    api = c.WinDLL('user32', use_last_error=True).SetThreadDpiAwarenessContext
    api.argtypes = [c.c_void_p]
    api.restype = c.c_void_p
    previous = api(c.c_void_p(-4))
    if not previous:
        raise ComputerError('windows_visual_dpi_unavailable')
    try:
        yield
    finally:
        api(previous)


def capture(window, check):
    with _physical_pixels():
        return _capture_physical(window, check)


def _capture_physical(window, check, *, full_window=False):
    """Render one HWND: top chrome by default, full window only for opt-in vision."""
    import ctypes
    from ctypes import wintypes as w
    import win32gui, win32ui
    identity, kind, rect = _guard(window, check)
    width, height = rect[2]-rect[0], rect[3]-rect[1] if full_window else min(240, rect[3]-rect[1])
    if height > 4096 or width * height > 12_000_000:
        raise ComputerError('windows_visual_frame_limit')
    dc = win32gui.GetWindowDC(identity['hwnd'])
    source = memory = bitmap = old_bitmap = None
    try:
        source = win32ui.CreateDCFromHandle(dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        old_bitmap = memory.SelectObject(bitmap)
        api = ctypes.windll.user32.PrintWindow
        api.argtypes = [w.HWND,w.HDC,w.UINT]
        api.restype = w.BOOL
        if not api(identity['hwnd'], memory.GetSafeHdc(), 2):
            raise ComputerError('windows_visual_capture_failed')
        raw = bitmap.GetBitmapBits(True)
        # GetBitmapBits(DDB) returns top-down BGRX (same orientation as
        # pywinauto's native capture). A negative BMP height preserves it.
        info = bitmap.GetInfo()
        if info['bmBitsPixel'] != 32 or len(raw) != width*height*4:
            raise ComputerError('windows_visual_capture_failed')
        header = struct.pack('<2sIHHI', b'BM', 54+len(raw),0,0,54)
        header += struct.pack('<IiiHHIIiiII',40,width,-height,1,32,0,len(raw),0,0,0,0)
        data = header + raw
    finally:
        if memory is not None and old_bitmap is not None:
            memory.SelectObject(old_bitmap)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if memory is not None:
            memory.DeleteDC()
        if source is not None:
            source.DeleteDC()
        win32gui.ReleaseDC(identity['hwnd'], dc)
    fresh, _, current = _guard(window, check)
    if current != rect or fresh != identity:
        raise ComputerError('screen_changed_reobserve')
    return data, identity, kind, rect


def ocr_runtime():
    from .windows_ocr import RAPID_RECIPE, WINRT_RECIPE
    local = Path(__file__).resolve().parents[1]/'data/vision_runtime/Scripts/python.exe'
    if local.is_file():
        return RAPID_RECIPE, [(str(local), 'rapidocr')]
    return WINRT_RECIPE, [(x,'winrt') for x in dict.fromkeys(
        [sys.executable, getattr(sys, '_base_executable', sys.executable)])]


def recognize(data, root, check, *, profile='navigation'):
    """Hash-addressed local cache; OCR interpreter is fixed, never model supplied."""
    from .windows_access import BASE
    key = sha256(data).hexdigest()
    recipe, interpreters = ocr_runtime()
    if profile=='forms':
        from .windows_ocr import RAPID_RECIPE, FORM_RECIPE
        if recipe!=RAPID_RECIPE:raise ComputerError('windows_form_ocr_unavailable')
        recipe,interpreters=FORM_RECIPE,[(path,'rapidocr-form') for path,_ in interpreters]
    elif profile!='navigation':raise ComputerError('windows_ocr_profile_invalid')
    cache = BASE/'ocr-cache'/recipe
    cache.mkdir(exist_ok=True, parents=True)
    bmp = cache/(key+'.bmp')
    output = cache/(key+'.json')
    if output.exists():
        frame = read_record(output)
        if (not bmp.is_file() or sha256(bmp.read_bytes()).hexdigest() != key
                or frame.get('sha256') != key or frame.get('recipe') != recipe):
            raise ComputerError('windows_visual_cache_changed')
        return validate_frame(frame)
    if bmp.exists():
        if bmp.read_bytes() != data:
            raise ComputerError('windows_visual_cache_changed')
    else:
        with bmp.open('xb') as handle:
            handle.write(data)
    for interpreter, provider in interpreters:
        check()
        process = subprocess.Popen([interpreter,'-B','-m','agent_service.windows_ocr',str(bmp),provider],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until = time.monotonic()+8
            while True:
                check()
                if time.monotonic()>until:
                    raise ComputerError('windows_ocr_timeout')
                try:
                    raw, _ = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdout.close()
        try:
            if len(raw)>100_000:
                raise ComputerError('windows_ocr_response_invalid')
            payload = json.loads(raw)
        except (ValueError,UnicodeError):
            raise ComputerError('windows_ocr_response_invalid') from None
        if payload.get('ok') is not True:
            if payload.get('code') == 'windows_ocr_unavailable':
                continue
            raise ComputerError(payload.get('code','windows_ocr_unavailable'))
        frame = validate_frame(payload['result'])
        if frame['sha256'] != key or frame.get('recipe') != recipe:
            raise ComputerError('windows_visual_cache_changed')
        write_new(output,frame)
        return frame
    raise ComputerError('windows_ocr_unavailable')


def observe(window, selector, value, root, check):
    decode(selector, value)
    for attempt in range(3):
        pixels, identity, kind, rect = capture(window, check)
        frame = recognize(pixels, root, check)
        latest, fresh, _, current = capture(window, check)
        if latest == pixels and current == rect and fresh == identity:
            break
        if attempt == 2:
            raise ComputerError('screen_changed_reobserve')
    c, details = presentation(frame, selector, value)
    details['rect'] = rect
    return {'window': identity, 'application': kind, 'controls': [c], 'dialogs': [],
            'truncated': False, 'scope': 'visual_navigation', 'visual': details}, {}


def perform(request, root, check, epoch):
    """One approved navigation; refreshed OCR is independent postcondition proof."""
    from .computer import write_new
    from .windows_pointer import guarded_click
    selector, value = request['selector'], request['value']
    before, _ = observe(request['window'], selector, value, root, check)
    if digest(before) != request['before_sha'] or not allowed(before, request['control'], ACTION, value):
        raise ComputerError('screen_changed_reobserve')
    # Re-capture immediately before input; no cached coordinates after a change.
    pixels, _, _, rect = capture(request['window'], check)
    if sha256(pixels).hexdigest() != before['visual']['frame']['sha256'] or rect != before['visual']['rect']:
        raise ComputerError('screen_changed_reobserve')
    box = before['visual']['label_boxes'][0]
    x,y = round(rect[0]+box[0]+box[2]/2), round(rect[1]+box[1]+box[3]/2)
    write_new(root/'native-intent.json', {'request_sha':digest(request),
              'frame_sha256':sha256(pixels).hexdigest(), 'point':[x,y], 'at':time.time()})
    epoch[0] = guarded_click(request['window']['hwnd'],x,y,check=check,
                           should_stop=lambda:(root/'cancel.flag').exists())
    deadline = time.monotonic()+4
    while True:
        after,_ = observe(request['window'],selector,value,root,check)
        if after['visual']['verified']:
            return {'ok':True,'state':'verified','effects':'verified','observation':after}
        if time.monotonic()>deadline:
            return {'ok':False,'state':'unknown','effects':'unknown',
                    'code':'windows_visual_postcondition_failed','observation':after}
        time.sleep(.12)
