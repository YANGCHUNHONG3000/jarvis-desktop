"""Skill input failures and prepared execution never replay an Office effect."""

from hashlib import sha256
import json
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET
import zipfile

import pytest

from agent_service import artifact_source, local_skills as skills, office, office_resume, office_source


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def manifest(recipe="document_copy", *, version=1):
    return {"schema": 1, "id": "copy-doc", "version": version, "name": "资料副本",
            "recipe": recipe, "sum_columns": [], "draft": False}


def word(path, title="源标题", paragraphs=("原文",)):
    root = ET.Element(f"{{{W}}}document")
    body = ET.SubElement(root, f"{{{W}}}body")
    for value in (title, *paragraphs):
        p = ET.SubElement(body, f"{{{W}}}p")
        ET.SubElement(ET.SubElement(p, f"{{{W}}}r"), f"{{{W}}}t").text = value
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", ET.tostring(root, encoding="utf-8"))


def setup(tmp_path, recipe="document_copy"):
    store, base = tmp_path / "skills", tmp_path / "tasks"
    candidate = manifest(recipe)
    digest = skills.register_candidate(store, candidate)["sha256"]
    skills.verify_offline(store, candidate["id"], digest)
    skills.record_review(store, candidate["id"], digest, "reviewer", "fixed recipe")
    return store, base, digest


def task(base):
    return base / ("task-" + uuid.uuid4().hex)


def fake_office(spec, step_root, stop):
    assert not stop()
    assert spec["kind"] == "document"
    artifact = step_root / "document.docx"
    word(artifact, spec["title"], spec["paragraphs"])
    observed = office._public_observation(spec, office.verify_office_file(artifact, spec))
    return {"ok": True, "state": "succeeded", "artifact": str(artifact),
            "sha256": sha256(artifact.read_bytes()).hexdigest(), "observed": observed,
            "app": {"progid": "KWPS.Application", "name": "Fake WPS", "version": "12"},
            "application_verified": True, "file_verified": True}


def test_wrong_kind_persists_preflight_without_consuming_active_run(tmp_path, monkeypatch):
    store, base, digest = setup(tmp_path)
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, base, runs=2)
    source = tmp_path / "data.csv"
    source.write_text("id,value\nA,3\n", encoding="utf-8")
    root = task(base)
    calls = []
    result = skills.run_skill(store, "copy-doc", root, base, source_file=source,
                              expected_digest=digest,
                              execute=lambda *_: calls.append(1))
    assert result["kind"] == "skill" and result["state"] == "failed"
    assert result["phase"] == "preflight" and result["effect_started"] is False
    assert result["error"] == "skill_source_kind_mismatch"
    assert calls == [] and not list(root.glob("workflow-*"))
    request = json.loads((root / "skill-request.json").read_text(encoding="utf-8"))
    assert request["sha256"] == digest and request["workflow_root"].startswith(str(root))
    assert json.loads((root / "result.json").read_text(encoding="utf-8")) == result
    state = skills.status(store, "copy-doc", base)
    assert state["active"] == digest and state["remaining_runs"] == 2 and not state["blocked"]
    with pytest.raises(skills.SkillError, match="skill_task_replay_blocked"):
        skills.run_skill(store, "copy-doc", root, base, source_file=source,
                         expected_digest=digest, execute=lambda *_: calls.append(1))


def test_missing_document_table_is_durable_and_never_executes(tmp_path):
    store, base, digest = setup(tmp_path, "document_table_report")
    source = tmp_path / "source.docx"
    word(source)
    root = task(base)
    result = skills.run_skill(store, "copy-doc", root, base, source_file=source,
                              trial_digest=digest, expected_digest=digest,
                              execute=lambda *_: pytest.fail("Office ran"))
    assert result["error"] == "skill_document_table_required"
    assert result["effect_started"] is False and (root / "result.json").exists()


def test_digest_pin_rechecked_before_task_claim(tmp_path):
    store, base, first = setup(tmp_path)
    second = skills.register_candidate(store, manifest(version=2))["sha256"]
    source = tmp_path / "source.docx"
    word(source)
    root = task(base)
    with pytest.raises(skills.SkillError, match="skill_selection_stale"):
        skills.run_skill(store, "copy-doc", root, base, source_file=source,
                         trial_digest=first, expected_digest=second)
    assert not root.exists()


def test_table_copy_has_exact_single_step_without_sums_or_draft():
    candidate = manifest("table_copy")
    snapshot = {"source_id": "selected", "kind": "table", "sha256": "a" * 64,
                "display_name": "data.csv", "observed": {"headers": ["id", "amount", "total"],
                "rows": [["001", "3", "8"], ["=1+1", "5", "8"]]}}
    steps = skills.build_steps(candidate, snapshot)
    assert len(steps) == 1 and steps[0]["action"] == "spreadsheet"
    assert steps[0]["data"]["headers"] == snapshot["observed"]["headers"]
    assert steps[0]["data"]["rows"] == snapshot["observed"]["rows"]
    assert steps[0]["data"]["sum_columns"] == []
    skills.validate_manifest(candidate)
    candidate["draft"] = True
    with pytest.raises(skills.SkillError):
        skills.validate_manifest(candidate)


def _prepared(tmp_path, source_file):
    base = tmp_path / "tasks"
    root = task(base)
    root.mkdir(parents=True)
    (root / "events.jsonl").write_text('{"event":"started"}\n', encoding="utf-8")
    snapshot = office_source.snapshot_source(source_file, root)
    return base, root, snapshot


def test_prepared_source_mutation_fails_durably_before_effect(tmp_path, monkeypatch):
    store, _, digest = setup(tmp_path)
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, tmp_path / "tasks", runs=1)
    source = tmp_path / "source.docx"
    word(source)
    base, root, snapshot = _prepared(tmp_path, source)
    (root / "sources" / "source.docx").write_bytes(b"tampered")
    def publish(result):
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    monkeypatch.setattr(skills.workflow, "run_office_workflow", lambda *_a, **_k: pytest.fail("Office ran"))
    result = skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                       expected_digest=digest, publish=publish)
    assert result["state"] == "failed" and result["error"] == "skill_source_changed"
    assert skills.status(store, "copy-doc", base)["remaining_runs"] == 1


def test_prepared_missing_snapshot_and_stale_pin_cannot_execute(tmp_path, monkeypatch):
    store, _, digest = setup(tmp_path)
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, tmp_path / "tasks", runs=1)
    source = tmp_path / "source.docx"
    word(source)
    base, root, snapshot = _prepared(tmp_path, source)
    calls = []
    monkeypatch.setattr(skills.workflow, "run_office_workflow", lambda *_a, **_k: calls.append(1))
    with pytest.raises(skills.SkillError, match="skill_selection_stale"):
        skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                  expected_digest="f" * 64, publish=lambda _r: None)
    assert not (root / "skill-request.json").exists() and calls == []
    (root / "sources" / "source.json").unlink()
    def publish(result):
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "state": "succeeded"}  # Callback advice cannot override durable bytes.
    result = skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                       expected_digest=digest, publish=publish)
    assert result["kind"] == "skill" and result["effect_started"] is False and calls == []
    assert result == json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert skills.status(store, "copy-doc", base)["remaining_runs"] == 1


def test_prepared_success_publishes_before_clearing_inflight(tmp_path, monkeypatch):
    store, _, digest = setup(tmp_path)
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, tmp_path / "tasks", runs=1)
    source = tmp_path / "source.docx"
    word(source)
    base, root, snapshot = _prepared(tmp_path, source)
    original = skills.workflow.run_office_workflow
    def workflow_with_fake(steps, path, **kwargs):
        assert kwargs["execute"] is None
        return original(steps, path, execute=fake_office, deadline=kwargs["deadline"],
                        cancel_requested=kwargs["cancel_requested"])
    monkeypatch.setattr(skills.workflow, "run_office_workflow", workflow_with_fake)
    def publish(result):
        assert skills._state(skills._store(store), "copy-doc")["inflight"] == str(root)
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    result = skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                       expected_digest=digest, publish=publish)
    assert result["state"] == "succeeded" and result["kind"] == "office"
    assert skills._state(skills._store(store), "copy-doc")["inflight"] is None
    assert skills.status(store, "copy-doc", base)["remaining_runs"] == 0
    preview = office_resume.preview_resume(root, base)
    assert preview["eligible"] is False and preview["reason"] == "resume_skill_owned"
    with pytest.raises(office_resume.ResumeError, match="resume_skill_owned"):
        office_resume.resume_task(root, base, "0" * 64, uuid.uuid4().hex)
    assert not list(root.glob("resume-*"))


def test_parent_skill_marker_blocks_generic_resume_if_request_missing(tmp_path):
    base = tmp_path / "tasks"
    root = task(base)
    root.mkdir(parents=True)
    (root / "result.json").write_text(json.dumps({"kind": "office", "state": "failed",
        "ok": False, "root": str(root), "skill": {"id": "copy-doc"}}), encoding="utf-8")
    assert office_resume.preview_resume(root, base)["reason"] == "resume_skill_owned"


def test_publish_failure_keeps_active_inflight_unknown(tmp_path, monkeypatch):
    store, _, digest = setup(tmp_path)
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, tmp_path / "tasks", runs=1)
    source = tmp_path / "source.docx"
    word(source)
    base, root, snapshot = _prepared(tmp_path, source)
    original = skills.workflow.run_office_workflow
    monkeypatch.setattr(skills.workflow, "run_office_workflow",
                        lambda steps, path, **kw: original(steps, path, execute=fake_office,
                                                           deadline=kw["deadline"],
                                                           cancel_requested=kw["cancel_requested"]))
    def lost_publish(_result):
        raise OSError("synthetic publish interruption")
    with pytest.raises(OSError):
        skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                  expected_digest=digest, publish=lost_publish)
    state = skills._state(skills._store(store), "copy-doc")
    assert state["inflight"] == str(root) and state["remaining_runs"] == 0
    assert not (root / "result.json").exists()
    with pytest.raises(skills.SkillError, match="skill_disabled_or_unknown"):
        skills.run_prepared_skill(store, "copy-doc", root, base, snapshot=snapshot,
                                  expected_digest=digest, publish=lost_publish)


@pytest.mark.parametrize("tamper", [None, "source", "provenance"])
def test_prepared_generated_artifact_rechecks_original_receipt(tmp_path, monkeypatch, tamper):
    store, base, digest = setup(tmp_path)
    source = tmp_path / "source.docx"
    word(source)
    original_task = task(base)
    first = skills.run_skill(store, "copy-doc", original_task, base, source_file=source,
                             trial_digest=digest, expected_digest=digest, execute=fake_office)
    assert first["state"] == "succeeded"
    # This original is skill-owned, so generic continuation is blocked; its
    # private verified receipt snapshot remains available to artifact handoff.
    assert office_resume.preview_resume(original_task, base)["reason"] == "resume_skill_owned"
    ref = artifact_source.prepare_artifact_source(first["artifact"],
                                                  first["readback_sha256"], base)
    prepared = task(base)
    prepared.mkdir(parents=True)
    (prepared / "events.jsonl").write_text('{"event":"started"}\n', encoding="utf-8")
    snapshot = artifact_source.snapshot_artifact_source(ref, prepared, base)
    assert snapshot["kind"] == "document"
    if tamper == "source":
        (prepared / "sources" / "source.docx").write_bytes(b"changed")
    elif tamper == "provenance":
        path = prepared / "sources" / "provenance.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["token"] = "f" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(skills, "_live_check", lambda *_: {"passed": True})
    skills.activate(store, "copy-doc", digest, base, runs=1)
    original_run = skills.workflow.run_office_workflow
    calls = []
    def run(steps, path, **kwargs):
        calls.append(1)
        return original_run(steps, path, execute=fake_office,
                            deadline=kwargs["deadline"], cancel_requested=kwargs["cancel_requested"])
    monkeypatch.setattr(skills.workflow, "run_office_workflow", run)
    def publish(result):
        (prepared / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    result = skills.run_prepared_skill(store, "copy-doc", prepared, base, snapshot=snapshot,
                                       expected_digest=digest, publish=publish)
    if tamper is None:
        assert result["state"] == "succeeded" and calls == [1]
    else:
        assert result["state"] == "failed" and result["phase"] == "preflight"
        assert result["effect_started"] is False and calls == []
