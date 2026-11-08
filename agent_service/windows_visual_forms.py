"""Reviewed UIA/OCR form contracts; no arbitrary button or editable surface."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new
from . import windows_visual as visual, windows_visual_memory as memory

BASE = Path(__file__).resolve().parents[1] / 'data/windows_visual_forms'
TYPE, ID, ACTION = 'VisualForm', 'jarvis.visual.form.v1', 'visual_form'
KINDS = {'search_text', 'close_find'}


def is_goal(selector):
    return isinstance(selector, dict) and selector.get('type') == TYPE and selector.get('automation_id') == ID


def plain(value):
    return type(value) is str and len(value) <= 300 and not any(ord(c) < 32 for c in value)


def label(value):
    return re.sub(r'\([A-Za-z]\)$|[:：]$', '', value.replace('&', '')).strip()


def controls(snapshot):
    """Derive candidates only where native role and visible OCR agree."""
    memory._window(snapshot['window']); memory._window(snapshot['dialog'])
    memory._bitmap(snapshot['frame'])
    rect = snapshot['rect']
    if (type(rect) is not list or len(rect) != 4 or any(type(v) is not int for v in rect)
            or [rect[2]-rect[0], rect[3]-rect[1]] != [snapshot['frame']['width'], snapshot['frame']['height']]):
        raise ComputerError('windows_form_geometry_invalid')
    items = snapshot['controls']
    if type(items) is not list or len(items) > 100:
        raise ComputerError('windows_form_controls_invalid')
    def ready(c):
        b = c.get('bounds')
        return (c.get('visible') is True and c.get('enabled') is True and not c.get('password')
                and not c.get('is_password') and c.get('owner_hwnd') == snapshot['dialog']['hwnd']
                and type(b) is list and len(b) == 4 and all(type(x) is int for x in b)
                and rect[0] <= b[0] < b[2] <= rect[2] and rect[1] <= b[1] < b[3] <= rect[3])
    fields = [c for c in items if ready(c) and c.get('type') == 'ComboBox'
              and c.get('name') in {'查找内容(N)', 'Find what:'}]
    closes = [c for c in items if ready(c) and c.get('type') == 'Button'
              and c.get('name') in {'关闭', 'Close'}
              and not any(p.get('type') == 'TitleBar' for p in c.get('ancestors', []))]
    if len(fields) != 1 or len(closes) != 1:
        raise ComputerError('windows_form_controls_ambiguous')
    field, close = fields[0], closes[0]
    ancestor = {k: field[k] for k in ('type', 'name', 'automation_id')}
    edits = [c for c in items if ready(c) and c.get('type') == 'Edit'
             and ancestor in c.get('ancestors', []) and 'set_value' in c.get('patterns', [])]
    if len(edits) != 1 or not plain(edits[0].get('value')):
        raise ComputerError('windows_form_editor_unavailable')
    result = {}
    for kind, c in [('search_text', field), ('close_find', close)]:
        boxes = visual.spans(snapshot['frame'], label(c['name']))
        x1,y1,x2,y2 = [v-rect[i%2] for i,v in enumerate(c['bounds'])]
        if kind == 'close_find':
            boxes = [b for b in boxes if x1 <= b[0] and y1 <= b[1]
                     and b[0]+b[2] <= x2 and b[1]+b[3] <= y2]
        else:
            boxes = [b for b in boxes if b[0]+b[2] <= x1+3 and x1-(b[0]+b[2]) <= 180
                     and abs(b[1]+b[3]/2-(y1+y2)/2) <= max(12,(y2-y1)/2)]
        if len(boxes) != 1:
            raise ComputerError('windows_form_label_unproven')
        b = boxes[0]
        result[kind] = {'control': deepcopy(edits[0] if kind == 'search_text' else c),
                        'label': c['name'], 'label_box': b,
                        'point': [round(rect[0]+b[0]+b[2]/2), round(rect[1]+b[1]+b[3]/2)]}
    return result


def verify_snapshot(snapshot):
    from .windows_visual_binding import verify as verify_document
    if type(snapshot) is not dict or set(snapshot) != {'window','dialog','document','frame','rect','controls','native_proof'}:
        raise ComputerError('windows_form_source_invalid')
    proof = snapshot['native_proof']
    if type(proof) is not dict or set(proof) != {'root','request_sha256','outcome_sha256'}:
        raise ComputerError('windows_form_source_invalid')
    root = safe_root(proof['root'])
    request, outcome = read_record(root/'native-request.json'), read_record(root/'native-outcome.json')
    if (sha256((root/'native-request.json').read_bytes()).hexdigest() != proof['request_sha256']
            or sha256((root/'native-outcome.json').read_bytes()).hexdigest() != proof['outcome_sha256']
            or request.get('op') != 'visual_form_snapshot' or request.get('window') != snapshot['window']
            or outcome.get('ok') is not True
            or outcome.get('snapshot') != {k:v for k,v in snapshot.items() if k != 'native_proof'}):
        raise ComputerError('windows_form_source_changed')
    verify_document(snapshot['document'], require_native=False)
    controls(snapshot)
    return snapshot


def save(snapshot, *, parent=BASE):
    from .windows_task import _save
    verify_snapshot(snapshot)
    root = safe_root(parent)/('form-'+uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    return {'root': str(root), 'sha256': _save(root/'source.json', snapshot)}


def load(reference):
    from .windows_task import _load
    if type(reference) is not dict or set(reference) != {'root','sha256'}:
        raise ComputerError('windows_form_reference_invalid')
    value, checksum = _load(safe_root(reference['root'])/'source.json')
    if checksum != reference['sha256']:
        raise ComputerError('windows_form_source_changed')
    return verify_snapshot(value)


def candidates_for(snapshot, *, parent=BASE):
    ref = save(snapshot, parent=parent)
    return [{'window':deepcopy(snapshot['window']), 'label':c['label'], 'expected':[],
             'source':{'kind':'form','reference':ref,'role':kind}}
            for kind,c in controls(snapshot).items()]


def verify_candidate(item, *, pending=False):
    if type(item) is not dict or set(item) != {'window','label','expected','source'}:
        raise ComputerError('windows_form_candidate_invalid')
    source = item['source']
    if type(source) is not dict or set(source) != {'kind','reference','role'} or source['kind'] != 'form' or source['role'] not in KINDS:
        raise ComputerError('windows_form_candidate_invalid')
    snap = load(source['reference'])
    if snap['window'] != item['window'] or controls(snap)[source['role']]['label'] != item['label']:
        raise ComputerError('windows_form_candidate_changed')
    expected = item['expected']
    if source['role'] == 'search_text':
        if not (pending and expected == []) and not (type(expected) is list and len(expected) == 1 and plain(expected[0])):
            raise ComputerError('windows_form_text_invalid')
    elif expected != []:
        raise ComputerError('windows_form_candidate_invalid')
    return item


def condition(item, *, number=1):
    verify_candidate(item)
    source = item['source']
    value = json.dumps({'source':source['reference'],'kind':source['role'],
                        'text': item['expected'][0] if source['role']=='search_text' else ''},ensure_ascii=False,separators=(',',':'))
    return {'id':f'target_{number}','window':deepcopy(item['window']),
            'selector':{'type':TYPE,'automation_id':ID,'name':item['label']},'state':'value','value':value,'retain':False}


def decode(selector, value):
    try: data=json.loads(value)
    except (TypeError, ValueError): raise ComputerError('windows_form_goal_invalid') from None
    if (not is_goal(selector) or set(selector) != {'type','name','automation_id'}
            or type(data) is not dict or set(data) != {'source','kind','text'} or data['kind'] not in KINDS
            or not plain(data['text']) or (data['kind']=='close_find' and data['text'] != '')):
        raise ComputerError('windows_form_goal_invalid')
    snapshot=load(data['source'])
    if controls(snapshot)[data['kind']]['label'] != selector['name']:
        raise ComputerError('windows_form_goal_invalid')
    return data,snapshot


def review_text(selector,value):
    data,_=decode(selector,value)
    return ('输入 '+repr(data['text'])+'；核验输入框实际回读，不执行查找' if data['kind']=='search_text'
            else '关闭原查找窗口；核验原窗口销毁、文档及文件不变')


def _observation(window, selector, value, check, *, evidence_root=None, receipt='form-observe.json'):
    from . import windows_visual_form_native as native
    data,source=decode(selector,value)
    if source['window'] != window: raise ComputerError('windows_form_window_changed')
    state=native.observe(window,source,data['kind'],data['text'],check)
    if state['dialog_presence']=='hidden':raise ComputerError('windows_form_close_unproven')
    current=state['snapshot']
    live=None
    if current is not None:
        live, original = controls(current), controls(source)
        for kind in KINDS:
            if any(live[kind]['control'][k] != original[kind]['control'][k]
                   for k in ('id','type','name','automation_id')):
                raise ComputerError('windows_form_control_changed')
    met=(state['field_value']==data['text'] if data['kind']=='search_text' else state['dialog_presence']=='destroyed')
    c={'id':digest({'selector':selector,'source':data['source']})[:24],**selector,'visible':True,'enabled':True,
       'value':value if met else '', 'patterns':[ACTION]}
    readback=deepcopy(state)
    if data['kind']=='search_text':
        if current is None or live is None:
            raise ComputerError('windows_form_editor_unavailable')
        # A UIA ValuePattern operation uses native identity/value and current
        # OCR semantics, not a click coordinate. Preserve every native control
        # fact and require a unique OCR match within its native geometry while
        # keeping the measured OCR boxes solely in the independent raw receipt.
        # This avoids caret-only pixel hashes and OCR subpixel box jitter
        # in the task's existing exact observation digest. Pixels remain intact
        # in the independent receipt and are never masked or called semantic.
        readback.pop('snapshot')
        readback['semantic_snapshot']={
            'window':deepcopy(current['window']), 'dialog':deepcopy(current['dialog']),
            'document':deepcopy(current['document']), 'rect':deepcopy(current['rect']),
            'controls':deepcopy(current['controls']),
            'ocr':{'recipe':current['frame'].get('recipe'),
                   'width':current['frame']['width'],'height':current['frame']['height'],
                   'candidates':{kind:{'label':live[kind]['label'],
                        'control':deepcopy(live[kind]['control']),
                        'label_match':'unique_within_native_geometry'} for kind in sorted(KINDS)}}}
    observed={'window':window,'application':'wps','controls':[c],'dialogs':[], 'truncated':False,
              'scope':'visual_form','form':{'selector':selector,'value':value,'readback':readback,'verified':met}}
    if evidence_root is not None:
        write_new(safe_root(evidence_root)/receipt,{'schema':1,'window':window,'selector':selector,
                  'value':value,'observation_sha256':digest(observed),'readback':deepcopy(state)})
    return observed,current


def observation(window, selector, value, check, *, evidence_root=None):
    observed,_=_observation(window,selector,value,check,evidence_root=evidence_root)
    return observed


def actions(observed,goal):
    if observed.get('scope')!='visual_form' or observed.get('form',{}).get('verified'):return []
    readback=observed['form'].get('readback',{})
    if readback.get('snapshot') is None and readback.get('semantic_snapshot') is None:return []
    c=observed['controls'][0]
    if observed['form']['selector'] != goal['selector'] or observed['form']['value'] != goal['value']:return []
    return [{'control_id':c['id'],'action':ACTION,'value':goal['value']}]


def _close_postcondition(window,selector,value,source,root,check):
    """At most three independent reads after one known Close dispatch.

    Revalidation of the immutable source and popup/window gate is outside the
    retry catch, so real source changes or a replacement dialog cannot become
    transient successes. No input action is reachable from this helper.
    """
    from . import windows_visual_form_native as native
    transient={'windows_visual_document_changed','windows_visual_form_dialog_changed',
               'windows_visual_document_identity_unavailable'}
    for number in range(1,4):
        check()
        receipt='form-after.json' if number==1 else f'form-after-{number}.json'
        try:
            return _observation(window,selector,value,check,evidence_root=root,receipt=receipt)
        except ComputerError as exc:
            if str(exc) not in transient:raise
            failure={'schema':1,'attempt':number,'code':str(exc),'source':json.loads(value)['source'],
                     'at':time.time(),'action':'read_only_postcondition'}
            for key in ('content_diagnostic','document_diagnostic'):
                if getattr(exc,key,None) is not None:failure[key]=getattr(exc,key)
            write_new(root/f'form-after-retry-{number}.json',failure)
            if number==3:raise
        # Every source file/proof/bitmap must still match its fixed identity.
        _,current_source=decode(selector,value)
        if current_source!=source:raise ComputerError('windows_form_source_changed')
        _,dialog,_=native._guard(window,check,expected=source['dialog'],allow_closed=True)
        if dialog is not None:raise ComputerError('windows_visual_form_dialog_changed')
        check()
        time.sleep(.1)


def perform(request, root, check, epoch):
    from . import windows_visual_form_native as native
    selector,value=request['selector'],request['value']
    data,source=decode(selector,value)
    before,snap=_observation(request['window'],selector,value,check,evidence_root=root,receipt='form-before.json')
    if digest(before)!=request['before_sha'] or before['controls']!=[request['control']] or before['form']['verified']:
        raise ComputerError('screen_changed_reobserve')
    info=controls(snap)[data['kind']]
    write_new(root/'native-intent.json',{'request_sha':digest(request),'kind':data['kind'],
               'source':data['source'],'frame_sha256':snap['frame']['sha256'],'at':time.time()})
    if data['kind']=='search_text':native.set_field(snap,info['control']['id'],data['text'],check)
    else:native.click_close(snap,info['point'],check,epoch,should_stop=lambda:(root/'cancel.flag').exists())
    if data['kind']=='close_find':
        after,_=_close_postcondition(request['window'],selector,value,source,root,check)
    else:
        after,_=_observation(request['window'],selector,value,check,evidence_root=root,receipt='form-after.json')
    if not after['form']['verified']:
        return {'ok':False,'state':'unknown','effects':'unknown','code':'windows_form_postcondition_failed','observation':after}
    return {'ok':True,'state':'verified','effects':'verified','observation':after}
