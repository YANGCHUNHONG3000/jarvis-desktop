"""Adversarial offline cases; fixture workers are synthetic, never live evidence."""
import json
from pathlib import Path
import tempfile
import uuid

import pytest

from agent_service import local_skills as skills, office_host, office_workflow as workflow, skill_resume
from test_skill_resume import _contract


@pytest.fixture
def guarded_contract(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="srg-", dir=Path.cwd()) as directory:
        yield _contract(Path(directory), monkeypatch)


@pytest.mark.parametrize("mutation", ["source", "source_metadata", "review_bytes", "state_bytes",
                                     "implementation", "unknown_next", "permit", "worker", "artifact"])
def test_changed_evidence_rejects_before_new_effect(guarded_contract, monkeypatch, mutation):
    base, store, task, digest = guarded_contract
    preview = skill_resume.preview_resume(task, base, store)
    assert preview["eligible"]
    parent = json.loads((task / "result.json").read_text(encoding="utf-8"))
    work = Path(parent["workflow_root"])
    calls = []
    monkeypatch.setattr(office_host, "execute_office_supervised", lambda *args: calls.append(args))
    if mutation == "source":
        selected = next(p for p in (task / "sources").iterdir() if p.name in {"source.csv", "source.xlsx", "source.docx"})
        selected.write_bytes(selected.read_bytes() + b"changed")
    elif mutation == "source_metadata":
        path = task / "sources" / "source.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["display_name"] = "changed.docx"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif mutation in {"review_bytes", "state_bytes"}:
        path = (store / "copy-doc" / digest / "review.json" if mutation == "review_bytes"
                else store / "copy-doc" / "active.json")
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "implementation":
        monkeypatch.setattr(skills, "implementation_sha256", lambda: "0" * 64)
    elif mutation == "unknown_next":
        (work / "s2").mkdir()
    elif mutation == "permit":
        (task / "skill-resume-permit.json").write_text("{}", encoding="utf-8")
    elif mutation == "worker":
        path = work / "s1" / "worker-outcome.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["app"]["version"] = "changed"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "artifact":
        Path(parent["artifacts"][0]["path"]).write_bytes(b"broken artifact")
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
    attempt_id = uuid.uuid4().hex
    with pytest.raises((ValueError, OSError, TypeError, KeyError)):
        skill_resume.resume_task(task, base, store=store, expected_token=preview["token"], attempt_id=attempt_id)
    assert calls == []
    assert not (task / ("skill-resume-" + attempt_id)).exists()
    assert not (work / "s2" / "intent.json").exists()


def test_precreated_cancel_only_attempt_has_no_new_effect(guarded_contract, monkeypatch):
    base, store, task, _ = guarded_contract
    preview = skill_resume.preview_resume(task, base, store)
    attempt_id = uuid.uuid4().hex
    attempt = task / ("skill-resume-" + attempt_id)
    attempt.mkdir()
    (attempt / "cancel.flag").touch()
    result = skill_resume.resume_task(task, base, store=store, expected_token=preview["token"], attempt_id=attempt_id)
    assert result["state"] == "cancelled" and result["completed"] == 1
    assert not (Path(result["workflow_root"]) / "s2").exists()
    assert skill_resume.read_resume_result(task, attempt, base)["state"] == "cancelled"
    assert not skill_resume.preview_resume(task, base, store)["eligible"]


def test_guard_rechecks_publisher_after_outer_preview(guarded_contract, monkeypatch):
    base, store, task, _ = guarded_contract
    preview = skill_resume.preview_resume(task, base, store)
    original_run = workflow.run_office_workflow
    effect_calls = []
    from agent_service import email_draft
    monkeypatch.setattr(email_draft, "execute_draft", lambda *args: effect_calls.append(args))

    def change_then_run(*args, **kwargs):
        path = store / "copy-doc" / "active.json"
        path.write_bytes(path.read_bytes() + b"\n")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(workflow, "run_office_workflow", change_then_run)
    result = skill_resume.resume_task(task, base, store=store, expected_token=preview["token"],
                                     attempt_id=uuid.uuid4().hex)
    assert result["state"] == "unknown" and effect_calls == []
    assert not (Path(result["workflow_root"]) / "s2").exists()
    assert not skill_resume.preview_resume(task, base, store)["eligible"]


def test_completed_history_survives_later_code_and_gate_changes(guarded_contract, monkeypatch):
    base, store, task, digest = guarded_contract
    preview = skill_resume.preview_resume(task, base, store)
    result = skill_resume.resume_task(task, base, store=store, expected_token=preview["token"],
                                     attempt_id=uuid.uuid4().hex)
    assert result["state"] == "succeeded"
    monkeypatch.setattr(skills, "implementation_sha256", lambda: "0" * 64)
    (store / "copy-doc" / digest / "review.json").write_text("{}", encoding="utf-8")
    assert skill_resume.latest_continuation(task, base)["state"] == "succeeded"
    assert not skill_resume.preview_resume(task, base, store)["eligible"]
