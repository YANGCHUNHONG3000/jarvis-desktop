"""Offline recovery contracts for Office workflows; no native app is launched."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service.office_workflow import WorkflowError, run_office_workflow


def _workflow_root(tmp_path):
    return tmp_path / ("workflow-" + uuid.uuid4().hex)


def _steps():
    return [
        {"id": "s1", "action": "spreadsheet", "data": {
            "kind": "spreadsheet", "title": "任务统计", "headers": ["任务", "数量"],
            "rows": [["已完成", 3], ["待审核", 2]], "sum_columns": [1]}},
        {"id": "s2", "action": "document_from_sheet", "from": "s1",
         "title": "任务汇总", "paragraphs": ["数据取自已核验的工作表。"]},
    ]


def _verified_executor(calls):
    def execute(spec, step_root, should_stop):
        calls.append(spec["kind"])
        suffix = ".xlsx" if spec["kind"] == "spreadsheet" else ".docx"
        artifact = step_root / ("verified" + suffix)
        content = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        artifact.write_bytes(content)
        if spec["kind"] == "spreadsheet":
            observed = {"headers": spec["headers"], "rows": spec["rows"], "totals": {"1": 5}}
        else:
            observed = {"text": spec["title"] + "\n" + "\n".join(spec["paragraphs"])}
            if "table" in spec:
                observed["table"] = {"headers": spec["table"]["headers"],
                                     "rows": [[str(cell) for cell in row]
                                              for row in spec["table"]["rows"]]}
        return {"ok": True, "state": "succeeded", "artifact": str(artifact),
                "sha256": sha256(content).hexdigest(), "observed": observed,
                "app": {"progid": "fake-WPS", "name": "Offline fake", "version": "1"},
                "application_verified": True, "file_verified": True}
    return execute


def test_cancel_after_verified_s1_resume_preserves_hash_and_executes_only_s2(tmp_path):
    workflow_root = _workflow_root(tmp_path)
    steps = _steps()
    first_calls = []
    execute_first = _verified_executor(first_calls)

    def cancel_after_s1(spec, step_root, should_stop):
        result = execute_first(spec, step_root, should_stop)
        (workflow_root / "cancel.flag").touch()
        return result

    cancelled = run_office_workflow(steps, workflow_root, execute=cancel_after_s1)
    assert cancelled["state"] == "cancelled"
    assert cancelled["completed"] == ["s1"]
    s1_hash = cancelled["receipts"]["s1"]["sha256"]
    assert first_calls == ["spreadsheet"]

    (workflow_root / "cancel.flag").unlink()
    resumed_calls = []
    resumed = run_office_workflow(steps, workflow_root,
                                  execute=_verified_executor(resumed_calls), resume=True)

    assert resumed["state"] == "succeeded"
    assert resumed["receipts"]["s1"]["sha256"] == s1_hash
    assert resumed["completed"] == ["s1", "s2"]
    assert resumed_calls == ["document"]


def test_changed_s1_source_artifact_blocks_resuming_dependent_s2(tmp_path):
    workflow_root = _workflow_root(tmp_path)
    steps = _steps()
    calls = []
    execute = _verified_executor(calls)

    def cancel_after_s1(spec, step_root, should_stop):
        result = execute(spec, step_root, should_stop)
        (workflow_root / "cancel.flag").touch()
        return result

    cancelled = run_office_workflow(steps, workflow_root, execute=cancel_after_s1)
    source_path = Path(cancelled["receipts"]["s1"]["artifact"])
    source_path.write_bytes(b"changed after verified receipt")
    (workflow_root / "cancel.flag").unlink()
    calls_before_resume = list(calls)

    with pytest.raises(WorkflowError, match="artifact_hash_mismatch"):
        run_office_workflow(steps, workflow_root,
                            execute=lambda *_: pytest.fail("dependent document must not run"),
                            resume=True)

    assert calls == calls_before_resume == ["spreadsheet"]
    assert not (workflow_root / "s2" / "intent.json").exists()
