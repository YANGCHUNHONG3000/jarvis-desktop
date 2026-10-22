"""Offline adapter contracts; fake networking is never live-site evidence."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from agent_service.browser_github import GithubEdgeSession
from agent_service.browser_interaction import BrowserInteraction, InteractionError, _digest, validate_config


def config(**extra):
    return validate_config({'mode':'github_repo', 'start_url':'https://github.com/python/peps',
        'query':'peps user:python', 'required_text':['Python proposals'], **extra})


def cursor(cfg, phase='search_results'):
    return {'version':1, 'mode':'github_repo', 'phase':phase,
        'url':cfg['start_url'] if phase == 'repository_opened' else
            'https://github.com/search?' + urlencode({'q':cfg['query'], 'type':'repositories'}),
        'config_sha256':_digest(cfg)}


@pytest.fixture
def offline(monkeypatch):
    settings = SimpleNamespace(status=200, finish=True, matches=True, text='Python proposals', calls=[])

    class Page:
        def __init__(self):
            self.handlers = {}
            self.main_frame = object()
            self._url = 'about:blank'

        @property
        def url(self):
            assert not self.owner.manual, 'URL observed during human ownership'
            return self._url

        def on(self, event, callback):
            self.handlers[event] = callback

        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        def goto(self, url, **kwargs):
            settings.calls.append(url)
            self._url = url
            self.handlers['response'](SimpleNamespace(url=url, status=settings.status))
            if settings.finish:
                self.handlers['requestfinished'](SimpleNamespace(url=url))
            return SimpleNamespace(status=settings.status)

        def wait_for_timeout(self, value):
            pass

        def close(self):
            pass

    class Context:
        def __init__(self):
            self.pages = []

        def route(self, *args):
            pass

        route_web_socket = route
        on = route

        def new_page(self):
            page = Page()
            self.pages.append(page)
            return page

    context = Context()
    browser = SimpleNamespace(new_context=lambda **kwargs:context, close=lambda:None)
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs:browser), stop=lambda:None)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', lambda:SimpleNamespace(start=lambda:runtime))
    monkeypatch.setattr('agent_service.browser_github.socket.getaddrinfo',
        lambda *args, **kwargs:[(2, 1, 6, '', ('140.82.112.3', 443))])
    monkeypatch.setattr('agent_service.research._public_search_proxy', lambda:None)

    class Session(GithubEdgeSession):
        def _bind_page(self, page):
            page.owner = self
            super()._bind_page(page)

        def _challenge(self):
            assert not self.manual
            return False

        def _authenticated(self):
            assert not self.manual
            return True

        def _dom(self):
            assert not self.manual
            if self.registry is None:
                self.registry = object()
                self.document_id = 'fresh-' + str(len(settings.calls))
            return {'controls':[{'kind':kind, 'identity':n, 'selector':kind, 'value_matches':False}
                    for n, kind in enumerate(['repo', 'opener', 'input'])],
                'controls_overflow':False, 'repo_matches':settings.matches,
                'text':settings.text, 'candidate_targets':[], 'search_semantics':[], 'main_present':True}

    settings.session = Session
    return settings


def test_search_resume_fetches_canonical_results_and_offers_only_fresh_repo_action(offline):
    cfg = config()
    saved = cursor(cfg)
    session = offline.session(cfg, resume=saved)
    saved['url'] = 'https://evil.example/'
    snap = session.observe()
    assert offline.calls == [cursor(cfg)['url']]
    assert snap['discovery']['query_verified'] is True
    assert snap['discovery']['search_proof'] == 'fresh'
    assert snap['discovery']['repository_search_status'] == 200
    assert len(snap['actions']) == 1
    assert snap['actions'][0]['rule_id'].startswith('github-repo-fresh-')
    assert not snap['checks']['goal_met']
    assert session.completed == []


@pytest.mark.parametrize('status,finished', [(503, True), (200, False), (None, True)])
def test_search_resume_requires_current_success_and_finished_response(offline, status, finished):
    offline.status, offline.finish = status, finished
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg))
    snap = session.observe()
    assert not snap['discovery']['query_verified']
    assert all(action['kind'] == 'browser_wait' for action in snap['actions'])
    assert snap['discovery']['search_proof'] is None


def test_search_resume_rejects_current_url_with_changed_query(offline):
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg))
    session.page._url = 'https://github.com/search?q=changed&type=repositories'
    snap = session.observe()
    assert snap['locked']
    assert not snap['discovery']['query_verified']
    assert snap['actions'] == []


def test_repository_resume_requires_fresh_native_read_and_labels_historical_search(offline):
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg, 'repository_opened'))
    snap = session.observe()
    assert offline.calls == [cfg['start_url']]
    assert snap['checks']['goal_met']
    assert snap['actions'] == []
    assert snap['completed_rules'] == []
    assert snap['discovery']['search_proof'] == 'historical'
    assert snap['discovery']['search_status'] is None
    assert snap['discovery']['repository_search_status'] is None
    assert snap['discovery']['repository_status'] == 200
    assert snap['discovery']['repository_proof'] == 'fresh'


@pytest.mark.parametrize('change', ['failed_http', 'unfinished', 'wrong_identity', 'missing_text'])
def test_repository_resume_cannot_replay_search_to_repair_unverified_read(offline, change):
    if change == 'failed_http':
        offline.status = 503
    elif change == 'unfinished':
        offline.finish = False
    elif change == 'wrong_identity':
        offline.matches = False
    else:
        offline.text = 'different body'
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg, 'repository_opened'))
    snap = session.observe()
    assert not snap['checks']['goal_met']
    assert snap['text'] == ''
    assert all(action['kind'] == 'browser_wait' for action in snap['actions'])
    assert offline.calls == [cfg['start_url']]


@pytest.mark.parametrize('change', [
    {'version':True}, {'mode':'discover_query'}, {'phase':'filled'},
    {'url':'https://github.com/python/peps/issues'}, {'config_sha256':'0' * 64},
    {'old_control_id':'stale'}, {'url':'https://github.com/search?type=repositories&q=peps+user%3Apython'},
    {'phase':[]}, {'url':42},
])
def test_invalid_cursor_fails_before_dns_or_browser_launch(monkeypatch, change):
    cfg = config()
    saved = cursor(cfg) | change
    monkeypatch.setattr('agent_service.browser_github.socket.getaddrinfo',
        lambda *args, **kwargs:pytest.fail('invalid cursor reached DNS'))
    with pytest.raises(InteractionError, match='browser_resume_invalid'):
        GithubEdgeSession(cfg, resume=saved)
    with pytest.raises(InteractionError, match='browser_resume_invalid'):
        BrowserInteraction(cfg, resume=saved)


@pytest.mark.parametrize('phase', ['search_results', 'repository_opened'])
def test_manual_handoff_preserves_target_and_discards_old_control_ids(offline, phase):
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg, phase))
    old = session.observe()
    old_document = session.document_id
    session.human_needed = True
    session.human_reason = 'github_login_required'
    session.enter_manual()
    assert 'return_to=' + urlencode({'x':cursor(cfg, phase)['url']})[2:] in offline.calls[-1]
    assert session.observe()['actions'] == []
    assert session.human_tick() == {'manual':True}
    assert session.registry is None and session.rules == []
    assert session.resume_manual() == {'resumed':True, 'authenticated':True}
    new = session.observe()
    assert offline.calls[-1] == cursor(cfg, phase)['url']
    assert session.document_id != old_document
    assert new['discovery']['resume'] == cursor(cfg, phase)
    if old['actions']:
        assert session.execute(old['actions'][0])['code'] == 'browser_action_stale'


def test_in_memory_search_phase_survives_manual_handoff(offline):
    cfg = config()
    session = offline.session(cfg)
    session.page._url = cursor(cfg)['url']
    session.search_pending = session.search_finished = session.repository_search_finished = True
    session.search_status = session.repository_search_status = 200
    assert session.observe()['discovery']['query_verified']
    session.human_needed = True
    session.human_reason = 'github_verification_required'
    session.enter_manual()
    session.resume_manual()
    assert offline.calls[-1] == cursor(cfg)['url']
    assert session.observe()['discovery']['query_verified']


def test_in_memory_repository_phase_survives_manual_handoff(offline):
    cfg = config()
    session = offline.session(cfg)
    session.query_verified = session.repo_clicked = session.repo_finished = True
    session.search_status = session.repository_search_status = session.repo_status = 200
    assert session.observe()['checks']['goal_met']
    session.human_needed = True
    session.human_reason = 'github_verification_required'
    session.enter_manual()
    session.resume_manual()
    snap = session.observe()
    assert offline.calls == [cfg['start_url'], cfg['start_url']]
    assert snap['checks']['goal_met']
    assert snap['discovery']['search_proof'] == 'historical'
    assert snap['discovery']['search_status'] is None


def test_unknown_effect_lock_cannot_expose_resume_actions_or_enter_handoff(offline):
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg))
    session.observe()
    session._violation('browser_effect_unknown')
    session.human_needed = True
    assert session.observe()['actions'] == []
    with pytest.raises(InteractionError, match='browser_github_handoff_rejected'):
        session.enter_manual()


def test_unproved_dispatched_repo_click_cannot_return_to_search_and_replay(offline):
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg))
    session.observe()
    session.repo_clicked = session.repo_pending = True
    session.human_needed = True
    with pytest.raises(InteractionError, match='browser_github_unverified_repository_opening'):
        session.enter_manual()
    assert session.locked
    assert not session.manual
    assert session.observe()['actions'] == []


def test_unproved_search_submit_cannot_replay_after_human_handoff(offline):
    cfg = config()
    session = offline.session(cfg)
    session.search_pending = True
    session.human_needed = True
    with pytest.raises(InteractionError, match='browser_github_unverified_search_submission'):
        session.enter_manual()
    assert session.locked
    assert not session.manual
    assert offline.calls == [cfg['start_url']]


@pytest.mark.parametrize('phase', ['search_results', 'repository_opened'])
def test_proven_cursor_can_handoff_before_refetch_finishes(offline, phase):
    offline.finish = False
    cfg = config()
    session = offline.session(cfg, resume=cursor(cfg, phase))
    session.human_needed = True
    session.human_reason = 'github_verification_required'
    assert session.enter_manual()['manual']
    offline.finish = True
    session.resume_manual()
    snap = session.observe()
    assert snap['discovery']['query_verified']
    assert offline.calls[-1] == cursor(cfg, phase)['url']


def test_worker_initialization_passes_cursor_detached_to_github_adapter(monkeypatch):
    from agent_service.browser_interaction import _worker
    cfg = config()
    saved = cursor(cfg)
    received = []
    monkeypatch.setattr('agent_service.browser_github.GithubEdgeSession',
        lambda config, resume=None:received.append((config, resume)) or SimpleNamespace(close=lambda:None))
    input_bytes = b'1' + json.dumps({'op':'init', 'config':cfg, 'resume':saved}).encode() + b'\n'
    output = io.BytesIO()
    monkeypatch.setattr('agent_service.browser_interaction.sys.stdin', SimpleNamespace(buffer=io.BytesIO(input_bytes)))
    monkeypatch.setattr('agent_service.browser_interaction.sys.stdout', SimpleNamespace(buffer=output))
    _worker()
    assert received == [(cfg, saved)]
    assert json.loads(output.getvalue()) == {'ready':True}


def test_client_cursor_is_copied_before_worker_start():
    cfg = config()
    saved = cursor(cfg)
    client = BrowserInteraction(cfg, resume=saved)
    expected = deepcopy(saved)
    saved['url'] = 'https://evil.example/'
    assert client.resume == expected
