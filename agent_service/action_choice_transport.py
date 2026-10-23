"""Short wire aliases for one bounded agent action decision.

The durable action choices stay canonical. Only a deep-copied model context
uses short aliases, which are mapped back before the caller records a choice.
"""
from copy import deepcopy
import re

from .computer import ComputerError, digest


_ID = re.compile(r'[0-9a-f]{24}\Z')


def call_model(context, callback, allow_none=False):
    if (type(context) is not dict or type(context.get('choices')) is not list
            or not 1 <= len(context['choices']) <= 8 or type(allow_none) is not bool
            or not callable(callback)):
        raise ComputerError('agent_decision_invalid')
    canonical = []
    for choice in context['choices']:
        if type(choice) is not dict or type(choice.get('id')) is not str or not _ID.fullmatch(choice['id']):
            raise ComputerError('agent_decision_invalid')
        canonical.append(choice['id'])
    if len(set(canonical)) != len(canonical):
        raise ComputerError('agent_decision_invalid')

    wire = deepcopy(context)
    aliases = {}
    suffix = digest(canonical)[:8]
    for index, choice in enumerate(wire['choices']):
        alias = f'a{index}_{suffix}'
        choice['id'] = alias
        aliases[alias] = canonical[index]
    response = callback(wire)
    if type(response) is not dict or set(response) != {'action_id'}:
        raise ComputerError('agent_decision_invalid')
    selected = response['action_id']
    if selected is None and allow_none:
        return {'action_id': None}
    if type(selected) is not str or selected not in aliases:
        raise ComputerError('agent_decision_invalid')
    return {'action_id': aliases[selected]}
