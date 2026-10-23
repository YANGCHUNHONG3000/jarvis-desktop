"""New repair prompts distinguish publication copyright from project roles."""
from agent_service.publisher_evidence import POLICY, attribution_status, review_instructions


def test_route6_explains_literal_footer_without_changing_older_prompts():
    old = {'comparison': {'publisher_policy': POLICY, 'dimension_routing': 5}}
    new = {'comparison': {'publisher_policy': POLICY, 'dimension_routing': 6}}
    before = review_instructions(old)
    after = review_instructions(new)
    assert after.startswith(before)
    assert 'A literal documentation footer' not in before
    suffix = after[len(before):]
    assert 'Example Person' in suffix and 'never this example' in suffix
    assert 'original name/quote schema' in suffix
    assert 'Null remains required' in suffix
    assert 'John McNamara' not in after


def test_footer_gate_still_requires_its_literal_named_attribution():
    source = {'url': 'https://example.org/manual', 'title': 'Manual',
              'text': '© Copyright 2020-2026, Example Person.'}
    assert attribution_status(source, {'name': 'Example Person', 'quote': source['text']},
                              policy=POLICY) == 'supported'
    assert attribution_status(source, {'name': 'Someone Else', 'quote': source['text']},
                              policy=POLICY) == 'attribution_missing'
    assert attribution_status(source, {'name': 'Example Person', 'quote': 'Created using Example Person'},
                              policy=POLICY) == 'platform_credit'


def test_compact_default_keeps_latest_answer_and_attribution_per_query():
    from agent_service.comparison_crosssite import _recent_source_ids
    captures = [({'id': sid, 'query_index': query}, {})
                for sid, query in [('old-a', 0), ('b', 1), ('answer', 0), ('author', 0)]]
    assert _recent_source_ids(captures) == ['b', 'answer', 'author']


def test_compact_default_is_bounded_and_keeps_stable_order():
    from agent_service.comparison_crosssite import _recent_source_ids
    captures = [({'id': str(n), 'query_index': n % 2}, {}) for n in range(6)]
    assert _recent_source_ids(captures) == ['2', '3', '4', '5']


def test_cache_only_attribution_kept_in_history_but_not_navigation_inventory():
    from agent_service.comparison_crosssite import CrossSiteEnvironment
    env = object.__new__(CrossSiteEnvironment)
    env.goal = {'comparison': {'dimension_routing': 6}}
    history = {'discoveries': [{'proof': {'version': 11}}, {'proof': {'version': 12}},
                               {'proof': {'version': 13}}]}
    env._repair_input = lambda: ([], history)
    assert env._prior_discoveries() == [history['discoveries'][0], history['discoveries'][2]]
    assert len(history['discoveries']) == 3
