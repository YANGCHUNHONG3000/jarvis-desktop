"""Narrow project-role evidence; a captured assertion is not legal ownership."""
import re
from urllib.parse import urlsplit

from .project_scope import project_scope

POLICY = 'project_attribution_v5'
_ROLES = {'project_author', 'project_maintainer'}


def _member(value):
    if (len(value) > 80 or len(value.split()) > 6
            or not any(c.isalpha() for c in value)
            or any(not (c.isalnum() or c in " .'-’~_@") for c in value)
            or '@' in value.removeprefix('@') or '...' in value
            or re.search(r'\b(?:authors?|maintainers?|contributors?|references?|citation|copyright|'
                         r'project|published|maintained|written|not|never|license|by|is|are|was|were|'
                         r'et al|follow|more)\b', value, re.I)):
        return False
    words = value.split()
    if len(words) == 1:
        return not value.endswith('.')
    particles = {'de', 'del', 'van', 'von', 'der', 'den', 'da', 'das', 'dos', 'di', 'du', 'la', 'le'}
    for word in words:
        if any(c.isdigit() for c in word):
            return False
        if '.' in word and not re.fullmatch(r'(?:[^\W\d_]\.)+', word):
            return False
        letter = next((c for c in word if c.isalpha()), '')
        if not letter or (letter.islower() and word not in particles):
            return False
    return True


def enabled(goal):
    return goal.get('comparison', {}).get('publisher_policy') == POLICY


def _roster(source, publisher):
    path = urlsplit(source['url']).path
    scope = project_scope(source['url'])
    if scope is None:
        return False
    parts = path.strip('/').split('/')
    edge = (parts.index('-') + 1 if scope[0] == 'gitlab' else
            3 if scope[0] == 'huggingface' and scope[1] != 'model' else 2)
    allowed = {'src', 'raw'} if scope[0] == 'codeberg' else {'blob', 'raw'}
    if (len(parts) <= edge + 2 or parts[edge] not in allowed
            or path.rsplit('/', 1)[-1].casefold() not in
            {'authors', 'authors.txt', 'authors.md', 'authors.rst'}):
        return False
    text, name, quote = source['text'], publisher['name'], publisher['quote']
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if (not lines or not re.fullmatch(r'(?:#{1,3}\s+)?AUTHORS', lines[0], re.I)
            or not 1 <= len(lines) - 1 <= 250):
        return False
    members = lines[1:]
    # A roster has names/handles, not citations, nested sections or prose. This
    # contract concerns the captured roster only, not an exhaustive author list.
    if not all(_member(member) for member in members):
        return False
    if members.count(name) != 1 or len(set(members)) != len(members):
        return False
    # Require a complete name line plus an adjacent member or the roster heading.
    # This must be a literal contiguous substring; it cannot be stitched together.
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0 or quote.strip() == name:
        return False
    at = start
    selected_lines = []
    for line in quote.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        end = at + len(body)
        if ((at == 0 or text[at-1] in '\r\n')
                and (end == len(text) or text[end] in '\r\n')):
            if body.strip():
                selected_lines.append(body.strip())
        at += len(line)
    return name in selected_lines and len(selected_lines) >= 2


def role(source, publisher):
    """Return only unambiguous, nonexclusive roles tied to this exact project."""
    if project_scope(source['url']) is None:
        return None
    text, quote, name = source['text'], publisher['quote'], publisher['name']
    if (not quote.strip() or len(quote) > 240 or not name.strip()
            or text.count(quote) != 1 or name not in quote
            or re.search(r'\b(?:not|never|neither|no longer)\b', quote, re.I)):
        return None
    if _roster(source, publisher):
        return 'project_author'
    q, n = ' '.join(quote.split()), re.escape(' '.join(name.split()))
    subject = r'(?:this|the)\s+project\s+(?:(?:is|was|has been)\s+)?'
    # Full sentence/quote matching prevents attributing a quoted paper or another
    # project based on a role word elsewhere in the supplied evidence.
    if re.fullmatch(subject + r'(?:authored|created|started|founded)\s+by\s+' + n + r'\.?', q, re.I):
        return 'project_author'
    if (re.fullmatch(subject + r'maintained\s+by\s+' + n + r'\.?', q, re.I)
            or re.fullmatch(r'(?:In\s+[0-9]{4}\s+)?' + n + r'\s+(?:is|became|serves as)\s+(?:the\s+|a\s+)?'
                           r'(?:co-)?maintainer\s+of\s+(?:this|the)\s+project\.?', q, re.I)):
        return 'project_maintainer'
    return None


def compatible(sources, anchors, roles):
    """Several credited people are not several independent publication sources."""
    scopes = [project_scope(sources[sid]['url']) for sid in anchors]
    return (bool(scopes) and scopes[0] is not None
            and all(scope == scopes[0] for scope in scopes)
            and all(roles.get(sid) in _ROLES for sid in anchors))


def candidates(source):
    """At most two literal role suggestions, never a substituted model verdict."""
    found = []
    def add(name, quote):
        publisher = {'name': name, 'quote': quote}
        kind = role(source, publisher)
        if kind and not any(item['name'] == name for item in found):
            found.append({**publisher, 'role': kind})
    lines = list(re.finditer(r'[^\r\n]+', source['text']))
    if lines and re.fullmatch(r'(?:#{1,3}\s+)?AUTHORS', lines[0].group().strip(), re.I):
        for previous, current in zip(lines[:2], lines[1:3]):
            add(current.group().strip(), source['text'][previous.start():current.end()])
    # Capture complete explicit project sentences with bounded names. Ambiguous
    # punctuation/name forms remain for ordinary review rather than guessing.
    patterns = (
        r'\b(?:This|The) project (?:(?:is|was|has been) )?(?:authored|created|started|founded|maintained) by (?P<name>[^\r\n.!?]{1,80})\.',
        r'\b(?:In [0-9]{4} )?(?P<name>[^\r\n.!?]{1,80}) (?:is|became|serves as) (?:the |a )?(?:co-)?maintainer of (?:this|the) project\.',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source['text']):
            if len(found) >= 2:
                break
            add(match.group('name').strip(), match.group())
    return found[:2]
