import hashlib
import json

from run_jarvis_assistant import _last_json_lines, _saved_path
from run_jarvis_assistant import _durable_task_result, _record_desktop_outcome


def test_saved_path_accepts_matching_file(tmp_path):
    base = tmp_path / "owned"
    base.mkdir()
    target = base / "result.txt"
    target.write_text("verified", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    assert _saved_path(str(target), base, digest) == target.resolve()


def test_saved_path_rejects_hash_mismatch_and_outside_path(tmp_path):
    base = tmp_path / "owned"
    base.mkdir()
    target = base / "result.txt"
    target.write_text("verified", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("verified", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    assert _saved_path(str(target), base, "0" * 64) is None
    assert _saved_path(str(outside), base, hashlib.sha256(outside.read_bytes()).hexdigest()) is None


def test_saved_path_rejects_missing_fields(tmp_path):
    base = tmp_path / "owned"
    base.mkdir()
    target = base / "result.txt"
    target.write_text("verified", encoding="utf-8")

    assert _saved_path(None, base, "0" * 64) is None
    assert _saved_path(str(target), base) is None
    assert _saved_path(str(target), base, None) is None


def test_last_json_lines_returns_last_valid_result_and_skips_malformed():
    output = "\n".join(
        [
            json.dumps({"ok": True, "id": 1}),
            "{not json",
            json.dumps({"progress": "still running"}),
            json.dumps({"ok": False, "id": 2}),
        ]
    )

    assert _last_json_lines(output) == {"ok": False, "id": 2}


def test_last_json_lines_returns_empty_when_no_result_object():
    assert _last_json_lines("bad json\n[]\n{\"progress\": 1}") == {}


def test_durable_result_only_uses_matching_owned_task(tmp_path):
    task = tmp_path / ("task-" + "a" * 32)
    task.mkdir()
    result = {"root": str(task), "ok": True, "state": "succeeded"}
    (task / "result.json").write_text(json.dumps(result), encoding="utf-8")
    assert _durable_task_result(task, tmp_path) == result
    assert _durable_task_result(task, tmp_path / "other") == {}
    result["root"] = str(tmp_path)
    (task / "result.json").write_text(json.dumps(result), encoding="utf-8")
    assert _durable_task_result(task, tmp_path) == {}
    (task / "result.json").write_text("[]", encoding="utf-8")
    assert _durable_task_result(task, tmp_path) == {}


def test_prepared_notepad_records_separate_verified_outcome(tmp_path):
    task = tmp_path / ("task-" + "b" * 32)
    task.mkdir()
    prepared = {"root": str(task), "ok": True, "state": "prepared", "kind": "notepad", "text": "你好"}
    (task / "result.json").write_text(json.dumps(prepared), encoding="utf-8")
    doc = tmp_path / "owned.txt"
    doc.write_text("你好", encoding="utf-8")
    digest = hashlib.sha256(doc.read_bytes()).hexdigest()
    result = {"ok": True, "file_saved": True, "ui_verified": True, "document": str(doc),
              "file_sha256": digest, "expected_sha256": digest, "readback_sha256": digest}
    assert _record_desktop_outcome(task, result, tmp_path, tmp_path)["state"] == "succeeded"
    assert _durable_task_result(task, tmp_path)["state"] == "prepared"
    assert json.loads((task / "desktop-result.json").read_text())["ok"] is True


def test_desktop_outcome_rejects_unrelated_text_and_nonzero_exit(tmp_path):
    from run_jarvis_assistant import _desktop_saved_path
    task = tmp_path / ("task-" + "c" * 32)
    task.mkdir()
    prepared = {"root": str(task), "ok": True, "state": "prepared", "kind": "notepad", "text": "wanted"}
    (task / "result.json").write_text(json.dumps(prepared), encoding="utf-8")
    doc = tmp_path / "unrelated.txt"
    doc.write_text("unrelated", encoding="utf-8")
    digest = hashlib.sha256(doc.read_bytes()).hexdigest()
    result = {"ok": True, "file_saved": True, "ui_verified": True, "document": str(doc),
              "file_sha256": digest, "expected_sha256": digest, "readback_sha256": digest}
    assert _desktop_saved_path(result, 1, tmp_path) is None
    assert _record_desktop_outcome(task, result, tmp_path, tmp_path)["state"] == "unknown"
