from pathlib import Path
import hashlib
import sys
from types import ModuleType

import pytest

from run_jarvis_assistant import (
    _history_state_label,
    _history_list_state_label,
    _resume_reason_label,
    _source_file_argument,
    _task_command,
    _validate_source_file,
    _verified_history_path,
)


@pytest.mark.parametrize(
    ("state", "label"),
    [("succeeded", "成功"), ("recovered", "部分完成"),
     ("needs_review", "待核验"), ("failed", "失败"),
     ("unsupported", "暂不支持"),
     ("unknown", "待核验"), (None, "待核验")],
)
def test_history_states_are_never_promoted_to_success(state, label):
    assert _history_state_label(state) == label


def test_history_listing_marks_metadata_as_unverified():
    assert _history_list_state_label("succeeded") == "记录成功·待核验"
    assert _history_list_state_label("recovered") == "记录部分完成·待核验"
    assert _history_list_state_label("needs_review") == "待核验"
    assert "不能直接续跑" in _resume_reason_label("resume_skill_owned")


def test_history_artifact_must_still_match_inspection_hash_when_opened(tmp_path):
    artifact = tmp_path / "verified.docx"
    artifact.write_bytes(b"first content")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert _verified_history_path(str(artifact), digest, tmp_path) == artifact.resolve()

    artifact.write_bytes(b"changed after inspection")
    assert _verified_history_path(str(artifact), digest, tmp_path) is None


def test_source_path_accepts_supported_existing_file_and_quotes(tmp_path):
    source = tmp_path / "本周数据.csv"
    source.write_text("name,value\nA,1\n", encoding="utf-8")

    assert _validate_source_file(f'"{source}"') == source.resolve()


@pytest.mark.parametrize("suffix", [".txt", ".xls", ".pdf", ""])
def test_source_path_rejects_unsupported_extension(tmp_path, suffix):
    source = tmp_path / ("source" + suffix)
    source.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="仅支持"):
        _validate_source_file(str(source))


def test_source_path_rejects_missing_file_even_when_extension_is_valid(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        _validate_source_file(str(tmp_path / "missing.xlsx"))


def test_cli_source_file_is_initial_selection_only_and_command_forwards_exact_path(tmp_path):
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx placeholder")
    selected = _source_file_argument(["--source-file", str(source), "--other", "ignored"])
    assert selected == source.resolve()

    command = _task_command(tmp_path / "request.txt", tmp_path / "task", str(selected))
    assert command[-2:] == ["--source-file", str(selected)]
    assert _source_file_argument([]) is None


def test_invalid_cli_source_does_not_become_an_executable_path(tmp_path):
    invalid = _source_file_argument(["--source-file", str(tmp_path / "gone.csv")])
    assert isinstance(invalid, ValueError)


def test_main_builds_history_widgets_before_binding_and_starts_headlessly(monkeypatch):
    import run_jarvis_assistant as launcher

    class Widget:
        def __init__(self, *args, **kwargs):
            self.items = []

        def __getattr__(self, name):
            if name in {"pack", "configure", "title", "geometry", "protocol", "after", "add",
                        "mainloop", "heading", "column", "bind", "delete", "insert", "set"}:
                return lambda *args, **kwargs: None
            if name == "get_children":
                return lambda: ()
            if name == "selection":
                return lambda: ()
            if name == "get":
                return lambda *args, **kwargs: ""
            raise AttributeError(name)

    class Variable:
        def __init__(self, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

        def trace_add(self, *_args):
            return "trace"

    tk = ModuleType("tkinter")
    tk.Tk = Widget
    tk.StringVar = Variable
    tk.IntVar = Variable
    tk.BooleanVar = Variable
    tk.Text = Widget
    ttk = ModuleType("tkinter.ttk")
    for name in ("Notebook", "Frame", "Label", "Entry", "Combobox", "Button", "Treeview", "Checkbutton"):
        setattr(ttk, name, Widget)
    tk.ttk = ttk
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)

    class NoStartThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(launcher.threading, "Thread", NoStartThread)
    launcher.main([])
