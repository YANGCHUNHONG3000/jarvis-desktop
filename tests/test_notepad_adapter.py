import builtins
import json
import threading

import pytest

from agent_service.notepad import DesktopError, NotepadJob, literal_keys, normalize_text, validate_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("你好\n", "你好\n")],
)
def test_normalize_text(value, expected):
    assert normalize_text(value) == expected


def test_validate_text_normalizes_and_checks_limits():
    assert validate_text("a\r\nb") == "a\nb"
    for bad in ("", "x" * 12001, None, "a\x00b"):
        with pytest.raises(ValueError):
            validate_text(bad)


def test_literal_keys_escapes_sendkeys_metacharacters():
    assert literal_keys("{}+^%~()abc") == "{{}{}}{+}{^}{%}{~}{(}{)}abc"


def test_create_uses_new_owned_directory_and_blank_seed(tmp_path):
    sentinel = tmp_path / "personal.txt"
    sentinel.write_text("leave me alone", encoding="utf-8")
    job = NotepadJob.create(tmp_path, "hello\r\n世界", minimized=True, save=True)

    assert job.root.parent == tmp_path.resolve()
    assert job.root.is_dir()
    assert job.document.exists() and job.document.read_bytes() == b""
    assert sentinel.read_text(encoding="utf-8") == "leave me alone"
    request = json.loads((job.root / "request.json").read_text(encoding="utf-8"))
    assert request == {"text": "hello\n世界", "minimized": True, "save": True}


def test_preset_cancel_does_not_load_windows_dependencies(tmp_path, monkeypatch):
    job = NotepadJob.create(tmp_path, "cancel me")
    cancel = threading.Event()
    cancel.set()
    real_import = builtins.__import__
    attempted = []

    def guarded_import(name, *args, **kwargs):
        if name == "pythoncom" or name.startswith(("pywinauto", "win32")):
            attempted.append(name)
            raise AssertionError("Windows dependencies must not load for pre-cancelled work")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = job.run(cancel=cancel)

    assert result["state"] == "cancelled"
    assert result["code"] == "cancelled"
    assert attempted == []
