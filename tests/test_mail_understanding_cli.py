"""Offline CLI dispatch checks for explicit mail-understanding mode."""

import json
from types import SimpleNamespace

import run_jarvis_task
import pytest


def _inputs(tmp_path):
    instruction = tmp_path / "instruction.txt"
    instruction.write_text("Create a short action summary", encoding="utf-8")
    source = {"version": 1, "artifact": str(tmp_path / "mail.docx"), "sha256": "a" * 64}
    return instruction, source


def test_mail_flag_dispatches_to_mail_module_with_verified_reference(tmp_path, monkeypatch, capsys):
    instruction, source = _inputs(tmp_path)
    calls = []
    fake = SimpleNamespace(run_task=lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"ok": True, "state": "succeeded"}))
    monkeypatch.setattr(run_jarvis_task, "run_task",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("assistant path")))
    monkeypatch.setattr("agent_service.mail_understanding", fake, raising=False)
    root = tmp_path / "runs"
    task_root = root / ("task-" + "b" * 32)

    code = run_jarvis_task.main([
        "--text-file", str(instruction), "--root", str(root), "--task-root", str(task_root),
        "--source-artifact-json", json.dumps(source), "--mail-understanding",
    ])

    assert code == 0
    assert calls == [(("Create a short action summary", root),
                      {"task_root": task_root, "source_artifact": source})]
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"


def test_mail_flag_requires_source_artifact(tmp_path, monkeypatch, capsys):
    instruction, _source = _inputs(tmp_path)
    calls = []
    fake = SimpleNamespace(run_task=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr("agent_service.mail_understanding", fake, raising=False)

    code = run_jarvis_task.main(["--text-file", str(instruction), "--mail-understanding"])

    assert code == 1
    assert calls == []
    assert json.loads(capsys.readouterr().out)["error"] == "task_failed"


def test_cli_without_mail_flag_preserves_assistant_path(tmp_path, monkeypatch, capsys):
    instruction, source = _inputs(tmp_path)
    calls = []
    monkeypatch.setattr(run_jarvis_task, "run_task", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"ok": True, "state": "succeeded"}))
    fake = SimpleNamespace(run_task=lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("mail path")))
    monkeypatch.setattr("agent_service.mail_understanding", fake, raising=False)
    root = tmp_path / "runs"

    code = run_jarvis_task.main([
        "--text-file", str(instruction), "--root", str(root),
        "--source-artifact-json", json.dumps(source),
    ])

    assert code == 0
    assert calls == [(("Create a short action summary", root),
                      {"task_root": None, "source_artifact": source})]
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"


def test_explicit_review_resume_json_dispatches_only_to_mail(tmp_path, monkeypatch, capsys):
    instruction, source = _inputs(tmp_path)
    reference = {'task_root': str(tmp_path / ('task-' + 'a' * 32)), 'token': 'b' * 64}
    calls = []
    monkeypatch.setattr('agent_service.mail_understanding', SimpleNamespace(
        run_task=lambda *a, **kw: calls.append((a, kw)) or {'ok': True}), raising=False)
    code = run_jarvis_task.main(['--text-file', str(instruction), '--mail-understanding',
        '--source-artifact-json', json.dumps(source), '--mail-review-resume-json', json.dumps(reference)])
    assert code == 0
    assert calls[0][1]['review_resume'] == reference
    assert calls[0][1]['source_artifact'] == source
    capsys.readouterr()


@pytest.mark.parametrize('mail_flag,source_flag', [(False, True), (True, False), (False, False)])
def test_review_resume_never_falls_back_to_normal_assistant(tmp_path, monkeypatch, capsys, mail_flag, source_flag):
    instruction, source = _inputs(tmp_path)
    monkeypatch.setattr(run_jarvis_task, 'run_task', lambda *a, **kw: pytest.fail('normal task'))
    monkeypatch.setattr('agent_service.mail_understanding', SimpleNamespace(
        run_task=lambda *a, **kw: pytest.fail('mail task')), raising=False)
    args = ['--text-file', str(instruction), '--mail-review-resume-json', '{}']
    if mail_flag:
        args += ['--mail-understanding']
    if source_flag:
        args += ['--source-artifact-json', json.dumps(source)]
    assert run_jarvis_task.main(args) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'task_failed'


@pytest.mark.parametrize('reference', ['null', '[]', '{}', 'true'])
def test_review_resume_malformed_json_never_silently_generates(tmp_path, monkeypatch, capsys, reference):
    instruction, source = _inputs(tmp_path)
    monkeypatch.setattr('agent_service.mail_understanding', SimpleNamespace(
        run_task=lambda *a, **kw: pytest.fail('must reject malformed resume')), raising=False)
    assert run_jarvis_task.main(['--text-file', str(instruction), '--mail-understanding',
        '--source-artifact-json', json.dumps(source), '--mail-review-resume-json', reference]) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'task_failed'
