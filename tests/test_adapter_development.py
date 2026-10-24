"""Bounded gap-to-adapter workflow and one-shot trial recovery."""

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import uuid

import pytest

from agent_service import adapter_development as development
from agent_service import local_skills as skills, office_workflow as workflow, skill_resume
from test_local_skills import fake_office, manifest as copy_manifest, source


@pytest.fixture
def short_tmp():
    with tempfile.TemporaryDirectory(prefix="ad-") as value:
        yield Path(value)


def _gap(base, instruction):
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir(parents=True)
    task = task.resolve()
    (task / "request.txt").write_text(instruction, encoding="utf-8")
    gap = {"schema": 1, "instruction_sha256": sha256(instruction.encode()).hexdigest(),
           "model_reason": "适配器缺失", "development_request":
           {"state": "queued", "automatic_code_execution": False}}
    raw = json.dumps(gap, ensure_ascii=False).encode("utf-8")
    (task / "capability-gap.json").write_bytes(raw)
    (task / "result.json").write_text(json.dumps({"root": str(task), "ok": False,
        "state": "unsupported", "kind": "unsupported", "capability_gap":
        {"filename": "capability-gap.json", "sha256": sha256(raw).hexdigest()}}), encoding="utf-8")
    return task


def _manifest():
    return {"schema": 1, "id": "word-table-transcript", "version": 1,
            "name": "表格原文转存报告", "recipe": "document_table_transcription",
            "sum_columns": [], "draft": False}


def _source_ref():
    return {"version": 1, "task_root": "C:/managed/task", "workflow_root": "C:/managed/workflow",
            "step_id": "s1", "artifact": str(Path("C:/managed/document.docx")),
            "sha256": "a" * 64, "receipt_sha256": "b" * 64, "token": "c" * 64}


def test_gap_identity_and_unrelated_device_rejected(tmp_path, monkeypatch):
    base, store = tmp_path / "tasks", tmp_path / "capabilities"
    base.mkdir()
    task = _gap(base, "通过蓝牙读温湿度设备，不要制作模拟文档或 Word 报告")
    assert len(development.list_requests(base, store)) == 1
    monkeypatch.setattr(development, "_source_reference", lambda *a, **k: _source_ref())
    with pytest.raises(development.DevelopmentError, match="maintainer_adapter_required"):
        development.start_proposal(task, base, store, _manifest(), effect="完整复制表格并写报告，不进行任何数值转换",
                                   source_artifact=_source_ref(), proposer="Builder")
    assert not store.exists()
    (task / "request.txt").write_text("改动指令", encoding="utf-8")
    with pytest.raises(development.DevelopmentError, match="development_instruction_changed"):
        development.inspect(task, base, store)


def test_proposal_source_must_equal_gap_selected_artifact(tmp_path, monkeypatch):
    base, store = tmp_path / "tasks", tmp_path / "capabilities"
    base.mkdir()
    task = _gap(base, "请把 Word 唯一表格原样转成 Excel，不转换数字，不重新计算合计；然后生成 Word 报告，标题为“表格原文转存报告”。")
    ref = _source_ref()
    provenance = {"kind": "verified_generated_office_artifact", **ref}
    snapshot = {"source_id": "selected", "kind": "document", "sha256": ref["sha256"],
                "bytes": 100, "display_name": "source.docx", "coverage": {}, "observed": {}}
    sources = task / "sources"
    sources.mkdir()
    workflow._write_new(sources / "source.json", snapshot)
    workflow._write_new(sources / "provenance.json", provenance)
    parent = json.loads((task / "result.json").read_text(encoding="utf-8"))
    parent["source"] = {k: v for k, v in snapshot.items() if k != "observed"}
    parent["source_provenance"] = provenance
    skills._record(task / "result.json", parent)
    monkeypatch.setattr(skills, "_prepared_snapshot", lambda *a: snapshot)
    monkeypatch.setattr(development.artifact_source, "prepare_artifact_source",
                        lambda *a: ref)
    assert development._source_reference(task, base) == ref
    unrelated = dict(ref, artifact="C:/other/document.docx")
    with pytest.raises(development.DevelopmentError, match="development_source_changed"):
        development.start_proposal(task, base, store, _manifest(),
            effect="保留 Word 表格全部行列文本，生成 Excel 与注明未重算的 Word 报告",
            source_artifact=unrelated, proposer="Builder")


def test_transcription_proposal_is_versioned_and_review_is_independent(tmp_path, monkeypatch):
    base, store = tmp_path / "tasks", tmp_path / "capabilities"
    base.mkdir()
    task = _gap(base, "请把 Word 唯一表格原样转成 Excel，不转换数字，不重新计算合计；然后生成 Word 报告，标题为“表格原文转存报告”。")
    monkeypatch.setattr(development, "_source_reference", lambda *a, **k: _source_ref())
    args = dict(effect="保留 Word 表格所有行列和文本，生成 Excel 与注明未重算的 Word 报告",
                source_artifact=_source_ref(), proposer="Builder")
    result = development.start_proposal(task, base, store, _manifest(), **args)
    digest = result["proposals"][0]["digest"]
    assert result["proposals"][0]["actions"] == ["test"]
    assert development.start_proposal(task, base, store, _manifest(), **args)["versions"] == [digest]
    development.test(task, base, store, digest)
    with pytest.raises(development.DevelopmentError, match="independent_review"):
        development.review(task, base, store, digest, reviewer="builder", note="same person")
    development.review(task, base, store, digest, reviewer="Reviewer", note="Checked exact effect")
    assert development.inspect(task, base, store, digest=digest)["proposals"][0]["actions"] == ["trial"]
    path = development._proposal_path(task, store, digest)
    pinned = base / ("task-" + uuid.uuid4().hex)
    workflow._write_new(path / "trial-intent.json", {"schema": 1, "task_root": str(pinned)})
    calls = []
    def claimed(_store, _id, task_root, _base, **kwargs):
        calls.append(task_root)
        pinned.mkdir()
        return {"ok": False, "state": "unknown"}
    monkeypatch.setattr(skills, "run_skill", claimed)
    assert development.trial(task, base, store, digest)["state"] == "unknown"
    assert calls == [pinned]
    with pytest.raises(development.DevelopmentError, match="trial_already_claimed"):
        development.trial(task, base, store, digest)
    second = _manifest()
    second["version"] = 2
    newer = development.start_proposal(task, base, store, second, **args)
    assert len(newer["versions"]) == 2 and digest in newer["versions"]
    # A changed request invalidates every version before a stage can execute.
    gap_path = task / "capability-gap.json"
    gap_path.write_bytes(gap_path.read_bytes() + b" ")
    with pytest.raises(development.DevelopmentError, match="development_request_changed"):
        development.test(task, base, store, digest)


def test_new_fixed_recipe_retains_exact_title_and_text():
    candidate = _manifest()
    snapshot = skills._fixture(candidate)
    steps = skills.build_steps(candidate, snapshot)
    assert steps[0]["data"]["rows"] == snapshot["observed"]["table"]["rows"]
    assert steps[0]["data"]["sum_columns"] == []
    assert steps[1]["title"] == "表格原文转存报告"
    assert "未重新计算" in steps[1]["paragraphs"][-1]


def test_activation_counter_survives_retries_and_crash_claim(short_tmp, monkeypatch):
    tmp_path = short_tmp
    base, store = tmp_path / "tasks", tmp_path / "capabilities"
    base.mkdir()
    task = _gap(base, "请把 Word 唯一表格原样转成 Excel，不转换数字，不重新计算合计；然后生成 Word 报告，标题为“表格原文转存报告”。")
    monkeypatch.setattr(development, "_source_reference", lambda *a, **k: _source_ref())
    args = dict(effect="保留 Word 表格全部行列文本，生成 Excel 与注明未重算的 Word 报告",
                source_artifact=_source_ref(), proposer="Builder")
    digest = development.start_proposal(task, base, store, _manifest(), **args)["versions"][0]
    development.test(task, base, store, digest)
    development.review(task, base, store, digest, reviewer="Reviewer", note="Independent")
    monkeypatch.setattr(development, "verify_live", lambda *a, **k: {"passed": True})
    monkeypatch.setattr(skills, "_live_check", lambda *a, **k: {"passed": True})
    first = development.activate(task, base, store, digest, runs=1)
    assert first["remaining_runs"] == 1
    state_path = store / _manifest()["id"] / "active.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["remaining_runs"] = 0
    skills._record(state_path, state)
    assert development.activate(task, base, store, digest, runs=1)["remaining_runs"] == 0
    with pytest.raises(development.DevelopmentError, match="active_success_required"):
        development.activate(task, base, store, digest, runs=3)
    evidence = {"task_root": str(base / ("task-" + uuid.uuid4().hex)),
                "task_result_sha256": "a" * 64}
    monkeypatch.setattr(development, "_active_success", lambda *a: evidence)
    monkeypatch.setattr(development, "_check_active_success", lambda *a: evidence)
    expanded = development.activate(task, base, store, digest, runs=3)
    assert expanded["remaining_runs"] == 2
    expanded["remaining_runs"] = 1
    skills._record(state_path, expanded)
    assert development.activate(task, base, store, digest, runs=3)["remaining_runs"] == 1
    assert development.suspend(task, base, store, digest)["enabled"] is False
    with pytest.raises(development.DevelopmentError, match="activation_stale"):
        development.activate(task, base, store, digest, runs=3)

    later = _manifest()
    later["version"] = 2
    new_digest = development.start_proposal(task, base, store, later, **args)["proposals"][0]["digest"]
    development.test(task, base, store, new_digest)
    development.review(task, base, store, new_digest, reviewer="Reviewer", note="Independent")
    path = development._proposal_path(task, store, new_digest)
    workflow._write_new(path / "activation-1-intent.json", {"schema": 1,
                        "candidate_sha256": workflow._hash(later), "requested_ceiling": 1})
    with pytest.raises(development.DevelopmentError, match="activation_unknown"):
        development.activate(task, base, store, new_digest, runs=1)


def test_cancelled_trial_resumes_without_replaying_prefix(tmp_path, monkeypatch):
    # Synthetic supervised worker checks recovery mechanics. It is not live
    # certification, which also requires independent native application proof.
    with tempfile.TemporaryDirectory(prefix="adt-") as value:
        root = Path(value)
        base, store = root / "tasks", root / "skills"
        candidate = copy_manifest(draft=True)
        digest = skills.register_candidate(store, candidate)["sha256"]
        skills.verify_offline(store, candidate["id"], digest)
        skills.record_review(store, candidate["id"], digest, "Reviewer", "Contract review")
        task = base / ("task-" + uuid.uuid4().hex)
        calls = []

        def supervised(spec, step_root, should_stop):
            calls.append(step_root.name)
            result = fake_office(spec, step_root, should_stop)
            workflow._write_new(step_root / "worker-request.json", {"spec": spec})
            workflow._write_new(step_root / "worker-outcome.json", result)
            return result

        from agent_service import office_host, office_readiness
        monkeypatch.setattr(office_host, "execute_office_supervised", supervised)
        monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: None)
        original = skills.run_skill(store, candidate["id"], task, base,
                                    source_file=source(root), trial_digest=digest,
                                    pause_after_step="s1")
        assert original["state"] == "cancelled" and calls == ["s1"]
        assert not (Path(original["workflow_root"]) / "s2").exists()
        preview = skill_resume.preview_resume(task, base, store)
        assert preview["eligible"] and preview["completed"] == 1
        result = skill_resume.resume_task(task, base, expected_token=preview["token"],
                                          attempt_id=uuid.uuid4().hex, store=store)
        assert result["state"] == "succeeded"
        assert calls == ["s1"]
        # This test's monkeypatched worker is deliberately never submitted to
        # record_live; production certification requires native app evidence.
        assert skills._verify_success(task, base, digest)["state"] == "cancelled"
