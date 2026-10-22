"""Offline checks for the shared model spending gate."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from agent_service.budget import BudgetError, BudgetLedger


NY = ZoneInfo("America/New_York")


def instant(year, month, day, hour=0, minute=0, *, fold=0):
    return datetime(year, month, day, hour, minute, tzinfo=NY, fold=fold).timestamp()


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def test_reserve_settle_snapshot_and_restart(tmp_path):
    path = tmp_path / "budget.sqlite3"
    clock = Clock(instant(2026, 10, 17, 12))
    ledger = BudgetLedger(path, clock=clock)
    rid = ledger.reserve(4_000_000, "request-a")
    assert ledger.snapshot() == {
        "limit_micro": 5_000_000, "charged_micro": 0,
        "held_micro": 4_000_000, "remaining_micro": 1_000_000,
    }
    second = BudgetLedger(path, clock=clock)
    with pytest.raises(BudgetError):
        second.reserve(1_000_001, "request-b")
    second.settle(rid, 2_000_000, {"input_tokens": 100, "output_tokens": 20})
    assert ledger.snapshot()["remaining_micro"] == 3_000_000
    assert ledger.snapshot()["charged_micro"] == 2_000_000
    assert ledger.snapshot()["held_micro"] == 0


def test_pending_survives_restart_and_never_expires(tmp_path):
    path = tmp_path / "budget.sqlite3"
    clock = Clock(instant(2026, 1, 1))
    BudgetLedger(path, clock=clock).reserve(5_000_000, "unknown-charge")
    clock.value = instant(2027, 2, 1)
    reopened = BudgetLedger(path, clock=clock)
    assert reopened.snapshot()["held_micro"] == 5_000_000
    assert reopened.snapshot()["remaining_micro"] == 0
    with pytest.raises(BudgetError):
        reopened.reserve(1, "new")


def test_cross_day_settlement_protects_both_calendar_days_and_rolling(tmp_path):
    clock = Clock(instant(2026, 1, 1, 23))
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", clock=clock)
    rid = ledger.reserve(4_000_000, "late")
    clock.value = instant(2026, 1, 2, 1)
    ledger.settle(rid, 4_000_000, {})
    assert ledger.snapshot()["charged_micro"] == 4_000_000
    assert ledger.snapshot()["remaining_micro"] == 1_000_000
    clock.value = instant(2026, 1, 1, 23, 30)
    assert ledger.snapshot()["charged_micro"] == 4_000_000
    clock.value = instant(2026, 1, 3, 1, 1)
    assert ledger.snapshot()["remaining_micro"] == 5_000_000


def test_rolling_24_hours_blocks_new_calendar_day(tmp_path):
    clock = Clock(instant(2026, 4, 1, 23, 30))
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", clock=clock)
    rid = ledger.reserve(4_000_000, "old-day")
    ledger.settle(rid, 4_000_000, {})
    clock.value = instant(2026, 4, 2, 0, 5)
    assert ledger.snapshot()["charged_micro"] == 0
    assert ledger.snapshot()["remaining_micro"] == 1_000_000
    with pytest.raises(BudgetError):
        ledger.reserve(1_000_001, "too-soon")
    clock.value = instant(2026, 4, 2, 23, 31)
    assert ledger.snapshot()["remaining_micro"] == 5_000_000


@pytest.mark.parametrize("start,next_day", [
    ((2026, 3, 7, 23, 30), (2026, 3, 8, 3, 30)),
    ((2026, 10, 31, 23, 30), (2026, 11, 1, 1, 30)),
])
def test_dst_calendar_date_and_elapsed_24_hours(tmp_path, start, next_day):
    clock = Clock(instant(*start))
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", clock=clock)
    rid = ledger.reserve(4_000_000, "before-dst")
    ledger.settle(rid, 4_000_000, {})
    clock.value = instant(*next_day)
    assert ledger.snapshot()["charged_micro"] == 0
    assert ledger.snapshot()["remaining_micro"] == 1_000_000
    clock.value = instant(*start) + 24 * 3600 + 1
    assert ledger.snapshot()["remaining_micro"] == 5_000_000


def test_invalid_amounts_and_limit_never_mutate_ledger(tmp_path):
    path = tmp_path / "budget.sqlite3"
    for bad in (5_000_001, 0, -1, 1.0, Decimal("1"), True):
        with pytest.raises(BudgetError):
            BudgetLedger(path, limit_micro=bad)
    ledger = BudgetLedger(path, limit_micro=10)
    for bad in (0, -1, 1.0, Decimal("1"), True):
        with pytest.raises(BudgetError):
            ledger.reserve(bad, "bad")
    with pytest.raises(BudgetError):
        ledger.reserve(11, "too-big")
    with pytest.raises(BudgetError):
        ledger.reserve(1, "")
    assert ledger.snapshot()["held_micro"] == 0
    assert ledger.reserve(10, "valid")


def test_settlement_bounds_idempotence_and_tampering(tmp_path):
    ledger = BudgetLedger(tmp_path / "budget.sqlite3", limit_micro=100)
    rid = ledger.reserve(80, "request")
    for bad in (-1, 80.0, Decimal("80"), True, 81):
        with pytest.raises(BudgetError):
            ledger.settle(rid, bad, {"tokens": 1})
    assert ledger.snapshot()["held_micro"] == 80
    with pytest.raises(BudgetError):
        ledger.settle("missing", 1, {})
    with pytest.raises(BudgetError):
        ledger.settle(rid, 1, {"bad": float("nan")})
    ledger.settle(rid, 60, {"b": 2, "a": 1})
    ledger.settle(rid, 60, {"a": 1, "b": 2})
    for charge, usage in ((61, {"a": 1, "b": 2}), (60, {"a": 2, "b": 2})):
        with pytest.raises(BudgetError):
            ledger.settle(rid, charge, usage)
    assert ledger.snapshot()["charged_micro"] == 60


def test_many_connections_reserve_atomically(tmp_path):
    path = tmp_path / "budget.sqlite3"
    BudgetLedger(path, limit_micro=3_000_000)

    def attempt(index):
        try:
            return BudgetLedger(path, limit_micro=3_000_000).reserve(1_000_000, str(index))
        except BudgetError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    assert sum(result is not None for result in results) == 3
    assert BudgetLedger(path, limit_micro=3_000_000).snapshot()["held_micro"] == 3_000_000


def test_separate_processes_share_atomic_budget(tmp_path):
    path = tmp_path / "budget.sqlite3"
    BudgetLedger(path, limit_micro=2_000_000)
    code = """import sys
from agent_service.budget import BudgetError, BudgetLedger
try:
    BudgetLedger(sys.argv[1], limit_micro=2_000_000).reserve(1_000_000, sys.argv[2])
    print('reserved')
except BudgetError:
    print('denied')
"""
    processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(path), str(i)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(5)]
    outcomes = [proc.communicate(timeout=30) for proc in processes]
    assert all(proc.returncode == 0 for proc in processes), outcomes
    assert [stdout.strip() for stdout, _ in outcomes].count("reserved") == 2
    assert BudgetLedger(path, limit_micro=2_000_000).snapshot()["held_micro"] == 2_000_000
