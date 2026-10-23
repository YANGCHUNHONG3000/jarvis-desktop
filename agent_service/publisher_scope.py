"""Version-two transfer scope. URL structure never establishes publisher identity."""
import re
from urllib.parse import urlsplit

POLICY = 'repository_attribution_v2'
_OWNER = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?\Z', re.I)
_REPO = re.compile(r'[a-z0-9_.-]{1,100}\Z', re.I)
_RESERVED = frozenset(('about', 'account', 'apps', 'collections', 'codespaces',
    'contact', 'customer-stories', 'enterprise', 'events', 'explore', 'features',
    'issues', 'login', 'logout', 'marketplace', 'new', 'notifications', 'orgs',
    'organizations', 'pricing', 'pulls', 'search', 'security', 'sessions', 'settings',
    'signup', 'site', 'sponsors', 'topics', 'trending', 'users'))
_MULTITENANT = ('github.com', 'githubusercontent.com', 'github.io', 'gitlab.com',
    'bitbucket.org', 'codeberg.org', 'sourceforge.net', 'huggingface.co',
    'medium.com', 'dev.to', 'notion.so', 'notion.site', 'wordpress.com',
    'blogspot.com', 'docs.google.com', 'drive.google.com', 'sites.google.com')


def enabled(goal):
    return goal.get('comparison', {}).get('publisher_policy') == POLICY


def repository_scope(url):
    """Return a GitHub repo identity only for unambiguous supported content URLs."""
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.netloc.casefold() != 'github.com'
                or p.query or p.fragment or '?' in url or '#' in url
                or '%' in p.path or '\\' in url or '//' in p.path
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            return None
        parts = p.path.removesuffix('/').split('/')[1:]
        if (len(parts) < 2 or not _OWNER.fullmatch(parts[0])
                or parts[0].casefold() in _RESERVED or not _REPO.fullmatch(parts[1])
                or parts[1] in {'.', '..'} or parts[1].casefold().endswith('.git')
                or any(part in {'', '.', '..'} for part in parts)):
            return None
        if len(parts) > 2 and (parts[2] not in {'blob', 'tree', 'commits'}
                               or len(parts) < 4 or parts[2] == 'blob' and len(parts) < 5):
            return None
        return ('github', parts[0].casefold(), parts[1].casefold())
    except (ValueError, TypeError, AttributeError):
        return None


def transfer_scope(url):
    """Known multi-tenant hosts never fall back to a whole-host scope."""
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        if any(host == domain or host.endswith('.' + domain) for domain in _MULTITENANT):
            return repository_scope(url)
        if p.scheme not in {'http', 'https'} or not host:
            return None
        return ('host', host)
    except (ValueError, TypeError, AttributeError):
        return None


def same_repository(left, right):
    scope = repository_scope(left)
    return scope is not None and scope == repository_scope(right)


REASONS = {
    'publisher_scope_mismatch': '同站其他仓库或项目的归属证据不能共用',
    'repository_scope_missing': '无法确认仓库边界，需本页归属依据',
    'publisher_identity_conflict': '同一归属范围存在不同身份，需继续核验',
}
