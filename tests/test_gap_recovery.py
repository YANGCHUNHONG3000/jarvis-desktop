"""Offline evidence and retry tests; no network, model, or Office process."""

from hashlib import sha256
import json
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET
import zipfile

import pytest

from agent_service import assistant, gap_recovery
from agent_service import office, office_workflow
from agent_service.artifact_source import prepare_artifact_source


class UnsupportedClient:
    ledger = None

    def __init__(self):
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return {"kind": "unsupported", "reason": "尚无适配器"}


def _old(tmp_path, *, source=False):
    base = tmp_path / "assistant_tasks"
    selected = None
    if source:
        selected = tmp_path / "input.csv"
        selected.write_text("名称,数量\n甲,2\n乙,3\n", encoding="utf-8")
    client = UnsupportedClient()
    instruction = "按原请求处理资料" if source else "处理这项尚未支持的请求"
    result = assistant.run_task(instruction, base, client=client, source_file=selected)
    assert result["state"] == "unsupported" and len(client.calls) == 1
    return base, Path(result["root"]), instruction


def _new_task(base):
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    return task


def _rewrite_terminal(task, mutator):
    result_path = task / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    gap_path = task / "capability-gap.json"
    gap = json.loads(gap_path.read_text(encoding="utf-8"))
    mutator(result, gap)
    gap_bytes = json.dumps(gap, ensure_ascii=False).encode("utf-8")
    gap_path.write_bytes(gap_bytes)
    result["capability_gap"]["sha256"] = sha256(gap_bytes).hexdigest()
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    events = (task / "events.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(events[-1])
    last["result"] = result
    events[-1] = json.dumps(last, ensure_ascii=False)
    (task / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


def test_no_source_preview_and_retry_keeps_original_immutable(tmp_path):
    base, old, instruction = _old(tmp_path)
    originals = {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()}
    preview = gap_recovery.preview_retry(old, base)
    assert preview["eligible"] is True and preview["instruction"] == instruction
    assert preview["source_kind"] is None and len(preview["token"]) == 64
    assert gap_recovery.prepare_retry(old, base, preview["token"])["eligible"] is True
    task = _new_task(base)
    client = UnsupportedClient()
    result = assistant.run_task(instruction, base, task_root=task, client=client,
                                source_retry={"task_root": str(old), "token": preview["token"]})
    assert result["state"] == "unsupported" and len(client.calls) == 1
    assert (task / "retry-origin.json").is_file()
    assert (task / "request.txt").read_text(encoding="utf-8") == instruction
    assert {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()} == originals


def test_source_snapshot_is_copied_and_revalidated_before_client(tmp_path):
    base, old, instruction = _old(tmp_path, source=True)
    preview = gap_recovery.preview_retry(old, base)
    assert preview["eligible"] is True and preview["source_kind"] == "table"
    task = _new_task(base)
    copied = gap_recovery.snapshot_retry_source(
        {"task_root": str(old), "token": preview["token"]}, task, base, instruction)
    assert copied["observed"]["headers"] == ["名称", "数量"]
    assert copied["observed"]["rows"] == [["甲", 2], ["乙", 3]]
    assert (task / "sources" / "source.csv").read_bytes() == (old / "sources" / "source.csv").read_bytes()
    assert json.loads((task / "retry-origin.json").read_text(encoding="utf-8"))["token"] == preview["token"]
    assert gap_recovery.preview_retry(old, base)["token"] == preview["token"]


@pytest.mark.parametrize("target", ["result.json", "capability-gap.json", "request.txt",
                                     "events.jsonl", "sources/source.csv", "sources/source.json"])
def test_tampered_evidence_rejected_before_client(tmp_path, target):
    base, old, instruction = _old(tmp_path, source=True)
    original = gap_recovery.preview_retry(old, base)
    assert original["eligible"]
    path = old / target
    path.write_bytes(path.read_bytes() + b"x")
    assert gap_recovery.preview_retry(old, base)["eligible"] is False
    client = UnsupportedClient()
    result = assistant.run_task(instruction, base, task_root=_new_task(base), client=client,
                                source_retry={"task_root": str(old), "token": original["token"]})
    assert result["state"] == "failed" and client.calls == []


def test_unknown_effect_file_and_event_rejected(tmp_path):
    base, old, _ = _old(tmp_path)
    (old / ("workflow-" + "0" * 32)).mkdir()
    assert gap_recovery.preview_retry(old, base)["reason"] == "gap_effect_unknown"
    (old / ("workflow-" + "0" * 32)).rmdir()
    events = old / "events.jsonl"
    lines = events.read_text(encoding="utf-8").splitlines()
    lines.insert(-1, json.dumps({"at": "now", "event": "office_requested"}))
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert gap_recovery.preview_retry(old, base)["reason"] == "gap_effect_unknown"


def test_legacy_exact_request_requires_unique_match(tmp_path):
    base, old, instruction = _old(tmp_path)
    (old / "request.txt").unlink()
    legacy = base.parent / "user_requests"
    legacy.mkdir()
    (legacy / (uuid.uuid4().hex + ".txt")).write_text(instruction, encoding="utf-8")
    preview = gap_recovery.preview_retry(old, base)
    assert preview["eligible"] and preview["instruction"] == instruction
    (legacy / (uuid.uuid4().hex + ".txt")).write_text(instruction, encoding="utf-8")
    assert gap_recovery.preview_retry(old, base)["reason"] == "gap_instruction_ambiguous"


def test_older_gap_without_discovery_still_revisitable(tmp_path):
    base, old, _ = _old(tmp_path)
    _rewrite_terminal(old, lambda result, gap: (gap.pop("discovery", None),
                                                gap.pop("development_request", None)))
    assert gap_recovery.preview_retry(old, base)["eligible"] is True


def test_windows_multiline_request_matches_cli_text_normalization(tmp_path):
    base = tmp_path / "assistant_tasks"
    instruction = "第一行\n第二行"
    base.mkdir()
    task = _new_task(base)
    (task / "request.txt").write_bytes(instruction.replace("\n", "\r\n").encode("utf-8"))
    result = assistant.run_task(instruction, base, task_root=task, client=UnsupportedClient())
    assert result["state"] == "unsupported"
    preview = gap_recovery.preview_retry(task, base)
    assert preview["eligible"] and preview["instruction"] == instruction
    second = _new_task(base)
    result = assistant.run_task(instruction, base, task_root=second, client=UnsupportedClient(),
                                source_retry={"task_root": str(task), "token": preview["token"]})
    assert result["state"] == "unsupported"
    assert gap_recovery.preview_retry(second, base)["eligible"]


def test_wrong_instruction_or_stale_token_has_no_model_call(tmp_path):
    base, old, instruction = _old(tmp_path)
    preview = gap_recovery.preview_retry(old, base)
    with pytest.raises(ValueError):
        gap_recovery.prepare_retry(old, base, "0" * 64)
    task = _new_task(base)
    client = UnsupportedClient()
    result = assistant.run_task("不同的请求", base, task_root=task, client=client,
                                source_retry={"task_root": str(old), "token": preview["token"]})
    assert result["state"] == "failed" and client.calls == []
    assert not (task / "retry-origin.json").exists()
    assert instruction != "不同的请求"


def test_source_provenance_cannot_be_downgraded(tmp_path):
    base, old, _ = _old(tmp_path, source=True)
    _rewrite_terminal(old, lambda result, gap: result.update(source_provenance={"kind": "forged"}))
    assert gap_recovery.preview_retry(old, base)["reason"] == "gap_provenance_missing"


def test_linked_old_task_rejected_without_reading_outside(tmp_path):
    base, old, _ = _old(tmp_path)
    link = base / ("task-" + uuid.uuid4().hex)
    try:
        link.symlink_to(old, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert gap_recovery.preview_retry(link, base)["eligible"] is False


def test_second_unsupported_retry_can_be_revisited_without_recursive_history(tmp_path):
    base, first, instruction = _old(tmp_path, source=True)
    first_preview = gap_recovery.preview_retry(first, base)
    second = _new_task(base)
    second_result = assistant.run_task(
        instruction, base, task_root=second, client=UnsupportedClient(),
        source_retry={"task_root": str(first), "token": first_preview["token"]})
    assert second_result["state"] == "unsupported"
    second_preview = gap_recovery.preview_retry(second, base)
    assert second_preview["eligible"] is True
    assert second_preview["source_kind"] == "table"
    third = _new_task(base)
    snapshot = gap_recovery.snapshot_retry_source(
        {"task_root": str(second), "token": second_preview["token"]}, third, base, instruction)
    assert snapshot["observed"]["rows"] == [["甲", 2], ["乙", 3]]
    assert (third / "retry-origin.json").is_file()


def test_retry_origin_parent_change_blocks_future_handoff(tmp_path):
    base, first, instruction = _old(tmp_path)
    preview = gap_recovery.preview_retry(first, base)
    second = _new_task(base)
    result = assistant.run_task(instruction, base, task_root=second,
                                client=UnsupportedClient(),
                                source_retry={"task_root": str(first), "token": preview["token"]})
    assert result["state"] == "unsupported"
    assert gap_recovery.preview_retry(second, base)["eligible"]
    first_result = first / "result.json"
    first_result.write_bytes(first_result.read_bytes() + b" ")
    assert gap_recovery.preview_retry(second, base)["reason"] == "gap_origin_changed"


@pytest.mark.parametrize("name", ["request.txt", "events.jsonl", "sources/source.csv",
                                  "sources/source.json"])
def test_retry_chain_rechecks_entire_ancestor(tmp_path, name):
    base, first, instruction = _old(tmp_path, source=True)
    preview = gap_recovery.preview_retry(first, base)
    second = _new_task(base)
    result = assistant.run_task(instruction, base, task_root=second, client=UnsupportedClient(),
                                source_retry={"task_root": str(first), "token": preview["token"]})
    assert result["state"] == "unsupported"
    assert gap_recovery.preview_retry(second, base)["eligible"]
    path = first / name
    path.write_bytes(path.read_bytes() + b"x")
    assert gap_recovery.preview_retry(second, base)["eligible"] is False


def test_retry_origin_cycle_and_depth_are_bounded(tmp_path):
    base, first, instruction = _old(tmp_path)
    current = first
    chain = [first]
    for _ in range(9):
        preview = gap_recovery.preview_retry(current, base)
        assert preview["eligible"]
        child = _new_task(base)
        result = assistant.run_task(instruction, base, task_root=child,
                                    client=UnsupportedClient(),
                                    source_retry={"task_root": str(current), "token": preview["token"]})
        assert result["state"] == "unsupported"
        chain.append(child)
        current = child
    assert gap_recovery.preview_retry(current, base)["reason"] == "gap_origin_depth_invalid"
    # A forged origin on the first task must never recurse around this cycle.
    second = chain[1]
    origin = {"schema": 1, "task_root": str(second), "token": "a" * 64,
              "instruction_sha256": sha256(instruction.encode("utf-8")).hexdigest(),
              "result_sha256": sha256((second / "result.json").read_bytes()).hexdigest(),
              "gap_sha256": sha256((second / "capability-gap.json").read_bytes()).hexdigest(),
              "source_files": {}}
    (first / "retry-origin.json").write_text(json.dumps(origin), encoding="utf-8")
    assert gap_recovery.preview_retry(second, base)["reason"] == "gap_origin_cycle"


def test_generated_document_provenance_survives_retry_copy(tmp_path):
    base = tmp_path / "assistant_tasks"
    base.mkdir()
    producer = _new_task(base)
    workflow = producer / ("workflow-" + uuid.uuid4().hex)
    spec = {"kind": "document", "title": "原文标题", "paragraphs": ["正文"],
            "table": {"headers": ["项目", "内容"], "rows": [["甲", "001"]]}}
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def fake_office(data, step_root, stop):
        assert not stop()
        root = ET.Element(f"{{{namespace}}}document")
        body = ET.SubElement(root, f"{{{namespace}}}body")
        for value in [data["title"], *data["paragraphs"]]:
            paragraph = ET.SubElement(body, f"{{{namespace}}}p")
            ET.SubElement(ET.SubElement(paragraph, f"{{{namespace}}}r"),
                          f"{{{namespace}}}t").text = value
        table = ET.SubElement(body, f"{{{namespace}}}tbl")
        for values in [data["table"]["headers"], *data["table"]["rows"]]:
            row = ET.SubElement(table, f"{{{namespace}}}tr")
            for value in values:
                cell = ET.SubElement(row, f"{{{namespace}}}tc")
                paragraph = ET.SubElement(cell, f"{{{namespace}}}p")
                ET.SubElement(ET.SubElement(paragraph, f"{{{namespace}}}r"),
                              f"{{{namespace}}}t").text = value
        path = step_root / "document.docx"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("word/document.xml", ET.tostring(root, encoding="utf-8"))
        internal = office.verify_office_file(path, data)
        return {"ok": True, "state": "succeeded", "artifact": str(path),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "observed": office._public_observation(data, internal),
                "app": {"progid": "fake-WPS", "name": "Fake", "version": "1"},
                "application_verified": True, "file_verified": True}

    run = office_workflow.run_office_workflow(
        [{"id": "s1", "action": "document", "data": spec}], workflow, execute=fake_office)
    assert run["state"] == "succeeded"
    receipt = run["receipts"]["s1"]
    parent = {"ok": True, "state": "succeeded", "kind": "office", "root": str(producer),
              "workflow_root": str(workflow), "completed": ["s1"],
              "artifacts": [{"step": "s1", "path": receipt["artifact"], "sha256": receipt["sha256"]}]}
    (producer / "result.json").write_text(json.dumps(parent), encoding="utf-8")
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    instruction = "把原文继续整理"
    old = _new_task(base)
    old_result = assistant.run_task(instruction, base, task_root=old, client=UnsupportedClient(),
                                    source_artifact=ref)
    assert old_result["state"] == "unsupported"
    preview = gap_recovery.preview_retry(old, base)
    assert preview["eligible"] and preview["source_kind"] == "document"
    target = _new_task(base)
    snapshot = gap_recovery.snapshot_retry_source(
        {"task_root": str(old), "token": preview["token"]}, target, base, instruction)
    assert snapshot["observed"]["table"]["rows"] == [["甲", "001"]]
    assert json.loads((target / "sources" / "provenance.json").read_text(encoding="utf-8")) \
        == old_result["source_provenance"]
    assert (target / "sources" / "source.docx").read_bytes() \
        == (old / "sources" / "source.docx").read_bytes()
    (old / "sources" / "provenance.json").write_text("{}", encoding="utf-8")
    assert gap_recovery.preview_retry(old, base)["eligible"] is False
