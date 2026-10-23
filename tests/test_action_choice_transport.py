"""Offline bounded transport for agent action choice IDs."""
from copy import deepcopy

import pytest

from agent_service.action_choice_transport import call_model
from agent_service.computer import ComputerError, digest


FIRST, SECOND = '1' * 24, '2' * 24
SUFFIX = digest([FIRST, SECOND])[:8]
ALIAS0, ALIAS1 = f'a0_{SUFFIX}', f'a1_{SUFFIX}'


def context():
    return {'instruction': 'public research only', 'goal': {'queries': ['topic']},
            'observation': {'source_reads': 0},
            'choices': [
                {'id': FIRST, 'action': {'kind': 'search', 'query_index': 0}},
                {'id': SECOND, 'action': {'kind': 'read', 'candidate_id': '3' * 24},
                 'url': 'https://example.org/', 'title': 'Example'},
            ]}


def test_maps_one_short_alias_to_canonical_without_touching_context():
    original = context()
    preserved = deepcopy(original)
    calls = []
    def callback(wire):
        calls.append(deepcopy(wire))
        assert [choice['id'] for choice in wire['choices']] == [ALIAS0, ALIAS1]
        assert wire['choices'][1]['action'] == original['choices'][1]['action']
        assert wire['choices'][1]['url'] == original['choices'][1]['url']
        wire['choices'][0]['action']['query_index'] = 99
        return {'action_id': ALIAS1}
    assert call_model(original, callback) == {'action_id': SECOND}
    assert len(calls) == 1 and original == preserved


@pytest.mark.parametrize('response', [
    {'action_id': FIRST}, {'action_id': f'a2_{SUFFIX}'}, {'action_id': f'A0_{SUFFIX}'},
    {'action_id': ALIAS0, 'canonical_id': FIRST}, {'action_id': 0},
    {'id': ALIAS0}, None, [], {'action_id': None},
])
def test_rejects_unknown_full_mixed_or_malformed_response_once(response):
    calls = []
    def callback(wire):
        calls.append(wire)
        return response
    with pytest.raises(ComputerError, match='agent_decision_invalid'):
        call_model(context(), callback)
    assert len(calls) == 1


def test_none_is_allowed_only_for_explicit_public_selection():
    assert call_model(context(), lambda _: {'action_id': None}, allow_none=True) == {'action_id': None}
    assert call_model(context(), lambda _: {'action_id': ALIAS0}, allow_none=True) == {'action_id': FIRST}


def test_alias_is_deterministic_for_current_choices_and_rejects_prior_choice_set():
    old = context()
    seen = []
    for _ in range(2):
        assert call_model(old, lambda wire: (seen.append(wire['choices'][0]['id']) or
                                             {'action_id': wire['choices'][0]['id']})) == {'action_id': FIRST}
    assert seen == [ALIAS0, ALIAS0]
    changed = context()
    changed['choices'][0]['id'] = '4' * 24
    with pytest.raises(ComputerError, match='agent_decision_invalid'):
        call_model(changed, lambda _: {'action_id': ALIAS0})


@pytest.mark.parametrize('mutation', [
    lambda c: c.update(choices=[]),
    lambda c: c['choices'][1].update(id=FIRST),
    lambda c: c['choices'][0].update(id=ALIAS0),
    lambda c: c['choices'][0].update(id=None),
    lambda c: c.update(choices=[*c['choices']] * 5),
    lambda c: c.update(choices='choices'),
])
def test_invalid_context_never_invokes_callback(mutation):
    data = context()
    mutation(data)
    calls = []
    with pytest.raises(ComputerError, match='agent_decision_invalid'):
        call_model(data, lambda wire: calls.append(wire))
    assert calls == []


def test_callback_error_is_not_retried():
    calls = []
    def callback(wire):
        calls.append(wire)
        raise ComputerError('model_budget_exceeded')
    with pytest.raises(ComputerError, match='model_budget_exceeded'):
        call_model(context(), callback)
    assert len(calls) == 1
