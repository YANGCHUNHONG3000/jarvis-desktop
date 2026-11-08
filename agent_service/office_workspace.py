"""Save and revalidate explicit Office continuation inputs locally."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

from . import workspace_state


WorkspaceError = workspace_state.WorkspaceError
_CELL = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}\Z")
_REASONS = frozenset({"missing", "changed", "source_proof", "range_invalid",
                      "readonly", "limit", "unsupported"})


class WorkspaceVerificationError(WorkspaceError):
    """A stable repair hint with no path, content, or underlying exception text."""

    def __init__(self, reason, file_index=None, stage="source"):
        if reason not in _REASONS or stage not in {"source", "range"}:
            raise ValueError("workspace_verification_code_invalid")
        self.reason = reason
        self.file_index = file_index if type(file_index) is int and file_index >= 0 else None
        self.stage = stage
        super().__init__("workspace_" + reason)


_MESSAGES = {
    "missing": "文件已不存在；请重新选择已核验的成果。",
    "changed": "文件字节已变化；请重新选择当前已核验的成果。",
    "source_proof": "成果来源或回执无法核验；请重新选择有完整证明的成果。",
    "range_invalid": "保存的编辑位置已失效；请重新选择范围。",
    "readonly": "所选位置均为只读；请加入至少一个可编辑位置。",
    "limit": "所选文件或范围超过限制；请缩小选择。",
    "unsupported": "当前文件结构无法用于此编辑范围；请重新选择受支持的成果或范围。",
}


def describe_error(error):
    """Return a bounded, safe message for the repair UI."""
    if not isinstance(error, WorkspaceVerificationError) or error.reason not in _MESSAGES:
        return "工作区无法核验；请重新选择已核验的成果和范围。"
    prefix = f"第{error.file_index + 1}份文件：" if error.file_index is not None else "工作区："
    return prefix + _MESSAGES[error.reason]


def _location(value, suffix):
    if type(value) is not dict:
        raise WorkspaceError("workspace_payload_invalid")
    if suffix == ".docx":
        keys = set(value)
        if keys not in ({"paragraph_index"},
                        {"table_index", "row_index", "column_index", "paragraph_index"}):
            raise WorkspaceError("workspace_payload_invalid")
        if any(type(index) is not int or index < 0 for index in value.values()):
            raise WorkspaceError("workspace_payload_invalid")
    elif (set(value) != {"sheet", "cell"} or type(value["sheet"]) is not str
          or not value["sheet"] or len(value["sheet"]) > 31
          or type(value["cell"]) is not str or not _CELL.fullmatch(value["cell"])):
        raise WorkspaceError("workspace_payload_invalid")


def _payload(value):
    if type(value) is not dict or set(value) != {"draft", "records", "scope"}:
        raise WorkspaceError("workspace_payload_invalid")
    draft, records, scope = value["draft"], value["records"], value["scope"]
    if type(draft) is not str or len(draft) > 12_000:
        raise WorkspaceError("workspace_payload_invalid")
    if type(records) is not list or not 1 <= len(records) <= 4:
        raise WorkspaceError("workspace_payload_invalid")
    seen = set()
    for record in records:
        path = workspace_state._file_record(record)
        key = str(path).casefold()
        if key in seen:
            raise WorkspaceError("workspace_payload_invalid")
        seen.add(key)
    if scope is not None:
        if type(scope) is not list or len(scope) != len(records):
            raise WorkspaceError("workspace_payload_invalid")
        count = 0
        for record, selection in zip(records, scope):
            if type(selection) is not dict or set(selection) != {"path", "sha256", "locations"}:
                raise WorkspaceError("workspace_payload_invalid")
            path = workspace_state._file_record({"path": selection["path"],
                                                "sha256": selection["sha256"]})
            if (selection["path"] != record["path"]
                    or selection["sha256"] != record["sha256"]
                    or type(selection["locations"]) is not list
                    or not selection["locations"]):
                raise WorkspaceError("workspace_payload_invalid")
            positions = set()
            for location in selection["locations"]:
                _location(location, path.suffix.lower())
                key = workspace_state._encode(location)
                if key in positions:
                    raise WorkspaceError("workspace_payload_invalid")
                positions.add(key)
                count += 1
                if count > 2000:
                    raise WorkspaceError("workspace_payload_invalid")
    if len(workspace_state._encode(value)) > workspace_state._MAX_SNAPSHOT:
        raise WorkspaceError("workspace_oversize")
    return deepcopy(value)


def load(store):
    return workspace_state.load(store, _validate=_payload)


def save(store, payload, expected_id):
    return workspace_state.save(store, payload, expected_id, _validate=_payload)


def _source_failure(record, index):
    """Classify the current local bytes after one provenance check failed."""
    path = Path(record["path"])
    try:
        workspace_state._safe_components(path)
        if not path.exists():
            return WorkspaceVerificationError("missing", index)
        if not path.is_file():
            return WorkspaceVerificationError("source_proof", index)
        if path.stat().st_size > 20_000_000:
            return WorkspaceVerificationError("limit", index)
        digest = sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            return WorkspaceVerificationError("changed", index)
    except (OSError, ValueError):
        return WorkspaceVerificationError("source_proof", index)
    return WorkspaceVerificationError("source_proof", index)


def _snapshot_failure(exc, record, index):
    code = str(exc)
    if code in {"source_path_invalid", "source_changed", "edit_source_changed"}:
        diagnosis = _source_failure(record, index)
        if diagnosis.reason in {"missing", "changed"} or code == "source_path_invalid":
            return diagnosis
    if code.endswith(("_limit", "_size_invalid")):
        return WorkspaceVerificationError("limit", index, "range")
    return WorkspaceVerificationError("unsupported", index, "range")


def verify(payload, task_base):
    """Verify receipts first, then restore only currently valid locations."""
    try:
        checked = _payload(payload)
    except WorkspaceError as exc:
        if type(payload) is dict and type(payload.get("records")) is list:
            for index, item in enumerate(payload["records"][:4]):
                if type(item) is dict and type(item.get("path")) is str:
                    try:
                        workspace_state._safe_components(Path(item["path"]))
                    except (WorkspaceError, OSError, ValueError):
                        raise WorkspaceVerificationError("source_proof", index) from exc
        reason = "limit" if str(exc) == "workspace_oversize" else (
            "range_invalid" if type(payload) is dict and payload.get("scope") is not None
            else "source_proof")
        raise WorkspaceVerificationError(reason, stage="range" if reason != "source_proof" else "source") from exc
    from .office_handoff import select_outputs
    refs = []
    for index, record in enumerate(checked["records"]):
        path = Path(record["path"])
        try:
            workspace_state._safe_components(path)
            if not path.exists():
                raise WorkspaceVerificationError("missing", index)
            if not path.is_file():
                raise WorkspaceVerificationError("source_proof", index)
        except WorkspaceVerificationError:
            raise
        except (OSError, ValueError) as exc:
            raise WorkspaceVerificationError("source_proof", index) from exc
        try:
            selected = select_outputs([record], base=task_base)
            if type(selected) is not list or len(selected) != 1 or type(selected[0]) is not dict:
                raise ValueError("invalid reference")
            refs.append(selected[0])
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise _source_failure(record, index) from exc
    result = {"payload": checked, "refs": refs, "source_ref": None}
    if checked["scope"] is None:
        return result

    from .office_edit import snapshot_edit_source, verify_edit_source
    from .office_scope import entries, scoped_entries, selection_from_indices

    try:
        base = Path(task_base)
        if not base.is_absolute() or ".." in base.parts:
            raise WorkspaceVerificationError("source_proof")
        checks = workspace_state._store_root(base.parent / "office_workspace_checks", create=True)
        evidence = checks / uuid.uuid4().hex
        evidence.mkdir(exist_ok=False)
        catalog = {"files": [], "artifact_refs": refs}
        snapshots, indices, contexts = [], [], []
        for i, (record, selection) in enumerate(zip(checked["records"], checked["scope"])):
            folder = evidence / ("f" + str(i + 1))
            folder.mkdir(exist_ok=False)
            try:
                public = snapshot_edit_source(record["path"], folder)
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                raise _snapshot_failure(exc, record, i) from exc
            if public["sha256"] != record["sha256"]:
                raise WorkspaceVerificationError("changed", i, "range")
            try:
                selected_rows = scoped_entries(public, record["path"], selection)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise WorkspaceVerificationError("range_invalid", i, "range") from exc
            all_rows = entries(public)
            catalog["files"].append({"path": record["path"], "sha256": public["sha256"],
                                     "kind": public["kind"], "entries": all_rows})
            contexts.append(selected_rows)
            wanted = {workspace_state._encode(row["at"]) for row in selected_rows}
            indices.extend((i, j) for j, row in enumerate(all_rows)
                           if workspace_state._encode(row["at"]) in wanted)
            snapshots.append((folder, public))
        if sum(len(file["entries"]) for file in catalog["files"]) > 2000:
            raise WorkspaceVerificationError("limit", stage="range")
        editable = sum(row["editable"] is True for rows in contexts for row in rows)
        if editable == 0:
            raise WorkspaceVerificationError("readonly", stage="range")
        if editable > 120 or len(json.dumps(contexts, ensure_ascii=False)) > 18_000:
            raise WorkspaceVerificationError("limit", stage="range")
        try:
            selected = selection_from_indices(catalog, indices)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkspaceVerificationError("range_invalid", stage="range") from exc
        if len(selected["scope"]) != len(checked["scope"]):
            raise WorkspaceVerificationError("range_invalid", stage="range")
        for i, (rebuilt, saved) in enumerate(zip(selected["scope"], checked["scope"])):
            if (rebuilt["path"] != saved["path"] or rebuilt["sha256"] != saved["sha256"]
                    or {workspace_state._encode(at) for at in rebuilt["locations"]}
                    != {workspace_state._encode(at) for at in saved["locations"]}):
                raise WorkspaceVerificationError("range_invalid", i, "range")
        for i, (folder, public) in enumerate(snapshots):
            try:
                if verify_edit_source(folder) != public:
                    raise WorkspaceVerificationError("changed", i, "range")
            except WorkspaceVerificationError:
                raise
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                raise WorkspaceVerificationError("changed", i, "range") from exc
    except WorkspaceVerificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise WorkspaceVerificationError("unsupported", stage="range") from exc
    result["evidence_root"] = str(evidence)
    result["review"] = [{"path": item["path"], "rows": deepcopy(rows)}
                        for item, rows in zip(checked["records"], contexts)]
    # Ephemeral local snapshots let the review reuse the planner's exact input
    # limits; they are never part of the saved workspace or uploaded here.
    result["review_sources"] = [
        {"id": f"e{i + 1}", "root": str(folder), "public": deepcopy(public),
         "path": record["path"], "scope": deepcopy(selection)}
        for i, ((folder, public), record, selection) in enumerate(
            zip(snapshots, checked["records"], checked["scope"]))]
    return result
