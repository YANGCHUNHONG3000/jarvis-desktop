"""Small contracted GET cursors derived only from verified action history."""
from copy import deepcopy
from urllib.parse import urlencode, parse_qsl, urlsplit

from .computer import ComputerError, digest


def validate(config, cursor):
    if config.get('mode') == 'public_search':
        return _validate_public(config, cursor)
    if (config.get('mode') != 'github_repo' or type(cursor) is not dict
            or set(cursor) != {'version','mode','phase','url','config_sha256'}
            or type(cursor['version']) is not int or cursor['version'] != 1
            or cursor['mode'] != 'github_repo'
            or cursor['phase'] not in {'search_results','repository_opened'}
            or cursor['config_sha256'] != digest(config)):
        raise ComputerError('recovery_browser_resume_invalid')
    expected = (config['start_url'] if cursor['phase'] == 'repository_opened' else
                'https://github.com/search?' + urlencode({'q':config['query'],'type':'repositories'}))
    if cursor['url'] != expected:
        raise ComputerError('recovery_browser_resume_invalid')
    return deepcopy(cursor)


validate_cursor = validate


def public_search_url(config):
    from .public_site_profiles import public_search_url as profile_search_url
    return profile_search_url(config)


def public_target(config, url):
    """Recheck profile authority and an optional explicit target independently."""
    from .browser_public import validate_public_target
    try:
        target = validate_public_target(config, url)
        if target != url or config.get('target_url') and target != config['target_url']:
            raise ValueError()
        return target
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ComputerError('recovery_browser_target_not_verified') from None


def _validate_public(config, cursor):
    from .browser_public import validate_public_config
    try:
        approved = validate_public_config(config)
        if (type(cursor) is not dict or set(cursor) != {
                'version','mode','phase','url','config_sha256','target_url'}
                or type(cursor['version']) is not int or cursor['version'] != 1
                or cursor['mode'] != 'public_search'
                or cursor['phase'] not in ('search_results','target_opened')
                or cursor['config_sha256'] != digest(approved)):
            raise ValueError()
        if cursor['phase'] == 'search_results':
            if cursor['target_url'] != '' or cursor['url'] != public_search_url(approved):
                raise ValueError()
        elif cursor['url'] != public_target(approved, cursor['target_url']):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ComputerError('recovery_browser_resume_invalid') from None
    return deepcopy(cursor)


def _public_search(config, url):
    try:
        actual, expected = urlsplit(url), urlsplit(public_search_url(config))
        pairs = parse_qsl(actual.query, keep_blank_values=True)
        expected_pairs = parse_qsl(expected.query, keep_blank_values=True)
        return (actual.scheme == expected.scheme and actual.netloc == expected.netloc
                and actual.path == expected.path and not actual.fragment
                and len(pairs) == len(expected_pairs) and dict(pairs) == dict(expected_pairs))
    except (ValueError, TypeError):
        return False


def _derive_public(config, attempts, *, completed_browser=None):
    from .browser_public import validate_public_config
    config = validate_public_config(config)
    filled = submitted = searched = False
    selected, opened, last_browser, submitted_session = '', False, None, None
    for attempt in attempts:
        action, result = attempt['action'], attempt['result']
        if not action['kind'].startswith('browser_') or result.get('ok') is not True:
            continue
        if (result.get('state') != 'verified'
                or result.get('verification', {}).get('independent_read') is not True):
            raise ComputerError('recovery_browser_effect_not_verified')
        browser, code = result['observation'], result.get('code')
        last_browser = browser
        discovery = browser.get('discovery', {})
        if config['site'] == 'ikea_us' and discovery.get('query_verified') is True:
            # A rendered search shell cannot establish a commerce result phase.
            if discovery.get('search_proof') == 'historical' and (not searched or not selected):
                raise ComputerError('recovery_browser_commerce_not_verified')
            from .browser_checkpoint import verify_browser
            verify_browser(config, browser, complete=False)
        before = attempt.get('browser_before')
        if (submitted and not searched and not selected and before is not None
                and code == 'browser_public_target_opened'):
            # This pre-click observation is supplied only after verify_action
            # rechecks its persisted candidate, choice, intent and result chain.
            from .browser_checkpoint import verify_browser
            verify_browser(config, before, complete=False)
            prior = before.get('discovery', {})
            if (before['session_id'] != submitted_session
                    or before['session_id'] != browser['session_id']
                    or action not in before['actions'] or action.get('state_id') != before['state_id']
                    or prior.get('mode') != 'public_search' or prior.get('site') != config['site']
                    or prior.get('phase') != 'search_results' or prior.get('search_proof') != 'fresh'
                    or any(prior.get(k) is not True for k in (
                        'query_verified','search_submitted','search_finished'))
                    or prior.get('target_clicked') is not False
                    or type(prior.get('search_status')) is not int or prior['search_status'] != 200
                    or prior.get('ambiguous_controls') is not False
                    or prior.get('controls_overflow') is not False
                    or not _public_search(config, before['url'])):
                raise ComputerError('recovery_browser_public_read_missing')
            searched = True
        if (discovery.get('mode') != 'public_search'
                or selected and code != 'browser_wait_observed'
                or searched and not selected and code not in {
                    'browser_wait_observed','browser_public_target_opened'}):
            raise ComputerError('recovery_browser_public_read_missing')
        if code == 'browser_fill_verified':
            if filled or submitted or action['kind'] != 'browser_fill':
                raise ComputerError('recovery_browser_public_read_missing')
            filled = True
        elif code == 'browser_public_search_submitted':
            if submitted or action['kind'] != 'browser_click':
                raise ComputerError('recovery_browser_public_read_missing')
            submitted = True
            submitted_session = browser['session_id']
        elif code == 'browser_public_target_opened':
            target = public_target(config, discovery.get('target_url'))
            # The old explicit-target contract did not persist URL metadata in
            # its candidate. Only that historical schema may omit this field.
            legacy = bool(config['target_url'] and 'search_proof' not in discovery
                          and 'resume' not in discovery)
            if (not searched or action['kind'] != 'browser_click'
                    or action.get('target_url', target if legacy else None) != target
                    or discovery.get('target_clicked') is not True
                    or discovery.get('query_verified') is not True):
                raise ComputerError('recovery_browser_public_read_missing')
            selected = target
        elif code != 'browser_wait_observed':
            raise ComputerError('recovery_browser_public_read_missing')
        if selected:
            if (discovery.get('target_url') != selected
                    or discovery.get('target_clicked') is not True
                    or discovery.get('query_verified') is not True
                    or browser['url'] != selected and (opened or not _public_search(config, browser['url']))):
                raise ComputerError('recovery_browser_public_read_missing')
            opened = opened or browser['url'] == selected
        if (not selected and submitted and discovery.get('query_verified') is True
                and discovery.get('search_submitted') is True
                and discovery.get('search_finished') is True
                and type(discovery.get('search_status')) is int
                and discovery['search_status'] == 200
                and discovery.get('ambiguous_controls') is False
                and discovery.get('controls_overflow') is False
                and _public_search(config, browser['url'])):
            searched = True
    if completed_browser is not None:
        # Only a loaded independent capture may close a navigation race. It
        # does not create an action acknowledgement or a selected capability.
        from .browser_checkpoint import verify_browser
        verify_browser(config, completed_browser)
        if (not selected or completed_browser['url'] != selected
                or (not opened and (last_browser is None
                    or completed_browser['session_id'] != last_browser['session_id']))):
            raise ComputerError('recovery_browser_public_read_missing')
        opened = True
    if not searched or selected and not opened:
        raise ComputerError('recovery_browser_read_not_completed')
    return validate(config, {'version':1,'mode':'public_search',
        'phase':'target_opened' if selected else 'search_results',
        'url':selected or public_search_url(config), 'target_url':selected,
        'config_sha256':digest(config)})


def _search(config, url):
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return (parsed.scheme == 'https' and parsed.netloc == 'github.com'
            and parsed.path == '/search' and not parsed.fragment
            and len(pairs) == 2 and dict(pairs) == {'q':config['query'],'type':'repositories'})


def derive(config, attempts, *, completed_browser=None):
    """Call only after each action's independent receipt has been revalidated."""
    if config.get('mode') == 'public_search':
        return _derive_public(config, attempts, completed_browser=completed_browser)
    if config.get('mode') != 'github_repo':
        raise ComputerError('recovery_browser_read_not_completed')
    submitted, searched, opened = False, False, False
    phase = None
    for attempt in attempts:
        action, result = attempt['action'], attempt['result']
        if not action['kind'].startswith('browser_'):
            continue
        if result.get('ok') is not True:
            # verify_action independently establishes that no effect was sent.
            continue
        browser = result['observation']
        discovery = browser.get('discovery', {})
        code = result.get('code')
        if result.get('verification', {}).get('independent_read') is not True:
            raise ComputerError('recovery_browser_effect_not_verified')
        if opened and code != 'browser_wait_observed':
            raise ComputerError('recovery_browser_repository_read_missing')
        if searched and not opened and code not in {
                'browser_wait_observed','browser_github_repository_opened'}:
            raise ComputerError('recovery_browser_repository_read_missing')
        if code == 'browser_github_search_submitted':
            if submitted or action['kind'] != 'browser_click':
                raise ComputerError('recovery_browser_repository_read_missing')
            submitted = True
        elif code == 'browser_github_repository_opened':
            if (not searched or action['kind'] != 'browser_click'
                    or browser['url'] != config['start_url']
                    or discovery.get('query_verified') is not True):
                raise ComputerError('recovery_browser_repository_read_missing')
            opened, phase = True, 'repository_opened'
        elif code not in {'browser_fill_verified','browser_github_search_opened',
                'browser_github_search_opener_dispatched',
                'browser_github_repository_filter_selected','browser_wait_observed'}:
            raise ComputerError('recovery_browser_repository_read_missing')
        if (not opened and submitted and discovery.get('query_verified') is True
                and type(discovery.get('repository_search_status')) is int
                and discovery['repository_search_status'] == 200
                and discovery.get('ambiguous_controls') is False
                and discovery.get('controls_overflow') is False
                and _search(config, browser['url'])):
            searched, phase = True, 'search_results'
    if phase is None:
        raise ComputerError('recovery_browser_read_not_completed')
    return validate(config, {'version':1,'mode':'github_repo','phase':phase,
        'url':config['start_url'] if opened else 'https://github.com/search?' +
            urlencode({'q':config['query'],'type':'repositories'}),
        'config_sha256':digest(config)})
