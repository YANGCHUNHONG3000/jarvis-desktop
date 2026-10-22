"""The public CLI returns one bounded JSON result without leaking paths."""

import json
import uuid

from run_jarvis_skill_resume import main


def test_cli_rejects_bad_argv_as_single_json(capsys, tmp_path):
    assert main(["--task-root", "relative", "--token", "a" * 64,
                 "--attempt-id", uuid.uuid4().hex], repo_root=tmp_path) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "state": "blocked", "kind": "skill_resume",
                      "error": "task_root_invalid"}
    assert main(["--task-root", str(tmp_path)], repo_root=tmp_path) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "arguments_invalid"


def test_cli_rejects_unowned_task_without_effect(capsys, tmp_path):
    task = tmp_path / "data" / "assistant_tasks" / ("task-" + uuid.uuid4().hex)
    task.parent.mkdir(parents=True)
    task.mkdir()
    assert main(["--task-root", str(task), "--token", "a" * 64,
                 "--attempt-id", uuid.uuid4().hex], repo_root=tmp_path) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["state"] == "blocked"
    assert not any(p.name.startswith("skill-resume-") for p in task.iterdir())
