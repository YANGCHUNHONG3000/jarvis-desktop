"""One guarded native click; target authorization belongs to the caller.

Only approved local control targets may use this primitive. A successful
injection is not evidence that the application's desired state was reached.
"""
import ctypes
from ctypes import wintypes
import secrets
import time

from .computer import ComputerError
from . import windows_input as inputs

_MOVE, _DOWN, _UP = 0x200, 0x201, 0x202
_MESSAGES = (_MOVE, _DOWN, _UP)


def _pointer_api():
    user, kernel, callback_type = inputs._api()
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.GetSystemMetrics.restype = ctypes.c_int
    user.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    return user, kernel, callback_type


def _geometry(user, x, y):
    left, top, width, height = (int(user.GetSystemMetrics(i)) for i in (76, 77, 78, 79))
    if (width <= 0 or height <= 0 or not left <= x < left + width
            or not top <= y < top + height):
        raise ComputerError('windows_pointer_out_of_bounds')
    # Target the centre of the pixel's normalized bin, including negative origins.
    dx = min(65535, ((x - left) * 2 + 1) * 65536 // (width * 2))
    dy = min(65535, ((y - top) * 2 + 1) * 65536 // (height * 2))
    if dx * width // 65536 + left != x or dy * height // 65536 + top != y:
        raise ComputerError('windows_pointer_coordinate_unrepresentable')
    return dx, dy


def _target(user, hwnd, x, y):
    if int(user.GetForegroundWindow() or 0) != hwnd:
        raise ComputerError('windows_input_foreground_changed')
    hit = int(user.WindowFromPoint(wintypes.POINT(x, y)) or 0)
    if not hit or int(user.GetAncestor(hit, 2) or 0) != hwnd:  # GA_ROOT, not owner
        raise ComputerError('windows_pointer_target_obscured')


def _inject(user, dx, dy, marker, *, release_only=False):
    flags = (0x0004,) if release_only else (0x0001 | 0x8000 | 0x4000 | 0x2000, 0x0002, 0x0004)
    events = (inputs._Input * len(flags))()
    for event, flag in zip(events, flags):
        event.type = 0
        event.mi = inputs._MouseInput(dx if flag & 1 else 0, dy if flag & 1 else 0,
                                      0, flag, 0, marker)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(inputs._Input), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    return int(user.SendInput(len(events), events, ctypes.sizeof(inputs._Input)))


class _PointerMonitor(inputs._Monitor):
    def __init__(self, user, kernel, callback_type, marker, x, y):
        super().__init__(user, kernel, callback_type, marker)
        self.x, self.y = x, y

    def diagnostic(self):
        return {**super().diagnostic(), 'expected_x': self.x, 'expected_y': self.y,
                'matched_pointer_events': self.count, 'expected_marker': self.marker}

    def _event(self, structure, message, data):
        if structure is inputs._KeyboardHook:
            self.keyboard_events += 1
            self._takeover('foreign_keyboard', message=int(message), flags=int(data.flags))
            return
        self.mouse_events += 1
        if int(data.dwExtraInfo) != self.marker:
            self._takeover('foreign_mouse', message=int(message), flags=int(data.flags),
                           observed_marker=int(data.dwExtraInfo), expected_marker=self.marker)
            return
        self.own_events += 1
        if (self.count >= len(_MESSAGES) or int(message) != _MESSAGES[self.count]
                or not int(data.flags) & 1 or data.pt.x != self.x or data.pt.y != self.y):
            self._takeover('marked_pointer_sequence', message=int(message),
                           observed_x=int(data.pt.x), observed_y=int(data.pt.y),
                           expected_message=_MESSAGES[self.count] if self.count < len(_MESSAGES) else 0)
            return
        self.count += 1
        if self.count == len(_MESSAGES):
            self.seen.set()

    def _work(self):
        hooks, callbacks = [], []
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            for hook_id, structure in ((13, inputs._KeyboardHook), (14, inputs._MouseHook)):
                def receive(code, word, pointer, structure=structure):
                    try:
                        if code >= 0:
                            data = ctypes.cast(pointer, ctypes.POINTER(structure)).contents
                            self._event(structure, word, data)
                    except Exception:
                        self._takeover('hook_callback')
                    return self.user.CallNextHookEx(None, code, word, pointer)
                callback = self.callback_type(receive)
                callbacks.append(callback)
                hook = self.user.SetWindowsHookExW(hook_id, callback, self.kernel.GetModuleHandleW(None), 0)
                if not hook:
                    raise ComputerError('windows_input_hook_unavailable')
                hooks.append(hook)
            self.ready.set()
            while not self.done.is_set():
                if self.user.GetMessageW(ctypes.byref(message), None, 0, 0) <= 0:
                    break
            if not self.done.is_set():
                self.error = 'windows_input_hook_stopped'
        except Exception:
            self.error = 'windows_input_hook_unavailable'
            self.ready.set()
        finally:
            for hook in reversed(hooks):
                self.user.UnhookWindowsHookEx(hook)


def guarded_click(hwnd, screen_x, screen_y, *, check, should_stop, timeout=1.0, after_target=None):
    """Move and click once, returning the input epoch after hook confirmation.

    All errors carry input_diagnostic. Once injection starts the caller must
    regard failure as uncertain; this function never retries the click. A caller
    may supply a bounded after_target(user, hwnd, x, y) check for an approved
    window-closing effect. Pre-injection foreground/hit testing is unchanged;
    the callback runs only after all injected events are hook-confirmed.
    """
    diagnostic = {'phase': 'validation', 'sent_events': 0}
    monitor, user, previous_dpi = None, None, None
    sent, released = 0, False
    dx = dy = marker = 0
    try:
        if (type(hwnd) is not int or hwnd <= 0 or type(screen_x) is not int
                or type(screen_y) is not int or not callable(check) or not callable(should_stop)
                or (after_target is not None and not callable(after_target))
                or type(timeout) not in (int, float) or not 0 < timeout <= 2):
            raise ComputerError('windows_input_invalid')
        deadline = time.monotonic() + timeout
        user, kernel, callback_type = _pointer_api()
        diagnostic['phase'] = 'dpi_context'
        previous_dpi = user.SetThreadDpiAwarenessContext(wintypes.HANDLE(-4))  # PMv2
        if not previous_dpi:
            raise ComputerError('windows_pointer_dpi_unavailable')
        dx, dy = _geometry(user, screen_x, screen_y)
        # Keep the complete tag representable through 32/64-bit mouse input
        # chains. Recognition still compares every bit, never a truncated mask.
        marker = secrets.randbits(31) or 1
        monitor = _PointerMonitor(user, kernel, callback_type, marker, screen_x, screen_y)
        diagnostic['phase'] = 'hook_start'
        monitor.start(timeout)
        diagnostic['phase'] = monitor.phase = 'caller_check_before_injection'
        check()
        monitor.check()
        if should_stop():
            raise ComputerError('windows_input_cancelled')
        diagnostic['phase'] = monitor.phase = 'before_injection'
        _target(user, hwnd, screen_x, screen_y)
        if any(user.GetAsyncKeyState(key) & 0x8000 for key in range(1, 256)):
            raise ComputerError('windows_input_keys_held')
        monitor.check()
        _target(user, hwnd, screen_x, screen_y)
        if time.monotonic() >= deadline:
            raise ComputerError('windows_input_hook_timeout')
        diagnostic['phase'] = monitor.phase = 'injecting'
        sent = _inject(user, dx, dy, marker)
        diagnostic['sent_events'] = sent
        released = sent == 3
        if sent != 3:
            raise ComputerError('windows_input_partial')
        diagnostic['phase'] = monitor.phase = 'awaiting_confirmation'
        while not monitor.seen.is_set():
            monitor.check()
            if should_stop():
                raise ComputerError('windows_input_cancelled')
            if time.monotonic() >= deadline:
                raise ComputerError('windows_input_hook_timeout')
            monitor.seen.wait(.005)
        monitor.check()
        diagnostic['phase'] = monitor.phase = 'foreground_validation'
        if after_target is None:
            _target(user, hwnd, screen_x, screen_y)
        else:
            after_target(user, hwnd, screen_x, screen_y)
        monitor.check()
        if should_stop():
            raise ComputerError('windows_input_cancelled')
        diagnostic['phase'] = monitor.phase = 'reading_epoch'
        epoch = inputs._epoch(user)
        monitor.check()
        return epoch
    except Exception as exc:
        error = exc if isinstance(exc, ComputerError) else ComputerError('windows_pointer_failed')
        existing = getattr(error, 'input_diagnostic', {})
        if type(existing) is not dict:
            existing = {}
        error.input_diagnostic = {**(monitor.diagnostic() if monitor else {}),
                                  **diagnostic, **existing}
        raise error from None
    finally:
        try:
            # Release a partially injected left-down once; never replay movement
            # or down. The original failure and uncertainty remain unchanged.
            if sent == 2 and not released:
                try:
                    _inject(user, dx, dy, marker, release_only=True)
                except Exception:
                    pass
        finally:
            if monitor:
                monitor.close()
            if previous_dpi:
                user.SetThreadDpiAwarenessContext(previous_dpi)
