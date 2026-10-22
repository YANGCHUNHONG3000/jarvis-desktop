"""Offline CLI argument routing for a verified gap retry."""

import json

import pytest

import run_jarvis_task


def test_retry_ref_is_exclusive_and_passed_unmodified(tmp_path, monkeypatch, capsys):
    text_file = tmp_path / "request.txt"
    text_file.write_text("继续原任务", encoding="utf-8")
    reference = {"task_root": str(tmp_path / ("task-" + "a" * 32)), "token": "b" * 64}
    received = []

    def fake_run(instruction, root, **options):
        received.append((instruction, root, options))
        return {"ok": True, "state": "succeeded"}

    monkeypatch.setattr(run_jarvis_task, "run_task", fake_run)
    code = run_jarvis_task.main(["--text-file", str(text_file), "--root", str(tmp_path),
                                 "--source-retry-json", json.dumps(reference)])
    assert code == 0
    assert received[0][0] == "继续原任务"
    assert received[0][2]["source_retry"] == reference
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"
    with pytest.raises(SystemExit):
        run_jarvis_task.main(["--text-file", str(text_file), "--source-file", str(text_file),
                              "--source-retry-json", json.dumps(reference)])
    assert len(received) == 1


def test_oversize_retry_json_rejected_without_executor(tmp_path, monkeypatch, capsys):
    text_file = tmp_path / "request.txt"
    text_file.write_text("任务", encoding="utf-8")
    monkeypatch.setattr(run_jarvis_task, "run_task",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("executor called")))
    code = run_jarvis_task.main(["--text-file", str(text_file),
                                 "--source-retry-json", "x" * 2001])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed"
