"""Bounded local workspace snapshots; Office provenance is checked on restore."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .office_workflow import _owner


_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SNAPSHOT = 64 * 1024
_MAX_POINTER = 1024


class WorkspaceError(ValueError):
    """Stable, path-free error code for a workspace failure."""


def _linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise WorkspaceError("workspace_path_invalid") from exc
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _safe_components(path):
    current = path
    while True:
        if _linked(current):
            raise WorkspaceError("workspace_path_invalid")
        if current == current.parent:
            return
        current = current.parent


def _store_root(store, *, create=False):
    try:
        root = Path(store)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("workspace_path_invalid") from exc
    if not root.is_absolute() or ".." in root.parts:
        raise WorkspaceError("workspace_path_invalid")
    _safe_components(root)
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if root.exists() and not root.is_dir():
            raise WorkspaceError("workspace_path_invalid")
    except WorkspaceError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkspaceError("workspace_io") from exc
    _safe_components(root)
    return root


def _file_record(value):
    if type(value) is not dict or set(value) != {"path", "sha256"}:
        raise WorkspaceError("workspace_payload_invalid")
    name, digest = value["path"], value["sha256"]
    if type(name) is not str or type(digest) is not str or not _SHA.fullmatch(digest):
        raise WorkspaceError("workspace_payload_invalid")
    try:
        path = Path(name)
    except ValueError as exc:
        raise WorkspaceError("workspace_payload_invalid") from exc
    if (not path.is_absolute() or ".." in path.parts
            or path.suffix.lower() not in {".docx", ".xlsx"}):
        raise WorkspaceError("workspace_payload_invalid")
    _safe_components(path)
    return path


def _payload(value):
    if type(value) is not dict or set(value) != {"draft", "records", "source"}:
        raise WorkspaceError("workspace_payload_invalid")
    draft, records, source = value["draft"], value["records"], value["source"]
    if type(draft) is not str or len(draft) > 12_000:
        raise WorkspaceError("workspace_payload_invalid")
    if type(records) is not list or len(records) > 4:
        raise WorkspaceError("workspace_payload_invalid")
    seen = {}
    for item in records:
        path = _file_record(item)
        key = str(path).casefold()
        if key in seen:
            raise WorkspaceError("workspace_payload_invalid")
        seen[key] = item["sha256"]
    if source is not None:
        path = _file_record(source)
        old = seen.get(str(path).casefold())
        if old is not None and old != source["sha256"]:
            raise WorkspaceError("workspace_payload_invalid")
    return deepcopy(value)


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkspaceError("workspace_payload_invalid") from exc


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError("workspace_record_invalid")
        result[key] = value
    return result


def _read(path, limit):
    _safe_components(path)
    try:
        if not path.is_file():
            raise WorkspaceError("workspace_record_invalid")
        if path.stat().st_size > limit:
            raise WorkspaceError("workspace_oversize")
        with path.open("rb") as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise WorkspaceError("workspace_oversize")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError("workspace_record_invalid") from exc
    if type(value) is not dict:
        raise WorkspaceError("workspace_record_invalid")
    return value, data


def _write_new(path, data):
    _safe_components(path)
    try:
        with path.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except OSError as exc:
        raise WorkspaceError("workspace_io") from exc


def _load(root, *, _validate=_payload):
    latest = root / "latest.json"
    _safe_components(latest)
    if not latest.exists():
        return None
    pointer, _ = _read(latest, _MAX_POINTER)
    if (set(pointer) != {"id", "sha256"} or type(pointer["id"]) is not str
            or type(pointer["sha256"]) is not str or not _ID.fullmatch(pointer["id"])
            or not _SHA.fullmatch(pointer["sha256"])):
        raise WorkspaceError("workspace_record_invalid")
    snapshot, raw = _read(root / ("snapshot-" + pointer["id"] + ".json"), _MAX_SNAPSHOT)
    if sha256(raw).hexdigest() != pointer["sha256"]:
        raise WorkspaceError("workspace_record_invalid")
    if set(snapshot) != {"id", "payload"} or snapshot["id"] != pointer["id"]:
        raise WorkspaceError("workspace_record_invalid")
    try:
        payload = _validate(snapshot["payload"])
    except WorkspaceError as exc:
        raise WorkspaceError("workspace_record_invalid") from exc
    return {"id": pointer["id"], "payload": payload}


def load(store, *, _validate=_payload):
    """Read the latest intact snapshot, or None before the first save."""
    root = _store_root(store)
    if not root.exists():
        return None
    return _load(root, _validate=_validate)


def save(store, payload, expected_id, *, _validate=_payload):
    """Publish one immutable snapshot using a cross-process compare-and-swap."""
    checked = _validate(payload)
    if expected_id is not None and (type(expected_id) is not str
                                    or not _ID.fullmatch(expected_id)):
        raise WorkspaceError("workspace_conflict")
    root = _store_root(store, create=True)
    try:
        with _owner(root):
            current = _load(root, _validate=_validate)
            if (current or {}).get("id") != expected_id:
                raise WorkspaceError("workspace_conflict")
            if current is not None and current["payload"] == checked:
                return current
            ident = uuid.uuid4().hex
            record = {"id": ident, "payload": checked}
            raw = _encode(record)
            if len(raw) > _MAX_SNAPSHOT:
                raise WorkspaceError("workspace_oversize")
            _write_new(root / ("snapshot-" + ident + ".json"), raw)
            pointer = _encode({"id": ident, "sha256": sha256(raw).hexdigest()})
            temporary = root / ("latest-" + uuid.uuid4().hex + ".tmp")
            _write_new(temporary, pointer)
            try:
                os.replace(temporary, root / "latest.json")
            except OSError as exc:
                raise WorkspaceError("workspace_io") from exc
            return deepcopy(record)
    except WorkspaceError:
        raise
    except ValueError as exc:
        code = "workspace_busy" if str(exc) == "workflow_busy" else "workspace_io"
        raise WorkspaceError(code) from exc
    except OSError as exc:
        raise WorkspaceError("workspace_io") from exc


def verify(payload, task_base):
    """Revalidate every Office source and reuse a selected batch reference."""
    checked = _payload(payload)
    from .office_handoff import select_outputs
    records, source = checked["records"], checked["source"]
    try:
        refs = select_outputs(records, base=task_base) if records else []
        source_ref = None
        if source is not None:
            index = next((index for index, item in enumerate(records)
                          if item["path"].casefold() == source["path"].casefold()), None)
            source_ref = refs[index] if index is not None else select_outputs([source], base=task_base)[0]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkspaceError("workspace_reference_invalid") from exc
    return {"payload": checked, "refs": refs, "source_ref": source_ref}
