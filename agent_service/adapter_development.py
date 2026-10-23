"""Durable development of a bounded Office adapter from a verified gap request.

This module is a workflow coordinator. Candidate JSON cannot add executable code,
approve itself, or turn an unrelated request into a supported Office operation.
"""

from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
import uuid

from . import artifact_source, local_skills as skills, skill_resume, task_history
from . import office_workflow as workflow


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OFFICE = re.compile(r"word|excel|文档|表格|工作簿|电子表格", re.I)
_DEVICE = re.compile(r"蓝牙|传感器|温湿度|设备协议|bluetooth|sensor|serial|串口", re.I)
_ACTIONS = {
    "workbook_audit_report": (re.compile(r"工作簿|Excel|电子表格|workbook", re.I),
                              re.compile(r"全部|所有|完整|every|all", re.I),
                              re.compile(r"单元格|cell", re.I),
                              re.compile(r"公式|formula", re.I),
                              re.compile(r"核验|重算|计算结果|verified|recalculat", re.I),
                              re.compile(r"Word|文档|报告", re.I)),
    "document_structure_report": (re.compile(r"Word|文档|document", re.I),
                                  re.compile(r"全部|所有|完整|every|all", re.I),
                                  re.compile(r"段落|paragraph", re.I),
                                  re.compile(r"表格|table", re.I),
                                  re.compile(r"位置|顺序|结构|合并|嵌套|position|structure|merge|nest", re.I),
                                  re.compile(r"Excel|电子表格|工作簿", re.I)),
    "document_table_transcription": (re.compile(r"表格|table", re.I),
                                     re.compile(r"excel|电子表格|工作簿", re.I),
                                     re.compile(r"word|文档|报告|report", re.I)),
    "document_table_report": (re.compile(r"表格|table", re.I),
                              re.compile(r"excel|电子表格|工作簿", re.I),
                              re.compile(r"word|文档|报告|report", re.I)),
    "document_copy": (re.compile(r"word|文档|document", re.I),
                      re.compile(r"副本|复制|copy|转存", re.I)),
    "table_copy": (re.compile(r"excel|表格|工作簿|spreadsheet", re.I),
                   re.compile(r"副本|复制|copy|转存", re.I)),
    "table_report": (re.compile(r"excel|表格|工作簿|spreadsheet", re.I),
                     re.compile(r"报告|汇总|report", re.I)),
    "document_numeric_report": (re.compile(r"表格|table", re.I),
                                re.compile(r"数字|数值|求和|合计|numeric|sum", re.I),
                                re.compile(r"报告|report", re.I)),
}


class DevelopmentError(ValueError):
    pass


def _read(path, limit=100_000):
    if task_history._linked(path) or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise DevelopmentError("development_evidence_invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise DevelopmentError("development_evidence_invalid") from exc


def _request(task_root, base):
    task = task_history._task(task_root, task_history._base(base))
    result = _read(task / "result.json")
    if (result.get("root") != str(task) or result.get("kind") != "unsupported"
            or result.get("state") != "unsupported"):
        raise DevelopmentError("development_request_invalid")
    pointer = result.get("capability_gap")
    checked = task_history._verified_capability_gap(pointer, task)
    if checked is None:
        raise DevelopmentError("development_request_changed")
    gap = _read(task / "capability-gap.json")
    instruction_path = task / "request.txt"
    if (task_history._linked(instruction_path) or not instruction_path.is_file()
            or not 0 < instruction_path.stat().st_size <= 48_000):
        raise DevelopmentError("development_instruction_invalid")
    instruction = instruction_path.read_text(encoding="utf-8")
    if sha256(instruction.encode("utf-8")).hexdigest() != gap["instruction_sha256"]:
        raise DevelopmentError("development_instruction_changed")
    return task, instruction, pointer["sha256"]


def _root(store, *, create=False):
    skill_root = Path(store).absolute()
    if task_history._linked(skill_root):
        raise DevelopmentError("development_store_invalid")
    root = skill_root.parent / "adapter_development"
    if task_history._linked(root):
        raise DevelopmentError("development_store_invalid")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _source_reference(task, base):
    """Bind a proposed artifact to the gap's selected, verified source."""
    parent = _read(task / "result.json")
    snapshot = _read(task / "sources" / "source.json")
    provenance_path = task / "sources" / "provenance.json"
    public = {key: snapshot[key] for key in
              ("source_id", "kind", "sha256", "bytes", "display_name", "coverage")}
    if (parent.get("source") != public
            or skills._prepared_snapshot(task, snapshot, base) != snapshot):
        raise DevelopmentError("development_source_changed")
    if not provenance_path.exists() and not task_history._linked(provenance_path):
        if ("source_provenance" in parent or snapshot.get("kind") not in
                {"document_structure", "workbook"} or
                snapshot.get("coverage", {}).get("scope") not in
                {"verified_external_document_structure", "all_sheets_all_rows"} or
                type(snapshot.get("native_sha256")) is not str or
                not _HASH.fullmatch(snapshot["native_sha256"])):
            raise DevelopmentError("development_source_requires_artifact")
        for name in ("source.json", "native-request.json", "source-native.json"):
            record = task / "sources" / name
            value = _read(record)
            canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
            if record.read_bytes() != canonical:
                raise DevelopmentError("development_source_changed")
        from . import gap_recovery
        proof = gap_recovery._proof(task, base)
        if proof["snapshot"] != snapshot or proof["provenance"] is not None:
            raise DevelopmentError("development_source_changed")
        source_name = "source.docx" if snapshot["kind"] == "document_structure" else "source.xlsx"
        required = {source_name, "source.json", "native-request.json", "source-native.json"}
        if set(proof["source_files"]) != required:
            raise DevelopmentError("development_source_changed")
        return {"kind": "external_office_snapshot", "version": 1,
                "artifact": str(task / "sources" / source_name), "source_task": str(task),
                "sha256": snapshot["sha256"],
                "snapshot_sha256": proof["source_files"]["source.json"],
                "source_files": proof["source_files"], "gap_token": proof["token"]}
    provenance = _read(provenance_path)
    if provenance.get("kind") == "verified_generated_office_artifact":
        ref = {key: provenance[key] for key in artifact_source._FIELDS}
    elif provenance.get("kind") in {"verified_generated_edited_table", "verified_edited_document",
                                    "verified_edited_workbook"}:
        ref = provenance.get("edited")
    else:
        raise DevelopmentError("development_source_requires_artifact")
    if type(ref) is not dict or ref.get("sha256") != snapshot.get("sha256"):
        raise DevelopmentError("development_source_changed")
    if (parent.get("source_provenance") != provenance
            and parent.get("source_provenance") != ref):
        raise DevelopmentError("development_source_changed")
    checked = artifact_source.prepare_artifact_source(ref["artifact"], ref["sha256"], base)
    if checked != ref:
        raise DevelopmentError("development_source_changed")
    return ref


def _proposal_path(task, store, digest):
    if type(digest) is not str or not _HASH.fullmatch(digest):
        raise DevelopmentError("development_proposal_invalid")
    return _root(store) / task.name / digest


def _proposal(task, instruction, request_digest, store, digest):
    path = _proposal_path(task, store, digest)
    value = _read(path / "proposal.json")
    if (type(value) is not dict or workflow._hash(value) != digest
            or value.get("schema") != 1 or value.get("request_task") != str(task)
            or value.get("request_sha256") != request_digest
            or value.get("instruction_sha256") != sha256(instruction.encode("utf-8")).hexdigest()
            or value.get("implementation_sha256") != skills.implementation_sha256()
            or type(value.get("source_artifact")) is not dict):
        raise DevelopmentError("development_proposal_stale")
    manifest = skills.validate_manifest(value.get("manifest"))
    if workflow._hash(manifest) != value.get("candidate_sha256"):
        raise DevelopmentError("development_candidate_changed")
    _relevance(instruction, manifest, value.get("effect"), value["source_artifact"])
    if _source_reference(task, task.parent) != value["source_artifact"]:
        raise DevelopmentError("development_source_changed")
    _source_scope(task, manifest)
    return path, value


def _source_scope(task, manifest):
    expected = {"workbook_audit_report": "workbook",
                "document_structure_report": "document_structure"}.get(manifest["recipe"])
    if expected is not None and _read(task / "sources" / "source.json").get("kind") != expected:
        raise DevelopmentError("development_source_kind_mismatch")


def _relevance(instruction, manifest, effect, source):
    recipe = manifest["recipe"]
    if (_DEVICE.search(instruction) or not _OFFICE.search(instruction)
            or recipe not in _ACTIONS or any(not item.search(instruction) for item in _ACTIONS[recipe])):
        raise DevelopmentError("maintainer_adapter_required")
    if (type(effect) is not str or not 20 <= len(effect.strip()) <= 2000
            or type(source) is not dict or source.get("version") != 1):
        raise DevelopmentError("development_effect_invalid")
    suffix = Path(str(source.get("artifact", ""))).suffix.lower()
    expected = ".docx" if recipe.startswith("document") else ".xlsx"
    if suffix != expected:
        raise DevelopmentError("development_source_kind_mismatch")
    explicit_title = re.search(r"标题为[“\"]([^”\"]{1,80})[”\"]", instruction)
    if explicit_title and manifest["name"] != explicit_title.group(1):
        raise DevelopmentError("development_title_mismatch")
    if (recipe in {"document_table_report", "document_table_transcription"} and
            re.search(r"不转换数字|不重新计算|未重算|原样|原文", instruction) and
            (manifest["sum_columns"] or manifest["draft"])):
        raise DevelopmentError("development_effect_mismatch")
    if (recipe == "document_table_transcription" and
            (not re.search(r"不转换数字|不重新计算|未重算|原样|原文", instruction)
             or not explicit_title)):
        raise DevelopmentError("development_effect_mismatch")
    if (recipe in {"document_table_report", "document_table_transcription"}
            and (manifest["sum_columns"] or manifest["draft"])
            or recipe == "document_numeric_report" and not manifest["sum_columns"]):
        raise DevelopmentError("development_effect_mismatch")
    if recipe in {"workbook_audit_report", "document_structure_report"}:
        if (manifest["sum_columns"] or manifest["draft"]
                or not re.search(r"全部|所有|完整|every|all", effect, re.I)
                or not re.search(r"Excel|电子表格|工作簿", effect, re.I)
                or not re.search(r"Word|文档|报告", effect, re.I)):
            raise DevelopmentError("development_effect_mismatch")
        if recipe == "workbook_audit_report" and not (
                re.search(r"静态审计|静态副本|static audit|不保留可执行公式", instruction, re.I)
                and re.search(r"不要求复制原布局|不保留原布局|无需原布局|static audit|layout not", instruction, re.I)
                and re.search(r"公式|formula", effect, re.I)
                and re.search(r"重算|核验|verified|recalculat", effect, re.I)
                and re.search(r"静态|不保留.*公式|static|not.*executable", effect, re.I)):
            raise DevelopmentError("development_effect_mismatch")
        if recipe == "document_structure_report" and not (
                re.search(r"不重建.*布局|不保留.*布局|静态审计|static audit|layout not", instruction, re.I)
                and re.search(r"段落|paragraph", effect, re.I)
                and re.search(r"表格|table", effect, re.I)
                and re.search(r"结构|合并|嵌套|位置|顺序|structure|merge|nest|position", effect, re.I)):
            raise DevelopmentError("development_effect_mismatch")


def _versions(task, store):
    family = _root(store) / task.name
    if task_history._linked(family) or not family.is_dir():
        return []
    return sorted((path.name for path in family.iterdir() if path.is_dir()
                   and not task_history._linked(path) and _HASH.fullmatch(path.name)), reverse=True)


def list_requests(base, store, *, limit=500):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise DevelopmentError("development_limit_invalid")
    result = []
    base_path = task_history._base(base)
    if not base_path.is_dir():
        return result
    candidates = sorted((p for p in base_path.iterdir() if task_history._TASK.fullmatch(p.name)
                         and p.is_dir() and not task_history._linked(p)
                         and (p / "capability-gap.json").is_file()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for task_path in candidates[:limit]:
        try:
            task, instruction, _ = _request(task_path, base)
            gap = _read(task / "capability-gap.json")
            result.append({"task_root": str(task), "instruction": instruction,
                           "reason": gap.get("model_reason", ""),
                           "state": "queued", "versions": _versions(task, store)})
        except (ValueError, OSError, KeyError, TypeError):
            result.append({"task_root": str(task_path), "instruction": "",
                           "reason": "", "state": "needs_review", "versions": []})
    return result


def start_proposal(task_root, base, store, manifest, *, effect, source_artifact=None, proposer):
    task, instruction, request_digest = _request(task_root, base)
    candidate = skills.validate_manifest(manifest)
    if (type(proposer) is not str or not 1 <= len(proposer.strip()) <= 100):
        raise DevelopmentError("development_proposer_invalid")
    checked_source = _source_reference(task, base)
    if source_artifact is None:
        source_artifact = checked_source
    _relevance(instruction, candidate, effect, source_artifact)
    if checked_source != source_artifact:
        raise DevelopmentError("development_source_changed")
    _source_scope(task, candidate)
    record = {"schema": 1, "request_task": str(task), "request_sha256": request_digest,
              "instruction_sha256": sha256(instruction.encode("utf-8")).hexdigest(),
              "implementation_sha256": skills.implementation_sha256(),
              "manifest": candidate, "candidate_sha256": workflow._hash(candidate),
              "effect": effect.strip(), "source_artifact": checked_source,
              "proposer": proposer.strip()}
    digest = workflow._hash(record)
    path = _proposal_path(task, store, digest)
    _root(store, create=True)
    with workflow._owner(_root(store)):
        current_task, current_instruction, current_request = _request(task, base)
        if (current_task != task or current_request != request_digest
                or current_instruction != instruction or _source_reference(task, base) != checked_source
                or skills.implementation_sha256() != record["implementation_sha256"]):
            raise DevelopmentError("development_proposal_stale")
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            _proposal(task, instruction, request_digest, store, digest)
        else:
            path.mkdir()
            workflow._write_new(path / "proposal.json", record)
        skills.register_candidate(store, candidate)
    return inspect(task, base, store, digest=digest)


def propose_external(task_root, base, store, *, proposer):
    """Create the bounded audit candidate from the gap's pinned native source."""
    task, instruction, _ = _request(task_root, base)
    source = _source_reference(task, base)
    if source.get("kind") != "external_office_snapshot":
        raise DevelopmentError("development_external_source_required")
    snapshot = _read(task / "sources" / "source.json")
    if snapshot["kind"] == "document_structure":
        skill_id, recipe, name = ("ext-word", "document_structure_report",
                                  "外部Word完整结构审计")
        effect = "完整保留外部 Word 所有段落、表格、顺序、合并与嵌套结构，生成 Excel 与 Word 静态审计报告。"
    else:
        skill_id, recipe, name = ("ext-book", "workbook_audit_report",
                                  "外部工作簿完整审计")
        effect = "完整保留外部 Excel 工作簿所有工作表、单元格、公式文本及重算核验值，生成 Excel 与 Word 静态审计报告，不保留可执行公式。"
    title = re.search(r"标题为[“\"]([^”\"]{1,80})[”\"]", instruction)
    if title:
        name = title.group(1)
    manifest = {"schema": 1, "id": skill_id + "-" + task.name[5:17], "version": 1, "name": name,
                "recipe": recipe, "sum_columns": [], "draft": False}
    current_implementation = skills.implementation_sha256()
    latest_version = 0
    for digest in _versions(task, store):
        prior = _read(_proposal_path(task, store, digest) / "proposal.json")
        old = skills.validate_manifest(prior.get("manifest"))
        if old["id"] != manifest["id"]:
            continue
        latest_version = max(latest_version, old["version"])
        same_recipe = {k: v for k, v in old.items() if k != "version"} == {
            k: v for k, v in manifest.items() if k != "version"}
        if (same_recipe and prior.get("implementation_sha256") == current_implementation
                and prior.get("source_artifact") == source and prior.get("effect") == effect):
            return inspect(task, base, store, digest=digest)
    manifest["version"] = latest_version + 1
    return start_proposal(task, base, store, manifest, effect=effect,
                          source_artifact=source, proposer=proposer)


def _selected(task_root, base, store, digest):
    task, instruction, request_digest = _request(task_root, base)
    path, proposal = _proposal(task, instruction, request_digest, store, digest)
    return task, path, proposal


def _gate(store, proposal, name):
    candidate = proposal["manifest"]
    path, _ = skills._version(store, candidate["id"], proposal["candidate_sha256"])
    return skills._gate_file(path, name, proposal["candidate_sha256"])


def _trial_root(path, base):
    intent = path / "trial-intent.json"
    if not intent.exists():
        return None
    value = _read(intent)
    if (value.get("schema") == 1 and set(value) == {"schema", "task_root"}):
        pass
    elif (value.get("schema") == 2 and set(value) == {"schema", "task_root", "source_retry"}
          and type(value["source_retry"]) is dict
          and set(value["source_retry"]) == {"task_root", "token"}):
        pass
    else:
        raise DevelopmentError("development_trial_intent_invalid")
    task = Path(value["task_root"])
    if (not task.is_absolute() or not task_history._TASK.fullmatch(task.name)
            or task.parent != task_history._base(base) or task_history._linked(task)):
        raise DevelopmentError("development_trial_intent_invalid")
    return task_history._task(task, task_history._base(base)) if task.is_dir() else task


def _trial_state(path, proposal, base, store):
    task = _trial_root(path, base)
    if task is None:
        return "not_started", None, None, []
    if not task.exists():
        return "interrupted", str(task), None, []
    source = proposal["source_artifact"]
    if source.get("kind") == "external_office_snapshot":
        expected = {"task_root": source["source_task"], "token": source["gap_token"]}
        intent = _read(path / "trial-intent.json")
        request = _read(task / "skill-request.json")
        if (intent.get("schema") != 2 or intent.get("source_retry") != expected
                or request.get("source_retry") != expected):
            raise DevelopmentError("development_trial_source_changed")
        from .gap_recovery import verify_prepared_origin
        verify_prepared_origin(task, base)
    inspected = task_history.inspect_task(task, base)
    try:
        skills._verify_success(task, base, proposal["candidate_sha256"])
        return "succeeded", str(task), None, inspected.get("artifacts", [])
    except (ValueError, OSError, TypeError, KeyError):
        pass
    resume = skill_resume.preview_resume(task, base, store)
    if resume["eligible"]:
        return "resumable", str(task), resume, []
    return "blocked", str(task), resume, []


def external_trial_binding(task_root, base, store):
    """Find a pinned development trial when a generic skill resume opens it."""
    task = task_history._task(task_root, task_history._base(base))
    root = _root(store)
    if not root.is_dir() or task_history._linked(root):
        return None
    for request_dir in root.iterdir():
        if (not task_history._TASK.fullmatch(request_dir.name) or
                task_history._linked(request_dir) or not request_dir.is_dir()):
            continue
        for version_dir in request_dir.iterdir():
            if (not _HASH.fullmatch(version_dir.name) or task_history._linked(version_dir)
                    or not version_dir.is_dir()):
                continue
            intent_path = version_dir / "trial-intent.json"
            if not intent_path.exists() and not task_history._linked(intent_path):
                continue
            intent = _read(intent_path)
            if intent.get("task_root") != str(task):
                continue
            original_task = task_history._base(base) / request_dir.name
            original, instruction, request_digest = _request(original_task, base)
            _, proposal = _proposal(original, instruction, request_digest, store, version_dir.name)
            source = proposal["source_artifact"]
            if source.get("kind") != "external_office_snapshot":
                return None
            expected = {"task_root": source["source_task"], "token": source["gap_token"]}
            if intent != {"schema": 2, "task_root": str(task), "source_retry": expected}:
                raise DevelopmentError("development_trial_source_changed")
            return expected
    return None


def activated_external_trial(store, skill_id, digest, base):
    """Resolve an enabled development candidate through its activation token."""
    token_path = Path(store).absolute() / skill_id / "development-activation.json"
    if not token_path.exists() and not task_history._linked(token_path):
        return None
    token = _read(token_path)
    if (token.get("schema") != 1 or token.get("candidate_sha256") != digest
            or token.get("implementation_sha256") != skills.implementation_sha256()
            or type(token.get("proposal_digest")) is not str
            or not _HASH.fullmatch(token["proposal_digest"])):
        raise DevelopmentError("development_activation_token_changed")
    root = _root(store)
    matches = [p / token["proposal_digest"] for p in root.iterdir()
               if task_history._TASK.fullmatch(p.name) and p.is_dir()
               and not task_history._linked(p) and
               (p / token["proposal_digest"] / "proposal.json").is_file()]
    if len(matches) != 1:
        raise DevelopmentError("development_activation_token_changed")
    path = matches[0]
    original_task = task_history._base(base) / path.parent.name
    original, instruction, request_digest = _request(original_task, base)
    _, proposal = _proposal(original, instruction, request_digest, store, path.name)
    if (proposal["manifest"]["id"] != skill_id or
            proposal["candidate_sha256"] != digest):
        raise DevelopmentError("development_activation_token_changed")
    source = proposal["source_artifact"]
    if source.get("kind") != "external_office_snapshot":
        return None
    trial_task = _trial_root(path, base)
    expected = {"task_root": source["source_task"], "token": source["gap_token"]}
    if (trial_task is None or _read(path / "trial-intent.json") !=
            {"schema": 2, "task_root": str(trial_task), "source_retry": expected}):
        raise DevelopmentError("development_trial_source_changed")
    return str(trial_task), expected


def _run_record_path(proposal, store, task_name):
    if not task_history._TASK.fullmatch(task_name):
        raise DevelopmentError("development_active_evidence_invalid")
    return Path(store).absolute() / proposal["manifest"]["id"] / "active-runs" / (task_name + ".json")


def _check_active_success(proposal, base, store, evidence, nonce):
    if (type(evidence) is not dict or set(evidence) !=
            {"task_root", "task_result_sha256", "run_record_sha256"}):
        raise DevelopmentError("development_active_evidence_invalid")
    task = task_history._task(evidence["task_root"], task_history._base(base))
    record_path = _run_record_path(proposal, store, task.name)
    record = _read(record_path)
    if sha256(record_path.read_bytes()).hexdigest() != evidence["run_record_sha256"]:
        raise DevelopmentError("development_active_evidence_changed")
    if record != {"schema": 1, "task_root": str(task),
                   "candidate_sha256": proposal["candidate_sha256"],
                   "implementation_sha256": proposal["implementation_sha256"],
                   "nonce": nonce, "result_sha256": evidence["task_result_sha256"]}:
        raise DevelopmentError("development_active_evidence_invalid")
    result_path = task / "result.json"
    if sha256(result_path.read_bytes()).hexdigest() != evidence["task_result_sha256"]:
        raise DevelopmentError("development_active_evidence_changed")
    result = skills._verify_success(task, base, proposal["candidate_sha256"])
    info = result["skill"]
    if (result["state"] != "succeeded" or info.get("mode") != "active"
            or info.get("id") != proposal["manifest"]["id"]
            or info.get("implementation_sha256") != proposal["implementation_sha256"]):
        raise DevelopmentError("development_active_evidence_invalid")
    return evidence


def _active_success(proposal, base, store, nonce):
    runs = Path(store).absolute() / proposal["manifest"]["id"] / "active-runs"
    if not runs.is_dir() or task_history._linked(runs):
        raise DevelopmentError("development_active_success_required")
    for path in sorted(runs.glob("task-*.json"), key=lambda p: p.name, reverse=True):
        try:
            record = _read(path)
            evidence = {"task_root": record["task_root"],
                        "task_result_sha256": record["result_sha256"],
                        "run_record_sha256": sha256(path.read_bytes()).hexdigest()}
            return _check_active_success(proposal, base, store, evidence, nonce)
        except (ValueError, OSError, TypeError, KeyError):
            continue
    raise DevelopmentError("development_active_success_required")


def inspect(task_root, base, store, *, digest=None):
    task, instruction, _ = _request(task_root, base)
    gap = _read(task / "capability-gap.json")
    try:
        external_source = _source_reference(task, base).get("kind") == "external_office_snapshot"
    except (ValueError, OSError, TypeError, KeyError):
        external_source = False
    proposals = []
    for version in ([digest] if digest else _versions(task, store)):
        try:
            _, path, proposal = _selected(task, base, store, version)
            stages = {"offline": False, "review": False, "live": False, "activation": False}
            for key, filename in (("offline", "offline.json"), ("review", "review.json"), ("live", "live.json")):
                try:
                    _gate(store, proposal, filename)
                    if key == "live":
                        skill_path, _ = skills._version(store, proposal["manifest"]["id"], proposal["candidate_sha256"])
                        skills._live_check(skill_path, proposal["candidate_sha256"], base)
                    stages[key] = True
                except (ValueError, OSError, TypeError, KeyError):
                    pass
            gate_dir = skills._version(store, proposal["manifest"]["id"], proposal["candidate_sha256"])[0]
            stale_gate = any((gate_dir / filename).exists() and not stages[key]
                             for key, filename in (("offline", "offline.json"),
                                                   ("review", "review.json"), ("live", "live.json")))
            trial_state, trial_task, resume, artifacts = _trial_state(path, proposal, base, store)
            active = _read(path / "activation.json") if (path / "activation.json").exists() else None
            if active:
                token = _read(Path(store).absolute() / proposal["manifest"]["id"] /
                              "development-activation.json")
                if token != {"schema": 1, "candidate_sha256": proposal["candidate_sha256"],
                              "implementation_sha256": proposal["implementation_sha256"],
                              "nonce": active.get("nonce"), "proposal_digest": version}:
                    raise DevelopmentError("development_activation_token_changed")
            if active and active.get("ceiling") == 3:
                _check_active_success(proposal, base, store, active.get("active_success"), active.get("nonce"))
            pending_activation = ((path / "activation-1-intent.json").exists() and not active
                                  or (path / "activation-3-intent.json").exists()
                                  and (not active or active.get("ceiling") != 3))
            skill_state = skills.status(store, proposal["manifest"]["id"], base)
            stages["activation"] = bool(active and active.get("candidate_sha256") == proposal["candidate_sha256"]
                                        and skill_state["active"] == proposal["candidate_sha256"]
                                        and skill_state["enabled"])
            next_actions = []
            if stale_gate:
                trial_state = "evidence_stale"
            elif pending_activation:
                trial_state = "activation_unknown"
            elif not stages["offline"]:
                next_actions.append("test")
            elif not stages["review"]:
                next_actions.append("review_cli")
            elif trial_state == "not_started":
                next_actions.append("trial")
            elif trial_state == "resumable":
                next_actions.append("resume")
            elif trial_state == "succeeded" and not stages["live"]:
                next_actions.append("verify_live")
            elif stages["live"] and not active:
                next_actions.append("activate_one")
            elif stages["live"] and active and active.get("ceiling") == 1:
                try:
                    _active_success(proposal, base, store, active.get("nonce"))
                    next_actions.append("activate_three")
                except (ValueError, OSError, TypeError, KeyError):
                    pass
            proposals.append({"digest": version, "proposal_digest": version,
                              "state": trial_state, "stages": stages,
                              "next_actions": next_actions, "actions": next_actions,
                              "manifest": proposal["manifest"],
                              "effect": proposal["effect"], "source_artifact": proposal["source_artifact"],
                              "trial_task": trial_task, "resume": resume, "artifacts": artifacts,
                              "remaining_runs": skill_state["remaining_runs"] if stages["activation"] else 0,
                              "activation_ceiling": active.get("ceiling") if active else 0})
        except (ValueError, OSError, TypeError, KeyError):
            proposals.append({"digest": version, "proposal_digest": version,
                              "state": "needs_review", "stages": {},
                              "next_actions": [], "actions": [], "artifacts": []})
    return {"task_root": str(task), "instruction": instruction,
            "external_proposal_available": external_source,
            "reason": gap.get("model_reason", ""), "state": "queued", "versions": _versions(task, store),
            "proposals": proposals, "supported_now": bool(_OFFICE.search(instruction) and not _DEVICE.search(instruction))}


def test(task_root, base, store, digest):
    with workflow._owner(_root(store)):
        return _test_locked(task_root, base, store, digest)


def _test_locked(task_root, base, store, digest):
    _, path, proposal = _selected(task_root, base, store, digest)
    gate = skills._version(store, proposal["manifest"]["id"], proposal["candidate_sha256"])[0] / "offline.json"
    if gate.exists():
        return _gate(store, proposal, "offline.json")
    return skills.verify_offline(store, proposal["manifest"]["id"], proposal["candidate_sha256"])


def review(task_root, base, store, digest, *, reviewer, note):
    with workflow._owner(_root(store)):
        return _review_locked(task_root, base, store, digest, reviewer=reviewer, note=note)


def _review_locked(task_root, base, store, digest, *, reviewer, note):
    _, _, proposal = _selected(task_root, base, store, digest)
    if type(reviewer) is not str or reviewer.strip().casefold() == proposal["proposer"].casefold():
        raise DevelopmentError("development_independent_review_required")
    _gate(store, proposal, "offline.json")
    gate = skills._version(store, proposal["manifest"]["id"], proposal["candidate_sha256"])[0] / "review.json"
    if gate.exists():
        return _gate(store, proposal, "review.json")
    return skills.record_review(store, proposal["manifest"]["id"], proposal["candidate_sha256"], reviewer, note)


def _with_stop(stop, path, action):
    if stop is None:
        return action()
    done = threading.Event()
    def watcher():
        while not done.wait(.1):
            if stop():
                if path.is_dir():
                    (path / "cancel.flag").touch(exist_ok=True)
                return
    thread = threading.Thread(target=watcher, daemon=True)
    thread.start()
    try:
        return action()
    finally:
        done.set()
        thread.join(timeout=1)


def trial(task_root, base, store, digest, *, stop=None, pause_after_step=None):
    with workflow._owner(_root(store)):
        return _trial_locked(task_root, base, store, digest, stop=stop,
                             pause_after_step=pause_after_step)


def _trial_locked(task_root, base, store, digest, *, stop=None, pause_after_step=None):
    _, path, proposal = _selected(task_root, base, store, digest)
    _gate(store, proposal, "offline.json")
    _gate(store, proposal, "review.json")
    previous = _trial_root(path, base)
    source = proposal["source_artifact"]
    if previous is not None and source.get("kind") == "external_office_snapshot":
        expected = {"schema": 2, "task_root": str(previous),
                    "source_retry": {"task_root": source["source_task"],
                                     "token": source["gap_token"]}}
        if _read(path / "trial-intent.json") != expected:
            raise DevelopmentError("development_trial_source_changed")
    if previous is not None:
        state, _, _, _ = _trial_state(path, proposal, base, store)
        if state == "succeeded":
            return _read(previous / "result.json")
        if state != "interrupted" or previous.exists():
            raise DevelopmentError("development_trial_already_claimed")
        # An intent without its task directory has no local effect. Reuse its
        # exact pinned root after the source and gates above were rechecked.
        trial_task = previous
    else:
        trial_task = task_history._base(base) / ("task-" + uuid.uuid4().hex)
        source = proposal["source_artifact"]
        intent = {"schema": 1, "task_root": str(trial_task)}
        if source.get("kind") == "external_office_snapshot":
            intent = {"schema": 2, "task_root": str(trial_task),
                      "source_retry": {"task_root": source["source_task"],
                                       "token": source["gap_token"]}}
        workflow._write_new(path / "trial-intent.json", intent)
    source = proposal["source_artifact"]
    source_arg = ({"source_retry": {"task_root": source["source_task"], "token": source["gap_token"]},
                   "source_instruction": _request(source["source_task"], base)[1]}
                  if source.get("kind") == "external_office_snapshot" else
                  {"source_artifact": source})
    return _with_stop(stop, trial_task, lambda: skills.run_skill(
        store, proposal["manifest"]["id"], trial_task, base,
        trial_digest=proposal["candidate_sha256"],
        expected_digest=proposal["candidate_sha256"], pause_after_step=pause_after_step,
        **source_arg))


def resume(task_root, base, store, digest, *, stop=None):
    with workflow._owner(_root(store)):
        return _resume_locked(task_root, base, store, digest, stop=stop)


def _resume_locked(task_root, base, store, digest, *, stop=None):
    _, path, proposal = _selected(task_root, base, store, digest)
    _gate(store, proposal, "review.json")
    state, trial_task, preview, _ = _trial_state(path, proposal, base, store)
    if state != "resumable":
        raise DevelopmentError("development_resume_unavailable")
    attempt_id = uuid.uuid4().hex
    attempt = Path(trial_task) / ("skill-resume-" + attempt_id)
    return _with_stop(stop, attempt, lambda: skill_resume.resume_task(
        trial_task, base, expected_token=preview["token"], attempt_id=attempt_id, store=store))


def verify_live(task_root, base, store, digest):
    with workflow._owner(_root(store)):
        return _verify_live_locked(task_root, base, store, digest)


def _verify_live_locked(task_root, base, store, digest):
    _, path, proposal = _selected(task_root, base, store, digest)
    _gate(store, proposal, "review.json")
    state, trial_task, _, _ = _trial_state(path, proposal, base, store)
    if state != "succeeded":
        raise DevelopmentError("development_live_trial_required")
    gate_dir = skills._version(store, proposal["manifest"]["id"], proposal["candidate_sha256"])[0]
    if (gate_dir / "live.json").exists():
        return skills._live_check(gate_dir, proposal["candidate_sha256"], base)
    return skills.record_live(store, proposal["manifest"]["id"], proposal["candidate_sha256"], trial_task, base)


def activate(task_root, base, store, digest, *, runs=1):
    _, path, proposal = _selected(task_root, base, store, digest)
    if runs not in {1, 3} or type(runs) is not int:
        raise DevelopmentError("development_activation_limit_invalid")
    verify_live(task_root, base, store, digest)
    with workflow._owner(_root(store)):
        record_path = path / "activation.json"
        current = _read(record_path) if record_path.exists() else None
        skill_id, candidate = proposal["manifest"]["id"], proposal["candidate_sha256"]
        state = skills.status(store, skill_id, base)
        if current:
            if current.get("candidate_sha256") != candidate or current.get("ceiling") not in {1, 3}:
                raise DevelopmentError("development_activation_record_invalid")
            if (type(current.get("nonce")) is not str
                    or not re.fullmatch(r"[0-9a-f]{32}", current["nonce"])):
                raise DevelopmentError("development_activation_record_invalid")
            if state["active"] != candidate or candidate in state["blocked"] or state["inflight"]:
                raise DevelopmentError("development_activation_stale")
            if current["ceiling"] == 3:
                _check_active_success(proposal, base, store, current.get("active_success"), current["nonce"])
            if runs <= current["ceiling"]:
                return state
        elif runs != 1:
            raise DevelopmentError("development_activate_one_first")
        intent_path = path / ("activation-" + str(runs) + "-intent.json")
        if intent_path.exists():
            raise DevelopmentError("development_activation_unknown")
        nonce = current["nonce"] if current else uuid.uuid4().hex
        token_path = Path(store).absolute() / skill_id / "development-activation.json"
        if current:
            token = _read(token_path)
            if token != {"schema": 1, "candidate_sha256": candidate,
                          "implementation_sha256": proposal["implementation_sha256"],
                          "nonce": nonce, "proposal_digest": digest}:
                raise DevelopmentError("development_activation_token_changed")
        active_evidence = _active_success(proposal, base, store, nonce) if runs == 3 else None
        workflow._write_new(intent_path, {"schema": 1, "candidate_sha256": candidate,
                                          "requested_ceiling": runs,
                                          "active_success": active_evidence, "nonce": nonce})
        # A crash after this claim never grants a second allocation. The
        # maintainer must inspect local active state and create a new version.
        remaining = 1 if runs == 1 else state["remaining_runs"] + 2
        updated = skills.activate(store, skill_id, candidate, base, runs=remaining)
        if runs == 1:
            skills._record(token_path, {"schema": 1, "candidate_sha256": candidate,
                                        "implementation_sha256": proposal["implementation_sha256"],
                                        "nonce": nonce, "proposal_digest": digest})
        skills._record(record_path, {"schema": 1, "candidate_sha256": candidate,
                                     "ceiling": runs, "active_success": active_evidence,
                                     "nonce": nonce})
        return updated


def suspend(task_root, base, store, digest):
    with workflow._owner(_root(store)):
        return _suspend_locked(task_root, base, store, digest)


def _suspend_locked(task_root, base, store, digest):
    _, _, proposal = _selected(task_root, base, store, digest)
    skill_id, candidate = proposal["manifest"]["id"], proposal["candidate_sha256"]
    root = skills._store(store)
    with workflow._owner(root):
        state = skills._state(root, skill_id)
        if state["inflight"]:
            raise DevelopmentError("development_effect_unknown")
        if state["active"] not in {None, candidate}:
            raise DevelopmentError("development_other_version_active")
        if state["active"] == candidate:
            state.update(active=None, remaining_runs=0)
            state["blocked"] = list(dict.fromkeys([*state["blocked"], candidate]))
            skills._record(root / skill_id / "active.json", state)
        return dict(state, enabled=False)
