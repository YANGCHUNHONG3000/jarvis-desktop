"""Offline lifecycle checks; these fakes are not WPS acceptance evidence."""

from hashlib import sha256
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from agent_service.office_workflow import WorkflowError, run_office_workflow


def root(tmp_path):
    return tmp_path / ("workflow-" + uuid.uuid4().hex)


def sheet_step():
    return {"id": "s1", "action": "spreadsheet", "data": {
        "kind": "spreadsheet", "title": "成本", "headers": ["项目", "金额"],
        "rows": [["纸", 3], ["笔", 4]], "sum_columns": [1]}}


def report_step():
    return {"id": "s2", "action": "document_from_sheet", "from": "s1",
            "title": "汇总", "paragraphs": ["以下数字来自已核验的表格。"]}


def fake_execute(calls):
    def execute(spec, step_root, should_stop):
        assert not should_stop()
        calls.append(spec)
        suffix = ".xlsx" if spec["kind"] == "spreadsheet" else ".docx"
        artifact = step_root / ("created" + suffix)
        data = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        artifact.write_bytes(data)
        if spec["kind"] == "spreadsheet":
            observed = {"headers": spec["headers"], "rows": spec["rows"], "totals": {"1": 7}}
        else:
            observed = {"text": "\n".join([spec["title"], *spec["paragraphs"]])}
            if "table" in spec:
                observed["table"] = {"headers": spec["table"]["headers"],
                                     "rows": [[str(cell) for cell in row]
                                              for row in spec["table"]["rows"]]}
        return {"ok": True, "state": "succeeded", "artifact": str(artifact),
                "sha256": sha256(data).hexdigest(), "observed": observed,
                "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
                "application_verified": True, "file_verified": True}
    return execute


def test_cross_application_receipts_and_resume_skip_all(tmp_path):
    workspace = root(tmp_path)
    calls = []
    steps = [sheet_step(), report_step()]
    first = run_office_workflow(steps, workspace, execute=fake_execute(calls))
    assert first["state"] == "succeeded"
    assert len(calls) == 2
    assert calls[1]["table"] == {"headers": ["项目", "金额"],
                                  "rows": [["纸", 3], ["笔", 4], ["合计", 7]]}
    second = run_office_workflow(steps, workspace, execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert second["state"] == "succeeded"
    assert second["receipts"]["s1"]["sha256"] == first["receipts"]["s1"]["sha256"]


def test_started_intent_without_receipt_stays_unknown(tmp_path):
    workspace = root(tmp_path)
    calls = []

    def uncertain(spec, step_root, stop):
        calls.append(spec)
        (step_root / "created.xlsx").write_bytes(b"possibly saved")
        raise TimeoutError("COM did not answer")

    first = run_office_workflow([sheet_step()], workspace, execute=uncertain)
    assert first["state"] == "unknown" and first["step"] == "s1"
    assert (workspace / "s1" / "intent.json").exists()
    assert not (workspace / "s1" / "receipt.json").exists()
    second = run_office_workflow([sheet_step()], workspace, execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert second["state"] == "unknown" and len(calls) == 1


def test_cancel_between_steps_preserves_first_receipt(tmp_path):
    workspace = root(tmp_path)
    calls = []
    executor = fake_execute(calls)

    def cancel_after_first(spec, step_root, should_stop):
        value = executor(spec, step_root, should_stop)
        (workspace / "cancel.flag").touch()
        return value

    result = run_office_workflow([sheet_step(), report_step()], workspace, execute=cancel_after_first)
    assert result["state"] == "cancelled" and result["completed"] == ["s1"]
    assert result["receipts"]["s1"]["artifact"].endswith(".xlsx")
    assert len(calls) == 1 and not (workspace / "s2").exists()
    assert run_office_workflow([sheet_step(), report_step()], workspace,
                               execute=lambda *_: pytest.fail("replayed"), resume=True)["state"] == "cancelled"


def test_resume_runs_only_remaining_new_step(tmp_path, monkeypatch):
    import agent_service.office_workflow as workflow
    workspace = root(tmp_path)
    calls = []
    executor = fake_execute(calls)
    clock = [0.0]
    monkeypatch.setattr(workflow, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def complete_then_deadline(spec, step_root, should_stop):
        value = executor(spec, step_root, should_stop)
        clock[0] = 1.0
        return value

    first = run_office_workflow([sheet_step(), report_step()], workspace,
                                execute=complete_then_deadline, deadline=.5)
    assert first["state"] == "timed_out" and first["completed"] == ["s1"]
    assert first["receipts"]["s1"]["file_verified"] is True
    second = run_office_workflow([sheet_step(), report_step()], workspace,
                                 execute=executor, resume=True, deadline=2.0)
    assert second["state"] == "succeeded" and len(calls) == 2
    assert calls[0]["kind"] == "spreadsheet" and calls[1]["kind"] == "document"


def test_parent_cancel_callback_preserves_receipt_and_can_resume(tmp_path):
    workspace = root(tmp_path)
    cancel = {"requested": False}
    calls = []
    executor = fake_execute(calls)

    def execute(spec, step_root, should_stop):
        value = executor(spec, step_root, should_stop)
        cancel["requested"] = True
        return value

    first = run_office_workflow([sheet_step(), report_step()], workspace,
                                execute=execute, cancel_requested=lambda: cancel["requested"])
    assert first["state"] == "cancelled"
    assert list(first["receipts"]) == ["s1"]
    assert len(calls) == 1
    second = run_office_workflow([sheet_step(), report_step()], workspace,
                                 execute=executor, resume=True)
    assert second["state"] == "succeeded" and len(calls) == 2


def test_unknown_second_step_retains_prior_receipt(tmp_path):
    workspace = root(tmp_path)
    calls = []
    executor = fake_execute(calls)

    def execute(spec, step_root, should_stop):
        if spec["kind"] == "document":
            raise TimeoutError("native RPC uncertain")
        return executor(spec, step_root, should_stop)

    result = run_office_workflow([sheet_step(), report_step()], workspace, execute=execute)
    assert result["state"] == "unknown" and result["step"] == "s2"
    assert result["completed"] == ["s1"] and result["receipts"]["s1"]["sha256"]
    repeated = run_office_workflow([sheet_step(), report_step()], workspace,
                                   execute=lambda *_: pytest.fail("replayed"), resume=True)
    assert repeated["state"] == "unknown" and list(repeated["receipts"]) == ["s1"]


def test_deadline_before_first_effect_has_no_intent(tmp_path):
    workspace = root(tmp_path)
    result = run_office_workflow([sheet_step()], workspace,
                                 execute=lambda *_: pytest.fail("executed"), deadline=time.monotonic() - 1)
    assert result["state"] == "timed_out"
    assert not (workspace / "s1").exists()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True, 10**1000])
def test_deadline_must_be_finite(tmp_path, invalid):
    workspace = root(tmp_path)
    with pytest.raises(WorkflowError, match="deadline_invalid"):
        run_office_workflow([sheet_step()], workspace, execute=fake_execute([]), deadline=invalid)
    assert not workspace.exists()


def test_changed_manifest_and_tampered_artifact_rejected(tmp_path):
    workspace = root(tmp_path)
    steps = [sheet_step()]
    result = run_office_workflow(steps, workspace, execute=fake_execute([]))
    changed = [sheet_step()]
    changed[0]["data"]["title"] = "另一份"
    with pytest.raises(WorkflowError, match="manifest_mismatch"):
        run_office_workflow(changed, workspace, execute=fake_execute([]), resume=True)
    Path(result["receipts"]["s1"]["artifact"]).write_bytes(b"tampered")
    with pytest.raises(WorkflowError, match="artifact_hash_mismatch"):
        run_office_workflow(steps, workspace, execute=fake_execute([]), resume=True)


@pytest.mark.parametrize("steps", [
    [{"id": "s1", "action": "document_from_sheet", "from": "s2", "title": "x", "paragraphs": []}],
    [{"id": "s1", "action": "shell", "data": {"command": "whoami"}}],
    [{"id": "s1", "action": "spreadsheet", "data": {"kind": "spreadsheet", "title": "x",
       "headers": ["n"], "rows": [[1]], "sum_columns": [0], "path": "C:/user.xlsx"}}],
])
def test_invalid_manifest_before_effect(tmp_path, steps):
    workspace = root(tmp_path)
    with pytest.raises((WorkflowError, ValueError)):
        run_office_workflow(steps, workspace, execute=lambda *_: pytest.fail("executed"))
    assert not workspace.exists()


def test_same_workflow_cannot_execute_concurrently(tmp_path):
    workspace = root(tmp_path)
    entered, release = threading.Event(), threading.Event()
    results = []

    def blocking(spec, step_root, stop):
        entered.set()
        assert release.wait(5)
        return fake_execute([])(spec, step_root, stop)

    thread = threading.Thread(target=lambda: results.append(run_office_workflow(
        [sheet_step()], workspace, execute=blocking)))
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(WorkflowError, match="workflow_busy"):
            run_office_workflow([sheet_step()], workspace, execute=fake_execute([]), resume=True)
    finally:
        release.set()
        thread.join(5)
    assert results[0]["state"] == "succeeded"
