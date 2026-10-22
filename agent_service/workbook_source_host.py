"""Read-only WPS snapshot worker, bounded over startup, COM and parent loss."""
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
from .office_host import ROOT, _stop_worker
from .workbook_source import WorkbookSourceError, _digest, _read


def native_snapshot_supervised(snapshot, directory, stop=lambda: False, deadline=None):
    from run_jarvis_desktop import desktop_python
    snapshot, directory = Path(snapshot).resolve(), Path(directory).resolve()
    if office_source._linked(directory) or snapshot != directory / "source.xlsx":
        raise WorkbookSourceError("workbook_worker_owner_invalid")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise WorkbookSourceError("workbook_deadline_invalid")
    seconds = min(75, deadline - time.monotonic()) if deadline is not None else 75
    if stop() or seconds <= 0:
        raise WorkbookSourceError("workbook_cancelled")
    office_source._write_json_new(directory / "native-request.json", {"sha256": _digest(snapshot)})
    roots = {str(Path(find_spec(name).origin).parent.parent) for name in ("lxml", "openpyxl")}
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join([str(ROOT), *sorted(roots), os.environ.get("PYTHONPATH", "")]))
    options = {"cwd": ROOT, "stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([desktop_python(), "-B", "-m", "agent_service.workbook_source_host",
                                "--worker", str(directory), "--seconds", str(seconds)], **options)
    until = time.monotonic() + seconds
    reason = None
    try:
        try:
            process.stdin.write(b"1")
            process.stdin.flush()
        except (OSError, BrokenPipeError):
            reason = "workbook_worker_exited"
        while reason is None and process.poll() is None:
            if stop():
                reason = "workbook_cancelled"
            elif time.monotonic() >= until:
                reason = "workbook_timeout"
            else:
                time.sleep(.05)
        if reason:
            (directory / "native-cancel.flag").touch(exist_ok=True)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _stop_worker(process)
        if (directory / "source-native.json").is_file():
            return _read(directory / "source-native.json")
        raise WorkbookSourceError(reason or "workbook_native_unknown")
    finally:
        if process.poll() is None:
            _stop_worker(process)
        process.stdin.close()


def worker(directory, seconds):
    # Import before a background stdin reader; see the §29 controlled import
    # evidence. Parent-side supervision also covers this startup period.
    import openpyxl
    from .workbook_source import _native_snapshot
    if sys.stdin.buffer.read(1) != b"1":
        return 2
    directory = Path(directory).resolve()
    cancelled, finished = threading.Event(), threading.Event()
    deadline = time.monotonic() + seconds
    def watch_parent():
        sys.stdin.buffer.read(1)
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
        snapshot = directory / "source.xlsx"
        if _digest(snapshot) != request["sha256"]:
            raise WorkbookSourceError("workbook_source_changed")
        receipt = _native_snapshot(snapshot, stop)
        office_source._write_json_new(directory / "source-native.json", receipt)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, WorkbookSourceError) else "workbook_native_unknown"
        office_source._write_json_new(directory / "source-native-error.json", {"ok": False, "state": "unknown", "code": code})
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
