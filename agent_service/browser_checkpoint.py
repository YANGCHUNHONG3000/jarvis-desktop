"""Local, historical page captures; never browser sessions or credentials."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import math
import time
from urllib.parse import urlsplit

from .computer import ComputerError, digest, read_record
from .computer_agent_tools import _load, _save


def _rejected_public_initial(config, browser):
    """A denied bootstrap read proves no completion and authorizes no action."""
    from .browser_resume import validate
    if type(browser) is not dict:
        return False
    discovery = browser.get('discovery', {})
    if (config.get('mode') != 'public_search' or type(discovery) is not dict
            or browser.get('failures') != [{'code':'browser_public_method_blocked'}]
            or browser.get('checks') != {'goal_met':False}
            or browser.get('text') != '' or browser.get('actions') != []
            or browser.get('controls') != [] or browser.get('completed_rules') != []
            or browser.get('url') != config['start_url']
            or discovery.get('mode') != 'public_search' or discovery.get('site') != config['site']
            or discovery.get('phase') != 'halted'
            or any(discovery.get(key) is not False for key in (
                'query_verified','content_verified','main_present','ambiguous_controls','controls_overflow'))
            or discovery.get('content_proof') is not None
            or discovery.get('last_observation_error') is not None
            or discovery.get('observation_retries') != 0):
        return False
    try:
        cursor = validate(config, discovery.get('resume'))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    if (discovery.get('target_url') != (cursor['target_url']
            if cursor['phase'] == 'target_opened' else config.get('target_url', ''))
            or discovery.get('search_proof') != ('historical'
                if cursor['phase'] == 'target_opened' else None)
            or cursor['phase'] == 'target_opened' and discovery.get('search_status') is not None):
        return False
    return True


def verify_browser(config, browser, *, complete=True, rejected_initial=False):
    """Check adapter evidence independently of its goal_met boolean."""
    from .browser_interaction import _origin
    if rejected_initial and (complete or not _rejected_public_initial(config, browser)):
        raise ComputerError('recovery_browser_snapshot_invalid')
    if (type(browser) is not dict or browser.get('locked') is not rejected_initial
            or browser.get('human_required', False) is not False
            or type(browser.get('session_id')) is not str or not browser['session_id']
            or browser.get('state_id') != digest({k:v for k,v in browser.items()
                                                   if k not in {'state_id','actions'}})
            or type(browser.get('actions')) is not list
            or _origin(browser['url']) != _origin(config['start_url'])):
        raise ComputerError('recovery_browser_snapshot_invalid')
    if (config.get('site') == 'ikea_us'
            and browser.get('discovery', {}).get('query_verified') is True):
        try:
            commerce = browser['discovery']
            if commerce.get('search_proof') == 'historical':
                from .browser_resume import validate
                cursor = validate(config, commerce.get('resume'))
                if (cursor['phase'] != 'target_opened'
                        or commerce.get('target_url') != cursor['target_url']
                        or commerce.get('search_status') is not None
                        or commerce.get('commerce_search') is not None):
                    raise ValueError()
            else:
                from .browser_commerce import verify_commerce_search
                verify_commerce_search(config, browser)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ComputerError('recovery_browser_commerce_not_verified') from None
    if not complete:
        return browser
    text = browser.get('text')
    if (browser.get('checks') != {'goal_met':True}
            or type(text) is not str or not text.strip() or len(text) > 24000
            or any(term not in text for term in config['required_text'])):
        raise ComputerError('recovery_browser_read_not_completed')
    discovery = browser.get('discovery', {})
    if config.get('mode') == 'github_repo':
        historical_search = discovery.get('search_proof') == 'historical'
        if historical_search:
            from .browser_resume import validate
            cursor = validate(config, discovery.get('resume'))
            if (cursor['phase'] != 'repository_opened'
                    or discovery.get('repository_search_status') is not None):
                raise ComputerError('recovery_browser_target_not_verified')
        if (browser['url'] != config['start_url'] or browser['actions'] != []
                or discovery.get('mode') != 'github_repo'
                or any(discovery.get(k) is not True for k in (
                    'query_verified','repository_verified','repository_identity_matches','main_present'))
                or type(discovery.get('authenticated')) is not bool
                or (config['human_login'] and not discovery['authenticated'])
                or type(discovery.get('repository_status')) is not int
                or discovery['repository_status'] != 200
                or (not historical_search and (type(discovery.get('repository_search_status')) is not int
                       or discovery['repository_search_status'] != 200))
                or discovery.get('ambiguous_controls') is not False
                or discovery.get('controls_overflow') is not False):
            raise ComputerError('recovery_browser_target_not_verified')
    elif config.get('mode') == 'public_search':
        from .browser_resume import public_target, validate
        target = public_target(config, discovery.get('target_url'))
        historical_search = discovery.get('search_proof') == 'historical'
        legacy = bool(config.get('target_url') and 'search_proof' not in discovery
                      and 'resume' not in discovery)
        if historical_search:
            cursor = validate(config, discovery.get('resume'))
            if (cursor['phase'] != 'target_opened' or cursor['target_url'] != target
                    or discovery.get('search_status') is not None):
                raise ComputerError('recovery_browser_target_not_verified')
        if (browser['url'] != target or browser['actions'] != []
                or discovery.get('mode') != 'public_search'
                or discovery.get('phase') != 'verified'
                or any(discovery.get(k) is not True for k in (
                    'query_verified','content_verified','main_present','search_submitted',
                    'target_clicked','content_finished'))
                or type(discovery.get('content_status')) is not int
                or discovery['content_status'] != 200
                or (not historical_search and (type(discovery.get('search_status')) is not int
                    or discovery['search_status'] != 200 or discovery.get('search_finished') is not True))
                or (not legacy and (discovery.get('content_proof') != 'fresh'
                    or discovery.get('search_proof') not in ('fresh','historical')))
                or discovery.get('ambiguous_controls') is not False
                or discovery.get('controls_overflow') is not False):
            raise ComputerError('recovery_browser_target_not_verified')
    elif config.get('mode') == 'discover_query':
        if (discovery.get('mode') != 'discover_query'
                or discovery.get('query_verified') is not True
                or (config.get('javascript') and discovery.get('javascript') is not True)):
            raise ComputerError('recovery_browser_query_not_verified')
        if config.get('javascript'):
            attempts = discovery.get('attempts', [])
            if (discovery.get('phase') != 'verified' or not attempts
                    or attempts[-1].get('state') != 'verified' or not attempts[-1].get('requests')
                    or any(r.get('method') != 'GET' or r.get('finished') is not True
                           or r.get('failed') is not False or type(r.get('status')) is not int
                           or not 200 <= r['status'] < 300
                           or _origin(r['url']) != _origin(config['start_url'])
                           for r in attempts[-1]['requests'])):
                raise ComputerError('recovery_browser_query_not_verified')
    elif (urlsplit(browser['url']).path or '/') not in config['allowed_paths']:
        raise ComputerError('recovery_browser_target_not_verified')
    return browser


def capture(run, config, browser):
    """Record the first independently observed completed page, once per run."""
    verify_browser(config, browser)
    path = run/'browser-checkpoint.json'
    if not path.exists():
        _save(path, {'version':1, 'run':str(run), 'config_sha256':digest(config),
                    'captured_at':time.time(), 'browser':deepcopy(browser),
                    'content_sha256':sha256(browser['text'].encode('utf-8')).hexdigest()})


def load(run, config):
    if not (run/'browser-checkpoint.json').is_file():
        raise ComputerError('recovery_browser_read_not_completed') from None
    checkpoint, _ = _load(run/'browser-checkpoint.json')
    if (type(checkpoint) is not dict or set(checkpoint) != {
            'version','run','config_sha256','captured_at','browser','content_sha256'}
            or checkpoint['version'] != 1 or checkpoint['run'] != str(run)
            or checkpoint['config_sha256'] != digest(config)
            or type(checkpoint['captured_at']) not in (int,float)
            or not math.isfinite(checkpoint['captured_at'])):
        raise ComputerError('recovery_browser_checkpoint_changed')
    browser = verify_browser(config, checkpoint['browser'])
    if checkpoint['content_sha256'] != sha256(browser['text'].encode('utf-8')).hexdigest():
        raise ComputerError('recovery_browser_content_changed')
    # The capture must also occur in the engine's independent observation log.
    if not any(read_record(path).get('browser') == browser
               for path in run.glob('observation-*.json')):
        raise ComputerError('recovery_browser_observation_missing')
    return checkpoint


def verify_action(run, step, config, action, result, *, include_before=False):
    """A captured page never erases an earlier unacknowledged browser effect."""
    from .computer_agent import choices_for
    before = read_record(run/('observation-'+step.name[-2:]+'.json'))
    browser = verify_browser(config, before['browser'], complete=False)
    selected = read_record(step/'intent.json')
    choices = read_record(step/'choices.json')
    decision = read_record(step/'decision.json')
    state, expected = choices_for(run, before, [v['action'] for v in choices['choices']])
    if (action not in browser['actions'] or action.get('state_id') != browser['state_id']
            or choices != {'state_sha256':state,'choices':expected}
            or selected not in expected or selected.get('action') != action
            or decision.get('choice') != selected or decision.get('state_sha256') != state
            or decision.get('method') != ('single_legal_action' if len(expected) == 1 else 'model_choice')):
        raise ComputerError('recovery_browser_action_changed')
    after = verify_browser(config, result['observation'], complete=False)
    if after['session_id'] != browser['session_id']:
        raise ComputerError('recovery_browser_session_changed')
    proof = result.get('verification', {})
    good = (result.get('ok') is True and result.get('state') == 'verified'
            and proof.get('independent_read') is True)
    harmless = (result.get('ok') is False and result.get('state') == 'failed'
                and proof.get('effect_dispatched') is False)
    # JS retries are allowed only with the adapter's recorded GET failure.
    if (not good and result.get('code') == 'browser_query_readonly_failed'
            and result.get('ok') is False and result.get('state') == 'failed'
            and proof.get('independent_read') is True and config.get('javascript')):
        attempts = after.get('discovery', {}).get('attempts', [])
        harmless = bool(attempts and attempts[-1].get('state') == 'readonly_failed'
            and attempts[-1].get('requests') and all(r.get('method') == 'GET'
            and (r.get('failed') is True or type(r.get('status')) is int)
            for r in attempts[-1]['requests']) and any(r.get('failed') is True
            or type(r.get('status')) is int and not 200 <= r['status'] < 300
            for r in attempts[-1]['requests']))
    if not good and not harmless:
        raise ComputerError('recovery_browser_effect_not_verified')
    if good and config.get('mode') == 'public_search' and result.get('code') == 'browser_public_target_opened':
        from .browser_resume import public_target, _public_search
        discovery = after.get('discovery', {})
        target = public_target(config, discovery.get('target_url'))
        legacy = bool(config.get('target_url') and 'search_proof' not in discovery
                      and 'resume' not in discovery)
        if (action.get('kind') != 'browser_click'
                or action.get('target_url', target if legacy else None) != target
                or (after['url'] != target and not _public_search(config, after['url']))
                or discovery.get('target_clicked') is not True):
            raise ComputerError('recovery_browser_action_changed')
    return deepcopy(browser) if include_before else after['session_id']


def frozen(checkpoint):
    browser = deepcopy(checkpoint['browser'])
    browser['actions'] = []
    browser['historical_capture'] = {k:checkpoint[k] for k in (
        'run','captured_at','content_sha256','config_sha256')}
    browser['state_id'] = digest({k:v for k,v in browser.items() if k not in {'actions','state_id'}})
    return browser


def verify_history(config, checkpoint, attempts, *, resumed=False):
    discovery = checkpoint['browser'].get('discovery', {})
    public_capture = (config.get('mode') == 'public_search'
        and not (config.get('target_url') and 'search_proof' not in discovery
                 and 'resume' not in discovery))
    if public_capture:
        # Goal completion may be first observed in the next ordinary loop
        # observation, with no wait action. Require its real immutable capture
        # and independent observation record before using this additional proof.
        if load(Path(checkpoint['run']), config) != checkpoint:
            raise ComputerError('recovery_browser_checkpoint_changed')
    reads = [a for a in attempts if a['action']['kind'].startswith('browser_')]
    completed = [a['result']['observation'] for a in reads if a['result'].get('ok') is True
                 and a['result']['observation'].get('checks', {}).get('goal_met') is True]
    if public_capture and not resumed:
        successful = [a['result']['observation'] for a in reads if a['result'].get('ok') is True]
        if (discovery.get('resume') is not None or discovery.get('search_proof') != 'fresh'
                or not successful
                or successful[-1]['session_id'] != checkpoint['browser']['session_id']):
            raise ComputerError('recovery_browser_independent_read_missing')
    if not resumed and not public_capture and not any(b['session_id'] == checkpoint['browser']['session_id']
               and b['text'] == checkpoint['browser']['text']
               and b['url'] == checkpoint['browser']['url'] for b in completed):
        raise ComputerError('recovery_browser_independent_read_missing')
    if config.get('mode') == 'github_repo':
        if resumed:
            from .browser_resume import derive
            if derive(config, attempts)['phase'] != 'repository_opened':
                raise ComputerError('recovery_browser_repository_read_missing')
        else:
            codes = [a['result'].get('code') for a in reads if a['result'].get('ok') is True]
            opened = codes.index('browser_github_repository_opened') if 'browser_github_repository_opened' in codes else -1
            if (codes.count('browser_github_repository_opened') != 1
                    or 'browser_github_search_submitted' not in codes[:opened]
                    or any(code != 'browser_wait_observed' for code in codes[opened+1:])):
                raise ComputerError('recovery_browser_repository_read_missing')
    elif config.get('mode') == 'public_search':
        from .browser_resume import derive
        discovery = checkpoint['browser']['discovery']
        legacy = bool(config.get('target_url') and 'search_proof' not in discovery
                      and 'resume' not in discovery)
        if not legacy:
            cursor = derive(config, attempts, completed_browser=checkpoint['browser'])
            if (cursor['phase'] != 'target_opened'
                    or cursor['target_url'] != checkpoint['browser']['url']):
                raise ComputerError('recovery_browser_public_read_missing')
            return
        codes = [a['result'].get('code') for a in reads if a['result'].get('ok') is True]
        opened = codes.index('browser_public_target_opened') if 'browser_public_target_opened' in codes else -1
        if (resumed or codes.count('browser_public_search_submitted') != 1
                or codes.count('browser_public_target_opened') != 1
                or 'browser_public_search_submitted' not in codes[:opened]
                or any(code != 'browser_wait_observed' for code in codes[opened+1:])):
            raise ComputerError('recovery_browser_public_read_missing')
