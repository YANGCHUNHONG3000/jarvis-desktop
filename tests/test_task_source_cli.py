"""Offline CLI argument forwarding checks; the source adapter is not invoked."""

from pathlib import Path

import run_jarvis_task


def _inputs(tmp_path):
    request = tmp_path / "request.txt"
    request.write_text("Summarize this file", encoding="utf-8")
    return request


def test_cli_passes_exact_optional_source_file_to_assistant(tmp_path, monkeypatch, capsys):
    request = _inputs(tmp_path)
    source = tmp_path / "Selected source.xlsx"
    calls = []

    def fake_run_task(instruction, root, **kwargs):
        calls.append((instruction, root, kwargs))
        return {"ok": True}

    monkeypatch.setattr(run_jarvis_task, "run_task", fake_run_task)
    code = run_jarvis_task.main([
        "--text-file", str(request), "--root", str(tmp_path / "tasks"),
        "--source-file", str(source),
    ])

    assert code == 0
    assert calls == [("Summarize this file", Path(tmp_path / "tasks"),
                      {"task_root": None, "source_file": source})]
    assert '"ok":true' in capsys.readouterr().out


def test_cli_keeps_existing_call_shape_without_source_file(tmp_path, monkeypatch, capsys):
    request = _inputs(tmp_path)
    calls = []

    def fake_run_task(instruction, root, **kwargs):
        calls.append((instruction, root, kwargs))
        return {"ok": True}

    monkeypatch.setattr(run_jarvis_task, "run_task", fake_run_task)
    code = run_jarvis_task.main([
        "--text-file", str(request), "--root", str(tmp_path / "tasks"),
    ])

    assert code == 0
    assert calls == [("Summarize this file", Path(tmp_path / "tasks"), {"task_root": None})]
    assert '"ok":true' in capsys.readouterr().out
