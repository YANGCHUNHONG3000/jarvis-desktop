"""Complete, bounded external Word snapshots with a supervised native receipt."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from . import office_edit, office_source, structured_document_source


_HEX = re.compile(r"[0-9a-f]{64}\Z")


class ExternalDocumentError(office_source.SourceError):
    pass


def _fail(code):
    raise ExternalDocumentError(code)


def _digest(path):
    path = Path(path)
    if (office_source._linked(path) or not path.is_file()
            or not 0 < path.stat().st_size <= 20_000_000):
        _fail("external_document_path_invalid")
    return sha256(path.read_bytes()).hexdigest()


def _read(path):
    path = Path(path)
    if office_source._linked(path) or not path.is_file() or path.stat().st_size > 500_000:
        _fail("external_document_record_invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExternalDocumentError("external_document_record_invalid") from exc


def _observation_hash(observed):
    return sha256(json.dumps(observed, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _coverage(parsed):
    return {**parsed, "scope": "verified_external_document_structure"}


def _parse(path):
    # The simple reader supplies its package/content exclusions, while the
    # structural reader accounts for every body paragraph, table and cell.
    parts = office_source._package_parts(path)
    try:
        office_source._document(parts)
    except office_source.SourceError:
        pass
    observed, parsed = structured_document_source.parse(path)
    return observed, _coverage(parsed)


def validate_snapshot(public):
    if (type(public) is not dict or set(public) != {"source_id", "kind", "sha256", "bytes",
                                                    "display_name", "coverage", "observed", "native_sha256"}
            or public["source_id"] != "selected" or public["kind"] != "document_structure"
            or type(public["bytes"]) is not int or not 0 < public["bytes"] <= 20_000_000
            or any(type(public[key]) is not str or not _HEX.fullmatch(public[key])
                   for key in ("sha256", "native_sha256"))
            or type(public["display_name"]) is not str or not public["display_name"]
            or len(public["display_name"]) > 255 or Path(public["display_name"]).name != public["display_name"]
            or type(public["coverage"]) is not dict
            or set(public["coverage"]) != {"scope", "records", "tables", "cells", "paragraphs", "layout_preserved"}
            or public["coverage"].get("scope") != "verified_external_document_structure"
            or public["coverage"].get("layout_preserved") is not False):
        _fail("external_document_snapshot_invalid")
    try:
        structured_document_source.audit_table(public["observed"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ExternalDocumentError("external_document_snapshot_invalid") from exc
    records = public["observed"]["records"]
    if (len(records) != public["coverage"]["records"]
            or any(sum(item["type"] == kind for item in records) != public["coverage"][name]
                   for kind, name in (("table", "tables"), ("cell", "cells"), ("paragraph", "paragraphs")))):
        _fail("external_document_coverage_invalid")
    return deepcopy(public)


def _native_matches(receipt, public):
    if (type(receipt) is not dict or set(receipt) != {"version", "source_sha256", "observed_sha256",
                                                      "application_verified", "file_unchanged", "app"}
            or receipt["version"] != 1 or receipt["source_sha256"] != public["sha256"]
            or receipt["observed_sha256"] != _observation_hash(public["observed"])
            or receipt["application_verified"] is not True or receipt["file_unchanged"] is not True
            or type(receipt["app"]) is not dict or set(receipt["app"]) != {"progid", "name", "version"}
            or receipt["app"]["progid"] != "KWPS.Application"
            or any(type(receipt["app"][key]) is not str or not receipt["app"][key]
                   or len(receipt["app"][key]) > 200 for key in ("name", "version"))):
        _fail("external_document_native_receipt_invalid")


def _native_snapshot(path, stop=lambda: False):
    """Read only our copied document; close it without saving, never quit WPS."""
    import pythoncom
    import win32com.client

    if office_source._linked(path):
        _fail("external_document_path_invalid")
    path = Path(path).resolve()
    observed, _ = _parse(path)
    initial = _digest(path)

    def check():
        if stop():
            _fail("external_document_cancelled")

    check()
    pythoncom.CoInitialize()
    owned = None
    try:
        app = win32com.client.DispatchEx("KWPS.Application")
        check()
        owned = app.Documents.Open(str(path), ReadOnly=True)
        check()
        office_edit._native_complex_word(owned, office_edit._word_body(office_edit._parts(path)), check)
        check()
        unchanged = _digest(path) == initial
        if not unchanged:
            _fail("external_document_source_changed")
        return {"version": 1, "source_sha256": initial,
                "observed_sha256": _observation_hash(observed),
                "application_verified": True, "file_unchanged": True,
                "app": {"progid": "KWPS.Application", "name": str(app.Name),
                        "version": str(app.Version)}}
    finally:
        try:
            if owned is not None:
                owned.Close(SaveChanges=False)
        finally:
            pythoncom.CoUninitialize()


def snapshot_external_document(path, task_root, *, stop=lambda: False, deadline=None):
    from .external_document_source_host import native_snapshot_supervised

    source, root = Path(path).absolute(), Path(task_root).absolute()
    if (office_source._linked(root) or not root.is_dir() or office_source._linked(source)
            or not source.is_file() or source.suffix.casefold() != ".docx"
            or source.resolve().is_relative_to(root.resolve())):
        _fail("external_document_source_invalid")
    if stop():
        _fail("external_document_cancelled")
    # Reject malformed or unrepresentable input before creating task state.
    _parse(source)
    directory = root / "sources"
    try:
        directory.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ExternalDocumentError("external_document_snapshot_exists") from exc
    snapshot = directory / "source.docx"
    digest, size = office_source._copy_stable(source, snapshot)
    observed, coverage = _parse(snapshot)
    receipt = native_snapshot_supervised(snapshot, directory, stop=stop, deadline=deadline)
    if stop() or deadline is not None and time.monotonic() >= deadline:
        _fail("external_document_cancelled")
    if _digest(snapshot) != digest or _digest(source) != digest:
        _fail("external_document_source_changed")
    public = {"source_id": "selected", "kind": "document_structure", "sha256": digest,
              "bytes": size, "display_name": source.name, "coverage": coverage,
              "observed": observed, "native_sha256": _digest(directory / "source-native.json")}
    validate_snapshot(public)
    _native_matches(receipt, public)
    office_source._write_json_new(directory / "source.json", public)
    return public


def verify_external_document(task_root):
    directory = Path(task_root).absolute() / "sources"
    if office_source._linked(directory) or not directory.is_dir():
        _fail("external_document_source_invalid")
    public = validate_snapshot(_read(directory / "source.json"))
    request = _read(directory / "native-request.json")
    if type(request) is not dict or request != {"sha256": public["sha256"]}:
        _fail("external_document_native_request_changed")
    source = directory / "source.docx"
    if (_digest(source) != public["sha256"] or source.stat().st_size != public["bytes"]
            or _digest(directory / "source-native.json") != public["native_sha256"]):
        _fail("external_document_source_changed")
    observed, coverage = _parse(source)
    if observed != public["observed"] or coverage != public["coverage"]:
        _fail("external_document_source_changed")
    _native_matches(_read(directory / "source-native.json"), public)
    return public
