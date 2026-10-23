"""Verified Office output as an explicit, immutable source for a later task."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from . import office, office_resume, office_source, office_workflow, task_history


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STEP = re.compile(r"s[1-8]\Z")
_TASK = re.compile(r"task-[0-9a-f]{32}\Z")
_FIELDS = {"version", "task_root", "workflow_root", "step_id", "artifact",
           "sha256", "receipt_sha256", "token"}
_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class ArtifactSourceError(ValueError):
    """Stable failure code that never includes source content or local paths."""

    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


def _digest(value):
    return type(value) is str and bool(_DIGEST.fullmatch(value))


def _verified(artifact_path, expected_sha256, base):
    if not _digest(expected_sha256):
        raise ArtifactSourceError("artifact_source_digest_invalid")
    if not isinstance(artifact_path, (str, Path)):
        raise ArtifactSourceError("artifact_source_path_invalid")
    artifact = Path(artifact_path)
    if (not artifact.is_absolute() or office_source._linked(artifact)
            or artifact.suffix.casefold() not in {".xlsx", ".docx"}):
        raise ArtifactSourceError("artifact_source_path_invalid")
    step_root = artifact.parent
    workflow_root = step_root.parent
    task_root = workflow_root.parent
    if (not _STEP.fullmatch(step_root.name)
            or artifact.name not in {"spreadsheet.xlsx", "document.docx"}
            or (artifact.name == "spreadsheet.xlsx") != (artifact.suffix.casefold() == ".xlsx")):
        raise ArtifactSourceError("artifact_source_path_invalid")
    try:
        snap = office_resume._snapshot(task_root, base)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise ArtifactSourceError("artifact_source_owner_invalid") from exc
    if snap["workflow_root"] != workflow_root or snap["root"] != task_root:
        raise ArtifactSourceError("artifact_source_owner_invalid")
    step_id = step_root.name
    receipt = snap["receipts"].get(step_id)
    if (receipt is None or receipt["artifact"] != str(artifact)
            or receipt["sha256"] != expected_sha256):
        raise ArtifactSourceError("artifact_source_receipt_mismatch")
    original_count = snap["original_completed"]
    index = next((i for i, step in enumerate(snap["steps"]) if step["id"] == step_id), None)
    if index is None:
        raise ArtifactSourceError("artifact_source_receipt_mismatch")
    if index >= original_count:
        try:
            parent = office_workflow._read_json(task_root / "result.json")
        except (ValueError, OSError) as exc:
            raise ArtifactSourceError("artifact_source_owner_invalid") from exc
        skill_owned = type(parent.get("skill")) is dict
        if skill_owned:
            from .skill_resume import latest_continuation
        else:
            latest_continuation = office_resume.latest_continuation
        prior = latest_continuation(task_root, base)
        if ((skill_owned and (type(prior) is not dict
                              or prior.get("kind") != "skill_resume"))
                or prior is None or not any(item["step"] == step_id and item["path"] == str(artifact)
                                         and item["sha256"] == expected_sha256
                                         for item in prior["artifacts"])):
            raise ArtifactSourceError("artifact_source_continuation_unverified")
    receipt_path = step_root / "receipt.json"
    if office_source._linked(receipt_path) or not receipt_path.is_file():
        raise ArtifactSourceError("artifact_source_receipt_mismatch")
    raw_receipt = receipt_path.read_bytes()
    if len(raw_receipt) > office_workflow._MAX_JSON:
        raise ArtifactSourceError("artifact_source_receipt_mismatch")
    ref = {"version": 1, "task_root": str(task_root),
           "workflow_root": str(workflow_root), "step_id": step_id,
           "artifact": str(artifact), "sha256": expected_sha256,
           "receipt_sha256": sha256(raw_receipt).hexdigest(), "token": snap["token"]}
    return ref, snap, receipt, index


def prepare_artifact_source(artifact_path, expected_sha256, base) -> dict:
    """Select a local Office artifact only after current history evidence agrees."""
    if isinstance(artifact_path, (str, Path)) and Path(artifact_path).name in {'edited-document.docx','edited-spreadsheet.xlsx'}:
        from .edited_artifact_source import prepare_reference
        return prepare_reference(artifact_path, expected_sha256, base)
    ref, _, _, _ = _verified(artifact_path, expected_sha256, base)
    return ref


def require_external_source_file(source_path, base) -> Path:
    """Keep managed Office outputs on the receipt-verified source route."""
    path = Path(source_path).absolute()
    if office_source._linked(path):
        raise ArtifactSourceError("source_path_invalid")
    resolved = path.resolve()
    parents = resolved.parents
    managed_layout = (resolved.name in {"document.docx", "spreadsheet.xlsx"}
                      and len(parents) >= 3
                      and _STEP.fullmatch(parents[0].name)
                      and re.fullmatch(r"workflow-[0-9a-f]{32}", parents[1].name)
                      and _TASK.fullmatch(parents[2].name))
    managed_layout = managed_layout or (resolved.name in {'edited-document.docx','edited-spreadsheet.xlsx'}
                      and len(parents) >= 2 and parents[0].name == 'edit'
                      and _TASK.fullmatch(parents[1].name))
    if resolved.is_relative_to(Path(base).resolve()) or managed_layout:
        raise ArtifactSourceError("source_generated_requires_reference")
    return path


def prepare_artifact_task_root(task_root, base) -> Path:
    """Create a fresh task directory before the GUI writes its request text."""
    root = Path(task_root).absolute()
    base_path = Path(base).absolute()
    if (not _TASK.fullmatch(root.name) or root.parent != base_path
            or ".." in root.parts or ".." in base_path.parts
            or office_source._linked(root) or office_source._linked(base_path)
            or root.exists()):
        raise ArtifactSourceError("artifact_source_task_invalid")
    for directory in reversed((base_path, *base_path.parents)):
        if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
            raise ArtifactSourceError("artifact_source_task_invalid")
        if directory.exists():
            if not directory.is_dir():
                raise ArtifactSourceError("artifact_source_task_invalid")
            continue
        try:
            directory.mkdir(exist_ok=False)
        except OSError as exc:
            raise ArtifactSourceError("artifact_source_task_invalid") from exc
    if office_source._linked(root):
        raise ArtifactSourceError("artifact_source_task_invalid")
    try:
        root.mkdir(exist_ok=False)
    except OSError as exc:
        raise ArtifactSourceError("artifact_source_task_invalid") from exc
    return task_history._task(root, task_history._base(base_path))


def _reference(ref):
    if (type(ref) is not dict or set(ref) != _FIELDS or type(ref.get("version")) is not int
            or ref["version"] != 1 or type(ref["step_id"]) is not str
            or not _STEP.fullmatch(ref["step_id"])
            or any(not _digest(ref[key]) for key in ("sha256", "receipt_sha256", "token"))
            or any(type(ref[key]) is not str or len(ref[key]) > 1200
                   for key in ("task_root", "workflow_root", "artifact"))):
        raise ArtifactSourceError("artifact_source_reference_invalid")
    return ref


def _merge_checked(path, spec):
    """Verify the adapter-owned title merge and no extra visible sheet data."""
    try:
        with zipfile.ZipFile(path) as package:
            names = [name for name in package.namelist()
                     if re.fullmatch(r"xl/worksheets/[^/]+\.xml", name)]
            if len(names) != 1:
                raise ArtifactSourceError("artifact_source_sheet_invalid")
            info = package.getinfo(names[0])
            if info.file_size > 10_000_000:
                raise ArtifactSourceError("artifact_source_sheet_invalid")
            root = ET.fromstring(package.read(info))
            workbook = ET.fromstring(package.read("xl/workbook.xml"))
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise ArtifactSourceError("artifact_source_sheet_invalid") from exc
    merged = [item.get("ref") for item in root.iter(f"{{{_SHEET}}}mergeCell")]
    width = len(spec["headers"])
    expected = [f"A1:{office._col(width - 1)}1"] if width > 1 else []
    if merged != expected:
        raise ArtifactSourceError("artifact_source_sheet_invalid")
    sheets = workbook.find(f"{{{_SHEET}}}sheets")
    if sheets is None or len(sheets) != 1:
        raise ArtifactSourceError("artifact_source_sheet_invalid")
    last_data_row = len(spec["rows"]) + 2
    allowed = {f"{office._col(c)}{row}" for row in range(2, last_data_row + 1)
               for c in range(width)}
    allowed.add("A1")
    if spec["sum_columns"]:
        total_row = last_data_row + 1
        allowed.update(f"{office._col(c)}{total_row}" for c in spec["sum_columns"])
        label = office._total_label_col(spec)
        if label is not None:
            allowed.add(f"{office._col(label)}{total_row}")
    for cell in root.iter(f"{{{_SHEET}}}c"):
        if cell.get("r") in allowed:
            continue
        value = cell.find(f"{{{_SHEET}}}v")
        inline = cell.find(f"{{{_SHEET}}}is")
        formula = cell.find(f"{{{_SHEET}}}f")
        if (formula is not None or value is not None and value.text is not None
                or inline is not None and any(text.text for text in inline.iter(f"{{{_SHEET}}}t"))):
            raise ArtifactSourceError("artifact_source_sheet_invalid")


def _extract(snapshot, receipt, index, source_file):
    step = snapshot["steps"][index]
    prior = {key: value for key, value in list(snapshot["receipts"].items())[:index]}
    try:
        spec = office_workflow._spec(step, prior, snapshot["workflow_root"])
        actual = office.verify_office_file(source_file, spec)
    except (ValueError, RuntimeError, TypeError, KeyError, OSError, zipfile.BadZipFile,
            ET.ParseError, IndexError) as exc:
        raise ArtifactSourceError("artifact_source_file_invalid") from exc
    observed = receipt["observed"]
    if spec["kind"] == "spreadsheet":
        _merge_checked(source_file, spec)
        if not office._compare(actual, office._expected(spec)):
            raise ArtifactSourceError("artifact_source_file_invalid")
        if (observed.get("headers") != spec["headers"]
                or not office._compare(observed.get("rows"), spec["rows"])
                or not office._compare(observed.get("totals"), actual["totals"])):
            raise ArtifactSourceError("artifact_source_receipt_mismatch")
        data = {"headers": observed["headers"][:],
                "rows": [row[:] for row in observed["rows"]]}
        coverage = {"scope": "verified_generated_workbook_data_rows",
                    "rows": len(data["rows"]), "columns": len(data["headers"]),
                    "derived_totals": dict(observed["totals"]),
                    "title_row_excluded": True, "totals_row_excluded": True}
        kind = "table"
    else:
        expected_text = "\n".join([spec["title"], *spec["paragraphs"]])
        expected_table = None
        if "table" in spec:
            expected_table = {"headers": spec["table"]["headers"],
                              "rows": [["" if value is None else str(value) for value in row]
                                       for row in spec["table"]["rows"]]}
        if observed.get("text") != expected_text or observed.get("table") != expected_table:
            raise ArtifactSourceError("artifact_source_receipt_mismatch")
        data = {"paragraphs": spec["paragraphs"][:]}
        if "table" in observed:
            data["table"] = {"headers": observed["table"]["headers"][:],
                             "rows": [row[:] for row in observed["table"]["rows"]]}
        coverage = {"scope": "verified_generated_document_text", "paragraphs": len(data["paragraphs"]),
                    "tables": int("table" in data), "layout_preserved": False,
                    "source_title": spec["title"],
                    "title_policy": "replaced_by_next_document_title"}
        kind = "document"
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > 32_000:
        raise ArtifactSourceError("artifact_source_observation_oversize")
    try:
        office_source._writer_compatible(source_file.name, kind, data)
    except ValueError as exc:
        raise ArtifactSourceError("artifact_source_writer_unsupported") from exc
    return kind, data, coverage


def snapshot_artifact_source(ref, task_root, base, *, stop=lambda:False, deadline=None,
                             full_workbook=False) -> dict:
    """Revalidate the selected receipt and copy its bytes before any model call."""
    if type(ref) is dict and ref.get('kind') == 'office_edit':
        from .edited_artifact_source import snapshot_reference
        return snapshot_reference(ref, task_root, base, stop=stop, deadline=deadline,
                                  full_workbook=full_workbook)
    if full_workbook:
        raise ArtifactSourceError('artifact_workbook_source_requires_verified_edit')
    ref = _reference(ref)
    if office_source._linked(task_root) or office_source._linked(base):
        raise ArtifactSourceError("artifact_source_task_invalid")
    try:
        root = task_history._task(task_root, task_history._base(base))
    except (ValueError, OSError) as exc:
        raise ArtifactSourceError("artifact_source_task_invalid") from exc
    current, snapshot, receipt, index = _verified(ref["artifact"], ref["sha256"], base)
    if current != ref or root == snapshot["root"]:
        raise ArtifactSourceError("artifact_source_reference_stale")
    sources = root / "sources"
    try:
        sources.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ArtifactSourceError("artifact_source_snapshot_exists") from exc
    except OSError as exc:
        raise ArtifactSourceError("artifact_source_task_invalid") from exc
    target = sources / ("source" + Path(ref["artifact"]).suffix.casefold())
    try:
        digest, size = office_source._copy_stable(Path(ref["artifact"]), target)
    except (ValueError, OSError) as exc:
        raise ArtifactSourceError("artifact_source_changed") from exc
    if digest != ref["sha256"]:
        raise ArtifactSourceError("artifact_source_changed")
    current, snapshot, receipt, index = _verified(ref["artifact"], ref["sha256"], base)
    if current != ref:
        raise ArtifactSourceError("artifact_source_reference_stale")
    kind, observed, coverage = _extract(snapshot, receipt, index, target)
    result = {"source_id": "selected", "kind": kind, "sha256": digest,
              "bytes": size, "display_name": target.name, "coverage": coverage,
              "observed": observed}
    provenance = {"kind": "verified_generated_office_artifact", **ref}
    office_source._write_json_new(sources / "provenance.json", provenance)
    office_source._write_json_new(sources / "source.json", result)
    return result
