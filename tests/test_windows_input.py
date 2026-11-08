"""Guarded-chord contracts with simulated OS calls; never inject real input."""
import ctypes
import threading

import pytest

from agent_service import windows_input as wi
from agent_service.computer import ComputerError


@pytest.fixture
def native(monkeypatch):
    class User:
        foreground = 100
        held = False
        def GetForegroundWindow(self):
            return self.foreground
        def GetAsyncKeyState(self, key):
            return 0x8000 if self.held else 0
    user = User()
    class Monitor:
        instance = None
        takeover = False
        fail_start = False
        def __init__(self, user, kernel, callback_type, marker):
            Monitor.instance = self
            self.seen = threading.Event()
            self.closed = False
            self.marker = marker
        def start(self, timeout):
            if self.fail_start:
                raise ComputerError('windows_input_hook_unavailable')
        def check(self):
            if self.takeover:
                raise ComputerError('user_takeover')
        def close(self):
            self.closed = True
    calls = []
    def inject(user, strokes, marker):
        calls.append((tuple(strokes), marker))
        Monitor.instance.seen.set()
        return len(strokes)
    monkeypatch.setattr(wi, '_api', lambda: (user, None, None))
    monkeypatch.setattr(wi, '_Monitor', Monitor)
    monkeypatch.setattr(wi, '_inject', inject)
    monkeypatch.setattr(wi, '_epoch', lambda user: 4321)
    return user, Monitor, calls


def test_only_fixed_chord_is_sent_and_epoch_returned(native):
    user, monitor, calls = native
    checks = []
    assert wi.send_find(100, check=lambda: checks.append('before')) == 4321
    assert checks == ['before']
    assert calls[0][0] == ((0x11, False), (0x46, False), (0x46, True), (0x11, True))
    assert calls[0][1] and monitor.instance.closed


def test_close_sends_only_ctrl_w_and_runs_final_document_guard_once(native):
    _, monitor, calls = native
    checks = []
    assert wi.send_close(100, check=lambda: checks.append('exact clean copy')) == 4321
    assert checks == ['exact clean copy']
    assert calls[0][0] == ((0x11, False), (0x57, False), (0x57, True), (0x11, True))
    assert monitor.instance.chord == wi._CLOSE_CHORD and monitor.instance.closed


def test_close_document_guard_rejection_never_injects_or_releases_user_keys(native):
    _, monitor, calls = native
    def changed():
        raise ComputerError('windows_document_unsaved_changes')
    with pytest.raises(ComputerError, match='windows_document_unsaved_changes'):
        wi.send_close(100, check=changed)
    assert not calls and monitor.instance.closed


def test_failed_hook_installation_never_injects(native):
    _, monitor, calls = native
    monitor.fail_start = True
    with pytest.raises(ComputerError, match='windows_input_hook_unavailable'):
        wi.send_find(100, check=lambda: None)
    assert not calls and monitor.instance.closed


def test_foreground_change_before_chord_never_injects(native):
    user, monitor, calls = native
    user.foreground = 200
    with pytest.raises(ComputerError, match='windows_input_foreground_changed'):
        wi.send_find(100, check=lambda: None)
    assert not calls and monitor.instance.closed


def test_existing_physical_chord_keys_are_not_released_or_injected(native):
    user, _, calls = native
    user.held = True
    with pytest.raises(ComputerError, match='windows_input_keys_held'):
        wi.send_find(100, check=lambda: None)
    assert not calls


def test_partial_injection_releases_only_own_pressed_keys(native, monkeypatch):
    _, monitor, calls = native
    def partial(user, strokes, marker):
        calls.append((tuple(strokes), marker))
        return 2 if len(calls) == 1 else len(strokes)
    monkeypatch.setattr(wi, '_inject', partial)
    with pytest.raises(ComputerError, match='windows_input_partial'):
        wi.send_find(100, check=lambda: None)
    assert calls[1][0] == ((0x46, True), (0x11, True))
    assert calls[0][1] == calls[1][1] and monitor.instance.closed


def test_third_party_input_during_chord_is_not_adopted_as_new_epoch(native, monkeypatch):
    _, monitor, calls = native
    def takeover(user, strokes, marker):
        calls.append((tuple(strokes), marker))
        monitor.instance.takeover = True
        monitor.instance.seen.set()
        return len(strokes)
    monkeypatch.setattr(wi, '_inject', takeover)
    monkeypatch.setattr(wi, '_epoch', lambda user: pytest.fail('must not adopt third party epoch'))
    with pytest.raises(ComputerError, match='user_takeover'):
        wi.send_find(100, check=lambda: None)
    assert monitor.instance.closed


def test_unowned_foreground_after_chord_refused(native, monkeypatch):
    user, monitor, calls = native
    original = wi._inject
    def changed(*args):
        count = original(*args)
        user.foreground = 200
        return count
    monkeypatch.setattr(wi, '_inject', changed)
    with pytest.raises(ComputerError, match='user_takeover'):
        wi.send_find(100, check=lambda: None)
    assert monitor.instance.closed


def test_owned_dialog_after_chord_allowed(native, monkeypatch):
    user, _, _ = native
    original = wi._inject
    def changed(*args):
        count = original(*args)
        user.foreground = 200
        return count
    monkeypatch.setattr(wi, '_inject', changed)
    assert wi.send_find(100, check=lambda: None, owns=lambda hwnd: hwnd == 200) == 4321


def test_missing_hook_confirmation_times_out(native, monkeypatch):
    _, monitor, _ = native
    monkeypatch.setattr(wi, '_inject', lambda user, strokes, marker: len(strokes))
    with pytest.raises(ComputerError, match='windows_input_hook_timeout'):
        wi.send_find(100, check=lambda: None, timeout=.01)
    assert monitor.instance.closed


def test_marker_is_attached_to_every_native_event():
    captured = []
    class SendInput:
        def __call__(self, count, inputs, size):
            captured.extend((inputs[i].ki.wVk, inputs[i].ki.dwFlags, inputs[i].ki.dwExtraInfo)
                            for i in range(count))
            assert size == ctypes.sizeof(wi._Input)
            return count
    class User:
        pass
    user = User()
    user.SendInput = SendInput()
    assert wi._inject(user, wi._CHORD, 123456) == 4
    assert captured == [(0x11, 0, 123456), (0x46, 0, 123456),
                        (0x46, 2, 123456), (0x11, 2, 123456)]


def test_open_sends_only_fixed_ctrl_o(native):
    _, monitor, calls = native
    assert wi.send_open(100, check=lambda: None) == 4321
    assert calls[0][0] == ((0x11, False), (0x4F, False), (0x4F, True), (0x11, True))
    assert monitor.instance.chord == wi._OPEN_CHORD and monitor.instance.closed


def test_open_partial_injection_releases_only_own_ctrl_o(native, monkeypatch):
    _, monitor, calls = native
    def partial(user, strokes, marker):
        calls.append((tuple(strokes), marker))
        return 2 if len(calls) == 1 else len(strokes)
    monkeypatch.setattr(wi, '_inject', partial)
    with pytest.raises(ComputerError, match='windows_input_partial'):
        wi.send_open(100, check=lambda: None)
    assert calls[1][0] == ((0x4F, True), (0x11, True))
    assert calls[0][1] == calls[1][1] and monitor.instance.closed


@pytest.mark.parametrize('sender', [wi.send_find, wi.send_open, wi.send_close])
def test_both_fixed_chords_stop_for_takeover(native, monkeypatch, sender):
    _, monitor, _ = native
    def takeover(user, strokes, marker):
        monitor.instance.takeover = True
        monitor.instance.seen.set()
        return len(strokes)
    monkeypatch.setattr(wi, '_inject', takeover)
    with pytest.raises(ComputerError, match='user_takeover'):
        sender(100, check=lambda: None)


def test_private_sender_refuses_arbitrary_key_sequences(native):
    _, _, calls = native
    with pytest.raises(ComputerError, match='windows_input_invalid'):
        wi._send_fixed(100, ((0x41, False),), check=lambda: None, owns=lambda hwnd: True,
                       should_stop=lambda: False, timeout=1)
    assert not calls


@pytest.mark.parametrize('chord', [wi._CHORD, wi._OPEN_CHORD, wi._CLOSE_CHORD])
@pytest.mark.parametrize('foreign', [False, True])
@pytest.mark.parametrize('ctrl_vk', [0x11, 0xA2, 0xA3])
@pytest.mark.parametrize('marked', [True, False])
def test_native_monitor_requires_marker_and_exact_key_order(chord, foreign, ctrl_vk, marked):
    callbacks = {}
    class Kernel:
        def GetCurrentThreadId(self): return 1
        def GetModuleHandleW(self, module): return 1
    class User:
        def PeekMessageW(self, *args): return 0
        def SetWindowsHookExW(self, hook, callback, *args):
            callbacks[hook] = callback
            return hook
        def UnhookWindowsHookEx(self, handle): return True
        def CallNextHookEx(self, *args): return 0
        def GetMessageW(self, *args):
            for index, (key, released) in enumerate(chord):
                if key == 0x11:
                    key = ctrl_vk
                if foreign and index == 1:
                    key = 0x41  # Same marker cannot authorize another key.
                event = wi._KeyboardHook(key, 0, 0x80 if released else 0, 0, 99 if marked else 0)
                callbacks[13](0, 0x101 if released else 0x100, ctypes.addressof(event))
            monitor.done.set()
            return 0
    monitor = wi._Monitor(User(), Kernel(), lambda callback: callback, 99)
    monitor.chord = chord
    monitor._work()
    failed = foreign or ctrl_vk == 0xA3 or not marked
    assert monitor.takeover.is_set() == failed
    assert monitor.seen.is_set() == (not failed)
    diagnostic = monitor.diagnostic()
    assert diagnostic['keyboard_events'] == 4 and diagnostic['mouse_events'] == 0
    if not marked:
        assert diagnostic['source'] == 'foreign_keyboard'
        assert 'observed_vk' not in diagnostic and 'expected_vk' not in diagnostic
    elif failed:
        assert diagnostic['source'] == 'marked_sequence'
    elif ctrl_vk == 0xA2:
        assert diagnostic['normalized_controls'] == 2


def test_foreground_takeover_has_fixed_nontext_diagnostic(native, monkeypatch):
    user, _, _ = native
    original = wi._inject
    def changed(*args):
        count = original(*args)
        user.foreground = 200
        return count
    monkeypatch.setattr(wi, '_inject', changed)
    with pytest.raises(ComputerError, match='user_takeover') as raised:
        wi.send_open(100, check=lambda: None)
    assert raised.value.input_diagnostic == {'phase': 'foreground_validation', 'source': 'foreground',
                                              'expected_hwnd': 100, 'observed_hwnd': 200}
