"""Offline contract tests: no model transport or public network is used."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import pytest

from agent_service import assistant
from agent_service.contracts import Observation
from agent_service.research import run_research


URL = "https://example.org/research"
EXCERPT = "The sample has three observations. The measured interval is 12 seconds."


class FakeLedger:
    def snapshot(self):
        return {"remaining_micro_cny": 4_000_000}


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0
        self.token_limits = []
        self.systems = []
        self.users = []
        self.ledger = FakeLedger()

    def complete(self, *, system, user, max_tokens):
        assert max_tokens in {1024, 2048}
        self.calls += 1
        self.token_limits.append(max_tokens)
        self.systems.append(system)
        self.users.append(user)
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeBrowser:
    def read(self, url, should_stop=lambda: False):
        assert url == URL
        return Observation(url, url, "A *title*", EXCERPT, "2026-10-17T00:00:00Z")


def fake_research(topic, root, max_sources=2):
    return run_research(topic, root, max_sources=max_sources,
                        search=lambda query, count: [URL], browser=FakeBrowser())


def summary(text="A measured interval was reported.", quote="The measured interval is 12 seconds."):
    return {"title": "Summary", "claims": [{"text": text, "sources": [1],
                                               "quotes": [{"source": 1, "quote": quote}]}]}


def plan():
    return {"kind": "research", "query": "sample", "max_sources": 1}


def test_research_uses_own_observations_and_verified_report(tmp_path):
    client = FakeClient(plan(), summary())
    result = assistant.run_task("Summarize sample", tmp_path, client=client, research=fake_research)
    assert result["ok"] is True and result["state"] == "succeeded"
    assert result["kind"] == "research" and result["sources"] == [URL] and result["source_count"] == 1
    data = Path(result["artifact"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == result["readback_sha256"]
    assert "The sample has three observations\\." in Path(result["source_artifact"]).read_text(encoding="utf-8")
    assert result["budget"]["remaining_micro_cny"] == 4_000_000
    assert client.calls == 2
    assert client.token_limits == [2048, 2048]
    assert Path(result["root"], "result.json").exists()


def test_incorrect_quote_rejected_after_one_bounded_repair(tmp_path):
    client = FakeClient(plan(), summary(quote="nonexistent"), summary(quote="also absent"))
    result = assistant.run_task("Summarize sample", tmp_path, client=client, research=fake_research)
    assert result["ok"] is False and result["error"] == "citation_invalid"
    assert client.calls == 3
    assert list(Path(result["root"]).glob("artifacts/report-*.md")) == []
    assert "model_schema_rejected" in Path(result["root"], "events.jsonl").read_text(encoding="utf-8")


def test_untrusted_markdown_is_escaped(tmp_path):
    client = FakeClient(plan(), summary(text="<script>x</script> *bold* [link](evil)"))
    result = assistant.run_task("Summarize sample", tmp_path, client=client, research=fake_research)
    assert result["ok"] is True
    report = Path(result["artifact"]).read_text(encoding="utf-8")
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "\\*bold\\*" in report
    assert "\\[link\\]\\(evil\\)" in report


def test_published_quotes_have_25_unit_per_source_budget(tmp_path):
    quote = "One two three four five six seven eight nine ten eleven twelve thirteen fourteen"
    source_text = quote + ". More source detail."

    class LongBrowser:
        def read(self, url, should_stop=lambda: False):
            return Observation(url, url, "Long source", source_text, "2026-10-17T00:00:00Z")

    def research(topic, root, max_sources=2):
        return run_research(topic, root, max_sources=max_sources,
                            search=lambda query, count: [URL], browser=LongBrowser())

    claim = lambda text: {"text": text, "sources": [1], "quotes": [{"source": 1, "quote": quote}]}
    client = FakeClient(plan(), {"title": "Summary", "claims": [claim("First"), claim("Second")]})
    result = assistant.run_task("Summarize", tmp_path, client=client, research=research)
    assert result["ok"] is True
    report = Path(result["artifact"]).read_text(encoding="utf-8")
    assert report.count(quote) == 1
    assert "引文已核对，见来源[1]" in report
    assert "First [1]" in report and "Second [1]" in report
    assert "fourteen" in Path(result["source_artifact"]).read_text(encoding="utf-8")
    assert assistant._quote_units("中文汉字") == 4
    assert assistant._quote_units("alpha beta") == 2


def test_unknown_schema_never_invokes_tool(tmp_path):
    invoked = []
    client = FakeClient({"kind": "shell", "command": "echo bad"},
                        {"kind": "notepad", "text": "ok", "path": "C:/override"})
    result = assistant.run_task("Run a command", tmp_path, client=client,
                                research=lambda *a, **k: invoked.append(True))
    assert result["ok"] is False and result["state"] == "failed"
    assert not invoked and client.calls == 2


def test_prompt_examples_are_valid_json_and_unsupported_stays_unsupported(tmp_path):
    client = FakeClient({"kind": "unsupported", "reason": "No authorized capability"})
    result = assistant.run_task("Delete files and run shell", tmp_path, client=client)
    assert result["state"] == "unsupported" and result["ok"] is False
    plan_lines = client.systems[0].splitlines()
    examples = [json.loads(line.split(": ", 1)[1]) for line in plan_lines
                if line.startswith(("Research: ", "Notepad: ", "Unsupported: "))]
    assert [item["kind"] for item in examples] == ["research", "notepad", "unsupported"]
    assert "{kind:" not in client.systems[0]

    client = FakeClient(plan(), summary())
    result = assistant.run_task("Summarize", tmp_path, client=client, research=fake_research)
    assert result["ok"] is True
    synth_line = next(line for line in client.systems[1].splitlines() if line.startswith("Example: "))
    assert json.loads(synth_line.split(": ", 1)[1])["claims"][0]["sources"] == [1]


def test_notepad_is_prepared_only(tmp_path):
    client = FakeClient({"kind": "notepad", "text": "记下来"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=client)
    assert result["ok"] is True and result["state"] == "prepared"
    assert result["kind"] == "notepad" and result["text"] == "记下来"
    assert "artifact" not in result


def test_notepad_control_characters_fail_schema_without_desktop_action(tmp_path):
    client = FakeClient({"kind": "notepad", "text": "unsafe\x1bshortcut"},
                        {"kind": "unsupported", "reason": "Cannot prepare this text"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=client)
    assert result["state"] == "unsupported" and client.calls == 2

    client = FakeClient({"kind": "notepad", "text": "line 1\r\nline 2"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=client)
    assert result["state"] == "prepared" and result["text"] == "line 1\nline 2"


def test_budget_rejection_has_safe_code_and_no_replay(tmp_path):
    class BudgetExceeded(RuntimeError):
        pass

    client = FakeClient(BudgetExceeded("secret in transport"))
    result = assistant.run_task("Summarize", tmp_path, client=client)
    assert result["error"] == "budget_rejected" and client.calls == 1
    assert "secret" not in Path(result["root"], "events.jsonl").read_text(encoding="utf-8")


def test_only_invalid_model_json_receives_one_format_repair(tmp_path):
    from agent_service.model import ModelError

    client = FakeClient(ModelError("model_json_invalid"), {"kind": "notepad", "text": "ready"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=client)
    assert result["state"] == "prepared" and client.calls == 2
    assert client.token_limits == [2048, 2048]

    failed = FakeClient(ModelError("model_json_invalid"), ModelError("model_json_invalid"))
    result = assistant.run_task("Write in Notepad", tmp_path, client=failed)
    assert result["error"] == "model_json_invalid" and failed.calls == 2

    transport = FakeClient(ModelError("model_transport_failed"), {"kind": "notepad", "text": "unexpected"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=transport)
    assert result["error"] == "model_transport_failed" and transport.calls == 1


def test_model_event_records_only_safe_reservation_id(tmp_path):
    class MetadataClient(FakeClient):
        def complete(self, **kwargs):
            self.last_call = {"reservation_id": "a" * 32, "model": "deepseek-flash",
                              "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                              "secret": "never log this"}
            return super().complete(**kwargs)

    client = MetadataClient({"kind": "notepad", "text": "ready"})
    result = assistant.run_task("Write in Notepad", tmp_path, client=client)
    events = Path(result["root"], "events.jsonl").read_text(encoding="utf-8")
    assert '"reservation_id": "' + "a" * 32 + '"' in events
    assert "never log this" not in events


def test_search_failure_replans_once(tmp_path):
    attempts = []

    def research(topic, root, max_sources=2):
        attempts.append(topic)
        if len(attempts) == 1:
            return {"ok": False, "reason": "browser.read failed (source_rejected)",
                    "sources": [URL + "?token=do-not-log"]}
        return fake_research(topic, root, max_sources=max_sources)

    client = FakeClient(plan(), {"kind": "research", "query": "better sample", "max_sources": 1}, summary())
    result = assistant.run_task("Summarize", tmp_path, client=client, research=research)
    assert result["ok"] is True and attempts == ["sample", "better sample"]
    assert client.calls == 3
    replan = json.loads(client.users[1])
    assert replan["prior_query"] == "sample"
    assert replan["failure_code"] == "source_rejected"
    assert replan["selected_urls"] == ["https://example.org/"]
    assert "do-not-log" not in Path(result["root"], "events.jsonl").read_text(encoding="utf-8")


def test_non_retryable_research_failure_stops_without_paid_replan(tmp_path):
    client = FakeClient(plan(), {"kind": "research", "query": "unused", "max_sources": 1})

    def broken_research(*args, **kwargs):
        raise OSError("disk path with sensitive detail")

    result = assistant.run_task("Summarize", tmp_path, client=client, research=broken_research)
    assert result["ok"] is False and result["error"] == "filesystem_error"
    assert client.calls == 1
    events = Path(result["root"], "events.jsonl").read_text(encoding="utf-8")
    assert "sensitive detail" not in events and "search_replan_requested" not in events


def test_preallocated_task_root_is_validated_and_reused(tmp_path):
    task_root = tmp_path / ("task-" + "a" * 32)
    task_root.mkdir()
    client = FakeClient({"kind": "notepad", "text": "ready"})
    result = assistant.run_task("Prepare", tmp_path, client=client, task_root=task_root)
    assert result["root"] == str(task_root.resolve()) and result["state"] == "prepared"
    with pytest.raises(assistant.AssistantError, match="task_root_in_use"):
        assistant.run_task("Prepare", tmp_path, client=client, task_root=task_root)
    with pytest.raises(assistant.AssistantError, match="task_root_invalid"):
        assistant.run_task("Prepare", tmp_path, client=client, task_root=tmp_path / "outside")


def test_concurrent_same_task_root_allows_only_one_model_call(tmp_path):
    task_root = tmp_path / ("task-" + "b" * 32)
    task_root.mkdir()
    barrier = threading.Barrier(2)
    client = FakeClient({"kind": "notepad", "text": "ready"})

    def attempt():
        barrier.wait(timeout=5)
        try:
            return assistant.run_task("Prepare", tmp_path, client=client, task_root=task_root)
        except assistant.AssistantError as exc:
            return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(result.get("state", result.get("error")) for result in results) == ["prepared", "task_root_in_use"]
    assert client.calls == 1


def test_research_does_not_start_without_workflow_time(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant, "WORKFLOW_SECONDS", assistant.TASK_SECONDS)
    invoked = []
    client = FakeClient(plan())
    result = assistant.run_task("Summarize", tmp_path, client=client,
                                research=lambda *args, **kwargs: invoked.append(True))
    assert result["error"] == "task_deadline" and client.calls == 1
    assert invoked == []


def test_report_name_conflict_preserves_existing_bytes(tmp_path, monkeypatch):
    real_report = assistant._report
    collision = b"keep me"

    def colliding_report(*args):
        task_root = next(tmp_path.glob("task-*"))
        artifact_dir = task_root / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        # The report UUID is controlled only to exercise exclusive-create failure.
        (artifact_dir / ("report-" + "a" * 32 + ".md")).write_bytes(collision)
        return real_report(*args)

    class UUID:
        def __init__(self, value):
            self.hex = value

    uuids = iter([UUID("b" * 32), UUID("c" * 32), UUID("d" * 32), UUID("a" * 32)])
    monkeypatch.setattr(assistant.uuid, "uuid4", lambda: next(uuids))
    monkeypatch.setattr(assistant, "_report", colliding_report)
    client = FakeClient(plan(), summary())
    result = assistant.run_task("Summarize", tmp_path, client=client, research=fake_research)
    assert result["ok"] is False and result["error"] == "filesystem_error"
    assert Path(result["root"], "artifacts", "report-" + "a" * 32 + ".md").read_bytes() == collision


def test_cli_stdout_is_one_final_json(tmp_path, monkeypatch, capsys):
    import run_jarvis_task

    instruction = tmp_path / "instruction.txt"
    instruction.write_text("Write in Notepad", encoding="utf-8")

    def offline_run(text, root, task_root=None):
        print("internal diagnostic that must not reach stdout")
        return assistant.run_task(text, root, client=FakeClient({"kind": "notepad", "text": "ok"}),
                                  task_root=task_root)

    monkeypatch.setattr(run_jarvis_task, "run_task", offline_run)
    assert run_jarvis_task.main(["--text-file", str(instruction), "--root", str(tmp_path / "runs")]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert json.loads(output)["state"] == "prepared"
