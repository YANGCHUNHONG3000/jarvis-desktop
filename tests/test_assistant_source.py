"""Selected-source Office integration using fake model and native executor."""

from copy import deepcopy
from hashlib import sha256
import csv
import io
import json
from pathlib import Path

import pytest

from agent_service import assistant


def _csv_source(path):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(["项目", "数量", "备注"])
    writer.writerow(["咖啡", "3", '=HYPERLINK("https://example.invalid","点击")'])
    writer.writerow(["茶", "2", "中文说明"])
    data = ("\ufeff" + stream.getvalue()).encode("utf-8")
    path.write_bytes(data)
    return data


def _source_plan():
    return {"kind": "source_office", "steps": [
        {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
         "title": "选定数据", "columns": [0, 1, 2], "sum_columns": [1]},
        {"id": "s2", "action": "report_from_sheet", "from": "s1", "title": "数据汇总"},
    ]}


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.ledger = None

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        return self.answers.pop(0)


def _fake_office(monkeypatch):
    from agent_service import office_host
    specs = []

    def execute(spec, step_root, should_stop):
        assert not should_stop()
        specs.append(deepcopy(spec))
        extension = ".xlsx" if spec["kind"] == "spreadsheet" else ".docx"
        artifact = Path(step_root) / ("verified" + extension)
        content = json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        artifact.write_bytes(content)
        if spec["kind"] == "spreadsheet":
            observed = {"headers": spec["headers"], "rows": deepcopy(spec["rows"]),
                        "totals": {str(column): sum(row[column] for row in spec["rows"])
                                   for column in spec["sum_columns"]}}
        else:
            observed = {"text": "\n".join([spec["title"], *spec["paragraphs"]])}
            if "table" in spec:
                observed["table"] = {
                    "headers": list(spec["table"]["headers"]),
                    "rows": [["" if value is None else str(value) for value in row]
                             for row in spec["table"]["rows"]],
                }
        return {"ok": True, "state": "succeeded", "artifact": str(artifact),
                "sha256": sha256(content).hexdigest(), "observed": observed,
                "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
                "application_verified": True, "file_verified": True}

    monkeypatch.setattr(office_host, "execute_office_supervised", execute)
    return specs


def test_selected_csv_snapshot_is_exact_path_free_and_drives_office_values(tmp_path, monkeypatch):
    source_file = tmp_path / "private-original-name.csv"
    original = _csv_source(source_file)
    original_hash = sha256(original).hexdigest()
    task_root = tmp_path / "tasks" / ("task-" + "a" * 32)
    specs = _fake_office(monkeypatch)
    client = FakeClient(_source_plan())

    result = assistant.run_task("整理选定文件并生成表格和汇总文档", tmp_path / "tasks",
                                task_root=task_root, source_file=source_file, client=client)

    snapshot = task_root / "sources" / "source.csv"
    assert snapshot.read_bytes() == original
    assert sha256(snapshot.read_bytes()).hexdigest() == original_hash
    assert source_file.read_bytes() == original
    assert result["ok"] is True and result["kind"] == "office", result
    assert result["source"]["sha256"] == original_hash
    assert result["source"]["display_name"] == source_file.name
    assert "path" not in result["source"]
    assert str(source_file) not in json.dumps(result, ensure_ascii=False)

    spreadsheet, report = specs
    expected_rows = [["咖啡", 3, '=HYPERLINK("https://example.invalid","点击")'],
                     ["茶", 2, "中文说明"]]
    assert spreadsheet["rows"] == expected_rows
    assert spreadsheet["headers"] == ["项目", "数量", "备注"]
    assert spreadsheet["sum_columns"] == [1]
    assert report["table"] == {
        "headers": ["项目", "数量", "备注"],
        "rows": [["咖啡", 3, '=HYPERLINK("https://example.invalid","点击")'],
                 ["茶", 2, "中文说明"], ["合计", 5, ""]],
    }
    assert len(client.requests) == 1
    plan_user = json.loads(client.requests[0]["user"])
    assert plan_user["selected_source"]["observed"]["rows"] == expected_rows
    assert str(source_file) not in client.requests[0]["user"]


def test_source_snapshot_exists_before_cloud_client_is_constructed(tmp_path, monkeypatch):
    source_file = tmp_path / "source.csv"
    _csv_source(source_file)
    task_root = tmp_path / "tasks" / ("task-" + "b" * 32)
    specs = _fake_office(monkeypatch)
    import agent_service.model as model
    client = FakeClient(_source_plan())
    constructed = []

    def make_client(**kwargs):
        constructed.append(kwargs)
        snapshot = task_root / "sources" / "source.csv"
        assert snapshot.is_file()
        assert sha256(snapshot.read_bytes()).hexdigest() == sha256(source_file.read_bytes()).hexdigest()
        return client

    monkeypatch.setattr(model, "CloudClient", make_client)
    result = assistant.run_task("Create a table from my selected CSV", tmp_path / "tasks",
                                task_root=task_root, source_file=source_file)

    assert result["ok"] is True and len(specs) == 2, result
    assert len(constructed) == 1


def test_invalid_source_fails_before_any_model_call_or_copy(tmp_path):
    source_file = tmp_path / "not-supported.txt"
    source_file.write_text("not a supported source", encoding="utf-8")
    client = FakeClient(_source_plan())

    result = assistant.run_task("Use this file", tmp_path / "tasks",
                                source_file=source_file, client=client)

    assert result["ok"] is False and result["error"] == "source_path_invalid"
    assert client.requests == []
    assert list((tmp_path / "tasks").glob("task-*/sources")) == []


def test_invalid_source_plan_cannot_fall_through_to_generic_office(tmp_path, monkeypatch):
    source_file = tmp_path / "source.csv"
    _csv_source(source_file)
    calls = []

    def execute(spec, step_root, should_stop):
        calls.append(spec)
        pytest.fail("invalid selected-source plan must not execute Office")

    from agent_service import office_host
    monkeypatch.setattr(office_host, "execute_office_supervised", execute)
    invalid_source_plan = {"kind": "source_office", "steps": [
        {"id": "s1", "action": "spreadsheet_from_source", "source_id": "selected",
         "title": "bad", "columns": [99], "sum_columns": []},
    ]}
    generic_office_plan = {"kind": "office", "steps": [
        {"id": "s1", "action": "spreadsheet", "data": {
            "kind": "spreadsheet", "title": "invented", "headers": ["项目"],
            "rows": [["模型虚构"]], "sum_columns": []}},
    ]}
    client = FakeClient(invalid_source_plan, generic_office_plan)

    result = assistant.run_task("Create a table from my selected CSV", tmp_path / "tasks",
                                source_file=source_file, client=client)

    assert result["ok"] is False and result["state"] == "failed"
    assert result["error"] == "source_plan_required", result
    assert len(client.requests) == 2
    assert calls == []


def test_pre_cancel_returns_without_snapshot_or_model_call(tmp_path):
    source_file = tmp_path / "source.csv"
    original = _csv_source(source_file)
    task_root = tmp_path / "tasks" / ("task-" + "c" * 32)
    task_root.mkdir(parents=True)
    (task_root / "cancel.flag").touch()
    client = FakeClient(_source_plan())

    result = assistant.run_task("Use this selected CSV", tmp_path / "tasks", task_root=task_root,
                                source_file=source_file, client=client)

    assert result["ok"] is False, result
    assert client.requests == []
    assert not (task_root / "sources").exists()
    assert source_file.read_bytes() == original


def test_source_edits_are_audited_before_native_effect_and_preserve_original(tmp_path, monkeypatch):
    source_file = tmp_path / "source.csv"
    original = _csv_source(source_file)
    plan = _source_plan()
    plan["steps"][0]["edits"] = [{"row": 0, "column": 1, "op": "add", "value": 2}]
    plan["steps"][0]["append_rows"] = [["水", 4, "新增"]]
    specs = _fake_office(monkeypatch)
    result = assistant.run_task("咖啡数量增加2，追加一行水4备注新增", tmp_path / "tasks",
                                source_file=source_file, client=FakeClient(plan, {"approved": True, "reason": "明确请求"}))
    assert result["ok"] is True, result
    assert source_file.read_bytes() == original
    assert specs[0]["rows"][0][1] == 5
    assert specs[0]["rows"][-1] == ["水", 4, "新增"]
    assert result["source_edits"] == [
        {"step": "s1", "row": 0, "column": 1, "before": 3, "after": 5, "op": "add"},
        {"step": "s1", "row": 2, "op": "append", "values": ["水", 4, "新增"]}]
    events = [json.loads(line) for line in (Path(result["root"]) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    names = [event["event"] for event in events]
    assert names.index("source_edits_prepared") < names.index("office_requested")
    assert next(event for event in events if event["event"] == "source_edits_prepared")["edits"] == result["source_edits"]


@pytest.mark.parametrize("verdict", [{"approved": False, "reason": "未要求修改"},
                                      {"approved": "true", "reason": "错误类型"}, {}])
def test_source_edit_review_fails_closed_before_any_native_effect(tmp_path, monkeypatch, verdict):
    source_file = tmp_path / "source.csv"
    original = _csv_source(source_file)
    plan = _source_plan()
    plan["steps"][0]["edits"] = [{"row": 0, "column": 1, "op": "set", "value": 99}]
    specs = _fake_office(monkeypatch)
    client = FakeClient(plan, verdict)
    result = assistant.run_task("只复制数据", tmp_path / "tasks", source_file=source_file, client=client)
    assert not result["ok"]
    assert specs == []
    assert source_file.read_bytes() == original
    assert len(client.requests) == 2
    review = json.loads(client.requests[1]["user"])
    assert review["edited_source_rows"]["0"][1] == 3
    assert review["proposed_source_edits"][0]["after"] == 99
