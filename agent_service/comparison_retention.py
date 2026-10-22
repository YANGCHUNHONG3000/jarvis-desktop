"""Retain prior reviewed literal spans without inheriting their review verdict."""
from copy import deepcopy

from .computer import ComputerError
from .source_comparison import build_context, verify_evidence, _response


def retain_selection(goal, current_accepted, raw_response, prior_goal, prior_accepted, prior_evidence):
    """Return merged raw selection and an explicit deterministic retention audit.

    The caller stores the untouched new model response separately, then performs
    a fresh independent review of the returned response. Unsupported new spans
    remain raw audit data unless replaced by an eligible prior literal span.
    """
    if (type(goal) is not dict or type(prior_goal) is not dict or goal != prior_goal
            or type(goal.get('comparison')) is not dict
            or goal['comparison'].get('evidence_version') not in {3,4}):
        raise ComputerError('comparison_retention_invalid')
    current = build_context(goal, current_accepted)
    prior = build_context(prior_goal, prior_accepted)
    checked = verify_evidence(prior_goal, prior_accepted, prior_evidence)
    # Validate current shape and all span types even where v3 canonicalization
    # will later mark absent/ambiguous literal spans as missing.
    _response(current, raw_response)
    merged = deepcopy(raw_response)
    new_rows = {row['id']: row for row in merged['sources']}
    old_sources = {source['id']: source for source in prior['sources']}
    old_rows = {row['id']: row for row in checked['response']['sources']}
    retained = []
    for source in current['sources']:
        sid = source['id']
        old = old_sources.get(sid)
        if old is None or any(source[key] != old[key] for key in ('url', 'sha256', 'text')):
            continue
        for index, (span, cell) in enumerate(zip(old_rows[sid]['spans'], checked['assessment']['cells'][sid])):
            if span is not None and cell['status'] == 'relevant':
                new_rows[sid]['spans'][index] = deepcopy(span)
                retained.append({'id': sid, 'dimension_index': index, 'quote': span['quote']})
    # Confirm retained spans remain literal and unique in the current context.
    _response(current, merged)
    return {'response': merged, 'retained': retained}
