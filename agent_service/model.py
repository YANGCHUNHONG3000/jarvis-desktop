"""Budgeted JSON model client for the independent assistant, never voice Runtime."""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time

import requests
from bs4 import BeautifulSoup

from .budget import BudgetLedger
from .research import _public_search_proxy

ROOT = Path(__file__).resolve().parents[1]
PERMISSION = ROOT / "data" / "agent_service" / "model-permission.json"
KEY_FILE = ROOT / "DEEPSEEK_API_KEY.txt"
MODEL = "deepseek-flash"
API = "https://api.deepseek.com/v1"
PRICING = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
CONTEXT_LIMIT = 1_048_576
MAX_OUTPUT = 2048
# CNY / million tokens == integer micro-CNY / token. Peak, uncached ceilings.
INPUT_RATE, OUTPUT_RATE = 2, 8
PROMPT_FRAMING_ALLOWANCE = 4096
DEFAULT_TASK_SECONDS = 390
MIN_PAID_WINDOW_SECONDS = 10


class ModelError(ValueError):
    """Only stable, non-secret codes may reach logs/UI."""


def request_token_bound(encoded):
    """Conservative bound for this client's two text messages, not a token count.

    The inspected official V4 tokenizer is byte-level BPE with no expanding
    normalizer. Full JSON bytes overcount message text/escapes; the allowance
    covers role, JSON-output and generation framing. Tools/images/thinking are
    not supported here. Returned usage is checked, and violations halt billing.
    """
    if type(encoded) is not bytes or not encoded:
        raise ModelError('model_input_invalid')
    return len(encoded) + PROMPT_FRAMING_ALLOWANCE


def checked_prices(html):
    """Recognize the official CNY table; a changed/unrecognized layout stops calls."""
    table = BeautifulSoup(html, "html.parser").find("table")
    if table is None:
        raise ModelError("pricing_unrecognized")
    rows = [[c.get_text(" ", strip=True) for c in row.find_all(("th", "td"), recursive=False)]
            for row in table.find_all("tr")]
    if (not rows or len(rows[0]) != 3 or rows[0][0] != "模型"
            or not re.fullmatch(r"deepseek-flash(?:\s*\(\d+\))?", rows[0][1])
            or rows[0][2] != "deepseek-v4-pro"):
        raise ModelError("pricing_unrecognized")
    category, rates = None, {}
    for row in rows[1:]:
        labels = " ".join(row[:-2])
        if "百万tokens输入" in labels:
            category = "input" if "缓存未命中" in labels else "cached"
        elif "百万tokens输出" in labels:
            category = "output"
        if len(row) < 3 or row[-3] not in {"空闲时段", "高峰时段"}:
            if any(re.search(r"\d+(?:\.\d+)?\s*(?:元|yuan|CNY|RMB|USD|美元)|[¥￥$]\s*\d", cell, re.I) for cell in row):
                raise ModelError("pricing_unrecognized")
            continue
        if category not in {"cached", "input", "output"}:
            raise ModelError("pricing_unrecognized")
        if not re.fullmatch(r"\d+(?:\.\d+)?元", row[-2]):
            raise ModelError("pricing_unrecognized")
        value = Decimal(row[-2][:-1])
        label = (category, row[-3])
        if label in rates or not value.is_finite() or value < 0:
            raise ModelError("pricing_unrecognized")
        rates[label] = value
    if len(rates) != 6:
        raise ModelError("pricing_unrecognized")
    input_rate = max(rates[("input", period)] for period in ("空闲时段", "高峰时段"))
    output_rate = max(rates[("output", period)] for period in ("空闲时段", "高峰时段"))
    if (input_rate > INPUT_RATE or output_rate > OUTPUT_RATE
            or max(rates[("cached", period)] for period in ("空闲时段", "高峰时段")) > INPUT_RATE):
        raise ModelError("pricing_ceiling_changed")
    return {"uncached_cny_per_million": str(input_rate), "output_cny_per_million": str(output_rate)}


class CloudClient:
    def __init__(self, *, ledger=None, session=None, key_path=KEY_FILE,
                 permission_path=PERMISSION, clock=time.monotonic, deadline=None, should_stop=lambda: False):
        self.ledger = ledger if ledger is not None else BudgetLedger()
        self.session = session
        self.key_path = Path(key_path)
        self.permission_path = Path(permission_path)
        self.clock = clock
        self.deadline = self.clock() + DEFAULT_TASK_SECONDS if deadline is None else deadline
        if isinstance(self.deadline, bool) or not isinstance(self.deadline, (int, float)) or not math.isfinite(self.deadline):
            raise ModelError("model_deadline_invalid")
        self.last_call = None
        self._last_content = None  # Ephemeral diagnostic only; never emitted to events or UI.
        self.calls = 0
        if not callable(should_stop):
            raise ModelError('model_cancel_check_invalid')
        self.should_stop = should_stop

    def _check_cancelled(self):
        if self.should_stop():
            raise ModelError('model_cancelled_before_request')

    def _permission(self):
        try:
            permit = json.loads(self.permission_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ModelError("model_permission_missing") from None
        if (not isinstance(permit, dict) or permit.get("authorized") is not True
                or permit.get("daily_limit_cny") != "5.00"
                or permit.get("timezone") != "America/New_York"):
            raise ModelError("model_permission_invalid")

    def _check_deadline(self, started=None, minimum=0):
        now = self.clock()
        if now >= self.deadline or (started is not None and now - started >= 50) or self.deadline - now < minimum:
            raise ModelError("model_deadline")
        return now

    def _request_options(self, options, started, minimum=0):
        now = self._check_deadline(started, minimum)
        remaining = min(50 - (now - started), self.deadline - now)
        return {**options, "timeout": (min(5, remaining), min(35, remaining))}

    def _read(self, response, started, limit=512 * 1024):
        try:
            self._check_deadline(started)
            if response.status_code != 200:
                raise ModelError("model_http_" + str(response.status_code))
            body = bytearray()
            for chunk in response.iter_content(8192):
                self._check_deadline(started)
                body.extend(chunk)
                if len(body) > limit:
                    raise ModelError("model_response_limit")
            self._check_deadline(started)
            return bytes(body)
        finally:
            response.close()

    def complete(self, system, user, max_tokens=1024):
        self._last_content = None
        self.last_call = None
        self._check_cancelled()
        self._permission()
        if (not isinstance(system, str) or not 1 <= len(system) <= 12000
                or not isinstance(user, str) or not 1 <= len(user) <= 48000
                or type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT):
            raise ModelError("model_input_invalid")
        if self.calls >= 6:
            raise ModelError("model_call_limit")
        # This client always requests json_object. DeepSeek requires the word
        # JSON in a message; enforce locally before any paid request.
        if "json" not in (system + " " + user).lower():
            system = "Return valid JSON only. " + system
        body = {"model": MODEL, "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                "max_tokens": max_tokens, "stream": False}
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeError:
            raise ModelError('model_input_invalid') from None
        prompt_bound = request_token_bound(encoded)
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise ModelError("model_key_unavailable") from None
        if not key or "\n" in key or "\r" in key:
            raise ModelError("model_key_unavailable")
        owned = self.session is None
        session = self.session or requests.Session()
        session.trust_env = False
        proxy = _public_search_proxy() if owned else None
        options = {"proxies": {"https": proxy} if proxy else {}, "allow_redirects": False,
                   "stream": True}
        reservation = None
        try:
            # Public verification carries no Authorization header, cookies or account data.
            session.cookies.clear()
            started = self.clock()
            price_html = self._read(session.get(PRICING, **self._request_options(options, started)), started)
            prices = checked_prices(price_html)
            session.cookies.clear()
            self._check_cancelled()
            headers = {"Authorization": "Bearer " + key}
            started = self.clock()
            models = json.loads(self._read(session.get(API + "/models", headers=headers,
                                                **self._request_options(options, started)), started))
            candidates = [m for m in models.get("data", []) if isinstance(m, dict) and m.get("id") == MODEL]
            if len(candidates) != 1:
                raise ModelError("model_unavailable")
            meta = candidates[0]
            if (type(meta.get("context_window")) is not int
                    or not 1 <= meta["context_window"] <= CONTEXT_LIMIT
                    or type(meta.get("max_output_tokens")) is not int
                    or meta["max_output_tokens"] < max_tokens):
                raise ModelError("model_limits_changed")
            if prompt_bound + max_tokens > meta['context_window']:
                raise ModelError('model_input_exceeds_context')
            reserve = prompt_bound * INPUT_RATE + max_tokens * OUTPUT_RATE
            self._check_deadline(minimum=MIN_PAID_WINDOW_SECONDS)
            self._check_cancelled()
            reservation = self.ledger.reserve(reserve, sha256(encoded).hexdigest())
            self.calls += 1
            self.last_call = {"reservation_id": reservation, "model": MODEL,
                              "reserved_micro": reserve, "prices": prices, "state": "pending",
                              "budget_policy": "request_utf8_v1", "prompt_token_bound": prompt_bound}
            session.cookies.clear()
            started = self.clock()
            self._check_cancelled()
            data = json.loads(self._read(session.post(API + "/chat/completions", data=encoded,
                                  headers={**headers, "Content-Type": "application/json"},
                                  **self._request_options(options, started, MIN_PAID_WINDOW_SECONDS)), started))
            actual_model = data.get("model") if isinstance(data, dict) else None
            self.last_call["returned_model"] = actual_model if isinstance(actual_model, str) and len(actual_model) < 100 else "invalid"
            if actual_model != MODEL:
                raise ModelError("model_identity_changed")
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            prompt, completion, total = (usage.get(name) for name in
                                          ("prompt_tokens", "completion_tokens", "total_tokens"))
            if (any(type(n) is not int or n < 0 for n in (prompt, completion, total))
                    or total != prompt + completion):
                raise ModelError("model_usage_invalid")
            cost = prompt * INPUT_RATE + completion * OUTPUT_RATE
            if cost > 9_223_372_036_854_775_807:
                raise ModelError('model_usage_invalid')
            accounted_usage = {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': total}
            if prompt > prompt_bound or completion > max_tokens or total > meta['context_window']:
                self.ledger.settle_anomaly(reservation, cost, accounted_usage, reason='request_bound_exceeded')
                self.last_call.update(state='accounting_fault', charged_micro=cost, usage=accounted_usage)
                raise ModelError('model_usage_exceeds_request_bound')
            self.ledger.settle(reservation, cost, {"prompt_tokens": prompt,
                                                  "completion_tokens": completion, "total_tokens": total})
            self.last_call.update(state="settled", charged_micro=cost, usage=usage,
                                  returned_model=data.get("model"), elapsed_seconds=round(self.clock() - started, 3))
            choices = data.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                raise ModelError("model_response_incomplete")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("model_content_empty")
            self._last_content = content
            try:
                result = json.loads(content)
            except ValueError:
                raise ModelError("model_json_invalid") from None
            if not isinstance(result, dict):
                raise ModelError("model_json_invalid")
            return result
        except requests.RequestException:
            raise ModelError("model_network_failed") from None
        except (KeyError, TypeError, AttributeError, InvalidOperation, json.JSONDecodeError):
            raise ModelError("model_response_invalid") from None
        finally:
            session.cookies.clear()
            if owned:
                session.close()
