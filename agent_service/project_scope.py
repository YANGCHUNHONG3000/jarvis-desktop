"""Version-three project boundaries; structure never establishes authorship."""
import re
from urllib.parse import urlsplit

from . import publisher_scope as legacy

POLICY = 'project_attribution_v3'
_SEGMENT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z')
_RESERVED = frozenset(('api', 'docs', 'help', 'explore', 'search', 'about', 'login',
    'logout', 'users', 'user', 'settings', 'groups', 'projects', 'dashboard',
    'admin', 'oauth', 'profile', 'organizations', 'collections', 'join', 'pricing',
    'discussions', 'blog', 'posts', 'notifications', 'datasets', 'spaces', 'models'))


def enabled(goal):
    return goal.get('comparison', {}).get('publisher_policy') in {POLICY, 'project_attribution_v4', 'project_attribution_v5', 'project_attribution_v6'}


def _parsed(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.netloc not in {'gitlab.com', 'huggingface.co', 'codeberg.org'}
                or any(c in url for c in ('?', '#', '%', '\\')) or '//' in p.path
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            return None
        parts = p.path.removesuffix('/').split('/')[1:]
        if not parts or any(part in {'', '.', '..'} for part in parts):
            return None
        return p.netloc, parts
    except (ValueError, AttributeError, TypeError):
        return None


def _names(parts):
    return (all(_SEGMENT.fullmatch(s) and not s.endswith('.git') for s in parts)
            and parts[0].casefold() not in _RESERVED)


def project_scope(url):
    github = legacy.repository_scope(url)
    if github:
        return github
    parsed = _parsed(url)
    if parsed is None:
        return None
    host, parts = parsed
    if host == 'gitlab.com':
        # /group/subgroup may denote a group. Only /-/ content identifies a repo.
        if parts.count('-') != 1:
            return None
        edge = parts.index('-')
        names, tail = parts[:edge], parts[edge + 1:]
        if (not 2 <= len(names) <= 10 or not _names(names) or len(tail) < 2
                or tail[0] not in {'blob', 'tree', 'raw', 'commits'}
                or tail[0] in {'blob', 'raw'} and len(tail) < 3):
            return None
        return ('gitlab', *names)
    if host == 'huggingface.co':
        kind = 'model'
        if parts[0] in {'datasets', 'spaces'}:
            kind = 'dataset' if parts[0] == 'datasets' else 'space'
            parts = parts[1:]
        if len(parts) < 2 or not _names(parts[:2]):
            return None
        tail = parts[2:]
        if tail and (len(tail) < 2 or tail[0] not in {'blob', 'tree', 'raw', 'commits'}
                     or tail[0] in {'blob', 'raw'} and len(tail) < 3):
            return None
        return ('huggingface', kind, *parts[:2])
    if host == 'codeberg.org':
        if len(parts) < 2 or not _names(parts[:2]):
            return None
        tail = parts[2:]
        if tail and (len(tail) < 3 or tail[0] not in {'src', 'raw', 'commits'}
                     or tail[1] not in {'branch', 'tag', 'commit'}):
            return None
        return ('codeberg', *parts[:2])
    return None


def same_project(left, right):
    scope = project_scope(left)
    return scope is not None and scope == project_scope(right)


def project_site(url):
    """Canonical search restriction only; never a URL to fetch implicitly."""
    scope = project_scope(url)
    if scope is None:
        return None
    if scope[0] == 'huggingface':
        prefix = {'model': '', 'dataset': 'datasets/', 'space': 'spaces/'}[scope[1]]
        return 'huggingface.co/' + prefix + '/'.join(scope[2:])
    host = {'github': 'github.com', 'gitlab': 'gitlab.com', 'codeberg': 'codeberg.org'}[scope[0]]
    return host + '/' + '/'.join(scope[1:])


def requires_project(url):
    try:
        host = (urlsplit(url).hostname or '').lower().rstrip('.')
        return any(host == site or host.endswith('.' + site) for site in legacy._MULTITENANT)
    except (ValueError, AttributeError, TypeError):
        return True


def transfer_scope(url):
    return project_scope(url) if requires_project(url) else legacy.transfer_scope(url)
