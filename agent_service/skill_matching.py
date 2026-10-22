"""Read-only discovery of installed, gated declarative Office skill candidates.

Discovery is a planner aid only. Query matches are catalog hints, and a candidate
still needs explicit user intent review before the existing skill executor runs.
"""

from __future__ import annotations

import re
from itertools import islice
from pathlib import Path

from . import capabilities, local_skills


_ID = re.compile(r"[a-z][a-z0-9-]{2,47}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FAMILIES = 100
_MAX_VERSIONS = 100
_MAX_CANDIDATES = 50


def _linked(path: Path) -> bool:
    """Reject symlinks and Windows junctions without traversing them."""
    try:
        return local_skills._linked(path)
    except (OSError, ValueError):
        return True


def _summary(steps, recipe=None):
    """Return complete effect shape while deliberately omitting all source values."""
    result = []
    for step in steps:
        action = step.get("action")
        item = {"id": step.get("id"), "action": action}
        if action == "spreadsheet":
            data = step.get("data", {})
            item.update(output=".xlsx", title=data.get("title"),
                        columns=len(data.get("headers", [])), rows=len(data.get("rows", [])),
                        sum_columns=list(data.get("sum_columns", [])))
            if recipe == "document_numeric_report":
                # This fixed recipe retains every source column in order, so
                # source and output coordinates coincide. Do not expose cells.
                item["numeric_source_columns"] = list(data.get("sum_columns", []))
        elif action in {"document", "document_from_sheet"}:
            data = step.get("data", {})
            table = data.get("table", {})
            source_step = step.get("from")
            if source_step is None and recipe in {"table_report", "document_table_report"}:
                source_step = "s1"
            item.update(output=".docx", title=step.get("title", data.get("title")),
                        from_step=source_step,
                        paragraphs=len(step.get("paragraphs", data.get("paragraphs", []))),
                        table_rows=len(table.get("rows", [])) if type(table) is dict else 0)
        elif action == "email_draft":
            item.update(output=".eml", subject=step.get("data", {}).get("subject"),
                        attached_steps=list(step.get("attachments", [])),
                        recipient_count=len(step.get("data", {}).get("to", [])))
        result.append(item)
    return result


def _manifest_candidates(store, family, active_digest=None):
    """Read at most a bounded number of digest directories; skip malformed entries."""
    versions = []
    seen = set()
    if type(active_digest) is str and _HASH.fullmatch(active_digest):
        active_path = family / active_digest
        if not _linked(active_path):
            try:
                if active_path.is_dir():
                    _path, manifest = local_skills._version(store, family.name, active_digest)
                    versions.append((active_digest, manifest))
                    seen.add(active_digest)
            except (ValueError, OSError, TypeError, KeyError):
                pass
    try:
        children = sorted(islice(family.iterdir(), _MAX_VERSIONS), key=lambda p: p.name)
    except OSError:
        return versions
    for child in children:
        digest = child.name
        if digest in seen or not _HASH.fullmatch(digest) or _linked(child):
            continue
        try:
            if not child.is_dir():
                continue
            path, manifest = local_skills._version(store, family.name, digest)
            versions.append((digest, manifest))
        except (ValueError, OSError, TypeError, KeyError):
            continue
    return versions


def _hints(query):
    if query == "":
        return {"matched_ids": [], "match_semantics": "catalog_hint_only"}
    try:
        result = capabilities.capability_gap(query)
        return {"matched_ids": result["matched_ids"],
                "match_semantics": "static_alias_or_substring_hint_only"}
    except (ValueError, TypeError):
        return {"matched_ids": [], "match_semantics": "catalog_hint_only",
                "reason": "query_invalid"}


def discover_candidates(store, base, snapshot=None, query=""):
    """List bounded installed skill versions and source-compatible effect previews.

    `query` can populate static catalog hints only. Candidate discovery itself is
    independent of matching and never mutates state, executes, or calls a model.
    Source cell values and document paragraphs are never included in this result.
    """
    hints = _hints(query)
    candidates = []
    try:
        root = local_skills._store(store)
        families = sorted(islice(root.iterdir(), _MAX_FAMILIES), key=lambda p: p.name)
    except (ValueError, OSError, TypeError):
        return {"schema": 1, "candidates": [], "hints": hints}

    try:
        implementation = local_skills.implementation_sha256()
    except (ValueError, OSError, TypeError):
        implementation = None
    scanned = 0
    for family in families:
        if scanned >= _MAX_FAMILIES:
            break
        if not _ID.fullmatch(family.name) or _linked(family) or not family.is_dir():
            continue
        scanned += 1
        try:
            state = local_skills.status(root, family.name, base)
            active_digest = state.get("active")
        except (ValueError, OSError, TypeError, KeyError):
            state = {"active": None, "remaining_runs": 0, "inflight": None,
                     "blocked": [], "enabled": False}
            active_digest = None
        versions = _manifest_candidates(root, family, active_digest)

        # Keep the active pinned version ahead of any older/inactive versions.
        versions.sort(key=lambda pair: (pair[0] != active_digest,
                                        -pair[1]["version"], pair[0]))

        for digest, manifest in versions:
            if len(candidates) >= _MAX_CANDIDATES:
                break
            active = digest == active_digest
            enabled = active and state.get("enabled") is True
            reason = None
            if not active:
                reason = "not_active"
            elif not enabled:
                if digest in state.get("blocked", []):
                    reason = "candidate_blocked"
                elif state.get("inflight"):
                    reason = "candidate_inflight"
                elif not state.get("remaining_runs"):
                    reason = "run_budget_exhausted_or_disabled"
                else:
                    reason = "verification_stale_or_invalid"

            effects = []
            if snapshot is None:
                status = "incompatible"
                reason = "source_required" if reason is None or enabled else reason
            else:
                try:
                    steps = local_skills.build_steps(manifest, snapshot)
                    effects = _summary(steps, manifest["recipe"])
                    status = "available" if enabled else "disabled"
                except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                    status = "incompatible"
                    code = str(exc)
                    reason = code if re.fullmatch(r"[a-z][a-z0-9_]{1,79}", code) else "source_incompatible"

            if implementation is None and status == "available":
                status, reason = "disabled", "implementation_fingerprint_unavailable"

            candidates.append({
                "id": manifest["id"], "sha256": digest,
                "implementation_sha256": implementation,
                "version": manifest["version"], "name": manifest["name"],
                "recipe": manifest["recipe"],
                "remaining_runs": state.get("remaining_runs", 0) if active else 0,
                "status": status, "reason": reason,
                "source_kind": snapshot.get("kind") if type(snapshot) is dict else None,
                "effects": effects,
            })
        if len(candidates) >= _MAX_CANDIDATES:
            break

    return {"schema": 1, "candidates": candidates, "hints": hints}


def candidate_plan(store, skill_id, digest, snapshot):
    """Build a pinned version's resolved plan; callers must explicitly review it."""
    if type(skill_id) is not str or not _ID.fullmatch(skill_id):
        raise local_skills.SkillError("skill_identity_invalid")
    if type(digest) is not str or not _HASH.fullmatch(digest):
        raise local_skills.SkillError("skill_identity_invalid")
    _path, manifest = local_skills._version(local_skills._store(store), skill_id, digest)
    return local_skills.build_steps(manifest, snapshot)


def list_available_for_prompt(store, base, snapshot=None, query=""):
    """Return only compact currently available candidates for budgeted planning."""
    found = discover_candidates(store, base, snapshot=snapshot, query=query)
    return [{key: item[key] for key in
             ("id", "sha256", "recipe", "name", "effects")}
            for item in found["candidates"] if item["status"] == "available"]
