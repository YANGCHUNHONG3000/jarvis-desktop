"""Bing link decoding is local candidate discovery, never redirect navigation."""
import base64
from urllib.parse import urlencode

import pytest

from agent_service.research import _candidate, search_public


def bing_link(target, **extra):
    payload = base64.urlsafe_b64encode(target.encode('utf-8')).decode('ascii').rstrip('=')
    return 'https://www.bing.com/ck/a?' + urlencode({'p': 'synthetic', 'u': 'a1' + payload, 'ntb': '1', **extra})


def test_real_shape_decodes_product_and_preserves_query():
    target = 'https://www.logitech.com/en-us/shop/p/mk270-wireless-keyboard-mouse?x=1&y=2'
    assert _candidate(bing_link(target)) == target
    assert _candidate(bing_link(target).replace('&', '&amp;')) == target


def test_current_real_bing_sentinel_before_tracking_fields():
    target = 'https://www.adafruit.com/product/1738'
    link = bing_link(target).replace('/ck/a?', '/ck/a?!&&')
    assert _candidate(link) == target
    assert _candidate(link.replace('&', '&amp;')) == target


@pytest.mark.parametrize('prefix', ['!', '!&', '!!&&', '!&&&', '&', '&&', '%21&&', '!&&broken&'])
def test_other_malformed_prefixes_are_not_normalized(prefix):
    link = bing_link('https://example.org/a').replace('/ck/a?', '/ck/a?' + prefix)
    assert _candidate(link) is None


def test_padding_and_unicode_url_validated_by_existing_policy():
    target = 'https://example.org/café?q=keyboard'
    assert _candidate(bing_link(target)) == 'https://example.org/caf%C3%A9?q=keyboard'
    encoded = base64.urlsafe_b64encode(b'https://example.org/a').decode('ascii')
    assert _candidate('https://www.bing.com/ck/a?u=a1' + encoded) == 'https://example.org/a'


@pytest.mark.parametrize('target', [
    'http://example.org/a', '//example.org/a', 'ftp://example.org/a', 'javascript:alert(1)',
    'https://localhost/a', 'https://x.internal/a', 'https://x.local/a',
    'https://127.0.0.1/a', 'https://10.0.0.1/a', 'https://[::1]/a',
    'https://169.254.169.254/a', 'https://user:secret@example.org/a',
    'https://example.org:8443/a', 'https://example.org/a/../b', 'https://example.org/%zz',
    'https://example.org/\nsecret', 'https://www.bing.com/ck/a?u=a1abc',
    'https://html.duckduckgo.com/html/',
])
def test_unsafe_decoded_targets_rejected(target):
    assert _candidate(bing_link(target)) is None


@pytest.mark.parametrize('url', [
    'http://www.bing.com/ck/a?u=a1aGVsbG8', '//www.bing.com/ck/a?u=a1aGVsbG8',
    'https://www.bing.com:443/ck/a?u=a1aGVsbG8',
    'https://user@www.bing.com/ck/a?u=a1aGVsbG8',
    'https://www.bing.com/ck/a?u=a1aGVsbG8#fragment',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&u=a1aGVsbG8',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&p=1&p=2',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&%75=a1aGVsbG8',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&broken',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&p=%ff',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&p=%zz',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&=value',
    'https://www.bing.com/ck/a?u=a1aGVsbG8&p=' + 'a' * 8200,
    'https://www.bing.com/ck/a', 'https://www.bing.com/ck/a?u=',
    'https://www.bing.com/ck/a?u=a1', 'https://www.bing.com/ck/a?u=b1aGVsbG8',
    'https://www.bing.com/ck/a?u=a1%%%%', 'https://www.bing.com/ck/a?u=a1abc+',
    'https://www.bing.com/ck/a?u=a1abc/', 'https://www.bing.com/ck/a?u=a1a===',
    'https://www.bing.com/ck/a?u=a1_w',  # Invalid UTF-8.
    'https://www.bing.com/ck/a?u=a1YQ=',  # Partially supplied padding.
    'https://www.bing.com/ck/a?u=a1' + 'A' * 2733,
    'https://[invalid/ck/a?u=a1abc',
])
def test_malformed_wrappers_fail_closed(url):
    assert _candidate(url) is None


def test_noncanonical_base64_pad_bits_rejected():
    # Zg is canonical for f; Zh has the same decoded byte with nonzero pad bits.
    assert _candidate('https://www.bing.com/ck/a?u=a1Zh') is None


def test_existing_direct_and_duckduckgo_links_unchanged():
    target = 'https://www.adafruit.com/product/1738'
    assert _candidate(target) == target
    assert _candidate('/l/?uddg=' + target) == target
    assert _candidate('https://duckduckgo.com/l/?uddg=' + target) == target
    assert _candidate('https://www.bing.com/search?q=x') is None


def test_search_decodes_before_scoring_and_never_requests_redirect():
    good = 'https://www.logitech.com/en-us/shop/p/mk270-wireless-keyboard-mouse'
    other = 'https://example.org/other'
    body = '<ol>' + ''.join('<li class="b_algo"><h2><a href="' + bing_link(url) + '">' + title + '</a></h2></li>'
        for url, title in [(other, 'Other'), ('https://127.0.0.1/a', 'Keyboard'), (good, 'Keyboard')]) + '</ol>'
    calls = []
    class Response:
        def __init__(self, status, content):
            self.status_code, self.content = status, content.encode('utf8')
            self.headers = {'Content-Type': 'text/html'}
        def iter_content(self, size):
            yield self.content
        def close(self):
            pass
    class Session:
        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs['allow_redirects'] is False
            return Response(503, '') if 'duckduckgo' in url else Response(200, body)
    assert search_public('keyboard', 2, session=Session()) == [good, other]
    assert calls == ['https://html.duckduckgo.com/html/', 'https://www.bing.com/search']
