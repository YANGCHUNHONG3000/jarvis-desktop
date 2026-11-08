"""Only the already-verified DOCX opening path should use the direct editor."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import word_open
from test_office_chain_drafts_panel import main_function


def _opener(monkeypatch, calls, statuses):
    monkeypatch.setattr(word_open, 'request_open_word', lambda path: calls.append(('word', path)) or 'WPS Office')
    env = {'os': SimpleNamespace(startfile=lambda path: calls.append(('shell', path))),
           'task_status': SimpleNamespace(set=statuses.append)}
    return main_function('open_artifact', env)


def test_docx_uses_direct_editor_without_mutating_file(tmp_path, monkeypatch):
    file = tmp_path / '保留成果.docx'
    file.write_bytes(b'existing artifact')
    calls, statuses = [], []
    fn = _opener(monkeypatch, calls, statuses)
    before = file.read_bytes(), file.stat().st_mtime_ns
    assert fn(file) is True
    assert calls == [('word', file)]
    assert '已请求 WPS Office' in statuses[-1] and '已打开' not in statuses[-1]
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


def test_direct_editor_failure_never_falls_back_to_app_picker(tmp_path, monkeypatch):
    file = tmp_path / 'report.docx'
    file.write_bytes(b'unchanged')
    calls, statuses = [], []
    fn = _opener(monkeypatch, calls, statuses)
    def fail(_):
        raise word_open.WordOpenError('未找到已安装的办公程序。')
    monkeypatch.setattr(word_open, 'request_open_word', fail)
    assert fn(file) is False and calls == []
    assert statuses[-1] == '无法打开：未找到已安装的办公程序。'


@pytest.mark.parametrize('suffix,directory', [('.docx', True), ('.xlsx', False), ('.txt', False)])
def test_other_open_routes_preserved(tmp_path, monkeypatch, suffix, directory):
    file = tmp_path / ('artifact' + suffix)
    file.write_bytes(b'existing')
    calls, statuses = [], []
    assert _opener(monkeypatch, calls, statuses)(file, directory=directory) is True
    assert calls == [('shell', str(file.parent if directory else file))]


@pytest.mark.parametrize('entry', ['open_history_path', 'office_chain_open_result', 'open_path'])
@pytest.mark.parametrize('verified', [True, False])
def test_receipt_gate_still_precedes_direct_editor(tmp_path, monkeypatch, entry, verified):
    file = tmp_path / 'existing.docx'
    file.write_bytes(b'verified externally')
    calls, statuses = [], []
    opener = _opener(monkeypatch, calls, statuses)
    status = SimpleNamespace(set=statuses.append)
    env = {'Path': Path, 'TASK_ROOT': tmp_path, 'task_status': status, 'history_detail': status,
           'open_artifact': opener, 'office_chain_status': statuses.append,
           '_verified_history_path': lambda *_: file if verified else None,
           'history_artifacts': {'item': (file, 'a' * 64)},
           'history_output_choice': SimpleNamespace(get=lambda: 'item'),
           'last_paths': {'agent': file}, 'agent_outputs': {'item': file},
           'agent_output_hashes': {'item': 'a' * 64},
           'agent_output_choice': SimpleNamespace(get=lambda: 'item')}
    fn = main_function(entry, env)
    if entry == 'open_path':
        fn('agent')
    elif entry == 'office_chain_open_result':
        fn({'path': str(file), 'sha256': 'a' * 64})
    else:
        fn()
    assert calls == ([('word', file)] if verified else [])
