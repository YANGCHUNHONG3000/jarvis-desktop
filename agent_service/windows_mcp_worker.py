"""One owned stdio MCP session. Never a generic tool or arbitrary-command gateway."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from .computer import ComputerError, digest, write_new
from .windows_mcp import TOOLS, LIMIT, environment, match_snapshot, validate_action


def publish(root, name, value):
    pending = root / (name + '.pending')
    write_new(pending, value)
    os.rename(pending, root / name)


def text_result(result):
    if result.is_error:
        raise ComputerError('windows_mcp_tool_failed')
    texts = []
    for block in result.content:
        if block.type != 'text':
            raise ComputerError('windows_mcp_unexpected_image')
        item = block.text
        # FastMCP serializes a list[str] result into one JSON TextContent block.
        if item.lstrip().startswith('['):
            try:
                decoded = json.loads(item)
            except (ValueError, TypeError):
                decoded = None
            if type(decoded) is list:
                if not decoded or any(type(part) is not str for part in decoded):
                    raise ComputerError('windows_mcp_unexpected_content')
                item = '\n'.join(decoded)
        texts.append(item)
    text = '\n'.join(texts)
    if len(text) > LIMIT:
        raise ComputerError('windows_mcp_output_limit')
    return text


def point_guard(hwnd, point):
    import win32gui
    import win32api
    if win32gui.GetForegroundWindow() != hwnd:
        raise ComputerError('windows_mcp_foreground_changed')
    actual = win32gui.WindowFromPoint(tuple(point))
    if actual != hwnd and not win32gui.IsChild(hwnd, actual):
        raise ComputerError('windows_mcp_point_obscured')
    if any(win32api.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 4, 16, 17, 18, 91, 92)):
        raise ComputerError('windows_input_busy')


async def run(root):
    from importlib.metadata import version
    if (sys.version_info[:3] != (3, 14, 6) or version('mcp') != '2.3.0'
            or version('fastmcp') != '4.1.6' or version('windows-mcp') != '0.8.8'):
        raise ComputerError('windows_mcp_runtime_version_mismatch')
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    request = json.loads((root / 'request.json').read_text(encoding='utf-8'))
    control, hwnd = request['control'], request['hwnd']
    args = ['-B', '-m', 'windows_mcp', 'serve', '--transport', 'stdio', '--no-control',
            '--config', str(root / 'server.toml'), '--tools', ','.join(TOOLS)]
    parameters = StdioServerParameters(command=sys.executable, args=args, env=environment(root), cwd=str(root))
    with (root / 'server.log').open('w', encoding='utf-8') as log:
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = sorted(t.name for t in listed.tools)
                write_new(root / 'server.json', {'command': sys.executable, 'args': args, 'tools': names,
                          'telemetry': False, 'control': False, 'transport': 'stdio'})
                if names != sorted(TOOLS):
                    raise ComputerError('windows_mcp_tool_allowlist_mismatch')

                async def call(tool, arguments, filename):
                    before = time.time()
                    result = await session.call_tool(tool, arguments)
                    text = text_result(result)
                    if tool == 'Snapshot' and 'UI Tree:' in text:
                        # Other desktop window titles are irrelevant to this scoped adapter.
                        text = 'UI Tree:' + text.split('UI Tree:', 1)[1]
                    write_new(root / filename, {'tool': tool, 'arguments': arguments, 'text': text,
                              'started_at': before, 'finished_at': time.time()})
                    return text

                import win32gui
                title = win32gui.GetWindowText(hwnd)
                # Local wait is only readiness evidence, never proof of the requested effect.
                await call('WaitFor', {'condition': 'element_exists', 'text': control['name'],
                           'window_name': title, 'timeout': 3.0, 'interval': .2}, 'wait.json')
                snapshot = await call('Snapshot', {'use_vision': False, 'use_dom': False,
                    'use_annotation': False, 'use_ui_tree': True,
                    'region': list(win32gui.GetWindowRect(hwnd))}, 'snapshot.json')
                point = match_snapshot(snapshot, control)
                point_guard(hwnd, point)
                publish(root, 'ready.json', {'ok': True, 'snapshot': snapshot,
                                            'request_sha256': digest(request)})
                # Parent rechecks native identity and journals intent before releasing this byte.
                if await asyncio.to_thread(sys.stdin.buffer.read, 1) != b'2':
                    raise ComputerError('windows_mcp_dispatch_cancelled')
                dispatch = json.loads((root / 'dispatch.json').read_text(encoding='utf-8'))
                binding = json.loads((root / 'binding.json').read_text(encoding='utf-8'))
                if (dispatch['binding_sha256'] != digest(binding) or dispatch['point'] != point
                        or dispatch['hwnd'] != hwnd or dispatch['action'] != request['action']):
                    raise ComputerError('windows_mcp_binding_changed')
                validate_action(control, dispatch['action'], dispatch['value'])
                point_guard(hwnd, point)
                tool, arguments = 'Click', {'loc': point, 'button': 'left', 'clicks': 1}
                write_new(root / 'mcp-intent.json', {'tool': tool, 'arguments': arguments,
                          'dispatch_sha256': digest(dispatch)})
                await call(tool, arguments, 'action.json')
                publish(root, 'result.json', {'ok': True, 'dispatch_sha256': digest(dispatch),
                                             'effects': 'dispatched_not_verified'})


def main():
    root = Path(sys.argv[1]).resolve()
    if sys.stdin.buffer.read(1) != b'1':
        return
    try:
        asyncio.run(run(root))
    except Exception as exc:
        import traceback
        # Local diagnostics only: no credential environment is inherited by this worker.
        (root / 'failure.txt').write_text(''.join(traceback.format_exception(exc)), encoding='utf-8')
        def code(e):
            if isinstance(e, ComputerError):
                return str(e)
            for child in getattr(e, 'exceptions', ()):
                found = code(child)
                if found != 'windows_mcp_failed':
                    return found
            return 'windows_mcp_failed'
        publish(root, 'error.json', {'ok': False, 'code': code(exc), 'error_type': type(exc).__name__})


if __name__ == '__main__':
    main()
