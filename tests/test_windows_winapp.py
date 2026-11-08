import pytest

from agent_service import windows_winapp as winapp
from agent_service.computer import ComputerError


def test_property_null_is_not_a_boolean():
    element = {'selector': 'btn-1', 'type': 'Button', 'name': '视图', 'className': 'Tab'}
    payload = {'elementId': 'btn-1', 'element': dict(element),
               'properties': {'IsPassword': 'False', 'AutomationId': None}}
    props = winapp.property_result(payload, element)
    assert winapp.boolean_property(props, 'IsPassword') is False
    assert props['AutomationId'] is None
    payload['properties'] = {'IsSelected': None}
    missing = winapp.property_result(payload, element, 'IsSelected')
    with pytest.raises(ComputerError, match='winapp_property_unavailable'):
        winapp.boolean_property(missing, 'IsSelected')


def test_exact_match_rejects_ambiguity_truncation_and_window_change():
    node = {'type': 'Button', 'name': 'Example', 'selector': 'btn-1234',
            'isEnabled': True, 'isOffscreen': False}
    data = {'windows': [{'hwnd': 1, 'elements': [node, dict(node)]}]}
    with pytest.raises(ComputerError, match='winapp_target_not_unique'):
        winapp.exact_element(data, 1, name='Example', control_type='Button')
    data['windows'][0]['elements'] = [{**node, 'hasMoreChildren': True}]
    with pytest.raises(ComputerError, match='winapp_tree_truncated'):
        winapp.exact_element(data, 1, name='Example', control_type='Button')
    with pytest.raises(ComputerError, match='winapp_window_mismatch'):
        winapp.exact_element(data, 2, name='Example', control_type='Button')


def test_property_target_and_types_must_match():
    element = {'selector': 'btn-1', 'type': 'Button', 'name': 'X', 'className': 'C'}
    response = {'elementId': 'btn-1', 'element': dict(element), 'properties': {'IsPassword': False}}
    with pytest.raises(ComputerError, match='winapp_property_invalid'):
        winapp.property_result(response, element)
    response['properties'] = {'IsPassword': 'False'}
    response['element']['name'] = 'Y'
    with pytest.raises(ComputerError, match='winapp_target_changed'):
        winapp.property_result(response, element)


def test_commands_are_scoped_and_inputs_bounded(tmp_path, monkeypatch):
    reader = winapp.WinAppReader(tmp_path)
    calls = []
    data = {'windows': [{'hwnd': 7, 'elements': []}]}
    monkeypatch.setattr(reader, '_run', lambda args: calls.append(args) or data)
    reader.inspect(7)
    assert calls == [['ui', 'inspect', '--window', '7', '--depth', '25', '--json']]
    for invalid in (0, -1, True, '7'):
        with pytest.raises(ComputerError):
            reader.inspect(invalid)
    with pytest.raises(ComputerError, match='winapp_selector_invalid'):
        reader.get_property(7, {'selector': '--help'})
    with pytest.raises(ComputerError, match='winapp_property_denied'):
        reader.get_property(7, {'selector': 'btn-1'}, name='anything')
    assert len(calls) == 1
    with pytest.raises(ComputerError, match='winapp_command_denied'):
        winapp._command(['ui', 'click', 'x', '--window', '7', '--json'])


def test_json_requires_one_complete_document():
    for raw in (b'banner\n{}', b'{}\n{}', b'\xff'):
        with pytest.raises(ComputerError, match='winapp_json_invalid'):
            winapp._decode(raw)
    assert winapp._decode(b'{}\n') == {}


def test_binary_mismatch_stops_before_command(tmp_path, monkeypatch):
    binary = tmp_path / 'winapp.exe'
    binary.write_bytes(b'untrusted')
    monkeypatch.setattr(winapp, 'EXE', binary)
    reader = winapp.WinAppReader(tmp_path / 'evidence')
    with pytest.raises(ComputerError, match='winapp_binary_mismatch'):
        reader.inspect(1)
    assert not reader.parent.exists()


def test_binding_requires_complete_unique_geometry_and_properties(tmp_path, monkeypatch):
    from types import SimpleNamespace
    reader = winapp.WinAppReader(tmp_path)
    wrapper = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=1, top=2, right=11, bottom=22),
                              element_info=SimpleNamespace(element=SimpleNamespace(CurrentClassName='TestClass')))
    control = {'type': 'Edit', 'name': 'Search', 'automation_id': 'field', 'runtime_id': [1, 2]}
    element = {'type': 'Edit', 'name': 'Search', 'className': 'TestClass', 'selector': winapp._native_slug(control),
               'isEnabled': True, 'isOffscreen': False, 'x': 1, 'y': 2, 'width': 10, 'height': 20}
    tree = {'windows': [{'hwnd': 5, 'elements': [element]}]}
    props = {'Name': 'Search', 'AutomationId': 'field', 'ControlType': 'Edit', 'ClassName': 'TestClass',
             'IsPassword': 'False', 'IsEnabled': 'True', 'IsOffscreen': 'False'}
    monkeypatch.setattr(reader, 'inspect', lambda hwnd: tree)
    monkeypatch.setattr(reader, 'get_property', lambda hwnd, element, **kw:
                        (props, element) if kw.get('canonical') else props)
    assert reader.bind(5, control, wrapper) == element
    props['AutomationId'] = None
    control['automation_id'] = ''
    element['selector'] = winapp._native_slug(control)
    assert reader.bind(5, control, wrapper) == element
    # CLI's IsPassword result is not a safety signal. The caller uses native UIA.
    props['IsPassword'] = 'True'
    assert reader.bind(5, control, wrapper) == element
    props['IsEnabled'] = None
    with pytest.raises(ComputerError, match='winapp_target_changed'):
        reader.bind(5, control, wrapper)
    props['IsEnabled'] = 'True'
    tree['windows'][0]['elements'].append(dict(element))
    with pytest.raises(ComputerError, match='winapp_target_not_unique'):
        reader.bind(5, control, wrapper)


def test_null_cli_class_requires_explicit_empty_native_class(tmp_path, monkeypatch):
    from types import SimpleNamespace
    reader = winapp.WinAppReader(tmp_path)
    info = SimpleNamespace(element=SimpleNamespace(CurrentClassName=''))
    wrapper = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=1, top=2, right=11, bottom=22), element_info=info)
    control = {'type': 'TreeItem', 'name': '图库', 'automation_id': '', 'runtime_id': [1, 2]}
    element = {'type': 'TreeItem', 'name': '图库', 'selector': winapp._native_slug(control),
               'isEnabled': True, 'isOffscreen': False, 'x': 1, 'y': 2, 'width': 10, 'height': 20}
    tree = {'windows': [{'hwnd': 5, 'elements': [element]}]}
    props = {'Name': '图库', 'AutomationId': None, 'ControlType': 'TreeItem', 'ClassName': None,
             'IsEnabled': 'True', 'IsOffscreen': 'False'}
    monkeypatch.setattr(reader, 'inspect', lambda hwnd: tree)
    monkeypatch.setattr(reader, 'get_property', lambda hwnd, element, **kw:
                        (props, element) if kw.get('canonical') else props)
    assert reader.bind(5, control, wrapper) == element
    del props['ClassName']
    with pytest.raises(ComputerError, match='winapp_target_changed'):
        reader.bind(5, control, wrapper)
    props['ClassName'] = None
    info.element.CurrentClassName = None
    with pytest.raises(ComputerError, match='winapp_class_unavailable'):
        reader.bind(5, control, wrapper)
    info.element.CurrentClassName = 'NativeClass'
    element['className'] = 'NativeClass'
    with pytest.raises(ComputerError, match='winapp_target_changed'):
        reader.bind(5, control, wrapper)


def test_automation_id_selector_is_canonicalized_only_after_stable_exact_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    reader = winapp.WinAppReader(tmp_path)
    wrapper = SimpleNamespace(rectangle=lambda: SimpleNamespace(left=1, top=2, right=11, bottom=22),
                              element_info=SimpleNamespace(element=SimpleNamespace(CurrentClassName='UIItem')))
    control = {'type': 'ListItem', 'name': 'example.xlsx', 'automation_id': '0',
               'runtime_id': [72732, 876145216, 0]}
    element = {'type': 'ListItem', 'name': 'example.xlsx', 'className': 'UIItem', 'selector': '0',
               'isEnabled': True, 'isOffscreen': False, 'x': 1, 'y': 2, 'width': 10, 'height': 20}
    canonical = {**element, 'selector': 'itm-0-9d2b'}
    props = {'Name': control['name'], 'AutomationId': '0', 'ControlType': 'ListItem',
             'ClassName': 'UIItem', 'IsEnabled': 'True', 'IsOffscreen': 'False'}
    monkeypatch.setattr(reader, 'inspect', lambda hwnd: {'windows': [{'hwnd': hwnd, 'elements': [element]}]})
    calls = []
    def read(args):
        calls.append(args[2])
        return {'elementId': canonical['selector'], 'element': dict(canonical), 'properties': dict(props)}
    monkeypatch.setattr(reader, '_run', read)
    assert reader.bind(5, control, wrapper) == canonical
    assert calls == ['0', 'itm-0-9d2b']
    def changed(args):
        response = read(args)
        if args[2] == canonical['selector']:
            response['element']['x'] += 1
        return response
    monkeypatch.setattr(reader, '_run', changed)
    with pytest.raises(ComputerError, match='winapp_target_changed'):
        reader.bind(5, control, wrapper)

    for fake in ('example', 'other-id', 'itm-0-0000', 'btn-0-9d2b'):
        canonical['selector'] = fake
        with pytest.raises(ComputerError, match='winapp_native_identity_mismatch'):
            reader.bind(5, control, wrapper)


def test_action_uses_exact_cli_action_and_rejects_wrong_receipt(tmp_path, monkeypatch):
    reader = winapp.WinAppReader(tmp_path)
    commands = []
    def invoke(args):
        commands.append(args)
        return {'elementId': 'btn-test-1234', 'hwnd': 5, 'requestedAction': 'select', 'performedAction': 'select'}
    monkeypatch.setattr(reader, '_run', invoke)
    reader.perform_bound(5, {'selector': 'btn-test-1234'}, 'select', '')
    assert commands[0] == ['ui', 'invoke', 'btn-test-1234', '--window', '5', '--action', 'select', '--json']
    monkeypatch.setattr(reader, '_run', lambda args: {'elementId': 'other'})
    with pytest.raises(ComputerError, match='winapp_action_result_invalid'):
        reader.perform_bound(5, {'selector': 'btn-test-1234'}, 'select', '')
    with pytest.raises(ComputerError, match='winapp_action_denied'):
        reader.perform_bound(5, {'selector': 'btn-test-1234'}, 'set_value', 'secret')
    with pytest.raises(ComputerError, match='winapp_command_denied'):
        winapp._command(['ui', 'set-value', 'btn-1', 'secret', '--window', '5', '--json'])


@pytest.mark.parametrize('value, requested', [('1', 'toggle-on'), ('0', 'toggle-off')])
def test_toggle_accepts_only_explicit_togglepattern_receipt(tmp_path, monkeypatch, value, requested):
    reader = winapp.WinAppReader(tmp_path)
    result = {'elementId': 'chk-test-1234', 'hwnd': 5, 'pattern': 'TogglePattern',
              'requestedAction': requested, 'performedAction': 'toggle'}
    calls = []
    monkeypatch.setattr(reader, '_run', lambda args: calls.append(args) or dict(result))
    assert reader.perform_bound(5, {'selector': 'chk-test-1234'}, 'toggle', value) == result
    assert calls == [['ui', 'invoke', 'chk-test-1234', '--window', '5', '--action', requested, '--json']]
    for key, wrong in [('elementId', 'chk-other-1234'), ('hwnd', 6), ('pattern', 'InvokePattern'),
                       ('requestedAction', 'toggle'), ('performedAction', 'none'),
                       ('performedAction', requested)]:
        altered = {**result, key: wrong}
        monkeypatch.setattr(reader, '_run', lambda args, value=altered: value)
        with pytest.raises(ComputerError, match='winapp_action_result_invalid'):
            reader.perform_bound(5, {'selector': 'chk-test-1234'}, 'toggle', value)
    monkeypatch.setattr(reader, '_run', lambda args: {**result, 'hwnd': True})
    with pytest.raises(ComputerError, match='winapp_action_result_invalid'):
        reader.perform_bound(1, {'selector': 'chk-test-1234'}, 'toggle', value)


def test_receipt_removes_values_recursively():
    payload = {'value': 'secret', 'child': [{'Value': 'other', 'name': 'label',
        'properties': {'Value': 'secret', 'IsReadOnly': 'False'}, 'oldValue': 'hidden'}]}
    assert winapp._without_values(payload) == {'child': [{'name': 'label',
        'properties': {'IsReadOnly': 'False'}}]}


def test_runner_never_persists_raw_stdout_or_stderr(tmp_path, monkeypatch):
    from hashlib import sha256
    from io import BytesIO
    import json
    from types import SimpleNamespace
    executable = tmp_path / 'winapp.exe'
    executable.write_bytes(b'synthetic')
    monkeypatch.setattr(winapp, 'EXE', executable)
    monkeypatch.setattr(winapp, 'SHA256', sha256(b'synthetic').hexdigest())
    monkeypatch.setattr(winapp, 'DLL_HASHES', {})
    data = {'windows': [{'hwnd': 7, 'elements': [{'type': 'Edit', 'name': 'label',
        'isEnabled': True, 'isOffscreen': False, 'value': 'sensitive-output'}]}]}
    process = SimpleNamespace(stdout=BytesIO(json.dumps(data).encode()), stderr=BytesIO(b'sensitive-error'),
                              returncode=0, poll=lambda: 0, wait=lambda **kwargs: 0)
    monkeypatch.setattr(winapp.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(winapp, 'WindowsJob', lambda p: SimpleNamespace(close=lambda: None))
    reader = winapp.WinAppReader(tmp_path / 'receipts')
    assert reader.inspect(7)['windows'][0]['elements'][0]['value'] == 'sensitive-output'
    receipt = next(reader.parent.iterdir())
    contents = b''.join(p.read_bytes() for p in receipt.iterdir())
    assert b'sensitive-output' not in contents and b'sensitive-error' not in contents
    assert sorted(p.name for p in receipt.iterdir()) == ['command.json', 'parse.json', 'result.json', 'stdout.json']


def test_winapp_backend_observes_through_guarded_worker(tmp_path, monkeypatch):
    from agent_service.windows_access import WinappBackend
    backend = WinappBackend(tmp_path)
    calls = []
    monkeypatch.setattr(backend, '_call', lambda request, **kw: calls.append(request) or
                        {'ok': True, 'observation': {'controls': ['raw']}})
    assert backend.observe({'hwnd': 9}) == {'controls': ['raw']}
    assert calls[0]['provider'] == winapp.PROVIDER_BINDING
    selector = {'type': 'Edit', 'name': 'Search', 'automation_id': ''}
    backend.observe_target({'hwnd': 9}, selector)
    assert calls[1]['selector'] == selector and calls[1]['provider'] == winapp.PROVIDER_BINDING
