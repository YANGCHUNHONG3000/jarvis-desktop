"""Fixed, supervised public-search worker; no shell or arbitrary child command."""
import json
import math
import sys

from .computer import ComputerError
from .policy import source_url

SEARCH_TIMEOUT = 25


def validate_query(query, max_sources, *, provider=None, comparison=False):
    if (type(query) is not str or not query.strip() or len(query) > 500
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in query)):
        raise ComputerError('search_query_invalid')
    if type(comparison) is not bool or comparison and provider != 'bing':
        raise ComputerError('search_provider_invalid')
    if type(max_sources) is not int or not 1 <= max_sources <= (4 if comparison else 2):
        raise ComputerError('search_count_invalid')


def validate_result(value, max_sources):
    from datetime import datetime
    if type(max_sources) is not int or not 1 <= max_sources <= 4:
        raise ComputerError('search_receipt_invalid')
    if type(value) is not dict or set(value) != {'urls', 'searched_at'}:
        raise ComputerError('search_receipt_invalid')
    urls = value['urls']
    if type(urls) is not list or not 1 <= len(urls) <= max_sources:
        raise ComputerError('search_receipt_invalid')
    try:
        if (any(type(url) is not str or len(url) > 2000 or source_url(url) != url for url in urls)
                or len(set(urls)) != len(urls) or type(value['searched_at']) is not str
                or len(value['searched_at']) > 80 or datetime.fromisoformat(value['searched_at']).utcoffset() is None):
            raise ValueError()
    except (ValueError, TypeError):
        raise ComputerError('search_receipt_invalid') from None
    return value


class SearchProcess:
    def __init__(self, *, provider=None, comparison=False):
        if provider is not None and provider != 'bing':
            raise ComputerError('search_provider_invalid')
        if type(comparison) is not bool or comparison and provider != 'bing':
            raise ComputerError('search_provider_invalid')
        self.provider, self.comparison = provider, comparison

    def search(self, query, max_sources=2, *, should_stop=lambda: False, timeout=SEARCH_TIMEOUT):
        from .process import run_child
        validate_query(query, max_sources, provider=self.provider, comparison=self.comparison)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= SEARCH_TIMEOUT:
            raise ComputerError('search_timeout')
        if should_stop():
            raise ComputerError('search_cancelled')
        request = {'query':query,'max_sources':max_sources}
        if self.provider is not None:
            if self.provider != 'bing':
                raise ComputerError('search_provider_invalid')
            request['provider'] = self.provider
        if self.comparison:
            request['comparison'] = True
        command = [sys.executable, '-B', '-m', 'agent_service.computer_search',
                   json.dumps(request, ensure_ascii=False)]
        try:
            raw = run_child(command, should_stop=should_stop, timeout=timeout)
        except InterruptedError:
            raise ComputerError('search_cancelled') from None
        except TimeoutError:
            raise ComputerError('search_timeout') from None
        except (OSError, RuntimeError, UnicodeError):
            raise ComputerError('search_unavailable') from None
        if should_stop():
            raise ComputerError('search_cancelled')
        try:
            if type(raw) is not str or len(raw) > 10000:
                raise ValueError()
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise ComputerError('search_receipt_invalid') from None
        if value == {'error': 'search_unavailable'}:
            raise ComputerError('search_unavailable')
        return validate_result(value, max_sources)


def main():
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    # All search imports and external work happen after parent process ownership.
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        request = json.loads(sys.argv[1])
        fields = {'query','max_sources'}
        if (type(request) is not dict or set(request) not in (fields, fields | {'provider'}, fields | {'provider', 'comparison'})
                or 'provider' in request and request['provider'] != 'bing'):
            raise ValueError()
        comparison = request.get('comparison', False)
        if 'comparison' in request and comparison is not True:
            raise ValueError()
        validate_query(request['query'],request['max_sources'],
                       provider=request.get('provider'), comparison=comparison)
        from .research import search_public
        from .contracts import utc_now
        extra = {'provider':'bing'} if 'provider' in request else {}
        if comparison:
            extra['comparison'] = True
        value = validate_result({'urls': search_public(request['query'], request['max_sources'], **extra),
                                 'searched_at': utc_now()}, request['max_sources'])
    except Exception:
        value = {'error': 'search_unavailable'}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
