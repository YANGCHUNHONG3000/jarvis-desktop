"""Deterministic policy over existing guarded Windows executors.

This policy chooses once; it never retries with another executor. Component
bindings are part of the persisted policy so recovery cannot upgrade a driver.
"""
from copy import deepcopy

from .computer import ComputerError, digest, read_record
from .windows_winapp import PROVIDER_BINDING as WINAPP, OFFICE_PROVIDER_BINDING as OFFICE
from .windows_mcp import PROVIDER_BINDING as MCP

PROVIDER_BINDING = {'provider': 'auto', 'version': 1,
    'native_contract': 'windows-guarded-v1',
    'components': {'winapp': deepcopy(WINAPP), 'office': deepcopy(OFFICE), 'mcp': deepcopy(MCP)},
    'policy': 'mcp-state-winapp-tree-native-value-specialized-v1'}


def options(observation, condition):
    from . import windows_access as access, windows_winapp_office as office
    from . import windows_visual as visual, windows_visual_forms as forms, windows_reviewed_controls as reviewed
    if any(module.is_goal(condition['selector']) for module in (visual, forms, reviewed)):
        return []
    if office.is_goal(condition['selector']):
        return office.candidates(observation, condition)
    controls = {item['id']: item for item in observation['controls']}
    return [item for item in access.candidates(observation, condition)
            if route(observation, controls.get(item['control_id']), item['action'], item['value'],
                     condition['selector']) is not None]


def route(observation, control, action, value, selector=None):
    from . import windows_access as access, windows_winapp_office as office
    from . import windows_file_actions as files, windows_document_lifecycle as lifecycle, windows_content_find as content
    if office.is_goal(selector):
        if action in office.ACTION.values():
            return {'provider': deepcopy(OFFICE), 'route': OFFICE['routes'][action]}
        return None
    if not access.allowed(observation, control, action, value):
        return None
    if any(module.is_goal(selector) for module in (files, lifecycle, content)):
        return {'provider': None, 'route': 'native-specialized-v1'}
    if access.WindowsMcpBackend.allowed(observation, control, action, value):
        return {'provider': deepcopy(MCP), 'route': MCP['routes'][action]}
    kind = control.get('type') if type(control) is dict else None
    if ((kind in {'ListItem', 'TreeItem'} and action == 'select')
            or (kind == 'TreeItem' and action in {'expand', 'collapse'})):
        if access.WinappBackend.allowed(observation, control, action, value):
            return {'provider': deepcopy(WINAPP), 'route': WINAPP['routes'][action]}
    if action == 'set_value':
        return {'provider': None, 'route': 'native-ValuePattern'}
    if action == 'reveal' and kind in {'ListItem', 'TreeItem', 'DataItem'}:
        return {'provider': None, 'route': 'native-ScrollItemPattern'}
    if action == 'open_find':
        return {'provider': None, 'route': 'native-open-find-v1'}
    return None


def verify_request(request, root):
    """Recompute the recorded decision before the existing worker may act."""
    try:
        proof = read_record(root / 'auto-route.json')
        if (set(proof) != {'schema', 'policy_binding', 'condition', 'observation', 'selected'}
                or proof['schema'] != 1 or proof['policy_binding'] != PROVIDER_BINDING
                or request.get('auto_route_sha256') != digest(proof)):
            raise ValueError()
        condition, observation = proof['condition'], proof['observation']
        expected = {'op': 'act', 'window': condition['window'], 'control': request['control'],
                    'action': request['action'], 'value': request['value'],
                    'before_sha': digest(observation), 'selector': condition['selector'],
                    'auto_route_sha256': digest(proof)}
        selected = route(observation, request['control'], request['action'], request['value'], condition['selector'])
        if selected is None or selected != proof['selected']:
            raise ValueError()
        if selected['provider'] is not None:
            expected['provider'] = selected['provider']
        from . import windows_file_actions as files, windows_document_lifecycle as lifecycle, windows_content_find as content
        from . import windows_winapp_office as office
        if any(module.is_goal(condition['selector']) for module in (files, lifecycle, content, office)):
            expected['document_token'] = condition['value']
        actual = {k: v for k, v in request.items() if k != 'input_epoch'}
        candidate = {'control_id': request['control']['id'] if request['control'] else None,
                     'action': request['action'], 'value': request['value']}
        if actual != expected or options(observation, condition) != [candidate]:
            raise ValueError()
        if request['control'] is not None and observation['controls'].count(request['control']) != 1:
            raise ValueError()
        return deepcopy(proof)
    except Exception:
        raise ComputerError('windows_auto_route_invalid') from None


def verify_result(operation, condition, candidate, before_sha, result):
    """Successful auto receipts must include the decision and real worker output."""
    try:
        return _verify_result(operation, condition, candidate, before_sha, result)
    except Exception:
        raise ComputerError('windows_auto_receipt_invalid') from None


def _verify_result(operation, condition, candidate, before_sha, result):
    request = read_record(operation / 'native-request.json')
    proof = verify_request(request, operation)
    if (proof['condition'] != condition or request['before_sha'] != before_sha
            or request['action'] != candidate['action'] or request['value'] != candidate['value']
            or (request['control']['id'] if request['control'] else None) != candidate['control_id']):
        raise ComputerError('windows_auto_route_invalid')
    if result['ok']:
        native = read_record(operation / 'native-outcome.json')
        if {k: v for k, v in native.items() if k != 'input_epoch'} != result:
            raise ComputerError('windows_auto_receipt_invalid')
        if read_record(operation / 'native-intent.json').get('request_sha') != digest(request):
            raise ComputerError('windows_auto_receipt_invalid')
