"""Fixed guarded find/open/close-document chords with per-call markers."""
import ctypes
from ctypes import wintypes
import os
import secrets
import threading
import time

from .computer import ComputerError

_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t
_VK_CONTROL, _VK_F, _VK_O = 0x11, 0x46, 0x4F
_CHORD = ((_VK_CONTROL, False), (_VK_F, False), (_VK_F, True), (_VK_CONTROL, True))
_OPEN_CHORD = ((_VK_CONTROL, False), (_VK_O, False), (_VK_O, True), (_VK_CONTROL, True))
_CLOSE_CHORD = ((_VK_CONTROL, False), (0x57, False), (0x57, True), (_VK_CONTROL, True))
_CONFLICT_KEYS = (_VK_CONTROL, 0xA2, 0xA3, 0x10, 0xA0, 0xA1, 0x12, 0xA4, 0xA5,
                  0x5B, 0x5C, _VK_F, _VK_O, 0x57)


def _api():
    if os.name != 'nt':
        raise ComputerError('windows_required')
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
    user.SetWindowsHookExW.restype = wintypes.HANDLE
    user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user.UnhookWindowsHookEx.restype = wintypes.BOOL
    user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user.CallNextHookEx.restype = _LRESULT
    user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user.GetMessageW.restype = wintypes.BOOL
    user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user.PeekMessageW.restype = wintypes.BOOL
    user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostThreadMessageW.restype = wintypes.BOOL
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user.GetAsyncKeyState.restype = ctypes.c_short
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    return user, kernel, callback_type


class _KeyboardHook(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', _PTR)]


class _MouseHook(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', _PTR)]


class _KeyInput(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', _PTR)]


class _MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', _PTR)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [('ki', _KeyInput), ('mi', _MouseInput), ('hi', _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', _InputUnion)]


def _inject(user, strokes, marker):
    inputs = (_Input * len(strokes))()
    for item, (key, released) in zip(inputs, strokes):
        item.type = 1  # INPUT_KEYBOARD
        item.ki = _KeyInput(key, 0, 2 if released else 0, 0, marker)
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    user.SendInput.restype = wintypes.UINT
    return int(user.SendInput(len(inputs), inputs, ctypes.sizeof(_Input)))


def _epoch(user):
    class LastInput(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
    data = LastInput(ctypes.sizeof(LastInput), 0)
    user.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInput)]
    user.GetLastInputInfo.restype = wintypes.BOOL
    if not user.GetLastInputInfo(ctypes.byref(data)):
        raise ComputerError('windows_input_state_unavailable')
    return int(data.dwTime)


class _Monitor:
    def __init__(self, user, kernel, callback_type, marker):
        self.user, self.kernel, self.callback_type, self.marker = user, kernel, callback_type, marker
        self.ready, self.done, self.takeover, self.seen = (threading.Event() for _ in range(4))
        self.count, self.thread_id, self.error = 0, None, None
        self.keyboard_events = self.mouse_events = self.own_events = self.normalized_controls = 0
        self.first_takeover = None
        self.phase = 'hook_start'
        self.chord = _CHORD
        self.thread = threading.Thread(target=self._work, daemon=True)

    def _takeover(self, source, **numbers):
        if self.first_takeover is None:
            self.first_takeover = {'source': source, **numbers}
        self.takeover.set()

    def diagnostic(self):
        return {'phase': self.phase, 'keyboard_events': self.keyboard_events,
                'mouse_events': self.mouse_events, 'own_events': self.own_events,
                'matched_strokes': self.count, 'normalized_controls': self.normalized_controls,
                **(self.first_takeover or {})}

    def _work(self):
        hooks = []
        callbacks = []
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            for hook_id, structure in ((13, _KeyboardHook), (14, _MouseHook)):
                def receive(code, word, pointer, structure=structure):
                    try:
                        if code >= 0:
                            data = ctypes.cast(pointer, ctypes.POINTER(structure)).contents
                            if structure is _KeyboardHook:
                                self.keyboard_events += 1
                            else:
                                self.mouse_events += 1
                            if structure is _KeyboardHook and int(data.dwExtraInfo) == self.marker:
                                self.own_events += 1
                                key = int(data.vkCode)
                                # SendInput's generic Ctrl may reach the LL hook
                                # as VK_LCONTROL. Only this call's marker and
                                # its expected Ctrl position authorize this map.
                                if (key == 0xA2 and self.count < len(self.chord)
                                        and self.chord[self.count][0] == _VK_CONTROL):
                                    key = _VK_CONTROL
                                    self.normalized_controls += 1
                                stroke = (key, bool(data.flags & 0x80))
                                if self.count >= len(self.chord) or stroke != self.chord[self.count]:
                                    self._takeover('marked_sequence', observed_vk=int(data.vkCode),
                                                   expected_vk=self.chord[self.count][0] if self.count < len(self.chord) else 0,
                                                   observed_up=int(bool(data.flags & 0x80)),
                                                   expected_up=int(self.chord[self.count][1]) if self.count < len(self.chord) else 0)
                                else:
                                    self.count += 1
                                    if self.count == len(self.chord):
                                        self.seen.set()
                            else:
                                self._takeover('foreign_keyboard' if structure is _KeyboardHook else 'foreign_mouse',
                                               message=int(word), flags=int(data.flags))
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
                received = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if received <= 0:
                    break
            if not self.done.is_set():
                self.error = 'windows_input_hook_stopped'
        except Exception:
            self.error = 'windows_input_hook_unavailable'
            self.ready.set()
        finally:
            for hook in reversed(hooks):
                self.user.UnhookWindowsHookEx(hook)

    def start(self, timeout):
        self.thread.start()
        if not self.ready.wait(timeout) or self.error or not self.thread.is_alive():
            raise ComputerError('windows_input_hook_unavailable')

    def check(self):
        if self.error or not self.thread.is_alive():
            raise ComputerError('windows_input_hook_stopped')
        if self.takeover.is_set():
            error = ComputerError('user_takeover')
            error.input_diagnostic = self.diagnostic()
            raise error

    def close(self):
        self.done.set()
        if self.thread_id is not None:
            self.user.PostThreadMessageW(self.thread_id, 0x12, 0, 0)  # WM_QUIT
        if self.thread.is_alive():
            self.thread.join(timeout=.5)


def send_find(hwnd, *, check, owns=lambda other: False, should_stop=lambda: False, timeout=1.0):
    """Send only Ctrl+F and return its input epoch after guarded completion.

    ``check`` runs before injection because callers may bind the previous input
    epoch. After injection, hooks distinguish this chord from third party input.
    ``owns`` must validate the target's OS owner chain for a newly opened dialog.
    """
    return _send_fixed(hwnd, _CHORD, check=check, owns=owns, should_stop=should_stop, timeout=timeout)


def send_open(hwnd, *, check, owns=lambda other: False, should_stop=lambda: False, timeout=1.0):
    """Send only Ctrl+O under the same double-hook and ownership guards."""
    return _send_fixed(hwnd, _OPEN_CHORD, check=check, owns=owns, should_stop=should_stop, timeout=timeout)


def send_close(hwnd, *, check, owns=lambda other: False, should_stop=lambda: False, timeout=1.0):
    """Close only the active document with Ctrl+W; never dismiss a save prompt."""
    return _send_fixed(hwnd, _CLOSE_CHORD, check=check, owns=owns, should_stop=should_stop, timeout=timeout)


def _send_fixed(hwnd, chord, *, check, owns, should_stop, timeout):
    if (chord not in (_CHORD, _OPEN_CHORD, _CLOSE_CHORD) or type(hwnd) is not int or hwnd <= 0
            or not callable(check) or not callable(owns) or not callable(should_stop)
            or type(timeout) not in (int, float) or not 0 < timeout <= 2):
        raise ComputerError('windows_input_invalid')
    user, kernel, callback_type = _api()
    marker = secrets.randbits(ctypes.sizeof(_PTR) * 8 - 1) or 1
    monitor = _Monitor(user, kernel, callback_type, marker)
    monitor.chord = chord
    pressed, sent = [], 0
    deadline = time.monotonic() + timeout
    phase = 'hook_start'
    try:
        monitor.start(timeout)
        phase = monitor.phase = 'caller_check_before_injection'
        check()
        phase = monitor.phase = 'before_injection'
        monitor.check()
        if should_stop():
            raise ComputerError('windows_input_cancelled')
        if int(user.GetForegroundWindow() or 0) != hwnd:
            raise ComputerError('windows_input_foreground_changed')
        if any(user.GetAsyncKeyState(key) & 0x8000 for key in _CONFLICT_KEYS):
            raise ComputerError('windows_input_keys_held')
        monitor.check()
        if time.monotonic() >= deadline:
            raise ComputerError('windows_input_hook_timeout')
        phase = monitor.phase = 'injecting'
        sent = _inject(user, chord, marker)
        for key, released in chord[:sent]:
            if released:
                if key in pressed:
                    pressed.remove(key)
            else:
                pressed.append(key)
        if sent != len(chord):
            raise ComputerError('windows_input_partial')
        phase = monitor.phase = 'awaiting_confirmation'
        while not monitor.seen.is_set():
            monitor.check()
            if should_stop():
                raise ComputerError('windows_input_cancelled')
            if time.monotonic() >= deadline:
                raise ComputerError('windows_input_hook_timeout')
            monitor.seen.wait(.005)
        monitor.check()
        phase = monitor.phase = 'foreground_validation'
        foreground = int(user.GetForegroundWindow() or 0)
        if foreground != hwnd and not owns(foreground):
            error = ComputerError('user_takeover')
            error.input_diagnostic = {'phase': phase, 'source': 'foreground',
                                      'expected_hwnd': hwnd, 'observed_hwnd': foreground}
            raise error
        if should_stop():
            raise ComputerError('windows_input_cancelled')
        phase = monitor.phase = 'reading_epoch'
        epoch = _epoch(user)
        phase = monitor.phase = 'after_epoch'
        monitor.check()
        return epoch
    except ComputerError as exc:
        if str(exc) == 'user_takeover' and not hasattr(exc, 'input_diagnostic'):
            diagnostic = getattr(monitor, 'diagnostic', None)
            exc.input_diagnostic = diagnostic() if callable(diagnostic) else {'phase': phase}
            exc.input_diagnostic.setdefault('source', 'caller_check_or_monitor')
        raise
    finally:
        try:
            if pressed:
                _inject(user, tuple((key, True) for key in reversed(pressed)), marker)
        finally:
            monitor.close()
