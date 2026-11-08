"""Bounded, source-backed WPS Find actions for the opt-in Winapp provider.

The CLI can dispatch a single explicitly named UIA InvokePattern. The native
worker owns all window/document/target guards and independent postconditions.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import time
import uuid

from .computer import ComputerError, digest, read_record, safe_root, write_new

TYPE = 'WinappOffice'
ID = 'jarvis.winapp.office.v1'
ROLES = {'next_hit', 'close_find'}
ACTION = {'next_hit': 'office_next_hit', 'close_find': 'office_close_find'}
LABEL = {'next_hit': '查找下一个匹配', 'close_find': '关闭查找窗口'}
BUTTON_NAMES = {'next_hit': {'下一个(F)', 'Find Next'}, 'close_find': {'关闭', 'Close'}}


def is_goal(selector):
    return type(selector) is dict and selector.get('type') == TYPE and selector.get('automation_id') == ID


def _button(controls, dialog, role):
    found = [c for c in controls if c.get('owner_hwnd') == dialog['hwnd'] and c.get('type') == 'Button'
             and c.get('name') in BUTTON_NAMES[role] and c.get('enabled') is True and c.get('visible') is True
             and 'invoke' in c.get('patterns', [])
             and not any(p.get('type') == 'TitleBar' for p in c.get('ancestors', []) if type(p) is dict)]
    if len(found) != 1:
        raise ComputerError('windows_office_button_ambiguous')
    return found[0]


def _field(controls, dialog):
    fields = [c for c in controls if c.get('owner_hwnd') == dialog['hwnd']
              and c.get('type') == 'ComboBox' and c.get('name') in {'查找内容(N)', 'Find what:'}
              and c.get('visible') is True and c.get('enabled') is True]
    if len(fields) != 1 or type(fields[0].get('value')) is not str:
        raise ComputerError('windows_office_find_field_invalid')
    return fields[0]['value']


def _options(controls, dialog):
    def one(kind, name):
        matches = [c for c in controls if c.get('owner_hwnd') == dialog['hwnd']
                   and c.get('type') == kind and c.get('name') == name
                   and c.get('visible') is True and c.get('enabled') is True]
        if len(matches) != 1:
            raise ComputerError('windows_office_options_unavailable')
        return matches[0]
    fields = {'scope': one('ComboBox', '范围(H)').get('value'),
              'order': one('ComboBox', '搜索(S)').get('value'),
              'look': one('ComboBox', '查找范围(L)').get('value'),
              'case': one('CheckBox', '区分大小写(C)').get('checked'),
              'whole': one('CheckBox', '单元格匹配(O)').get('checked'),
              'width': one('CheckBox', '区分全/半角(B)').get('checked')}
    if (fields['scope'] != '工作表' or fields['order'] != '按行' or fields['look'] not in {'智能', '值'}
            or any(type(fields[k]) is not int or fields[k] != 0 for k in ('case', 'whole', 'width'))):
        raise ComputerError('windows_office_options_unsupported')
    return fields


def _next_proof(window, document, query, options, check):
    """Unique literal CJK match across live values/formulas/display in active sheet."""
    from . import windows_file_dialog as files, windows_workbook_model as model
    from .windows_content_find import _get, _cell_location
    from .windows_content_query import scalar_text
    model.ensure_dependencies()
    from openpyxl.utils.cell import get_column_letter
    if not query or len(query) > 100 or any(not '\u4e00' <= c <= '\u9fff' for c in query):
        raise ComputerError('windows_office_query_unsupported')
    book, flags = files.bound_document(window, document['path'], check)
    if flags.get('active') is not True or flags.get('saved') is not True:
        raise ComputerError('windows_office_document_changed')
    raw, _ = model.capture(book.pointer, document['path'], check)
    structure = json.loads(raw)
    app = _get(book.pointer, 'Application')
    active = _cell_location(_get(app, 'ActiveCell'))
    selected = _cell_location(_get(app, 'Selection'))
    sheet = _get(app, 'ActiveSheet')
    name = _get(sheet, 'Name')
    if (active != selected or active.get('path') != document['path'] or active.get('sheet') != name
            or active.get('count') != 1 or active.get('areas') != 1):
        raise ComputerError('windows_office_selection_unsupported')
    sheets = [s for s in structure['sheets'] if s['name'] == name and s['state'] == 'visible']
    if len(sheets) != 1:
        raise ComputerError('windows_office_selection_unsupported')
    rows = sheets[0]['values']
    facts, matches = [], []
    for row_number, row in enumerate(rows, 1):
        for column, _ in enumerate(row, 1):
            check()
            address = f'{get_column_letter(column)}{row_number}'
            cell = _get(sheet, 'Range', address)
            value = scalar_text(_get(cell, 'Value2'))
            formula = scalar_text(_get(cell, 'Formula'))
            displayed = _get(cell, 'Text')
            if displayed is not None and (type(displayed) is not str or len(displayed) > 4000):
                raise ComputerError('windows_office_cell_unsupported')
            if any(x is not None and len(x) > 4000 for x in (value, formula)):
                raise ComputerError('windows_office_cell_unsupported')
            facts.append((address, value, formula, displayed))
            if any(query in x for x in (value, formula, displayed) if x is not None):
                matches.append(address)
    if len(matches) != 1:
        raise ComputerError('windows_office_match_ambiguous')
    address = matches[0]
    letters = ''.join(c for c in address if c.isalpha())
    digits = ''.join(c for c in address if c.isdigit())
    expected = {**active, 'address': f'${letters}${digits}'}
    check()
    return {'options': options, 'before': active, 'expected': expected,
            'model_sha256': sha256(raw).hexdigest(), 'cells_sha256': digest(facts)}


def snapshot(window, check):
    from . import windows_access as access, windows_visual_form_native as native
    parent, dialog, _ = native._guard(window, check, max_dialog_height=720)
    if dialog is None:
        raise ComputerError('windows_office_dialog_missing')
    document = native.document(window, check, expected_dialog=dialog, max_dialog_height=720)
    if document['format'] != 'xlsx':
        raise ComputerError('windows_office_format_unsupported')
    observed, _ = access._observe(window, check, winapp_find=True)
    if observed.get('truncated') or observed.get('blocked_reason') or len(observed.get('dialogs', [])) != 1:
        raise ComputerError('windows_office_observation_invalid')
    actual_dialog = {k: v for k, v in observed['dialogs'][0].items() if k != 'dialog_kind'}
    if actual_dialog != dialog or parent != window:
        raise ComputerError('windows_office_dialog_changed')
    query = _field(observed['controls'], dialog)
    if len(query) > 300 or any(ord(c) < 32 for c in query):
        raise ComputerError('windows_office_query_invalid')
    buttons = {}
    for role in ROLES:
        try:
            buttons[role] = _button(observed['controls'], dialog, role)
        except ComputerError:
            pass
    if native.document(window, check, expected_dialog=dialog, max_dialog_height=720) != document:
        raise ComputerError('windows_office_document_changed')
    next_proof = None
    next_unavailable = 'windows_office_button_unavailable' if 'next_hit' not in buttons else None
    if 'next_hit' in buttons:
        try:
            options = _options(observed['controls'], dialog)
            first_proof = _next_proof(window, document, query, options, check)
            if _next_proof(window, document, query, options, check) != first_proof:
                raise ComputerError('windows_office_find_changed')
            if native.document(window, check, expected_dialog=dialog, max_dialog_height=720) != document:
                raise ComputerError('windows_office_document_changed')
            next_proof = first_proof
        except ComputerError as exc:
            check()
            if str(exc) == 'windows_office_document_changed':
                raise
            code = str(exc)
            next_unavailable = code if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code) else 'windows_office_next_unavailable'
    return {'schema': 1, 'window': deepcopy(window), 'dialog': dialog, 'document': document,
            'query': query, 'buttons': buttons, 'next_proof': next_proof, 'next_unavailable': next_unavailable}


def verify_snapshot(value):
    from .windows_winapp import OFFICE_PROVIDER_BINDING
    if type(value) is not dict or type(value.get('native_proof')) is not dict:
        raise ComputerError('windows_office_proof_invalid')
    proof = value['native_proof']
    if set(proof) != {'root', 'request_sha256', 'outcome_sha256'}:
        raise ComputerError('windows_office_proof_invalid')
    root = safe_root(proof['root'])
    request_path, outcome_path = root / 'native-request.json', root / 'native-outcome.json'
    request, outcome = read_record(request_path), read_record(outcome_path)
    if (sha256(request_path.read_bytes()).hexdigest() != proof['request_sha256']
            or sha256(outcome_path.read_bytes()).hexdigest() != proof['outcome_sha256']
            or request.get('op') != 'office_snapshot' or request.get('provider') != OFFICE_PROVIDER_BINDING
            or request.get('window') != value.get('window') or outcome.get('ok') is not True
            or outcome.get('snapshot') != {k: v for k, v in value.items() if k != 'native_proof'}):
        raise ComputerError('windows_office_proof_changed')
    if (value.get('schema') != 1 or type(value.get('buttons')) is not dict
            or type(value.get('window')) is not dict or type(value.get('dialog')) is not dict
            or type(value.get('document')) is not dict or value['document'].get('format') != 'xlsx'):
        raise ComputerError('windows_office_proof_invalid')
    return deepcopy(value)


def candidates_for(snapshot):
    snapshot = verify_snapshot(snapshot)
    proof = snapshot['native_proof']
    return [{'window': deepcopy(snapshot['window']), 'label': LABEL[role],
             'source': {'kind': 'winapp_office', 'role': role, 'root': proof['root'],
                        'request_sha256': proof['request_sha256'], 'outcome_sha256': proof['outcome_sha256'],
                        'snapshot_sha256': digest({k: v for k, v in snapshot.items() if k != 'native_proof'})}}
            for role in ('next_hit', 'close_find') if role in snapshot['buttons']
            and (role != 'next_hit' or (snapshot.get('next_proof') is not None
                 and snapshot['next_proof']['before'] != snapshot['next_proof']['expected']))]


def verify_candidate(item, *, pending=False):
    if type(item) is not dict or set(item) != {'window', 'label', 'source'} or type(item['source']) is not dict:
        raise ComputerError('windows_office_candidate_invalid')
    source = item['source']
    if (set(source) != {'kind', 'role', 'root', 'request_sha256', 'outcome_sha256', 'snapshot_sha256'}
            or source.get('kind') != 'winapp_office' or source.get('role') not in ROLES
            or item['label'] != LABEL[source['role']]):
        raise ComputerError('windows_office_candidate_invalid')
    root = safe_root(source['root'])
    outcome = read_record(root / 'native-outcome.json')
    raw = outcome.get('snapshot')
    if type(raw) is not dict or digest(raw) != source['snapshot_sha256']:
        raise ComputerError('windows_office_proof_changed')
    snapshot = {**raw, 'native_proof': {k: source[k] for k in ('root', 'request_sha256', 'outcome_sha256')}}
    verify_snapshot(snapshot)
    if item['window'] != snapshot['window'] or source['role'] not in snapshot['buttons']:
        raise ComputerError('windows_office_candidate_invalid')
    if source['role'] == 'next_hit' and (snapshot.get('next_proof') is None
            or snapshot['next_proof']['before'] == snapshot['next_proof']['expected']):
        raise ComputerError('windows_office_candidate_invalid')
    return snapshot


def condition(item, *, number=1):
    verify_candidate(item)
    selector = {'type': TYPE, 'name': LABEL[item['source']['role']], 'automation_id': ID}
    token = json.dumps({'schema': 1, 'candidate': item}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(token) > 4000:
        raise ComputerError('windows_office_token_invalid')
    return {'id': f'target_{number}', 'window': deepcopy(item['window']), 'selector': selector,
            'state': 'value', 'value': token, 'retain': False}


def decode(selector, token):
    if not is_goal(selector) or type(token) is not str or len(token) > 4000:
        raise ComputerError('windows_office_token_invalid')
    try:
        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result: raise ValueError()
                result[k] = v
            return result
        obj = json.loads(token, object_pairs_hook=unique)
        if type(obj) is not dict or set(obj) != {'schema', 'candidate'} or obj['schema'] != 1:
            raise ValueError()
        snapshot = verify_candidate(obj['candidate'])
        if selector != {'type': TYPE, 'name': obj['candidate']['label'], 'automation_id': ID}:
            raise ValueError()
        return obj['candidate'], snapshot
    except (ValueError, TypeError, KeyError):
        raise ComputerError('windows_office_token_invalid') from None


def observe(window, selector, token, check):
    from . import windows_visual_form_native as native
    candidate, source = decode(selector, token)
    if window != source['window']:
        raise ComputerError('windows_office_window_changed')
    state = native.closed(source, check, max_dialog_height=720)
    if state['dialog_presence'] == 'hidden':
        raise ComputerError('windows_office_dialog_hidden')
    role = candidate['source']['role']
    readback = {'document': deepcopy(state['document']), 'dialog_presence': state['dialog_presence']}
    if role == 'close_find':
        verified = state['dialog_presence'] == 'destroyed'
    else:
        if state['dialog_presence'] != 'visible':
            raise ComputerError('windows_office_dialog_changed')
        current = snapshot(window, check)
        original, actual = source['next_proof'], current['next_proof']
        if (actual is None or current['dialog'] != source['dialog'] or current['document'] != source['document']
                or current['query'] != source['query']
                or any(actual[k] != original[k] for k in ('options', 'expected', 'model_sha256', 'cells_sha256'))):
            raise ComputerError('windows_office_find_changed')
        if actual['before'] not in (original['before'], original['expected']):
            raise ComputerError('windows_office_selection_changed')
        verified = actual['before'] == original['expected']
        # _next_proof's before is the actual current, independently matched
        # ActiveCell and Selection; it is not the original source selection.
        readback.update(active_cell=deepcopy(actual['before']), selection=deepcopy(actual['before']),
                        expected_cell=deepcopy(original['expected']), options=deepcopy(actual['options']),
                        model_sha256=actual['model_sha256'], cells_sha256=actual['cells_sha256'])
    control = {'id': 'winapp-office-' + role, **selector, 'enabled': True, 'visible': True,
               'value': token if verified else '', 'patterns': [ACTION[role]]}
    return {'window': deepcopy(window), 'controls': [control], 'application': 'wps',
            'scope': 'winapp_office', 'truncated': False, 'dialogs': [],
            'office': {'role': role, 'verified': verified, 'dialog_presence': state['dialog_presence'],
                       'readback': readback}}


def candidates(observation, condition):
    try:
        item, _ = decode(condition['selector'], condition['value'])
        if observation['office']['verified'] is False and observation['office']['role'] == item['source']['role']:
            return [{'control_id': observation['controls'][0]['id'],
                     'action': ACTION[item['source']['role']], 'value': condition['value']}]
    except (ComputerError, KeyError, TypeError):
        pass
    return []


def _close_after(request, source, root, check):
    """Retry only transient reads after one already-journaled Close invoke."""
    from . import windows_visual_form_native as native
    transient = {'windows_visual_document_identity_unavailable', 'windows_visual_document_changed',
                 'windows_visual_form_dialog_changed'}
    for number in range(1, 4):
        check()
        try:
            return observe(request['window'], request['selector'], request['value'], check)
        except ComputerError as exc:
            if str(exc) not in transient:
                raise
            import win32gui
            decode(request['selector'], request['value'])
            _, dialog, _ = native._guard(request['window'], check, expected=source['dialog'],
                                         allow_closed=True, max_dialog_height=720)
            if dialog is not None or win32gui.IsWindow(source['dialog']['hwnd']):
                raise
            write_new(root / f'office-read-retry-{number}.json', {'schema': 1, 'attempt': number,
                      'code': str(exc), 'action': 'read_only_postcondition'})
            if number == 3:
                raise
            check()
            time.sleep(.1)


def perform(request, root, check, epoch):
    from . import windows_access as access, windows_visual_form_native as native
    from .windows_winapp import WinAppReader
    item, source = decode(request['selector'], request['value'])
    role = item['source']['role']
    if request['action'] != ACTION[role]:
        raise ComputerError('windows_office_action_denied')
    before = observe(request['window'], request['selector'], request['value'], check)
    if digest(before) != request['before_sha'] or before['controls'] != [request['control']] or before['office']['verified']:
        raise ComputerError('screen_changed_reobserve')
    _, dialog, _ = native._guard(request['window'], check, expected=source['dialog'], max_dialog_height=720)
    observed, wrappers = access._observe(request['window'], check, winapp_find=True)
    if observed.get('truncated') or observed.get('blocked_reason'):
        raise ComputerError('windows_office_observation_invalid')
    control = _button(observed['controls'], dialog, role)
    approved = source['buttons'][role]
    if any(control.get(k) != approved.get(k) for k in ('id', 'runtime_id', 'type', 'name', 'automation_id', 'owner_hwnd')):
        raise ComputerError('screen_changed_reobserve')
    wrapper = wrappers[control['id']]
    # Pattern existence is independently checked immediately before binding.
    wrapper.iface_invoke
    reader = WinAppReader(root / 'winapp', timeout=5)
    element = reader.bind(dialog['hwnd'], control, wrapper, action='invoke')
    fresh = access._native_record(wrapper, dialog['pid'])
    if fresh is None or any(fresh.get(k) != v for k, v in control.items() if k not in {'focused', 'ancestors', 'owner_hwnd'}):
        raise ComputerError('screen_changed_reobserve')
    native._guard(request['window'], check, expected=source['dialog'], max_dialog_height=720)
    if native.document(request['window'], check, expected_dialog=source['dialog'], max_dialog_height=720) != source['document']:
        raise ComputerError('windows_office_document_changed')
    if observe(request['window'], request['selector'], request['value'], check) != before:
        raise ComputerError('screen_changed_reobserve')
    fresh = access._native_record(wrapper, dialog['pid'])
    if fresh is None or any(fresh.get(k) != v for k, v in control.items() if k not in {'focused', 'ancestors', 'owner_hwnd'}):
        raise ComputerError('screen_changed_reobserve')
    native._guard(request['window'], check, expected=source['dialog'], max_dialog_height=720)
    check()
    write_new(root / 'native-intent.json', {'request_sha': digest(request), 'role': role,
              'route': 'winapp-strict-invoke-close-v1' if role == 'close_find' else 'winapp-strict-invoke-next-v1', 'at': time.time()})
    reader.perform_bound(dialog['hwnd'], element, 'invoke', '', office=True)
    deadline = time.monotonic() + 3
    while True:
        check()
        after = (_close_after(request, source, root, check) if role == 'close_find'
                 else observe(request['window'], request['selector'], request['value'], check))
        if after['office']['verified']:
            write_new(root / 'office-after.json', {'schema': 1, 'observation': after})
            return {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': after}
        if time.monotonic() >= deadline:
            return {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'windows_office_postcondition_failed'}
        time.sleep(.1)


def _close_recheck_source(root):
    """Validate the last unknown task and its one original CLI dispatch."""
    from . import windows_task as task
    from .windows_visual_proposal import approved
    from .windows_winapp import OFFICE_PROVIDER_BINDING, EXE, _native_slug
    scan = task._scan(root)
    task._verify_sources(scan)
    goal, outcome = scan['goal'], scan['last_outcome']
    if (not scan['uncertain'] or not outcome or outcome.get('state') != 'unknown'
            or scan['task'].get('provider_binding') != OFFICE_PROVIDER_BINDING
            or scan['task'].get('schema') != 3 or not approved(root, scan)
            or len(goal['conditions']) != 1 or scan['completed'] or scan['actions'] != 1
            or not scan['attempts']):
        raise ComputerError('windows_office_recheck_ineligible')
    condition = goal['conditions'][0]
    item, source = decode(condition['selector'], condition['value'])
    if item['source']['role'] != 'close_find' or condition['retain']:
        raise ComputerError('windows_office_recheck_ineligible')
    attempt = root / 'attempts' / f'{scan["attempts"]:04}'
    events = [task._load(path)[0] for path in sorted((attempt / 'events').iterdir())]
    if len(events) < 3:
        raise ComputerError('windows_office_recheck_ineligible')
    observed, intent, result = events[-3:]
    candidate = intent.get('candidate', {})
    if (observed.get('kind') != 'observation' or intent.get('kind') != 'intent' or result.get('kind') != 'result'
            or any(e.get('condition_id') != condition['id'] for e in (observed, intent, result))
            or any(e.get('purpose') for e in (intent, result)) or candidate.get('action') != ACTION['close_find']
            or candidate.get('value') != condition['value'] or result.get('result', {}).get('state') != 'unknown'
            or result.get('result', {}).get('effects') != 'unknown'):
        raise ComputerError('windows_office_recheck_ineligible')
    operation = attempt / 'operations' / '0001'
    try:
        request = read_record(operation / 'native-request.json')
        native_intent = read_record(operation / 'native-intent.json')
        native = read_record(operation / 'native-outcome.json')
        native_result = {k: v for k, v in native.items() if k != 'input_epoch'}
        if (request.get('op') != 'act' or request.get('provider') != OFFICE_PROVIDER_BINDING
                or request.get('action') != ACTION['close_find'] or request.get('window') != condition['window']
                or request.get('selector') != condition['selector'] or request.get('value') != condition['value']
                or request.get('document_token') != condition['value'] or request.get('before_sha') != intent['before_sha256']
                or request.get('control', {}).get('id') != candidate.get('control_id')
                or native_intent.get('request_sha') != digest(request) or native_intent.get('role') != 'close_find'
                or native_intent.get('route') != 'winapp-strict-invoke-close-v1'
                or native_result != result['result'] or native.get('ok') is not False):
            raise ComputerError('windows_office_recheck_ineligible')
        calls = []
        for folder in (operation / 'winapp').iterdir():
            command = read_record(folder / 'command.json')
            args = command.get('argv')
            if type(args) is list and len(args) > 2 and args[1:3] == ['ui', 'invoke']:
                calls.append((folder, args))
        slug = _native_slug(source['buttons']['close_find'])
        expected = [str(EXE), 'ui', 'invoke', slug, '--window', str(source['dialog']['hwnd']),
                    '--action', 'invoke', '--json']
        if len(calls) != 1 or calls[0][1] != expected:
            raise ComputerError('windows_office_recheck_ineligible')
        call = calls[0][0]
        stdout, status = read_record(call / 'stdout.json'), read_record(call / 'result.json')
        if (status.get('status') != 'ok' or status.get('exit_code') != 0
                or stdout.get('elementId') != slug or stdout.get('hwnd') != source['dialog']['hwnd']
                or stdout.get('requestedAction') != 'invoke' or stdout.get('performedAction') != 'invoke'
                or stdout.get('pattern') != 'InvokePattern'):
            raise ComputerError('windows_office_recheck_ineligible')
    except (OSError, TypeError, ValueError, KeyError):
        raise ComputerError('windows_office_recheck_ineligible') from None
    return {'task_root': str(root), 'task_sha256': scan['task_sha256'], 'task_inventory_sha256': task._inventory(root),
            'attempt': scan['attempts'], 'operation': operation.relative_to(root).as_posix(),
            'condition': deepcopy(condition), 'source_document': deepcopy(source['document']),
            'source_dialog': deepcopy(source['dialog']), 'original_state': 'unknown'}


def _close_recheck_observation(condition, source, observation):
    from . import windows_task as task
    observed = task._observation(observation, condition['window'])
    info = observed.get('office', {})
    facts = info.get('readback', {})
    if (observed.get('scope') != 'winapp_office' or info.get('role') != 'close_find'
            or info.get('verified') is not True or info.get('dialog_presence') != 'destroyed'
            or facts.get('dialog_presence') != 'destroyed' or facts.get('document') != source['source_document']
            or not task.satisfied(observed, condition)):
        raise ComputerError('windows_office_recheck_unverified')
    return observed


def recheck_close(task_root, backend=None, *, stop=lambda: False):
    """Read once through a new production worker; never alter or run the task."""
    from . import windows_task as task, windows_access as access
    from .windows_winapp import OFFICE_PROVIDER_BINDING
    root = safe_root(task_root)
    if backend is not None and type(backend) is not access.WinappOfficeBackend:
        raise ComputerError('windows_office_recheck_backend_invalid')
    if backend is None:
        backend = access.WinappOfficeBackend()
    if backend.provider_binding != OFFICE_PROVIDER_BINDING:
        raise ComputerError('windows_office_recheck_backend_invalid')
    # Ignore caller-supplied output roots: original tasks, stages and source
    # proofs are immutable. Always create a fresh production observation.
    backend = access.WinappOfficeBackend(parent=access.BASE, seconds=backend.seconds)
    with task._own(root), task._stage_guard(root):
        source = _close_recheck_source(root)
        session = safe_root(task._load(root / 'task.json')[0]['stage_binding']['root'])
        parent = safe_root(access.BASE)
        if parent == root or root in parent.parents or parent == session or session in parent.parents:
            raise ComputerError('windows_office_recheck_destination_invalid')
        if stop():
            raise ComputerError('windows_office_recheck_cancelled')
        condition = source['condition']
        operation = safe_root(backend.parent / ('observe-' + uuid.uuid4().hex))
        request = {'op': 'observe', 'window': condition['window'], 'selector': condition['selector'],
                   'document_token': condition['value'], 'provider': OFFICE_PROVIDER_BINDING}
        # Call the concrete implementation, not an injected backend override.
        result = access.WindowsBackend._call(backend, request, stop=stop, root=operation)
        if not result.get('ok'):
            raise ComputerError(result.get('code', 'windows_office_recheck_failed'))
        observation = _close_recheck_observation(condition, source, result.get('observation'))
        if stop() or _close_recheck_source(root) != source:
            raise ComputerError('windows_office_recheck_source_changed')
        proof = {'root': str(operation), 'request_sha256': sha256((operation / 'native-request.json').read_bytes()).hexdigest(),
                 'outcome_sha256': sha256((operation / 'native-outcome.json').read_bytes()).hexdigest(),
                 'inventory_sha256': task._inventory(operation)}
        return {'schema': 1, 'kind': 'office_close_current_readback', 'source': source,
                'native_proof': proof, 'observation': observation, 'actions': [],
                'original_state': 'unknown', 'resumable': False}


def verify_close_recheck(payload, task_root):
    """Verify independent native evidence and the unchanged unknown source."""
    from . import windows_task as task
    root = safe_root(task_root)
    with task._own(root), task._stage_guard(root):
        scan = task._scan(root)
        binding = scan['task'].get('stage_binding')
        if type(binding) is not dict:
            raise ComputerError('windows_office_recheck_invalid')
        return verify_close_recheck_locked(payload, root, scan=scan, expected_stage_root=binding['root'])


def verify_close_recheck_locked(payload, task_root, *, scan, expected_stage_root):
    """Verify with the caller holding the session, ancestor and child leases.

    The session owner must independently validate stage order/approval and hold
    every lease, as windows_stages._records does. No locks are acquired here.
    """
    from . import windows_task as task
    from .windows_winapp import OFFICE_PROVIDER_BINDING
    root = safe_root(task_root)
    if (type(payload) is not dict or set(payload) != {'schema', 'kind', 'source', 'native_proof', 'observation',
            'actions', 'original_state', 'resumable'} or payload['schema'] != 1
            or payload['kind'] != 'office_close_current_readback' or payload['actions'] != []
            or payload['original_state'] != 'unknown' or payload['resumable'] is not False):
        raise ComputerError('windows_office_recheck_invalid')
    binding = scan.get('task', {}).get('stage_binding') if type(scan) is dict else None
    if (type(binding) is not dict or set(binding) != {'root', 'number'}
            or type(binding['number']) is not int or binding['number'] < 1
            or safe_root(binding['root']) != safe_root(expected_stage_root) or task._scan(root) != scan):
        raise ComputerError('windows_office_recheck_source_changed')
    source = _close_recheck_source(root)
    if payload['source'] != source:
        raise ComputerError('windows_office_recheck_source_changed')
    proof = payload['native_proof']
    if type(proof) is not dict or set(proof) != {'root', 'request_sha256', 'outcome_sha256', 'inventory_sha256'}:
        raise ComputerError('windows_office_recheck_invalid')
    operation = safe_root(proof['root'])
    request_path, outcome_path = operation / 'native-request.json', operation / 'native-outcome.json'
    request, outcome = read_record(request_path), read_record(outcome_path)
    condition = source['condition']
    if (sha256(request_path.read_bytes()).hexdigest() != proof['request_sha256']
            or sha256(outcome_path.read_bytes()).hexdigest() != proof['outcome_sha256']
            or task._inventory(operation) != proof['inventory_sha256'] or (operation / 'native-intent.json').exists()
            or request != {'op': 'observe', 'window': condition['window'], 'selector': condition['selector'],
                           'document_token': condition['value'], 'provider': OFFICE_PROVIDER_BINDING}
            or outcome.get('ok') is not True or outcome.get('observation') != payload['observation']):
        raise ComputerError('windows_office_recheck_invalid')
    _close_recheck_observation(condition, source, payload['observation'])
    # Stage/task leases do not lock external source and observation directories.
    if (task._scan(root) != scan or _close_recheck_source(root) != source
            or sha256(request_path.read_bytes()).hexdigest() != proof['request_sha256']
            or sha256(outcome_path.read_bytes()).hexdigest() != proof['outcome_sha256']
            or task._inventory(operation) != proof['inventory_sha256']):
        raise ComputerError('windows_office_recheck_source_changed')
    return deepcopy(payload)
