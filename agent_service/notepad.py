"""Owned Notepad document write/readback, independent of the voice Runtime.

Dependencies are loaded only in the Windows worker. Never chooses an existing
personal document. There is one write attempt; uncertainty is retained on disk.
"""
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid


SAMPLE = "Jarvis desktop acceptance sample.\n你好，贾维斯。中文写入与读回验证。"
MAX_TEXT = 12000


class DesktopError(RuntimeError):
    pass


def normalize_text(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def literal_keys(text):
    """Escape pywinauto's SendKeys grammar; content cannot become a shortcut."""
    return "".join("{" + c + "}" if c in "{}+^%~()" else c for c in text)


def validate_text(text):
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
        raise ValueError("Text must contain 1..12000 characters")
    if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise ValueError("Unsupported control character")
    return normalize_text(text)


def process_image(handle):
    """Use the documented limited-query handle API (works with packaged Notepad)."""
    import ctypes
    from ctypes import wintypes
    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                      ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not query(int(handle), 0, buffer, ctypes.byref(size)):
        raise DesktopError("process_image_unavailable")
    image = Path(buffer.value)
    normalized = str(image).casefold()
    system = str(Path(os.environ["SystemRoot"]) / "System32" / "notepad.exe").casefold()
    packaged = str(Path(os.environ["ProgramFiles"]) / "WindowsApps" / "Microsoft.WindowsNotepad_").casefold()
    if image.name.casefold() != "notepad.exe" or not (normalized == system or normalized.startswith(packaged)):
        raise DesktopError("unexpected_application")
    return str(image)


@dataclass
class NotepadJob:
    root: Path
    text: str
    minimized: bool = False
    save: bool = False

    @classmethod
    def create(cls, parent, text=SAMPLE, minimized=False, save=False):
        text = validate_text(text)
        root = Path(parent).resolve() / ("notepad-" + uuid.uuid4().hex)
        root.mkdir(parents=True, exist_ok=False)
        job = cls(root, text, bool(minimized), bool(save))
        with (root / "request.json").open("x", encoding="utf-8") as f:
            json.dump(dict(text=text, minimized=job.minimized, save=job.save), f, ensure_ascii=False)
        # An empty file identifies our document; the content is written ONLY via UI.
        job.document.touch(exist_ok=False)
        return job

    @property
    def document(self):
        return self.root / (self.root.name + ".txt")

    def event(self, stage, **fields):
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(at=time.time(), stage=stage, **fields), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def run(self, cancel=None, progress=lambda message: None, timeout=30):
        cancel = cancel or threading.Event()
        began = time.monotonic()
        deadline = began + timeout
        write_started = False
        result = dict(ok=False, state="failed", document=str(self.document),
                      effects="not_started", expected_sha256=sha256(self.text.encode()).hexdigest())

        def check():
            if cancel.is_set():
                raise DesktopError("cancelled")
            if time.monotonic() >= deadline:
                raise DesktopError("deadline_exceeded")

        def emit(stage, label, **data):
            check()
            self.event(stage, **data)
            progress(label)

        process_handle = None
        pythoncom = None
        try:
            check()
            if os.name != "nt":
                raise DesktopError("windows_required")
            import pythoncom
            pythoncom.CoInitialize()
            from pywinauto import Desktop
            import win32api
            import win32con
            import win32gui
            import win32process
            result["dependencies"] = {n: version(n) for n in ("pywinauto", "comtypes", "pywin32")}
            input_epoch = win32api.GetLastInputInfo()
            if self.document.read_bytes() != b"":
                raise DesktopError("seed_document_not_blank")
            emit("launch", "正在打开专用测试文档")
            subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32" / "notepad.exe"),
                              str(self.document)])
            desktop = Desktop(backend="uia")
            title_pattern = re.compile(r"^\*?" + re.escape(self.document.name) + r"(?: - (?:Notepad|记事本))?$")
            hwnd = None
            while not hwnd:
                check()
                matches = []
                def visit(h, unused):
                    title = win32gui.GetWindowText(h)
                    if win32gui.IsWindowVisible(h) and title_pattern.fullmatch(title):
                        matches.append(h)
                win32gui.EnumWindows(visit, None)
                if len(matches) > 1:
                    raise DesktopError("ambiguous_window")
                if matches:
                    hwnd = matches[0]
                else:
                    cancel.wait(.1)
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            process_handle = win32api.OpenProcess(0x1000 | 0x100000, False, pid)
            created = win32process.GetProcessTimes(process_handle)["CreationTime"]
            image = process_image(process_handle)
            result.update(hwnd=hwnd, pid=pid, process_created=str(created), process_image=image)

            def identity():
                check()
                if (not win32gui.IsWindow(hwnd)
                        or win32process.GetWindowThreadProcessId(hwnd)[1] != pid
                        or win32process.GetProcessTimes(process_handle)["CreationTime"] != created
                        or not title_pattern.fullmatch(win32gui.GetWindowText(hwnd))):
                    raise DesktopError("document_identity_changed")
                if win32api.GetLastInputInfo() != input_epoch:
                    raise DesktopError("user_takeover")

            if self.minimized:
                identity()
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                until = min(deadline, time.monotonic() + 3)
                while not win32gui.IsIconic(hwnd):
                    identity()
                    if time.monotonic() > until:
                        raise DesktopError("minimize_setup_failed")
                    cancel.wait(.05)
                emit("minimized", "正在验证最小化窗口还原")
            identity()
            was_minimized = bool(win32gui.IsIconic(hwnd))
            if was_minimized:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            # The visible Start button grants this worker process foreground rights.
            if win32gui.GetForegroundWindow() != hwnd:
                win32gui.SetForegroundWindow(hwnd)
            until = min(deadline, time.monotonic() + 4)
            while win32gui.IsIconic(hwnd) or win32gui.GetForegroundWindow() != hwnd:
                identity()
                if time.monotonic() > until:
                    raise DesktopError("window_not_ready")
                cancel.wait(.05)
            emit("ready", "窗口已就绪，定位编辑区", restored=was_minimized)

            def find_editor():
                identity()
                window = desktop.window(handle=hwnd).wrapper_object()
                nodes = window.descendants()
                if len(nodes) > 512:
                    raise DesktopError("control_limit")
                docs = [e for e in nodes if e.element_info.control_type == "Document"
                        and e.is_visible() and e.is_enabled()]
                candidates = docs or [e for e in nodes if e.element_info.control_type == "Edit"
                                      and e.is_visible() and e.is_enabled()]
                if len(candidates) != 1:
                    raise DesktopError("editor_not_unique")
                e = candidates[0]
                if e.element_info.process_id != pid or e.element_info.element.CurrentIsPassword:
                    raise DesktopError("editor_unsafe")
                return e

            def read(e):
                from pywinauto.uia_defines import NoPatternInterfaceError
                try:
                    value = e.iface_value
                except NoPatternInterfaceError:
                    text = e.iface_text.DocumentRange.GetText(MAX_TEXT + 2)
                    route = "text_pattern"
                else:
                    if value.CurrentIsReadOnly:
                        raise DesktopError("editor_readonly")
                    text, route = value.CurrentValue, "value_pattern"
                if len(text) > MAX_TEXT + 1:
                    raise DesktopError("text_limit")
                return normalize_text(text), route

            editor = find_editor()
            runtime_id = tuple(editor.element_info.element.GetRuntimeId())
            before, route = read(editor)
            if route != "value_pattern":
                # Do not expose global keyboard input before that route is validated.
                raise DesktopError("writable_value_pattern_required")
            if before not in ("", "\n"):
                raise DesktopError("editor_not_blank")
            editor.element_info.element.SetFocus()
            identity()
            if (win32gui.GetForegroundWindow() != hwnd
                    or not editor.element_info.element.CurrentHasKeyboardFocus):
                raise DesktopError("editor_not_focused")
            editor = find_editor()
            if tuple(editor.element_info.element.GetRuntimeId()) != runtime_id or read(editor)[0] != before:
                raise DesktopError("editor_changed")
            emit("write_intent", "正在写入文字", route=route, characters=len(self.text))
            identity()
            if (win32gui.GetForegroundWindow() != hwnd
                    or not editor.element_info.element.CurrentHasKeyboardFocus
                    or read(editor)[0] != before):
                raise DesktopError("editor_changed_before_write")
            write_started = True
            result["effects"] = "unknown"
            editor.iface_value.SetValue(self.text)
            emit("readback", "正在重新读取编辑区并核对")
            fresh = find_editor()
            if tuple(fresh.element_info.element.GetRuntimeId()) != runtime_id:
                raise DesktopError("editor_changed_after_write")
            actual, read_route = read(fresh)
            if actual != self.text:
                raise DesktopError("readback_mismatch")
            result.update(ui_verified=True,
                          file_saved=False, route=route,
                          read_route=read_route, restored=was_minimized, characters=len(actual),
                          readback_sha256=sha256(actual.encode()).hexdigest(), readback=actual)
            self.event("verified", sha256=result["readback_sha256"], characters=len(actual))
            if self.save:
                emit("save", "正在通过记事本菜单保存专用文档")
                identity()
                window = desktop.window(handle=hwnd).wrapper_object()
                menus = [e for e in window.descendants(control_type="MenuItem")
                         if e.element_info.automation_id == "File"]
                if len(menus) != 1:
                    raise DesktopError("file_menu_unavailable")
                menus[0].iface_invoke.Invoke()
                until = min(deadline, time.monotonic() + 3)
                saves = []
                while not saves:
                    identity()
                    saves = [e for e in window.descendants(control_type="MenuItem")
                             if e.is_visible() and e.is_enabled()
                             and e.element_info.name.split("\t")[0] in {"保存", "Save"}]
                    if time.monotonic() >= until:
                        raise DesktopError("save_menu_unavailable")
                    if not saves:
                        cancel.wait(.05)
                if len(saves) != 1:
                    raise DesktopError("save_menu_ambiguous")
                identity()
                saves[0].iface_invoke.Invoke()
                until = min(deadline, time.monotonic() + 4)
                while True:
                    identity()
                    raw = self.document.read_bytes()
                    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                    if normalize_text(raw.decode(encoding)) == self.text:
                        break
                    if time.monotonic() >= until:
                        raise DesktopError("saved_file_readback_mismatch")
                    cancel.wait(.05)
                result.update(file_saved=True, file_sha256=sha256(raw).hexdigest(), file_bytes=len(raw))
                self.event("file_verified", sha256=result["file_sha256"], bytes=len(raw))
            result.update(ok=True, state="succeeded", effects="verified")
            progress("写入、保存与文件读回通过" if self.save else "写入与编辑区读回通过（尚未保存）")
        except Exception as error:
            result.update(code=str(error) if isinstance(error, DesktopError) else type(error).__name__,
                          state="cancelled" if cancel.is_set() else "failed",
                          effects="unknown" if write_started else "not_started")
            self.event("failed", code=result["code"], effects=result["effects"])
            progress("已停止：" + result["code"])
        finally:
            try:
                if process_handle is not None:
                    process_handle.Close()
                if pythoncom is not None:
                    pythoncom.CoUninitialize()
            except Exception:
                result["cleanup_warning"] = True
            result["elapsed_seconds"] = round(time.monotonic() - began, 3)
            result["implementation_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
            with (self.root / "result.json").open("x", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        return result
