"""Strict local Office continuation snapshots and range revalidation."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from agent_service import office_handoff, office_scope, office_workspace, workspace_state
from agent_service.office_edit import snapshot_edit_source
from test_office_edit import sheet_fixture, word_fixture


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    files = [tmp_path / "report.docx", tmp_path / "budget.xlsx"]
    word_fixture(files[0]); sheet_fixture(files[1])
    records = [{"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
               for path in files]
    scope = []
    for index, path in enumerate(files):
        folder = tmp_path / ("fixture" + str(index))
        folder.mkdir()
        public = snapshot_edit_source(path, folder)
        rows = office_scope.entries(public)
        wanted = (next(row for row in rows if row["at"] == {"paragraph_index": 1})
                  if index == 0 else next(row for row in rows if row["at"] ==
                                           {"sheet": "预算", "cell": "A2"}))
        assert wanted["editable"] is True
        scope.append({"path": str(path), "sha256": records[index]["sha256"],
                      "locations": [wanted["at"]]})
    calls = []
    def select(items, *, base):
        calls.append(deepcopy(items))
        if len(items) != 1 or items[0] not in records:
            raise ValueError("changed")
        path = Path(items[0]["path"])
        if sha256(path.read_bytes()).hexdigest() != items[0]["sha256"]:
            raise ValueError("changed")
        return [{"artifact": item["path"], "sha256": item["sha256"]} for item in items]
    monkeypatch.setattr(office_handoff, "select_outputs", select)
    return {"draft": "  修改原文\r\n", "records": records, "scope": scope}, files, calls


def test_save_reload_and_verify_two_formats(prepared, tmp_path):
    payload, files, calls = prepared
    store = tmp_path / "office-store"
    saved = office_workspace.save(store, payload, None)
    assert office_workspace.load(store) == saved
    assert saved["payload"]["draft"] == "  修改原文\r\n"
    same = office_workspace.save(store, payload, saved["id"])
    assert same["id"] == saved["id"]
    result = office_workspace.verify(saved["payload"], tmp_path / "assistant_tasks")
    assert calls == [[payload["records"][0]], [payload["records"][1]]]
    assert len(result["refs"]) == 2 and result["source_ref"] is None
    assert result["payload"] == payload
    evidence = Path(result["evidence_root"])
    assert (evidence / "f1" / "source.docx").is_file()
    assert (evidence / "f2" / "source.xlsx").is_file()
    assert [path.read_bytes() for path in files] == [
        (evidence / "f1" / "source.docx").read_bytes(),
        (evidence / "f2" / "source.xlsx").read_bytes()]


def test_scope_none_verifies_receipts_without_snapshot(prepared, tmp_path):
    payload, _, calls = prepared
    payload["scope"] = None
    result = office_workspace.verify(payload, tmp_path / "assistant_tasks")
    assert len(calls) == 2 and result == {"payload": payload,
                                        "refs": result["refs"], "source_ref": None}
    assert not (tmp_path / "office_workspace_checks").exists()


def test_location_order_does_not_change_selected_range(prepared, tmp_path):
    payload, _, calls = prepared
    payload["scope"][0]["locations"] = [
        {"paragraph_index": 2}, {"paragraph_index": 1}]
    result = office_workspace.verify(payload, tmp_path / "assistant_tasks")
    assert result["payload"]["scope"][0]["locations"] == payload["scope"][0]["locations"]
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["location", "hash", "readonly", "missing_file"])
def test_changed_or_unsupported_scope_rejected(prepared, tmp_path, damage, monkeypatch):
    payload, files, _ = prepared
    if damage == "location":
        payload["scope"][0]["locations"] = [{"paragraph_index": 999}]
    elif damage == "hash":
        files[0].write_bytes(b"changed")
    elif damage == "readonly":
        payload["records"] = payload["records"][1:]
        payload["scope"] = payload["scope"][1:]
        payload["scope"][0]["locations"] = [{"sheet": "预算", "cell": "B4"}]
        def select(items, *, base):
            return [{"artifact": item["path"], "sha256": item["sha256"]} for item in items]
        monkeypatch.setattr(office_handoff, "select_outputs", select)
    else:
        files[0].unlink()
    expected = {"location": "range_invalid", "hash": "changed",
                "readonly": "readonly", "missing_file": "missing"}[damage]
    with pytest.raises(office_workspace.WorkspaceVerificationError,
                       match="^workspace_" + expected + "$") as captured:
        office_workspace.verify(payload, tmp_path / "assistant_tasks")
    assert captured.value.reason == expected
    assert captured.value.file_index == (None if damage == "readonly" else 0)


@pytest.mark.parametrize("damage", ["extra", "record_count", "scope_count", "scope_order",
                                     "duplicate", "location_shape", "location_count", "oversize"])
def test_payload_shape_and_bounds_rejected(prepared, tmp_path, damage):
    payload, _, _ = prepared
    if damage == "extra": payload["approval"] = True
    elif damage == "record_count": payload["records"] = []
    elif damage == "scope_count": payload["scope"].pop()
    elif damage == "scope_order": payload["scope"].reverse()
    elif damage == "duplicate": payload["scope"][0]["locations"] *= 2
    elif damage == "location_shape": payload["scope"][0]["locations"] = [{"paragraph_index": True}]
    elif damage == "location_count":
        payload["scope"][0]["locations"] = [{"paragraph_index": i} for i in range(2001)]
    else:
        payload["draft"] = "🧪" * 12_000
        payload["scope"][0]["locations"] = [{"paragraph_index": i} for i in range(1000)]
    with pytest.raises(office_workspace.WorkspaceError, match="^workspace_(payload_invalid|oversize)$"):
        office_workspace.save(tmp_path / "office-store", payload, None)


def test_separate_store_schemas_reject_each_other(prepared, tmp_path):
    payload, _, _ = prepared
    office_store = tmp_path / "office-store"
    office_workspace.save(office_store, payload, None)
    with pytest.raises(workspace_state.WorkspaceError, match="^workspace_record_invalid$"):
        workspace_state.load(office_store)
    main_store = tmp_path / "main-store"
    workspace_state.save(main_store, {"draft": "", "records": [], "source": None}, None)
    with pytest.raises(office_workspace.WorkspaceError, match="^workspace_record_invalid$"):
        office_workspace.load(main_store)


def test_cas_does_not_overwrite_other_window(prepared, tmp_path):
    payload, _, _ = prepared
    store = tmp_path / "office-store"
    first = office_workspace.save(store, payload, None)
    newer = deepcopy(payload); newer["draft"] = "newer"
    office_workspace.save(store, newer, first["id"])
    with pytest.raises(office_workspace.WorkspaceError, match="^workspace_conflict$"):
        office_workspace.save(store, payload, first["id"])


def test_second_file_proof_failure_has_safe_indexed_reason(prepared, tmp_path, monkeypatch):
    payload, _, calls = prepared
    def select(items, *, base):
        calls.append(deepcopy(items))
        if items[0]["path"] == payload["records"][1]["path"]:
            raise ValueError("secret receipt and path")
        return [{"artifact": items[0]["path"], "sha256": items[0]["sha256"]}]
    monkeypatch.setattr(office_handoff, "select_outputs", select)
    with pytest.raises(office_workspace.WorkspaceVerificationError) as captured:
        office_workspace.verify(payload, tmp_path / "assistant_tasks")
    error = captured.value
    assert (error.reason, error.file_index, error.stage, str(error)) == (
        "source_proof", 1, "source", "workspace_source_proof")
    assert len(calls) == 2
    message = office_workspace.describe_error(error)
    assert "第2份文件" in message and "重新选择" in message
    assert "secret" not in message and payload["records"][1]["path"] not in message


@pytest.mark.parametrize("code,reason", [
    ("edit_word_complex", "unsupported"),
    ("edit_observation_limit", "limit"),
])
def test_snapshot_failure_classified_without_raw_details(prepared, tmp_path, monkeypatch,
                                                         code, reason):
    from agent_service import office_edit
    payload, _, calls = prepared
    def reject(*_args):
        raise ValueError(code)
    monkeypatch.setattr(office_edit, "snapshot_edit_source", reject)
    with pytest.raises(office_workspace.WorkspaceVerificationError) as captured:
        office_workspace.verify(payload, tmp_path / "assistant_tasks")
    error = captured.value
    assert (error.reason, error.file_index, error.stage) == (reason, 0, "range")
    assert str(error) == "workspace_" + reason and len(calls) == 2


def test_linked_file_is_source_proof_failure(prepared, tmp_path):
    payload, files, calls = prepared
    backup = tmp_path / "original-report.docx"
    files[0].rename(backup)
    try:
        files[0].symlink_to(backup)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(office_workspace.WorkspaceVerificationError) as captured:
        office_workspace.verify(payload, tmp_path / "assistant_tasks")
    assert (captured.value.reason, captured.value.file_index) == ("source_proof", 0)
    assert calls == []


def test_unknown_error_description_is_generic():
    assert office_workspace.describe_error(ValueError("private path and text")) == (
        "工作区无法核验；请重新选择已核验的成果和范围。")
