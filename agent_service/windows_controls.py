"""Small, state-verifiable UIA operations within explicitly selected apps.

This module does not authorize windows, invoke buttons, or verify business
effects. The caller owns identity/input checks and durable before/after evidence.
"""
import re

from .computer import ComputerError

_DANGER = re.compile(
    r'删除|移除|清空|发送|发信|提交|购买|付款|支付|订阅|订单|安装|卸载|'
    r'权限|授权|管理员|登录|登入|登出|密码|验证码|安全|隐私|共享|分享|上传|下载|'
    r'宏|凭据|密钥|证书|防火墙|杀毒|允许访问|网络访问|启用编辑|自动启动|开机|'
    r'执行|运行|命令|终端|脚本|文件名|文件路径|地址栏|另存|保存|替换|'
    r'\b(?:delete|remove|clear|send|submit|buy|purchase|pay|payment|subscribe|order|'
    r'install|uninstall|permission|authorize|admin|login|log\s*in|sign\s*in|'
    r'logout|log\s*out|sign\s*out|password|security|privacy|share|upload|download|'
    r'macro|macros|credential|credentials|token|certificate|firewall|trust|allow|'
    r'execute|run|command|terminal|script|file\s*name|file\s*path|address|save|replace)\b',
    re.IGNORECASE,
)
_SEARCH = re.compile(r'^(?:搜索|查找|搜尋|尋找|search\b|find\b)', re.IGNORECASE)


def _binary(value):
    return type(value) in (bool, int) and value in (0, 1)


def _safe_context(control):
    parents = control.get('ancestors', [])
    if type(parents) is not list or any(type(p) is not dict for p in parents):
        return False
    if 'ancestor' in control:
        if type(control['ancestor']) is not dict:
            return False
        parents = [*parents, control['ancestor']]
    for item in [control, *parents]:
        for key in ('name', 'automation_id'):
            label = item.get(key, '')
            if type(label) is not str:
                return False
            words = re.sub(r'(?<=[a-z])(?=[A-Z])|[_-]', ' ', label)
            if _DANGER.search(words):
                return False
    return True


def _ready(observation, control):
    if type(observation) is not dict or type(observation.get('dialogs', [])) is not list:
        return False
    return (observation.get('application') in {'explorer', 'wps'}
            and not observation.get('blocked_reason') and not observation.get('truncated')
            and not any(d.get('dialog_kind') == 'file_status'
                        for d in observation.get('dialogs', []) if type(d) is dict)
            and type(control) is dict and control.get('visible') is True
            and control.get('enabled') is True and not control.get('password')
            and not control.get('is_password') and _safe_context(control))


def allowed(observation, control, action, value):
    """Only new generic actions; existing List/Tree policy stays with caller."""
    if not _ready(observation, control):
        return False
    patterns = control.get('patterns', [])
    if type(patterns) is not list or action not in patterns:
        return False
    kind = control.get('type')
    if action == 'select':
        return (kind in {'TabItem', 'RadioButton'} and value == ''
                and _binary(control.get('selected')))
    if action == 'toggle':
        return (kind == 'CheckBox' and type(value) is str and value in {'0', '1'}
                and _binary(control.get('checked')))
    if action == 'set_value':
        name = control.get('name', '').strip()
        return (kind in {'Edit', 'ComboBox'} and bool(_SEARCH.match(name))
                and type(value) is str and len(value) <= 300
                and not any(ord(c) < 32 for c in value))
    return False


def states(observation, control):
    """List states offered to the local UI/model, never action permissions."""
    result = []
    for state, action, value in [('selected', 'select', ''), ('checked', 'toggle', '1'),
                                  ('value', 'set_value', '')]:
        if allowed(observation, control, action, value):
            result.append(state)
    return result


def candidate(observation, condition, control):
    """Return one action, or None for denied/already-satisfied targets.

    Toggle uses a desired binary state string, not a blind flip instruction.
    """
    if type(condition) is not dict or type(control) is not dict:
        return None
    state, value = condition.get('state'), condition.get('value')
    if state == 'selected' and _binary(value) and value == 1:
        action, argument, current = 'select', '', control.get('selected')
    elif state == 'checked' and _binary(value):
        action, argument, current = 'toggle', str(int(value)), control.get('checked')
    elif state == 'value' and type(value) is str:
        action, argument, current = 'set_value', value, control.get('value')
    else:
        return None
    if not allowed(observation, control, action, argument) or current == value:
        return None
    identifier = control.get('id')
    if type(identifier) is not str or not identifier:
        return None
    return {'control_id': identifier, 'action': action, 'value': argument}


def bounds(wrapper):
    """Return native screen bounds; unavailable/empty geometry grants no click."""
    rect = wrapper.rectangle()
    values = [rect.left, rect.top, rect.right, rect.bottom]
    if (any(type(v) is not int for v in values)
            or rect.right <= rect.left or rect.bottom <= rect.top):
        return None
    return values


def enrich(wrapper, record):
    """Add bounds and a readable Toggle state to an existing protected record.

    Pattern absence is optional; provider failures propagate and stop observation.
    The caller must reject password/foreign-process nodes before invoking this.
    """
    if record is None:
        return None
    from pywinauto.uia_defines import NoPatternInterfaceError
    record['bounds'] = bounds(wrapper)
    record['checked'] = None
    if record.get('type') != 'CheckBox':
        return record
    try:
        pattern = wrapper.iface_toggle
    except (NoPatternInterfaceError, AttributeError):
        return record
    value = pattern.CurrentToggleState
    # Keep indeterminate distinguishable, but never offer a toggle for it.
    if type(value) is not int or value not in (0, 1, 2):
        raise ComputerError('windows_toggle_state_invalid')
    record['checked'] = value
    if 'toggle' not in record['patterns']:
        record['patterns'].append('toggle')
    return record


def perform(wrapper, control, action, value):
    """Dispatch once after caller policy/identity/intent checks; no verification.

    Toggle rereads its state immediately to avoid flipping an already met goal.
    """
    if action == 'toggle' and control.get('type') == 'CheckBox' and value in {'0', '1'}:
        pattern = wrapper.iface_toggle
        actual = pattern.CurrentToggleState
        if not _binary(actual) or actual != control.get('checked'):
            raise ComputerError('screen_changed_reobserve')
        if actual != int(value):
            pattern.Toggle()
    elif action == 'select' and control.get('type') in {'TabItem', 'RadioButton'} and value == '':
        wrapper.iface_selection_item.Select()
    elif action == 'set_value' and control.get('type') in {'Edit', 'ComboBox'}:
        pattern = wrapper.iface_value
        if pattern.CurrentIsReadOnly:
            raise ComputerError('control_readonly')
        pattern.SetValue(value)
    else:
        raise ComputerError('windows_action_denied')
