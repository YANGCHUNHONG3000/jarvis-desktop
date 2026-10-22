"""Supervised Office worker tests with fake subprocesses; never starts WPS."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from agent_service import office_host


SPEC = {"kind": "document", "title": "核对", "paragraphs": ["已保存"]}


class FakePipe:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, *, returncode=0, running=False, wait_timeout=False):
        self.returncode = None if running else returncode
        self.stdin = FakePipe()
        self.stdout = FakePipe()
        self.terminate_calls = 0
        self.wait_calls = []
        self.wait_timeout = wait_timeout

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_timeout:
            self.wait_timeout = False
            raise subprocess.TimeoutExpired("fake-office-worker", timeout)
        return self.returncode


def _install_fake(monkeypatch, process, on_launch=lambda root: None):
    import run_jarvis_desktop

    monkeypatch.setattr(run_jarvis_desktop, "desktop_python", lambda: "fake-python")
    launches = []

    def popen(argv, **options):
        launches.append((argv, options))
        on_launch(Path(argv[-3]))  # --worker ROOT --seconds N
        return process

    monkeypatch.setattr(office_host.subprocess, "Popen", popen)
    return launches


def _valid_outcome(root: Path):
    artifact = root / "document.docx"
    data = b"owned-fake-docx"
    artifact.write_bytes(data)
    value = {"ok": True, "state": "succeeded", "artifact": str(artifact),
             "sha256": sha256(data).hexdigest(), "application_verified": True,
             "file_verified": True, "observed": {"text": "核对\n已保存"},
             "app": {"progid": "KWPS.Application", "name": "WPS compatibility", "version": "12.0"}}
    (root / "worker-outcome.json").write_text(json.dumps(value), encoding="utf-8")
    return value


def test_cancelled_before_launch_has_no_worker_or_request(tmp_path, monkeypatch):
    process = FakeProcess()
    launches = _install_fake(monkeypatch, process)
    root = tmp_path / "step"
    result = office_host.execute_office_supervised(SPEC, root, should_stop=lambda: True)
    assert result == {"ok": False, "state": "cancelled", "code": "cancelled_before_start"}
    assert launches == [] and not (root / "worker-request.json").exists()


def test_missing_worker_outcome_is_unknown(tmp_path, monkeypatch):
    process = FakeProcess(returncode=1)
    launches = _install_fake(monkeypatch, process)
    result = office_host.execute_office_supervised(SPEC, tmp_path / "step")
    assert result == {"ok": False, "state": "unknown", "code": "office_worker_exited",
                      "effects": "unknown"}
    assert len(launches) == 1 and process.stdin.written == b"1"
    assert process.stdin.closed and process.stdout.closed


def test_known_worker_failure_receipt_is_returned(tmp_path, monkeypatch):
    process = FakeProcess(returncode=1)
    failed = {"ok": False, "state": "unknown", "code": "office_operation_failed"}

    def launched(root):
        (root / "worker-outcome.json").write_text(json.dumps(failed), encoding="utf-8")

    _install_fake(monkeypatch, process, launched)
    assert office_host.execute_office_supervised(SPEC, tmp_path / "step") == failed


def test_success_receipt_requires_owned_file_hash_and_two_readbacks(tmp_path):
    root = tmp_path / "step"
    root.mkdir()
    value = _valid_outcome(root)
    assert office_host._outcome(root) == value
    value["sha256"] = "0" * 64
    (root / "worker-outcome.json").write_text(json.dumps(value), encoding="utf-8")
    assert office_host._outcome(root) is None
    value["sha256"] = sha256((root / "document.docx").read_bytes()).hexdigest()
    value["application_verified"] = False
    (root / "worker-outcome.json").write_text(json.dumps(value), encoding="utf-8")
    assert office_host._outcome(root) is None
    value["application_verified"] = True
    value["artifact"] = str(tmp_path / "outside.docx")
    (tmp_path / "outside.docx").write_bytes(b"owned-fake-docx")
    (root / "worker-outcome.json").write_text(json.dumps(value), encoding="utf-8")
    assert office_host._outcome(root) is None


def test_persisted_valid_outcome_wins_nonzero_returncode(tmp_path, monkeypatch):
    process = FakeProcess(returncode=1)
    _install_fake(monkeypatch, process, _valid_outcome)
    result = office_host.execute_office_supervised(SPEC, tmp_path / "step")
    assert result["ok"] is True and result["state"] == "succeeded"
    assert process.terminate_calls == 0


def test_timeout_terminates_only_owned_worker(tmp_path, monkeypatch):
    process = FakeProcess(running=True, wait_timeout=True)
    _install_fake(monkeypatch, process)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(office_host.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(office_host.time, "sleep", lambda seconds: None)
    root = tmp_path / "step"
    result = office_host.execute_office_supervised(SPEC, root, timeout=1)
    assert result["ok"] is False and result["state"] == "unknown"
    assert result["code"] == "office_timeout" and process.terminate_calls == 1
    assert process.wait_calls == [2, 5] and (root / "cancel.flag").is_file()


def test_preexisting_request_conflict_prevents_worker_replay(tmp_path, monkeypatch):
    process = FakeProcess()
    launches = _install_fake(monkeypatch, process)
    root = tmp_path / "step"
    root.mkdir()
    request = root / "worker-request.json"
    request.write_text('{"spec":"prior"}', encoding="utf-8")
    with pytest.raises(FileExistsError):
        office_host.execute_office_supervised(SPEC, root)
    assert launches == [] and request.read_text(encoding="utf-8") == '{"spec":"prior"}'


def test_preexisting_outcome_cannot_be_accepted_as_new_worker_success(tmp_path, monkeypatch):
    process = FakeProcess(returncode=1)
    launches = _install_fake(monkeypatch, process)
    root = tmp_path / "step"
    root.mkdir()
    _valid_outcome(root)
    with pytest.raises((FileExistsError, ValueError)):
        office_host.execute_office_supervised(SPEC, root)
    assert launches == []


def test_worker_early_exit_during_start_signal_returns_unknown_and_closes_pipes(tmp_path, monkeypatch):
    process = FakeProcess(returncode=1)

    def broken_write(data):
        raise BrokenPipeError("worker closed stdin")

    process.stdin.write = broken_write
    _install_fake(monkeypatch, process)
    result = office_host.execute_office_supervised(SPEC, tmp_path / "step")
    assert result["ok"] is False and result["state"] == "unknown"
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.parametrize("timeout", [0, -1, 76, float("nan"), float("inf"), True, "5"])
def test_invalid_timeout_is_rejected_before_launch(tmp_path, monkeypatch, timeout):
    process = FakeProcess()
    launches = _install_fake(monkeypatch, process)
    root = tmp_path / "step"
    with pytest.raises(ValueError, match="office_timeout_invalid"):
        office_host.execute_office_supervised(SPEC, root, timeout=timeout)
    assert launches == [] and not root.exists()


def test_worker_detects_parent_pipe_eof_and_reports_non_success(tmp_path, monkeypatch):
    from agent_service import office
    import io
    import time

    root = tmp_path / "step"
    root.mkdir()
    (root / "worker-request.json").write_text(json.dumps({"spec": SPEC}), encoding="utf-8")
    observed_stop = []

    def execute(spec, run_root, should_stop):
        until = time.monotonic() + 1
        while not should_stop() and time.monotonic() < until:
            time.sleep(.01)
        observed_stop.append(should_stop())
        return {"ok": False, "state": "unknown", "code": "parent_lost"}

    monkeypatch.setattr(office, "execute_office", execute)
    monkeypatch.setattr(office_host.sys, "stdin", type("FakeStdin", (), {"buffer": io.BytesIO(b"1")})())
    code = office_host.worker(root, seconds=10)
    assert code == 1 and observed_stop == [True]
    assert office_host._outcome(root) == {"ok": False, "state": "unknown", "code": "parent_lost"}


def test_unresponsive_worker_does_not_escape_supervisor_or_touch_office(tmp_path, monkeypatch):
    class UnresponsiveProcess(FakeProcess):
        def __init__(self):
            super().__init__(running=True)
            self.kill_calls = 0

        def terminate(self):
            self.terminate_calls += 1  # The simulated process ignores termination.

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if self.returncode is None:
                raise subprocess.TimeoutExpired("fake-office-worker", timeout)
            return self.returncode

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

    process = UnresponsiveProcess()
    _install_fake(monkeypatch, process)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(office_host.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(office_host.time, "sleep", lambda seconds: None)
    result = office_host.execute_office_supervised(SPEC, tmp_path / "step", timeout=1)
    assert result["ok"] is False and result["state"] == "unknown"
    assert process.terminate_calls >= 1
    assert process.kill_calls == 1
