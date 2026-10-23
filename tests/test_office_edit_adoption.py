"""Explicit legacy binding checks; no native Office or model calls."""
from pathlib import Path
import tempfile

import pytest

from agent_service import interaction_recovery as recovery
from agent_service import office_edit_adapter as adapter
from agent_service.computer import digest, read_record, write_new
from test_computer_interaction import ctx
from test_interaction_recovery import partial


@pytest.fixture
def short_store():
    with tempfile.TemporaryDirectory(prefix="ja-") as folder:
        yield Path(folder) / "a"


def _ready(store, monkeypatch, ctx):
    origin, outcome = partial(ctx)
    session = ctx["session"]
    plan = recovery.prepare(session, outcome["run"])["plan"]
    candidate = adapter.ensure_candidate(store, session, origin, proposer="author")["candidate"]
    monkeypatch.setattr(adapter, "_gates", lambda root: None)
    monkeypatch.setattr(adapter, "_activation_limit", lambda root: 1)
    monkeypatch.setattr(adapter, "_trial_stage", lambda root: None)
    return session, origin, plan, store, candidate


def _review(store, candidate, session, plan):
    prepared = adapter.prepare_adoption(store, candidate, session, plan, proposer="author")
    request_id = prepared["request_sha256"]
    adapter.record_adoption_review(store, candidate, request_id,
                                   reviewer="independent", note="Checked exact origin and evidence")
    return request_id


def test_adoption_requires_independent_review_and_consumes_one_slot(short_store, monkeypatch, ctx):
    session, origin, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request = adapter.prepare_adoption(store, candidate, session, plan, proposer="author")
    request_id = request["request_sha256"]
    assert not list((store / candidate).glob("active-*-intent.json"))
    with pytest.raises(ValueError):
        adapter.record_adoption_review(store, candidate, request_id,
                                       reviewer="author", note="self-review")
    with pytest.raises(ValueError):
        adapter.adopt(store, candidate, session, plan, request_id)
    adapter.record_adoption_review(store, candidate, request_id,
                                   reviewer="independent", note="Verified original receipts")
    bound = adapter.adopt(store, candidate, session, plan, request_id)
    intent = read_record(store / candidate / "active-1-intent.json")
    assert bound["index"] == 1 and intent["plan_sha256"] == origin["sha256"]
    assert intent["adoption"] == request_id
    assert adapter.owns_recovery(store, session, plan)["index"] == 1
    assert adapter._active_records(store / candidate)[0]["pending"]
    with pytest.raises(ValueError, match="active_pending"):
        adapter.adopt(store, candidate, session, plan, request_id)
    assert len(list((store / candidate).glob("active-*-intent.json"))) == 1
    review = store / candidate / f"adoption-{request_id}-review.json"
    review.write_text(review.read_text(encoding="utf-8").replace("Verified original receipts", "Changed note"),
                      encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_changed"):
        adapter._active_records(store / candidate)


def test_adoption_rechecks_immutable_parent_before_binding(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, plan)
    source = next(Path(plan["goal"]["edits"][0]["source_root"]).glob("source.*"))
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        adapter.adopt(store, candidate, session, plan, request_id)
    assert not list((store / candidate).glob("active-*-intent.json"))


def test_claimed_origin_and_changed_review_refuse_adoption(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, plan)
    root = store / candidate
    review = root / f"adoption-{request_id}-review.json"
    original_review = review.read_bytes()
    review.write_text(review.read_text(encoding="utf-8").replace("independent", "author"),
                      encoding="utf-8")
    with pytest.raises(ValueError, match="adoption_review_changed"):
        adapter.adopt(store, candidate, session, plan, request_id)
    review.write_bytes(original_review)
    write_new(Path(plan["recovery"]["parent_run"]) / "continuation-claim.json",
              {"child": "0" * 64})
    with pytest.raises(ValueError):
        adapter.adopt(store, candidate, session, plan, request_id)
    assert not list(root.glob("active-*-intent.json"))


def test_stranded_binding_can_be_reconciled_and_prepared(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, plan)
    adapter.adopt(store, candidate, session, plan, request_id)
    monkeypatch.setattr(adapter, "_gates", lambda root: None)
    result = adapter.reconcile(store, candidate)
    assert result["records_finalized"] == 1
    resumed = adapter.prepare_resume(store, candidate, session, active_index=1)
    assert resumed["plan"]["recovery"]["parent_run"] == plan["recovery"]["parent_run"]
    assert len(list((store / candidate).glob("active-*-intent.json"))) == 1


def test_exhausted_limit_cannot_create_adoption_request(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    monkeypatch.setattr(adapter, "_activation_limit", lambda root: 0)
    with pytest.raises(ValueError, match="limit_reached"):
        adapter.prepare_adoption(store, candidate, session, plan, proposer="author")
    assert not list((store / candidate).glob("adoption-*-request.json"))


def test_new_preview_uses_same_reviewed_parent_request(short_store, monkeypatch, ctx):
    session, _, old_preview, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, old_preview)
    with monkeypatch.context() as clock:
        clock.setattr(adapter.time, "time", lambda: old_preview["prepared_at"] + 200)
        fresh = recovery.prepare(session, old_preview["recovery"]["parent_run"])["plan"]
        assert fresh["sha256"] != old_preview["sha256"]
        assert adapter.prepare_adoption(store, candidate, session, fresh,
                                        proposer="author")["request_sha256"] == request_id
        assert adapter.adopt(store, candidate, session, fresh, request_id)["index"] == 1


def test_skipped_resume_intent_uses_actual_parent_claim(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, plan)
    adapter.adopt(store, candidate, session, plan, request_id)
    root = store / candidate
    parent = Path(plan["recovery"]["parent_run"])
    first, second = "1" * 64, "2" * 64
    write_new(root / "active-1-resume-1-intent.json",
              {"parent_run": str(parent), "plan_sha256": first})
    write_new(root / "active-1-resume-2-intent.json",
              {"parent_run": str(parent), "plan_sha256": second})
    write_new(parent / "continuation-claim.json", {"child": second})
    (session.root / "goals" / (second + ".run")).mkdir()
    assert adapter._active_records(root)[0]["pending"]
    (session.root / "goals" / (first + ".run")).mkdir()
    with pytest.raises(ValueError, match="adoption_changed"):
        adapter._active_records(root)


def test_orphan_old_candidate_stage_blocks_binding(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    old_manifest = {"schema": 1, "implementation_sha256": "0" * 64}
    old_root = store / digest(old_manifest)
    old_root.mkdir()
    write_new(old_root / "candidate.json", old_manifest)
    write_new(old_root / "active-1-result.json", {"outcome": {"ok": True}})
    with pytest.raises(ValueError, match="adoption_store_changed"):
        adapter.prepare_adoption(store, candidate, session, plan, proposer="author")


def test_valid_unrelated_old_resume_does_not_block_request(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    old_manifest = {"schema": 1, "implementation_sha256": "0" * 64}
    old_root = store / digest(old_manifest)
    old_root.mkdir()
    write_new(old_root / "candidate.json", old_manifest)
    write_new(old_root / "active-1-intent.json", {"candidate": old_root.name,
        "plan_sha256": "3" * 64, "session_root": str(session.root)})
    write_new(old_root / "active-1-resume-1-intent.json", {"parent_run": "unrelated",
        "plan_sha256": "4" * 64})
    assert adapter.prepare_adoption(store, candidate, session, plan,
                                    proposer="author")["request_sha256"]


def test_binding_sidecar_recovers_and_missing_adoption_cannot_bypass_review(short_store, monkeypatch, ctx):
    session, _, plan, store, candidate = _ready(short_store, monkeypatch, ctx)
    request_id = _review(store, candidate, session, plan)
    adapter.adopt(store, candidate, session, plan, request_id)
    root = store / candidate
    sidecar = root / "active-1-adoption.json"
    sidecar.unlink()
    assert adapter.reconcile(store, candidate)["records_finalized"] >= 1
    assert sidecar.is_file()
    intent_path = root / "active-1-intent.json"
    intent = read_record(intent_path)
    del intent["adoption"]
    del intent["adoption_review_sha256"]
    intent_path.unlink()
    write_new(intent_path, intent)
    with pytest.raises(ValueError, match="adoption_changed"):
        adapter._active_records(root)
