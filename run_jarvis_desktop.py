"""Jarvis's local desktop launcher. Existing dependencies, one complete entry."""
import argparse
from importlib.util import find_spec
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent


def desktop_python():
    if find_spec("pywinauto"):
        return sys.executable
    candidates = [os.environ.get("JARVIS_DESKTOP_PYTHON", ""),
                  str(ROOT / ".venv-desktop" / "Scripts" / "python.exe"),
                  str(Path.home() / "Downloads" / "Jarvis-Windows-Desktop-v23-Cloud-Candidate"
                      / "jarvis-desktop-candidate" / "offline-source" / ".jarvis-desktop-venv"
                      / "Scripts" / "python.exe")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("Desktop dependencies unavailable; set JARVIS_DESKTOP_PYTHON")


def window(job, autoclose=False):
    import tkinter as tk
    root = tk.Tk()
    root.title("Jarvis · 记事本验证")
    root.geometry("530x230")
    messages = queue.Queue()
    cancel = threading.Event()
    started = False
    finished = False
    began = None
    result = None
    tk.Label(root, text="Jarvis 记事本写入与读回", font=("Microsoft YaHei UI", 15)).pack(pady=12)
    tk.Label(root, text="创建专用测试文档，保留你已有的文档。\n点击开始后请暂时松开鼠标键盘，约需几秒。",
             font=("Microsoft YaHei UI", 10)).pack()
    status = tk.StringVar(value="准备就绪")
    tk.Label(root, textvariable=status, wraplength=490).pack(pady=15)

    def work():
        messages.put(("result", job.run(cancel, lambda msg: messages.put(("progress", msg)))))

    def start():
        nonlocal started, began
        if started:
            return
        started, began = True, time.monotonic()
        print(json.dumps(dict(event="started")), flush=True)
        button.configure(state="disabled")
        threading.Thread(target=work, name="jarvis-notepad", daemon=True).start()

    def close():
        if started and not finished:
            cancel.set()
            status.set("正在停止；已写入的文字会保留。")
        else:
            root.destroy()

    button = tk.Button(root, text="开始", width=16, command=start)
    button.pack()
    tk.Button(root, text="停止 / 关闭", command=close).pack(pady=5)
    root.protocol("WM_DELETE_WINDOW", close)

    def poll():
        nonlocal finished, result
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == "progress":
                    status.set(value)
                else:
                    result, finished = value, True
                    print(json.dumps(value, ensure_ascii=False), flush=True)
                    if autoclose:
                        root.after(500, root.destroy)
                        return
        except queue.Empty:
            pass
        # Keep UI and cancellation responsive even if native/COM calls stall.
        if began and not finished and time.monotonic() - began > 30:
            cancel.set()
            status.set("正在停止超时操作，保留结果。")
        if (job.root / "cancel.flag").exists():
            cancel.set()
        root.after(50, poll)
    root.after(50, poll)
    root.mainloop()
    return 0 if result and result.get("ok") else 1


def supervise(job, interpreter, autoclose):
    """A parent process can stop a stalled GUI/COM worker and retain its result."""
    command = [interpreter, "-B", str(Path(__file__).resolve()), "--worker", str(job.root)]
    if autoclose:
        command.append("--autoclose")
    child = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    lines = queue.Queue()
    def consume():
        for line in child.stdout:
            lines.put(line)
    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    began = None
    def recorded_result():
        try:
            value = json.loads((job.root / "result.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and "ok" in value else None
        except (OSError, ValueError):
            return None

    def terminate_worker():
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    try:
        while child.poll() is None or not lines.empty():
            try:
                line = lines.get(timeout=.1)
                print(line, end="", flush=True)
                try:
                    payload = json.loads(line)
                except ValueError:
                    payload = {}
                if payload.get("event") == "started":
                    began = time.monotonic()
                if "ok" in payload:
                    began = None
            except queue.Empty:
                pass
            if began is not None and time.monotonic() - began > 35:
                recorded = recorded_result()
                if recorded is not None:
                    began = None
                    continue
                terminate_worker()
                recorded = recorded_result()
                if recorded is not None:
                    print(json.dumps(recorded, ensure_ascii=False), flush=True)
                    return 0 if recorded.get("ok") else 1
                failure = dict(ok=False, state="timed_out", code="supervisor_timeout", effects="unknown")
                job.event("supervisor_timeout", effects="unknown")
                try:
                    with (job.root / "result.json").open("x", encoding="utf-8") as f:
                        json.dump(failure, f)
                except FileExistsError:
                    pass
                print(json.dumps(failure), flush=True)
                return 2
    except KeyboardInterrupt:
        recorded = recorded_result()
        if recorded is not None:
            terminate_worker()
            print(json.dumps(recorded, ensure_ascii=False), flush=True)
            return 0 if recorded.get("ok") else 1
        (job.root / "cancel.flag").touch(exist_ok=True)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_worker()
        recorded = recorded_result()
        if recorded is not None:
            print(json.dumps(recorded, ensure_ascii=False), flush=True)
            return 0 if recorded.get("ok") else 1
        failure = dict(ok=False, state="cancelled", effects="unknown" if began else "not_started")
        try:
            with (job.root / "result.json").open("x", encoding="utf-8") as f:
                json.dump(failure, f)
        except FileExistsError:
            pass
        return 130
    reader.join(timeout=1)
    while not lines.empty():
        print(lines.get_nowait(), end="", flush=True)
    if not (job.root / "result.json").exists():
        failure = dict(ok=False, state="failed" if began else "cancelled",
                       code="worker_exited" if began else "cancelled_before_start",
                       effects="unknown" if began else "not_started")
        with (job.root / "result.json").open("x", encoding="utf-8") as f:
            json.dump(failure, f)
        print(json.dumps(failure), flush=True)
        return 1
    recorded = recorded_result()
    if recorded is not None:
        return 0 if recorded.get("ok") else (child.returncode or 1)
    return child.returncode or 1


def main():
    parser = argparse.ArgumentParser(description="Jarvis real Windows Notepad write/readback")
    parser.add_argument("--text", help="Text to enter in a new owned document")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "desktop_runs")
    parser.add_argument("--minimized", action="store_true", help="Exercise restore before writing")
    parser.add_argument("--save", action="store_true", help="Save the owned document through Notepad and read its bytes")
    parser.add_argument("--autoclose", action="store_true", help="Close Jarvis panel after result; preserve Notepad")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        from agent_service.notepad import NotepadJob
        request = json.loads((args.worker / "request.json").read_text(encoding="utf-8"))
        return window(NotepadJob(args.worker, request["text"], request["minimized"], request.get("save", False)), args.autoclose)
    interpreter = desktop_python()
    from agent_service.notepad import NotepadJob, SAMPLE
    if args.text and args.text_file:
        parser.error("Choose --text or --text-file")
    text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text or SAMPLE
    job = NotepadJob.create(args.root, text, args.minimized, args.save)
    print(json.dumps(dict(workspace=str(job.root)), ensure_ascii=False), flush=True)
    return supervise(job, interpreter, args.autoclose)


if __name__ == "__main__":
    raise SystemExit(main())
