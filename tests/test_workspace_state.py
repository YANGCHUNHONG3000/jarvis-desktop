"""Local workspace persistence and explicit Office provenance recovery."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from threading import Barrier
import pytest

from agent_service import office_handoff, workspace_state as workspace


def _payload(tmp_path, draft="  next task\r\n"):
    path = str(tmp_path / "report.docx")
    return {"draft": draft,
            "records": [{"path": path, "sha256": "a" * 64}],
            "source": {"path": path, "sha256": "a" * 64}}


def test_save_reload_preserves_exact_draft_and_idempotent_snapshot(tmp_path):
    store = tmp_path / "workspace"
    payload = _payload(tmp_path)
    first = workspace.save(store, payload, None)
    assert first == workspace.load(store)
    assert first["payload"]["draft"] == "  next task\r\n"
    payload["draft"] = "changed after save"
    first["payload"]["draft"] = "changed after return"
    assert workspace.load(store)["payload"]["draft"] == "  next task\r\n"
    same = workspace.save(store, _payload(tmp_path), first["id"])
    assert same["id"] == first["id"]
    assert len(list(store.glob("snapshot-*.json"))) == 1


def test_compare_and_swap_rejects_stale_window(tmp_path):
    store = tmp_path / "workspace"
    initial = workspace.save(store, _payload(tmp_path), None)
    changed = _payload(tmp_path, "window one")
    winner = workspace.save(store, changed, initial["id"])
    pointer = store / "latest.json"
    before = pointer.stat().st_mtime_ns
    with pytest.raises(workspace.WorkspaceError, match="^workspace_conflict$"):
        workspace.save(store, _payload(tmp_path, "window two"), initial["id"])
    assert workspace.load(store) == winner
    assert pointer.stat().st_mtime_ns == before
    assert len(list(store.glob("snapshot-*.json"))) == 2


def test_concurrent_new_window_compare_and_swap(tmp_path):
    store = tmp_path / "workspace"
    first = workspace.save(store, _payload(tmp_path), None)
    gate = Barrier(2)
    def save_one(text):
        gate.wait(timeout=5)
        try:
            return workspace.save(store, _payload(tmp_path, text), first["id"])
        except workspace.WorkspaceError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save_one, ("first", "second")))
    winners = [item for item in outcomes if type(item) is dict]
    assert len(winners) == 1
    assert set(item for item in outcomes if type(item) is str) <= {
        "workspace_busy", "workspace_conflict"}
    assert workspace.load(store) == winners[0]


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(extra="approval"),
    lambda p: p.update(draft="x" * 12001),
    lambda p: p.update(records=[p["records"][0]] * 2),
    lambda p: p["records"][0].update(sha256="bad"),
    lambda p: p.update(source={"path": p["source"]["path"], "sha256": "b" * 64}),
])
def test_payload_rejects_extra_oversized_duplicate_or_conflicting_source(tmp_path, mutation):
    payload = _payload(tmp_path)
    mutation(payload)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_payload_invalid$"):
        workspace.save(tmp_path / "workspace", payload, None)


def test_casefold_duplicate_rejected(tmp_path):
    payload = _payload(tmp_path)
    other = deepcopy(payload["records"][0])
    other["path"] = other["path"].upper()
    payload["records"].append(other)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_payload_invalid$"):
        workspace.save(tmp_path / "workspace", payload, None)


def test_corrupt_pointer_snapshot_and_hash_rejected(tmp_path):
    store = tmp_path / "workspace"
    saved = workspace.save(store, _payload(tmp_path), None)
    pointer_path = store / "latest.json"
    original = pointer_path.read_bytes()
    pointer_path.write_text('{"id":', encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError, match="^workspace_record_invalid$"):
        workspace.load(store)
    pointer_path.write_bytes(original)
    snapshot = store / ("snapshot-" + saved["id"] + ".json")
    snapshot.write_bytes(snapshot.read_bytes() + b" ")
    with pytest.raises(workspace.WorkspaceError, match="^workspace_record_invalid$"):
        workspace.load(store)


def test_oversized_snapshot_rejected(tmp_path):
    store = tmp_path / "workspace"
    saved = workspace.save(store, _payload(tmp_path), None)
    snapshot = store / ("snapshot-" + saved["id"] + ".json")
    snapshot.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(workspace.WorkspaceError, match="^workspace_oversize$"):
        workspace.load(store)


def test_oversized_pointer_and_new_snapshot_rejected(tmp_path, monkeypatch):
    store = tmp_path / "workspace"
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT", 100)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_oversize$"):
        workspace.save(store, _payload(tmp_path), None)
    assert not (store / "latest.json").exists()
    assert not list(store.glob("snapshot-*.json"))
    monkeypatch.setattr(workspace, "_MAX_SNAPSHOT", 64 * 1024)
    workspace.save(store, _payload(tmp_path), None)
    pointer = store / "latest.json"
    pointer.write_bytes(b"x" * 1025)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_oversize$"):
        workspace.load(store)


def test_duplicate_json_keys_in_pointer_or_snapshot_rejected(tmp_path):
    store = tmp_path / "workspace"
    saved = workspace.save(store, _payload(tmp_path), None)
    pointer = store / "latest.json"
    original_pointer = pointer.read_bytes()
    pointer.write_text('{"id":"' + saved["id"] + '","id":"' + saved["id"]
                       + '","sha256":"' + "a" * 64 + '"}', encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError, match="^workspace_record_invalid$"):
        workspace.load(store)
    pointer.write_bytes(original_pointer)
    snapshot = store / ("snapshot-" + saved["id"] + ".json")
    duplicate = ('{"id":"' + saved["id"] + '","id":"' + saved["id"]
                 + '","payload":' + json.dumps(saved["payload"]) + '}').encode("utf-8")
    snapshot.write_bytes(duplicate)
    pointer.write_bytes(workspace._encode({"id": saved["id"],
                                          "sha256": sha256(duplicate).hexdigest()}))
    with pytest.raises(workspace.WorkspaceError, match="^workspace_record_invalid$"):
        workspace.load(store)


def test_deeply_nested_snapshot_does_not_crash_load_or_controller(tmp_path):
    from agent_service.workspace_resume import WorkspaceResume

    store = tmp_path / "workspace"
    saved = workspace.save(store, _payload(tmp_path), None)
    snapshot = store / ("snapshot-" + saved["id"] + ".json")
    snapshot.write_bytes(b"[" * 20_000 + b"0" + b"]" * 20_000)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_record_invalid$"):
        workspace.load(store)
    controller = WorkspaceResume(
        context=lambda: {"payload": _payload(tmp_path)},
        busy=lambda: False, on_busy=lambda _value: None,
        apply=lambda _value: None, status=lambda _value: None,
        store=store, task_base=tmp_path)
    assert controller.expected_id is None


def test_linked_pointer_and_root_rejected(tmp_path):
    store = tmp_path / "workspace"
    workspace.save(store, _payload(tmp_path), None)
    latest = store / "latest.json"
    backup = store / "saved-pointer.json"
    latest.rename(backup)
    try:
        latest.symlink_to(backup)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(workspace.WorkspaceError, match="^workspace_path_invalid$"):
        workspace.load(store)
    latest.unlink()
    backup.rename(latest)
    alias = tmp_path / "workspace-link"
    alias.symlink_to(store, target_is_directory=True)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_path_invalid$"):
        workspace.load(alias)


def test_linked_snapshot_and_office_path_rejected(tmp_path):
    store = tmp_path / "workspace"
    saved = workspace.save(store, _payload(tmp_path), None)
    snapshot = store / ("snapshot-" + saved["id"] + ".json")
    backup = store / "saved-snapshot.json"
    snapshot.rename(backup)
    try:
        snapshot.symlink_to(backup)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(workspace.WorkspaceError, match="^workspace_path_invalid$"):
        workspace.load(store)
    office = tmp_path / "report.docx"
    office.symlink_to(backup)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_path_invalid$"):
        workspace.verify(_payload(tmp_path), tmp_path)


def test_replace_failure_preserves_old_pointer_and_snapshot(tmp_path, monkeypatch):
    store = tmp_path / "workspace"
    first = workspace.save(store, _payload(tmp_path), None)
    pointer_bytes = (store / "latest.json").read_bytes()
    pointer_mtime = (store / "latest.json").stat().st_mtime_ns
    old_snapshot = store / ("snapshot-" + first["id"] + ".json")
    snapshot_bytes = old_snapshot.read_bytes()
    snapshot_mtime = old_snapshot.stat().st_mtime_ns
    def fail_replace(*_args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    with pytest.raises(workspace.WorkspaceError, match="^workspace_io$"):
        workspace.save(store, _payload(tmp_path, "new"), first["id"])
    assert (store / "latest.json").read_bytes() == pointer_bytes
    assert (store / "latest.json").stat().st_mtime_ns == pointer_mtime
    assert old_snapshot.read_bytes() == snapshot_bytes
    assert old_snapshot.stat().st_mtime_ns == snapshot_mtime
    assert workspace.load(store) == first
    assert len(list(store.glob("snapshot-*.json"))) == 2
    assert len(list(store.glob("latest-*.tmp"))) == 1


def test_verify_reuses_batch_reference_for_selected_source(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    source_ref = {"artifact": payload["source"]["path"], "sha256": "a" * 64}
    calls = []
    def select(records, *, base):
        calls.append((deepcopy(records), base))
        return [source_ref]
    monkeypatch.setattr(office_handoff, "select_outputs", select)
    result = workspace.verify(payload, tmp_path)
    assert result["refs"] == [source_ref]
    assert result["source_ref"] is source_ref
    assert calls == [(payload["records"], tmp_path)]
    result["payload"]["draft"] = "edited"
    assert payload["draft"] == "  next task\r\n"


def test_verify_external_source_checked_once_after_batch(tmp_path, monkeypatch):
    payload = _payload(tmp_path)
    payload["source"] = {"path": str(tmp_path / "prior.xlsx"), "sha256": "b" * 64}
    calls = []
    def select(records, *, base):
        calls.append(deepcopy(records))
        return [{"artifact": item["path"]} for item in records]
    monkeypatch.setattr(office_handoff, "select_outputs", select)
    result = workspace.verify(payload, tmp_path)
    assert calls == [payload["records"], [payload["source"]]]
    assert result["source_ref"] == {"artifact": payload["source"]["path"]}
