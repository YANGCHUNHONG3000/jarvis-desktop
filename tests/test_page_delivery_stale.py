"""A verified non-dispatched stale attempt must not erase a completed read."""
from copy import deepcopy
import pytest
from agent_service.page_office_delivery import _harmless_failures


def outcome():
    return {"attempts":[{"action":{"kind":"browser_wait"}, "result":{
        "ok":False,"state":"failed","code":"browser_action_stale",
        "verification":{"effect_dispatched":False}}}],
        "failures":[{"kind":"browser_wait","code":"browser_action_stale"}]}


@pytest.mark.parametrize("kind", ["browser_fill","browser_click","browser_wait"])
def test_accepts_exact_no_effect_stale_summary(kind):
    value=outcome()
    value["attempts"][0]["action"]["kind"]=kind
    value["failures"][0]["kind"]=kind
    assert _harmless_failures(value)
    assert _harmless_failures({"attempts":[],"failures":[]})


@pytest.mark.parametrize("change", [
    "unknown", "missing_effect", "zero_effect", "effect_true", "other_code",
    "other_action", "missing_summary", "extra_summary", "different_summary",
    "non_boolean_ok", "omitted_attempt", "reordered",
])
def test_rejects_unknown_or_unmatched_failure_evidence(change):
    value=outcome()
    result=value["attempts"][0]["result"]
    if change=="unknown":result["state"]="unknown"
    elif change=="missing_effect":result["verification"]={}
    elif change=="zero_effect":result["verification"]["effect_dispatched"]=0
    elif change=="effect_true":result["verification"]["effect_dispatched"]=True
    elif change=="other_code":result["code"]="browser_effect_unknown"
    elif change=="other_action":value["attempts"][0]["action"]["kind"]="office_edit"
    elif change=="missing_summary":value["failures"]=[]
    elif change=="extra_summary":value["failures"]*=2
    elif change=="different_summary":value["failures"][0]["kind"]="browser_click"
    elif change=="non_boolean_ok":result["ok"]=0
    elif change=="omitted_attempt":value["attempts"]=[]
    elif change=="reordered":
        second=deepcopy(value["attempts"][0])
        second["action"]["kind"]="browser_click"
        value["attempts"].append(second)
        value["failures"].insert(0,{"kind":"browser_click","code":"browser_action_stale"})
    assert not _harmless_failures(value)
