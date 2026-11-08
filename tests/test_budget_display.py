import pytest

from agent_service.budget_display import budget_status_text


def test_uncapped_shows_spend_and_unknown_hold_not_infinite_balance():
    value = budget_status_text(dict(limit_micro=None, remaining_micro=None,
        charged_micro=6_000_000, held_micro=2_364_618, faulted=False), 'GPT')
    assert '本地金额上限已取消' in value and '今日计账 ¥6.0000' in value
    assert '待核费用 ¥2.3646' in value and '可用' not in value and '¥5' not in value


def test_uncapped_accounting_fault_is_visible():
    value = budget_status_text(dict(limit_micro=None, remaining_micro=None,
        charged_micro=0, held_micro=0, faulted=True), 'GPT')
    assert '模型调用已停止' in value


def test_bounded_display_uses_snapshot_limit():
    value = budget_status_text(dict(limit_micro=3_000_000, remaining_micro=1_000_000,
        charged_micro=2_000_000, held_micro=0), 'GPT')
    assert '本地上限 ¥3.00' in value and '可用 ¥1.0000' in value


@pytest.mark.parametrize('changed', [{'remaining_micro':0}, {'faulted':None}, {'held_micro':-1}])
def test_bad_uncapped_snapshot_is_not_displayed_as_available(changed):
    values = dict(limit_micro=None, remaining_micro=None, charged_micro=0, held_micro=0, faulted=False)
    with pytest.raises(ValueError, match='budget_display_invalid'):
        budget_status_text({**values, **changed}, 'GPT')
