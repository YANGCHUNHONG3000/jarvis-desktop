"""Offline task-result publication and partial research/Office visibility checks."""

from hashlib import sha256
import json
from pathlib import Path
import uuid

import pytest

from agent_service import assistant
from agent_service.contracts import Observation
from agent_service.research import run_research
from run_jarvis_assistant import _durable_task_result, _office_artifacts


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)

    def complete(self, **kwargs):
        return self.answers.pop(0)


def _unsupported():
    return {"kind": "unsupported", "reason": "Outside the bounded actions"}


def _task_root(tmp_path):
    root = tmp_path / ("task-" + uuid.uuid4().hex)
    root.mkdir()
    return root


def test_interrupted_serialization_never_publishes_partial_result(tmp_path, monkeypatch):
    task_root = _task_root(tmp_path)
    real_dump = assistant.json.dump

    def interrupted_dump(value, file, **kwargs):
        if Path(file.name).name == "result.pending.json":
            file.write('{"ok":')
            raise SystemExit("simulated process interruption")
        return real_dump(value, file, **kwargs)

    monkeypatch.setattr(assistant.json, "dump", interrupted_dump)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        assistant.run_task("Do unavailable work", tmp_path, task_root=task_root,
                           client=FakeClient(_unsupported()))
    assert not (task_root / "result.json").exists()
    assert (task_root / "result.pending.json").read_text(encoding="utf-8") == '{"ok":'
    assert _durable_task_result(task_root, tmp_path) == {}


def test_existing_pending_result_is_never_clobbered(tmp_path):
    task_root = _task_root(tmp_path)
    pending = task_root / "result.pending.json"
    pending.write_bytes(b"prior interrupted result")
    with pytest.raises(FileExistsError):
        assistant.run_task("Do unavailable work", tmp_path, task_root=task_root,
                           client=FakeClient(_unsupported()))
    assert pending.read_bytes() == b"prior interrupted result"
    assert not (task_root / "result.json").exists()


def test_successful_publication_is_valid_and_matches_result_event(tmp_path):
    task_root = _task_root(tmp_path)
    result = assistant.run_task("Do unavailable work", tmp_path, task_root=task_root,
                                client=FakeClient(_unsupported()))
    assert result["state"] == "unsupported"
    assert json.loads((task_root / "result.json").read_text(encoding="utf-8")) == result
    assert not (task_root / "result.pending.json").exists()
    events = [json.loads(line) for line in (task_root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "result" and events[-1]["result"] == result
    assert _durable_task_result(task_root, tmp_path) == result


def test_research_report_remains_visible_and_hash_checked_when_office_fails(tmp_path, monkeypatch):
    from agent_service import office_host, office_readiness

    url = "https://example.org/evidence"
    excerpt = "The source says three tasks were completed."

    class FakeBrowser:
        def read(self, requested_url, should_stop=lambda: False):
            assert requested_url == url
            return Observation(url, url, "Evidence", excerpt, "2026-10-17T00:00:00Z")

    def research(query, root, max_sources=1):
        return run_research(query, root, max_sources=max_sources,
                            search=lambda *_: [url], browser=FakeBrowser())

    def failed_office(spec, step_root, should_stop):
        return {"ok": False, "state": "unknown", "file_verified": False,
                "application_verified": False}

    monkeypatch.setattr(office_host, "execute_office_supervised", failed_office)
    monkeypatch.setattr(office_readiness, "preflight_office_window", lambda: None)
    plan = {"kind": "research", "query": "task evidence", "max_sources": 1,
            "office_formats": ["document"]}
    summary = {"title": "Summary", "claims": [{"text": "Three tasks were completed.",
               "sources": [1], "quotes": [{"source": 1, "quote": "three tasks were completed"}]}]}
    result = assistant.run_task("Research and write a document", tmp_path,
                                client=FakeClient(plan, summary), research=research)
    assert result["kind"] == "office" and result["state"] == "unknown"
    assert result["artifacts"] == []
    task_root = Path(result["root"])
    durable = _durable_task_result(task_root, tmp_path)
    outputs = _office_artifacts(durable, task_root)
    assert len(outputs) == 1 and outputs[0][0] == "资料归纳与来源 (.md)"
    report = outputs[0][1]
    assert sha256(report.read_bytes()).hexdigest() == result["research_sha256"]
    report.write_bytes(b"tampered")
    assert _office_artifacts(durable, task_root) == []
