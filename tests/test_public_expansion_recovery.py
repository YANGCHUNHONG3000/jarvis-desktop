"""New site presets share the real recovery and Office-source proof path."""
from copy import deepcopy
from urllib.parse import urlsplit

import pytest

from agent_service.browser_resume import public_search_url, validate
from agent_service.browser_public import validate_public_config
from agent_service.computer import ComputerError, digest
from agent_service.interaction_panel import PUBLIC_SITES, build_request
from agent_service import interaction_recovery as recovery, page_office_delivery as page
from test_computer_interaction import ctx, run
import test_public_selection_recovery as history


CASES = [
    ("pandas_docs", "https://pandas.pydata.org/docs/search.html",
     "https://pandas.pydata.org/docs/reference/api/pandas.read_excel.html"),
    ("django_docs", "https://docs.djangoproject.com/en/5.2/",
     "https://docs.djangoproject.com/en/5.2/topics/email/"),
]


def config(site, start):
    return validate_public_config({**history.CONFIG, "site":site, "start_url":start, "query":"email report"})


@pytest.mark.parametrize("site,start,target", CASES)
@pytest.mark.parametrize("phase", [2, 3])
def test_new_site_phase_resume_to_verified_office_source(ctx, monkeypatch, site, start, target, phase):
    cfg = config(site, start)
    monkeypatch.setattr(history, "CONFIG", cfg)
    monkeypatch.setattr(history, "TARGET", target)
    prior, browser = history.parent(ctx, phase=phase)
    effects = deepcopy(browser.effects)
    plan = recovery.prepare(ctx["session"], prior["run"])["plan"]
    cursor = recovery.validate(ctx["session"], plan)["browser_resume"]
    assert cursor["url"] == (public_search_url(cfg) if phase == 2 else target)
    result = run(ctx, plan, browser_factory=history.PublicBrowser)
    assert result["ok"]
    codes = [a["result"]["code"] for a in result["attempts"]]
    assert not set(codes) & {"browser_fill_verified", "browser_public_search_submitted"}
    if phase == 3:
        assert "browser_public_target_opened" not in codes
    assert browser.effects == effects and plan["goal"]["page_office"]
    source = page.verify_page_source(result["run"], computer_base=ctx["session"].root.parent)
    assert source["metadata"]["url"] == target
    assert source["metadata"]["config"]["target_url"] == ""


@pytest.mark.parametrize("site,start,target", CASES)
@pytest.mark.parametrize("change", ["host", "path", "extra", "duplicate", "missing", "fragment"])
def test_new_site_resume_query_is_exact(site, start, target, change):
    cfg = config(site, start)
    url = public_search_url(cfg)
    altered = {
        "host":url.replace(urlsplit(url).netloc, "example.org"),
        "path":url.replace("search", "other"),
        "extra":url + "&extra=value",
        "duplicate":url + "&q=email+report",
        "missing":url.split("?")[0],
        "fragment":url + "#target",
    }[change]
    cursor = {"version":1,"mode":"public_search","phase":"search_results",
              "target_url":"","url":url,"config_sha256":digest(cfg)}
    assert validate(cfg, cursor) == cursor
    with pytest.raises(ComputerError, match="resume_invalid"):
        validate(cfg, {**cursor,"url":altered})


@pytest.mark.parametrize("site,start,target", CASES)
def test_new_site_gui_default_keeps_target_optional_and_office_enabled(site, start, target):
    label = next(k for k,v in PUBLIC_SITES.items() if v == (site, start))
    request = build_request({"site_mode":label,"url":"","query":"email report",
                             "target_url":"","expected":"","word":"","excel":""})
    assert request["browser"]["site"] == site
    assert request["browser"]["target_url"] == ""
    assert request["browser"]["required_text"] == ["email report"]
    assert request["page_office"] is True


def test_new_site_search_url_preserves_fixed_form_fields():
    assert public_search_url(config(*CASES[0][:2])) == "https://pandas.pydata.org/docs/search.html?q=email+report"
    assert public_search_url(config(*CASES[1][:2])) == "https://docs.djangoproject.com/en/5.2/search/?q=email+report&category="
