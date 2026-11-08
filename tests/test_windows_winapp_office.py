from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from agent_service import windows_winapp_office as office, windows_access as access, windows_task as task
from agent_service.computer import ComputerError, digest, write_new
from agent_service.windows_winapp import PROVIDER_BINDING, OFFICE_PROVIDER_BINDING


WINDOW = {'hwnd': 10, 'pid': 20, 'created': 'time', 'image': 'C:/WPS/wps.exe',
          'class_name': 'XLMAIN', 'title': 'example.xlsx', 'profile': 'observe_only'}


def source_snapshot(tmp_path, *, next_proof=None):
    raw = {'schema': 1, 'window': WINDOW, 'dialog': {'hwnd': 30},
           'document': {'format': 'xlsx'}, 'query': '纸张',
           'buttons': {'close_find': {'id': 'close'}, 'next_hit': {'id': 'next'}},
           'next_proof': next_proof}
    root = tmp_path / 'worker'
    root.mkdir()
    request = {'op': 'office_snapshot', 'window': WINDOW, 'provider': OFFICE_PROVIDER_BINDING}
    write_new(root / 'native-request.json', request)
    write_new(root / 'native-outcome.json', {'ok': True, 'snapshot': raw})
    return {**raw, 'native_proof': {'root': str(root),
        'request_sha256': sha256((root / 'native-request.json').read_bytes()).hexdigest(),
        'outcome_sha256': sha256((root / 'native-outcome.json').read_bytes()).hexdigest()}}


def test_snapshot_proof_and_v1_does_not_get_office_capabilities(tmp_path):
    snapshot = source_snapshot(tmp_path)
    candidates = office.candidates_for(snapshot)
    assert [c['source']['role'] for c in candidates] == ['close_find']
    condition = office.condition(candidates[0])
    assert office.decode(condition['selector'], condition['value'])[0] == candidates[0]
    with pytest.raises(ComputerError, match='windows_office_stage_required'):
        task.create(tmp_path, {'schema': 1, 'instruction': 'close', 'conditions': [condition], 'max_steps': 1},
                    provider_binding=PROVIDER_BINDING)
    assert type(access.backend_for_binding(PROVIDER_BINDING)) is access.WinappBackend
    assert type(access.backend_for_binding(OFFICE_PROVIDER_BINDING)) is access.WinappOfficeBackend
    root = tmp_path / 'worker' / 'native-outcome.json'
    root.write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError):
        office.decode(condition['selector'], condition['value'])


def test_next_requires_distinct_proved_target(tmp_path):
    proof = {'before': {'address': '$A$1'}, 'expected': {'address': '$A$1'}}
    snapshot = source_snapshot(tmp_path, next_proof=proof)
    assert [c['source']['role'] for c in office.candidates_for(snapshot)] == ['close_find']


def test_options_require_all_explicit_values_and_checkbox_states():
    dialog = {'hwnd': 30}
    controls = []
    for kind, name, value, checked in [
        ('ComboBox', '范围(H)', '工作表', None), ('ComboBox', '搜索(S)', '按行', None),
        ('ComboBox', '查找范围(L)', '智能', None), ('CheckBox', '区分大小写(C)', '', 0),
        ('CheckBox', '单元格匹配(O)', '', 0), ('CheckBox', '区分全/半角(B)', '', 0)]:
        controls.append({'type': kind, 'name': name, 'value': value, 'checked': checked,
                         'owner_hwnd': 30, 'visible': True, 'enabled': True})
    assert office._options(controls, dialog)['look'] == '智能'
    controls[-1]['checked'] = None
    with pytest.raises(ComputerError, match='windows_office_options_unsupported'):
        office._options(controls, dialog)
    with pytest.raises(ComputerError, match='windows_office_options_unavailable'):
        office._options(controls[:-1], dialog)


@pytest.mark.parametrize('second', ['changed', 'failed', 'document_changed'])
def test_unstable_next_proof_never_becomes_candidate(monkeypatch, second):
    from agent_service import windows_visual_form_native as native
    dialog = {'hwnd': 30, 'pid': 20}
    document = {'format': 'xlsx'}
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (WINDOW, dialog, None))
    reads = []
    def get_document(*args, **kwargs):
        reads.append(1)
        return {'format': 'xlsx', 'changed': True} if second == 'document_changed' and len(reads) >= 3 else document
    monkeypatch.setattr(native, 'document', get_document)
    monkeypatch.setattr(access, '_observe', lambda *args, **kwargs: ({'truncated': False, 'controls': [],
        'dialogs': [{**dialog, 'dialog_kind': 'find'}]}, {}))
    monkeypatch.setattr(office, '_field', lambda *args: '纸张')
    monkeypatch.setattr(office, '_button', lambda *args: {'id': args[-1]})
    monkeypatch.setattr(office, '_options', lambda *args: {'scope': '工作表'})
    calls = []
    def proof(*args):
        calls.append(1)
        if len(calls) == 2 and second == 'failed':
            raise ComputerError('windows_office_find_changed')
        return {'before': 1, 'expected': 2 if len(calls) == 1 or second == 'document_changed' else 3}
    monkeypatch.setattr(office, '_next_proof', proof)
    if second == 'document_changed':
        with pytest.raises(ComputerError, match='windows_office_document_changed'):
            office.snapshot(WINDOW, lambda: None)
    else:
        assert office.snapshot(WINDOW, lambda: None)['next_proof'] is None


def test_next_proof_requires_unique_live_cell_distinct_from_selection(monkeypatch):
    import json
    from agent_service import windows_content_find, windows_file_dialog, windows_workbook_model
    document = {'path': 'C:/fixture.xlsx'}
    selection = {'path': document['path'], 'sheet': 'Sheet1', 'address': '$A$1', 'count': 1, 'areas': 1}
    cells = {'A1': ('other', 'other', 'other'), 'B1': ('纸张', '纸张', '纸张')}
    dependencies = []
    monkeypatch.setattr(windows_workbook_model, 'ensure_dependencies', lambda: dependencies.append('loaded'))
    monkeypatch.setattr(windows_file_dialog, 'bound_document', lambda *args:
                        (SimpleNamespace(pointer='book'), {'active': True, 'saved': True}))
    raw = json.dumps({'sheets': [{'name': 'Sheet1', 'state': 'visible', 'values': [[None, None]]}]}).encode()
    monkeypatch.setattr(windows_workbook_model, 'capture', lambda *args: (raw, {}))
    monkeypatch.setattr(windows_content_find, '_cell_location', lambda obj: deepcopy(selection))
    def get(obj, name, *args):
        table = {('book', 'Application'): 'app', ('app', 'ActiveCell'): 'active',
                 ('app', 'Selection'): 'selection', ('app', 'ActiveSheet'): 'sheet', ('sheet', 'Name'): 'Sheet1'}
        if (obj, name) in table: return table[(obj, name)]
        if obj == 'sheet' and name == 'Range': return args[0]
        if obj in cells: return cells[obj][{'Value2': 0, 'Formula': 1, 'Text': 2}[name]]
        raise AssertionError((obj, name))
    monkeypatch.setattr(windows_content_find, '_get', get)
    proof = office._next_proof(WINDOW, document, '纸张', {'scope': '工作表'}, lambda: None)
    assert dependencies == ['loaded']
    assert proof['before']['address'] == '$A$1' and proof['expected']['address'] == '$B$1'
    cells['A1'] = ('纸张', '纸张', '纸张')
    with pytest.raises(ComputerError, match='windows_office_match_ambiguous'):
        office._next_proof(WINDOW, document, '纸张', {'scope': '工作表'}, lambda: None)
    with pytest.raises(ComputerError, match='windows_office_query_unsupported'):
        office._next_proof(WINDOW, document, '*', {'scope': '工作表'}, lambda: None)


def test_next_observe_rejects_changed_options_or_unexpected_cell(monkeypatch):
    from agent_service import windows_visual_form_native as native
    proof = {'before': {'address': '$A$1'}, 'expected': {'address': '$B$1'},
             'options': {'scope': '工作表'}, 'model_sha256': 'model', 'cells_sha256': 'cells'}
    source = {'window': WINDOW, 'dialog': {'hwnd': 30}, 'document': {'format': 'xlsx'},
              'query': '纸张', 'next_proof': proof}
    item = {'source': {'role': 'next_hit'}}
    current = deepcopy(source)
    monkeypatch.setattr(office, 'decode', lambda *args: (item, source))
    monkeypatch.setattr(native, 'closed', lambda *args, **kwargs:
                        {'dialog_presence': 'visible', 'document': deepcopy(source['document'])})
    monkeypatch.setattr(office, 'snapshot', lambda *args: current)
    selector = {'type': office.TYPE, 'name': office.LABEL['next_hit'], 'automation_id': office.ID}
    assert not office.observe(WINDOW, selector, 'token', lambda: None)['office']['verified']
    current['next_proof']['before'] = {'address': '$B$1'}
    after = office.observe(WINDOW, selector, 'token', lambda: None)['office']
    assert after['verified']
    assert after['readback']['active_cell'] == {'address': '$B$1'}
    assert after['readback']['selection'] == {'address': '$B$1'}
    assert after['readback']['document'] == source['document']
    current['next_proof']['before'] = {'address': '$C$1'}
    with pytest.raises(ComputerError, match='windows_office_selection_changed'):
        office.observe(WINDOW, selector, 'token', lambda: None)
    current['next_proof']['options'] = {'scope': '工作簿'}
    with pytest.raises(ComputerError, match='windows_office_find_changed'):
        office.observe(WINDOW, selector, 'token', lambda: None)


def test_close_readback_records_destroyed_dialog_and_document(monkeypatch):
    from agent_service import windows_visual_form_native as native
    source = {'window': WINDOW, 'document': {'path': 'C:/fixture.xlsx', 'sha256': 'saved'}}
    monkeypatch.setattr(office, 'decode', lambda *args: ({'source': {'role': 'close_find'}}, source))
    monkeypatch.setattr(native, 'closed', lambda *args, **kwargs:
                        {'dialog_presence': 'destroyed', 'document': deepcopy(source['document'])})
    selector = {'type': office.TYPE, 'name': office.LABEL['close_find'], 'automation_id': office.ID}
    facts = office.observe(WINDOW, selector, 'token', lambda: None)['office']
    assert facts['verified'] and facts['readback'] == {'dialog_presence': 'destroyed',
                                                        'document': source['document']}


def test_old_verified_office_receipt_still_loads_without_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(office, 'decode', lambda *args: ({'source': {'role': 'next_hit'}}, {}))
    selector = {'type': office.TYPE, 'name': office.LABEL['next_hit'], 'automation_id': office.ID}
    condition = {'window': WINDOW, 'selector': selector, 'state': 'value', 'value': 'token'}
    observed = {'window': WINDOW, 'controls': [{'id': 'office', **selector, 'visible': True,
                 'enabled': True, 'value': 'token'}], 'truncated': False, 'scope': 'winapp_office',
                'office': {'role': 'next_hit', 'verified': True, 'dialog_presence': 'visible'}}
    write_new(tmp_path / 'office-after.json', {'schema': 1, 'observation': observed})
    result = {'ok': True, 'state': 'verified', 'effects': 'verified', 'observation': observed}
    assert task._office_completion(condition, result, tmp_path) == observed


def test_close_once_intent_precedes_dispatch_and_postcondition(tmp_path, monkeypatch):
    from agent_service import windows_visual_form_native as native, windows_winapp
    control = {'id': 'x', 'runtime_id': [1, 2], 'type': 'Button', 'name': '关闭', 'automation_id': '',
               'owner_hwnd': 30, 'enabled': True, 'visible': True, 'patterns': ['invoke'], 'ancestors': []}
    source = {'window': WINDOW, 'dialog': {'hwnd': 30, 'pid': 20}, 'document': {'format': 'xlsx'},
              'buttons': {'close_find': control}}
    item = {'source': {'role': 'close_find'}}
    monkeypatch.setattr(office, 'decode', lambda *args: (item, source))
    state = {'dispatched': 0}
    selector = {'type': office.TYPE, 'name': office.LABEL['close_find'], 'automation_id': office.ID}
    def observe(window, selector, token, check):
        yes = bool(state['dispatched'])
        return {'window': window, 'controls': [{'id': 'office', **selector, 'visible': True,
            'enabled': True, 'value': token if yes else ''}], 'truncated': False,
            'scope': 'winapp_office', 'office': {'role': 'close_find', 'verified': yes}}
    monkeypatch.setattr(office, 'observe', observe)
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (WINDOW, source['dialog'], None))
    monkeypatch.setattr(native, 'document', lambda *args, **kwargs: source['document'])
    wrapper = SimpleNamespace(iface_invoke=object())
    monkeypatch.setattr(access, '_observe', lambda *args, **kwargs:
                        ({'controls': [control], 'truncated': False}, {'x': wrapper}))
    monkeypatch.setattr(access, '_native_record', lambda *args: {k: v for k, v in control.items()
                        if k not in {'ancestors', 'owner_hwnd'}})
    class Reader:
        def __init__(self, *args, **kwargs): pass
        def bind(self, *args, **kwargs): return {'selector': 'btn-x-1234'}
        def perform_bound(self, *args, **kwargs):
            assert (tmp_path / 'native-intent.json').is_file()
            state['dispatched'] += 1
    monkeypatch.setattr(windows_winapp, 'WinAppReader', Reader)
    before = observe(WINDOW, selector, 'token', lambda: None)
    request = {'window': WINDOW, 'selector': selector, 'value': 'token', 'control': before['controls'][0],
               'before_sha': digest(before), 'action': 'office_close_find'}
    result = office.perform(request, tmp_path, lambda: None, [None])
    assert result['ok'] and state['dispatched'] == 1
    assert (tmp_path / 'office-after.json').is_file()


def test_close_postread_retries_only_after_destroyed_dialog(tmp_path, monkeypatch):
    import sys
    from agent_service import windows_visual_form_native as native
    source = {'dialog': {'hwnd': 30}}
    request = {'window': WINDOW, 'selector': {}, 'value': 'token'}
    calls = []
    def read(*args):
        calls.append('read')
        if len(calls) == 1:
            raise ComputerError('windows_visual_document_identity_unavailable')
        return {'office': {'verified': True}}
    monkeypatch.setattr(office, 'observe', read)
    monkeypatch.setattr(office, 'decode', lambda *args: None)
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (WINDOW, None, None))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(IsWindow=lambda hwnd: False))
    assert office._close_after(request, source, tmp_path, lambda: None)['office']['verified']
    assert calls == ['read', 'read'] and (tmp_path / 'office-read-retry-1.json').exists()


def test_close_postread_never_retries_hidden_or_visible_dialog(tmp_path, monkeypatch):
    import sys
    from agent_service import windows_visual_form_native as native
    calls = []
    def read(*args):
        calls.append(1)
        raise ComputerError('windows_visual_document_identity_unavailable')
    monkeypatch.setattr(office, 'observe', read)
    monkeypatch.setattr(office, 'decode', lambda *args: None)
    monkeypatch.setattr(native, '_guard', lambda *args, **kwargs: (WINDOW, None, None))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(IsWindow=lambda hwnd: True))
    with pytest.raises(ComputerError, match='windows_visual_document_identity_unavailable'):
        office._close_after({'window': WINDOW, 'selector': {}, 'value': 'token'},
                            {'dialog': {'hwnd': 30}}, tmp_path, lambda: None)
    assert calls == [1] and not list(tmp_path.iterdir())


def test_close_recheck_proof_requires_production_backend_and_unchanged_native_receipt(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from agent_service.windows_winapp import OFFICE_PROVIDER_BINDING
    task_root = tmp_path / 'task'
    task_root.mkdir()
    selector = {'type': office.TYPE, 'name': office.LABEL['close_find'], 'automation_id': office.ID}
    condition = {'window': WINDOW, 'selector': selector, 'state': 'value', 'value': 'token'}
    source = {'condition': condition, 'source_document': {'path': 'saved.xlsx'}, 'original_state': 'unknown'}
    stage_root = tmp_path / 'stage'
    stage_root.mkdir()
    scan = {'task': {'stage_binding': {'root': str(stage_root), 'number': 2}}}
    monkeypatch.setattr(task, '_own', lambda root: nullcontext())
    monkeypatch.setattr(task, '_stage_guard', lambda root: nullcontext())
    monkeypatch.setattr(task, '_scan', lambda root: deepcopy(scan))
    monkeypatch.setattr(task, '_load', lambda path: (deepcopy(scan['task']), 'unused'))
    monkeypatch.setattr(access, 'BASE', tmp_path / 'observations')
    monkeypatch.setattr(office, '_close_recheck_source', lambda root: deepcopy(source))
    with pytest.raises(ComputerError, match='windows_office_recheck_backend_invalid'):
        office.recheck_close(task_root, SimpleNamespace())
    observation = {'window': WINDOW, 'controls': [{'id': 'office', **selector, 'visible': True,
        'enabled': True, 'value': 'token'}], 'truncated': False, 'scope': 'winapp_office',
        'office': {'role': 'close_find', 'verified': True, 'dialog_presence': 'destroyed',
                   'readback': {'dialog_presence': 'destroyed', 'document': source['source_document']}}}
    def read_worker(self, request, *, stop, root):
        root.mkdir(parents=True)
        write_new(root / 'native-request.json', request)
        write_new(root / 'native-outcome.json', {'ok': True, 'observation': observation})
        return {'ok': True, 'observation': observation}
    monkeypatch.setattr(access.WindowsBackend, '_call', read_worker)
    # Caller cannot redirect the new read into the original immutable task.
    backend = access.WinappOfficeBackend(parent=task_root)
    receipt = office.recheck_close(task_root, backend)
    assert not list(task_root.iterdir())
    assert __import__('pathlib').Path(receipt['native_proof']['root']).parent == access.BASE
    assert receipt['original_state'] == 'unknown' and receipt['actions'] == [] and not receipt['resumable']
    assert office.verify_close_recheck(receipt, task_root) == receipt
    assert office.verify_close_recheck_locked(receipt, task_root, scan=scan, expected_stage_root=stage_root) == receipt
    with pytest.raises(ComputerError, match='windows_office_recheck_source_changed'):
        office.verify_close_recheck_locked(receipt, task_root, scan=scan, expected_stage_root=tmp_path / 'other')
    altered = deepcopy(receipt)
    altered['observation']['office']['readback']['document']['path'] = 'other.xlsx'
    with pytest.raises(ComputerError, match='windows_office_recheck_invalid'):
        office.verify_close_recheck_locked(altered, task_root, scan=scan, expected_stage_root=stage_root)
    source_reads = []
    def drifting_source(root):
        source_reads.append(1)
        return deepcopy(source) if len(source_reads) == 1 else {**source, 'original_state': 'changed'}
    monkeypatch.setattr(office, '_close_recheck_source', drifting_source)
    with pytest.raises(ComputerError, match='windows_office_recheck_source_changed'):
        office.verify_close_recheck_locked(receipt, task_root, scan=scan, expected_stage_root=stage_root)
    monkeypatch.setattr(office, '_close_recheck_source', lambda root: deepcopy(source))
    path = __import__('pathlib').Path(receipt['native_proof']['root']) / 'native-outcome.json'
    original_read = office._close_recheck_observation
    def changed_during_verification(*args):
        result = original_read(*args)
        path.write_text('{}', encoding='utf-8')
        return result
    monkeypatch.setattr(office, '_close_recheck_observation', changed_during_verification)
    with pytest.raises(ComputerError, match='windows_office_recheck_source_changed'):
        office.verify_close_recheck_locked(receipt, task_root, scan=scan, expected_stage_root=stage_root)
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError, match='windows_office_recheck_invalid'):
        office.verify_close_recheck(receipt, task_root)


@pytest.mark.parametrize('case', ['valid', 'next', 'no_intent', 'two_invokes', 'not_approved'])
def test_recheck_source_requires_original_approved_unique_close_dispatch(tmp_path, monkeypatch, case):
    from agent_service import windows_visual_proposal, windows_winapp
    root = tmp_path / 'task'
    operation = root / 'attempts' / '0001' / 'operations' / '0001'
    events = root / 'attempts' / '0001' / 'events'
    events.mkdir(parents=True)
    operation.mkdir(parents=True)
    button = {'type': 'Button', 'name': '关闭', 'automation_id': '', 'runtime_id': [1, 2]}
    source = {'buttons': {'close_find': button}, 'dialog': {'hwnd': 30}, 'document': {'path': 'saved.xlsx'}}
    role = 'next_hit' if case == 'next' else 'close_find'
    monkeypatch.setattr(office, 'decode', lambda *args: ({'source': {'role': role}}, source))
    selector = {'type': office.TYPE, 'name': office.LABEL['close_find'], 'automation_id': office.ID}
    condition = {'id': 'target_1', 'window': WINDOW, 'selector': selector, 'state': 'value',
                 'value': 'token', 'retain': False}
    result = {'ok': False, 'state': 'unknown', 'effects': 'unknown', 'code': 'failure'}
    scan = {'task': {'provider_binding': OFFICE_PROVIDER_BINDING, 'schema': 3}, 'task_sha256': 'task',
            'goal': {'conditions': [condition]}, 'last_outcome': {'state': 'unknown'},
            'uncertain': True, 'completed': [], 'actions': 1, 'attempts': 1}
    monkeypatch.setattr(task, '_scan', lambda root: scan)
    monkeypatch.setattr(task, '_verify_sources', lambda scan: None)
    monkeypatch.setattr(task, '_inventory', lambda root: 'inventory')
    monkeypatch.setattr(windows_visual_proposal, 'approved', lambda *args: case != 'not_approved')
    observation = {'kind': 'observation', 'condition_id': 'target_1'}
    intent = {'kind': 'intent', 'condition_id': 'target_1', 'before_sha256': 'before',
              'candidate': {'control_id': 'office', 'action': 'office_close_find', 'value': 'token'}}
    outcome = {'kind': 'result', 'condition_id': 'target_1', 'result': result}
    for index, event in enumerate((observation, intent, outcome), 1):
        task._save(events / f'{index:04}.json', event)
    request = {'op': 'act', 'provider': OFFICE_PROVIDER_BINDING, 'action': 'office_close_find',
               'window': WINDOW, 'selector': selector, 'value': 'token', 'document_token': 'token',
               'before_sha': 'before', 'control': {'id': 'office'}}
    write_new(operation / 'native-request.json', request)
    if case != 'no_intent':
        write_new(operation / 'native-intent.json', {'request_sha': digest(request), 'role': 'close_find',
                  'route': 'winapp-strict-invoke-close-v1'})
    write_new(operation / 'native-outcome.json', result)
    slug = windows_winapp._native_slug(button)
    for index in range(2 if case == 'two_invokes' else 1):
        call = operation / 'winapp' / f'call-{index}'
        call.mkdir(parents=True)
        write_new(call / 'command.json', {'argv': [str(windows_winapp.EXE), 'ui', 'invoke', slug,
                  '--window', '30', '--action', 'invoke', '--json']})
        write_new(call / 'stdout.json', {'elementId': slug, 'hwnd': 30, 'requestedAction': 'invoke',
                  'performedAction': 'invoke', 'pattern': 'InvokePattern'})
        write_new(call / 'result.json', {'status': 'ok', 'exit_code': 0})
    if case == 'valid':
        assert office._close_recheck_source(root)['original_state'] == 'unknown'
    else:
        with pytest.raises(ComputerError, match='windows_office_recheck_ineligible'):
            office._close_recheck_source(root)
