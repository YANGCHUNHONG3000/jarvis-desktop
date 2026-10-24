"""Fixed offline snapshots and API doubles; never observes the real desktop."""

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from agent_service import office_readiness as readiness


def _snapshot(executable="wps.exe", enabled=True, hung=False, **metadata):
    values = {"class_name": "OpusApp", "style": 0x16CF0000,
              "ex_style": 0x100, "has_owner": False}
    values.update(metadata)
    return readiness.WindowSnapshot(executable, enabled, hung, **values)


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.call_count = 0

    def __call__(self, *arguments):
        self.call_count += 1
        return self.implementation(*arguments)


def _api(monkeypatch, *, executable="wps.exe", enabled=True, hung=False,
         visible=True, enum_ok=True, query_ok=True, open_ok=True, close_ok=True,
         disappear=False, callback_error=False):
    closed = []
    window_checks = []
    identity_queries = []
    handle = 0x100000001

    def is_window(hwnd):
        window_checks.append(hwnd)
        return not disappear or len(window_checks) == 1

    def process_id(hwnd, pointer):
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = 42
        return 1

    def query(process, flags, buffer, pointer):
        identity_queries.append(process)
        value = "C:\\Office\\" + executable
        buffer.value = value
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = len(value)
        return query_ok

    def enum(callback, parameter):
        callback(handle, parameter)
        return enum_ok

    def enabled_flag(hwnd):
        if callback_error:
            raise RuntimeError("offline callback failure")
        return enabled

    user32 = SimpleNamespace(
        EnumWindows=Function(enum), IsWindow=Function(is_window),
        IsWindowVisible=Function(lambda hwnd: visible),
        IsWindowEnabled=Function(enabled_flag), IsHungAppWindow=Function(lambda hwnd: hung),
        GetWindowThreadProcessId=Function(process_id),
    )
    def class_name(hwnd, buffer, size):
        buffer.value = "OpusApp"
        return len(buffer.value)
    user32.GetClassNameW = Function(class_name)
    get_style = Function(lambda hwnd, index: 0x16CF0000 if index == -16 else 0x100)
    user32.GetWindowLongPtrW = get_style
    user32.GetWindowLongW = get_style
    user32.GetWindow = Function(lambda hwnd, relation: 0)
    # Last-error APIs do not exist on non-Windows Python; keep tests offline.
    monkeypatch.setattr(ctypes, "set_last_error", lambda code: None, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    kernel32 = SimpleNamespace(
        OpenProcess=Function(lambda *args: handle if open_ok else 0),
        QueryFullProcessImageNameW=Function(query),
        CloseHandle=Function(lambda process: closed.append(process) or close_ok),
    )
    monkeypatch.setattr(readiness.sys, "platform", "win32")
    monkeypatch.setattr(readiness, "_load_windows_api", lambda: (user32, kernel32, lambda f: f))
    return closed, identity_queries, user32, kernel32


def test_empty_and_clear_office_snapshots():
    assert readiness.classify_office_windows([]) is None
    rows = [_snapshot(name, True, False) for name in readiness.OFFICE_EXECUTABLES]
    assert readiness.classify_office_windows(rows) is None


def test_disabled_or_hung_known_office_is_blocked():
    for name in readiness.OFFICE_EXECUTABLES:
        for enabled, hung in ((False, False), (True, True)):
            assert readiness.classify_office_windows([
                _snapshot(name.upper(), enabled, hung)
            ]) == readiness.BLOCKED
    assert readiness.classify_office_windows([
        _snapshot("unrelated.exe", False, True)
    ]) is None


def test_missing_or_malformed_snapshot_never_passes():
    for rows in (None, {}, [None], [_snapshot("wps.exe", None, False)],
                 [_snapshot("C:\\wps.exe", True, False)],
                 [_snapshot("", True, False)]):
        assert readiness.classify_office_windows(rows) == readiness.UNAVAILABLE


def test_non_windows_is_unavailable_without_probe(monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setattr(readiness, "_observe_windows", lambda: pytest.fail("must not probe"))
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE


def test_windows_clear_probe_closes_pointer_sized_handle(monkeypatch):
    closed, queries, _, _ = _api(monkeypatch, executable="EXCEL.EXE")
    assert readiness.preflight_office_window() is None
    assert closed == queries == [0x100000001]


def test_hidden_windows_are_not_identified(monkeypatch):
    closed, queries, _, _ = _api(monkeypatch, visible=False, enabled=False)
    assert readiness.preflight_office_window() is None
    assert closed == queries == []


def test_non_office_disabled_window_is_clear(monkeypatch):
    _api(monkeypatch, executable="other.exe", enabled=False, hung=True)
    assert readiness.preflight_office_window() is None


def test_office_window_flags_block(monkeypatch):
    _api(monkeypatch, enabled=False)
    assert readiness.preflight_office_window() == readiness.BLOCKED
    _api(monkeypatch, hung=True)
    assert readiness.preflight_office_window() == readiness.BLOCKED


def test_identity_failure_always_closes_handle(monkeypatch):
    closed, _, _, _ = _api(monkeypatch, query_ok=False)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    assert closed == [0x100000001]


def test_permission_denied_and_close_failure_are_unavailable(monkeypatch):
    closed, _, _, _ = _api(monkeypatch, open_ok=False)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    assert closed == []
    _api(monkeypatch, close_ok=False)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE


def test_disappeared_window_and_callback_exception_are_unavailable(monkeypatch):
    _api(monkeypatch, disappear=True)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    _api(monkeypatch, visible=False, disappear=True)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    _api(monkeypatch, callback_error=True)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE


def test_enumeration_failure_and_missing_observation_are_unavailable(monkeypatch):
    _api(monkeypatch, enum_ok=False)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    monkeypatch.setattr(readiness, "_observe_windows", lambda: None)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE
    def unavailable_api():
        raise OSError("offline API failure")
    monkeypatch.setattr(readiness, "_observe_windows", unavailable_api)
    assert readiness.preflight_office_window() == readiness.UNAVAILABLE


def test_windows_api_declares_pointer_sized_types(monkeypatch):
    # Exercise the production ABI declarations while keeping every API offline.
    _, _, user32, kernel32 = _api(monkeypatch)
    monkeypatch.undo()
    user_functions = user32
    kernel_functions = kernel32
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs:
                        user_functions if name == "user32" else kernel_functions, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    user32, kernel32, callback_type = readiness._load_windows_api()
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert user32.EnumWindows.argtypes == [callback_type, ctypes.c_ssize_t]
    assert user32.GetWindowThreadProcessId.argtypes[0] is wintypes.HWND
    assert ctypes.sizeof(wintypes.HWND) == ctypes.sizeof(ctypes.c_void_p)
    assert kernel32.QueryFullProcessImageNameW.argtypes[0] is wintypes.HANDLE


def _diagnostic_api(monkeypatch, *, class_ok=True):
    _, _, user32, _ = _api(monkeypatch, enabled=False)

    def class_name(hwnd, buffer, size):
        buffer.value = "SoPY_Status"
        return len(buffer.value) if class_ok else 0

    user32.GetClassNameW = Function(class_name)
    get_style = Function(lambda hwnd, index: 0x9C000000 if index == -16 else 0x08080088)
    user32.GetWindowLongPtrW = get_style
    user32.GetWindowLongW = get_style
    user32.GetWindow = Function(lambda hwnd, relation: 0)
    # Last-error APIs do not exist on non-Windows Python; keep these tests offline.
    monkeypatch.setattr(ctypes, "set_last_error", lambda code: None, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    return user32


def test_explicit_local_diagnostic_returns_only_window_metadata(monkeypatch):
    user32 = _diagnostic_api(monkeypatch)
    assert readiness._diagnose_office_windows() == [{
        "hwnd": 0x100000001, "executable": "wps.exe", "enabled": False, "hung": False,
        "class_name": "SoPY_Status", "style": 0x9C000000, "ex_style": 0x08080088,
        "has_owner": False,
    }]
    assert user32.GetWindow.restype is wintypes.HWND
    assert user32.GetWindowLongPtrW.restype is ctypes.c_ssize_t
    assert readiness.preflight_office_window() is None


def test_local_diagnostic_failure_never_returns_partial_snapshot(monkeypatch):
    _diagnostic_api(monkeypatch, class_ok=False)
    with pytest.raises(OSError, match="window_enumeration_unavailable"):
        readiness._diagnose_office_windows()


@pytest.mark.parametrize("executable", ["wps.exe", "et.exe"])
def test_known_wps_12_1_status_signature_is_the_only_disabled_exception(executable):
    signature = {"class_name": "SoPY_Status", "style": 0x9C000000,
                 "ex_style": 0x08080088, "has_owner": False}
    window = _snapshot(executable=executable, enabled=False, **signature)
    assert readiness.classify_office_windows([window]) is None
    for changes in ({"executable": "winword.exe"},
                    {"executable": "excel.exe"}, {"class_name": "Other"},
                    {"class_name": "sopy_status"}, {"has_owner": True},
                    {"style": 0x9C000001}, {"style": 0x94000000},
                    {"ex_style": 0x08080089}, {"ex_style": 0x08000088},
                    {"hung": True}):
        values = dict(signature, executable=executable, enabled=False, hung=False)
        values.update(changes)
        changed = _snapshot(**values)
        assert not readiness._is_known_wps_status_window(changed), changes
        assert readiness.classify_office_windows([changed]) == readiness.BLOCKED, changes
    enabled = _snapshot(executable=executable, enabled=True, **signature)
    assert not readiness._is_known_wps_status_window(enabled)
    assert readiness.classify_office_windows([enabled]) is None
    # An exempt helper cannot hide a blocked document window in the same snapshot.
    assert readiness.classify_office_windows([
        window, _snapshot(enabled=False)
    ]) == readiness.BLOCKED


def test_production_uses_one_native_enumeration_for_flags_and_metadata(monkeypatch):
    user32 = _diagnostic_api(monkeypatch)
    assert readiness.preflight_office_window() is None
    assert user32.EnumWindows.call_count == 1
    assert user32.GetClassNameW.call_count == 1
    assert user32.GetWindowLongPtrW.call_count == 2
    assert user32.GetWindow.call_count == 1


def test_metadata_failures_block_production_without_broad_style_exemptions(monkeypatch):
    for failed in ("class", "style", "owner"):
        user32 = _diagnostic_api(monkeypatch, class_ok=failed != "class")
        if failed == "style":
            user32.GetWindowLongPtrW = user32.GetWindowLongW = Function(lambda *args: 0)
        if failed in {"style", "owner"}:
            monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
        assert readiness.preflight_office_window() == readiness.UNAVAILABLE, failed
    signature = {"class_name": "SoPY_Status", "style": 0x9C000000,
                 "ex_style": 0x08080088, "has_owner": False}
    for changes in ({"class_name": None}, {"class_name": ""}, {"style": None},
                    {"ex_style": True}, {"has_owner": None}):
        values = dict(signature)
        values.update(changes)
        assert readiness.classify_office_windows([
            _snapshot(enabled=False, **values)
        ]) == readiness.UNAVAILABLE
