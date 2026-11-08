"""Automatic route selection and durable binding; no live UI or providers."""
from copy import deepcopy

import pytest

from agent_service import windows_access as access, windows_auto as auto, windows_task as task
from agent_service import windows_winapp as winapp, windows_mcp as mcp
from agent_service.computer import ComputerError, digest, read_record, write_new
from test_windows_task import WINDOW, goal
from test_windows_mcp_backend import native


def fixture(kind='TreeItem', action='select', state='selected', value=True, **changes):
    control = dict(id='target', runtime_id=[1, 2], type=kind, name='View', automation_id='target',
        patterns=[action], enabled=True, visible=True, selected=False, checked=0,
        expanded=0, value='', bounds=[10, 10, 30, 30], ancestors=[])
    control.update(changes)
    observed = dict(window=deepcopy(WINDOW), application='explorer', controls=[control],
                    truncated=False, dialogs=[])
    condition = dict(id='target', window=deepcopy(WINDOW),
        selector={k: control[k] for k in ('type', 'name', 'automation_id')},
        state=state, value=value, retain=True)
    return observed, condition, control


@pytest.mark.parametrize('kind,action,state,value,provider,route', [
    ('CheckBox', 'toggle', 'checked', 1, mcp.PROVIDER_BINDING, 'windows-mcp-guarded-click-v1'),
    ('RadioButton', 'select', 'selected', True, mcp.PROVIDER_BINDING, 'windows-mcp-guarded-click-v1'),
    ('TabItem', 'select', 'selected', True, mcp.PROVIDER_BINDING, 'windows-mcp-guarded-click-v1'),
    ('TreeItem', 'select', 'selected', True, winapp.PROVIDER_BINDING, 'winapp-explicit-invoke'),
    ('ListItem', 'select', 'selected', True, winapp.PROVIDER_BINDING, 'winapp-explicit-invoke'),
    ('TreeItem', 'expand', 'expanded', 1, winapp.PROVIDER_BINDING, 'winapp-explicit-invoke'),
    ('TreeItem', 'collapse', 'collapsed', 0, winapp.PROVIDER_BINDING, 'winapp-explicit-invoke'),
    ('Edit', 'set_value', 'value', 'needle', None, 'native-ValuePattern'),
    ('ComboBox', 'set_value', 'value', 'needle', None, 'native-ValuePattern'),
])
def test_deterministic_routes_and_intent_precedes_worker(tmp_path, monkeypatch, kind, action, state, value, provider, route):
    observed, condition, control = fixture(kind, action, state, value,
        name='Search option' if action == 'set_value' else 'View', expanded=1 if action == 'collapse' else 0)
    backend = access.AutoWindowsBackend(tmp_path)
    candidates = backend.candidates(observed, condition)
    assert len(candidates) == 1
    root = tmp_path / 'operation'
    calls = []
    def worker(request, **kwargs):
        proof = auto.verify_request(request, root)
        assert proof['selected'] == {'provider': provider, 'route': route}
        assert kwargs['root'] == root
        calls.append(request)
        return {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    monkeypatch.setattr(backend, '_call', worker)
    argument = candidates[0]['value']
    result = backend.act(WINDOW, control, action, argument, digest(observed), root)
    assert result['state'] == 'unknown' and len(calls) == 1
    assert calls[0].get('provider') == provider
    with pytest.raises(ComputerError, match='windows_auto_route_missing'):
        backend.act(WINDOW, control, action, argument, digest(observed), tmp_path / 'retry')
    assert len(calls) == 1


@pytest.mark.parametrize('kind,action,state,value,changes', [
    ('Button', 'invoke', 'selected', True, {}),
    ('Edit', 'Type', 'value', 'needle', {'name': 'Search'}),
    ('CheckBox', 'toggle', 'checked', 1, {'name': 'Delete account'}),
    ('TabItem', 'select', 'selected', True, {'bounds': None}),
    ('TreeItem', 'expand', 'expanded', 1, {'expanded': None}),
    ('Edit', 'set_value', 'value', 'needle', {'name': 'Password'}),
])
def test_unsupported_target_stops_without_dispatch(tmp_path, kind, action, state, value, changes):
    obs, condition, control = fixture(kind, action, state, value, **changes)
    backend = access.AutoWindowsBackend(tmp_path)
    assert backend.candidates(obs, condition) == []
    assert auto.route(obs, control, action, value, condition['selector']) is None


def prepared(tmp_path, monkeypatch):
    obs, condition, control = fixture()
    backend = access.AutoWindowsBackend(tmp_path)
    backend.candidates(obs, condition)
    root = tmp_path / 'operation'
    seen = []
    monkeypatch.setattr(backend, '_call', lambda request, **kwargs: seen.append(request) or {'ok': False})
    backend.act(WINDOW, control, 'select', '', digest(obs), root)
    return root, seen[0], obs, condition, control


@pytest.mark.parametrize('field,value', [('provider', mcp.PROVIDER_BINDING), ('action', 'toggle'),
    ('value', '1'), ('before_sha', 'wrong'), ('auto_route_sha256', 'wrong'),
    ('document_token', 'invented'), ('unexpected', True)])
def test_worker_rejects_changed_route_or_request(tmp_path, monkeypatch, field, value):
    root, request, *_ = prepared(tmp_path, monkeypatch)
    request[field] = value
    with pytest.raises(ComputerError, match='windows_auto_route_invalid'):
        auto.verify_request(request, root)


def test_recovery_keeps_exact_component_binding_and_old_tasks(tmp_path):
    backend = access.AutoWindowsBackend(tmp_path)
    bound = task.create(tmp_path, goal(), provider_binding=auto.PROVIDER_BINDING)
    assert task.load(bound)['provider_binding'] == auto.PROVIDER_BINDING
    assert type(access.backend_for_task(bound)) is access.AutoWindowsBackend
    assert access.backend_for_binding(auto.PROVIDER_BINDING, fallback=backend) is backend
    assert backend.allow_visual_options is False and backend.allow_office_options is True
    old = task.create(tmp_path, goal())
    assert type(access.backend_for_task(old)) is access.WindowsBackend
    assert type(access.backend_for_binding(mcp.PROVIDER_BINDING)) is access.WindowsMcpBackend
    invalid = deepcopy(auto.PROVIDER_BINDING)
    invalid['components']['winapp']['version'] = 'future'
    assert not winapp.valid_provider_binding(invalid)
    with pytest.raises(ComputerError, match='windows_provider_binding_invalid'):
        access.backend_for_binding(invalid)


def test_no_success_from_wrapper_without_worker_receipts(tmp_path, monkeypatch):
    root, request, observed, condition, _ = prepared(tmp_path, monkeypatch)
    candidate = {'control_id': 'target', 'action': 'select', 'value': ''}
    write_new(root / 'native-request.json', request)
    result = {'ok': True, 'state': 'verified', 'effects': 'verified'}
    with pytest.raises(ComputerError):
        auto.verify_result(root, condition, candidate, digest(observed), result)
    write_new(root / 'native-outcome.json', {**result, 'input_epoch': 33})
    write_new(root / 'native-intent.json', {'request_sha': digest(request)})
    auto.verify_result(root, condition, candidate, digest(observed), result)


def test_unknown_task_never_replays_or_switches_provider(tmp_path):
    from test_windows_task import Backend
    bound = task.create(tmp_path, goal(), provider_binding=auto.PROVIDER_BINDING)
    backend = Backend()
    backend.provider_binding = auto.PROVIDER_BINDING
    backend.result = {'ok': False, 'state': 'unknown', 'effects': 'unknown'}
    first = task.run(bound, backend)
    assert first['state'] == 'unknown' and len(backend.calls) == 1
    with pytest.raises(ComputerError, match='windows_unknown_requires_review'):
        task.run(bound, backend)
    assert len(backend.calls) == 1


def test_office_auto_keeps_source_snapshot_concrete_binding_and_stage_gate(tmp_path):
    from test_windows_winapp_office import source_snapshot
    from agent_service import windows_winapp_office as office
    snapshot = source_snapshot(tmp_path)
    item = office.candidates_for(snapshot)[0]
    condition = office.condition(item)
    spec = {'schema': 1, 'instruction': 'close', 'conditions': [condition], 'max_steps': 1}
    with pytest.raises(ComputerError, match='windows_office_stage_required'):
        task.create(tmp_path, spec, provider_binding=auto.PROVIDER_BINDING)
    child = task.create(tmp_path, spec, provider_binding=auto.PROVIDER_BINDING,
        stage_binding={'root': str(tmp_path / 'stages'), 'number': 1})
    assert task.load(child)['provider_binding'] == auto.PROVIDER_BINDING
    assert read_record(tmp_path / 'worker' / 'native-request.json')['provider'] == winapp.OFFICE_PROVIDER_BINDING


def test_auto_selected_mcp_enters_existing_guarded_worker_and_readback(native, tmp_path):
    observed, control = native.obs, native.c
    condition = dict(id='target', window=observed['window'],
        selector={k: control[k] for k in ('type', 'name', 'automation_id')},
        state='selected', value=True, retain=True)
    proof = {'schema': 1, 'policy_binding': deepcopy(auto.PROVIDER_BINDING), 'condition': condition,
             'observation': deepcopy(observed), 'selected': auto.route(observed, control, 'select', '')}
    write_new(tmp_path / 'auto-route.json', proof)
    native.request.update(selector=condition['selector'], auto_route_sha256=digest(proof))
    result = access._perform(native.request, tmp_path, lambda: None, [1])
    assert result['ok'] and result['state'] == 'verified'
    assert native.calls.count('dispatch') == 1
    assert read_record(tmp_path / 'native-intent.json')['request_sha'] == digest(native.request)


def test_native_value_route_preserves_same_find_observation_mode(tmp_path, monkeypatch):
    root, request, *_ = prepared(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(access, '_observe', lambda *args, **kwargs: calls.append(kwargs) or ({}, {}))
    request.pop('provider')
    access._request_observation(request, lambda: None)
    assert calls == [{'winapp_find': True}]
