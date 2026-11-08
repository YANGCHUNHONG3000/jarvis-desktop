import json
from types import SimpleNamespace
from copy import deepcopy
import pytest

from agent_service.computer import ComputerError
from agent_service import windows_mcp as m
from agent_service.windows_mcp_worker import text_result


@pytest.fixture
def control():
    return {'type': 'CheckBox', 'name': '区分大小写', 'bounds': [10, 20, 110, 60],
            'runtime_id': [42, 7], 'checked': 0}


def test_upstream_semantic_snapshot_matches_native_bounds(control):
    text = 'UI Tree:\n└── (60,40) check box "区分大小写"  [action: toggle]'
    assert m.match_snapshot(text, control) == [60, 40]


@pytest.mark.parametrize('text', [
    '(600,400) check box "区分大小写"  [action: toggle]',
    '(60,40) button "区分大小写"  [action: click]',
    '(60,40) check box "其他"  [action: toggle]',
    '(60,40) check box "区分大小写"  [action: toggle]\n[truncated: reached limit]',
    '(60,40) check box "区分大小写"  [action: toggle]\n(61,40) check box "区分大小写"  [action: toggle]',
    'Error capturing desktop state: failed',
])
def test_no_permission_from_ambiguous_or_failed_snapshot(control, text):
    with pytest.raises(ComputerError):
        m.match_snapshot(text, control)


@pytest.mark.parametrize('patch', [{'bounds': [1, 2, 1, 3]}, {'bounds': [True, 2, 3, 4]},
    {'runtime_id': []}, {'password': True}, {'is_password': True}, {'name': 'bad\nname'},
    {'name': '"quoted"'}, {'name': ''}])
def test_invalid_target_denied(control, patch):
    control.update(patch)
    with pytest.raises(ComputerError):
        m.point_for(control)


@pytest.mark.parametrize('action,value,kind', [('set_value', 'text', 'Edit'),
    ('select', '', 'ListItem'), ('expand', '', 'TreeItem'), ('invoke', '', 'Button'),
    ('toggle', True, 'CheckBox'), ('toggle', '2', 'CheckBox')])
def test_unapproved_capabilities_rejected(control, action, value, kind):
    control['type'] = kind
    with pytest.raises(ComputerError):
        m.validate_action(control, action, value)


def test_safe_states_and_closed_tool_set(control):
    m.validate_action(control, 'toggle', '1')
    control['type'] = 'RadioButton'
    m.validate_action(control, 'select', '')
    assert set(m.TOOLS) == {'Click', 'ControlStatus', 'Snapshot', 'WaitFor'}
    assert set(m.ROUTES) == {'select', 'toggle'}


def test_environment_never_passes_credentials_or_external_config(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'not-a-real-key')
    monkeypatch.setenv('PYTHONPATH', 'untrusted')
    monkeypatch.setenv('WINDOWS_MCP_TOOLS', 'PowerShell')
    monkeypatch.setenv('ANONYMIZED_TELEMETRY', 'true')
    env = m.environment(tmp_path)
    assert not {'OPENAI_API_KEY', 'PYTHONPATH', 'WINDOWS_MCP_TOOLS'} & env.keys()
    assert env['ANONYMIZED_TELEMETRY'] == 'false'
    assert env['POSTHOG_API_KEY'] == ''
    assert env['HOME'] == env['USERPROFILE'] == str(tmp_path)
    assert env['WINDOWS_MCP_CONTROL'] == 'false'


def test_runtime_change_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(m, 'RUNTIME', tmp_path)
    monkeypatch.setattr(m, 'PYTHON', tmp_path / 'python.exe')
    with pytest.raises(ComputerError, match='runtime_mismatch'):
        m.verify_runtime()


@pytest.mark.parametrize('bad', [SimpleNamespace(is_error=True, content=[]),
    SimpleNamespace(is_error=False, content=[SimpleNamespace(type='image')]),
    SimpleNamespace(is_error=False, content=[SimpleNamespace(type='text', text='x' * (m.LIMIT + 1))])])
def test_mcp_error_or_unrequested_image_is_not_success(bad):
    with pytest.raises(ComputerError):
        text_result(bad)


def test_text_content_preserved():
    assert text_result(SimpleNamespace(is_error=False, content=[SimpleNamespace(type='text', text='ok')])) == 'ok'


def test_real_fastmcp_array_and_localized_control_type(control):
    raw = json.dumps(['UI Tree:\n└── (60,40) 复选框 "区分大小写"  [action: click]  [toggle:off]'])
    parsed = text_result(SimpleNamespace(is_error=False, content=[SimpleNamespace(type='text', text=raw)]))
    assert m.match_snapshot(parsed, control) == [60, 40]


def test_duplicate_popup_representation_same_physical_point(control):
    line = '(60,40) 复选框 "区分大小写"  [action: click]'
    assert m.match_snapshot(line + '\n' + line, control) == [60, 40]


@pytest.mark.parametrize('raw', ['[]', '[{}]', '[["nested"]]'])
def test_mixed_upstream_payload_rejected(raw):
    with pytest.raises(ComputerError):
        text_result(SimpleNamespace(is_error=False, content=[SimpleNamespace(type='text', text=raw)]))


@pytest.mark.skipif(not m.PYTHON.is_file(), reason='optional isolated MCP runtime not installed')
def test_installed_mcp_sdk_result_contract():
    import subprocess
    result = subprocess.run([str(m.PYTHON), '-B', '-c',
        "from mcp.types import CallToolResult, TextContent; "
        "from agent_service.windows_mcp_worker import text_result; "
        "r=CallToolResult(content=[TextContent(type='text',text='sdk-result')],is_error=False); "
        "assert text_result(r)=='sdk-result'"], cwd=m.ROOT, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')


def test_waiting_cancelled_before_dispatch(tmp_path):
    reader = m.McpReader(tmp_path / 'mcp', check=lambda: (_ for _ in ()).throw(ComputerError('user_cancelled')))
    with pytest.raises(ComputerError, match='user_cancelled'):
        reader._wait('ready.json')
    assert not reader.used


def test_bound_action_cannot_replay_or_change(tmp_path, control):
    reader = m.McpReader(tmp_path)
    reader.bound = {'hwnd': 12, 'control': control, 'point': [60, 40], 'action': 'toggle'}
    reader.used = True
    with pytest.raises(ComputerError, match='binding_changed'):
        reader.perform_bound(12, deepcopy(reader.bound), 'toggle', '1')


def test_physical_takeover_during_wait_stops_before_success(tmp_path):
    reader = m.McpReader(tmp_path)
    (tmp_path / 'result.json').write_text(json.dumps({'ok': True}), encoding='utf-8')
    reader.monitor = SimpleNamespace(check=lambda: (_ for _ in ()).throw(ComputerError('user_takeover')))
    with pytest.raises(ComputerError, match='user_takeover'):
        reader._wait('result.json', acting=True)


@pytest.mark.parametrize('events,releases', [([(0x201, True)], 1),
    ([(0x201, True), (0x202, True)], 0),
    ([(0x201, True), (0x201, False)], 0),
    ([(0x201, True), (0x202, False)], 0)])
def test_interrupted_click_release_preserves_human_input(monkeypatch, events, releases):
    import sys
    from agent_service.windows_mcp_input import PhysicalInputMonitor
    monitor = object.__new__(PhysicalInputMonitor)
    monitor.left_held = monitor.physical_left = monitor.released_left = False
    sent = []
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(mouse_event=lambda *args: sent.append(args)))
    for message, injected in events:
        monitor.record_mouse(message, injected)
    monitor.release_owned_input()
    assert sent == [(4, 0, 0, 0, 0)] * releases
