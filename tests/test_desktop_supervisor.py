"""Offline checks for the desktop launcher's process supervision."""

import json
import queue
import subprocess
from types import SimpleNamespace

import run_jarvis_desktop as desktop


class ScriptedQueue:
    def __init__(self, items):
        self.items = list(items)

    def put(self, item):
        self.items.append(item)

    def get(self, timeout=None):
        if self.items:
            item = self.items.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise queue.Empty

    def empty(self):
        return not self.items


class FakeChild:
    def __init__(self, running=True):
        self.returncode = None if running else 0
        self.stdout = []
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 2

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


def arrange(monkeypatch, tmp_path, child, queue_items, ticks=()):
    events = []
    job = SimpleNamespace(root=tmp_path, event=lambda *args, **kwargs: events.append((args, kwargs)))
    scripted_queue = ScriptedQueue(queue_items)
    clock = iter(ticks)
    monkeypatch.setattr(desktop, "queue", SimpleNamespace(Queue=lambda: scripted_queue, Empty=queue.Empty))
    monkeypatch.setattr(
        desktop,
        "subprocess",
        SimpleNamespace(
            Popen=lambda *args, **kwargs: child,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    monkeypatch.setattr(desktop, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    return job, events


def read_result(tmp_path):
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))


def test_timeout_preserves_existing_success(monkeypatch, tmp_path):
    success = {"ok": True, "state": "succeeded", "effects": "verified"}
    (tmp_path / "result.json").write_text(json.dumps(success), encoding="utf-8")
    child = FakeChild()
    job, events = arrange(monkeypatch, tmp_path, child, ['{"event":"started"}\n'], (0, 36))

    # Once the result is found, model the GUI closing on its own.
    calls = iter((None, None, 0))
    def poll():
        value = next(calls, 0)
        if value is not None:
            child.returncode = value
        return value
    child.poll = poll
    exit_code = desktop.supervise(job, "fake-python", False)

    assert exit_code == 0
    assert read_result(tmp_path) == success
    assert not child.terminated
    assert events == []


def test_interrupt_preserves_existing_success(monkeypatch, tmp_path):
    success = {"ok": True, "state": "succeeded", "effects": "verified"}
    (tmp_path / "result.json").write_text(json.dumps(success), encoding="utf-8")
    child = FakeChild()
    job, _ = arrange(monkeypatch, tmp_path, child, [KeyboardInterrupt()])

    assert desktop.supervise(job, "fake-python", False) == 0
    assert read_result(tmp_path) == success
    assert child.terminated
    assert not (tmp_path / "cancel.flag").exists()


def test_exit_before_start_records_no_effect(monkeypatch, tmp_path):
    child = FakeChild(running=False)
    job, _ = arrange(monkeypatch, tmp_path, child, [])

    assert desktop.supervise(job, "fake-python", False) == 1
    assert read_result(tmp_path) == {
        "ok": False,
        "state": "cancelled",
        "code": "cancelled_before_start",
        "effects": "not_started",
    }


def test_real_timeout_records_unknown_effect(monkeypatch, tmp_path):
    child = FakeChild()
    job, events = arrange(monkeypatch, tmp_path, child, ['{"event":"started"}\n'], (0, 36))

    assert desktop.supervise(job, "fake-python", False) == 2
    assert child.terminated
    assert read_result(tmp_path) == {
        "ok": False,
        "state": "timed_out",
        "code": "supervisor_timeout",
        "effects": "unknown",
    }
    assert events == [(('supervisor_timeout',), {'effects': 'unknown'})]
