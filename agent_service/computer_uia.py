"""Bounded native UIA backend. No input simulation or automatic retries.

All Windows imports live inside calls, allowing contract tests on other hosts.
The caller owns deadlines, intent persistence and post-action verification.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time


MAX_NODES = 200
MAX_DEPTH = 12
MAX_TEXT = 20000
_BUTTONS = {"num%dButton" % n for n in range(10)} | {
    "plusButton", "minusButton", "multiplyButton", "divideButton", "equalButton",
    "decimalSeparatorButton", "negateButton", "clearButton", "clearEntryButton",
    "backSpaceButton", "percentButton", "invertButton", "reciprocalButton", "xpower2Button",
    "squareRootButton"}
_DENY_PROCESS = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                 "conhost.exe", "openconsole.exe", "bash.exe", "wsl.exe",
                 "credentialuibroker.exe", "consent.exe", "logonui.exe",
                 "applicationframehost.exe", "codex.exe", "chatgpt.exe",
                 "python.exe", "pythonw.exe", "mshta.exe", "regedit.exe",
                 "taskmgr.exe", "sechealthui.exe", "systemsettings.exe",
                 "microsoft.cmdpal.ui.exe", "workbuddy.exe", "textinputhost.exe"}
_DENY_TITLE = re.compile(
    r"terminal|powershell|command prompt|credential|password|sign.?in|log.?in|"
    r"authentication|windows security|user account control|codex|chatgpt|jarvis|"
    r"终端|命令提示符|密码|凭据|登录|身份验证|安全中心|用户账户控制|贾维斯", re.I)


class UIAError(RuntimeError):
    pass


@contextmanager
def _native():
    if os.name != "nt":
        raise UIAError("windows_required")
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _profile(image):
    """Mutation privileges require an actual Microsoft installation path."""
    normalized = os.path.normcase(os.path.abspath(image))
    system_root = os.environ.get("SystemRoot", "")
    program_files = os.environ.get("ProgramFiles", "")
    name = Path(image).name.casefold()
    def under(root, relative):
        return bool(root) and normalized.startswith(
            os.path.normcase(os.path.abspath(os.path.join(root, relative))))
    notepad = (bool(system_root) and normalized == os.path.normcase(
        os.path.abspath(os.path.join(system_root, "System32", "notepad.exe"))))
    notepad |= under(program_files, "WindowsApps/Microsoft.WindowsNotepad_")
    calculator = under(program_files, "WindowsApps/Microsoft.WindowsCalculator_")
    calculator |= under(system_root, "SystemApps/Microsoft.WindowsCalculator_")
    if notepad and name == "notepad.exe":
        return "notepad"
    if calculator and name in {"calculatorapp.exe", "calculator.exe", "calc.exe"}:
        return "calculator"
    if system_root and normalized == os.path.normcase(os.path.abspath(os.path.join(system_root, 'explorer.exe'))):
        return 'explorer'
    return "observe_only"


def _identity(hwnd):
    import ctypes
    from ctypes import wintypes
    import win32api
    import win32gui
    import win32process
    if type(hwnd) is not int or not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
        raise UIAError("window_unavailable")
    title = win32gui.GetWindowText(hwnd)
    class_name = win32gui.GetClassName(hwnd)
    if not title or len(title) > 300 or _DENY_TITLE.search(title):
        raise UIAError("window_denied")
    if class_name in {'Progman', 'WorkerW', 'Shell_TrayWnd', 'PseudoConsoleWindow'}:
        raise UIAError('window_denied')
    cloaked = wintypes.DWORD()
    try:
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            raise UIAError('window_cloaked')
    except AttributeError:
        pass
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    handle = win32api.OpenProcess(0x1000 | 0x100000, False, pid)
    try:
        query = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                          ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not query(int(handle), 0, buffer, ctypes.byref(length)):
            raise UIAError("process_image_unavailable")
        image = buffer.value
        created = str(win32process.GetProcessTimes(handle)["CreationTime"])
    finally:
        handle.Close()
    if Path(image).name.casefold() in _DENY_PROCESS or _DENY_TITLE.search(class_name):
        raise UIAError("window_denied")
    profile = _profile(image)
    if profile == 'notepad' and class_name.casefold() != 'notepad':
        profile = 'observe_only'
    if profile == 'explorer' and class_name != 'CabinetWClass':
        profile = 'observe_only'
    return dict(hwnd=hwnd, pid=pid, created=created, image=image,
                class_name=class_name, title=title, profile=profile)


def _recheck(identity):
    if not isinstance(identity, dict):
        raise UIAError("window_identity_invalid")
    fresh = _identity(identity.get("hwnd"))
    for key in ("hwnd", "pid", "created", "image", "class_name", "profile"):
        if fresh[key] != identity.get(key):
            raise UIAError("window_identity_changed")
    return fresh


def list_windows():
    with _native():
        import win32gui
        windows = []
        def visit(hwnd, unused):
            if len(windows) >= 200:
                return
            try:
                windows.append(_identity(hwnd))
            except Exception:
                # Classic UWP frames are owned by ApplicationFrameHost; select
                # only their actual visible application child, never the host.
                if win32gui.GetClassName(hwnd) == 'ApplicationFrameWindow':
                    def child_visit(child, _):
                        try:
                            if win32gui.GetClassName(child) == 'Windows.UI.Core.CoreWindow':
                                identity = _identity(child)
                                if identity['profile'] == 'calculator':
                                    windows.append(identity)
                        except Exception:
                            pass
                    win32gui.EnumChildWindows(hwnd, child_visit, None)
        win32gui.EnumWindows(visit, None)
        return windows


def _record(wrapper, pid):
    from pywinauto.uia_defines import NoPatternInterfaceError
    element = wrapper.element_info.element
    # Do not touch Name, Value, Text or children of protected nodes.
    if element.CurrentIsPassword or wrapper.element_info.process_id != pid:
        return None
    runtime_id = [int(n) for n in element.GetRuntimeId()]
    name = wrapper.element_info.name or ""
    automation_id = wrapper.element_info.automation_id or ""
    if len(name) > 300 or len(automation_id) > 300:
        raise UIAError("control_text_limit")
    patterns = ["focus"] if (element.CurrentIsKeyboardFocusable
                             or wrapper.element_info.control_type == "Window") else []
    value = None
    selected = expanded = None
    try:
        pattern = wrapper.iface_value
    except NoPatternInterfaceError:
        try:
            value = wrapper.iface_text.DocumentRange.GetText(4001)
        except NoPatternInterfaceError:
            pass
    else:
        value = pattern.CurrentValue
        if not pattern.CurrentIsReadOnly:
            patterns.append("set_value")
    if value is not None and (not isinstance(value, str) or len(value) > 4000):
        raise UIAError("control_value_limit")
    for interface, action in (("iface_invoke", "invoke"), ("iface_selection_item", "select"),
                              ("iface_expand_collapse", "expand")):
        try:
            pattern = getattr(wrapper, interface)
        except NoPatternInterfaceError:
            continue
        patterns.append(action)
        if action == "select":
            selected = bool(pattern.CurrentIsSelected)
        elif action == "expand":
            patterns.append("collapse")
            expanded = int(pattern.CurrentExpandCollapseState)
    return dict(id=sha256(json.dumps(runtime_id).encode()).hexdigest()[:24],
                runtime_id=runtime_id, type=wrapper.element_info.control_type,
                name=name, automation_id=automation_id, value=value,
                enabled=bool(element.CurrentIsEnabled), visible=not bool(element.CurrentIsOffscreen),
                patterns=patterns, focused=bool(element.CurrentHasKeyboardFocus),
                selected=selected, expanded=expanded)


def _observe(identity, check=lambda: None):
    from pywinauto import Desktop
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    fresh = _recheck(identity)
    root = Desktop(backend="uia").window(handle=fresh["hwnd"]).wrapper_object()
    walker = IUIA().iuia.ControlViewWalker
    controls, wrappers = [], {}
    count = text_size = 0
    truncated = False
    def visit(element, depth):
        nonlocal count, text_size, truncated
        check()
        if count >= MAX_NODES:
            truncated = True
            return
        count += 1
        wrapper = UIAWrapper(UIAElementInfo(element))
        try:
            record = _record(wrapper, fresh["pid"])
        except UIAError as exc:
            if str(exc) not in {"control_text_limit", "control_value_limit"}:
                raise
            truncated = True
            return
        if record is None:
            return
        size = len(record["name"]) + len(record["automation_id"]) + len(record["value"] or "")
        if text_size + size > MAX_TEXT:
            truncated = True
            return
        text_size += size
        if record["id"] in wrappers:
            raise UIAError("ambiguous_control")
        controls.append(record)
        if depth == 0 and record['type'] == 'Window':
            import win32gui
            record['focused'] = win32gui.GetForegroundWindow() == win32gui.GetAncestor(fresh['hwnd'], 2)
        wrappers[record["id"]] = wrapper
        if depth >= MAX_DEPTH:
            truncated = True
            return
        child = walker.GetFirstChildElement(element)
        while child:
            if count >= MAX_NODES:
                truncated = True
                break
            visit(child, depth + 1)
            check()
            child = walker.GetNextSiblingElement(child)
    visit(root.element_info.element, 0)
    _recheck(identity)
    return dict(window=fresh, controls=controls, truncated=truncated), wrappers


def observe(identity):
    with _native():
        return _observe(identity)[0]


def perform(identity, expected_control, action, value, check):
    """Perform a single permitted native call; no retry or success claim."""
    if not callable(check) or not isinstance(expected_control, dict):
        raise UIAError("action_invalid")
    check()
    with _native():
        import win32api
        input_epoch = win32api.GetLastInputInfo()
        snapshot, wrappers = _observe(identity, check)
        matches = [r for r in snapshot["controls"] if r["id"] == expected_control.get("id")]
        if len(matches) != 1:
            raise UIAError("control_unavailable")
        actual = matches[0]
        if ({k: v for k, v in actual.items() if k != "focused"}
                != {k: v for k, v in expected_control.items() if k != "focused"}):
            raise UIAError("control_changed")
        if not actual["visible"] or not actual["enabled"] or action not in actual["patterns"]:
            raise UIAError("control_not_actionable")
        profile = snapshot["window"]["profile"]
        allowed = action == "focus"
        allowed |= (profile == "calculator" and action == "invoke"
                    and actual["type"] == "Button" and actual["automation_id"] in _BUTTONS)
        allowed |= (profile == "notepad" and action == "set_value"
                    and actual["type"] in {"Edit", "Document"} and actual["value"] is not None)
        allowed |= (profile == 'notepad' and action == 'invoke' and actual['type'] == 'Button' and actual['automation_id'] == 'AddButton')
        allowed |= (profile == 'notepad' and action == 'select' and actual['type'] == 'TabItem')
        allowed |= (profile == 'explorer' and ((actual['type'] == 'TreeItem' and action in {'select', 'expand', 'collapse'}) or (actual['type'] == 'ListItem' and action == 'select')))
        if not allowed:
            raise UIAError("action_profile_denied")
        if action == "set_value":
            if (not isinstance(value, str) or len(value) > 4000
                    or any(ord(c) < 32 and c not in "\r\n\t" for c in value)):
                raise UIAError("action_value_invalid")
        elif value != '':
            raise UIAError("action_value_invalid")
        check()
        _recheck(identity)
        wrapper = wrappers[actual["id"]]
        fresh = _record(wrapper, identity["pid"])
        if fresh is None or {k: v for k, v in fresh.items() if k != "focused"} != {
                k: v for k, v in actual.items() if k != "focused"}:
            raise UIAError("control_changed_before_action")
        check()
        if win32api.GetLastInputInfo() != input_epoch:
            raise UIAError('user_takeover')
        if action == "focus":
            import win32gui
            top = win32gui.GetAncestor(identity['hwnd'], 2)
            if win32gui.GetForegroundWindow() != top:
                try:
                    win32gui.SetForegroundWindow(top)
                except Exception:
                    raise UIAError('window_foreground_denied') from None
            until = time.monotonic() + 2
            while win32gui.GetForegroundWindow() != top:
                check()
                if time.monotonic() >= until:
                    raise UIAError("window_not_foreground")
                time.sleep(.03)
            if actual["type"] != "Window":
                wrapper.element_info.element.SetFocus()
        elif action == "set_value":
            if wrapper.iface_value.CurrentIsReadOnly:
                raise UIAError("control_readonly")
            wrapper.iface_value.SetValue(value)
        elif action == "invoke":
            wrapper.iface_invoke.Invoke()
        elif action == 'select':
            wrapper.iface_selection_item.Select()
        elif action == 'expand':
            wrapper.iface_expand_collapse.Expand()
        elif action == 'collapse':
            wrapper.iface_expand_collapse.Collapse()
        else:
            raise UIAError("action_profile_denied")
