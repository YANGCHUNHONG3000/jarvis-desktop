"""Pinned winappCli discovery and guarded stateful invocation.

Selectors are ephemeral discovery hints, never authorization or identity proof.
The caller must apply its own window/element identity and sensitivity checks.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from .computer import ComputerError, safe_root, write_new
from .process import WindowsJob

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'data/tool_runtime/winapp/0.7.1/bin/winapp.exe'
SHA256 = 'bf29f665ab4a030f20cd34a9121276301871ee89fd9c13118c7221563858d692'
DLL_HASHES = {'libHarfBuzzSharp.dll': '7ddabbcb6f631b08bf7ffd88df53cf4f3aca58ce41e4d44896d80ea4099dd384',
              'libSkiaSharp.dll': '016d87c41463f6fabd55baa41bda5b699491e0c4ec64b816bc3f1621b4049c1b'}
ROUTES = {'set_value': 'native-ValuePattern', 'select': 'winapp-explicit-invoke',
          'expand': 'winapp-explicit-invoke', 'collapse': 'winapp-explicit-invoke',
          'toggle': 'winapp-explicit-invoke'}
PROVIDER_BINDING = {'provider': 'winapp-cli', 'version': '0.7.1', 'sha256': SHA256,
                    'dll_sha256': DLL_HASHES, 'routes': ROUTES}
OFFICE_PROVIDER_BINDING = {**PROVIDER_BINDING, 'routes': {**ROUTES,
    'office_next_hit': 'winapp-strict-invoke-next-v1',
    'office_close_find': 'winapp-strict-invoke-close-v1'}}


def valid_provider_binding(binding):
    from .windows_mcp import PROVIDER_BINDING as MCP_PROVIDER_BINDING
    from .windows_auto import PROVIDER_BINDING as AUTO_PROVIDER_BINDING
    return binding in (PROVIDER_BINDING, OFFICE_PROVIDER_BINDING, MCP_PROVIDER_BINDING, AUTO_PROVIDER_BINDING)
LIMIT = 2_000_000
MAX_NODES = 2000
PROPERTIES = frozenset({'Name', 'AutomationId', 'ControlType', 'ClassName', 'IsEnabled',
    'IsOffscreen', 'IsPassword', 'IsReadOnly', 'Value', 'IsSelected', 'ToggleState',
    'ExpandCollapseState', 'HasKeyboardFocus'})
_SLUG = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,127}\Z')
# v0.7.1 SlugGenerator.GetPrefix/Normalize/ComputeHash. Unlike the CLI's
# looser parser, require the exact generated representation of our native ID.
# Upstream (MIT): https://github.com/microsoft/winappCli/blob/v0.7.1/src/winapp-CLI/WinApp.UIAutomation/Services/SlugGenerator.cs
_PREFIX = dict(button='btn', edit='txt', textbox='txt', checkbox='chk', combobox='cmb',
    listitem='itm', tabitem='tab', tab='tab', image='img', text='lbl', textblock='lbl',
    pane='pn', window='win', group='grp', hyperlink='lnk', menuitem='mnu', menubar='mnb',
    menu='mnu', list='lst', tree='tre', treeitem='tri', datagrid='grd', dataitem='dat',
    radiobutton='rdo', slider='sld', progressbar='prg', scrollbar='scr', toolbar='tlb',
    statusbar='stb', titlebar='ttl', splitbutton='spl', separator='sep', document='doc',
    header='hdr', headeritem='hdi', tooltip='tip', thumb='thm', table='tbl')
_PREFIXES = frozenset(_PREFIX.values()) | {'elm'}
_RUNTIME_SLUG = re.compile(r'([a-z]{2,3})-(?:[a-z0-9]{1,15}-)?[0-9a-f]{4}\Z')


def _window(hwnd):
    if type(hwnd) is not int or not 0 < hwnd <= 0xFFFFFFFFFFFFFFFF:
        raise ComputerError('winapp_window_invalid')
    return str(hwnd)


def _slug(value):
    if type(value) is not str or not _SLUG.fullmatch(value):
        raise ComputerError('winapp_selector_invalid')
    return value


def _runtime_slug(value):
    match = _RUNTIME_SLUG.fullmatch(value) if type(value) is str else None
    if not match or match.group(1) not in _PREFIXES:
        raise ComputerError('winapp_runtime_slug_invalid')
    return value


def _native_slug(control):
    runtime = control.get('runtime_id')
    if (type(runtime) is not list or not runtime or len(runtime) > 64
            or any(type(item) is not int or not -(2**31) <= item < 2**31 for item in runtime)
            or any(type(control.get(k)) is not str for k in ('type', 'name', 'automation_id'))):
        raise ComputerError('winapp_native_identity_invalid')
    def normalized(value):
        return re.sub('[^a-z0-9]', '', value.lower())[:15]
    name = normalized(control['automation_id']) or normalized(control['name'])
    hashed = 17
    for item in runtime:
        hashed = (hashed * 31 + item) & 0xffffffff
    prefix = _PREFIX.get(control['type'].lower(), 'elm')
    return _runtime_slug(f"{prefix}-" + (f"{name}-" if name else '') + f"{hashed & 0xffff:04x}")


def _decode(data):
    try:
        # json.loads rejects banners, trailing output, and malformed UTF-8.
        return json.loads(data.decode('utf-8-sig'))
    except (UnicodeError, ValueError, RecursionError):
        raise ComputerError('winapp_json_invalid') from None


def _tree(payload, hwnd):
    if type(payload) is not dict or type(payload.get('windows')) is not list or len(payload['windows']) != 1:
        raise ComputerError('winapp_inspect_invalid')
    window = payload['windows'][0]
    if type(window) is not dict or window.get('hwnd') != hwnd or type(window.get('elements')) is not list:
        raise ComputerError('winapp_window_mismatch')
    stack = [(item, 0) for item in window['elements']]
    nodes = []
    while stack:
        node, depth = stack.pop()
        if type(node) is not dict or depth > 25 or len(nodes) >= MAX_NODES:
            raise ComputerError('winapp_tree_invalid')
        if node.get('hasMoreChildren', False) is not False:
            raise ComputerError('winapp_tree_truncated')
        if type(node.get('type')) is not str or type(node.get('name', '')) is not str:
            raise ComputerError('winapp_tree_invalid')
        if type(node.get('isEnabled')) is not bool or type(node.get('isOffscreen')) is not bool:
            raise ComputerError('winapp_tree_invalid')
        children = node.get('children', [])
        if type(children) is not list:
            raise ComputerError('winapp_tree_invalid')
        nodes.append(node)
        stack.extend((child, depth + 1) for child in children)
    return nodes


def exact_element(payload, hwnd, *, name, control_type):
    """Return a unique exact named element; visibility flags are untrusted hints."""
    _window(hwnd)
    if any(type(v) is not str or not v or len(v) > 300 for v in (name, control_type)):
        raise ComputerError('winapp_target_invalid')
    matches = [n for n in _tree(payload, hwnd) if n.get('name') == name and n['type'] == control_type]
    if len(matches) != 1:
        raise ComputerError('winapp_target_not_unique')
    _slug(matches[0].get('selector'))
    return matches[0]


def property_result(payload, element, requested=None, *, allow_selector_change=False):
    """Validate target echo and typed property map; unavailable remains None."""
    if (type(payload) is not dict or type(element) is not dict
            or type(payload.get('element')) is not dict or type(payload.get('properties')) is not dict):
        raise ComputerError('winapp_property_invalid')
    echoed = payload['element']
    echoed_selector = _slug(echoed.get('selector'))
    if (payload.get('elementId') != echoed_selector
            or (not allow_selector_change and echoed_selector != element.get('selector'))):
        raise ComputerError('winapp_property_invalid')
    if any(echoed.get(k) != element.get(k) for k in ('type', 'name', 'className',
            'x', 'y', 'width', 'height', 'isEnabled', 'isOffscreen')):
        raise ComputerError('winapp_target_changed')
    properties = payload['properties']
    if any(type(k) is not str or (v is not None and type(v) is not str) for k, v in properties.items()):
        raise ComputerError('winapp_property_invalid')
    if requested is not None and requested not in properties:
        raise ComputerError('winapp_property_missing')
    return dict(properties)


def boolean_property(properties, name):
    value = properties.get(name)
    if value not in ('True', 'False'):
        raise ComputerError('winapp_property_unavailable')
    return value == 'True'


def _command(args):
    """Fail closed even if an internal caller tries to bypass public methods."""
    if type(args) is not list or any(type(x) is not str for x in args):
        raise ComputerError('winapp_command_denied')
    if len(args) == 7 and args[:3] == ['ui', 'inspect', '--window'] and args[4:] == ['--depth', '25', '--json']:
        hwnd = args[3]
    elif len(args) in (6, 8) and args[:2] == ['ui', 'get-property'] and args[3] == '--window' and args[5] == '--json':
        _slug(args[2]); hwnd = args[4]
        if len(args) == 8 and (args[6] != '--property' or args[7] not in PROPERTIES):
            raise ComputerError('winapp_command_denied')
    elif len(args) == 8 and args[:2] == ['ui', 'invoke'] and args[3] == '--window' and args[5] == '--action' and args[7] == '--json':
        _runtime_slug(args[2]); hwnd = args[4]
        if args[6] not in {'invoke', 'select', 'expand', 'collapse', 'toggle-on', 'toggle-off'}:
            raise ComputerError('winapp_command_denied')
    else:
        raise ComputerError('winapp_command_denied')
    if not hwnd.isascii() or not hwnd.isdecimal() or str(int(hwnd)) != hwnd:
        raise ComputerError('winapp_window_invalid')
    _window(int(hwnd))


def _without_values(value):
    """Remove value-bearing JSON fields at every depth before persistence."""
    if type(value) is dict:
        return {key: _without_values(item) for key, item in value.items()
                if type(key) is str and 'value' not in key.casefold()}
    if type(value) is list:
        return [_without_values(item) for item in value]
    return value


class WinAppReader:
    def __init__(self, evidence_parent, *, timeout=15):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ComputerError('winapp_timeout_invalid')
        self.parent = safe_root(evidence_parent)
        self.timeout = timeout

    def _run(self, args):
        # Public methods construct only the fixed scoped commands below.
        _command(args)
        executable = safe_root(EXE)
        if not executable.is_file() or sha256(executable.read_bytes()).hexdigest() != SHA256:
            raise ComputerError('winapp_binary_mismatch')
        for name, expected in DLL_HASHES.items():
            library = safe_root(executable.parent / name)
            if not library.is_file() or sha256(library.read_bytes()).hexdigest() != expected:
                raise ComputerError('winapp_binary_mismatch')
        self.parent.mkdir(parents=True, exist_ok=True)
        root = self.parent / ('winapp-' + uuid.uuid4().hex)
        root.mkdir(exist_ok=False)
        argv = [str(executable), *args]
        write_new(root / 'command.json', {'argv': argv, 'provider_binding': PROVIDER_BINDING,
                  'telemetry_optout': True, 'timeout_seconds': self.timeout,
                  'read_only': args[1] in {'inspect', 'get-property'}})
        env = os.environ.copy()
        env['WINAPP_CLI_TELEMETRY_OPTOUT'] = '1'
        env['DOTNET_CLI_TELEMETRY_OPTOUT'] = '1'
        env.pop('WINAPP_UI_WORKFLOW_ID', None)
        process = None
        job = None
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        started = time.monotonic()
        status = 'start_failed'
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=ROOT, env=env, shell=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            job = WindowsJob(process)
            def drain(pipe, output):
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        return
                    remaining = LIMIT - len(output)
                    output.extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        overflow.set()
                        return
            threads = [threading.Thread(target=drain, args=(pipe, out), daemon=True)
                       for pipe, out in zip((process.stdout, process.stderr), buffers)]
            for thread in threads:
                thread.start()
            while process.poll() is None and not overflow.is_set() and time.monotonic() - started < self.timeout:
                time.sleep(.02)
            status = ('output_limit' if overflow.is_set() else 'timeout' if process.poll() is None
                      else 'ok' if process.returncode == 0 else 'exit_error')
        finally:
            if job:
                job.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for thread in locals().get('threads', []):
                    thread.join(timeout=5)
                for pipe in (process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
            if overflow.is_set():
                status = 'output_limit'
            write_new(root / 'result.json', {'status': status,
                'exit_code': process.returncode if process is not None else None,
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'stdout_bytes': len(buffers[0]), 'stderr_bytes': len(buffers[1]),
                'stdout_sha256': sha256(buffers[0]).hexdigest(),
                'stderr_sha256': sha256(buffers[1]).hexdigest(),
                'output_complete': status in {'ok', 'exit_error'}})
        if status != 'ok':
            raise ComputerError('winapp_' + status)
        try:
            payload = _decode(bytes(buffers[0]))
        except ComputerError:
            write_new(root / 'parse.json', {'valid_json': False})
            raise
        write_new(root / 'stdout.json', _without_values(payload))
        write_new(root / 'parse.json', {'valid_json': True})
        return payload

    def inspect(self, hwnd):
        payload = self._run(['ui', 'inspect', '--window', _window(hwnd), '--depth', '25', '--json'])
        _tree(payload, hwnd)
        return payload

    def get_property(self, hwnd, element, *, name=None, canonical=False):
        if type(element) is not dict:
            raise ComputerError('winapp_target_invalid')
        slug = _slug(element.get('selector'))
        if name is not None and name not in PROPERTIES:
            raise ComputerError('winapp_property_denied')
        args = ['ui', 'get-property', slug, '--window', _window(hwnd), '--json']
        if name is not None:
            args += ['--property', name]
        result = self._run(args)
        properties = property_result(result, element, name, allow_selector_change=canonical)
        return (properties, result['element']) if canonical else properties

    def bind(self, hwnd, control, wrapper, *, action=None):
        """Cross-check a complete CLI tree against the already guarded UIA node."""
        rect = wrapper.rectangle()
        expected = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        if any(type(v) is not int for v in expected) or expected[2] <= 0 or expected[3] <= 0:
            raise ComputerError('winapp_bounds_invalid')
        # pywinauto projects COM failures and null to ''. Read the underlying
        # UIA property directly so only a real empty string counts as empty.
        try:
            class_name = wrapper.element_info.element.CurrentClassName
        except Exception:
            raise ComputerError('winapp_class_unavailable') from None
        if type(class_name) is not str:
            raise ComputerError('winapp_class_unavailable')
        nodes = _tree(self.inspect(hwnd), hwnd)
        matches = [n for n in nodes if n.get('type') == control['type']
                   and n.get('name', '') == control['name'] and n.get('className', '') == class_name
                   and tuple(n.get(k) for k in ('x', 'y', 'width', 'height')) == expected]
        if len(matches) != 1:
            raise ComputerError('winapp_target_not_unique')
        element = matches[0]
        slug = _slug(element.get('selector'))
        if sum(n.get('selector') == slug for n in nodes) != 1:
            raise ComputerError('winapp_target_not_unique')
        props, canonical = self.get_property(hwnd, element, canonical=True)
        if canonical.get('selector') != _native_slug(control):
            raise ComputerError('winapp_native_identity_mismatch')
        # inspect may use AutomationId while get-property returns a generated
        # slug. Re-resolve that slug and demand an identical target and fields.
        if self.get_property(hwnd, canonical) != props:
            raise ComputerError('winapp_target_changed')
        automation_id = props.get('AutomationId')
        if automation_id is None and 'AutomationId' in props:
            automation_id = ''
        # UIA's empty ClassName is omitted from inspect and projected as null
        # by get-property on some controls. Absence of the property key still
        # fails closed, and a nonempty native class must match exactly.
        projected_class = props.get('ClassName')
        if projected_class is None and 'ClassName' in props:
            projected_class = ''
        if (props.get('IsEnabled') != 'True'
                or props.get('IsOffscreen') != 'False'
                or 'AutomationId' not in props or automation_id != control['automation_id']
                or props.get('Name') != control['name'] or props.get('ControlType') != control['type']
                or 'ClassName' not in props or projected_class != class_name):
            raise ComputerError('winapp_target_changed')
        return canonical

    def perform_bound(self, hwnd, element, action, value, *, office=False):
        """Invoke once; any exception after the native intent means unknown effect."""
        slug = _runtime_slug(element.get('selector'))
        if action not in {'select', 'expand', 'collapse', 'toggle'} and not (office and action == 'invoke'):
            raise ComputerError('winapp_action_denied')
        desired = ('toggle-on' if value == '1' else 'toggle-off') if action == 'toggle' else action
        if action == 'toggle' and value not in {'0', '1'}:
            raise ComputerError('winapp_action_denied')
        args = ['ui', 'invoke', slug, '--window', _window(hwnd), '--action', desired, '--json']
        result = self._run(args)
        if type(result) is not dict:
            raise ComputerError('winapp_action_result_invalid')
        performed = 'toggle' if action == 'toggle' else desired
        if (result.get('elementId') != slug or type(result.get('hwnd')) is not int or result.get('hwnd') != hwnd
                or result.get('requestedAction') != desired or result.get('performedAction') != performed
                or (action == 'toggle' and result.get('pattern') != 'TogglePattern')):
            raise ComputerError('winapp_action_result_invalid')
        return result
