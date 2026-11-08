"""Offline tests: no live authorization, credentials, network, or shared ledger."""
import json

import pytest

from agent_service import budget, model_policy
from agent_service.budget import BudgetError, BudgetLedger


def permission(mode=None):
    value = {'authorized': True, 'timezone': 'America/New_York', 'daily_limit_cny': '5.00'}
    if mode == 'unlimited':
        value.update(budget_mode='unlimited', daily_limit_cny=None, rolling_limit_cny=None)
    elif mode is not None:
        value['budget_mode'] = mode
    return value


def write_permission(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


@pytest.fixture
def production(tmp_path, monkeypatch):
    ledger = tmp_path / 'production.sqlite3'
    permit = tmp_path / 'permission.json'
    monkeypatch.setattr(budget, 'DEFAULT_LEDGER', ledger)
    monkeypatch.setattr(model_policy, 'PERMISSION', permit)
    write_permission(permit, permission())
    return ledger, permit


def test_legacy_and_explicit_unlimited_modes_are_strict():
    assert model_policy.budget_mode(permission()) == 'limited'
    assert model_policy.budget_mode(permission('limited')) == 'limited'
    assert model_policy.budget_mode(permission('unlimited')) == 'unlimited'
    invalid = [
        {**permission('unlimited'), 'authorized': False},
        {**permission('unlimited'), 'timezone': 'UTC'},
        {**permission('unlimited'), 'daily_limit_cny': '5.00'},
        {**permission('unlimited'), 'rolling_limit_cny': '5.00'},
        {k: v for k, v in permission('unlimited').items() if k != 'rolling_limit_cny'},
        {**permission(), 'budget_mode': None},
        {**permission(), 'budget_mode': 'unlimited'},
        {**permission('limited'), 'rolling_limit_cny': None},
        {**permission(), 'rolling_limit_cny': None},
    ]
    for item in invalid:
        with pytest.raises(model_policy.PermissionPolicyError, match='model_permission_invalid'):
            model_policy.budget_mode(item)


def test_effective_limit_is_readonly_and_custom_ledger_remains_limited(production, tmp_path):
    path, permit = production
    write_permission(permit, permission('unlimited'))
    assert budget.effective_limit(path) is None
    assert not path.exists()
    permit.unlink()
    assert budget.effective_limit(tmp_path / 'isolated.sqlite3', 123) == 123
    assert not (tmp_path / 'isolated.sqlite3').exists()
    with pytest.raises(BudgetError, match='model_permission_missing'):
        budget.effective_limit(path)


def test_live_policy_changes_existing_production_ledger_without_releasing_hold(production):
    path, permit = production
    ledger = BudgetLedger(path)
    other_process_view = BudgetLedger(path)
    old = ledger.reserve(4_900_000, 'old-unknown')
    with pytest.raises(BudgetError, match='budget exceeded'):
        ledger.reserve(200_000, 'limited')
    write_permission(permit, permission('unlimited'))
    new = other_process_view.reserve(8_000_000, 'unlimited')
    assert ledger.snapshot() == {'limit_micro': None, 'remaining_micro': None,
                                  'held_micro': 12_900_000, 'charged_micro': 0, 'faulted': False}
    ledger.settle(new, 6_000_000, {'tokens': 1})
    assert ledger.snapshot()['held_micro'] == 4_900_000
    with ledger._transaction(write=False) as db:
        assert db.execute('SELECT charge_micro FROM reservations WHERE id=?', (old,)).fetchone() == (None,)
    write_permission(permit, permission())
    assert ledger.snapshot()['limit_micro'] == 5_000_000
    assert ledger.snapshot()['remaining_micro'] == 0
    with pytest.raises(BudgetError, match='budget exceeded'):
        ledger.reserve(1, 'limited-again')


def test_unlimited_does_not_clear_or_bypass_accounting_fault(production):
    path, permit = production
    ledger = BudgetLedger(path)
    reservation = ledger.reserve(100, 'overrun')
    ledger.settle_overrun(reservation, 101, {'tokens': 1})
    write_permission(permit, permission('unlimited'))
    assert ledger.snapshot() == {'limit_micro': None, 'remaining_micro': None,
                                  'held_micro': 0, 'charged_micro': 101, 'faulted': True}
    with pytest.raises(BudgetError, match='accounting fault'):
        ledger.reserve(1, 'still-blocked')
    with ledger._transaction(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM budget_faults').fetchone() == (1,)


def test_temporary_ledger_never_inherits_unlimited(production, tmp_path):
    _, permit = production
    write_permission(permit, permission('unlimited'))
    isolated = BudgetLedger(tmp_path / 'isolated.sqlite3', limit_micro=100)
    isolated.reserve(100, 'bounded')
    assert isolated.snapshot()['remaining_micro'] == 0
    with pytest.raises(BudgetError, match='budget exceeded'):
        isolated.reserve(1, 'still-bounded')


def test_production_policy_corruption_fails_closed_for_existing_instance(production):
    path, permit = production
    ledger = BudgetLedger(path)
    for value in (permission('unexpected'), {**permission('unlimited'), 'rolling_limit_cny': '5.00'}):
        write_permission(permit, value)
        with pytest.raises(BudgetError, match='model_permission_invalid'):
            ledger.reserve(1, 'blocked')
        with pytest.raises(BudgetError, match='model_permission_invalid'):
            ledger.snapshot()
    permit.write_text('{"authorized": true, "authorized": false}', encoding='utf-8')
    with pytest.raises(BudgetError, match='model_permission_invalid'):
        ledger.reserve(1, 'duplicate-key')
    with ledger._transaction(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM reservations').fetchone() == (0,)
