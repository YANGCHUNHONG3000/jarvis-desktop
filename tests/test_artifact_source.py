"""Offline generated Office artifact intake with receipt and package evidence."""

from hashlib import sha256
import json
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET
import zipfile

import pytest

from agent_service import model, office
from agent_service.artifact_source import (ArtifactSourceError,
    prepare_artifact_source, prepare_artifact_task_root, require_external_source_file,
    snapshot_artifact_source)
from agent_service.assistant import run_task
from agent_service.office_resume import preview_resume, resume_task
from agent_service.office_workflow import run_office_workflow


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xml(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _word(path, spec):
    root = ET.Element(f"{{{W}}}document")
    body = ET.SubElement(root, f"{{{W}}}body")
    for text in [spec["title"], *spec["paragraphs"]]:
        p = ET.SubElement(body, f"{{{W}}}p")
        ET.SubElement(ET.SubElement(p, f"{{{W}}}r"), f"{{{W}}}t").text = text
    if "table" in spec:
        table = ET.SubElement(body, f"{{{W}}}tbl")
        for values in [spec["table"]["headers"], *spec["table"]["rows"]]:
            row = ET.SubElement(table, f"{{{W}}}tr")
            for value in values:
                p = ET.SubElement(ET.SubElement(row, f"{{{W}}}tc"), f"{{{W}}}p")
                ET.SubElement(ET.SubElement(p, f"{{{W}}}r"), f"{{{W}}}t").text = str(value)
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", _xml(root))


def _sheet(path, spec):
    workbook = ET.Element(f"{{{S}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{S}}}sheets")
    ET.SubElement(sheets, f"{{{S}}}sheet", {"name": "Data", "sheetId": "1", f"{{{R}}}id": "rId1"})
    rels = ET.Element("Relationships")
    ET.SubElement(rels, "Relationship", {"Id": "rId1", "Target": "worksheets/sheet1.xml"})
    sheet = ET.Element(f"{{{S}}}worksheet")
    data = ET.SubElement(sheet, f"{{{S}}}sheetData")

    def put(row, column, value, formula=None):
        address = f"{chr(65 + column)}{row}"
        element = ET.SubElement(data, f"{{{S}}}row", {"r": str(row)}) if not list(data) or list(data)[-1].get("r") != str(row) else list(data)[-1]
        cell = ET.SubElement(element, f"{{{S}}}c", {"r": address})
        if formula is not None:
            ET.SubElement(cell, f"{{{S}}}f").text = formula
            ET.SubElement(cell, f"{{{S}}}v").text = str(value)
        elif type(value) is str:
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, f"{{{S}}}is"), f"{{{S}}}t").text = value
        elif value is not None:
            ET.SubElement(cell, f"{{{S}}}v").text = str(value)

    put(1, 0, spec["title"])
    for column, value in enumerate(spec["headers"]):
        put(2, column, value)
    for row, values in enumerate(spec["rows"], 3):
        for column, value in enumerate(values):
            put(row, column, value)
    total_row = len(spec["rows"]) + 3
    if spec["sum_columns"]:
        label_col = office._total_label_col(spec)
        if label_col is not None:
            put(total_row, label_col, "合计")
        for column in spec["sum_columns"]:
            total = sum(row[column] for row in spec["rows"])
            col = chr(65 + column)
            put(total_row, column, total, f"SUM({col}3:{col}{total_row-1})")
    if len(spec["headers"]) > 1:
        merges = ET.SubElement(sheet, f"{{{S}}}mergeCells", {"count": "1"})
        ET.SubElement(merges, f"{{{S}}}mergeCell", {"ref": f"A1:{chr(64+len(spec['headers']))}1"})
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("xl/workbook.xml", _xml(workbook))
        package.writestr("xl/_rels/workbook.xml.rels", _xml(rels))
        package.writestr("xl/worksheets/sheet1.xml", _xml(sheet))


def fake(spec, step_root, stop):
    assert not stop()
    path = step_root / ("spreadsheet.xlsx" if spec["kind"] == "spreadsheet" else "document.docx")
    (_sheet if spec["kind"] == "spreadsheet" else _word)(path, spec)
    raw = path.read_bytes()
    internal = office.verify_office_file(path, spec)
    return {"ok": True, "state": "succeeded", "artifact": str(path),
            "sha256": sha256(raw).hexdigest(), "observed": office._public_observation(spec, internal),
            "app": {"progid": "fake-WPS", "name": "Fake Office", "version": "1"},
            "application_verified": True, "file_verified": True}


def owned(tmp_path, spec, *, second=None):
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True)
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    workflow = task / ("workflow-" + uuid.uuid4().hex)
    steps = [{"id": "s1", "action": spec["kind"], "data": spec}]
    if second is not None:
        steps.append({"id": "s2", "action": "document", "data": second})
    run = run_office_workflow(steps, workflow, execute=fake)
    assert run["state"] == "succeeded"
    artifacts = [{"step": key, "path": value["artifact"], "sha256": value["sha256"]}
                 for key, value in run["receipts"].items()]
    parent = {"ok": True, "state": "succeeded", "kind": "office", "root": str(task),
              "workflow_root": str(workflow), "completed": run["completed"],
              "artifacts": artifacts}
    (task / "result.json").write_text(json.dumps(parent), encoding="utf-8")
    return base, task, workflow, run


def sheet_spec():
    return {"kind": "spreadsheet", "title": "费用", "headers": ["物品", "金额"],
            "rows": [["纸", 3], ["笔", 5]], "sum_columns": [1]}


def doc_spec():
    return {"kind": "document", "title": "邮件本地副本", "paragraphs": ["第一段", "第二段"],
            "table": {"headers": ["项目", "值"], "rows": [["甲", "三"]]}}


def new_task(base):
    root = base / ("task-" + uuid.uuid4().hex)
    assert prepare_artifact_task_root(root, base) == root
    return root


def test_generated_xlsx_excludes_title_and_total_rows_but_records_derived_total(tmp_path):
    base, task, _, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    assert prepare_artifact_source(Path(receipt["artifact"]), receipt["sha256"], base) == ref
    assert set(ref) == {"version", "task_root", "workflow_root", "step_id", "artifact",
                        "sha256", "receipt_sha256", "token"}
    target = new_task(base)
    source = snapshot_artifact_source(ref, target, base)
    assert source["kind"] == "table"
    assert source["observed"] == {"headers": ["物品", "金额"], "rows": [["纸", 3], ["笔", 5]]}
    assert source["coverage"]["derived_totals"] == {"1": 8}
    assert source["coverage"]["totals_row_excluded"] is True
    saved = json.loads((target / "sources" / "source.json").read_text(encoding="utf-8"))
    assert saved == source and "artifact" not in saved
    provenance = json.loads((target / "sources" / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["artifact"] == receipt["artifact"]


def test_generated_doc_preserves_observed_paragraphs_and_table_text(tmp_path):
    base, _, _, run = owned(tmp_path, doc_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    source = snapshot_artifact_source(ref, new_task(base), base)
    assert source["kind"] == "document"
    assert source["observed"] == {"paragraphs": ["第一段", "第二段"],
                                  "table": {"headers": ["项目", "值"], "rows": [["甲", "三"]]}}
    assert source["coverage"]["layout_preserved"] is False
    assert source["coverage"]["source_title"] == "邮件本地副本"
    assert source["coverage"]["title_policy"] == "replaced_by_next_document_title"


def test_changed_forged_and_outside_artifacts_reject_before_copy(tmp_path):
    base, _, _, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    target = new_task(base)
    with pytest.raises(ArtifactSourceError):
        snapshot_artifact_source({**ref, "receipt_sha256": "0" * 64}, target, base)
    assert not (target / "sources").exists()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(Path(receipt["artifact"]).read_bytes())
    with pytest.raises(ArtifactSourceError):
        prepare_artifact_source(outside, receipt["sha256"], base)
    Path(receipt["artifact"]).write_bytes(b"changed")
    with pytest.raises(ArtifactSourceError):
        snapshot_artifact_source(ref, target, base)
    assert not (target / "sources").exists()


def test_linked_artifact_and_new_task_ancestor_reject(tmp_path):
    base, task, workflow, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    target = Path(receipt["artifact"])
    other = tmp_path / "other.xlsx"
    other.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(other)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ArtifactSourceError):
        prepare_artifact_source(target, receipt["sha256"], base)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(base, target_is_directory=True)
    except OSError:
        pytest.skip("Directory links unavailable")
    new_root = alias / ("task-" + uuid.uuid4().hex)
    with pytest.raises(ArtifactSourceError):
        prepare_artifact_task_root(new_root, alias)
    assert not (base / new_root.name).exists()


def test_invalid_ref_stops_before_cloud_client_construction(tmp_path, monkeypatch):
    base, _, _, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    monkeypatch.setattr(model, "CloudClient", lambda **kwargs: pytest.fail("paid model constructed"))
    outcome = run_task("整理数据", base, source_artifact={**ref, "token": "0" * 64})
    assert not outcome["ok"] and outcome["state"] == "failed"
    assert outcome["error"] == "artifact_source_reference_stale"


def test_valid_artifact_source_reaches_model_with_path_free_observations(tmp_path):
    base, _, _, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)

    class Client:
        ledger = None

        def __init__(self):
            self.requests = []

        def complete(self, **request):
            self.requests.append(request)
            return {"kind": "unsupported", "reason": "仅验证来源交接"}

    client = Client()
    result = run_task("读取选中的办公成果", base, client=client,
                      source_artifact=ref, task_root=new_task(base))
    assert result["state"] == "unsupported"
    assert result["source_provenance"] == {"kind": "verified_generated_office_artifact", **ref}
    selected = json.loads(client.requests[0]["user"])["selected_source"]
    assert selected["observed"]["rows"] == [["纸", 3], ["笔", 5]]
    assert selected["coverage"]["derived_totals"] == {"1": 8}
    assert ref["artifact"] not in client.requests[0]["user"]
    assert "source_provenance" not in client.requests[0]["user"]


def test_receipt_mutation_after_selection_blocks_new_snapshot(tmp_path):
    base, _, workflow, run = owned(tmp_path, doc_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    path = workflow / "s1" / "receipt.json"
    path.write_bytes(path.read_bytes() + b" ")
    target = new_task(base)
    with pytest.raises(ArtifactSourceError):
        snapshot_artifact_source(ref, target, base)
    assert not (target / "sources").exists()


@pytest.mark.parametrize("spec,change", [
    (sheet_spec(), lambda receipt: receipt["observed"]["totals"].update({"1": 99})),
    (doc_spec(), lambda receipt: receipt["observed"]["table"]["rows"][0].__setitem__(1, "伪")),
])
def test_generated_package_must_match_observed_receipt_values(tmp_path, spec, change):
    base, _, workflow, run = owned(tmp_path, spec)
    receipt_path = workflow / "s1" / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    change(receipt)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    with pytest.raises(ArtifactSourceError, match="artifact_source_receipt_mismatch"):
        snapshot_artifact_source(ref, new_task(base), base)


def test_source_file_and_artifact_reference_are_mutually_exclusive(tmp_path):
    base, _, _, run = owned(tmp_path, sheet_spec())
    receipt = run["receipts"]["s1"]
    ref = prepare_artifact_source(receipt["artifact"], receipt["sha256"], base)
    with pytest.raises(ValueError, match="source_conflict"):
        run_task("整理资料", base, source_file=Path(receipt["artifact"]), source_artifact=ref)


def test_managed_output_cannot_bypass_verified_route_as_ordinary_file(tmp_path, monkeypatch):
    base, _, _, run = owned(tmp_path, doc_spec())
    receipt = run["receipts"]["s1"]
    monkeypatch.setattr(model, "CloudClient", lambda **kwargs: pytest.fail("paid model constructed"))
    outcome = run_task("整理资料", base, source_file=Path(receipt["artifact"]))
    assert outcome["state"] == "failed"
    assert outcome["error"] == "source_generated_requires_reference"
    with pytest.raises(ArtifactSourceError, match="source_generated_requires_reference"):
        require_external_source_file(receipt["artifact"], tmp_path / "another-task-base")


def test_resumed_artifact_requires_trusted_continuation(tmp_path):
    first = {"kind": "document", "title": "起始", "paragraphs": ["已完成"]}
    second = {"kind": "document", "title": "后续", "paragraphs": ["续跑内容"]}
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True)
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    workflow = task / ("workflow-" + uuid.uuid4().hex)
    steps = [{"id": "s1", "action": "document", "data": first},
             {"id": "s2", "action": "document", "data": second}]

    def stop_after_one(spec, step_root, stop):
        outcome = fake(spec, step_root, stop)
        (workflow / "cancel.flag").touch()
        return outcome

    run = run_office_workflow(steps, workflow, execute=stop_after_one)
    assert run["state"] == "cancelled"
    first_receipt = run["receipts"]["s1"]
    parent = {"ok": False, "state": "cancelled", "kind": "office", "root": str(task),
              "workflow_root": str(workflow), "completed": ["s1"],
              "artifacts": [{"step": "s1", "path": first_receipt["artifact"],
                             "sha256": first_receipt["sha256"]}]}
    (task / "result.json").write_text(json.dumps(parent), encoding="utf-8")
    token = preview_resume(task, base)["token"]
    outcome = resume_task(task, base, token, uuid.uuid4().hex, execute=fake)
    assert outcome["state"] == "succeeded"
    second_receipt = json.loads((workflow / "s2" / "receipt.json").read_text(encoding="utf-8"))
    ref = prepare_artifact_source(second_receipt["artifact"], second_receipt["sha256"], base)
    source = snapshot_artifact_source(ref, new_task(base), base)
    assert source["observed"]["paragraphs"] == ["续跑内容"]
    assert source["coverage"]["source_title"] == "后续"


def test_skill_resumed_output_requires_skill_specific_continuation(tmp_path, monkeypatch):
    from agent_service import skill_resume
    first = {"kind": "document", "title": "起始", "paragraphs": ["已完成"]}
    second = {"kind": "document", "title": "后续", "paragraphs": ["技能续跑内容"]}
    base = tmp_path / "data" / "assistant_tasks"
    base.mkdir(parents=True)
    task = base / ("task-" + uuid.uuid4().hex)
    task.mkdir()
    workflow = task / ("workflow-" + uuid.uuid4().hex)
    steps = [{"id": "s1", "action": "document", "data": first},
             {"id": "s2", "action": "document", "data": second}]

    def stop_after_one(spec, step_root, stop):
        outcome = fake(spec, step_root, stop)
        (workflow / "cancel.flag").touch()
        return outcome

    partial = run_office_workflow(steps, workflow, execute=stop_after_one)
    receipt = partial["receipts"]["s1"]
    (task / "result.json").write_text(json.dumps({
        "ok": False, "state": "cancelled", "kind": "office", "root": str(task),
        "workflow_root": str(workflow), "completed": ["s1"],
        "artifacts": [{"step": "s1", "path": receipt["artifact"], "sha256": receipt["sha256"]}],
        "skill": {"id": "document-office-copy", "sha256": "a" * 64,
                  "implementation_sha256": "b" * 64, "mode": "active"}}), encoding="utf-8")
    (workflow / "cancel.flag").unlink()
    finished = run_office_workflow(steps, workflow, execute=fake, resume=True)
    second_receipt = finished["receipts"]["s2"]
    monkeypatch.setattr(skill_resume, "latest_continuation", lambda *_args: None)
    with pytest.raises(ArtifactSourceError, match="artifact_source_continuation_unverified"):
        prepare_artifact_source(second_receipt["artifact"], second_receipt["sha256"], base)
    monkeypatch.setattr(skill_resume, "latest_continuation", lambda *_args: {
        "kind": "skill_resume", "state": "succeeded", "attempt_id": "c" * 32,
        "artifacts": [{"step": "s1", "path": receipt["artifact"], "sha256": receipt["sha256"]},
                      {"step": "s2", "path": second_receipt["artifact"],
                       "sha256": second_receipt["sha256"]}]})
    ref = prepare_artifact_source(second_receipt["artifact"], second_receipt["sha256"], base)
    source = snapshot_artifact_source(ref, new_task(base), base)
    assert source["observed"]["paragraphs"] == ["技能续跑内容"]
