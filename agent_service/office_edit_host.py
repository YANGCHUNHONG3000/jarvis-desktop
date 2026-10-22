"""Bounded editing worker with durable intent and no automatic replay."""
import argparse
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .office_edit import _bind, _read, _write, verify_edit_result
from .office_host import ROOT, _stop_worker
from .office_source import _linked


def execute_edit_supervised(spec, root, source_root, stop=lambda: False, deadline=None):
    from run_jarvis_desktop import desktop_python
    spec, _ = _bind(spec, source_root)
    root = Path(root).absolute()
    if _linked(root):
        raise ValueError("edit_root_invalid")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if any(root.iterdir()):
        raise ValueError("edit_output_conflict")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("edit_deadline_invalid")
    seconds = min(75, deadline - time.monotonic()) if deadline is not None else 75
    if stop() or seconds <= 0:
        return {"ok": False, "state": "cancelled", "code": "cancelled_before_start"}
    _write(root / "worker-request.json", {"spec": spec, "source_root": str(Path(source_root).resolve())})
    options = dict(cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    # The existing COM runtime is a same-version venv while OOXML libraries are
    # installed in the launcher interpreter. Reuse those installed packages;
    # neither install packages nor change the user's runtime configuration.
    package_roots = {str(Path(find_spec(name).origin).parent.parent) for name in ("lxml", "openpyxl")}
    options["env"]["PYTHONPATH"] = os.pathsep.join([str(ROOT), *sorted(package_roots),
                                                    os.environ.get("PYTHONPATH", "")])
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([desktop_python(), "-B", "-m", "agent_service.office_edit_host",
                                "--worker", str(root), "--seconds", str(seconds)], **options)
    until = time.monotonic() + seconds
    reason = None
    try:
        try:
            process.stdin.write(b"1")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            reason = "edit_worker_exited"
        while reason is None and process.poll() is None:
            if stop():
                reason = "edit_cancelled"
            elif time.monotonic() >= until:
                reason = "edit_timeout"
            else:
                time.sleep(.05)
        if reason:
            (root / "cancel.flag").touch(exist_ok=True)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _stop_worker(process)
        try:
            result = _read(root / "worker-outcome.json")
            if type(result) is not dict or type(result.get("ok")) is not bool:
                raise ValueError("edit_outcome_invalid")
            if result["ok"]:
                if Path(result["artifact"]).parent != root:
                    raise ValueError("edit_outcome_owner_invalid")
                verify_edit_result(result, source_root, spec)
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return {"ok": False, "state": "unknown", "code": reason or "edit_worker_exited", "effects": "unknown"}
    finally:
        if process.poll() is None:
            _stop_worker(process)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()


def worker(root, seconds):
    from .office_edit import execute_edit
    _write(Path(root) / "worker-ready.json", {"ready": True})
    if sys.stdin.buffer.read(1) != b"1":
        return 2
    # On this Windows runtime NumPy's native import (an optional openpyxl
    # dependency) blocks while a background buffered-stdin read is active.
    # Import the installed library before starting that watcher. The supervisor
    # still bounds startup and can stop this worker during dependency import.
    import openpyxl
    root = Path(root).resolve()
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
    _write(root / "worker-gated.json", {"gated": True})
    def stop():
        return cancelled.is_set() or (root / "cancel.flag").exists() or time.monotonic() >= deadline
    try:
        payload = _read(root / "worker-request.json")
        result = execute_edit(payload["spec"], root, payload["source_root"], stop)
    except Exception:
        result = {"ok": False, "state": "unknown", "code": "edit_worker_failed"}
    _write(root / "worker-outcome.json", result)
    finished.set()
    return 0 if result.get("ok") else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=75)
    args = parser.parse_args()
    return worker(args.worker, min(75, max(.1, args.seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
