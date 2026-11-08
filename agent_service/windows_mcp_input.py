"""Observe physical takeover during one upstream input call; never block input.

Reuses Jarvis's hook ABI. Injected events are not authenticated to a process;
this is human-takeover detection, not a boundary against other desktop automation.
"""
import ctypes as c
from ctypes import wintypes as w
import threading
from .computer import ComputerError


class PhysicalInputMonitor:
    def __init__(self):
        from .windows_input import _api
        self.user, self.kernel, self.callback_type = _api()
        self.ready, self.done, self.physical = (threading.Event() for _ in range(3))
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread_id = None
        self.error = False
        self.injected_count = 0
        self.first_physical = None
        self.left_held = False
        self.physical_left = False
        self.released_left = False

    def record_mouse(self, message, injected):
        if message == 0x201:  # WM_LBUTTONDOWN
            if injected:
                self.left_held = True
            else:
                self.physical_left = True
        elif message == 0x202:  # Either delivered up clears the application state.
            self.left_held = self.physical_left = False

    def release_owned_input(self):
        # A terminated upstream Click may have sent down but not its matching up.
        # Never release a subsequently delivered physical press belonging to the user.
        if self.left_held and not self.physical_left:
            import win32api
            win32api.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP only, no movement/down.
            self.released_left = True
            self.left_held = False

    def _work(self):
        from .windows_input import _KeyboardHook, _MouseHook
        hooks, callbacks = [], []
        try:
            self.thread_id = self.kernel.GetCurrentThreadId()
            msg = w.MSG()
            self.user.PeekMessageW(c.byref(msg), None, 0, 0, 0)
            for hook_id, struct, mask in ((13, _KeyboardHook, 0x10), (14, _MouseHook, 0x01)):
                def receive(code, word, pointer, struct=struct, mask=mask):
                    try:
                        if code >= 0:
                            data = c.cast(pointer, c.POINTER(struct)).contents
                            if mask == 1:
                                self.record_mouse(int(word), bool(data.flags & mask))
                            if data.flags & mask:
                                self.injected_count += 1
                            else:
                                if self.first_physical is None:
                                    self.first_physical = {'message': int(word), 'flags': int(data.flags),
                                                          'kind': 'mouse' if mask == 1 else 'keyboard'}
                                self.physical.set()
                    except Exception:
                        self.error = True
                    return self.user.CallNextHookEx(None, code, word, pointer)
                callback = self.callback_type(receive)
                callbacks.append(callback)
                hook = self.user.SetWindowsHookExW(hook_id, callback, self.kernel.GetModuleHandleW(None), 0)
                if not hook:
                    raise RuntimeError('hook unavailable')
                hooks.append(hook)
            self.ready.set()
            while not self.done.is_set():
                if self.user.GetMessageW(c.byref(msg), None, 0, 0) <= 0:
                    break
            if not self.done.is_set():
                self.error = True
        except Exception:
            self.error = True
            self.ready.set()
        finally:
            for hook in reversed(hooks):
                self.user.UnhookWindowsHookEx(hook)

    def start(self):
        self.thread.start()
        if not self.ready.wait(1):
            raise ComputerError('windows_mcp_input_monitor_unavailable')
        self.check()

    def check(self):
        if self.error or not self.thread.is_alive():
            raise ComputerError('windows_mcp_input_monitor_unavailable')
        if self.physical.is_set():
            raise ComputerError('user_takeover')

    def proof(self):
        return {'physical_input_observed': self.physical.is_set(), 'hook_error': self.error,
                'injected_events': self.injected_count, 'injected_origin_authenticated': False,
                'first_physical': self.first_physical, 'left_held': self.left_held,
                'physical_left': self.physical_left, 'released_left_on_close': self.released_left}

    def close(self):
        self.done.set()
        if self.thread_id:
            self.user.PostThreadMessageW(self.thread_id, 0x12, 0, 0)
        if self.thread.is_alive():
            self.thread.join(timeout=.5)
