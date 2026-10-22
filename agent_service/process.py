"""Owned subprocess tree for the fixed browser reader, not a shell capability."""
import os
from pathlib import Path
import signal
import subprocess
import time


class WindowsJob:
    def __init__(self, process):
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                        ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                        ("active", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(c.Structure):
            _fields_ = [("basic", Limits), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        self.api = c.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.api.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info))
                or not self.api.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle)))):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_child(command, should_stop, timeout):
    """The child must wait for one stdin byte before starting any external work."""
    if should_stop():
        raise InterruptedError("Stop requested")
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   cwd=Path(__file__).resolve().parents[1])
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    owner = None
    try:
        if os.name == "nt":
            owner = WindowsJob(process)
        process.stdin.write(b"1")
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + timeout
        while True:
            if should_stop():
                raise InterruptedError("Stop requested")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Browser operation exceeded its total deadline")
            try:
                output, _ = process.communicate(timeout=min(0.1, remaining))
                if process.returncode:
                    raise RuntimeError("Browser reader failed")
                return output.decode("utf-8")
            except subprocess.TimeoutExpired:
                continue
    finally:
        if owner:
            # Kills browser descendants even if the Python child already exited.
            owner.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout):
            if pipe:
                pipe.close()
