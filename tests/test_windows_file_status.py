"""Path-bound occupied prompts, including refusal of unknown dialog scopes."""
from copy import deepcopy

import pytest

from agent_service import windows_access as access
from agent_service import windows_file_status as status
from agent_service.computer import ComputerError


@pytest.fixture
def prompt(tmp_path):
    path = str(tmp_path / 'approved.docx')
    message = {'id': 'message', 'type': 'Text', 'automation_id': '', 'owner_hwnd': 200,
        'name': '文档已被占用，建议以“只读”模式打开。\n(' + path + ')',
        'visible': True, 'enabled': True, 'patterns': []}
    button = {'id': 'readonly', 'type': 'Button', 'automation_id': '', 'owner_hwnd': 200,
        'name': '只读打开', 'visible': True, 'enabled': True, 'patterns': ['invoke']}
    return path, {'application': 'wps', 'controls': [message, button], 'dialogs': [
        {'hwnd': 200, 'title': 'WPS 文字', 'class_name': 'Qt5QWindow', 'dialog_kind': 'file_status'}],
        'truncated': False}


def test_exact_occupied_path_binds_one_visible_message_and_readonly_button(prompt):
    path, observed = prompt
    message, button = status.occupied_controls(observed, path)
    assert message == observed['controls'][0] and button == observed['controls'][1]


@pytest.mark.parametrize('change', ['same_basename', 'relative', 'unknown_text', 'duplicate_message',
    'duplicate_button', 'no_invoke', 'button_automation_id', 'wrong_owner', 'hidden_message',
    'disabled_button', 'wrong_title', 'wrong_class', 'wrong_kind', 'other_application',
    'blocked', 'truncated', 'multiple_dialogs'])
def test_occupied_recognition_rejects_unbound_or_incomplete_prompt(prompt, tmp_path, change):
    path, observed = prompt
    message, button = observed['controls']
    dialog = observed['dialogs'][0]
    if change == 'same_basename':
        message['name'] = message['name'].replace(path, str(tmp_path / 'other' / 'approved.docx'))
    elif change == 'relative':
        message['name'] = message['name'].replace(path, 'approved.docx')
    elif change == 'unknown_text':
        message['name'] = '文件发生未知错误。\n(' + path + ')'
    elif change == 'duplicate_message':
        observed['controls'].append({**message, 'id': 'other-message'})
    elif change == 'duplicate_button':
        observed['controls'].append({**button, 'id': 'other-button'})
    elif change == 'no_invoke':
        button['patterns'] = []
    elif change == 'button_automation_id':
        button['automation_id'] = 'different'
    elif change == 'wrong_owner':
        button['owner_hwnd'] = 300
    elif change == 'hidden_message':
        message['visible'] = False
    elif change == 'disabled_button':
        button['enabled'] = False
    elif change == 'wrong_title':
        dialog['title'] = 'Other warning'
    elif change == 'wrong_class':
        dialog['class_name'] = '#32770'
    elif change == 'wrong_kind':
        dialog['dialog_kind'] = 'find'
    elif change == 'other_application':
        observed['application'] = 'explorer'
    elif change == 'blocked':
        observed['blocked_reason'] = 'unknown prompt'
    elif change == 'truncated':
        observed['truncated'] = True
    else:
        observed['dialogs'].append({**dialog, 'hwnd': 300})
    with pytest.raises(ComputerError, match='windows_occupied_prompt_unverified'):
        status.occupied_controls(observed, path)


@pytest.mark.parametrize('action', ['set_value', 'focus', 'reveal'])
def test_file_status_scope_cannot_grant_generic_actions(prompt, action):
    _, observed = prompt
    c = {'id': 'field', 'type': 'ComboBox' if action != 'reveal' else 'ListItem',
         'name': '查找内容(N)', 'automation_id': '', 'owner_hwnd': 200,
         'visible': action != 'reveal', 'enabled': True, 'patterns': [action]}
    observed['controls'].append(c)
    observed['target_selector'] = {k: c[k] for k in ('type', 'name', 'automation_id')}
    # A status prompt never extends the old find/focus/reveal action surface.
    assert not access.allowed(observed, c, action, 'query' if action == 'set_value' else '')
