"""Pinned, supervised Windows-MCP transport; native policy stays authoritative."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .computer import ComputerError, safe_root, write_new, digest
from .process import WindowsJob

ROOT = Path(__file__).resolve().parents[1]
COMMIT = 'b69ec872623b403f647fb3777685d4cf2678d3a2'
RUNTIME = ROOT / 'data/tool_runtime/windows-mcp/b69ec872'
PYTHON = RUNTIME / 'venv/Scripts/python.exe'
# Filled from the isolated, inspected installation; never trust a mutable manifest alone.
SOURCE_SHA256 = '9e851a15a4c98a873f54444b0886b25f1758884d187d344fd8b960ce99171893'
ROUTES = {'toggle': 'windows-mcp-guarded-click-v1',
          'select': 'windows-mcp-guarded-click-v1'}
PROVIDER_BINDING = {'provider': 'windows-mcp', 'version': '0.8.8', 'commit': COMMIT,
                    'routes': ROUTES}
TOOLS = ('Click', 'ControlStatus', 'Snapshot', 'WaitFor')  # Upstream always retains read-only ControlStatus.
LIMIT = 250_000


def verify_runtime():
    package = safe_root(RUNTIME / 'venv/Lib/site-packages/windows_mcp')
    files = sorted(package.rglob('*.py')) if package.is_dir() else []
    rows = {p.relative_to(package).as_posix(): sha256(p.read_bytes()).hexdigest() for p in files}
    if (not PYTHON.is_file() or not rows or
            sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest() != SOURCE_SHA256):
        raise ComputerError('windows_mcp_runtime_mismatch')
    return PYTHON


def environment(root):
    # Deliberately do not forward API keys, provider configuration, Python paths or MCP flags.
    names = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'PATH', 'PATHEXT',
             'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'}
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    env.update(USERPROFILE=str(root), HOME=str(root), PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PYTHONDONTWRITEBYTECODE='1', ANONYMIZED_TELEMETRY='false', POSTHOG_API_KEY='',
               WINDOWS_MCP_CONTROL='false', WINDOWS_MCP_WATCHDOG='false',
               WINDOWS_MCP_DISABLE_FLASH='1', WINDOWS_MCP_MAX_TREE_ELEMENTS='500',
               WINDOWS_MCP_UIA_CONNECTION_TIMEOUT_MS='1000', WINDOWS_MCP_UIA_TRANSACTION_TIMEOUT_MS='2000')
    return env


def point_for(control):
    if type(control) is not dict:
        raise ComputerError('windows_mcp_target_invalid')
    bounds = control.get('bounds')
    if (type(bounds) is not list or len(bounds) != 4 or any(type(v) is not int for v in bounds)
            or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]
            or not control.get('runtime_id') or control.get('password') or control.get('is_password')):
        raise ComputerError('windows_mcp_target_invalid')
    name = control.get('name')
    if type(name) is not str or not name or len(name) > 300 or any(c in name for c in '\r\n"'):
        raise ComputerError('windows_mcp_target_invalid')
    return [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2]


def match_snapshot(text, control):
    """Upstream coordinates corroborate native identity; they never authorize it."""
    point_for(control)
    if type(text) is not str or len(text) > LIMIT or '[truncated:' in text or 'Error capturing' in text:
        raise ComputerError('windows_mcp_snapshot_incomplete')
    expected_type = re.sub(r'\s+', '', control['type']).casefold()
    localized = {'复选框': 'checkbox', '单选按钮': 'radiobutton', '选项卡项目': 'tabitem'}
    pattern = re.compile(r'\((-?\d+)\s*,\s*(-?\d+)\)\s+([\w ]+)\s+"([^"\r\n]*)"\s+\[action:')
    matches = []
    l, t, r, b = control['bounds']
    for item in pattern.finditer(text):
        x, y = int(item[1]), int(item[2])
        observed_type = re.sub(r'\s+', '', item[3]).casefold()
        if (item[4] == control['name'] and localized.get(observed_type, observed_type) == expected_type
                and l <= x < r and t <= y < b):
            matches.append([x, y])
    # WPS can expose one popup under both its own and its owner's semantic tree.
    # Identical screen points still undergo a native RuntimeId hit-test before use.
    matches = sorted(set(tuple(point) for point in matches))
    if len(matches) != 1:
        raise ComputerError('windows_mcp_target_unmatched')
    return list(matches[0])


def validate_action(control, action, value):
    point_for(control)
    if action == 'toggle':
        ok = control.get('type') == 'CheckBox' and type(value) is str and value in {'0', '1'}
    elif action == 'select':
        ok = control.get('type') in {'TabItem', 'RadioButton'} and value == ''
    else:
        ok = False
    if not ok:
        raise ComputerError('windows_mcp_action_denied')


def native_guard(hwnd, control, wrapper, point):
    import win32gui
    import win32api
    from pywinauto import Desktop
    if win32gui.GetForegroundWindow() != hwnd:
        raise ComputerError('windows_mcp_foreground_changed')
    if any(win32api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92)):
        raise ComputerError('windows_input_busy')
    rect = wrapper.rectangle()
    if [rect.left, rect.top, rect.right, rect.bottom] != control['bounds']:
        raise ComputerError('screen_changed_reobserve')
    hit = Desktop(backend='uia').from_point(*point)
    if list(hit.element_info.runtime_id) != control['runtime_id']:
        raise ComputerError('windows_mcp_point_obscured')
    actual = win32gui.WindowFromPoint(tuple(point))
    if actual != hwnd and not win32gui.IsChild(hwnd, actual):
        raise ComputerError('windows_mcp_point_obscured')


class McpReader:
    def __init__(self, root, *, timeout=12, check=lambda: None):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ComputerError('windows_mcp_timeout_invalid')
        self.root, self.timeout, self.check = safe_root(root), timeout, check
        self.process = self.job = self.monitor = None
        self.bound = self.wrapper = None
        self.used = False

    def _wait(self, filename, *, acting=False):
        deadline = time.monotonic() + self.timeout
        while True:
            if acting:
                self.monitor.check()
            else:
                self.check()
            if (self.root.parent / 'cancel.flag').exists():
                raise ComputerError('user_cancelled')
            path = self.root / filename
            if path.is_file():
                if path.stat().st_size > LIMIT:
                    raise ComputerError('windows_mcp_output_limit')
                value = json.loads(path.read_text(encoding='utf-8'))
                if not value.get('ok'):
                    raise ComputerError(value.get('code', 'windows_mcp_failed'))
                return value
            error = self.root / 'error.json'
            if error.is_file():
                raise ComputerError(json.loads(error.read_text(encoding='utf-8')).get('code', 'windows_mcp_failed'))
            if self.process.poll() is not None:
                raise ComputerError('windows_mcp_process_failed')
            if time.monotonic() >= deadline:
                raise ComputerError('windows_mcp_timeout')
            time.sleep(.025)

    def bind(self, hwnd, control, wrapper, *, action):
        if self.process is not None or action not in ROUTES:
            raise ComputerError('windows_mcp_action_denied')
        point = point_for(control)
        native_guard(hwnd, control, wrapper, point)
        executable = verify_runtime()
        self.root.mkdir(parents=True, exist_ok=False)
        write_new(self.root / 'request.json', {'hwnd': hwnd, 'control': control, 'action': action,
                  'provider_binding': PROVIDER_BINDING})
        (self.root / 'server.toml').write_text('[control]\nenabled = false\n', encoding='utf-8')
        command = [str(executable), '-B', '-m', 'agent_service.windows_mcp_worker', str(self.root)]
        self.process = subprocess.Popen(command, cwd=ROOT, env=environment(self.root),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.job = WindowsJob(self.process)
        self.process.stdin.write(b'1')
        self.process.stdin.flush()
        ready = self._wait('ready.json')
        if ready.get('request_sha256') != digest(json.loads((self.root / 'request.json').read_text(encoding='utf-8'))):
            raise ComputerError('windows_mcp_binding_changed')
        point = match_snapshot(ready.get('snapshot'), control)
        self.check()
        native_guard(hwnd, control, wrapper, point)
        self.bound = {'hwnd': hwnd, 'control': deepcopy(control), 'point': point, 'action': action,
                      'snapshot_sha256': sha256(ready['snapshot'].encode()).hexdigest()}
        self.wrapper = wrapper
        write_new(self.root / 'binding.json', self.bound)
        return deepcopy(self.bound)

    def perform_bound(self, hwnd, binding, action, value):
        if self.used or self.bound is None or binding != self.bound or hwnd != binding['hwnd'] or action != binding['action']:
            raise ComputerError('windows_mcp_binding_changed')
        validate_action(binding['control'], action, value)
        self.check()
        native_guard(hwnd, binding['control'], self.wrapper, binding['point'])
        from .windows_mcp_input import PhysicalInputMonitor
        self.monitor = PhysicalInputMonitor()
        self.monitor.start()
        self.check()
        self.monitor.check()
        write_new(self.root / 'dispatch.json', {'binding_sha256': digest(binding), 'action': action,
                  'value': value, 'point': binding['point'], 'hwnd': hwnd})
        self.used = True
        self.process.stdin.write(b'2')
        self.process.stdin.flush()
        result = self._wait('result.json', acting=True)
        self.monitor.check()
        write_new(self.root / 'physical-input.json', self.monitor.proof())
        if result.get('dispatch_sha256') != digest(json.loads((self.root / 'dispatch.json').read_text(encoding='utf-8'))):
            raise ComputerError('windows_mcp_binding_changed')
        return result

    def close(self):
        # Closing the job kills only this operation's MCP tree, including on failed preparation.
        try:
            if self.job:
                self.job.close()
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=3)
                if self.process.stdin:
                    self.process.stdin.close()
        finally:
            if self.monitor:
                try:
                    self.monitor.release_owned_input()
                    if not (self.root / 'physical-input.json').exists():
                        write_new(self.root / 'physical-input.json', self.monitor.proof())
                finally:
                    self.monitor.close()

    def check_input(self):
        if self.monitor:
            self.monitor.check()
