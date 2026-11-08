"""Native, bounded control contracts sharing explicit proposal approval."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new

BASE = Path(__file__).resolve().parents[1] / 'data/windows_reviewed_controls'
TYPE, ID, ACTION = 'ReviewedControl', 'jarvis.reviewed.control.v1', 'reviewed_control'
ROLES = {'explorer': {'directory', 'up'}, 'open_dialog': {'filename', 'cancel'},
         'explorer_view': {'search_text', 'preview_pane'}}
FIELDS = {'directory', 'filename', 'search_text'}


def native(kind):
    if kind == 'explorer':
        from . import windows_explorer_native as provider
    elif kind == 'open_dialog':
        from . import windows_open_dialog_native as provider
    elif kind == 'explorer_view':
        from . import windows_explorer_view_native as provider
    else:
        raise ComputerError('windows_reviewed_kind_invalid')
    return provider


def is_goal(selector):
    return isinstance(selector, dict) and selector.get('type') == TYPE and selector.get('automation_id') == ID


def verify_snapshot(snapshot):
    from .windows_visual_memory import _window
    if (type(snapshot) is not dict or set(snapshot) !=
            {'window', 'application', 'kind', 'binding', 'controls', 'native_proof'}):
        raise ComputerError('windows_reviewed_source_invalid')
    _window(snapshot['window'])
    native(snapshot['kind']).validate({k: v for k, v in snapshot.items() if k != 'native_proof'})
    proof = snapshot['native_proof']
    if type(proof) is not dict or set(proof) != {'root', 'request_sha256', 'outcome_sha256'}:
        raise ComputerError('windows_reviewed_source_invalid')
    root = safe_root(proof['root'])
    request, outcome = read_record(root/'native-request.json'), read_record(root/'native-outcome.json')
    if (sha256((root/'native-request.json').read_bytes()).hexdigest() != proof['request_sha256']
            or sha256((root/'native-outcome.json').read_bytes()).hexdigest() != proof['outcome_sha256']
            or request.get('op') != 'reviewed_snapshot' or request.get('window') != snapshot['window']
            or request.get('kind') != snapshot['kind'] or outcome.get('ok') is not True
            or outcome.get('snapshot') != {k: v for k, v in snapshot.items() if k != 'native_proof'}):
        raise ComputerError('windows_reviewed_source_changed')
    roles = [c.get('role') for c in snapshot['controls']]
    if (not roles or len(roles) != len(set(roles)) or any(r not in ROLES[snapshot['kind']] for r in roles)):
        raise ComputerError('windows_reviewed_source_invalid')
    return snapshot


def load(reference):
    from .windows_task import _load
    if type(reference) is not dict or set(reference) != {'root', 'sha256'}:
        raise ComputerError('windows_reviewed_reference_invalid')
    value, checksum = _load(safe_root(reference['root'])/'source.json')
    if checksum != reference['sha256']:
        raise ComputerError('windows_reviewed_source_changed')
    return verify_snapshot(value)


def candidates_for(snapshot, *, parent=BASE):
    from .windows_task import _save
    verify_snapshot(snapshot)
    root = safe_root(parent)/('controls-'+uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    ref = {'root': str(root), 'sha256': _save(root/'source.json', snapshot)}
    return [{'window': deepcopy(snapshot['window']), 'label': c['label'], 'expected': [],
             'source': {'kind': 'reviewed_control', 'reference': ref, 'role': c['role']}}
            for c in snapshot['controls']]


def _text(role, value):
    if (type(value) is not str or not value or len(value) > 300 or any(ord(c) < 32 for c in value)):
        raise ComputerError('windows_reviewed_text_invalid')
    if role == 'directory':
        return str(native('explorer').local_directory(value))
    if role == 'search_text':return native('explorer_view').text(value)
    from .windows_file_dialog import snapshot_file
    return snapshot_file(value)['path']


def verify_candidate(item, *, pending=False):
    if type(item) is not dict or set(item) != {'window', 'label', 'expected', 'source'}:
        raise ComputerError('windows_reviewed_candidate_invalid')
    s = item['source']
    if type(s) is not dict or set(s) != {'kind', 'reference', 'role'} or s['kind'] != 'reviewed_control':
        raise ComputerError('windows_reviewed_candidate_invalid')
    source = load(s['reference'])
    controls = [c for c in source['controls'] if c['role'] == s['role']]
    if len(controls) != 1 or item['label'] != controls[0]['label'] or item['window'] != source['window']:
        raise ComputerError('windows_reviewed_candidate_changed')
    expected = item['expected']
    if s['role'] in FIELDS:
        if not (pending and expected == []):
            if type(expected) is not list or len(expected) != 1:
                raise ComputerError('windows_reviewed_text_invalid')
            _text(s['role'], expected[0])
    elif expected != []:
        raise ComputerError('windows_reviewed_candidate_invalid')
    return item


def condition(item, *, number=1):
    verify_candidate(item)
    s = item['source']
    text = _text(s['role'], item['expected'][0]) if s['role'] in FIELDS else ''
    target = None
    if s['role'] == 'filename':
        from .windows_file_dialog import snapshot_file
        target = snapshot_file(text)
    value = json.dumps({'source': s['reference'], 'role': s['role'], 'text': text,
                        'target_file': target}, ensure_ascii=False, separators=(',', ':'))
    return {'id': f'target_{number}', 'window': deepcopy(item['window']),
            'selector': {'type': TYPE, 'automation_id': ID, 'name': item['label']},
            'state': 'value', 'value': value, 'retain': False}


def decode(selector, value):
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        raise ComputerError('windows_reviewed_goal_invalid') from None
    if (not is_goal(selector) or set(selector) != {'type', 'automation_id', 'name'} or type(data) is not dict
            or set(data) != {'source', 'role', 'text', 'target_file'}):
        raise ComputerError('windows_reviewed_goal_invalid')
    source = load(data['source'])
    controls = [c for c in source['controls'] if c['role'] == data['role']]
    if len(controls) != 1 or controls[0]['label'] != selector['name']:
        raise ComputerError('windows_reviewed_goal_invalid')
    if data['role'] in FIELDS:
        if _text(data['role'], data['text']) != data['text']:
            raise ComputerError('windows_reviewed_text_invalid')
    elif data['text'] != '':
        raise ComputerError('windows_reviewed_goal_invalid')
    if data['role'] == 'filename':
        from .windows_file_dialog import snapshot_file
        if snapshot_file(data['text']) != data['target_file']:
            raise ComputerError('windows_reviewed_target_changed')
    elif data['target_file'] is not None:
        raise ComputerError('windows_reviewed_goal_invalid')
    return data, source


def review_text(selector, value):
    data, source = decode(selector, value)
    if source['kind']=='open_dialog' and source['binding'].get('scope')=='dialog_controls':
        if data['role']=='cancel':
            return '取消原文件选择框；核验原窗口销毁，不代表原文档状态已核验'
        return '文件名输入 '+repr(data['text'])+'；核验原生字段回读，不打开文件，不读取原文档状态'
    if data['role']=='preview_pane':
        return ('关闭' if source['binding']['preview']['visible'] else '打开')+'原窗口的预览面板；核验面板实际显示状态，保持未选中文件'
    return {
        'directory': '地址输入并导航至 '+repr(data['text'])+'；核验实际目录路径',
        'up': '返回原目录的上一级；核验实际目录路径',
        'filename': '文件名输入 '+repr(data['text'])+'；核验字段回读，不打开文件',
        'cancel': '取消原文件选择框；核验原窗口销毁和原文档不变',
        'search_text': '搜索框输入 '+repr(data['text'])+'；输入可能触发Windows自动搜索；核验字段回读，不按回车、不证明搜索结果',
    }[data['role']]


def validate_sequence(conditions):
    by_window = {}
    for condition in conditions:
        if not is_goal(condition['selector']):
            continue
        data, source = decode(condition['selector'], condition['value'])
        key = (source['window']['hwnd'], source['window']['pid'], source['window']['created'])
        previous = by_window.setdefault(key, [])
        if source['kind']=='explorer' and previous:
            raise ComputerError('windows_reviewed_new_observation_required')
        if any(role=='cancel' for role in previous) or data['role'] in previous:
            raise ComputerError('windows_reviewed_sequence_invalid')
        previous.append(data['role'])


def failure_text(code):
    """Explain specialised failures without promising an unsafe replay."""
    if not isinstance(code,str) or not code.startswith(('windows_reviewed_','windows_open_dialog_','windows_explorer_')):
        return None
    if code=='windows_open_dialog_document_unavailable':
        return 'WPS文件选择框正在阻止原生文档核验，本次不生成方案。请在本地取消该窗口后重新观察；原任务和回执保留。'
    if code=='windows_open_dialog_input_busy':
        return 'WPS菜单或鼠标操作尚未结束，已停止自动操作。请在本地收起菜单或完成拖动后继续；已完成步骤不会重复。'
    if 'popup' in code:
        return '出现未批准的弹窗，当前步骤已暂停。请在本地处理弹窗后继续；已完成步骤不会重复。'
    if any(part in code for part in ('changed','invalid','ambiguous','unproven','unavailable','missing','unsupported')):
        return '当前目录、文档或控件无法与原方案唯一核对。请重新观察；原任务和回执保留，不会沿用旧批准操作替换窗口。'
    if 'foreground' in code:
        return '未能确认目标窗口在前台，已停止操作。请保持目标可见后继续未完成步骤。'
    if code.endswith('new_observation_required'):
        return '导航后需重新观察目录，再生成“向上”方案；不会使用导航前的父目录。'
    return '当前控件操作尚未通过独立核验；原记录保留，作用不明的操作不会自动重放。'


def observation(window, selector, value, check, *, evidence_root=None, receipt='reviewed-observe.json'):
    data, source = decode(selector, value)
    if source['window'] != window:
        raise ComputerError('windows_reviewed_window_changed')
    state = native(source['kind']).observe(window, source, data['role'], data['text'], check)
    if type(state) is not dict or type(state.get('verified')) is not bool or type(state.get('facts')) is not dict:
        raise ComputerError('windows_reviewed_observation_invalid')
    control = {'id': digest({'selector': selector, 'source': data['source']})[:24], **selector,
               'visible': True, 'enabled': True, 'value': value if state['verified'] else '', 'patterns': [ACTION]}
    observed = {'window': window, 'application': source['application'], 'controls': [control],
                'dialogs': [], 'truncated': False, 'scope': 'reviewed_control',
                'reviewed': {'selector': selector, 'value': value, **deepcopy(state)}}
    if evidence_root is not None:
        write_new(safe_root(evidence_root)/receipt, {'schema': 1, 'observation': observed})
    return observed


def actions(observed, goal):
    state = observed.get('reviewed', {})
    if (observed.get('scope') != 'reviewed_control' or state.get('verified') is not False
            or state.get('selector') != goal['selector'] or state.get('value') != goal['value']):
        return []
    return [{'control_id': observed['controls'][0]['id'], 'action': ACTION, 'value': goal['value']}]


def perform(request, root, check, epoch):
    selector, value = request['selector'], request['value']
    data, source = decode(selector, value)
    before = observation(request['window'], selector, value, check, evidence_root=root, receipt='reviewed-before.json')
    if (digest(before) != request['before_sha'] or before['controls'] != [request['control']]
            or before['reviewed']['verified']):
        raise ComputerError('screen_changed_reobserve')
    write_new(root/'native-intent.json', {'request_sha': digest(request), 'source': data['source'],
              'role': data['role'], 'at': time.time()})
    native(source['kind']).act(request['window'], source, data['role'], data['text'], check, epoch)
    after = observation(request['window'], selector, value, check, evidence_root=root, receipt='reviewed-after.json')
    if not after['reviewed']['verified']:
        return {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_reviewed_postcondition_failed', 'observation': after}
    return {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': after}
