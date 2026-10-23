"""Offline contract checks for paid model calls; all credentials and HTTP are fake."""

import json

import pytest
import requests

from agent_service.budget import BudgetLedger
from agent_service.model import (API, MODEL, PRICING, CloudClient, ModelError, checked_prices,
                                request_token_bound, PROMPT_FRAMING_ALLOWANCE)


FAKE_KEY = "test-key-never-real"


def posted_reserve(session, index=0):
    raw = session.posts[index][1]['data']
    return (len(raw) + PROMPT_FRAMING_ALLOWANCE) * 2 + json.loads(raw)['max_tokens'] * 8


@pytest.mark.parametrize('stage', ['initial', 'prices', 'models'])
def test_cancelled_preflight_never_starts_paid_request(local_client, stage):
    stopped = [stage == 'initial']
    def on_get(url):
        if (stage == 'prices' and url == PRICING) or (stage == 'models' and url == API + '/models'):
            stopped[0] = True
    session = FakeSession(on_get=on_get)
    client = local_client[0](session=session, should_stop=lambda: stopped[0])
    with pytest.raises(ModelError, match='model_cancelled_before_request'):
        client.complete('JSON', 'one step')
    assert not session.posts
    assert client.calls == 0


def test_cancel_after_post_still_accounts_returned_usage(local_client):
    stopped = [False]
    session = FakeSession(on_post=lambda: stopped.__setitem__(0, True))
    client = local_client[0](session=session, should_stop=lambda: stopped[0])
    assert client.complete('JSON', 'one step') == {'answer': 'ok'}
    assert client.last_call['state'] == 'settled'
    assert client.last_call['charged_micro'] == 60


def price_html(*, input_peak="2元", output_peak="8元"):
    rates = [
        ("百万tokens输入 缓存未命中", "空闲时段", "1元"),
        ("百万tokens输入 缓存未命中", "高峰时段", input_peak),
        ("百万tokens输入 缓存命中", "空闲时段", "0.2元"),
        ("百万tokens输入 缓存命中", "高峰时段", "0.2元"),
        ("百万tokens输出", "空闲时段", "4元"),
        ("百万tokens输出", "高峰时段", output_peak),
    ]
    rows = "".join(f"<tr><td>{category}</td><td>{period}</td><td>{price}</td><td>9元</td></tr>"
                   for category, period, price in rates)
    return ("<table><tr><th>模型</th><th>deepseek-flash</th>"
            "<th>deepseek-v4-pro</th></tr>" + rows + "</table>").encode()


def model_metadata(**changes):
    meta = {"id": MODEL, "context_window": 1_048_576, "max_output_tokens": 2048}
    meta.update(changes)
    return {"data": [meta]}


def completion(*, usage=None, content='{"answer":"ok"}', model=MODEL):
    if usage is None:
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return {"model": model, "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class Cookies:
    def __init__(self):
        self.clears = 0

    def clear(self):
        self.clears += 1


class Response:
    def __init__(self, body, *, status=200, clock=None, advance=0):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status_code = status
        self.closed = False
        self.clock = clock
        self.advance = advance

    def iter_content(self, size):
        if self.clock is not None:
            self.clock.value += self.advance
        yield self.body

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeSession:
    def __init__(self, *, prices=None, metadata=None, result=None, post_error=None,
                 on_post=None, on_get=None, post_clock=None, post_advance=0):
        self.cookies = Cookies()
        self.trust_env = True
        self.gets = []
        self.posts = []
        self.responses = []
        self.prices = prices if prices is not None else price_html()
        self.metadata = metadata if metadata is not None else model_metadata()
        self.result = result if result is not None else completion()
        self.post_error = post_error
        self.on_post = on_post
        self.on_get = on_get
        self.post_clock = post_clock
        self.post_advance = post_advance

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if self.on_get:
            self.on_get(url)
        body = self.prices if url == PRICING else self.metadata
        response = Response(body)
        self.responses.append(response)
        return response

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.on_post:
            self.on_post()
        if self.post_error:
            raise self.post_error
        response = Response(self.result, clock=self.post_clock, advance=self.post_advance)
        self.responses.append(response)
        return response


@pytest.fixture
def local_client(tmp_path):
    key = tmp_path / "fake-key.txt"
    key.write_text(FAKE_KEY, encoding="utf-8")
    permission = tmp_path / "permission.json"
    permission.write_text(json.dumps({"authorized": True, "daily_limit_cny": "5.00",
                                      "timezone": "America/New_York"}), encoding="utf-8")
    ledger = BudgetLedger(tmp_path / "ledger.sqlite3")

    def make(session=None, **kwargs):
        return CloudClient(ledger=ledger, session=session or FakeSession(),
                           key_path=key, permission_path=permission, **kwargs)

    return make, ledger, permission


def test_paid_post_follows_reservation_and_valid_verification(local_client):
    make, ledger, _ = local_client
    held_at_post = []
    session = FakeSession(on_post=lambda: held_at_post.append(ledger.snapshot()["held_micro"]))
    client = make(session)
    assert client.complete("system", "user", max_tokens=1024) == {"answer": "ok"}
    assert held_at_post == [posted_reserve(session)]
    assert held_at_post[0] < 20_000
    assert client.last_call['budget_policy'] == 'request_utf8_v1'
    assert client.last_call['prompt_token_bound'] == len(session.posts[0][1]['data']) + 4096
    assert ledger.snapshot()["charged_micro"] == 10 * 2 + 5 * 8
    assert ledger.snapshot()["held_micro"] == 0
    assert [url for url, _ in session.gets] == [PRICING, API + "/models"]
    assert [url for url, _ in session.posts] == [API + "/chat/completions"]
    assert "Authorization" not in session.gets[0][1]
    assert session.gets[1][1]["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    post = session.posts[0][1]
    assert post["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    assert post["allow_redirects"] is False and post["timeout"] == (5, 35)
    assert post["stream"] is True
    assert json.loads(post["data"])["thinking"] == {"type": "disabled"}
    assert "json" in json.loads(post["data"])["messages"][0]["content"].lower()
    assert session.cookies.clears >= 4
    assert all(response.closed for response in session.responses)


@pytest.mark.parametrize("bad_prices", [price_html(input_peak="3元"),
                                         price_html(output_peak="9元"),
                                         b"<html>no price table</html>"])
def test_price_increase_or_unknown_layout_blocks_post(local_client, bad_prices):
    make, ledger, _ = local_client
    session = FakeSession(prices=bad_prices)
    with pytest.raises(ModelError) as error:
        make(session).complete("system", "user")
    assert FAKE_KEY not in str(error.value)
    assert session.posts == []
    assert ledger.snapshot()["held_micro"] == 0


@pytest.mark.parametrize("metadata", [model_metadata(id="other-model"),
                                      model_metadata(context_window="1048576"),
                                      model_metadata(max_output_tokens=128),
                                      {"data": []}, []])
def test_unknown_or_changed_model_metadata_blocks_post(local_client, metadata):
    make, ledger, _ = local_client
    session = FakeSession(metadata=metadata)
    with pytest.raises(ModelError):
        make(session).complete("system", "user")
    assert session.posts == []
    assert ledger.snapshot()["held_micro"] == 0


def test_permission_missing_blocks_all_http_even_with_fake_key(local_client):
    make, ledger, permission = local_client
    permission.unlink()
    session = FakeSession()
    with pytest.raises(ModelError, match="model_permission_missing"):
        make(session).complete("system", "user")
    assert session.gets == session.posts == []
    assert ledger.snapshot()["held_micro"] == 0


@pytest.mark.parametrize("usage", [{},
                                    {"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 14},
                                    {"prompt_tokens": 10.0, "completion_tokens": 5,
                                     "total_tokens": 15},
                                    {"prompt_tokens": True, "completion_tokens": 5,
                                     "total_tokens": 6}])
def test_invalid_usage_keeps_reservation(local_client, usage):
    make, ledger, _ = local_client
    session = FakeSession(result=completion(usage=usage))
    client = make(session)
    with pytest.raises(ModelError):
        client.complete("system", "user")
    assert len(session.posts) == 1
    assert ledger.snapshot()["held_micro"] == posted_reserve(session)
    assert ledger.snapshot()["charged_micro"] == 0
    assert client.last_call["state"] == "pending"


def test_network_failure_and_post_deadline_keep_reservation(local_client):
    make, ledger, _ = local_client
    failed = FakeSession(post_error=requests.ConnectionError("secret=" + FAKE_KEY))
    with pytest.raises(ModelError, match="model_network_failed") as error:
        make(failed).complete("system", "user")
    assert FAKE_KEY not in str(error.value)
    assert ledger.snapshot()["held_micro"] > 0

    clock = FakeClock()
    timed = FakeSession(post_clock=clock, post_advance=51)
    with pytest.raises(ModelError, match="model_deadline"):
        make(timed, clock=clock).complete("system", "user")
    assert timed.responses[-1].closed
    assert ledger.snapshot()["held_micro"] == posted_reserve(failed) + posted_reserve(timed)


def test_bad_json_after_valid_usage_still_charges(local_client):
    make, ledger, _ = local_client
    session = FakeSession(result=completion(content="{bad json"))
    client = make(session)
    with pytest.raises(ModelError, match="model_json_invalid"):
        client.complete("system", "user")
    assert ledger.snapshot()["held_micro"] == 0
    assert ledger.snapshot()["charged_micro"] == 60
    assert client.last_call["state"] == "settled"


def test_max_tokens_boundary_and_six_call_ceiling(local_client, monkeypatch):
    from agent_service import model

    make, ledger, _ = local_client
    for bad in (0, 2049, 1.0, True):
        session = FakeSession()
        with pytest.raises(ModelError, match="model_input_invalid"):
            make(session).complete("system", "user", max_tokens=bad)
        assert session.gets == session.posts == []

    monkeypatch.setattr(model, "CONTEXT_LIMIT", 10000)
    session = FakeSession(metadata=model_metadata(context_window=10000))
    client = make(session)
    for _ in range(6):
        assert client.complete("system", "user", max_tokens=2048) == {"answer": "ok"}
    with pytest.raises(ModelError, match="model_call_limit"):
        client.complete("system", "user")
    assert len(session.posts) == 6
    assert ledger.snapshot()["held_micro"] == 0


def test_mismatched_paid_response_model_must_not_be_trusted(local_client):
    make, _, _ = local_client
    session = FakeSession(result=completion(model="unpriced-expensive-model"))
    with pytest.raises(ModelError):
        make(session).complete("system", "user")


def test_extra_tariff_row_requires_price_review():
    html = price_html().replace(b"</table>",
                                b"<tr><td>additional model surcharge</td>"
                                b"<td>peak</td><td>99yuan</td><td>99yuan</td></tr></table>")
    with pytest.raises(ModelError):
        checked_prices(html)


def test_expired_deadline_blocks_all_http(local_client):
    make, ledger, _ = local_client
    clock = FakeClock()
    session = FakeSession()
    with pytest.raises(ModelError, match="model_deadline"):
        make(session, clock=clock, deadline=0).complete("system", "user")
    assert session.gets == session.posts == []
    assert ledger.snapshot()["held_micro"] == 0


def test_price_read_past_deadline_stops_before_metadata(local_client):
    make, ledger, _ = local_client
    clock = FakeClock()
    session = FakeSession(on_get=lambda url: setattr(clock, "value", 21))
    with pytest.raises(ModelError, match="model_deadline"):
        make(session, clock=clock, deadline=20).complete("system", "user")
    assert [url for url, _ in session.gets] == [PRICING]
    assert session.posts == [] and ledger.snapshot()["held_micro"] == 0
    assert session.responses[0].closed


def test_insufficient_paid_window_before_reserve_blocks_post(local_client):
    make, ledger, _ = local_client
    clock = FakeClock()
    session = FakeSession(on_get=lambda url: setattr(clock, "value", 12)
                          if url == API + "/models" else None)
    with pytest.raises(ModelError, match="model_deadline"):
        make(session, clock=clock, deadline=20).complete("system", "user")
    assert len(session.gets) == 2 and session.posts == []
    assert ledger.snapshot()["held_micro"] == 0


def test_deadline_after_reserve_prevents_post_and_keeps_hold(local_client, monkeypatch):
    make, ledger, _ = local_client
    clock = FakeClock()
    original_reserve = ledger.reserve

    def reserve_then_advance(amount, fingerprint):
        reservation_id = original_reserve(amount, fingerprint)
        clock.value = 15
        return reservation_id

    monkeypatch.setattr(ledger, "reserve", reserve_then_advance)
    session = FakeSession()
    client = make(session, clock=clock, deadline=20)
    with pytest.raises(ModelError, match="model_deadline"):
        client.complete("system", "user")
    assert session.posts == []
    assert ledger.snapshot()["held_micro"] == client.last_call['reserved_micro']
    assert client.last_call['reserved_micro'] < 20_000
    assert client.last_call["state"] == "pending"


def test_deadline_caps_connect_and_read_timeouts(local_client):
    make, _, _ = local_client
    clock = FakeClock()
    session = FakeSession()
    assert make(session, clock=clock, deadline=20).complete("system", "user") == {"answer": "ok"}
    for _, options in session.gets + session.posts:
        assert options["timeout"] == (5, 20)


def test_paid_post_crossing_task_deadline_keeps_hold(local_client):
    make, ledger, _ = local_client
    clock = FakeClock()
    session = FakeSession(post_clock=clock, post_advance=21)
    with pytest.raises(ModelError, match="model_deadline"):
        make(session, clock=clock, deadline=20).complete("system", "user")
    assert len(session.posts) == 1
    assert session.responses[-1].closed
    assert ledger.snapshot()["held_micro"] == posted_reserve(session)


@pytest.mark.parametrize('text', ['ascii JSON', '中文表格 JSON', '🧪é\u0301\\\n<think> JSON'])
def test_request_bound_uses_final_utf8_body_including_json_and_framing(local_client, text):
    make, _, _ = local_client
    session = FakeSession()
    client = make(session)
    client.complete('system without format instruction', text, max_tokens=64)
    raw = session.posts[0][1]['data']; body = json.loads(raw)
    assert request_token_bound(raw) == len(raw) + 4096
    assert client.last_call['reserved_micro'] == (len(raw) + 4096) * 2 + 64 * 8
    assert set(body) == {'model', 'messages', 'response_format', 'thinking', 'max_tokens', 'stream'}
    assert [m['role'] for m in body['messages']] == ['system', 'user']
    assert all(set(m) == {'role', 'content'} for m in body['messages'])


def test_small_request_runs_with_old_unknown_hold_and_less_than_full_context_balance(local_client):
    make, ledger, _ = local_client
    unknown = ledger.reserve(2_101_248, 'old-unknown')
    # Match the real stricter rolling-window balance, not just calendar-day spend.
    settled = ledger.reserve(799_392, 'old-charged')
    ledger.settle(settled, 799_392, {})
    assert ledger.snapshot()['remaining_micro'] == 2_099_360
    client = make()
    assert client.complete('JSON', 'Return a status', max_tokens=64) == {'answer': 'ok'}
    assert client.last_call['reserved_micro'] < 20_000
    assert ledger.snapshot()['held_micro'] == 2_101_248
    with ledger._transaction(write=False) as db:
        assert db.execute('SELECT charge_micro FROM reservations WHERE id=?', (unknown,)).fetchone() == (None,)


def test_request_cost_still_blocks_when_remaining_budget_is_too_small(local_client):
    from agent_service.budget import BudgetError
    make, ledger, _ = local_client
    ledger.reserve(4_999_999, 'other-pending-call')
    session = FakeSession()
    with pytest.raises(BudgetError):
        make(session).complete('JSON', 'one step')
    assert not session.posts and ledger.snapshot()['held_micro'] == 4_999_999


def test_actual_context_fit_is_checked_before_reservation(local_client):
    make, ledger, _ = local_client
    session = FakeSession(metadata=model_metadata(context_window=4000))
    with pytest.raises(ModelError, match='model_input_exceeds_context'):
        make(session).complete('JSON', 'one step')
    assert not session.posts and ledger.snapshot()['held_micro'] == 0


@pytest.mark.parametrize('case', ['prompt', 'completion', 'absolute_context', 'extra_field'])
def test_credible_bound_violation_is_charged_and_persistently_stops_new_calls(local_client, case):
    from agent_service.budget import BudgetError, BudgetLedger
    make, ledger, _ = local_client
    session = FakeSession()
    def set_usage():
        prompt = (1_048_577 if case == 'absolute_context' else
                  request_token_bound(session.posts[0][1]['data']) + 1 if case == 'prompt' else 10)
        output = 0 if case in ('prompt', 'absolute_context') else 3000
        session.result = completion(usage={'prompt_tokens':prompt, 'completion_tokens':output,
                                           'total_tokens':prompt + output})
        if case == 'extra_field':
            session.result['usage']['extension'] = float('nan')
    session.on_post = set_usage
    client = make(session)
    with pytest.raises(ModelError, match='model_usage_exceeds_request_bound'):
        client.complete('JSON', 'one step')
    cost = client.last_call['charged_micro']
    assert (cost < client.last_call['reserved_micro']) == (case == 'prompt')
    assert ledger.snapshot()['charged_micro'] == cost
    assert ledger.snapshot()['held_micro'] == 0 and ledger.snapshot()['remaining_micro'] == 0
    assert client.last_call['state'] == 'accounting_fault'
    assert set(client.last_call['usage']) == {'prompt_tokens', 'completion_tokens', 'total_tokens'}
    # A fresh instance also refuses new paid dispatch after the durable fault.
    another = FakeSession()
    with pytest.raises(BudgetError, match='accounting fault'):
        make(another).complete('JSON', 'one step')
    assert not another.posts


def test_invalid_unicode_rejected_without_network_or_hold(local_client):
    make, ledger, _ = local_client
    session = FakeSession()
    with pytest.raises(ModelError, match='model_input_invalid'):
        make(session).complete('JSON', '\ud800')
    assert not session.gets and not session.posts and ledger.snapshot()['held_micro'] == 0
