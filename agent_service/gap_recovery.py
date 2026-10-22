"""Read-only capability-gap revisit and one-way source handoff to a new task.

Candidate discovery is a hint.  It never authorizes an effect or changes the old
unsupported task.  The normal assistant remains responsible for planning and
reviewing the new request.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from itertools import islice

from . import local_skills, office_source, office_workflow, task_history
from .skill_matching import discover_candidates


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY = re.compile(r"[0-9a-f]{32}\.txt\Z")
_ROOT_FILES = {"events.jsonl", "result.json", "capability-gap.json", "request.txt", "sources",
               "retry-origin.json"}
_EVENTS = {"started", "source_snapshot_requested", "source_snapshot_verified",
           "artifact_source_snapshot_requested", "artifact_source_snapshot_verified",
           "skills_discovered", "model_requested", "model_returned", "model_failed",
           "model_schema_rejected", "schema_repair_requested", "skill_recovery_requested",
           "plan_accepted", "capability_gap_recorded", "capability_gap_record_failed", "result",
           "gap_retry_snapshot_requested", "gap_retry_snapshot_verified"}
_SOURCE_NAMES = {"source.json", "provenance.json", "source.csv", "source.xlsx", "source.docx"}


class GapRetryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.safe_code = code


def _fail(code):
    raise GapRetryError(code)


def _read(path, limit):
    if task_history._linked(path) or not path.is_file():
        _fail("gap_evidence_invalid")
    size = path.stat().st_size
    if not 0 < size <= limit:
        _fail("gap_evidence_invalid")
    with path.open("rb") as file:
        data = file.read(limit + 1)
    if len(data) != size:
        _fail("gap_evidence_invalid")
    return data


def _json(path, limit=100_000):
    return _object_bytes(_read(path, limit))


def _object_bytes(data):
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise GapRetryError("gap_evidence_invalid") from exc
    if type(value) is not dict:
        _fail("gap_evidence_invalid")
    return value


def _decoded_instruction(data):
    try:
        # Match text-file reading in the assistant CLI on Windows. The model
        # receives this exact Unicode string, including normalized newlines.
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise GapRetryError("gap_instruction_invalid") from exc


def _instruction(task, base, expected):
    request = task / "request.txt"
    if request.exists() or task_history._linked(request):
        data = _read(request, 48_000)
    else:
        legacy = base.parent / "user_requests"
        if task_history._linked(legacy) or not legacy.is_dir():
            _fail("gap_instruction_missing")
        try:
            entries = list(islice(legacy.iterdir(), 513))
        except OSError as exc:
            raise GapRetryError("gap_instruction_missing") from exc
        if len(entries) > 512:
            _fail("gap_instruction_ambiguous")
        matches = []
        for path in entries:
            if not _LEGACY.fullmatch(path.name):
                continue
            if task_history._linked(path):
                _fail("gap_instruction_invalid")
            if not path.is_file() or path.stat().st_size > 48_000:
                continue
            candidate = _read(path, 48_000)
            if sha256(_decoded_instruction(candidate).encode("utf-8")).hexdigest() == expected:
                matches.append(candidate)
                if len(matches) > 1:
                    _fail("gap_instruction_ambiguous")
        if len(matches) != 1:
            _fail("gap_instruction_missing")
        data = matches[0]
    instruction = _decoded_instruction(data)
    if sha256(instruction.encode("utf-8")).hexdigest() != expected:
        _fail("gap_instruction_changed")
    if not 1 <= len(instruction.strip()) <= 12_000:
        _fail("gap_instruction_invalid")
    return instruction, data


def _events(task, result):
    raw = _read(task / "events.jsonl", 300_000)
    lines = raw.splitlines()
    if not lines or len(lines) > 1000 or not raw.endswith(b"\n"):
        _fail("gap_events_invalid")
    kinds = []
    for line in lines:
        try:
            item = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise GapRetryError("gap_events_invalid") from exc
        if type(item) is not dict or item.get("event") not in _EVENTS:
            _fail("gap_effect_unknown")
        kinds.append(item["event"])
    if kinds[0] != "started" or kinds[-1] != "result":
        _fail("gap_events_invalid")
    last = json.loads(lines[-1].decode("utf-8"))
    if last.get("result") != result:
        _fail("gap_events_invalid")
    return sha256(raw).hexdigest()


def _source(task, base, result):
    sources = task / "sources"
    source_info = result.get("source")
    if source_info is None:
        if sources.exists() or task_history._linked(sources) or "source_provenance" in result:
            _fail("gap_source_invalid")
        return None, None, {}
    if type(source_info) is not dict or task_history._linked(sources) or not sources.is_dir():
        _fail("gap_source_invalid")
    entries = list(islice(sources.iterdir(), 6))
    if (not entries or any(item.name not in _SOURCE_NAMES or task_history._linked(item)
                           or not item.is_file() for item in entries)):
        _fail("gap_source_invalid")
    names = {item.name for item in entries}
    copied = names & {"source.csv", "source.xlsx", "source.docx"}
    if len(copied) != 1 or "source.json" not in names:
        _fail("gap_source_invalid")
    supplied = _json(sources / "source.json")
    try:
        snapshot = local_skills._prepared_snapshot(task, supplied, base)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise GapRetryError("gap_source_invalid") from exc
    summary = {key: snapshot[key] for key in
               ("source_id", "kind", "sha256", "bytes", "display_name", "coverage")}
    if source_info != summary:
        _fail("gap_source_changed")
    provenance = result.get("source_provenance")
    if "provenance.json" in names:
        if provenance != _json(sources / "provenance.json"):
            _fail("gap_provenance_changed")
    elif provenance is not None:
        _fail("gap_provenance_missing")
    files = {name: sha256(_read(sources / name, 20_000_000 if name.startswith("source.")
                                and name != "source.json" else 100_000)).hexdigest()
             for name in names}
    return snapshot, provenance, files


def _origin(task, base, instruction_hash, source_files, depth, visited):
    path = task / "retry-origin.json"
    if not path.exists() and not task_history._linked(path):
        return None
    value = _json(path)
    if (set(value) != {"schema", "task_root", "token", "instruction_sha256",
                       "result_sha256", "gap_sha256", "source_files"}
            or value["schema"] != 1 or type(value["task_root"]) is not str
            or any(type(value[key]) is not str or not _HASH.fullmatch(value[key])
                   for key in ("token", "instruction_sha256", "result_sha256", "gap_sha256"))
            or value["instruction_sha256"] != instruction_hash
            or value["source_files"] != source_files):
        _fail("gap_origin_invalid")
    try:
        parent = task_history._task(value["task_root"], base)
    except (ValueError, OSError) as exc:
        raise GapRetryError("gap_origin_invalid") from exc
    if parent == task:
        _fail("gap_origin_invalid")
    if (sha256(_read(parent / "result.json", 100_000)).hexdigest() != value["result_sha256"]
            or sha256(_read(parent / "capability-gap.json", 100_000)).hexdigest()
            != value["gap_sha256"]):
        _fail("gap_origin_changed")
    try:
        parent_proof = _proof(parent, base, depth=depth + 1, visited=visited)
    except GapRetryError as exc:
        if exc.safe_code in {"gap_origin_depth_invalid", "gap_origin_cycle"}:
            raise
        raise GapRetryError("gap_origin_changed") from exc
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise GapRetryError("gap_origin_changed") from exc
    if (parent_proof["token"] != value["token"]
            or parent_proof["binding"]["result"] != value["result_sha256"]
            or parent_proof["binding"]["gap"] != value["gap_sha256"]
            or parent_proof["gap"]["instruction_sha256"] != instruction_hash
            or parent_proof["source_files"] != source_files):
        _fail("gap_origin_changed")
    return sha256(_read(path, 100_000)).hexdigest()


def _proof(task_root, base, *, depth=0, visited=frozenset()):
    if depth > 8:
        _fail("gap_origin_depth_invalid")
    base = task_history._base(base)
    task = task_history._task(task_root, base)
    if task in visited:
        _fail("gap_origin_cycle")
    visited = visited | {task}
    entries = list(islice(task.iterdir(), 7))
    if len(entries) > 6:
        _fail("gap_effect_unknown")
    if any(item.name not in _ROOT_FILES or task_history._linked(item) for item in entries):
        _fail("gap_effect_unknown")
    result_path = task / "result.json"
    gap_path = task / "capability-gap.json"
    result_raw = _read(result_path, 100_000)
    result = _object_bytes(result_raw)
    if (result.get("root") != str(task) or result.get("ok") is not False
            or result.get("state") != "unsupported" or result.get("kind") != "unsupported"
            or type(result.get("reason")) is not str or not 1 <= len(result["reason"]) <= 500
            or set(result) - {"ok", "state", "kind", "reason", "capability_gap",
                              "source", "source_edits", "source_provenance", "root", "budget"}
            or result.get("source_edits", []) != []):
        _fail("gap_result_invalid")
    pointer = result.get("capability_gap")
    if (type(pointer) is not dict or set(pointer) != {"filename", "sha256"}
            or pointer["filename"] != "capability-gap.json"
            or type(pointer["sha256"]) is not str or not _HASH.fullmatch(pointer["sha256"])):
        _fail("gap_result_invalid")
    gap_raw = _read(gap_path, 100_000)
    if sha256(gap_raw).hexdigest() != pointer["sha256"]:
        _fail("gap_changed")
    verified_gap = task_history._verified_capability_gap(pointer, task)
    if verified_gap is None:
        _fail("gap_changed")
    gap = _object_bytes(gap_raw)
    expected = gap.get("instruction_sha256")
    if (gap.get("schema") != 1 or type(expected) is not str or not _HASH.fullmatch(expected)
            or gap.get("model_reason") != result["reason"]):
        _fail("gap_changed")
    instruction, instruction_raw = _instruction(task, base, expected)
    event_hash = _events(task, result)
    snapshot, provenance, source_files = _source(task, base, result)
    origin_hash = _origin(task, base, expected, source_files, depth, visited)
    old_available = set()
    for item in verified_gap["candidates"]:
        if item["status"] == "available":
            old_available.add((item["id"], item["sha256"], item["implementation_sha256"]))
    binding = {"result": sha256(result_raw).hexdigest(), "gap": sha256(gap_raw).hexdigest(),
               "instruction": sha256(instruction_raw).hexdigest(), "events": event_hash,
               "source_files": source_files, "origin": origin_hash}
    token = office_workflow._hash(binding)
    return {"task": task, "base": base, "result": result, "gap": gap,
            "instruction": instruction, "snapshot": snapshot, "provenance": provenance,
            "source_files": source_files, "old_available": old_available,
            "token": token, "binding": binding}


def _candidates(proof, store):
    root = Path(store) if store is not None else proof["base"].parent / "capabilities"
    discovery = discover_candidates(root, proof["base"], snapshot=proof["snapshot"],
                                    query=" ".join(proof["instruction"].split())[:200])
    result = []
    for item in discovery["candidates"]:
        identity = (item["id"], item["sha256"], item["implementation_sha256"])
        result.append({**item, "newly_available": item["status"] == "available"
                       and identity not in proof["old_available"]})
    return result


def preview_retry(task_root, base, store=None) -> dict:
    """Recover only a proven unsupported request and show current catalog clues."""
    try:
        proof = _proof(task_root, base)
        return {"eligible": True, "reason": "ready", "task_root": str(proof["task"]),
                "token": proof["token"], "instruction": proof["instruction"],
                "source_kind": proof["snapshot"]["kind"] if proof["snapshot"] else None,
                "candidates": _candidates(proof, store)}
    except (ValueError, TypeError, KeyError, OSError) as exc:
        code = exc.safe_code if isinstance(exc, GapRetryError) else "gap_evidence_invalid"
        return {"eligible": False, "reason": code, "task_root": str(task_root),
                "source_kind": None, "candidates": []}


def prepare_retry(task_root, base, expected_token, store=None) -> dict:
    """Read-only recheck immediately before the user starts a new task."""
    if type(expected_token) is not str or not _HASH.fullmatch(expected_token):
        _fail("gap_token_invalid")
    preview = preview_retry(task_root, base, store)
    if not preview["eligible"]:
        _fail(preview["reason"])
    if preview["token"] != expected_token:
        _fail("gap_token_changed")
    return preview


def snapshot_retry_source(ref, new_task, base, instruction):
    """Copy a verified old source into this fresh task, then bind retry origin."""
    if (type(ref) is not dict or set(ref) != {"task_root", "token"}
            or type(ref["task_root"]) is not str or len(ref["task_root"]) > 1200
            or type(ref["token"]) is not str or not _HASH.fullmatch(ref["token"])):
        _fail("gap_reference_invalid")
    proof = _proof(ref["task_root"], base)
    if proof["token"] != ref["token"] or proof["instruction"] != instruction:
        _fail("gap_reference_changed")
    task = task_history._task(new_task, proof["base"])
    if task == proof["task"] or (task / "retry-origin.json").exists():
        _fail("gap_retry_task_invalid")
    snapshot = proof["snapshot"]
    if snapshot is not None:
        source_dir = task / "sources"
        source_dir.mkdir(exist_ok=False)
        old_dir = proof["task"] / "sources"
        for name in sorted(proof["source_files"]):
            origin = old_dir / name
            target = source_dir / name
            if name.startswith("source.") and name != "source.json":
                digest, _ = office_source._copy_stable(origin, target)
                if digest != proof["source_files"][name]:
                    _fail("gap_source_changed")
            else:
                data = _read(origin, 100_000)
                with target.open("xb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                if sha256(data).hexdigest() != proof["source_files"][name]:
                    _fail("gap_source_changed")
        try:
            copied = local_skills._prepared_snapshot(task, snapshot, proof["base"])
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise GapRetryError("gap_source_changed") from exc
        if copied != snapshot:
            _fail("gap_source_changed")
    fresh = _proof(ref["task_root"], proof["base"])
    if fresh["token"] != ref["token"]:
        _fail("gap_reference_changed")
    origin = {"schema": 1, "task_root": str(proof["task"]), "token": ref["token"],
              "instruction_sha256": proof["gap"]["instruction_sha256"],
              "result_sha256": proof["binding"]["result"],
              "gap_sha256": proof["binding"]["gap"],
              "source_files": proof["source_files"]}
    office_source._write_json_new(task / "retry-origin.json", origin)
    return snapshot
