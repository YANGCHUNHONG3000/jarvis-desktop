"""Shared, conservative budget ledger for paid model requests.

All unresolved reservations count against every future window. A settled charge
counts on both its reservation and settlement calendar days, and in a rolling
window if either timestamp is in that window. This intentionally overcounts a
cross-day/long-running request rather than making uncertain spending available.
"""

from contextlib import contextmanager
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo


DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "data" / "agent_service" / "model-budget.sqlite3"
_MAX_LIMIT_MICRO = 5_000_000
_DAY_ZONE = ZoneInfo("America/New_York")
_ROLLING_SECONDS = 24 * 60 * 60


class BudgetError(ValueError):
    """Invalid budget operation or insufficient budget."""


class BudgetLedger:
    def __init__(self, path=DEFAULT_LEDGER, limit_micro=5_000_000, clock=time.time):
        if type(limit_micro) is not int or not 0 < limit_micro <= _MAX_LIMIT_MICRO:
            raise BudgetError("limit_micro must be an integer from 1 to 5,000,000")
        if not callable(clock):
            raise BudgetError("clock must be callable")
        self.path = Path(path)
        self.limit_micro = limit_micro
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                reserved_micro INTEGER NOT NULL CHECK (reserved_micro > 0),
                reserved_at REAL NOT NULL,
                reserved_day TEXT NOT NULL,
                settled_at REAL,
                settled_day TEXT,
                charge_micro INTEGER CHECK (charge_micro >= 0),
                usage_json TEXT,
                CHECK ((settled_at IS NULL AND settled_day IS NULL
                        AND charge_micro IS NULL AND usage_json IS NULL)
                    OR (settled_at IS NOT NULL AND settled_day IS NOT NULL
                        AND charge_micro IS NOT NULL AND usage_json IS NOT NULL))
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS budget_faults (
                reservation_id TEXT PRIMARY KEY REFERENCES reservations(id),
                fault_code TEXT NOT NULL CHECK
                    (fault_code IN ('reservation_overrun', 'request_bound_exceeded')),
                reserved_micro INTEGER NOT NULL CHECK (reserved_micro > 0),
                charge_micro INTEGER NOT NULL CHECK (charge_micro >= 0),
                detected_at REAL NOT NULL
            )""")

    @contextmanager
    def _transaction(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise BudgetError("clock must return a finite Unix timestamp")
        return float(now), datetime.fromtimestamp(now, _DAY_ZONE).date().isoformat()

    @staticmethod
    def _totals(db, now, day):
        cutoff = now - _ROLLING_SECONDS
        held = db.execute("SELECT COALESCE(SUM(reserved_micro), 0) FROM reservations WHERE charge_micro IS NULL").fetchone()[0]
        daily = db.execute("""SELECT COALESCE(SUM(charge_micro), 0) FROM reservations
            WHERE charge_micro IS NOT NULL AND (reserved_day = ? OR settled_day = ?)""",
            (day, day)).fetchone()[0]
        rolling = db.execute("""SELECT COALESCE(SUM(charge_micro), 0) FROM reservations
            WHERE charge_micro IS NOT NULL AND (reserved_at >= ? OR settled_at >= ?)""",
            (cutoff, cutoff)).fetchone()[0]
        return held, daily, rolling

    def reserve(self, amount_micro: int, request_hash: str) -> str:
        if type(amount_micro) is not int or amount_micro <= 0:
            raise BudgetError("amount_micro must be a positive integer")
        if not isinstance(request_hash, str) or not request_hash.strip():
            raise BudgetError("request_hash must be a nonempty string")
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM budget_faults LIMIT 1").fetchone():
                raise BudgetError("model budget accounting fault")
            now, day = self._now()
            held, daily, rolling = self._totals(db, now, day)
            if (amount_micro > self.limit_micro - held - daily
                    or amount_micro > self.limit_micro - held - rolling):
                raise BudgetError("model budget exceeded")
            reservation_id = uuid.uuid4().hex
            db.execute("""INSERT INTO reservations
                (id, request_hash, reserved_micro, reserved_at, reserved_day)
                VALUES (?, ?, ?, ?, ?)""",
                (reservation_id, request_hash, amount_micro, now, day))
            return reservation_id

    def settle(self, reservation_id: str, charge_micro: int, usage: dict) -> None:
        if not isinstance(reservation_id, str) or not reservation_id:
            raise BudgetError("reservation_id must be a nonempty string")
        if type(charge_micro) is not int or charge_micro < 0:
            raise BudgetError("charge_micro must be a nonnegative integer")
        if type(usage) is not dict:
            raise BudgetError("usage must be a JSON-serializable dict")
        try:
            usage_json = json.dumps(usage, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BudgetError("usage must be a JSON-serializable dict") from exc
        with self._transaction() as db:
            row = db.execute("""SELECT reserved_micro, charge_micro, usage_json
                FROM reservations WHERE id = ?""", (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown reservation")
            reserved, prior_charge, prior_usage = row
            if prior_charge is not None:
                if charge_micro == prior_charge and usage_json == prior_usage:
                    return
                raise BudgetError("reservation already settled with different values")
            if charge_micro > reserved:
                raise BudgetError("charge_micro exceeds reserved amount")
            now, day = self._now()
            db.execute("""UPDATE reservations SET settled_at = ?, settled_day = ?,
                charge_micro = ?, usage_json = ? WHERE id = ? AND charge_micro IS NULL""",
                (now, day, charge_micro, usage_json, reservation_id))

    def settle_anomaly(self, reservation_id: str, charge_micro: int, usage: dict, *, reason: str) -> None:
        """Record trustworthy usage outside request bounds and stop new calls.

        This records spending that already happened; it never authorizes extra
        spending. Ordinary settlement keeps its original reservation ceiling.
        """
        if reason not in ("reservation_overrun", "request_bound_exceeded"):
            raise BudgetError("unknown budget anomaly reason")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise BudgetError("reservation_id must be a nonempty string")
        if type(charge_micro) is not int or charge_micro < 0:
            raise BudgetError("charge_micro must be a nonnegative integer")
        if type(usage) is not dict:
            raise BudgetError("usage must be a JSON-serializable dict")
        try:
            usage_json = json.dumps(usage, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BudgetError("usage must be a JSON-serializable dict") from exc
        with self._transaction() as db:
            row = db.execute("""SELECT reserved_micro, charge_micro, usage_json
                FROM reservations WHERE id = ?""", (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown reservation")
            reserved, prior_charge, prior_usage = row
            if reason == "reservation_overrun" and charge_micro <= reserved:
                raise BudgetError("overrun charge must exceed reserved amount")
            if prior_charge is not None:
                prior_fault = db.execute("SELECT fault_code FROM budget_faults WHERE reservation_id = ?",
                                         (reservation_id,)).fetchone()
                if (charge_micro == prior_charge and usage_json == prior_usage
                        and prior_fault == (reason,)):
                    return
                raise BudgetError("reservation already settled with different values")
            now, day = self._now()
            db.execute("""UPDATE reservations SET settled_at = ?, settled_day = ?,
                charge_micro = ?, usage_json = ? WHERE id = ? AND charge_micro IS NULL""",
                (now, day, charge_micro, usage_json, reservation_id))
            db.execute("""INSERT INTO budget_faults
                (reservation_id, fault_code, reserved_micro, charge_micro, detected_at)
                VALUES (?, ?, ?, ?, ?)""",
                (reservation_id, reason, reserved, charge_micro, now))

    def settle_overrun(self, reservation_id: str, charge_micro: int, usage: dict) -> None:
        """Record a charge above its reservation without authorizing new spend."""
        self.settle_anomaly(reservation_id, charge_micro, usage, reason="reservation_overrun")

    def snapshot(self) -> dict:
        with self._transaction(write=False) as db:
            now, day = self._now()
            held, daily, rolling = self._totals(db, now, day)
            faulted = db.execute("SELECT 1 FROM budget_faults LIMIT 1").fetchone() is not None
            return {
                "limit_micro": self.limit_micro,
                "charged_micro": daily,
                "held_micro": held,
                "remaining_micro": 0 if faulted else max(0, min(self.limit_micro - held - daily,
                                                               self.limit_micro - held - rolling)),
            }
