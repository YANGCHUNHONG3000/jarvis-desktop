"""Supervised, read-only native check for an owned external Word snapshot."""

import argparse
from importlib.util import find_spec
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from . import office_source
from .external_document_source import ExternalDocumentError, _digest, _native_snapshot, _read
from .office_host import ROOT, _stop_worker


def _child_environment():
    lxml = Path(find_spec("lxml").origin).parent.parent
    return dict(os.environ, PYTHONIOENCODING="utf-8",
                PYTHONPATH=os.pathsep.join([str(ROOT), str(lxml), os.environ.get("PYTHONPATH", "")]))


def native_snapshot_supervised(snapshot, directory, *, stop=lambda: False, deadline=None):
    from run_jarvis_desktop import desktop_python

    snapshot, directory = Path(snapshot).absolute(), Path(directory).absolute()
    if (office_source._linked(directory) or office_source._linked(snapshot)
            or snapshot.resolve() != directory.resolve() / "source.docx"):
        raise ExternalDocumentError("external_document_worker_owner_invalid")
    snapshot, directory = snapshot.resolve(), directory.resolve()
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ExternalDocumentError("external_document_deadline_invalid")
    seconds = min(75, deadline - time.monotonic()) if deadline is not None else 75
    if stop() or seconds <= 0:
        raise ExternalDocumentError("external_document_cancelled")
    office_source._write_json_new(directory / "native-request.json", {"sha256": _digest(snapshot)})
    options = {"cwd": ROOT, "stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "env": _child_environment()}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([desktop_python(), "-B", "-m", "agent_service.external_document_source_host",
                                "--worker", str(directory), "--seconds", str(seconds)], **options)
    until = time.monotonic() + seconds
    reason = None
    try:
        try:
            process.stdin.write(b"1")
            process.stdin.flush()
        except (OSError, BrokenPipeError):
            reason = "external_document_worker_exited"
        while reason is None and process.poll() is None:
            if stop():
                reason = "external_document_cancelled"
            elif time.monotonic() >= until:
                reason = "external_document_timeout"
            else:
                time.sleep(.05)
        if reason:
            (directory / "native-cancel.flag").touch(exist_ok=True)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _stop_worker(process)
            raise ExternalDocumentError(reason)
        if stop():
            raise ExternalDocumentError("external_document_cancelled")
        if time.monotonic() >= until:
            raise ExternalDocumentError("external_document_timeout")
        if process.returncode != 0 or not (directory / "source-native.json").is_file():
            error = directory / "source-native-error.json"
            if error.is_file():
                value = _read(error)
                raise ExternalDocumentError(value.get("code", "external_document_native_unknown"))
            raise ExternalDocumentError("external_document_native_unknown")
        return _read(directory / "source-native.json")
    finally:
        if process.poll() is None:
            _stop_worker(process)
        process.stdin.close()


def worker(directory, seconds):
    # Import before the watcher thread; COM startup remains parent supervised.
    from . import structured_document_source  # noqa: F401

    if sys.stdin.buffer.read(1) != b"1":
        return 2
    directory = Path(directory).absolute()
    if office_source._linked(directory) or office_source._linked(directory / "source.docx"):
        return 2
    directory = directory.resolve()
    cancelled, finished = threading.Event(), threading.Event()
    deadline = time.monotonic() + seconds

    def watch_parent():
        # An active daemon blocked on BufferedReader.read can abort Python at
        # interpreter shutdown after the native receipt has been committed.
        # The OS-level read watches the same pipe without holding that lock.
        os.read(sys.stdin.fileno(), 1)
        if not finished.is_set():
            cancelled.set()

    def watchdog():
        lost = None
        while not finished.wait(.1):
            now = time.monotonic()
            if cancelled.is_set() and lost is None:
                lost = now
            if now > deadline + 3 or (lost is not None and now - lost > 3):
                os._exit(2)

    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    def stop():
        return cancelled.is_set() or (directory / "native-cancel.flag").exists() or time.monotonic() >= deadline

    try:
        request = _read(directory / "native-request.json")
        snapshot = directory / "source.docx"
        if type(request) is not dict or request != {"sha256": _digest(snapshot)}:
            raise ExternalDocumentError("external_document_source_changed")
        receipt = _native_snapshot(snapshot, stop)
        if stop():
            raise ExternalDocumentError("external_document_cancelled")
        office_source._write_json_new(directory / "source-native.json", receipt)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ExternalDocumentError) else "external_document_native_unknown"
        office_source._write_json_new(directory / "source-native-error.json",
                                      {"ok": False, "state": "unknown", "code": code})
        return 1
    finally:
        finished.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=75)
    args = parser.parse_args()
    return worker(args.worker, min(75, max(.1, args.seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
