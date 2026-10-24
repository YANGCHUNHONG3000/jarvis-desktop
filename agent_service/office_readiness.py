"""Read-only native Office window snapshot before dispatching a new step.

A clear snapshot is not a readiness guarantee. No titles, document contents,
messages, COM calls or UI input are used here.
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ntpath
import sys


OFFICE_EXECUTABLES = frozenset({"wps.exe", "et.exe", "winword.exe", "excel.exe"})
BLOCKED = "office_ui_blocked"
UNAVAILABLE = "office_ui_unavailable"


@dataclass(frozen=True)
class WindowSnapshot:
    executable: str
    enabled: bool
    hung: bool
    class_name: str
    style: int
    ex_style: int
    has_owner: bool


def _is_known_wps_status_window(window):
    # Observed WPS 12.1 inactive status helper in wps.exe and et.exe.
    # This exact signature is the entire exception; NOACTIVATE or ownership
    # alone never bypasses the gate.
    return (window.executable.casefold() in {"wps.exe", "et.exe"}
            and window.class_name == "SoPY_Status"
            and window.enabled is False and window.hung is False
            and window.has_owner is False
            and window.style == 0x9C000000 and window.ex_style == 0x08080088)


def classify_office_windows(windows) -> str | None:
    """Classify a complete snapshot; missing/malformed observations never pass."""
    if not isinstance(windows, (list, tuple)):
        return UNAVAILABLE
    blocked = False
    for window in windows:
        if not isinstance(window, WindowSnapshot) or not isinstance(window.executable, str):
            return UNAVAILABLE
        if not window.executable or ntpath.basename(window.executable) != window.executable:
            return UNAVAILABLE
        if type(window.enabled) is not bool or type(window.hung) is not bool:
            return UNAVAILABLE
        if (not isinstance(window.class_name, str) or not window.class_name
                or type(window.style) is not int or not 0 <= window.style <= 0xFFFFFFFF
                or type(window.ex_style) is not int or not 0 <= window.ex_style <= 0xFFFFFFFF
                or type(window.has_owner) is not bool):
            return UNAVAILABLE
        if window.executable.casefold() in OFFICE_EXECUTABLES:
            if not _is_known_wps_status_window(window):
                blocked = blocked or not window.enabled or window.hung
    return BLOCKED if blocked else None


def _load_windows_api():
    """Declare every ABI explicitly, including pointer-sized HWND/LPARAM."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t)
    declarations = (
        (user32.EnumWindows, [callback_type, ctypes.c_ssize_t], wintypes.BOOL),
        (user32.IsWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
        (user32.IsWindowEnabled, [wintypes.HWND], wintypes.BOOL),
        (user32.IsHungAppWindow, [wintypes.HWND], wintypes.BOOL),
        (user32.GetWindowThreadProcessId,
         [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        (kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        (kernel32.QueryFullProcessImageNameW,
         [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
         wintypes.BOOL),
        (kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
    )
    for function, arguments, result in declarations:
        function.argtypes = arguments
        function.restype = result
    return user32, kernel32, callback_type


def _process_id(user32, hwnd):
    process_id = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) or not process_id.value:
        raise OSError("window_process_unavailable")
    return process_id.value


def _process_basename(kernel32, process_id):
    handle = kernel32.OpenProcess(0x1000, False, process_id)  # QUERY_LIMITED_INFORMATION
    if not handle:
        raise OSError("process_identity_unavailable")
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise OSError("process_identity_unavailable")
        if not 0 < size.value < len(buffer):
            raise OSError("process_identity_invalid")
        executable = ntpath.basename(buffer[:size.value]).casefold()
        if not executable or "\x00" in executable:
            raise OSError("process_identity_invalid")
        return executable
    finally:
        if not kernel32.CloseHandle(handle):
            raise OSError("process_handle_close_failed")


def _load_window_metadata_api(user32):
    """Read-only metadata for the narrowly identified WPS helper exception."""
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    get_style = (user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8
                 else user32.GetWindowLongW)
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    return get_style


def _window_metadata(user32, get_style, hwnd):
    class_buffer = ctypes.create_unicode_buffer(256)
    count = user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
    if not 0 < count < len(class_buffer) - 1:
        raise OSError("window_class_unavailable")
    styles = []
    for index in (-16, -20):  # GWL_STYLE, GWL_EXSTYLE
        ctypes.set_last_error(0)
        value = get_style(hwnd, index)
        if value == 0 and ctypes.get_last_error():
            raise OSError("window_style_unavailable")
        styles.append(value & 0xFFFFFFFF)
    ctypes.set_last_error(0)
    owner = user32.GetWindow(hwnd, 4)  # GW_OWNER
    if not owner and ctypes.get_last_error():
        raise OSError("window_owner_unavailable")
    return {"class_name": class_buffer.value, "style": styles[0], "ex_style": styles[1],
            "has_owner": bool(owner)}


def _observe_windows(*, _metadata=None):
    user32, kernel32, callback_type = _load_windows_api()
    get_style = _load_window_metadata_api(user32)
    windows = []
    errors = []

    def observe(hwnd, _parameter):
        # ctypes callbacks must catch exceptions themselves: escaping exceptions
        # are otherwise printed and ignored by ctypes, potentially appearing clear.
        try:
            if not user32.IsWindow(hwnd):
                raise OSError("window_disappeared")
            if not user32.IsWindowVisible(hwnd):
                if not user32.IsWindow(hwnd):
                    raise OSError("window_disappeared")
                return True
            process_id = _process_id(user32, hwnd)
            executable = _process_basename(kernel32, process_id)
            if executable in OFFICE_EXECUTABLES:
                enabled = bool(user32.IsWindowEnabled(hwnd))
                hung = bool(user32.IsHungAppWindow(hwnd))
                metadata = _window_metadata(user32, get_style, hwnd)
                windows.append(WindowSnapshot(executable, enabled, hung, **metadata))
                if _metadata is not None:
                    _metadata.append({"hwnd": int(hwnd), "executable": executable,
                                      "enabled": enabled, "hung": hung,
                                      **metadata})
            if not user32.IsWindow(hwnd) or _process_id(user32, hwnd) != process_id:
                raise OSError("window_changed")
            return True
        except Exception:
            errors.append(True)
            return False

    callback = callback_type(observe)
    if not user32.EnumWindows(callback, 0) or errors:
        raise OSError("window_enumeration_unavailable")
    return windows


def _diagnose_office_windows():
    """Explicit local metadata probe; never reads titles or document contents.

    This is not a production readiness decision and does not relax any gate.
    Failures raise rather than returning a partial diagnostic as complete.
    """
    if sys.platform != "win32":
        raise OSError("window_enumeration_unavailable")
    metadata = []
    _observe_windows(_metadata=metadata)
    return metadata


def preflight_office_window() -> str | None:
    """Return None only after a complete clear snapshot, otherwise a stable code."""
    if sys.platform != "win32":
        return UNAVAILABLE
    try:
        return classify_office_windows(_observe_windows())
    except Exception:
        # Only stable codes cross this boundary; paths or other identity details
        # never enter a task error or log.
        return UNAVAILABLE
