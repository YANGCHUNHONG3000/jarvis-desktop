"""Unsupported plans retain only bounded local capability lookup metadata."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service import assistant


class Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []
        self.ledger = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def test_source_unsupported_saves_bounded_local_gap_without_effects(tmp_path, monkeypatch):
    base = tmp_path / "tasks"
    selected = tmp_path / "source.csv"
    selected.write_text("项目,数量\n甲,2\n", encoding="utf-8")
    instruction = "读取所选表格并执行当前不支持的动作；私密提示只可存摘要"
    reason = "缺少此种操作能力\n" + "原因" * 240
    assert len(reason) <= 500
    client = Client({"kind": "unsupported", "reason": reason})
    monkeypatch.setattr(assistant, "run_research", lambda *a, **k: (_ for _ in ()).throw(AssertionError("research ran")))
    from agent_service import office_workflow
    monkeypatch.setattr(office_workflow, "run_office_workflow",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Office ran")))
    result = assistant.run_task(instruction, base, source_file=selected, client=client,
                                research=lambda *a, **k: (_ for _ in ()).throw(AssertionError("research ran")))
    assert result["state"] == "unsupported" and result["ok"] is False
    assert result["kind"] == "unsupported" and result["reason"] == reason
    assert len(client.calls) == 1
    task = Path(result["root"])
    pointer = result["capability_gap"]
    assert pointer["filename"] == "capability-gap.json"
    raw = (task / pointer["filename"]).read_bytes()
    assert pointer["sha256"] == sha256(raw).hexdigest()
    saved = json.loads(raw)
    assert set(saved) == {"schema", "instruction_sha256", "model_reason", "lookup", "scope",
                          "discovery", "development_request"}
    assert saved["discovery"]["candidates"] == []
    assert saved["development_request"]["automatic_code_execution"] is False
    assert saved["instruction_sha256"] == sha256(instruction.encode("utf-8")).hexdigest()
    assert instruction not in raw.decode("utf-8")
    assert saved["model_reason"] == reason
    assert saved["lookup"]["query"] == " ".join(reason.split())[:200]
    assert len(saved["lookup"]["query"]) <= 200
    assert saved["lookup"]["match_semantics"] == "static_alias_or_substring_hint_only"
    assert "execution decision" in saved["scope"]
    assert saved["lookup"]["matched_ids"] == []
    assert saved["lookup"]["acquisition_steps"]
    assert json.loads((task / "result.json").read_text(encoding="utf-8")) == result


def test_gap_write_conflict_preserves_unsupported_result(tmp_path):
    base = tmp_path / "tasks"
    base.mkdir()
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    existing = task / "capability-gap.json"
    existing.write_text("preexisting", encoding="utf-8")
    client = Client({"kind": "unsupported", "reason": "缺少能力"})
    result = assistant.run_task("未知能力", base, task_root=task, client=client)
    assert result["state"] == "unsupported" and result["ok"] is False
    assert "capability_gap" not in result
    assert existing.read_text(encoding="utf-8") == "preexisting"
    assert len(client.calls) == 1


def test_catalog_match_remains_only_a_hint(tmp_path):
    result = assistant.run_task("执行当前不支持的处理", tmp_path / "tasks",
                                client=Client({"kind": "unsupported", "reason": "Word"}))
    assert result["state"] == "unsupported" and result["ok"] is False
    path = Path(result["root"]) / result["capability_gap"]["filename"]
    lookup = json.loads(path.read_text(encoding="utf-8"))["lookup"]
    assert lookup["matched_ids"]
    assert lookup["gap"] is False
    assert lookup["match_semantics"] == "static_alias_or_substring_hint_only"


def test_linked_gap_target_cannot_write_outside_task(tmp_path):
    base = tmp_path / "tasks"
    base.mkdir()
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    try:
        (task / "capability-gap.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file links unavailable")
    result = assistant.run_task("未知能力", base, task_root=task,
                                client=Client({"kind": "unsupported", "reason": "缺少能力"}))
    assert result["state"] == "unsupported" and "capability_gap" not in result
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_linked_task_base_rejected_before_any_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory links unavailable")
    client = Client({"kind": "unsupported", "reason": "缺少能力"})
    with pytest.raises(assistant.AssistantError):
        assistant.run_task("未知能力", link / "tasks", client=client)
    assert list(outside.iterdir()) == [] and client.calls == []


def test_unsupported_reason_raw_length_bound_and_newline_normalization(tmp_path):
    base = tmp_path / "tasks"
    reason = "Word\n\t" + "甲" * 495
    assert len(reason) == 501
    # One schema repair is permitted for a model response outside the contract.
    client = Client({"kind": "unsupported", "reason": reason})
    result = assistant.run_task("尝试未知能力", base, client=client)
    assert result["state"] == "failed" and result["error"] == "schema_invalid"
    assert not list(base.glob("task-*/capability-gap.json"))
