"""Opt-in hosted-document boundaries and literal author suggestions for v6.

Read the Docs project subdomains and /projects/<alias> are the only supported
hosting routes. Suggestions remain untrusted evidence for a model to review.
"""
import re
from urllib.parse import urlsplit

POLICY = 'project_attribution_v6'
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_FIELD = re.compile(
    r'(?im)^(?:Author|Authors|Author\(s\))[ \t]*'
    r'(?:\r?\n[ \t]*)?:[ \t]*(?:\r?\n[ \t]*)?'
    r'(?P<names>[^\r\n]+)')
_PLATFORMS = frozenset({'read the docs', 'readthedocs', 'sphinx', 'github',
                        'gitlab', 'python', 'microsoft', 'openai'})


def enabled(goal):
    return (type(goal) is dict and type(goal.get('comparison')) is dict
            and goal['comparison'].get('publisher_policy') == POLICY)


def _parsed(url):
    if (type(url) is not str or any(ord(c) < 33 or ord(c) > 126 for c in url)
            or any(c in url for c in '%\\?#')):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        # Exact netloc equality also rejects credentials, explicit ports and a
        # trailing dot; do not normalize ambiguous authorities into permission.
        if (parsed.scheme != 'https' or parsed.netloc.lower() != host
                or not host.endswith('.readthedocs.io')):
            return None
        project = host.removesuffix('.readthedocs.io')
        if not _LABEL.fullmatch(project):
            return None
        path = parsed.path
        if not path:
            parts = []
        else:
            if not path.startswith('/') or '//' in path:
                return None
            parts = path.removesuffix('/').split('/')[1:]
            if parts == ['']:
                parts = []
            if any(p in {'', '.', '..'} for p in parts):
                return None
        return host, parts
    except (ValueError, TypeError, AttributeError):
        return None


def family(url):
    """Keep one hosted project together without merging neighboring projects."""
    parsed = _parsed(url)
    return parsed[0] if parsed is not None else None


def scope(url):
    """Bind author transfer to a project host and any explicit subproject alias."""
    parsed = _parsed(url)
    if parsed is None:
        return None
    host, parts = parsed
    if parts and parts[0].casefold() == 'projects':
        if (parts[0] != 'projects' or len(parts) < 2
                or not _LABEL.fullmatch(parts[1])):
            return None
        return ('readthedocs', host, parts[1])
    return ('readthedocs', host)


def transfer_scope(url):
    """Never let a malformed hosted URL fall back to ordinary host transfer."""
    if type(url) is str and 'readthedocs.io' in url.casefold():
        return scope(url)
    from .project_scope import transfer_scope as legacy_transfer_scope
    return legacy_transfer_scope(url)


def candidates(source):
    """Return at most two exact author-field candidates, never a verdict."""
    if (type(source) is not dict or scope(source.get('url')) is None
            or type(source.get('text')) is not str):
        return []
    from .publisher_roles import _member
    text = source['text']
    fields = list(_FIELD.finditer(text))
    # Multiple fields may describe different publications or be repeated UI.
    if len(fields) != 1:
        return []
    match = fields[0]
    quote, raw_names = match.group(), match.group('names')
    project = family(source['url']).removesuffix('.readthedocs.io')
    if (len(quote) > 240 or text.count(quote) != 1
            or raw_names != raw_names.strip()
            or re.search(r'\b(?:not|never|neither|no longer)\b', quote, re.I)):
        return []
    names = raw_names.split(', ')
    if (not 1 <= len(names) <= 2 or len(set(names)) != len(names)
            or any(len(name) > 80 or not _member(name)
                   or name.casefold().removeprefix('@') in _PLATFORMS
                   or name.casefold().removeprefix('@') == project for name in names)):
        return []
    # A following name line would make the apparent field incomplete. A prose
    # continuation or punctuation also cannot silently be dropped from a name.
    following = text[match.end():].splitlines()
    next_line = next((line.strip() for line in following if line.strip()), '')
    if next_line and (_member(next_line) or next_line.startswith((',', ';', 'and '))):
        return []
    return [{'name': name, 'quote': quote, 'role': 'project_author'} for name in names]


def role(source, publisher):
    """Only an exact suggested field can establish this nonexclusive role."""
    if type(publisher) is not dict or set(publisher) != {'name', 'quote'}:
        return None
    return ('project_author' if any(
        publisher == {key: item[key] for key in ('name', 'quote')}
        for item in candidates(source)) else None)
