"""Offline contract tests for the pinned OpenAI model branch."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from agent_service import openai_profile
from agent_service.budget import BudgetLedger, BudgetError
from agent_service.model import CloudClient, ModelError, configured_model_label


FAKE_KEY = "offline-test-key-only"
API = openai_profile.API + "/chat/completions"


def pricing_doc(*, input_price="0.80", cached="0.04", writes="1.00", output="4.00"):
    rows = "\n".join(f"| {label} | ${price} | 1M tokens |" for label, price in (
        ("Input", input_price), ("Cached input", cached),
        ("Cache writes", writes), ("Output", output)))
    return (f"Model ID: `{openai_profile.MODEL}`\n### Text tokens\n"
            "| Metric | Price | Unit |\n| --- | ---: | --- |\n" + rows + "\n"
            "## Endpoints\n").encode()


def fx_doc(*, date=None, rate=7.2, base="USD", amount=1):
    if date is None:
        date = datetime.now(timezone.utc).date().isoformat()
    return json.dumps({"amount": amount, "base": base, "date": date,
                       "rates": {"CNY": rate}}).encode()


def success(*, usage=None, content='{"answer":"ok"}', model=None):
    if usage is None:
        usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    return {"model": model or openai_profile.MODEL, "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class _Cookies:
    def clear(self):
        pass


class _Response:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status_code = status
        self.closed = False

    def iter_content(self, size):
        yield self.body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, prices=None, fx=None, metadata=None, result=None,
                 post_status=200, on_get=None):
        self.cookies = _Cookies()
        self.trust_env = True
        self.prices = prices if prices is not None else pricing_doc()
        self.fx = fx if fx is not None else fx_doc()
        # OpenAI model metadata intentionally omits context_window and
        # max_output_tokens; OpenAI's pinned profile supplies these locally.
        self.metadata = metadata if metadata is not None else {"data": [{"id": openai_profile.MODEL}]}
        self.result = result if result is not None else success()
        self.post_status = post_status
        self.on_get = on_get
        self.gets, self.posts, self.responses = [], [], []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if self.on_get:
            self.on_get(url)
        body = (self.prices if url == openai_profile.PRICING else
                self.fx if url == openai_profile.FX else
                self.metadata)
        response = _Response(body)
        self.responses.append(response)
        return response

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        response = _Response(self.result, status=self.post_status)
        self.responses.append(response)
        return response


@pytest.fixture
def setup_client(tmp_path):
    key = tmp_path / "fake-openai-key.txt"
    key.write_text(FAKE_KEY, encoding="utf-8")
    permission = tmp_path / "permission.json"
    permission.write_text(json.dumps({"authorized": True, "daily_limit_cny": "5.00",
                                      "timezone": "America/New_York", "provider": "openai",
                                      "model": openai_profile.MODEL}), encoding="utf-8")
    ledger = BudgetLedger(tmp_path / "fake-ledger.sqlite3")

    def make(session=None, **kwargs):
        return CloudClient(ledger=ledger, session=session or FakeSession(), key_path=key,
                           permission_path=permission, **kwargs)
    return make, ledger, permission


def test_openai_request_contract_uses_fixed_url_and_no_deepseek_fields(setup_client):
    make, _, _ = setup_client
    session = FakeSession()
    client = make(session)
    assert client.complete("system", "user", max_tokens=64) == {"answer": "ok"}
    assert [u for u, _ in session.gets] == [openai_profile.PRICING, openai_profile.FX,
                                           openai_profile.API + "/models"]
    assert [u for u, _ in session.posts] == [API]
    body = json.loads(session.posts[0][1]["data"])
    assert body == {
        "model": openai_profile.MODEL,
        "messages": [{"role": "system", "content": "Return valid JSON only. system"},
                     {"role": "user", "content": "user"}],
        "response_format": {"type": "json_object"}, "stream": False,
        "max_completion_tokens": 64, "reasoning_effort": "none",
        "service_tier": "default", "store": False,
        "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
    }
    assert not ({"thinking", "max_tokens", "prompt_cache_retention"} & body.keys())
    for _, options in session.gets:
        assert options["allow_redirects"] is False
    post = session.posts[0][1]
    assert post["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    assert post["allow_redirects"] is False and post["stream"] is True
    assert client.last_call["provider"] == "openai"


@pytest.mark.parametrize("fx", [
    fx_doc(date=(datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()),
    fx_doc(date=(datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()),
    fx_doc(rate=10.01), fx_doc(rate=0), fx_doc(rate=float("nan")),
    fx_doc(rate=True), fx_doc(amount=True), b"not-json", fx_doc(base="EUR"),
])
def test_unverified_or_out_of_policy_fx_blocks_post_without_hold(setup_client, fx):
    make, ledger, _ = setup_client
    session = FakeSession(fx=fx)
    with pytest.raises(ModelError):
        make(session).complete("system", "user")
    assert not session.posts
    assert ledger.snapshot()["held_micro"] == 0


@pytest.mark.parametrize("prices", [
    pricing_doc(input_price="1.01"), pricing_doc(cached="0.051"),
    pricing_doc(writes="1.26"), pricing_doc(output="5.01"),
    b"unrecognized pricing page", pricing_doc().replace(openai_profile.MODEL.encode(), b"other"),
    pricing_doc().replace(b"| Output | $4.00 | 1M tokens |",
                          b"| Output | $4.00 | 1M tokens |\n| Surcharge | $0.01 | 1M tokens |"),
    pricing_doc().replace(b"| Output | $4.00 | 1M tokens |",
                          b"| Input | $4.00 | 1M tokens |"),
])
def test_price_unknown_or_above_ceiling_blocks_post_without_hold(setup_client, prices):
    make, ledger, _ = setup_client
    session = FakeSession(prices=prices)
    with pytest.raises(ModelError):
        make(session).complete("system", "user")
    assert not session.posts
    assert ledger.snapshot()["held_micro"] == 0


def test_openai_model_metadata_needs_only_matching_id(setup_client):
    make, ledger, _ = setup_client
    session = FakeSession(metadata={"data": [{"id": openai_profile.MODEL, "vendor_note": "ignored"}]})
    assert make(session).complete("system", "user") == {"answer": "ok"}
    assert len(session.posts) == 1
    assert ledger.snapshot()["held_micro"] == 0


def test_insufficient_budget_refuses_openai_post(setup_client):
    make, ledger, _ = setup_client
    ledger.reserve(4_999_999, "preexisting-fake-reservation")
    session = FakeSession()
    with pytest.raises(BudgetError):
        make(session).complete("system", "user")
    assert not session.posts
    assert ledger.snapshot()["held_micro"] == 4_999_999


def test_openai_existing_client_reads_unlimited_policy_without_releasing_hold(setup_client, monkeypatch):
    from agent_service import budget, model_policy
    from agent_service.budget import BudgetError
    make, ledger, permission = setup_client
    monkeypatch.setattr(budget, 'DEFAULT_LEDGER', ledger.path)
    monkeypatch.setattr(model_policy, 'PERMISSION', permission)
    ledger.reserve(4_999_999, 'old-unknown')
    session = FakeSession()
    client = make(session=session)
    with pytest.raises(BudgetError, match='budget exceeded'):
        client.complete('JSON', 'one step', max_tokens=64)
    assert not session.posts
    permit = json.loads(permission.read_text(encoding='utf-8'))
    permit.update(budget_mode='unlimited', daily_limit_cny=None, rolling_limit_cny=None)
    permission.write_text(json.dumps(permit), encoding='utf-8')
    assert client.complete('JSON', 'one step', max_tokens=64) == {'answer': 'ok'}
    state = ledger.snapshot()
    assert len(session.posts) == 1 and state['held_micro'] == 4_999_999
    assert state['remaining_micro'] is None and state['faulted'] is False


def test_unknown_usage_keeps_hold_and_malformed_json_after_usage_is_settled(setup_client):
    make, ledger, _ = setup_client
    unknown = FakeSession(result=success(usage={}))
    client = make(unknown)
    with pytest.raises(ModelError, match="model_usage_invalid"):
        client.complete("system", "user")
    pending_hold = client.last_call["reserved_micro"]
    assert ledger.snapshot()["held_micro"] == pending_hold
    assert ledger.snapshot()["charged_micro"] == 0

    malformed = FakeSession(result=success(content="{bad json"))
    client2 = make(malformed)
    with pytest.raises(ModelError, match="model_json_invalid"):
        client2.complete("system", "user")
    assert client2.last_call["state"] == "settled"
    assert ledger.snapshot()["held_micro"] == pending_hold
    assert ledger.snapshot()["charged_micro"] == 12 * openai_profile.INPUT_RATE + 4 * openai_profile.OUTPUT_RATE


def test_permission_provider_change_between_calls_is_rejected_without_fallback(setup_client):
    make, ledger, permission = setup_client
    first_session = FakeSession()
    client = make(first_session)
    assert client.complete("system", "user") == {"answer": "ok"}
    permit = json.loads(permission.read_text(encoding="utf-8"))
    permit["provider"] = "deepseek"
    permission.write_text(json.dumps(permit), encoding="utf-8")

    second_session = FakeSession()
    with pytest.raises(ModelError, match="model_configuration_changed"):
        client.complete("system", "user")
    assert second_session.gets == [] and second_session.posts == []
    assert len(first_session.posts) == 1
    assert ledger.snapshot()["held_micro"] == 0


def test_openai_preflight_failure_never_falls_back_to_deepseek(setup_client):
    make, ledger, _ = setup_client
    session = FakeSession(fx=b"invalid-fx")
    with pytest.raises(ModelError, match="model_fx_unverified"):
        make(session).complete("system", "user")
    urls = [url for url, _ in session.gets] + [url for url, _ in session.posts]
    assert urls == [openai_profile.PRICING, openai_profile.FX]
    assert all("deepseek" not in url for url in urls)
    assert not session.posts and ledger.snapshot()["held_micro"] == 0


def test_cancel_during_openai_preflight_blocks_models_and_paid_post(setup_client):
    make, ledger, _ = setup_client
    stopped = [False]
    session = FakeSession(on_get=lambda url: stopped.__setitem__(0, True)
                          if url == openai_profile.FX else None)
    with pytest.raises(ModelError, match="model_cancelled_before_request"):
        make(session, should_stop=lambda: stopped[0]).complete("system", "user")
    assert [url for url, _ in session.gets] == [openai_profile.PRICING, openai_profile.FX]
    assert not session.posts and ledger.snapshot()["held_micro"] == 0


def test_mismatched_completion_identity_keeps_unknown_charge_on_hold(setup_client):
    make, ledger, _ = setup_client
    session = FakeSession(result=success(model="unlisted-model"))
    client = make(session)
    with pytest.raises(ModelError, match="model_identity_changed"):
        client.complete("system", "user")
    assert len(session.posts) == 1
    assert client.last_call["state"] == "pending"
    assert ledger.snapshot()["held_micro"] == client.last_call["reserved_micro"]
    assert ledger.snapshot()["charged_micro"] == 0


def test_unknown_model_for_same_provider_is_rejected_before_http(setup_client):
    make, ledger, permission = setup_client
    session = FakeSession()
    client = make(session)
    assert client.complete("system", "user") == {"answer": "ok"}
    permit = json.loads(permission.read_text(encoding="utf-8"))
    permit["model"] = "gpt-unreviewed-model"
    permission.write_text(json.dumps(permit), encoding="utf-8")
    before_gets, before_posts = len(session.gets), len(session.posts)
    with pytest.raises(ModelError, match="model_provider_invalid"):
        client.complete("system", "user")
    assert len(session.gets) == before_gets and len(session.posts) == before_posts
    assert ledger.snapshot()["held_micro"] == 0


def test_configured_model_label_never_echoes_unknown_config_or_fake_key(tmp_path):
    permission = tmp_path / "bad-permission.json"
    hostile_model = "unreviewed-" + FAKE_KEY
    permission.write_text(json.dumps({"provider": "openai", "model": hostile_model,
                                      "key_path": FAKE_KEY}), encoding="utf-8")
    label = configured_model_label(permission)
    assert label == "模型配置待检查"
    assert hostile_model not in label and FAKE_KEY not in label


@pytest.mark.parametrize("status,code,param", [
    (400, "unsupported_parameter", "prompt_cache_options"),
    (429, "rate_limit_exceeded", "service_tier"),
])
def test_http_errors_record_only_whitelisted_diagnostics_and_keep_hold(
        setup_client, status, code, param):
    make, ledger, _ = setup_client
    malicious = f"private message containing {FAKE_KEY} and secret=do-not-log"
    session = FakeSession(post_status=status, result={"error": {
        "code": code, "param": param, "message": malicious,
        "raw_api_key": FAKE_KEY, "provider_debug": "authorization=Bearer " + FAKE_KEY,
    }})
    client = make(session)
    with pytest.raises(ModelError, match=f"model_http_{status}") as caught:
        client.complete("system", "user")

    assert client.last_call["error_code"] == code
    assert client.last_call["error_param"] == param
    assert set(client.last_call).isdisjoint({"message", "raw_api_key", "provider_debug"})
    assert FAKE_KEY not in str(caught.value)
    assert FAKE_KEY not in repr(client.last_call)
    assert client.last_call["state"] == "pending"
    assert ledger.snapshot()["held_micro"] == client.last_call["reserved_micro"]
    assert ledger.snapshot()["charged_micro"] == 0


def test_http_error_unknown_diagnostic_values_are_discarded(setup_client):
    make, ledger, _ = setup_client
    session = FakeSession(post_status=400, result={"error": {
        "code": "invalid_api_key " + FAKE_KEY, "param": "authorization",
        "message": FAKE_KEY,
    }})
    client = make(session)
    with pytest.raises(ModelError, match="model_http_400") as caught:
        client.complete("system", "user")
    assert "error_code" not in client.last_call and "error_param" not in client.last_call
    assert FAKE_KEY not in str(caught.value) and FAKE_KEY not in repr(client.last_call)
    assert ledger.snapshot()["held_micro"] == client.last_call["reserved_micro"]
    assert ledger.snapshot()["charged_micro"] == 0
