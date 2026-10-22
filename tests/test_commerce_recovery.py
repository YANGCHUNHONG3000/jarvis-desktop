"""Commerce data proof must survive the real engine and persisted recovery."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_service.browser_checkpoint import verify_browser
from agent_service.computer import ComputerError, digest, read_record
from agent_service.computer_interaction import prepare
from agent_service import interaction_recovery as recovery, page_office_delivery as page
from test_browser_commerce import fixture, config, result_page, PRODUCT
from test_computer_interaction import ctx, run


@pytest.mark.parametrize("phase", ["search_results", "target_opened"])
def test_commerce_production_history_resumes_without_completed_ui_effects(ctx, fixture, phase):
    plan = prepare(ctx["session"],"Office desk sourcing",browser=config(),page_office=True)
    root = ctx["session"].root/"goals"/(plan["sha256"]+".run")
    def stop():
        for path in sorted(root.glob("step-*/verification.json")):
            result = read_record(path)
            d = result.get("observation",{}).get("discovery",{})
            if not result.get("ok") or not d.get("query_verified"):
                continue
            if (phase == "search_results" and not d.get("target_clicked")
                    or phase == "target_opened" and d.get("target_clicked")
                    and result["observation"]["url"] == PRODUCT):
                return True
        return False
    def factory(cfg, **options):
        return fixture(cfg,resume=options.get("resume"))
    prior = run(ctx,plan,browser_factory=factory,stop=stop)
    assert not prior["ok"] and prior["code"] == "goal_cancelled"
    originals = {p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
    proposal = recovery.prepare(ctx["session"],root)["plan"]
    assert recovery.validate(ctx["session"],proposal)["browser_resume"]["phase"] == phase
    result = run(ctx,proposal,browser_factory=factory)
    assert result["ok"],result
    codes = [a["result"]["code"] for a in result["attempts"]]
    assert not set(codes)&{"browser_fill_verified","browser_public_search_submitted"}
    if phase == "target_opened":
        assert "browser_public_target_opened" not in codes
    assert all(p.read_bytes()==raw for p,raw in originals.items())
    source = page.verify_page_source(result["run"],computer_base=ctx["session"].root.parent)
    assert source["metadata"]["url"] == PRODUCT
    assert source["metadata"]["config"]["target_url"] == ""


def test_independent_checkpoint_rejects_shell_only_and_unbound_commerce_proofs(fixture):
    session = fixture(config())
    try:
        good = result_page(session)
        assert verify_browser(session.config,good,complete=False) == good
        for change in ("missing","query","session","shell_status","api_finished","historical_without_cursor"):
            value = deepcopy(good)
            d = value["discovery"]
            proof = d["commerce_search"]
            if change == "missing":
                d["commerce_search"] = None
            elif change == "query":
                proof["query"] = "unapproved"
            elif change == "session":
                proof["session_id"] = "another-browser"
            elif change == "shell_status":
                proof["shell"]["status"] = 200.0
            elif change == "api_finished":
                proof["response"]["finished"] = False
            else:
                d["search_proof"] = "historical"
                d["search_status"] = None
                d["commerce_search"] = None
            if d["commerce_search"]:
                proof["proof_sha256"] = digest({k:v for k,v in proof.items() if k!="proof_sha256"})
            value["state_id"] = digest({k:v for k,v in value.items() if k not in {"state_id","actions"}})
            for action in value["actions"]:
                action["state_id"] = value["state_id"]
            with pytest.raises(ComputerError,match="commerce_not_verified"):
                verify_browser(session.config,value,complete=False)
    finally:
        session.close()
