"""Registered spreadsheet launch: fixed executable and passive explicit file."""
from pathlib import Path
import pytest
from agent_service import workbook_open as opening, windows_file_dialog as files
from agent_service.computer import ComputerError
from agent_service.word_open import WordOpenError


@pytest.fixture
def installation(tmp_path, monkeypatch):
    wps, et = tmp_path / 'wps.exe', tmp_path / 'et.exe'
    wps.write_bytes(b'installed test placeholder')
    et.write_bytes(b'installed test placeholder')
    monkeypatch.setattr(opening, '_registered_app_paths', lambda: [('wps.exe', 'WPS', (), str(wps))])
    return et


def test_registered_companion_is_resolved(installation):
    assert Path(opening.resolve_workbook_application()['executable']) == installation


def test_no_registered_wps_never_uses_shell(monkeypatch):
    monkeypatch.setattr(opening, '_registered_app_paths', lambda: [])
    with pytest.raises(WordOpenError): opening.resolve_workbook_application()


def test_launch_is_one_fixed_argument_array_after_file_validation(installation, monkeypatch, tmp_path):
    path = tmp_path / 'approved & spaced.xlsx'
    calls = []
    monkeypatch.setattr(files, 'snapshot_file', lambda p: {'path': str(path)})
    monkeypatch.setattr(opening.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)))
    assert opening.request_open_workbook(path) == 'WPS 表格'
    assert len(calls) == 1 and calls[0][0] == [str(installation), str(path)]
    assert calls[0][1]['shell'] is False


def test_unsafe_workbook_never_launches(installation, monkeypatch):
    def denied(path): raise ComputerError('windows_file_active_content')
    monkeypatch.setattr(files, 'snapshot_file', denied)
    monkeypatch.setattr(opening.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(ComputerError): opening.request_open_workbook('unsafe.xlsx')


def test_wrong_file_type_never_launches(installation, monkeypatch, tmp_path):
    monkeypatch.setattr(files, 'snapshot_file', lambda p: {'path': str(tmp_path / 'wrong.docx')})
    monkeypatch.setattr(opening.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(WordOpenError): opening.request_open_workbook('wrong.docx')


@pytest.mark.parametrize('source_value,executable_value', [
    (r'\\server\share\wps.exe', r'\\server\share\et.exe'),
    (r'\\?\C:\WPS\wps.exe', r'\\?\C:\WPS\et.exe'),
    (r'C:\WPS\wps.exe', r'C:\WPS\other.exe'),
    (r'C:\WPS\other.exe', r'C:\WPS\et.exe'),
    ('wps.exe', 'et.exe'),
    (r'C:\WPS\wps.exe', r'C:\OtherWPS\et.exe'),
])
def test_resolved_companions_must_both_remain_local_named_executables_in_same_directory(
        installation, monkeypatch, source_value, executable_value):
    original = installation.parent / 'wps.exe'
    real_resolve = Path.resolve
    resolved = []

    def redirect(path, *args, **kwargs):
        if path in (original, installation):
            assert kwargs.get('strict') is True
            resolved.append(path)
            return Path(source_value if path == original else executable_value)
        return real_resolve(path, *args, **kwargs)

    # Simulate parent junction redirection without opening links or a network.
    monkeypatch.setattr(Path, 'resolve', redirect)
    with pytest.raises(WordOpenError):
        opening.resolve_workbook_application()
    assert resolved == [original, installation]


@pytest.mark.parametrize('which', ['source', 'companion'])
def test_disappeared_resolved_executable_is_rejected(installation, monkeypatch, which):
    original = installation.parent / 'wps.exe'
    target = original if which == 'source' else installation
    real_resolve = Path.resolve

    def disappear(path, *args, **kwargs):
        if path == target:
            raise FileNotFoundError('installation changed')
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', disappear)
    with pytest.raises(WordOpenError):
        opening.resolve_workbook_application()


@pytest.mark.parametrize('registered_value', [
    'wps.exe', r'\\server\share\wps.exe', '"C:\\WPS\\wps.exe',
    'C:\\WPS\\wps.exe"', 'C:\\WPS\\wps.exe\x00',
])
def test_registered_value_cannot_be_relative_unc_or_malformed_quote(installation, monkeypatch, registered_value):
    monkeypatch.setattr(opening, '_registered_app_paths', lambda: [('wps.exe', 'WPS', (), registered_value)])
    with pytest.raises(WordOpenError):
        opening.resolve_workbook_application()


def test_registered_source_must_itself_be_named_wps_exe(installation, monkeypatch):
    other = installation.parent / 'other.exe'
    other.write_bytes(b'not the registered application name')
    monkeypatch.setattr(opening, '_registered_app_paths', lambda: [('wps.exe', 'WPS', (), str(other))])
    with pytest.raises(WordOpenError):
        opening.resolve_workbook_application()


def test_paired_outer_quotes_are_accepted(installation, monkeypatch):
    source = installation.parent / 'wps.exe'
    monkeypatch.setattr(opening, '_registered_app_paths', lambda: [('wps.exe', 'WPS', (), '"' + str(source) + '"')])
    assert Path(opening.resolve_workbook_application()['executable']) == installation
