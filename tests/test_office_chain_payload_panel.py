"""Exact final-payload handoff through the real task-chain panel worker."""
from copy import deepcopy

import pytest

from agent_service import office_chain_payload, office_task_chain_panel as module
from test_office_task_chain_panel import Thread, finish, harness, mutate


def matching_proof(state):
    record = deepcopy(state["payload"]["records"][0])
    return {
        "payload": deepcopy(state["payload"]),
        "refs": [deepcopy(record)],
        "source_ref": deepcopy(state["payload"]["source"]),
    }


def counted_verify(monkeypatch):
    calls = []
    def verify(payload, base):
        calls.append((deepcopy(payload), base))
        return {"payload": deepcopy(payload), "refs": [], "source_ref": None}
    monkeypatch.setattr(module.workspace_state, "verify", verify)
    return calls


def publish(state, panel):
    office_chain_payload.publish(state, panel.task_base, matching_proof(state))


def test_matching_final_proof_skips_duplicate_workspace_verify(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    calls = counted_verify(monkeypatch)

    assert panel._begin(lambda: (publish(state, panel), deepcopy(state))[1])
    assert len(Thread.all) == 1
    finish(panel)

    assert calls == []
    assert len(log.applied) == 1
    assert log.applied[0]["payload"] == state["payload"]


@pytest.mark.parametrize("mismatch", ["missing", "state"])
def test_missing_or_mismatched_proof_falls_back_to_workspace_verify(tmp_path, monkeypatch, mismatch):
    panel, _context, _log, state = harness(tmp_path, monkeypatch)
    calls = counted_verify(monkeypatch)

    def operation():
        result = deepcopy(state)
        if mismatch == "state":
            publish(state, panel)
            result["reason"] = "changed after publication"
        return result

    assert panel._begin(operation)
    finish(panel)

    assert len(calls) == 1
    assert calls[0][0] == state["payload"]


@pytest.mark.parametrize("late_change", ["cancel", "new_draft"])
def test_late_cancel_or_new_draft_does_not_adopt_matching_proof(tmp_path, monkeypatch, late_change):
    panel, context, log, state = harness(tmp_path, monkeypatch)
    calls = counted_verify(monkeypatch)

    def operation():
        publish(state, panel)
        if late_change == "cancel":
            panel.cancel()
        return deepcopy(state)

    assert panel._begin(operation)
    if late_change == "new_draft":
        mutate(context, "main_new")
    finish(panel)

    assert log.applied == []
    assert context["workspace"]["payload"]["draft"] == (
        "new main instruction" if late_change == "new_draft" else "")
    assert len(calls) <= 1


def test_edit_preview_reuses_payload_proof_but_still_verifies_edit_payload(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    state.update(status="edit_preview", edit_payload={"scope": "independent-edit-proof"})
    calls = counted_verify(monkeypatch)
    from agent_service import office_workspace
    edit_calls = []
    monkeypatch.setattr(office_workspace, "verify",
                        lambda payload, base: edit_calls.append((deepcopy(payload), base)) or {"edit": True})
    panel.apply_office = lambda verified: verified == {"edit": True}
    panel.prepare_edit = lambda restored: True

    assert panel._begin(lambda: (publish(state, panel), deepcopy(state))[1])
    finish(panel)

    assert calls == []
    assert len(edit_calls) == 1 and edit_calls[0][0] == state["edit_payload"]
    assert len(log.applied) == 1


def test_consecutive_requests_do_not_share_consumed_handoff(tmp_path, monkeypatch):
    panel, _context, _log, state = harness(tmp_path, monkeypatch)
    calls = counted_verify(monkeypatch)

    assert panel._begin(lambda: (publish(state, panel), deepcopy(state))[1])
    finish(panel)
    assert calls == []

    assert panel._begin(lambda: deepcopy(state))
    finish(panel)

    assert len(calls) == 1


def test_handoff_is_single_use_and_ignores_publish_after_take(tmp_path):
    record = {"path": str(tmp_path / "source.docx"), "sha256": "c" * 64}
    state = {"payload": {"draft": "authored", "records": [record], "source": record}}
    proof = matching_proof(state)

    with office_chain_payload.capture() as handoff:
        office_chain_payload.publish(state, tmp_path / "tasks", proof)
        first = handoff.take(state, tmp_path / "tasks")
        office_chain_payload.publish(state, tmp_path / "tasks", {
            **proof, "payload": {**state["payload"], "draft": "late mutation"}})
        second = handoff.take(state, tmp_path / "tasks")

    assert first == proof
    assert second is None


def test_exception_after_publish_cleans_handoff_for_next_request(tmp_path, monkeypatch):
    panel, _context, log, state = harness(tmp_path, monkeypatch)
    calls = counted_verify(monkeypatch)

    def fail_after_publish():
        publish(state, panel)
        raise RuntimeError("controlled operation failure")

    assert panel._begin(fail_after_publish)
    finish(panel)
    assert not panel.pending
    from agent_service.office_chain_diagnostics import verification_failure
    assert log.statuses[-1] == verification_failure(RuntimeError('controlled operation failure'))
    assert 'controlled operation failure' not in log.statuses[-1]

    assert panel._begin(lambda: deepcopy(state))
    finish(panel)

    assert len(calls) == 1
