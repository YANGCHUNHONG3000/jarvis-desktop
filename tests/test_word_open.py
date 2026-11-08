import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_service import word_open


def _registry(monkeypatch, entries):
    """Install a tiny, observable winreg double keyed by hive/key/view."""
    winreg = types.ModuleType('winreg')
    winreg.HKEY_CURRENT_USER = 1
    winreg.HKEY_LOCAL_MACHINE = 2
    winreg.KEY_READ = 0x20019
    winreg.KEY_WOW64_64KEY = 0x100
    winreg.KEY_WOW64_32KEY = 0x200
    winreg.REG_SZ = 1
    winreg.REG_EXPAND_SZ = 2
    winreg.KEY_SET_VALUE = 0x2
    winreg.KEY_CREATE_SUB_KEY = 0x4
    calls = []

    class Key:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_key(hive, name, reserved, access):
        calls.append((hive, name, reserved, access))
        view = access & (winreg.KEY_WOW64_64KEY | winreg.KEY_WOW64_32KEY)
        entry = entries.get((hive, name, view))
        if entry is None:
            raise FileNotFoundError(name)
        return Key(entry)

    def query_value(key, value_name):
        assert value_name == ''
        return key.value, winreg.REG_SZ

    winreg.OpenKey = open_key
    winreg.QueryValueEx = query_value
    monkeypatch.setitem(sys.modules, 'winreg', winreg)
    return winreg, calls


def _app_key(hive, filename):
    return (hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths' + '\\' + filename)


def test_resolves_local_registered_word_executable(tmp_path, monkeypatch):
    exe = tmp_path / 'Office' / 'WINWORD.EXE'
    exe.parent.mkdir()
    exe.write_bytes(b'fake executable')
    winreg, _ = _registry(monkeypatch, {
        (*_app_key(1, 'WINWORD.EXE'), 0x100): str(exe),
    })

    result = word_open.resolve_word_application()

    assert result == {
        'name': 'Microsoft Word',
        'executable': str(exe.resolve()),
        'flags': (),
    }


@pytest.mark.parametrize('registered_value', [
    '',
    'WINWORD.EXE',
    r'..\Office\WINWORD.EXE',
    r'\\server\share\WINWORD.EXE',
    'https://example.test/WINWORD.EXE',
    r'C:\Program Files\Microsoft Office\WINWORD.EXE /safe',
    r'C:\missing\WINWORD.EXE',
])
def test_rejects_invalid_registered_executable_values(registered_value, monkeypatch):
    monkeypatch.setattr(word_open, '_registered_app_paths', lambda: [
        ('WINWORD.EXE', 'Microsoft Word', (), registered_value),
    ])

    with pytest.raises(word_open.WordOpenError):
        word_open.resolve_word_application()


@pytest.mark.parametrize('resolved_value', [
    r'\\server\share\WINWORD.EXE',
    r'\\?\C:\Windows\System32\WINWORD.EXE',
    r'C:\Program Files\Other\OTHER.EXE',
    'WINWORD.EXE',
])
def test_rejects_unsafe_or_mismatched_final_resolved_executable(
        resolved_value, tmp_path, monkeypatch):
    registered_exe = tmp_path / 'Office' / 'WINWORD.EXE'
    registered_exe.parent.mkdir()
    registered_exe.write_bytes(b'fake executable')
    monkeypatch.setattr(word_open, '_registered_app_paths', lambda: [
        ('WINWORD.EXE', 'Microsoft Word', (), str(registered_exe)),
    ])
    original_resolve = Path.resolve

    def resolve_with_redirect(path, *args, **kwargs):
        if path == registered_exe:
            # Simulate a directory/junction redirect without touching a link
            # target or accessing a network path.
            return Path(resolved_value)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve_with_redirect)

    with pytest.raises(word_open.WordOpenError):
        word_open.resolve_word_application()


def test_falls_back_to_wps_when_word_entry_is_invalid(tmp_path, monkeypatch):
    wps = tmp_path / 'WPS Office' / 'wps.exe'
    wps.parent.mkdir()
    wps.write_bytes(b'fake executable')
    monkeypatch.setattr(word_open, '_registered_app_paths', lambda: iter([
        ('WINWORD.EXE', 'Microsoft Word', (), r'C:\missing\WINWORD.EXE'),
        ('wps.exe', 'WPS Office', ('/wps',), str(wps)),
    ]))

    result = word_open.resolve_word_application()

    assert result == {
        'name': 'WPS Office',
        'executable': str(wps.resolve()),
        'flags': ('/wps',),
    }


def test_registry_reads_hkcu_hklm_and_both_views_without_write_access(
        tmp_path, monkeypatch):
    wps = tmp_path / 'wps.exe'
    wps.write_bytes(b'fake executable')
    winreg, calls = _registry(monkeypatch, {
        (2, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\wps.exe',
         0x200): str(wps),
    })

    result = word_open.resolve_word_application()

    assert result['name'] == 'WPS Office'
    assert {(hive, access & 0x300) for hive, _, _, access in calls} == {
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    }
    assert all(reserved == 0 for _, _, reserved, _ in calls)
    assert all(access & winreg.KEY_READ for _, _, _, access in calls)
    assert all(not access & (winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY)
               for _, _, _, access in calls)


def test_request_launches_safe_argv_for_unicode_path_with_spaces(tmp_path, monkeypatch):
    document = tmp_path / '成果 文件' / '中文 报告.docx'
    document.parent.mkdir()
    document.write_bytes(b'docx placeholder')
    executable = tmp_path / 'Office Suite' / 'wps.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'fake executable')
    monkeypatch.setattr(word_open, 'resolve_word_application', lambda: {
        'name': 'WPS Office', 'executable': str(executable), 'flags': ('/wps',),
    })
    popen = Mock()
    monkeypatch.setattr(word_open.subprocess, 'Popen', popen)

    app_name = word_open.request_open_word(document)

    assert app_name == 'WPS Office'
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    assert argv == [str(executable), '/wps', str(document.resolve())]
    assert argv[-1] == str(document.resolve())
    assert popen.call_args.kwargs == {
        'shell': False,
        'stdin': word_open.subprocess.DEVNULL,
        'stdout': word_open.subprocess.DEVNULL,
        'stderr': word_open.subprocess.DEVNULL,
    }


def test_missing_or_unsupported_documents_are_rejected_without_launch(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(word_open.subprocess, 'Popen', popen)
    monkeypatch.setattr(word_open, 'resolve_word_application', Mock())

    for path in ('', None, tmp_path / 'missing.docx', tmp_path / 'notes.txt'):
        with pytest.raises(word_open.WordOpenError):
            word_open.request_open_word(path)

    popen.assert_not_called()
    word_open.resolve_word_application.assert_not_called()


def test_no_registered_application_does_not_fall_back_to_shell(tmp_path, monkeypatch):
    document = tmp_path / 'report.docx'
    document.write_bytes(b'docx placeholder')
    monkeypatch.setattr(word_open, '_registered_app_paths', lambda: iter(()))
    popen = Mock()
    monkeypatch.setattr(word_open.subprocess, 'Popen', popen)

    with pytest.raises(word_open.WordOpenError):
        word_open.request_open_word(document)

    popen.assert_not_called()


def test_launch_failure_is_reported_without_shell_fallback(tmp_path, monkeypatch):
    document = tmp_path / 'report.docx'
    document.write_bytes(b'docx placeholder')
    monkeypatch.setattr(word_open, 'resolve_word_application', lambda: {
        'name': 'WPS Office', 'executable': r'C:\Program Files\WPS\wps.exe',
        'flags': ('/wps',),
    })
    popen = Mock(side_effect=OSError('launch failed'))
    monkeypatch.setattr(word_open.subprocess, 'Popen', popen)

    with pytest.raises(word_open.WordOpenError, match='启动失败'):
        word_open.request_open_word(document)

    popen.assert_called_once()
    assert popen.call_args.kwargs['shell'] is False
