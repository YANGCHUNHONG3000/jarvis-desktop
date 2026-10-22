"""CLI contract tests with a fake Office executor; no native app is opened."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service.office_workflow import run_office_workflow
from run_jarvis_office import main


def _steps():
    return [{"id": "s1", "action": "document", "data": {
        "kind": "document", "title": "办公记录", "paragraphs": ["第一段"]}}]


def _executor(calls):
    def execute(spec, step_root, should_stop):
        assert not should_stop()
        calls.append(spec)
        path = step_root / "document.docx"
        data = json.dumps(spec, ensure_ascii=False).encode("utf-8")
        path.write_bytes(data)
        return {"ok": True, "state": "succeeded", "artifact": str(path),
                "sha256": sha256(data).hexdigest(), "observed": {"text": "办公记录\n第一段"},
                "app": {"progid": "fake", "name": "Fake WPS", "version": "1"},
                "application_verified": True, "file_verified": True}
    return execute


def _output(capsys):
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_fresh_request_and_resume_emit_one_json_without_replay(tmp_path, capsys):
    calls = []
    payload = json.dumps({"steps": _steps()}, ensure_ascii=False)
    assert main(["--request", payload], execute=_executor(calls), repo_root=tmp_path) == 0
    first = _output(capsys)
    assert first["ok"] is True and first["state"] == "succeeded"
    workspace = Path(first["root"])
    assert workspace.parent == tmp_path / "data" / "office_runs"
    assert len(calls) == 1
    assert main(["--resume", str(workspace)], execute=lambda *_: pytest.fail("replayed"), repo_root=tmp_path) == 0
    second = _output(capsys)
    assert second["receipts"]["s1"]["sha256"] == first["receipts"]["s1"]["sha256"]


def test_resume_existing_assistant_task_workflow(tmp_path, capsys):
    task = tmp_path / "data" / "assistant_tasks" / ("task-" + uuid.uuid4().hex)
    task.mkdir(parents=True)
    workspace = task / ("workflow-" + uuid.uuid4().hex)
    run_office_workflow(_steps(), workspace, execute=_executor([]))
    assert main(["--resume", str(workspace)], execute=lambda *_: pytest.fail("replayed"), repo_root=tmp_path) == 0
    assert _output(capsys)["state"] == "succeeded"


def test_unknown_intent_is_not_replayed(tmp_path, capsys):
    workspace = tmp_path / "data" / "office_runs" / ("workflow-" + uuid.uuid4().hex)
    workspace.parent.mkdir(parents=True)

    def interrupted(spec, step_root, should_stop):
        (step_root / "document.docx").write_bytes(b"effect uncertain")
        raise TimeoutError("no receipt")

    first = run_office_workflow(_steps(), workspace, execute=interrupted)
    assert first["state"] == "unknown"
    assert main(["--resume", str(workspace)], execute=lambda *_: pytest.fail("replayed"), repo_root=tmp_path) == 1
    result = _output(capsys)
    assert result["state"] == "unknown" and result["step"] == "s1"


@pytest.mark.parametrize("payload", ["", "[]", "{}", '{"steps":[],"command":"run"}', "{bad json}"])
def test_bad_request_has_no_workflow_side_effect(tmp_path, capsys, payload):
    assert main(["--request", payload], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 1
    assert _output(capsys)["ok"] is False
    assert not (tmp_path / "data" / "office_runs").exists()


def test_resume_rejects_outside_and_bad_name(tmp_path, capsys):
    outside = tmp_path / "elsewhere" / ("workflow-" + uuid.uuid4().hex)
    outside.mkdir(parents=True)
    for candidate in (outside, tmp_path / "data" / "office_runs" / "workflow-bad"):
        assert main(["--resume", str(candidate)], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 1
        assert _output(capsys)["error"] == "resume_path_invalid"


def test_resume_rejects_linked_workflow(tmp_path, capsys):
    real = tmp_path / "data" / "office_runs" / ("workflow-" + uuid.uuid4().hex)
    real.mkdir(parents=True)
    link = tmp_path / "data" / "office_runs" / ("workflow-" + uuid.uuid4().hex)
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable on this host")
    assert main(["--resume", str(link)], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 1
    assert _output(capsys)["error"] == "resume_path_invalid"


def test_help_or_missing_action_still_emits_single_json(tmp_path, capsys):
    for argv in ([], ["--help"]):
        assert main(argv, repo_root=tmp_path) == 1
        assert _output(capsys)["error"] == "arguments_invalid"


def test_list_empty_is_read_only(tmp_path, capsys):
    assert main(["--list"], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 0
    assert _output(capsys) == {"ok": True, "state": "listed", "workflows": []}
    assert not (tmp_path / "data").exists()


def test_list_and_show_do_not_change_workflow(tmp_path, capsys):
    workspace = tmp_path / "data" / "office_runs" / ("workflow-" + uuid.uuid4().hex)
    workspace.parent.mkdir(parents=True)
    run_office_workflow(_steps(), workspace, execute=_executor([]))
    before = {str(path.relative_to(workspace)): (path.stat().st_size, path.stat().st_mtime_ns)
              for path in workspace.rglob("*") if path.is_file()}
    assert main(["--list"], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 0
    listing = _output(capsys)
    assert listing["workflows"] == [{"root": str(workspace), "state": "succeeded"}]
    assert main(["--show", str(workspace)], execute=lambda *_: pytest.fail("executed"), repo_root=tmp_path) == 0
    shown = _output(capsys)
    assert shown["state"] == "shown" and shown["latest_result"]["state"] == "succeeded"
    after = {str(path.relative_to(workspace)): (path.stat().st_size, path.stat().st_mtime_ns)
             for path in workspace.rglob("*") if path.is_file()}
    assert after == before
