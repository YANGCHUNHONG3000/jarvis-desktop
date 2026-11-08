"""Recognize only the path-bound WPS occupied-document read-only prompt."""
from pathlib import Path
from .computer import ComputerError


def workbook_occupied_controls(observation, approved_path):
    """Recognize only; basename alone never authorizes a stand-alone click.

    The native open adapter may use this immediately after invoking its exact
    full-path file selection. It must also recheck owner/process and controls.
    """
    dialogs = observation.get('dialogs', [])
    path = Path(approved_path)
    if (not path.is_absolute() or path.suffix.casefold() != '.xlsx'
            or observation.get('application') != 'wps'
            or observation.get('window', {}).get('class_name') not in {'XLMAIN', 'OpusApp'}
            or observation.get('blocked_reason') or observation.get('truncated')
            or len(dialogs) != 1 or dialogs[0].get('dialog_kind') != 'file_status'
            or dialogs[0].get('title') != '文件正在使用'
            or dialogs[0].get('class_name') != 'bosa_sdm_XL9'):
        raise ComputerError('windows_occupied_prompt_unverified')
    controls = [c for c in observation['controls'] if c.get('owner_hwnd') == dialogs[0]['hwnd']]
    messages = [c for c in controls if c['type'] == 'Text'
                and c.get('name') == path.name + ' 正处于锁定状态,']
    buttons = [c for c in controls if c['type'] == 'Button' and c.get('name') == '只读(R)'
               and c.get('automation_id') == '' and 'invoke' in c.get('patterns', [])]
    if (len(messages) != 1 or len(buttons) != 1
            or not all(c.get('visible') and c.get('enabled') for c in [*messages, *buttons])):
        raise ComputerError('windows_occupied_prompt_unverified')
    return messages[0], buttons[0]


def occupied_controls(observation, approved_path):
    dialogs = observation.get('dialogs', [])
    if (observation.get('application') != 'wps' or observation.get('truncated')
            or observation.get('blocked_reason') or len(dialogs) != 1
            or dialogs[0].get('dialog_kind') != 'file_status'
            or dialogs[0].get('title') != 'WPS 文字' or dialogs[0].get('class_name') != 'Qt5QWindow'):
        raise ComputerError('windows_occupied_prompt_unverified')
    controls = [c for c in observation['controls'] if c.get('owner_hwnd') == dialogs[0]['hwnd']]
    messages = []
    for control in controls:
        text = control.get('name', '')
        if control['type'] != 'Text' or not text.startswith('文档已被占用，建议以“只读”模式打开。'):
            continue
        parts = text.rsplit('\n(', 1)
        if len(parts) == 2 and parts[1].endswith(')'):
            path = parts[1][:-1]
            if Path(path).is_absolute() and Path(path) == Path(approved_path):
                messages.append(control)
    buttons = [c for c in controls if c['type'] == 'Button' and c['name'] == '只读打开'
               and c.get('automation_id') == '' and 'invoke' in c.get('patterns', [])]
    if (len(messages) != 1 or len(buttons) != 1
            or not all(c.get('visible') and c.get('enabled') for c in [messages[0], buttons[0]])):
        raise ComputerError('windows_occupied_prompt_unverified')
    return messages[0], buttons[0]
