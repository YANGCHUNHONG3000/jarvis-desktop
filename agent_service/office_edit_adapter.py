"""Reviewed, limited activation of the existing supervised Office edit contract.

Candidate and run records are append only. A pending intent is deliberately not
retried: the normal interaction recovery verifier decides whether it is safe.
"""
from hashlib import sha256
import math
from pathlib import Path
import re
import tempfile
import time
import zipfile

from . import office_workflow as workflow
from .computer import ROOT, ComputerSession, digest, read_record, safe_root, write_new


class AdapterError(ValueError):
    pass


_ID = "office-copy-edit-v1"
DEFAULT_STORE = ROOT / "data" / "office_edit_adapters"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SOURCES = ("office_edit.py", "office_edit_host.py", "office_intent.py", "action_choice_transport.py",
            "office_scope.py", "computer_agent.py", "computer_interaction.py",
            "interaction_recovery.py", "office_handoff.py", "artifact_source.py",
            "edited_artifact_source.py", "office_source.py", "workbook_source.py",
            "office.py", "office_workflow.py", "computer.py", "computer_goals.py",
            "computer_agent_tools.py", "computer_goal_adapters.py", "run_lifecycle.py",
            "office_edit_adapter.py", "office_edit_adapter_panel.py", "computer_goal_panel.py")


def implementation_sha256():
    root = Path(__file__).resolve().parent
    files = {name: sha256((root / name).read_bytes()).hexdigest() for name in _SOURCES}
    cli = root.parent / "run_jarvis_edit_adapter.py"
    files["../run_jarvis_edit_adapter.py"] = sha256(cli.read_bytes()).hexdigest() if cli.exists() else None
    return digest(files)


def _manifest():
    return {"schema": 1, "id": _ID, "implementation_sha256": implementation_sha256(),
            "contract": "supervised_copied_docx_xlsx_literal_edits_v1",
            "kinds": ["document", "spreadsheet"], "max_files": 4,
            "max_changes_total": 20, "native_receipt_required": True}


def current_candidate():
    return digest(_manifest())


def _store(store, create=False):
    path = safe_root(store)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise AdapterError("edit_adapter_store_missing")
    return path


def _candidate(store, candidate):
    if type(candidate) is not str or not _SHA.fullmatch(candidate):
        raise AdapterError("edit_adapter_identity_invalid")
    base = _store(store)
    root = safe_root(base / candidate)
    if root.parent != base:
        raise AdapterError("edit_adapter_store_changed")
    if not root.is_dir() or read_record(root / "candidate.json") != _manifest() or digest(_manifest()) != candidate:
        raise AdapterError("edit_adapter_changed")
    return root


def _plan(session, plan, *, recovery=False):
    from .computer_interaction import validate_goal
    from .office_edit import _bind
    if (type(plan) is not dict or type(plan.get("sha256")) is not str
            or not _SHA.fullmatch(plan["sha256"])):
        raise AdapterError("edit_adapter_plan_invalid")
    key = plan["sha256"]
    if (read_record(session.root / "goals" / (key + ".json")) != plan
            or digest({k: v for k, v in plan.items() if k != "sha256"}) != key
            or plan.get("mode") != "agent" or ("recovery" in plan) != recovery):
        raise AdapterError("edit_adapter_plan_changed")
    goal = validate_goal(plan.get("goal"))
    if (goal.get("browser") is not None or goal.get("repository_document")
            or goal.get("page_office") or not goal["edits"]):
        raise AdapterError("edit_adapter_scope_invalid")
    for edit in goal["edits"]:
        root = safe_root(edit["source_root"])
        if root.parent != safe_root(session.root / "edit-inputs") or edit["after_browser"]:
            raise AdapterError("edit_adapter_source_invalid")
        _bind(edit["spec"], root)
    if sum(len(edit["spec"]["changes"]) for edit in goal["edits"]) > 20:
        raise AdapterError("edit_adapter_change_limit")
    return goal


def ensure_candidate(store, session, plan, *, proposer):
    """Register the immutable implementation profile after checking a real plan."""
    _plan(session, plan)
    if type(proposer) is not str or not 1 <= len(proposer.strip()) <= 100:
        raise AdapterError("edit_adapter_proposer_invalid")
    manifest = _manifest()
    key = digest(manifest)
    base = _store(store, create=True)
    with workflow._owner(base):
        root = base / key
        if root.exists():
            _candidate(base, key)
        else:
            root.mkdir()
            write_new(root / "candidate.json", manifest)
            write_new(root / "proposal.json", {"candidate": key, "proposer": proposer.strip(),
                                                  "origin_plan_sha256": plan["sha256"]})
    return status(base, key)


def _fixture():
    """Exercise the actual copy and independent preservation checks offline."""
    from openpyxl import Workbook
    from . import office_edit as edit
    from .office_edit import W
    with tempfile.TemporaryDirectory(prefix="jarvis-edit-adapter-") as folder:
        base = Path(folder)
        word = base / "source.docx"
        with zipfile.ZipFile(word, "w", compression=zipfile.ZIP_DEFLATED) as out:
            out.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            out.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            out.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>'
                '<w:p><w:r><w:t>prefix </w:t></w:r>'
                '<w:r><w:rPr><w:b/></w:rPr><w:t>before</w:t></w:r>'
                '<w:r><w:t> suffix</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>keep</w:t></w:r></w:p>'
                '<w:tbl><w:tblGrid><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/></w:tblGrid>'
                '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
                '<w:p><w:r><w:t>merged cell stays</w:t></w:r></w:p>'
                '<w:tbl><w:tblGrid><w:gridCol w:w="1000"/></w:tblGrid>'
                '<w:tr><w:tc><w:p><w:r><w:t>nested cell stays</w:t></w:r></w:p>'
                '</w:tc></w:tr></w:tbl><w:p/></w:tc></w:tr></w:tbl>'
                '</w:body></w:document>')
        book_path = base / "source.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "Inputs"
        sheet["A1"] = 2
        sheet["A2"] = "=SUM(A1:A1)"
        book.create_sheet("Keep")["A1"] = "unchanged"
        book.save(book_path)
        book.close()
        results = []
        for source, change in ((word, {"paragraph_index": 0, "before": "prefix before suffix", "after": "prefix after suffix"}),
                               (book_path, {"sheet": "Inputs", "cell": "A1", "before": 2, "after": 3})):
            source_root = base / ("input-" + source.suffix[1:])
            source_root.mkdir()
            public = edit.snapshot_edit_source(source, source_root)
            spec = {"kind": public["kind"], "source_id": "selected-edit",
                    "source_sha256": public["sha256"], "changes": [change]}
            edit._bind(spec, source_root)
            copy = base / ("copy" + source.suffix)
            edit._make_copy(spec, source_root / ("source" + source.suffix), copy,
                            public.get("observation_version", 1))
            edit.verify_preservation(source, copy, spec, public.get("observation_version", 1))
            edit.verify_edit_source(source_root)
            results.append({"kind": public["kind"], "source_sha256": public["sha256"],
                            "copy_sha256": edit._hash(copy)})
        return results


def offline(store, candidate):
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        path = root / "offline.json"
        if path.exists():
            return read_record(path)
        checked = _fixture()
        result = {"candidate": candidate, "passed": True, "fixtures": checked}
        write_new(path, result)
        return result


def record_review(store, candidate, *, reviewer, note):
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        proposal = read_record(root / "proposal.json")
        if (not (root / "offline.json").exists() or type(reviewer) is not str
                or not reviewer.strip() or reviewer.strip() == proposal["proposer"]
                or type(note) is not str or not 1 <= len(note.strip()) <= 2000):
            raise AdapterError("edit_adapter_review_invalid")
        record = {"candidate": candidate, "reviewer": reviewer.strip(),
                  "note": note.strip(), "offline_sha256": digest(read_record(root / "offline.json")),
                  "proposal_sha256": digest(proposal)}
        path = root / "review.json"
        if path.exists():
            if read_record(path) != record:
                raise AdapterError("edit_adapter_review_changed")
        else:
            write_new(path, record)
        return record


def _gates(root):
    offline_record = read_record(root / "offline.json")
    review = read_record(root / "review.json")
    proposal = read_record(root / "proposal.json")
    if (offline_record.get("passed") is not True or offline_record.get("candidate") != root.name
            or type(offline_record.get("fixtures")) is not list
            or any(type(v) is not dict for v in offline_record["fixtures"])
            or [v.get("kind") for v in offline_record["fixtures"]] != ["document", "spreadsheet"]
            or any(not _SHA.fullmatch(str(v.get("source_sha256")))
                   or not _SHA.fullmatch(str(v.get("copy_sha256")))
                   for v in offline_record["fixtures"])
            or review.get("candidate") != root.name
            or review.get("offline_sha256") != digest(offline_record)
            or review.get("proposal_sha256") != digest(proposal)
            or proposal.get("candidate") != root.name
            or review.get("reviewer") == proposal.get("proposer")):
        raise AdapterError("edit_adapter_gate_changed")


def _proof(root, path, intent):
    saved = read_record(path)
    if intent.get("candidate") != root.name:
        raise AdapterError("edit_adapter_proof_changed")
    if saved.get("plan_sha256") is None or saved.get("run") != str(
            Path(intent["session_root"]) / "goals" / (saved["plan_sha256"] + ".run")):
        raise AdapterError("edit_adapter_proof_changed")
    session = ComputerSession(intent["session_root"])
    plan = read_record(session.root / "goals" / (saved["plan_sha256"] + ".json"))
    _plan(session, plan, recovery="recovery" in plan)
    origin = plan
    depth = 0
    while "recovery" in origin:
        depth += 1
        if depth > 3:
            raise AdapterError("edit_adapter_recovery_changed")
        parent = Path(origin["recovery"]["parent_run"])
        if parent.parent != session.root / "goals":
            raise AdapterError("edit_adapter_recovery_changed")
        origin = read_record(parent.with_suffix(".json"))
    if origin["sha256"] != intent["plan_sha256"]:
        raise AdapterError("edit_adapter_recovery_changed")
    actual = _verified(session, plan, read_record(Path(saved["run"]) / "outcome.json"))
    if saved != actual:
        raise AdapterError("edit_adapter_proof_changed")
    return saved


def _stage_records(root, prefix, intent):
    """Bind every stored result to its exact agent run; reject orphan receipts."""
    if intent.get("candidate") != root.name or not _SHA.fullmatch(str(intent.get("plan_sha256"))):
        raise AdapterError("edit_adapter_records_changed")
    session_root = safe_root(intent["session_root"])
    if "adoption" in intent:
        if not prefix.startswith("active-"):
            raise AdapterError("edit_adapter_adoption_changed")
        _adoption_binding(root, intent, prefix)
    elif prefix.startswith("active-"):
        if (root / (prefix + "-adoption.json")).exists():
            raise AdapterError("edit_adapter_adoption_changed")
        for request_path in root.glob("adoption-*-request.json"):
            request_id = request_path.name[len("adoption-"):-len("-request.json")]
            request = _adoption_request(root, request_id)
            if (request.get("session_root") == intent["session_root"]
                    and request.get("origin_plan_sha256") == intent["plan_sha256"]):
                raise AdapterError("edit_adapter_adoption_changed")
    proof_path = root / (prefix + "-proof.json")
    saved_proof = read_record(proof_path) if proof_path.exists() else None
    initial_path = root / (prefix + "-result.json")
    if saved_proof is not None and not initial_path.exists():
        raise AdapterError("edit_adapter_records_changed")

    def checked(path, plan_sha):
        if not path.exists():
            return None
        value = read_record(path)
        run = session_root / "goals" / (plan_sha + ".run")
        outcome = read_record(run / "outcome.json")
        if (value.get("outcome") != outcome or outcome.get("run") != str(run)
                or value.get("proof") not in (None, saved_proof)):
            raise AdapterError("edit_adapter_records_changed")
        if prefix == "trial" and path == initial_path and value.get("plan_sha256") != plan_sha:
            raise AdapterError("edit_adapter_records_changed")
        return value

    initial = checked(initial_path, intent["plan_sha256"])
    resumes = sorted(root.glob(prefix + "-resume-*-intent.json"),
                     key=lambda p: int(p.name.split("-")[-2]))
    if [int(p.name.split("-")[-2]) for p in resumes] != list(range(1, len(resumes) + 1)):
        raise AdapterError("edit_adapter_records_changed")
    for path in root.glob(prefix + "-resume-*-result.json"):
        if not (root / path.name.replace("-result.json", "-intent.json")).exists():
            raise AdapterError("edit_adapter_records_changed")
    results = [initial] if initial is not None else []
    for path in resumes:
        child = read_record(path)
        if not _SHA.fullmatch(str(child.get("plan_sha256"))) or type(child.get("parent_run")) is not str:
            raise AdapterError("edit_adapter_records_changed")
        result = checked(root / path.name.replace("-intent.json", "-result.json"), child["plan_sha256"])
        if result is not None:
            results.append(result)
    success = [v for v in results if v["outcome"].get("ok") is True]
    if ((saved_proof is None) != (not success)
            or saved_proof is not None and (len(success) != 1 or success[0].get("proof") != saved_proof)
            or any(v["outcome"].get("ok") is not True and v.get("proof") is not None for v in results)):
        raise AdapterError("edit_adapter_records_changed")
    if saved_proof is not None:
        _proof(root, proof_path, intent)
    return {"proof": saved_proof, "initial": initial, "resumes": resumes,
            "complete": saved_proof is not None, "pending": saved_proof is None}


def _reconcile_stage(root, prefix, intent):
    """Finish bookkeeping from a terminal agent outcome; never dispatch an edit."""
    if intent.get("candidate") != root.name:
        raise AdapterError("edit_adapter_records_changed")
    session = ComputerSession(intent["session_root"])
    items = [(prefix, intent["plan_sha256"])]
    resumes = sorted(root.glob(prefix + "-resume-*-intent.json"),
                     key=lambda p: int(p.name.split("-")[-2]))
    items += [(path.name[:-12], read_record(path)["plan_sha256"]) for path in resumes]
    proof_path = root / (prefix + "-proof.json")
    changed = 0
    for name, plan_sha in items:
        run = session.root / "goals" / (plan_sha + ".run")
        outcome_path = run / "outcome.json"
        if not outcome_path.exists():
            continue
        outcome = read_record(outcome_path)
        if outcome.get("run") != str(run):
            raise AdapterError("edit_adapter_records_changed")
        plan = read_record(run.with_suffix(".json"))
        _plan(session, plan, recovery="recovery" in plan)
        if plan["sha256"] != plan_sha:
            raise AdapterError("edit_adapter_records_changed")
        proof = _verified(session, plan, outcome) if outcome.get("ok") is True else None
        if proof is not None and proof_path.exists() and read_record(proof_path) != proof:
            raise AdapterError("edit_adapter_proof_changed")
        result_path = root / (name + "-result.json")
        result = {"outcome": outcome, "proof": proof}
        if name == "trial":
            result["plan_sha256"] = plan_sha
        if result_path.exists():
            if read_record(result_path) != result:
                raise AdapterError("edit_adapter_records_changed")
        else:
            write_new(result_path, result)
            changed += 1
        if proof is not None and not proof_path.exists():
            write_new(proof_path, proof)
            changed += 1
    return changed


def reconcile(store, candidate):
    """Recover missing adapter receipts only from independently verified runs."""
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        changed = 0
        trial_intent = root / "trial-intent.json"
        if trial_intent.exists():
            changed += _reconcile_stage(root, "trial", read_record(trial_intent))
        for path in root.iterdir():
            if re.fullmatch(r"active-[1-9][0-9]*-intent\.json", path.name):
                prefix = path.name[:-12]
                intent = read_record(path)
                if "adoption" in intent and not (root / (prefix + "-adoption.json")).exists():
                    _adoption_binding(root, intent, prefix)
                    write_new(root / (prefix + "-adoption.json"), _adoption_index(root, intent, prefix))
                    changed += 1
                changed += _reconcile_stage(root, prefix, intent)
        _trial_stage(root)
        _active_records(root)
        return {"candidate": candidate, "records_finalized": changed}


def _active_records(root):
    """Count only numbered active slots and reject orphan or malformed stages."""
    names = {p.name for p in root.iterdir() if p.is_file()}
    slots = sorted(int(m.group(1)) for name in names
                   if (m := re.fullmatch(r"active-([1-9][0-9]*)-intent\.json", name)))
    if slots != list(range(1, len(slots) + 1)):
        raise AdapterError("edit_adapter_active_records_changed")
    for name in names:
        match = re.fullmatch(
            r"active-([1-9][0-9]*)-(?:intent|result|proof|adoption|resume-[1-9][0-9]*-(?:intent|result))\.json",
            name)
        if match and int(match.group(1)) not in slots:
            raise AdapterError("edit_adapter_active_records_changed")
    records = []
    for n in slots:
        intent = read_record(root / f"active-{n}-intent.json")
        if (intent.get("candidate") != root.name or not _SHA.fullmatch(str(intent.get("plan_sha256")))
                or type(intent.get("session_root")) is not str):
            raise AdapterError("edit_adapter_active_records_changed")
        stage = _stage_records(root, f"active-{n}", intent)
        records.append({"index": n, "intent": intent, "complete": stage["complete"],
                        "pending": stage["pending"]})
    return records


def _adoption_path(root, request_id, suffix):
    if type(request_id) is not str or not _SHA.fullmatch(request_id):
        raise AdapterError("edit_adapter_adoption_invalid")
    return root / f"adoption-{request_id}-{suffix}.json"


def _adoption_request(root, request_id):
    request = read_record(_adoption_path(root, request_id, "request"))
    if digest(request) != request_id or request.get("candidate") != root.name:
        raise AdapterError("edit_adapter_adoption_changed")
    return request


def _adoption_review(root, request_id):
    request = _adoption_request(root, request_id)
    path = _adoption_path(root, request_id, "review")
    if not path.exists():
        raise AdapterError("edit_adapter_adoption_review_required")
    review = read_record(path)
    if (set(review) != {"candidate", "request_sha256", "reviewer", "note"}
            or review.get("request_sha256") != request_id or review.get("candidate") != root.name
            or review.get("reviewer") == request.get("proposer")
            or type(review.get("reviewer")) is not str or not review["reviewer"].strip()
            or type(review.get("note")) is not str or not review["note"].strip()):
        raise AdapterError("edit_adapter_adoption_review_changed")
    return request


def _adoption_parent(session, plan):
    """Accept only an unclaimed, depth-zero pure Office continuation."""
    from .interaction_recovery import _inspect, _run_root, validate
    _plan(session, plan, recovery=True)
    metadata = plan["recovery"]
    if (metadata.get("depth") != 1 or "kind" in metadata
            or type(metadata.get("parent_run")) is not str):
        raise AdapterError("edit_adapter_adoption_scope_invalid")
    parent = _run_root(session, metadata["parent_run"])
    origin = read_record(parent.with_suffix(".json"))
    if "recovery" in origin:
        raise AdapterError("edit_adapter_adoption_scope_invalid")
    _plan(session, origin)
    validate(session, plan)
    proof = _inspect(session, parent)
    if (proof["depth"] != 0 or proof["browser_checkpoint"] is not None
            or proof["browser_resume"] is not None or not proof["remaining"]
            or proof["goal"] != plan["goal"]
            or proof["evidence_sha256"] != metadata["parent_evidence_sha256"]):
        raise AdapterError("edit_adapter_adoption_scope_invalid")
    return origin, proof


def prepare_adoption(store, candidate, session, plan, *, proposer):
    """Persist the exact legacy binding proposal without taking a quota slot."""
    if type(proposer) is not str or not 1 <= len(proposer.strip()) <= 100:
        raise AdapterError("edit_adapter_proposer_invalid")
    origin, proof = _adoption_parent(session, plan)
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        limit = _activation_limit(root)
        _trial_stage(root)
        active = _active_records(root)
        if any(item["pending"] for item in active):
            raise AdapterError("edit_adapter_active_pending")
        if len(active) >= limit:
            raise AdapterError("edit_adapter_limit_reached")
        _adoption_duplicate(base, session, origin["sha256"])
        request = {"schema": 1, "candidate": candidate, "session_root": str(session.root),
                   "origin_plan_sha256": origin["sha256"], "parent_run": proof["run"],
                   "parent_evidence_sha256": proof["evidence_sha256"],
                   "completed_edits": proof["done"], "remaining_edits": proof["remaining"],
                   "artifacts": proof["artifacts"],
                   "proposer": proposer.strip()}
        request_id = digest(request)
        path = _adoption_path(root, request_id, "request")
        if path.exists():
            if read_record(path) != request:
                raise AdapterError("edit_adapter_adoption_changed")
        else:
            write_new(path, request)
        return {"request_sha256": request_id, "request": request}


def record_adoption_review(store, candidate, request_id, *, reviewer, note):
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        request = _adoption_request(root, request_id)
        if (type(reviewer) is not str or not reviewer.strip()
                or reviewer.strip() == request["proposer"]
                or type(note) is not str or not 1 <= len(note.strip()) <= 2000):
            raise AdapterError("edit_adapter_adoption_review_invalid")
        review = {"candidate": candidate, "request_sha256": request_id,
                  "reviewer": reviewer.strip(), "note": note.strip()}
        path = _adoption_path(root, request_id, "review")
        if path.exists():
            if read_record(path) != review:
                raise AdapterError("edit_adapter_adoption_review_changed")
        else:
            write_new(path, review)
        return review


def _adoption_index(root, intent, prefix):
    return {"candidate": root.name, "slot": int(prefix.split("-")[1]),
            "request_sha256": intent["adoption"],
            "origin_plan_sha256": intent["plan_sha256"],
            "review_sha256": intent["adoption_review_sha256"]}


def _adoption_binding(root, intent, prefix):
    from .interaction_recovery import _inspect
    request_id = intent["adoption"]
    request = _adoption_review(root, request_id)
    if (intent.get("candidate") != root.name
            or intent.get("plan_sha256") != request["origin_plan_sha256"]
            or intent.get("session_root") != request["session_root"]
            or intent.get("adoption_review_sha256") != digest(read_record(
                _adoption_path(root, request_id, "review")))):
        raise AdapterError("edit_adapter_adoption_changed")
    sidecar = root / (prefix + "-adoption.json")
    if sidecar.exists() and read_record(sidecar) != _adoption_index(root, intent, prefix):
        raise AdapterError("edit_adapter_adoption_changed")
    session = ComputerSession(request["session_root"])
    resumes = sorted(root.glob(prefix + "-resume-*-intent.json"),
                     key=lambda p: int(p.name.split("-")[-2]))
    parent = Path(request["parent_run"])
    claim_path = parent / "continuation-claim.json"
    claimed = read_record(claim_path).get("child") if claim_path.exists() else None
    saved = [read_record(path) for path in resumes]
    if claimed is not None:
        matches = [i for i, item in enumerate(saved) if item.get("plan_sha256") == claimed
                   and item.get("parent_run") == str(parent)]
        if len(matches) != 1:
            raise AdapterError("edit_adapter_adoption_changed")
        first_claimed = matches[0]
    else:
        first_claimed = len(saved)
    for path, item in zip(resumes[:first_claimed], saved[:first_claimed]):
        child_path = session.root / "goals" / (str(item.get("plan_sha256")) + ".run")
        if (item.get("parent_run") != str(parent) or child_path.exists()
                or (root / path.name.replace("-intent.json", "-result.json")).exists()):
            raise AdapterError("edit_adapter_adoption_changed")
    child = claimed
    proof = _inspect(session, request["parent_run"], child=child,
                     allow_unclaimed=False)
    if (proof["evidence_sha256"] != request["parent_evidence_sha256"]
            or proof["done"] != request["completed_edits"]
            or proof["remaining"] != request["remaining_edits"]
            or proof["artifacts"] != request["artifacts"]):
        raise AdapterError("edit_adapter_adoption_changed")
    return request


def _adoption_duplicate(base, session, origin_sha):
    """A missing or damaged old candidate cannot silently clear ownership."""
    for candidate_dir in base.iterdir():
        if candidate_dir.is_symlink() or (hasattr(candidate_dir, "is_junction")
                                          and candidate_dir.is_junction()):
            raise AdapterError("edit_adapter_adoption_store_changed")
        if not candidate_dir.is_dir():
            continue
        if (not _SHA.fullmatch(candidate_dir.name) or not (candidate_dir / "candidate.json").is_file()
                or digest(read_record(candidate_dir / "candidate.json")) != candidate_dir.name):
            raise AdapterError("edit_adapter_adoption_store_changed")
        names = {path.name for path in candidate_dir.iterdir() if path.is_file()}
        if any(name.startswith("trial-") and name.endswith(".json") for name in names) and "trial-intent.json" not in names:
            raise AdapterError("edit_adapter_adoption_store_changed")
        slots = sorted({int(match.group(1)) for name in names
                        if (match := re.fullmatch(r"active-([1-9][0-9]*)-.*\.json", name))})
        if slots != list(range(1, len(slots) + 1)):
            raise AdapterError("edit_adapter_adoption_store_changed")
        for n in slots:
            if f"active-{n}-intent.json" not in names:
                raise AdapterError("edit_adapter_adoption_store_changed")
        for name in names:
            if re.fullmatch(r"trial-resume-[1-9][0-9]*-result\.json", name):
                if name.replace("-result.json", "-intent.json") not in names:
                    raise AdapterError("edit_adapter_adoption_store_changed")
            if re.fullmatch(r"active-[1-9][0-9]*-resume-[1-9][0-9]*-result\.json", name):
                if name.replace("-result.json", "-intent.json") not in names:
                    raise AdapterError("edit_adapter_adoption_store_changed")
        active_intents = [path for path in candidate_dir.iterdir()
                          if re.fullmatch(r"active-[1-9][0-9]*-intent\.json", path.name)]
        for path in (candidate_dir / "trial-intent.json", *active_intents):
            if not path.is_file():
                continue
            intent = read_record(path)
            if (intent.get("candidate") != candidate_dir.name
                    or not _SHA.fullmatch(str(intent.get("plan_sha256")))
                    or type(intent.get("session_root")) is not str):
                raise AdapterError("edit_adapter_adoption_store_changed")
            if (intent.get("session_root") == str(session.root)
                    and intent.get("plan_sha256") == origin_sha):
                raise AdapterError("edit_adapter_adoption_already_owned")


def adopt(store, candidate, session, plan, request_id):
    """Bind one reviewed legacy origin to a normal numbered active slot."""
    _fresh(plan)
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        request = _adoption_review(root, request_id)
        origin, proof = _adoption_parent(session, plan)
        if (request.get("session_root") != str(session.root)
                or request.get("origin_plan_sha256") != origin["sha256"]
                or request.get("parent_run") != proof["run"]
                or request.get("parent_evidence_sha256") != proof["evidence_sha256"]
                or request.get("completed_edits") != proof["done"]
                or request.get("remaining_edits") != proof["remaining"]
                or request.get("artifacts") != proof["artifacts"]):
            raise AdapterError("edit_adapter_adoption_changed")
        limit = _activation_limit(root)
        active = _active_records(root)
        if any(item["pending"] for item in active):
            raise AdapterError("edit_adapter_active_pending")
        if len(active) >= limit:
            raise AdapterError("edit_adapter_limit_reached")
        _adoption_duplicate(base, session, origin["sha256"])
        n = len(active) + 1
        write_new(root / f"active-{n}-intent.json", {"candidate": candidate,
            "plan_sha256": origin["sha256"], "session_root": str(session.root),
            "adoption": request_id, "adoption_review_sha256": digest(read_record(
                _adoption_path(root, request_id, "review")))})
        intent = read_record(root / f"active-{n}-intent.json")
        write_new(root / f"active-{n}-adoption.json", _adoption_index(root, intent, f"active-{n}"))
        return {"candidate": candidate, "index": n, "request_sha256": request_id}


def _trial_stage(root):
    intent_path = root / "trial-intent.json"
    if not intent_path.exists():
        if any(path.is_file() and path.name.startswith("trial-") and path.suffix == ".json"
               for path in root.iterdir()):
            raise AdapterError("edit_adapter_records_changed")
        return None
    return _stage_records(root, "trial", read_record(intent_path))


def _activation_limit(root):
    stage = _trial_stage(root)
    if stage is None or not stage["complete"]:
        raise AdapterError("edit_adapter_trial_required")
    trial = stage["proof"]
    limit = 0
    first = root / "activation-1.json"
    if first.exists():
        if read_record(first) != {"candidate": root.name, "total_runs": 1,
                                  "trial_proof_sha256": digest(trial)}:
            raise AdapterError("edit_adapter_activation_changed")
        limit = 1
    third = root / "activation-3.json"
    if third.exists():
        if limit != 1 or read_record(third) != {"candidate": root.name, "total_runs": 3,
                                               "trial_proof_sha256": digest(trial)}:
            raise AdapterError("edit_adapter_activation_changed")
        if not any(item["complete"] for item in _active_records(root)):
            raise AdapterError("edit_adapter_active_success_required")
        limit = 3
    return limit


def _resume_parent(root, session, prefix, origin):
    intents = sorted(root.glob(prefix + "-resume-*-intent.json"),
                     key=lambda p: int(p.name.split("-")[-2]))
    if intents:
        last = read_record(intents[-1])
        child = session.root / "goals" / (last["plan_sha256"] + ".run")
        if child.exists():
            return str(child)
        if (root / intents[-1].name.replace("-intent.json", "-result.json")).exists():
            raise AdapterError("edit_adapter_recovery_changed")
        parent = Path(last["parent_run"])
        if parent.parent != session.root / "goals" or (parent / "continuation-claim.json").exists():
            raise AdapterError("edit_adapter_recovery_changed")
        # The durable intent preceded run ownership. No run means no dispatched
        # native effect, so prepare a fresh preview from the same proven parent.
        return str(parent)
    return str(session.root / "goals" / (origin["plan_sha256"] + ".run"))


def _verified(session, plan, outcome):
    """Check durable production run evidence and exact original plan outputs."""
    from .interaction_recovery import _inspect
    from .office_handoff import select_outputs
    from .computer_agent_tools import _load
    run = safe_root(session.root / "goals" / (plan["sha256"] + ".run"))
    if outcome != read_record(run / "outcome.json") or outcome.get("ok") is not True:
        raise AdapterError("edit_adapter_trial_unverified")
    proof = _inspect(session, run, successful=True)
    edits = plan["goal"]["edits"]
    if (proof["goal"] != plan["goal"] or len(proof["done"]) != len(edits)
            or set(proof["done"]) != {item["id"] for item in edits}
            or len(proof["artifacts"]) != len(edits)
            or outcome["artifacts"] != proof["artifacts"]):
        raise AdapterError("edit_adapter_trial_unverified")
    refs = select_outputs([{"path": a["path"], "sha256": a["sha256"]}
                           for a in proof["artifacts"]])
    if len(refs) != len(edits):
        raise AdapterError("edit_adapter_trial_unverified")
    for artifact in proof["artifacts"]:
        output = Path(artifact["path"]).parent
        manifest, _ = _load(output / "edit-manifest.json")
        record, _ = _load(Path(manifest["step"]) / "environment-result.json")
        native = record.get("native")
        expected = "KWPS.Application" if manifest["spec"]["kind"] == "document" else "KET.Application"
        if (type(native) is not dict or native.get("app", {}).get("progid") != expected
                or native.get("artifact") != artifact["path"]
                or read_record(output / "worker-request.json") != {
                    "spec": manifest["spec"], "source_root": manifest["source_root"]}
                or read_record(output / "worker-outcome.json") != native
                or read_record(output / "worker-ready.json") != {"ready": True}
                or read_record(output / "worker-gated.json") != {"gated": True}):
            raise AdapterError("edit_adapter_native_worker_unverified")
    return {"plan_sha256": plan["sha256"], "run": str(run), "artifacts": proof["artifacts"],
            "kinds": sorted({edit["spec"]["kind"] for edit in edits}),
            "artifacts_sha256": digest(proof["artifacts"]), "evidence_sha256": proof["evidence_sha256"]}


def refresh_plan(session, plan):
    """Renew the approval preview locally after TTL without another model call."""
    from .computer_agent import prepare
    _plan(session, plan)
    return prepare(session, plan["instruction"], plan["goal"])


def _fresh(plan):
    from .computer import TTL
    stamp = plan.get("prepared_at")
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= TTL:
        raise AdapterError("edit_adapter_preview_expired")


def _unused_plan(session, plan):
    if (session.root / "goals" / (plan["sha256"] + ".run")).exists():
        raise AdapterError("edit_adapter_plan_already_started")


def _run(session, plan, stop, progress, pause_after_step):
    from .computer_agent import execute
    return execute(session, plan, approval=plan["sha256"], stop=stop,
                   progress=progress, pause_after_step=pause_after_step)


def trial(store, candidate, session, plan, *, approval, stop=lambda: False,
          progress=lambda value: None, pause_after_step=lambda: False):
    if approval != plan.get("sha256"):
        raise AdapterError("edit_adapter_approval_required")
    goal = _plan(session, plan)
    _fresh(plan)
    _unused_plan(session, plan)
    if {edit["spec"]["kind"] for edit in goal["edits"]} != {"document", "spreadsheet"}:
        raise AdapterError("edit_adapter_trial_requires_both_kinds")
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        if _trial_stage(root) is not None:
            raise AdapterError("edit_adapter_trial_already_started")
        write_new(root / "trial-intent.json", {"candidate": candidate,
                                               "plan_sha256": plan["sha256"],
                                               "session_root": str(session.root)})
        outcome = _run(session, plan, stop, progress, pause_after_step)
        if outcome.get("ok") is True:
            proof = _verified(session, plan, outcome)
            write_new(root / "trial-proof.json", proof)
        else:
            proof = None
        write_new(root / "trial-result.json", {"plan_sha256": plan["sha256"],
                                                  "outcome": outcome, "proof": proof})
        return {"outcome": outcome, "proof": proof}


def owns_recovery(store, session, plan):
    """Identify an adapter-owned recovery chain for GUI routing."""
    if type(plan) is not dict or "recovery" not in plan:
        return None
    origin = plan
    try:
        for _ in range(4):
            if "recovery" not in origin:
                break
            parent = Path(origin["recovery"]["parent_run"])
            if parent.parent != session.root / "goals":
                return None
            origin = read_record(parent.with_suffix(".json"))
        else:
            return None
        original = origin["sha256"]
        for candidate_dir in _store(store).iterdir():
            if not candidate_dir.is_dir() or candidate_dir.is_symlink():
                continue
            active = [p for p in candidate_dir.iterdir() if re.fullmatch(
                r"active-[1-9][0-9]*-intent\.json", p.name)]
            for path in (candidate_dir / "trial-intent.json", *active):
                if path.is_file():
                    intent = read_record(path)
                    if intent.get("plan_sha256") == original and intent.get("session_root") == str(session.root):
                        return {"candidate": candidate_dir.name,
                                "phase": "trial" if path.name == "trial-intent.json" else "active",
                                "index": None if path.name == "trial-intent.json" else int(path.name.split("-")[1])}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def prepare_resume(store, candidate, session, *, active_index=None):
    """Return a reviewable recovery plan for trial or one consumed active run."""
    from .interaction_recovery import prepare
    reconcile(store, candidate)
    root = _candidate(store, candidate)
    _trial_stage(root)
    active = _active_records(root)
    if active_index is None:
        if (root / "trial-proof.json").exists():
            pending = [item["index"] for item in active if item["pending"]]
            if not pending:
                raise AdapterError("edit_adapter_resume_unavailable")
            active_index = max(pending)
    prefix = "trial" if active_index is None else f"active-{active_index}"
    intent = read_record(root / (prefix + "-intent.json"))
    if (root / (prefix + "-proof.json")).exists() or intent["session_root"] != str(session.root):
        raise AdapterError("edit_adapter_resume_unavailable")
    return prepare(session, _resume_parent(root, session, prefix, intent))


def resume(store, candidate, session, plan, *, approval, stop=lambda: False,
           progress=lambda value: None, pause_after_step=lambda: False):
    if approval != plan.get("sha256"):
        raise AdapterError("edit_adapter_approval_required")
    _plan(session, plan, recovery=True)
    _fresh(plan)
    _unused_plan(session, plan)
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        _trial_stage(root)
        _active_records(root)
        owner = owns_recovery(base, session, plan)
        if owner is None or owner["candidate"] != candidate:
            raise AdapterError("edit_adapter_recovery_unowned")
        prefix = "trial" if owner["phase"] == "trial" else f"active-{owner['index']}"
        intent = read_record(root / (prefix + "-intent.json"))
        if intent["session_root"] != str(session.root) or (root / (prefix + "-proof.json")).exists():
            raise AdapterError("edit_adapter_resume_unavailable")
        parent = _resume_parent(root, session, prefix, intent)
        if plan["recovery"]["parent_run"] != parent:
            raise AdapterError("edit_adapter_recovery_changed")
        n = len(list(root.glob(prefix + "-resume-*-intent.json"))) + 1
        write_new(root / f"{prefix}-resume-{n}-intent.json", {"parent_run": parent,
                                                               "plan_sha256": plan["sha256"]})
        outcome = _run(session, plan, stop, progress, pause_after_step)
        proof = _verified(session, plan, outcome) if outcome.get("ok") is True else None
        write_new(root / f"{prefix}-resume-{n}-result.json", {"outcome": outcome, "proof": proof})
        if proof is not None:
            write_new(root / (prefix + "-proof.json"), proof)
        return {"outcome": outcome, "proof": proof}


def activate(store, candidate, *, runs=1):
    if runs not in (1, 3):
        raise AdapterError("edit_adapter_limit_invalid")
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        if not (root / "trial-proof.json").exists():
            raise AdapterError("edit_adapter_trial_required")
        trial_proof = _trial_stage(root)["proof"]
        if trial_proof.get("kinds") != ["document", "spreadsheet"]:
            raise AdapterError("edit_adapter_trial_requires_both_kinds")
        if runs == 3 and not any(item["complete"] for item in _active_records(root)):
            raise AdapterError("edit_adapter_active_success_required")
        if runs == 3 and not (root / "activation-1.json").exists():
            raise AdapterError("edit_adapter_first_activation_required")
        path = root / f"activation-{runs}.json"
        if not path.exists():
            write_new(path, {"candidate": candidate, "total_runs": runs,
                             "trial_proof_sha256": digest(read_record(root / "trial-proof.json"))})
        return status(base, candidate)


def execute(store, candidate, session, plan, *, approval, stop=lambda: False,
            progress=lambda value: None, pause_after_step=lambda: False):
    if approval != plan.get("sha256"):
        raise AdapterError("edit_adapter_approval_required")
    _plan(session, plan)
    _fresh(plan)
    _unused_plan(session, plan)
    base = _store(store)
    with workflow._owner(base):
        root = _candidate(base, candidate)
        _gates(root)
        limit = _activation_limit(root) if (root / "activation-1.json").exists() else 0
        active = _active_records(root)
        used = len(active)
        if any(item["pending"] for item in active):
            raise AdapterError("edit_adapter_active_pending")
        if used >= limit:
            raise AdapterError("edit_adapter_limit_reached")
        if any(item["intent"]["plan_sha256"] == plan["sha256"] for item in active):
            raise AdapterError("edit_adapter_plan_already_started")
        if read_record(root / "trial-intent.json")["plan_sha256"] == plan["sha256"]:
            raise AdapterError("edit_adapter_plan_already_started")
        n = used + 1
        write_new(root / f"active-{n}-intent.json", {"candidate": candidate,
                                                      "plan_sha256": plan["sha256"],
                                                      "session_root": str(session.root)})
        outcome = _run(session, plan, stop, progress, pause_after_step)
        proof = _verified(session, plan, outcome) if outcome.get("ok") is True else None
        if proof is not None:
            write_new(root / f"active-{n}-proof.json", proof)
        write_new(root / f"active-{n}-result.json", {"outcome": outcome, "proof": proof})
        return {"outcome": outcome, "proof": proof}


def status(store, candidate):
    root = _candidate(store, candidate)
    if (root / "review.json").exists():
        _gates(root)
    trial_stage = _trial_stage(root)
    limit = _activation_limit(root) if (root / "activation-1.json").exists() else 0
    active = _active_records(root)
    used = len(active)
    pending = any(item["pending"] for item in active)
    return {"candidate": candidate, "id": _ID,
            "offline": (root / "offline.json").exists(),
            "reviewed": (root / "review.json").exists(),
            "trial_started": trial_stage is not None,
            "trial_passed": bool(trial_stage and trial_stage["complete"]),
            "enabled_total": limit, "used": used, "remaining": 0 if pending else max(0, limit-used),
            "pending": pending}
