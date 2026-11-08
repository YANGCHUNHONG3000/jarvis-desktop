"""Approved local DOCX/XLSX open goals over the bounded Windows backend."""
from .computer import ComputerError
from . import windows_file_dialog as files

ACTIONS = {'open_file_dialog', 'set_file_path', 'open_local_file', 'open_readonly'}
READONLY_SELECTOR = {'type': 'Document', 'name': '已只读打开本地文件',
                     'automation_id': 'jarvis.readonly_document'}


def is_goal(selector):
    return selector in (files.OPEN_DOCUMENT_SELECTOR, READONLY_SELECTOR)


def observe(window, token, check, native_observe):
    target = files.decode_token(token)
    from .windows_access import _file_application
    if not _file_application(window, target['path']):
        raise ComputerError('windows_file_application_invalid')
    result, wrappers = native_observe()
    if result['application'] != 'wps':
        raise ComputerError('windows_file_application_invalid')
    result['file_token'] = token
    selector = result.get('target_selector', files.OPEN_DOCUMENT_SELECTOR)
    value = ''
    if not result.get('dialogs') and not result.get('blocked_reason'):
        active = files.document_is_active(window, target['path'], check)
        if active and selector == READONLY_SELECTOR:
            status = files.document_status(window, target['path'], check)
            result['document_status'] = status
            active = status['active'] and status['read_only'] is True
        if active:
            files.decode_token(token)
            value = token
    result['controls'].append(dict(selector,
        id='jarvis-open-document', runtime_id=[], enabled=True, visible=True,
        focused=False, selected=None, expanded=None, patterns=[], value=value))
    if len(result['controls']) > 200:
        result['truncated'] = True
    return result, wrappers


def allowed(observation, control, action, token):
    if (action not in ACTIONS or observation.get('application') != 'wps'
            or observation.get('blocked_reason') or observation.get('truncated')
            or token != observation.get('file_token')
            or not is_goal(observation.get('target_selector'))):
        return False
    try:
        target = files.decode_token(token)
        if action == 'open_file_dialog':
            return control is None and not observation.get('dialogs')
        if action == 'open_readonly':
            from .windows_file_status import occupied_controls
            _, button = occupied_controls(observation, target['path'])
            return control == button
        if action == 'set_file_path':
            field, _ = files.filename_edit(observation)
            return control == field and field.get('value') != target['path']
        field, edit = files.filename_edit(observation)
        return (control == files.open_button(observation) and 'invoke' in control.get('patterns', [])
                and field.get('value') == edit.get('value') == target['path'])
    except ComputerError:
        return False


def candidates(observation, condition):
    if condition['state'] != 'value' or condition['value'] != observation.get('file_token'):
        return []
    if (condition['selector'] == READONLY_SELECTOR
            and observation.get('document_status', {}).get('active') is True
            and observation['document_status'].get('read_only') is False):
        return []
    token = condition['value']
    try:
        target = files.decode_token(token)
        if not observation.get('dialogs'):
            control, action = None, 'open_file_dialog'
        elif len(observation['dialogs']) == 1 and observation['dialogs'][0].get('dialog_kind') == 'file_status':
            from .windows_file_status import occupied_controls
            _, control = occupied_controls(observation, target['path'])
            action = 'open_readonly'
        elif files.filename_field(observation).get('value') != target['path']:
            control, action = files.filename_field(observation), 'set_file_path'
        else:
            control, action = files.open_button(observation), 'open_local_file'
        if allowed(observation, control, action, token):
            return [{'control_id': control['id'] if control else None, 'action': action, 'value': token}]
    except ComputerError:
        pass
    return []


def verified(action, after, token):
    target = files.decode_token(token)
    if after.get('blocked_reason') or after.get('truncated'):
        return False
    if action == 'open_local_file' and after.get('dialogs'):
        try:
            from .windows_file_status import occupied_controls
            occupied_controls(after, target['path'])
            return True
        except ComputerError:
            return False
    if action in {'open_local_file', 'open_readonly'}:
        controls = [c for c in after['controls'] if c.get('id') == 'jarvis-open-document']
        return not after.get('dialogs') and len(controls) == 1 and controls[0]['value'] == token
    try:
        field, edit = files.filename_edit(after)
        if action == 'open_file_dialog':
            return True
        return action == 'set_file_path' and field.get('value') == edit.get('value') == target['path']
    except ComputerError:
        return False
