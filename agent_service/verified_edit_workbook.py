"""Complete native-verified workbook intake for receipt-bound edit chains."""

from hashlib import sha256
from pathlib import Path
import time

from . import artifact_source, office_source, workbook_source
from .computer import safe_root
from .computer_agent_tools import _load
from .office_edit import _read, verify_edit_source


def _derive(ref, base):
    from .edited_artifact_source import prepare_reference

    base = safe_root(base)
    edits, seen, current = [], set(), ref
    while True:
        artifact = safe_root(current["artifact"])
        if artifact in seen or len(edits) >= 4:
            raise ValueError("edited_workbook_chain_invalid")
        seen.add(artifact)
        if (artifact.name != "edited-spreadsheet.xlsx"
                or prepare_reference(artifact, current["sha256"], base) != current):
            raise ValueError("edited_workbook_ref_changed")
        manifest, _ = _load(artifact.parent / "edit-manifest.json")
        if manifest["spec"]["kind"] != "spreadsheet":
            raise ValueError("edited_workbook_kind_invalid")
        source_root = safe_root(manifest["source_root"])
        source = verify_edit_source(source_root)
        original = safe_root(_read(source_root / "source.json")["original"])
        edits.append(current)
        if original.name != "edited-spreadsheet.xlsx":
            break
        current = prepare_reference(original, source["sha256"], base)
    root_hash = source["sha256"]
    if sha256(original.read_bytes()).hexdigest() != root_hash:
        raise ValueError("edited_workbook_root_changed")
    root_ref = {"artifact": str(original), "sha256": root_hash}
    from .generated_edit_table import _generated_path
    # A managed Office creation root retains its creation receipt. The shape
    # follows the same task/workflow/step ownership convention as documents.
    if _generated_path(original, base):
        root_ref["creation"] = artifact_source.prepare_artifact_source(original, root_hash, base)
    return root_ref, list(reversed(edits))


def snapshot_reference(ref, root, base, *, stop=lambda: False, deadline=None):
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError("source_cancelled")
    derived = _derive(ref, base)
    artifact = safe_root(ref["artifact"])
    public = workbook_source.snapshot_workbook_source(artifact, root, stop=stop, deadline=deadline)
    if (public["sha256"] != ref["sha256"] or _derive(ref, base) != derived):
        raise ValueError("edited_workbook_changed")
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError("source_cancelled")
    root_ref, edits = derived
    sources = root / "sources"
    office_source._write_json_new(sources / "provenance.json",
                                  {"kind": "verified_edited_workbook", "edited": ref,
                                   "edits": edits, "root": root_ref})
    return public


def verify_snapshot(task, snapshot, provenance, base):
    if (type(provenance) is not dict
            or set(provenance) != {"kind", "edited", "edits", "root"}
            or provenance["kind"] != "verified_edited_workbook"):
        raise ValueError("edited_workbook_provenance_invalid")
    ref = provenance["edited"]
    if (type(ref) is not dict or set(ref) != {
            "version", "kind", "artifact", "sha256", "manifest_sha256", "receipt_sha256"}):
        raise ValueError("edited_workbook_provenance_invalid")
    public = workbook_source.verify_workbook_source(task)
    root_ref, edits = _derive(ref, base)
    if (snapshot != public or snapshot["sha256"] != ref["sha256"]
            or snapshot["display_name"] != Path(ref["artifact"]).name
            or root_ref != provenance["root"] or edits != provenance["edits"]):
        raise ValueError("edited_workbook_snapshot_changed")
    return "workbook", public["observed"], public["coverage"]
