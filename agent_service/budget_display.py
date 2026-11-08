"""Display local spend policy without implying a provider account balance."""


def budget_status_text(budget, model_label):
    limit, remaining = budget['limit_micro'], budget['remaining_micro']
    charged, held = budget['charged_micro'], budget['held_micro']
    if any(type(value) is not int or value < 0 for value in (charged, held)):
        raise ValueError('budget_display_invalid')
    if limit is None:
        if remaining is not None or type(budget.get('faulted')) is not bool:
            raise ValueError('budget_display_invalid')
        policy = '本地金额上限已取消'
    else:
        if (type(limit) is not int or limit <= 0 or type(remaining) is not int
                or not 0 <= remaining <= limit):
            raise ValueError('budget_display_invalid')
        policy = f'每日/滚动24h本地上限 ¥{limit/1e6:.2f}'
    text = f'{model_label} · {policy} · 今日计账 ¥{charged/1e6:.4f}'
    if remaining is not None:
        text += f' · 可用 ¥{remaining/1e6:.4f}'
    if held:
        text += f' · 待核费用 ¥{held/1e6:.4f}'
    if budget.get('faulted'):
        text += ' · 记账异常，模型调用已停止'
    return text
