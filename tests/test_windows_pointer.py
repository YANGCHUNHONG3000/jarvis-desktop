"""Pointer primitives tested offline; no hook installation or native input."""
import ctypes
import struct
import threading
from types import SimpleNamespace

import pytest

from agent_service.computer import ComputerError
from agent_service import windows_input as inputs
from agent_service import windows_pointer as pointer


class FakeUser:
    def __init__(self):
        self.foreground, self.hit, self.root = 100, 101, 100
        self.held = set()
        self.dpi_calls = []
        self.metrics = {76: -1920, 77: -100, 78: 3840, 79: 1180}

    def GetSystemMetrics(self, key):
        return self.metrics[key]

    def GetForegroundWindow(self):
        return self.foreground

    def WindowFromPoint(self, point):
        assert (point.x, point.y) == (20, 30)
        return self.hit

    def GetAncestor(self, hwnd, flag):
        assert hwnd == self.hit and flag == 2
        return self.root

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.held else 0

    def SetThreadDpiAwarenessContext(self, context):
        self.dpi_calls.append(context)
        return 123


class FakeMonitor:
    latest = None

    def __init__(self, *args):
        self.seen = threading.Event()
        self.error = None
        self.phase = ''
        self.closed = False
        FakeMonitor.latest = self

    def start(self, timeout):
        pass

    def check(self):
        if self.error:
            raise ComputerError(self.error)

    def close(self):
        self.closed = True

    def diagnostic(self):
        return {'matched_pointer_events': 3 if self.seen.is_set() else 0}


@pytest.fixture
def environment(monkeypatch):
    user, calls = FakeUser(), []
    monkeypatch.setattr(pointer, '_pointer_api', lambda: (user, None, None))
    monkeypatch.setattr(pointer, '_PointerMonitor', FakeMonitor)
    monkeypatch.setattr(inputs, '_epoch', lambda user: 812)

    def inject(user, dx, dy, marker, *, release_only=False):
        calls.append({'dx': dx, 'dy': dy, 'marker': marker, 'release_only': release_only})
        FakeMonitor.latest.seen.set()
        return 1 if release_only else 3
    monkeypatch.setattr(pointer, '_inject', inject)
    return user, calls


def click(**options):
    return pointer.guarded_click(100, 20, 30, check=lambda: None, should_stop=lambda: False, **options)


def test_one_guarded_click_returns_epoch_and_restores_dpi(environment):
    user, calls = environment
    assert click() == 812
    assert len(calls) == 1 and calls[0]['marker'] > 0
    assert not calls[0]['release_only']
    assert FakeMonitor.latest.closed
    assert len(user.dpi_calls) == 2 and user.dpi_calls[-1] == 123


@pytest.mark.parametrize('random_value,expected', [(0, 1), (0x7FFFFFFF, 0x7FFFFFFF)])
def test_mouse_marker_is_nonzero_and_representable_without_cross_bitness_truncation(environment, monkeypatch,
                                                                                    random_value, expected):
    _, calls = environment
    def random(bits):
        assert bits == 31
        return random_value
    monkeypatch.setattr(pointer.secrets, 'randbits', random)
    assert click() == 812
    assert calls[0]['marker'] == expected


@pytest.mark.parametrize('held', [1, 2, 4, 5, 6, 0x10, 0x11, 0x41, 0xFF])
def test_every_keyboard_or_mouse_press_blocks_injection(environment, held):
    user, calls = environment
    user.held.add(held)
    with pytest.raises(ComputerError, match='windows_input_keys_held') as error:
        click()
    assert calls == [] and error.value.input_diagnostic['sent_events'] == 0
    assert FakeMonitor.latest.closed


@pytest.mark.parametrize('change,code', [('foreground', 'windows_input_foreground_changed'),
                                        ('root', 'windows_pointer_target_obscured'),
                                        ('hit', 'windows_pointer_target_obscured')])
def test_foreground_overlay_and_missing_hit_are_rejected(environment, change, code):
    user, calls = environment
    setattr(user, change, 0 if change == 'hit' else 999)
    with pytest.raises(ComputerError, match=code) as error:
        click()
    assert calls == [] and error.value.input_diagnostic['phase'] == 'before_injection'


def test_caller_check_runs_inside_active_monitor_before_injection(environment):
    _, calls = environment
    def check():
        assert FakeMonitor.latest is not None and not FakeMonitor.latest.closed
        assert calls == []
        raise ComputerError('user_takeover')
    with pytest.raises(ComputerError, match='user_takeover') as error:
        pointer.guarded_click(100, 20, 30, check=check, should_stop=lambda: False)
    assert error.value.input_diagnostic['phase'] == 'caller_check_before_injection'
    assert calls == []


def test_cancel_before_injection_has_no_pointer_effect(environment):
    _, calls = environment
    with pytest.raises(ComputerError, match='windows_input_cancelled'):
        pointer.guarded_click(100, 20, 30, check=lambda: None, should_stop=lambda: True)
    assert calls == []


@pytest.mark.parametrize('sent', [0, 1, 2])
def test_partial_input_never_retries_click_and_releases_only_known_down(environment, monkeypatch, sent):
    _, calls = environment
    def inject(user, dx, dy, marker, *, release_only=False):
        calls.append(release_only)
        return 1 if release_only else sent
    monkeypatch.setattr(pointer, '_inject', inject)
    with pytest.raises(ComputerError, match='windows_input_partial') as error:
        click()
    assert calls == ([False, True] if sent == 2 else [False])
    assert error.value.input_diagnostic['sent_events'] == sent
    assert FakeMonitor.latest.closed


def test_confirmation_timeout_is_unknown_to_caller_without_retry(environment, monkeypatch):
    _, calls = environment
    def inject(*args, **kwargs):
        calls.append(False)
        return 3
    monkeypatch.setattr(pointer, '_inject', inject)
    with pytest.raises(ComputerError, match='windows_input_hook_timeout') as error:
        click(timeout=.01)
    assert calls == [False]
    assert error.value.input_diagnostic['sent_events'] == 3


def test_third_party_input_after_injection_is_not_hidden_by_seen(environment, monkeypatch):
    _, calls = environment
    def inject(*args, **kwargs):
        calls.append(False)
        FakeMonitor.latest.seen.set()
        FakeMonitor.latest.error = 'user_takeover'
        return 3
    monkeypatch.setattr(pointer, '_inject', inject)
    with pytest.raises(ComputerError, match='user_takeover') as error:
        click()
    assert calls == [False] and error.value.input_diagnostic['sent_events'] == 3


def test_foreground_or_overlay_change_after_click_is_not_success(environment, monkeypatch):
    user, calls = environment
    def inject(*args, **kwargs):
        calls.append(False)
        FakeMonitor.latest.seen.set()
        user.root = 999
        return 3
    monkeypatch.setattr(pointer, '_inject', inject)
    with pytest.raises(ComputerError, match='windows_pointer_target_obscured'):
        click()
    assert calls == [False]


def test_approved_after_target_runs_once_inside_live_monitor_after_complete_hook_sequence(environment):
    _, calls = environment
    observed = []
    def after(user, hwnd, x, y):
        assert len(calls) == 1 and FakeMonitor.latest.seen.is_set()
        assert not FakeMonitor.latest.closed
        observed.append((hwnd, x, y))
    assert click(after_target=after) == 812
    assert observed == [(100, 20, 30)] and FakeMonitor.latest.closed


def test_after_target_never_bypasses_preinjection_target_rule(environment):
    user, calls = environment
    user.foreground = 999
    with pytest.raises(ComputerError, match='foreground_changed'):
        click(after_target=lambda *args: pytest.fail('no postcheck before dispatch'))
    assert calls == []


@pytest.mark.parametrize('failure', ['partial', 'hook', 'takeover'])
def test_after_target_never_runs_without_complete_clean_hook_confirmation(environment, monkeypatch, failure):
    _, calls = environment
    def inject(*args, **kwargs):
        calls.append(False)
        if failure == 'partial':
            return 1
        if failure == 'takeover':
            FakeMonitor.latest.seen.set()
            FakeMonitor.latest.error = 'user_takeover'
        return 3
    monkeypatch.setattr(pointer, '_inject', inject)
    with pytest.raises(ComputerError):
        click(timeout=.01, after_target=lambda *args: pytest.fail('must not run postcheck'))
    assert calls == [False] and FakeMonitor.latest.closed


def test_after_target_rejection_preserves_uncertain_diagnostic_without_epoch_read(environment, monkeypatch):
    _, calls = environment
    monkeypatch.setattr(inputs, '_epoch', lambda user: pytest.fail('no epoch after rejected target'))
    def reject(*args):
        raise ComputerError('close_not_proven')
    with pytest.raises(ComputerError, match='close_not_proven') as error:
        click(after_target=reject)
    assert len(calls) == 1 and error.value.input_diagnostic['sent_events'] == 3
    assert error.value.input_diagnostic['phase'] == 'foreground_validation'


@pytest.mark.parametrize('change', ['takeover', 'cancel'])
def test_monitor_and_cancellation_remain_active_during_after_target(environment, change):
    cancelled = []
    def after(*args):
        assert not FakeMonitor.latest.closed
        if change == 'takeover':
            FakeMonitor.latest.error = 'user_takeover'
        else:
            cancelled.append(True)
    with pytest.raises(ComputerError, match='user_takeover|input_cancelled'):
        pointer.guarded_click(100, 20, 30, check=lambda: None,
                              should_stop=lambda: bool(cancelled), after_target=after)
    assert FakeMonitor.latest.closed


def test_noncallable_after_target_is_rejected_before_native_api(monkeypatch):
    monkeypatch.setattr(pointer, '_pointer_api', lambda: pytest.fail('no native API'))
    with pytest.raises(ComputerError, match='input_invalid'):
        click(after_target=True)


def test_takeover_during_epoch_read_is_rejected(environment, monkeypatch):
    def epoch(user):
        FakeMonitor.latest.error = 'user_takeover'
        return 812
    monkeypatch.setattr(inputs, '_epoch', epoch)
    with pytest.raises(ComputerError, match='user_takeover'):
        click()


@pytest.mark.parametrize('hwnd,x,y,timeout', [(True, 20, 30, 1), (0, 20, 30, 1),
                                          (100, 20.0, 30, 1), (100, 20, False, 1),
                                          (100, 20, 30, 0), (100, 20, 30, 3),
                                          (100, 20, 30, float('nan'))])
def test_invalid_calls_have_diagnostics_without_native_api(monkeypatch, hwnd, x, y, timeout):
    monkeypatch.setattr(pointer, '_pointer_api', lambda: pytest.fail('native API called'))
    with pytest.raises(ComputerError, match='windows_input_invalid') as error:
        pointer.guarded_click(hwnd, x, y, check=lambda: None, should_stop=lambda: False, timeout=timeout)
    assert error.value.input_diagnostic['phase'] == 'validation'


@pytest.mark.parametrize('x,y', [(-1921, 0), (1920, 0), (0, -101), (0, 1080)])
def test_virtual_desktop_limits_include_negative_origins(x, y):
    with pytest.raises(ComputerError, match='windows_pointer_out_of_bounds'):
        pointer._geometry(FakeUser(), x, y)


@pytest.mark.parametrize('x,y', [(-1920, -100), (1919, 1079), (0, 0)])
def test_absolute_pixel_centre_mapping_roundtrips_at_virtual_edges(x, y):
    dx, dy = pointer._geometry(FakeUser(), x, y)
    assert dx * 3840 // 65536 - 1920 == x
    assert dy * 1180 // 65536 - 100 == y


def test_unrepresentable_absolute_coordinate_is_refused_before_click():
    user = FakeUser()
    user.metrics = {76: 0, 77: 0, 78: 100000, 79: 100000}
    with pytest.raises(ComputerError, match='windows_pointer_coordinate_unrepresentable'):
        pointer._geometry(user, 2, 2)


def test_dpi_failure_has_no_injection_and_diagnostic(environment):
    user, calls = environment
    user.SetThreadDpiAwarenessContext = lambda context: 0
    with pytest.raises(ComputerError, match='windows_pointer_dpi_unavailable') as error:
        click()
    assert calls == [] and error.value.input_diagnostic['phase'] == 'dpi_context'


def monitor():
    return pointer._PointerMonitor(None, None, None, 333, 20, 30)


def mouse(marker=333, x=20, y=30, flags=1):
    return inputs._MouseHook(wintypes_point(x, y), 0, flags, 0, marker)


def wintypes_point(x, y):
    return inputs.wintypes.POINT(x, y)


def test_marker_requires_exact_move_down_up_sequence_at_target():
    m = monitor()
    for message in pointer._MESSAGES:
        m._event(inputs._MouseHook, message, mouse())
    assert m.seen.is_set() and not m.takeover.is_set() and m.count == 3


@pytest.mark.parametrize('message,event', [(pointer._DOWN, mouse()),
                                        (pointer._MOVE, mouse(marker=999)),
                                        (pointer._MOVE, mouse(x=21)),
                                        (pointer._MOVE, mouse(flags=0))])
def test_wrong_order_foreign_marker_coordinate_or_noninjected_event_is_takeover(message, event):
    m = monitor()
    m._event(inputs._MouseHook, message, event)
    assert m.takeover.is_set() and not m.seen.is_set()


def test_marker_mismatch_diagnostic_keeps_all_observed_bits():
    m = monitor()
    observed = (1 << 40) | m.marker
    m._event(inputs._MouseHook, pointer._MOVE, mouse(marker=observed))
    assert m.takeover.is_set() and m.count == 0
    assert m.diagnostic()['expected_marker'] == 333
    assert m.diagnostic()['observed_marker'] == observed


def test_windows_mouse_abi_layout_and_raw_hook_buffer_preserve_ulong_ptr():
    # ABI constants come from Win32 MSLLHOOKSTRUCT/MOUSEINPUT, not one ctypes
    # structure copied into another, so an accidental packing change is caught.
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(inputs.wintypes.LONG) == 4
    assert ctypes.sizeof(inputs.wintypes.DWORD) == 4
    assert ctypes.sizeof(inputs.wintypes.POINT) == 8
    extra_offset, mouse_size, input_size = (24, 32, 40) if pointer_size == 8 else (20, 24, 28)
    assert [(name, getattr(inputs._MouseHook, name).offset) for name, _ in inputs._MouseHook._fields_] == [
        ('pt', 0), ('mouseData', 8), ('flags', 12), ('time', 16), ('dwExtraInfo', extra_offset)]
    assert inputs._MouseInput.dwExtraInfo.offset == extra_offset
    assert ctypes.sizeof(inputs._MouseHook) == ctypes.sizeof(inputs._MouseInput) == mouse_size
    assert ctypes.sizeof(inputs._Input) == input_size
    raw = bytearray(mouse_size)
    struct.pack_into('<iiIII', raw, 0, 934, 76, 0, 1, 123)
    marker = 0x12345678ABCDEF01 if pointer_size == 8 else 0x7ABCDEF1
    struct.pack_into('<Q' if pointer_size == 8 else '<I', raw, extra_offset, marker)
    parsed = inputs._MouseHook.from_buffer_copy(raw)
    assert (parsed.pt.x, parsed.pt.y, parsed.flags, parsed.time, parsed.dwExtraInfo) == (934, 76, 1, 123, marker)


def test_any_keyboard_event_even_same_marker_is_takeover():
    m = monitor()
    m._event(inputs._KeyboardHook, 0x100, inputs._KeyboardHook(0x41, 0, 0x10, 0, 333))
    assert m.takeover.is_set()


def test_extra_marked_mouse_event_after_completion_is_takeover():
    m = monitor()
    for message in pointer._MESSAGES:
        m._event(inputs._MouseHook, message, mouse())
    m._event(inputs._MouseHook, pointer._MOVE, mouse())
    assert m.takeover.is_set()


def test_sendinput_payload_is_marker_guarded_virtual_desktop_move_then_click():
    captured = []
    class Function:
        def __call__(self, count, events, size):
            captured.extend((int(e.type), int(e.mi.dx), int(e.mi.dy),
                             int(e.mi.dwFlags), int(e.mi.dwExtraInfo)) for e in events)
            assert size == ctypes.sizeof(inputs._Input)
            return count
    user = SimpleNamespace(SendInput=Function())
    assert pointer._inject(user, 101, 202, 333) == 3
    assert captured == [(0, 101, 202, 0xE001, 333), (0, 0, 0, 2, 333), (0, 0, 0, 4, 333)]


@pytest.mark.parametrize('fail_second', [False, True])
def test_both_hook_lifecycle_and_partial_install_cleanup(fail_second):
    installed, removed = [], []
    class User:
        def PeekMessageW(self, *args):
            return 0
        def SetWindowsHookExW(self, hook, callback, module, thread):
            installed.append(hook)
            return 0 if hook == 14 and fail_second else hook
        def GetMessageW(self, *args):
            m.done.set()
            return 0
        def UnhookWindowsHookEx(self, hook):
            removed.append(hook)
    kernel = SimpleNamespace(GetCurrentThreadId=lambda: 7, GetModuleHandleW=lambda arg: 8)
    m = pointer._PointerMonitor(User(), kernel, lambda fn: fn, 333, 20, 30)
    m._work()
    assert installed == [13, 14] and m.ready.is_set()
    assert removed == ([13] if fail_second else [14, 13])
    assert m.error == ('windows_input_hook_unavailable' if fail_second else None)
