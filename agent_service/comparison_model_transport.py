"""Ephemeral source aliases for one bounded v4 model call.

Canonical contexts, selections and receipts never contain these wire aliases.
No text is shortened or recombined; the existing client limits still apply.
"""
from copy import deepcopy
import json

from .computer import ComputerError


MAX_INPUT_CHARS = 48000
MAX_OUTPUT_TOKENS = 2048


def _fail():
    raise ComputerError('comparison_transport_invalid')


def _rows(value, ids, fields):
    if type(value) is not list or len(value) != len(ids):
        _fail()
    seen = set()
    for row in value:
        if (type(row) is not dict or set(row) != fields
                or type(row.get('id')) is not str or row['id'] not in ids
                or row['id'] in seen):
            _fail()
        seen.add(row['id'])
    return value


def _pairs(value, ids):
    if type(value) is not list or len(value) != len(ids) * (len(ids) - 1) // 2:
        _fail()
    seen = set()
    for pair in value:
        if (type(pair) is not dict or set(pair) != {'left', 'right', 'relation', 'reason'}
                or type(pair.get('left')) is not str or type(pair.get('right')) is not str
                or pair['left'] not in ids or pair['right'] not in ids
                or pair['left'] == pair['right']):
            _fail()
        key = tuple(sorted((pair['left'], pair['right'])))
        if key in seen:
            _fail()
        seen.add(key)
    return value


def call_model(context, callback):
    """Call ``callback(aliased_context)`` once and restore strict canonical IDs.

    The callback owns the original session/ledger/client and its token ceiling.
    Selection and review keep their existing JSON grammars. Unknown IDs, duplicate
    IDs, missing pairs and mixed canonical/alias responses are terminal. The
    ordinary literal/schema validators still validate the restored response.
    """
    if (type(context) is not dict or not callable(callback)
            or type(context.get('goal')) is not dict
            or type(context['goal'].get('comparison')) is not dict
            or context['goal']['comparison'].get('evidence_version') != 4
            or type(context.get('sources')) is not list
            or not 1 <= len(context['sources']) <= 6):
        _fail()
    ids = [s.get('id') if type(s) is dict else None for s in context['sources']]
    if (any(type(sid) is not str or not sid.strip() or len(sid) > 200 for sid in ids)
            or len(set(ids)) != len(ids)):
        _fail()
    forward = {sid: f's{i}' for i, sid in enumerate(ids)}
    reverse = {alias: sid for sid, alias in forward.items()}
    wire = deepcopy(context)
    # Local run paths and provenance belong to the canonical input binding, not
    # the public evidence uploaded to the model.
    wire['goal']['comparison'].pop('seed', None)
    for source in wire['sources']:
        source['id'] = forward[source['id']]
    review = 'selection' in wire
    if review:
        selection = wire['selection']
        if type(selection) is not dict or set(selection) != {'sources'}:
            _fail()
        for row in _rows(selection['sources'], forward, {'id', 'spans'}):
            row['id'] = forward[row['id']]
    if 'pairs' in wire:
        for pair in _pairs(wire['pairs'], forward):
            pair['left'], pair['right'] = forward[pair['left']], forward[pair['right']]
    try:
        # Match model_json's exact serialization, including separator spacing.
        encoded = json.dumps(wire, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError):
        _fail()
    if not 1 <= len(encoded) <= MAX_INPUT_CHARS:
        raise ComputerError('model_input_invalid')
    response = deepcopy(callback(wire))
    expected = {'sources', 'pairs'} if review else {'sources'}
    if type(response) is not dict or set(response) != expected:
        _fail()
    fields = {'id', 'publisher', 'cells'} if review else {'id', 'spans'}
    for row in _rows(response['sources'], reverse, fields):
        row['id'] = reverse[row['id']]
    if review:
        for pair in _pairs(response['pairs'], reverse):
            pair['left'], pair['right'] = reverse[pair['left']], reverse[pair['right']]
    return response
