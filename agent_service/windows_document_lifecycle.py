"""Observed WPS copy-save and close goals over immutable copy receipts."""
from .computer import ComputerError
from . import windows_file_dialog as files

SAVE_SELECTOR = {'type': 'Document', 'name': '已保存本地副本', 'automation_id': 'jarvis.saved_copy'}
CLOSE_SELECTOR = {'type': 'Document', 'name': '已关闭已保存副本', 'automation_id': 'jarvis.closed_copy'}
ACTIONS = {'save_document_copy', 'close_saved_document'}


def is_goal(selector):
    return selector in (SAVE_SELECTOR, CLOSE_SELECTOR)


def observe(window, token, selector, check, native_observe):
    from . import windows_document_copy as copies
    manifest = copies.decode(token)
    from .windows_access import _file_application
    if not _file_application(window, manifest['source']['path']):
        raise ComputerError('windows_file_application_invalid')
    receipt = copies.saved_receipt(token)
    result, wrappers = native_observe()
    if result['application'] != 'wps':
        raise ComputerError('windows_file_application_invalid')
    state = {'saved': receipt is not None, 'active': False, 'read_only': None, 'clean': None, 'open': None}
    if not result.get('dialogs') and not result.get('blocked_reason'):
        path = receipt['staging']['path'] if receipt else manifest['source']['path']
        status = files.document_status(window, path, check)
        state.update(active=status['active'], read_only=status['read_only'], clean=status['saved'])
        if receipt:
            state['open'] = files.document_present(path, check, window=window)
    result['lifecycle_token'], result['copy_state'] = token, state
    reached = state['saved'] and (selector == SAVE_SELECTOR or state['open'] is False)
    result['controls'].append(dict(selector, id='jarvis-document-lifecycle', runtime_id=[],
        enabled=True, visible=True, focused=False, selected=None, expanded=None, patterns=[],
        value=token if reached else ''))
    if len(result['controls']) > 200:
        result['truncated'] = True
    return result, wrappers


def allowed(observation, control, action, token):
    if (control is not None or action not in ACTIONS or observation.get('application') != 'wps'
            or observation.get('dialogs') or observation.get('blocked_reason') or observation.get('truncated')
            or token != observation.get('lifecycle_token')):
        return False
    state = observation.get('copy_state', {})
    if action == 'save_document_copy':
        return (observation.get('target_selector') == SAVE_SELECTOR
                and state.get('saved') is False and state.get('active') is True)
    return (observation.get('target_selector') == CLOSE_SELECTOR and state.get('saved') is True
            and state.get('active') is True and state.get('clean') is True and state.get('open') is True)


def candidates(observation, condition):
    if condition['state'] != 'value':
        return []
    action = 'save_document_copy' if condition['selector'] == SAVE_SELECTOR else 'close_saved_document'
    state = observation.get('copy_state', {})
    if (action == 'close_saved_document' and state.get('saved') is True
            and state.get('open') is True and not observation.get('dialogs')
            and not observation.get('blocked_reason') and not observation.get('truncated')):
        if state.get('active') is not True:
            raise ComputerError('windows_document_not_active')
        if state.get('clean') is not True:
            raise ComputerError('windows_document_unsaved_changes')
    return ([{'control_id': None, 'action': action, 'value': condition['value']}]
            if allowed(observation, None, action, condition['value']) else [])


def preflight(window, token, action, check):
    from . import windows_document_copy as copies
    manifest = copies.decode(token)
    receipt = copies.saved_receipt(token)
    if action == 'save_document_copy':
        if receipt is not None:
            raise ComputerError('screen_changed_reobserve')
        return files.bound_document(window, manifest['source']['path'], check)
    if action != 'close_saved_document' or receipt is None:
        raise ComputerError('windows_copy_receipt_required')
    document, status = files.bound_document(window, receipt['staging']['path'], check)
    if status['saved'] is not True:
        raise ComputerError('windows_document_unsaved_changes')
    return document, status


def verified(action, after, token):
    if after.get('blocked_reason') or after.get('dialogs') or after.get('truncated'):
        return False
    state = after.get('copy_state', {})
    if token != after.get('lifecycle_token') or state.get('saved') is not True:
        return False
    return (state.get('open') is False if action == 'close_saved_document'
            else action == 'save_document_copy' and state.get('active') is True and state.get('clean') is True)
