"""GUI handoff checks for artifacts produced by explicit task continuation."""

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from test_history_resume_gui import (
    _Process, _Thread, _Widget, _button, _pump, desktop,
)
import run_jarvis_assistant as launcher


def _make_batch(task_root):
    workflow = task_root / ("workflow-" + "b" * 32)
    rows = []
    for step, name, label, payload in (
        ("s1", "document.docx", "s1 · 文档 (.docx)", b"first office"),
        ("s2", "draft.eml", "s2 · 未发送邮件草稿", b"draft only"),
        ("s3", "spreadsheet.xlsx", "s3 · 表格 (.xlsx)", b"last office"),
    ):
        folder = workflow / step
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(payload)
        rows.append({"label": label, "path": str(path),
                     "sha256": hashlib.sha256(payload).hexdigest(), "step": step})
    return rows


def _reader_batch(rows):
    return [{"step": row["step"], "path": row["path"], "sha256": row["sha256"]}
            for row in rows]


def _run_resume_success(root, task_root, state, tree, rows, kind):
    state["preview_kind"] = "skill_resume" if kind == "skill_resume" else None
    state["history_artifacts"] = rows
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    _button("继续剩余技能步骤" if kind == "skill_resume" else "继续剩余办公步骤").invoke()
    _Thread.run("jarvis-resume")
    process = _Process.created[-1]
    attempt_id = process.command[process.command.index("--attempt-id") + 1]
    state["continuation"] = {"state": "succeeded", "attempt_id": attempt_id,
                             **({"kind": "skill_resume"} if kind == "skill_resume" else {})}
    state["outcome"] = {"ok": True, "state": "succeeded", "task_root": str(task_root),
                         "artifacts": _reader_batch(rows)}
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    return attempt_id


@pytest.mark.parametrize("kind", ["office_resume", "skill_resume"])
def test_successful_resume_stages_complete_batch_and_verifies_last_office_source(
        desktop, monkeypatch, kind):
    root, task_root, state, tree, original = desktop
    rows = _make_batch(task_root)
    calls = []
    from agent_service import artifact_source

    def prepare(path, digest, base):
        calls.append((Path(path), digest, Path(base)))
        return {"artifact": str(path), "sha256": digest, "managed": True}

    monkeypatch.setattr(artifact_source, "prepare_artifact_source", prepare)
    _run_resume_success(root, task_root, state, tree, rows, kind)

    pickers = [w for w in _Widget.all if w.options.get("values") == tuple(r["label"] for r in rows)]
    assert len(pickers) == 2  # history picker plus the task output picker
    picker = next(w for w in pickers if w.options["textvariable"].get() == rows[-1]["label"])
    assert picker.options["values"] == tuple(r["label"] for r in rows)
    assert (task_root / "result.json").read_bytes() == original
    assert len(_Process.created) == 1  # continuation only; no new task/model process
    _Thread.run("jarvis-artifact-source-select")
    _pump(root)
    assert calls == [(Path(rows[-1]["path"]), rows[-1]["sha256"], task_root.parents[0])]
    assert all(Path(row["path"]).read_bytes() for row in rows)


@pytest.mark.parametrize("failure", ["unknown", "partial", "attempt", "skill_kind", "stopped", "timed_out"])
def test_unverified_resume_never_stages_next_source(desktop, monkeypatch, failure):
    root, task_root, state, tree, original = desktop
    rows = _make_batch(task_root)
    state["history_artifacts"] = rows
    if failure == "skill_kind":
        state["preview_kind"] = "skill_resume"
    tree.select(task_root.name)
    _Thread.run("jarvis-history-inspect")
    _pump(root)
    skill = failure == "skill_kind"
    _button("继续剩余技能步骤" if skill else "继续剩余办公步骤").invoke()
    if failure == "timed_out":
        def timeout_once(self, timeout=None):
            if not getattr(self, "timed_once", False):
                self.timed_once = True
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.returncode = 0
            return "{}", None
        monkeypatch.setattr(_Process, "communicate", timeout_once)
    _Thread.run("jarvis-resume")
    process = _Process.created[-1]
    attempt_id = process.command[process.command.index("--attempt-id") + 1]
    state["continuation"] = {"state": "succeeded", "attempt_id": "f" * 32,
                             **({"kind": "skill_resume"} if skill else {})} if failure == "attempt" else {
                                 "state": "succeeded", "attempt_id": attempt_id,
                                 **({"kind": "skill_resume"} if skill else {})}
    state["outcome"] = {"ok": True, "state": "succeeded", "task_root": str(task_root),
                         "artifacts": _reader_batch(rows)}
    if failure == "unknown":
        state["outcome"].update(ok=False, state="unknown")
    elif failure == "partial":
        state["outcome"].update(ok=False, state="partial")
    elif failure == "skill_kind":
        original_reader = sys.modules["agent_service.skill_resume"].read_resume_result
        monkeypatch.setattr(sys.modules["agent_service.skill_resume"], "read_resume_result",
                            lambda *_args: {**original_reader(*_args), "kind": "office_resume"})
    elif failure == "stopped":
        _button("停止继续执行").invoke()
    elif failure == "timed_out":
        assert process.timed_once is True
    _pump(root)
    _Thread.run("jarvis-resume-reconcile")
    _pump(root)
    assert (task_root / "result.json").read_bytes() == original
    assert len(_Process.created) == 1
    assert not any(thread.name == "jarvis-artifact-source-select" for thread in _Thread.all)


@pytest.mark.parametrize("mutation", ["path", "hash", "order", "missing", "outside", "eml_only",
                                     "missing_sha", "invalid_sha", "extra_office", "needs_review"])
def test_handoff_batch_helper_rejects_inconsistent_or_unusable_records(tmp_path, mutation):
    task_root = tmp_path / ("task-" + "a" * 32)
    task_root.mkdir()
    rows = _make_batch(task_root)
    inspection = {"root": str(task_root), "artifacts": rows}
    outcome = {"task_root": str(task_root), "attempt_id": "c" * 32,
               "ok": True, "state": "succeeded", "artifacts": _reader_batch(rows)}
    if mutation == "path":
        outcome["artifacts"][0]["path"] += ".changed"
    elif mutation == "hash":
        outcome["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "order":
        outcome["artifacts"].reverse()
    elif mutation == "missing":
        Path(rows[1]["path"]).unlink()
    elif mutation == "outside":
        outside = tmp_path / "outside.docx"
        outside.write_bytes(b"outside")
        inspection["artifacts"][0]["path"] = str(outside)
        outcome["artifacts"][0]["path"] = str(outside)
        outcome["artifacts"][0]["sha256"] = hashlib.sha256(b"outside").hexdigest()
    elif mutation == "eml_only":
        eml = rows[1]
        inspection["artifacts"] = [eml]
        outcome["artifacts"] = _reader_batch([eml])
    elif mutation == "missing_sha":
        del inspection["artifacts"][0]["sha256"]
    elif mutation == "invalid_sha":
        inspection["artifacts"][0]["sha256"] = "g" * 64
    elif mutation == "extra_office":
        extra = dict(rows[0], label="earlier stale Office output")
        extra["step"] = "s0"
        inspection["artifacts"].insert(0, extra)
    elif mutation == "needs_review":
        inspection["state"] = "needs_review"
    result = launcher._resume_handoff_outputs(inspection, outcome, task_root, "c" * 32)
    if mutation == "eml_only":
        assert len(result) == 1 and result[0][1].suffix == ".eml"
    else:
        assert result == []


@pytest.mark.parametrize("suffix,name,size,expected", [
    (".docx", "document.docx", 2_000_001, True),
    (".eml", "draft.eml", 2_000_001, True),
    (".docx", "document.docx", 20_000_001, False),
    (".eml", "draft.eml", 45_000_001, False),
])
def test_handoff_size_limits_distinguish_office_and_eml(tmp_path, suffix, name, size, expected):
    task_root = tmp_path / ("task-" + "a" * 32)
    task_root.mkdir()
    step = "s1"
    folder = task_root / ("workflow-" + "b" * 32) / step
    folder.mkdir(parents=True)
    path = folder / name
    if size > 20_000_000:
        # Sparse allocation exercises the pre-read size rejection cheaply.
        with path.open("wb") as stream:
            stream.truncate(size)
        digest = "a" * 64
    else:
        payload = b"x" * size
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
    row = {"label": "output", "path": str(path), "sha256": digest, "step": step}
    inspection = {"root": str(task_root), "state": "succeeded", "artifacts": [row]}
    outcome = {"task_root": str(task_root), "attempt_id": "c" * 32, "ok": True,
               "state": "succeeded", "artifacts": _reader_batch([row])}
    outputs = launcher._resume_handoff_outputs(inspection, outcome, task_root, "c" * 32)
    assert bool(outputs) is expected


def test_independent_research_report_stays_outside_exact_resume_batch(tmp_path):
    task_root = tmp_path / ("task-" + "a" * 32)
    task_root.mkdir()
    rows = _make_batch(task_root)
    report = task_root / "research.md"
    report.write_text("independent research report", encoding="utf-8")
    research = {"label": "独立资料报告", "path": str(report),
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
    inspection = {"root": str(task_root), "state": "succeeded",
                  "artifacts": [*rows, research]}
    outcome = {"task_root": str(task_root), "attempt_id": "c" * 32, "ok": True,
               "state": "succeeded", "artifacts": _reader_batch(rows)}
    outputs = launcher._resume_handoff_outputs(inspection, outcome, task_root, "c" * 32)
    assert [path for _label, path, _digest in outputs] == [Path(row["path"]) for row in rows]
