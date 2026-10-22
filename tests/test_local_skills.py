"""Offline publisher tests: synthetic files and injected executor only."""

from hashlib import sha256
import json
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET
import zipfile

import pytest

from agent_service import local_skills as skills, office


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def manifest(*, version=1, recipe="document_copy", draft=False):
    return {"schema": 1, "id": "copy-doc", "version": version, "name": "资料副本",
            "recipe": recipe, "sum_columns": [] if recipe == "document_copy" else [1], "draft": draft}


def word(path, spec):
    root = ET.Element(f"{{{W}}}document")
    body = ET.SubElement(root, f"{{{W}}}body")
    for value in [spec["title"], *spec["paragraphs"]]:
        para = ET.SubElement(body, f"{{{W}}}p")
        ET.SubElement(ET.SubElement(para, f"{{{W}}}r"), f"{{{W}}}t").text = value
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", ET.tostring(root, encoding="utf-8"))


def fake_office(spec, step_root, stop):
    assert not stop()
    assert spec["kind"] == "document"
    path = step_root / "document.docx"
    word(path, spec)
    observed = office._public_observation(spec, office.verify_office_file(path, spec))
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(path.read_bytes()).hexdigest(), "observed": observed,
            "app": {"progid": "KWPS.Application", "name": "Fake WPS", "version": "12"},
            "application_verified": True, "file_verified": True}


def setup(tmp_path, *, candidate=None):
    store, base = tmp_path / "skills", tmp_path / "tasks"
    candidate = candidate or manifest()
    digest = skills.register_candidate(store, candidate)["sha256"]
    return store, base, digest


def source(tmp_path):
    path = tmp_path / "source.docx"
    word(path, {"kind": "document", "title": "源标题", "paragraphs": ["原文一", "原文二"]})
    return path


def task(base):
    return base / ("task-" + uuid.uuid4().hex)


def gate(store, digest):
    skills.verify_offline(store, "copy-doc", digest)
    skills.record_review(store, "copy-doc", digest, "Maintainer", "Reviewed fixed recipe")


def test_candidate_cannot_grant_fields_or_mutate_source(tmp_path):
    bad = manifest()
    bad["python"] = "print('execute')"
    with pytest.raises(skills.SkillError):
        skills.validate_manifest(bad)
    bad = manifest(recipe="table_report")
    bad["sum_columns"] = [1, 1]
    with pytest.raises(skills.SkillError):
        skills.validate_manifest(bad)
    snapshot = {"source_id": "selected", "kind": "table", "sha256": "a" * 64,
                "display_name": "input.csv", "observed": {"headers": ["item", "amount"],
                                                          "rows": [["a", 3], ["b", 5]]}}
    original = json.loads(json.dumps(snapshot))
    steps = skills.build_steps(manifest(recipe="table_report"), snapshot)
    assert steps[0]["data"]["headers"] == ["item", "amount"]
    assert steps[0]["data"]["rows"] == [["a", 3], ["b", 5]]
    assert steps[0]["data"]["sum_columns"] == [1]
    assert snapshot == original


def test_offline_and_review_do_not_enable_without_real_live_evidence(tmp_path):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    assert skills.status(store, "copy-doc", base)["enabled"] is False
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.activate(store, "copy-doc", digest, base)


def test_fake_office_trial_cannot_be_recorded_as_live(tmp_path):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    path = task(base)
    result = skills.run_skill(store, "copy-doc", path, base,
                              source_file=source(tmp_path), trial_digest=digest,
                              execute=fake_office)
    assert result["state"] == "succeeded"
    request = json.loads((path / "skill-request.json").read_text(encoding="utf-8"))
    assert request["executor"] == "injected_test"
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.record_live(store, "copy-doc", digest, path, base)
    # Changing the marker cannot invent worker request/outcome evidence.
    request["executor"] = "supervised_office"
    (path / "skill-request.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.record_live(store, "copy-doc", digest, path, base)
    assert not (store / "copy-doc" / digest / "live.json").exists()


def test_replay_and_cancelled_task_block_before_executor(tmp_path):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    source_path = source(tmp_path)
    path = task(base)
    calls = []
    def execute(spec, root, stop):
        calls.append(1)
        return fake_office(spec, root, stop)
    assert skills.run_skill(store, "copy-doc", path, base, source_file=source_path,
                            trial_digest=digest, execute=execute)["ok"]
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.run_skill(store, "copy-doc", path, base, source_file=source_path,
                         trial_digest=digest, execute=execute)
    assert len(calls) == 1
    cancelled = task(base)
    cancelled.mkdir()
    (cancelled / "cancel.flag").touch()
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.run_skill(store, "copy-doc", cancelled, base, source_file=source_path,
                         trial_digest=digest, execute=execute)
    assert len(calls) == 1


def test_stale_implementation_gate_fails(tmp_path, monkeypatch):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    monkeypatch.setattr(skills, "implementation_sha256", lambda: "0" * 64)
    target = task(base)
    calls = []
    def execute(spec, root, stop):
        calls.append(1)
        return fake_office(spec, root, stop)
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.run_skill(store, "copy-doc", target, base,
                         source_file=source(tmp_path), trial_digest=digest,
                         execute=execute)
    assert not target.exists() and calls == []


def test_activation_bound_and_rollback_require_verified_previous(tmp_path, monkeypatch):
    store, base, first = setup(tmp_path)
    gate(store, first)
    second = skills.register_candidate(store, manifest(version=2))["sha256"]
    gate(store, second)
    with pytest.raises(skills.SkillError):
        skills.activate(store, "copy-doc", first, base, runs=11)
    # A patched gate isolates the activation counter; it is not live evidence.
    monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
    state = skills.activate(store, "copy-doc", first, base, runs=2)
    assert state["remaining_runs"] == 2
    state = skills.activate(store, "copy-doc", second, base, runs=1)
    assert state["previous"] == first
    monkeypatch.undo()
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.rollback(store, "copy-doc", base)


def test_crashed_inflight_reconcile_does_not_replay(tmp_path, monkeypatch):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    state = {"active": digest, "previous": None, "remaining_runs": 1,
             "inflight": str(task(base)), "blocked": []}
    active = store / "copy-doc" / "active.json"
    active.write_text(json.dumps(state), encoding="utf-8")
    def never(*args, **kwargs):
        pytest.fail("reconcile replayed an effect")
    monkeypatch.setattr(skills.workflow, "run_office_workflow", never)
    result = skills.reconcile(store, "copy-doc", base)
    assert result["active"] is None and digest in result["blocked"]
    assert result["inflight"] is None and result["remaining_runs"] == 0


def test_active_budget_consumed_and_unknown_disables_without_replay(tmp_path, monkeypatch):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    source_path = source(tmp_path)
    # Isolate the run counter. This mock supplies no live certification.
    monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
    skills.activate(store, "copy-doc", digest, base, runs=2)
    first = skills.run_skill(store, "copy-doc", task(base), base,
                             source_file=source_path, execute=fake_office)
    assert first["state"] == "succeeded"
    assert skills.status(store, "copy-doc", base)["remaining_runs"] == 1

    second_task = task(base)
    calls = []
    def failed(spec, step_root, stop):
        calls.append(1)
        raise RuntimeError("synthetic executor failure")
    second = skills.run_skill(store, "copy-doc", second_task, base,
                              source_file=source_path, execute=failed)
    assert second["state"] == "unknown" and len(calls) == 1
    state = skills.status(store, "copy-doc", base)
    assert state["enabled"] is False and digest in state["blocked"]
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.run_skill(store, "copy-doc", second_task, base,
                         source_file=source_path, execute=failed)
    assert len(calls) == 1


def test_linked_store_ancestor_rejected_before_creation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory links unavailable")
    with pytest.raises((skills.SkillError, ValueError, OSError)):
        skills.register_candidate(link / "skills", manifest())
    assert list(outside.iterdir()) == []


def test_status_disables_after_live_evidence_revalidation_fails(tmp_path, monkeypatch):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    monkeypatch.setattr(skills, "_live_check", lambda *args: {"passed": True})
    assert skills.activate(store, "copy-doc", digest, base, runs=1)["active"] == digest
    assert skills.status(store, "copy-doc", base)["enabled"] is True
    def changed(*args):
        raise skills.SkillError("skill_live_result_changed")
    monkeypatch.setattr(skills, "_live_check", changed)
    assert skills.status(store, "copy-doc", base)["enabled"] is False


def test_rollback_succeeds_only_with_verified_previous_and_never_executes(tmp_path, monkeypatch):
    store, base, first = setup(tmp_path)
    gate(store, first)
    second = skills.register_candidate(store, manifest(version=2))["sha256"]
    gate(store, second)
    checked = []
    def check(path, digest, supplied_base):
        checked.append(digest)
        assert supplied_base == base
        return {"passed": True}
    monkeypatch.setattr(skills, "_live_check", check)
    monkeypatch.setattr(skills.workflow, "run_office_workflow", lambda *a, **k: pytest.fail("rollback executed Office"))
    skills.activate(store, "copy-doc", first, base, runs=1)
    skills.activate(store, "copy-doc", second, base, runs=1)
    result = skills.rollback(store, "copy-doc", base)
    assert result["active"] == first and second in result["blocked"]
    assert checked == [first, second, first]


def test_managed_artifact_cannot_use_external_source_file_route(tmp_path):
    store, base, digest = setup(tmp_path)
    gate(store, digest)
    managed = base / ("task-" + uuid.uuid4().hex) / ("workflow-" + uuid.uuid4().hex) / "s1"
    managed.mkdir(parents=True)
    path = managed / "document.docx"
    word(path, {"kind": "document", "title": "源标题", "paragraphs": ["原文"]})
    target = task(base)
    calls = []
    def execute(spec, root, stop):
        calls.append(1)
        return fake_office(spec, root, stop)
    result = skills.run_skill(store, "copy-doc", target, base,
                              source_file=path, trial_digest=digest, execute=execute)
    assert result["kind"] == "skill" and result["phase"] == "preflight"
    assert result["effect_started"] is False and calls == []
    assert json.loads((target / "result.json").read_text(encoding="utf-8")) == result
