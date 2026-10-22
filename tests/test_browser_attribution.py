"""Opt-in full visible-body attribution capture; no browser or network needed."""
from contextlib import nullcontext
import json
import sys
from types import SimpleNamespace

import pytest

from agent_service import browser


URL = 'https://example.org/ODF'
ARTICLE = 'ODF and DOCX are supported document formats. ' * 5
FOOTER = 'The Example Foundation Wiki is published by Example Foundation.'


class Locator:
    def __init__(self, values):
        self.values = values

    def inner_text(self, timeout):
        assert timeout == 5000
        return self.values[0]

    def count(self):
        return len(self.values)

    def nth(self, index):
        return Locator([self.values[index]])


class Page:
    url = URL
    main_frame = object()

    def __init__(self, body=None):
        self.body = body if body is not None else 'Navigation\n' + ARTICLE + '\n' + FOOTER
        self.selectors = []

    def locator(self, selector):
        self.selectors.append(selector)
        return Locator([self.body] if selector == 'body' else [ARTICLE])

    def goto(self, url, **kwargs):
        assert url == URL
        return SimpleNamespace(ok=True, headers={'content-type': 'text/html'})

    def title(self):
        return 'Format reference'


def test_attribution_body_preserves_footer_while_default_preserves_main_preference():
    page = Page()
    assert browser._readable_text(page) == ARTICLE.strip()
    page.selectors.clear()
    assert browser._readable_text(page, include_attribution=True).endswith(FOOTER)
    assert page.selectors == ['body']


@pytest.mark.parametrize('include_attribution', [False, True])
def test_edge_browser_propagates_option_and_still_uses_existing_excerpt(monkeypatch, include_attribution):
    page = Page()
    calls = []
    context = SimpleNamespace(new_page=lambda: page, route=lambda *_: None)
    native = SimpleNamespace(new_context=lambda **_k: context, close=lambda: calls.append('close'))
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_k: native))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api',
                        SimpleNamespace(sync_playwright=lambda: nullcontext(playwright)))
    monkeypatch.setattr(browser, 'fetch_source', lambda url: calls.append(url) or ('text/html', b'<body>snapshot</body>'))
    excerpt = browser._excerpt
    def capture(text, query):
        calls.append((text, query))
        return excerpt(text, query)
    monkeypatch.setattr(browser, '_excerpt', capture)
    value = browser.EdgeBrowser().read(URL, 'formats publisher', include_attribution=include_attribution)
    assert (FOOTER in value.text) == include_attribution
    assert value.text == (page.body if include_attribution else ARTICLE.strip())
    assert calls == [URL, (page.body if include_attribution else ARTICLE.strip(), 'formats publisher'), 'close']


def test_attribution_capture_retains_existing_twelve_thousand_character_limit():
    page = Page('Opening\n' + 'Unrelated filler\n' * 1600 + FOOTER)
    body = browser._readable_text(page, include_attribution=True)
    value = browser._excerpt(body, 'published Example Foundation')
    assert FOOTER in value
    assert len(value) <= 12000


@pytest.mark.parametrize('query,include_attribution,tail', [
    (None, False, [URL]), ('formats', False, [URL, 'formats']),
    (None, True, [URL, '', '--include-attribution']),
    ('formats', True, [URL, 'formats', '--include-attribution']),
])
def test_process_default_command_unchanged_and_fixed_flag_explicit(monkeypatch, query, include_attribution, tail):
    calls = []
    observation = {'requested_url': URL, 'final_url': URL, 'title': 'Formats',
                   'text': ARTICLE, 'captured_at': '2026-10-21T00:00:00Z'}
    def child(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps(observation)
    monkeypatch.setattr('agent_service.process.run_child', child)
    assert browser.BrowserProcess(query, include_attribution=include_attribution).read(URL).text == ARTICLE
    assert calls[0][0] == [sys.executable, '-m', 'agent_service.browser', *tail]
    assert calls[0][1]['timeout'] == 40


@pytest.mark.parametrize('argv,query,include_attribution', [
    (['worker', URL], None, False), (['worker', URL, 'formats'], 'formats', False),
    (['worker', URL, '', '--include-attribution'], '', True),
    (['worker', URL, 'formats', '--include-attribution'], 'formats', True),
])
def test_worker_accepts_and_propagates_explicit_fixed_flag(monkeypatch, argv, query, include_attribution):
    calls = []
    monkeypatch.setattr(browser.EdgeBrowser, 'read', lambda self, *a, **k: calls.append((a, k)) or 'observation')
    assert browser._worker_read(argv) == 'observation'
    assert calls == [((URL, query), {'include_attribution': include_attribution})]


@pytest.mark.parametrize('argv', [[], ['worker'], ['worker', URL, 'query', '--arbitrary'],
    ['worker', URL, 'query', '--include-attribution', 'extra']])
def test_worker_rejects_arbitrary_flags_before_read(monkeypatch, argv):
    monkeypatch.setattr(browser.EdgeBrowser, 'read', lambda *_a, **_k: pytest.fail('unexpected GET'))
    with pytest.raises(ValueError, match='Invalid browser worker arguments'):
        browser._worker_read(argv)


@pytest.mark.parametrize('option', [None, 'true', 1, 0])
def test_flag_requires_real_boolean_before_external_work(monkeypatch, option):
    monkeypatch.setattr(browser, 'fetch_source', lambda *_: pytest.fail('unexpected GET'))
    with pytest.raises(ValueError, match='include_attribution'):
        browser.BrowserProcess(include_attribution=option)
    with pytest.raises(ValueError, match='include_attribution'):
        browser.EdgeBrowser().read(URL, include_attribution=option)
