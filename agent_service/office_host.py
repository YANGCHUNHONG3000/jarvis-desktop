"""Bounded native Office worker; never terminates a user's Office process."""
import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
MAX_RECORD = 1_000_000


def _write_new(path, value):
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())


def _outcome(root):
    try:
        path = root / "worker-outcome.json"
        if path.is_symlink() or path.stat().st_size > MAX_RECORD:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if type(value) is not dict or type(value.get("ok")) is not bool:
            return None
        if value["ok"]:
            artifact = Path(value["artifact"]).resolve()
            if (artifact.parent != root.resolve() or artifact.suffix not in {".docx", ".xlsx"}
                    or artifact.stat().st_size > 20_000_000
                    or sha256(artifact.read_bytes()).hexdigest() != value.get("sha256")
                    or value.get("application_verified") is not True or value.get("file_verified") is not True):
                return None
        return value
    except (KeyError, OSError, ValueError, TypeError):
        return None


def _stop_worker(process):
    """Bounded cleanup for this Popen child only; uncertainty stays unknown."""
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        kill = getattr(process, "kill", None)
        if callable(kill):
            try:
                kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def execute_office_supervised(spec, step_root, should_stop=lambda: False, *, timeout=75):
    from .office import validate_spec
    from run_jarvis_desktop import desktop_python
    checked = validate_spec(spec)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 75:
        raise ValueError("office_timeout_invalid")
    root = Path(step_root)
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError("office_root_invalid")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if should_stop():
        return {"ok": False, "state": "cancelled", "code": "cancelled_before_start"}
    request = root / "worker-request.json"
    if any((root / name).exists() for name in ("worker-outcome.json", "document.docx", "spreadsheet.xlsx",
                                               "staging-document.docx", "staging-spreadsheet.xlsx")):
        raise ValueError("office_output_conflict")
    # A prior attempt is never automatically replayed, even if it was interrupted.
    _write_new(request, {"spec": checked})
    options = dict(cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([desktop_python(), "-B", "-m", "agent_service.office_host",
                                "--worker", str(root), "--seconds", str(timeout)], **options)
    deadline = time.monotonic() + timeout
    reason = None
    try:
        # Keep stdin open: the worker's watcher detects parent loss on EOF.
        try:
            process.stdin.write(b"1")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            return {"ok": False, "state": "unknown", "code": "office_worker_exited", "effects": "unknown"}
        while process.poll() is None:
            if should_stop():
                reason = "office_cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "office_timeout"
                break
            time.sleep(.05)
        if reason:
            (root / "cancel.flag").touch(exist_ok=True)
            # Allow an in-flight call to return and publish an accurate receipt.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _stop_worker(process)
        value = _outcome(root)
        if value is not None:
            return value
        return {"ok": False, "state": "unknown", "code": reason or "office_worker_exited",
                "effects": "unknown"}
    finally:
        if process.poll() is None:
            _stop_worker(process)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass


def worker(root, seconds):
    if sys.stdin.buffer.read(1) != b"1":
        return 2
    root = Path(root).resolve()
    cancelled = threading.Event()
    finished = threading.Event()
    deadline = time.monotonic() + seconds

    def watch_parent():
        sys.stdin.buffer.read(1)
        if not finished.is_set():
            cancelled.set()

    def watchdog():
        parent_lost_at = None
        while not finished.wait(.1):
            now = time.monotonic()
            if cancelled.is_set() and parent_lost_at is None:
                parent_lost_at = now
            if now > deadline + 3 or (parent_lost_at is not None and now - parent_lost_at > 3):
                # Only this worker exits. COM server/documents remain untouched.
                os._exit(2)

    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    def stop():
        return cancelled.is_set() or (root / "cancel.flag").exists() or time.monotonic() >= deadline

    try:
        request = root / "worker-request.json"
        if request.stat().st_size > MAX_RECORD:
            raise ValueError("request_oversize")
        payload = json.loads(request.read_text(encoding="utf-8"))
        from .office import execute_office
        result = execute_office(payload["spec"], root, stop)
    except Exception:
        result = {"ok": False, "state": "unknown", "code": "office_worker_failed"}
    try:
        _write_new(root / "worker-outcome.json", result)
        # Parent reads durable outcome; stdout is deliberately small to avoid pipe stalls.
        print(json.dumps({"ok": result.get("ok") is True}), flush=True)
    finally:
        finished.set()
    return 0 if result.get("ok") is True else 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=75)
    args = parser.parse_args(argv)
    return worker(args.worker, min(75, max(1, args.seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
