"""Budget-free planner tests; no native Office effects or real model calls."""
import json
from pathlib import Path

import pytest

from agent_service import assistant, local_skills, skill_matching


class Client:
    ledger = None

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(kwargs)
        return self.answers.pop(0)


DIGEST = "a" * 64
SELECT = {"kind": "local_skill", "id": "table-office-copy", "sha256": DIGEST}
NO = {"kind": "unsupported", "reason": "无法满足全部要求"}
YES_REVIEW = {"approved": True, "reason": "范围一致"}
NO_REVIEW = {"approved": False, "reason": "额外作用"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "private-source.csv"
    source.write_text("项目,数量\n资料,3\n", encoding="utf-8")
    candidate = {"id": SELECT["id"], "sha256": DIGEST, "status": "available",
                 "recipe": "table_copy", "name": "表格副本", "effects": [{"output": ".xlsx"}]}
    discovery = {"schema": 1, "candidates": [candidate], "hints": {"matched_ids": []}}
    monkeypatch.setattr(skill_matching, "discover_candidates", lambda *a, **kw: discovery)
    plans = []
    calls = []

    def plan(store, skill_id, digest, snapshot):
        plans.append(snapshot)
        assert skill_id == SELECT["id"] and digest == DIGEST
        return [{"id": "s1", "action": "spreadsheet", "data": {
            "kind": "spreadsheet", "title": "表格副本", **snapshot["observed"], "sum_columns": []}}]

    def execute(store, skill_id, task_root, base, *, snapshot, expected_digest, publish, deadline):
        assert expected_digest == DIGEST
        assert json.loads((task_root / "sources" / "source.json").read_text(encoding="utf-8")) == snapshot
        assert not (task_root / "result.json").exists()
        calls.append(str(task_root))
        return publish({"ok": True, "state": "succeeded", "kind": "office", "skill": {"id": skill_id}})

    monkeypatch.setattr(skill_matching, "candidate_plan", plan)
    monkeypatch.setattr(local_skills, "run_prepared_skill", execute, raising=False)
    return source, tmp_path / "tasks", discovery, calls, plans


def run(setup, client, instruction="仅生成 Excel，保留所有行列，不求和"):
    return assistant.run_task(instruction, setup[1], source_file=setup[0], client=client)


def test_selected_skill_independent_review_shared_task_and_source(setup):
    client = Client(SELECT, YES_REVIEW)
    result = run(setup, client)
    assert result["ok"] and result["source"]["kind"] == "table"
    assert setup[3] == [result["root"]] and len(setup[4]) == 1
    assert len(client.requests) == 2
    assert str(setup[0]) not in json.dumps(client.requests, ensure_ascii=False)
    assert "local_skill" in client.requests[0]["system"]
    review = json.loads(client.requests[1]["user"])
    assert review["proposed_steps"][0]["data"]["rows"] == [["资料", 3]]
    coverage = review['source_table_coverage']
    assert coverage['all_source_retained'] is True and coverage['source_row_count'] == 1
    assert coverage['source_headers'] == ['项目', '数量'] and coverage['plan_step'] == 's1'
    assert 'rows' not in coverage and coverage['layout_preserved'] is False
    assert json.loads((Path(result["root"]) / "result.json").read_text(encoding="utf-8")) == result


def test_exact_generated_plan_reuses_skill_before_effects(setup):
    generated = {"kind": "source_office", "steps": [{"id": "s1", "action": "spreadsheet_from_source",
        "source_id": "selected", "title": "表格副本", "columns": [0, 1], "sum_columns": []}]}
    result = run(setup, Client(generated, YES_REVIEW))
    assert result["ok"] and result["skill"]["id"] == SELECT["id"] and len(setup[3]) == 1
    assert "skill_exact_plan_matched" in (Path(result["root"]) / "events.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize('table', [
    {'headers': ['changed', 'two'], 'rows': [[1, 2], [3, 4]]},
    {'headers': ['one', 'two'], 'rows': [[3, 4], [1, 2]]},
    {'headers': ['one', 'two'], 'rows': [[1, 2]]},
    {'headers': ['one', 'two'], 'rows': [[True, 2], [3, 4]]},
])
def test_literal_table_coverage_rejects_omitted_reordered_or_changed_data(table):
    snapshot = {'kind': 'table', 'sha256': 'a' * 64,
                'observed': {'headers': ['one', 'two'], 'rows': [[1, 2], [3, 4]]}}
    steps = [{'id': 's1', 'action': 'spreadsheet', 'data': table}]
    with pytest.raises(assistant.AssistantError, match='skill_source_projection_mismatch'):
        assistant._literal_table_review_coverage(snapshot, steps)


def test_false_table_coverage_never_reaches_paid_review_or_executor(setup, monkeypatch):
    monkeypatch.setattr(skill_matching, 'candidate_plan', lambda *a: [
        {'id': 's1', 'action': 'spreadsheet', 'data': {'headers': ['项目', '数量'], 'rows': []}}])
    client = Client(SELECT, YES_REVIEW)
    result = run(setup, client)
    assert not result['ok'] and len(client.requests) == 1 and not setup[3]
    assert result['error'] == 'skill_source_projection_mismatch'


def test_different_title_never_substitutes_fixed_skill(setup, monkeypatch):
    from agent_service import office_workflow
    generic = []
    def execute(steps, *args, **kwargs):
        generic.append(steps)
        return {"ok": False, "state": "unknown", "receipts": {}, "completed": []}
    monkeypatch.setattr(office_workflow, "run_office_workflow", execute)
    generated = {"kind": "source_office", "steps": [{"id": "s1", "action": "spreadsheet_from_source",
        "source_id": "selected", "title": "我的精确标题", "columns": [0, 1], "sum_columns": []}]}
    client = Client(generated)
    run(setup, client)
    assert not setup[3] and generic[0][0]["data"]["title"] == "我的精确标题" and len(client.requests) == 1


def test_boolean_numeric_python_equality_is_not_exact_match(setup, monkeypatch):
    setup[0].write_text("项目,数量\n资料,1\n", encoding="utf-8")
    from agent_service import office_workflow
    monkeypatch.setattr(skill_matching, "candidate_plan", lambda *args: [{"id": "s1", "action": "spreadsheet",
        "data": {"kind": "spreadsheet", "title": "表格副本", "headers": ["项目", "数量"],
                 "rows": [["资料", True]], "sum_columns": []}}])
    generic = []
    monkeypatch.setattr(office_workflow, "run_office_workflow", lambda steps, *a, **kw:
        generic.append(steps) or {"ok": False, "state": "unknown", "receipts": {}, "completed": []})
    client = Client({"kind": "source_office", "steps": [{"id": "s1", "action": "spreadsheet_from_source",
        "source_id": "selected", "title": "表格副本", "columns": [0, 1], "sum_columns": []}]})
    run(setup, client)
    assert generic and not setup[3] and len(client.requests) == 1


def test_false_unsupported_reconsiders_once_before_any_effect(setup):
    client = Client(NO, SELECT, YES_REVIEW)
    result = run(setup, client)
    assert result["ok"] and len(client.requests) == 3 and len(setup[3]) == 1
    events = (Path(result["root"]) / "events.jsonl").read_text(encoding="utf-8")
    assert "skill_recovery_requested" in events


def test_unsupported_after_one_retry_persists_development_request(setup):
    client = Client(NO, NO)
    result = run(setup, client)
    assert result["state"] == "unsupported" and len(client.requests) == 2
    assert setup[3] == []
    gap = json.loads((Path(result["root"]) / "capability-gap.json").read_text(encoding="utf-8"))
    assert gap["development_request"]["state"] == "queued"
    assert gap["development_request"]["automatic_code_execution"] is False
    assert gap["discovery"]["candidates"][0]["id"] == SELECT["id"]
    assert "private-source" not in json.dumps(gap)


def test_review_rejects_without_effect_and_falls_back_once(setup):
    client = Client(SELECT, NO_REVIEW, NO)
    result = run(setup, client)
    assert result["state"] == "unsupported" and not setup[3]
    assert len(client.requests) == 3
    assert "local_skill is forbidden" in client.requests[-1]["system"]


@pytest.mark.parametrize("change", [{"sha256": "b" * 64}, {"id": "other-skill"}, {"extra": "bad"}])
def test_unlisted_or_malformed_selection_never_executes(setup, change):
    choice = {**SELECT, **change}
    result = run(setup, Client(choice, choice))
    assert not result["ok"] and not setup[3] and not setup[4]


def test_disabled_candidates_do_not_force_replan_or_extra_model_call(setup):
    setup[2]["candidates"][0]["status"] = "disabled"
    client = Client(NO)
    result = run(setup, client)
    assert result["state"] == "unsupported" and len(client.requests) == 1 and not setup[3]


def test_failure_from_executor_is_not_replanned(setup, monkeypatch):
    def execute(*args, **kwargs):
        return kwargs["publish"]({"ok": False, "state": "unknown", "kind": "office",
                                  "error": "skill_effect_unknown"})
    monkeypatch.setattr(local_skills, "run_prepared_skill", execute)
    client = Client(SELECT, YES_REVIEW)
    result = run(setup, client)
    assert result["state"] == "unknown" and len(client.requests) == 2


def test_budget_refusal_during_review_never_executes(setup):
    class Refusal(Client):
        def complete(self, **kwargs):
            if self.requests:
                from agent_service.model import ModelError
                raise ModelError("budget_exceeded")
            return super().complete(**kwargs)
    result = run(setup, Refusal(SELECT))
    assert not result["ok"] and not setup[3]


def test_recipe_selection_needs_verified_source():
    with pytest.raises(assistant.AssistantError, match="skill_selection_unavailable"):
        assistant._plan(SELECT)


def test_gate_change_during_review_has_stable_failure_code(setup, monkeypatch):
    def changed(*args, **kwargs):
        raise local_skills.SkillError("skill_selection_stale")
    monkeypatch.setattr(local_skills, "run_prepared_skill", changed)
    result = run(setup, Client(SELECT, YES_REVIEW))
    assert result["error"] == "skill_selection_stale" and not result["ok"]


def test_post_publication_failure_reconciles_without_overwriting_result(setup, monkeypatch):
    reconciled = []
    def late_error(*args, **kwargs):
        kwargs["publish"]({"ok": True, "state": "succeeded", "kind": "office",
                           "skill": {"id": SELECT["id"]}})
        raise OSError("state update failed")
    monkeypatch.setattr(local_skills, "run_prepared_skill", late_error)
    monkeypatch.setattr(local_skills, "reconcile", lambda *args: reconciled.append(args) or {"active": DIGEST})
    client = Client(SELECT, YES_REVIEW)
    result = run(setup, client)
    assert result["ok"] and len(reconciled) == 1 and len(client.requests) == 2
    saved = json.loads((Path(result["root"]) / "result.json").read_text(encoding="utf-8"))
    assert saved == result
