"""Promote verified navigation bytes without reading the website again."""
from copy import deepcopy
from hashlib import sha256
import re

from .computer import ComputerError, digest


_ATTRIBUTION = re.compile(r'©|\bcopyright\b|\bauthors?\s*:|^\s*authors?\s*$', re.I)
_SEPARATOR = '\n\n[Additional literal attribution passages from the same captured page]\n'


def _literal_extraction(page, observation, *, primary_limit=9000):
    """Retain exact source spans; marker detection does not identify an owner."""
    if page['mime'].startswith('text/plain'):
        text = page['content'].decode('utf8', errors='replace').strip()
    else:
        from .project_documents import _soup
        soup = _soup(page['content'])
        for tag in soup(['script', 'style', 'template', 'noscript']):
            tag.decompose()
        text = (soup.body or soup).get_text('\n', strip=True).strip()
    lines = text.splitlines(keepends=True)
    spans, position, used = [], 0, 0
    for index, line in enumerate(lines):
        start = position
        position += len(line)
        if not _ATTRIBUTION.search(line) or used >= 2000 or len(spans) >= 8:
            continue
        if spans and start < spans[-1]['end']:
            continue
        # HTML often separates Author / ':' / names into different nodes. Keep
        # those following literal lines rather than synthesizing a name field.
        count = 3 if re.fullmatch(r'\s*authors?\s*:?\s*', line, re.I) else 2
        end = min(len(text), start + sum(len(v) for v in lines[index:index+count]),
                  start + 2000 - used)
        literal = text[start:end].rstrip()
        if not literal:
            continue
        end = start + len(literal)
        spans.append({'start': start, 'end': end, 'text': literal})
        used += len(literal)
    primary = observation.text[:primary_limit]
    additional = [span['text'] for span in spans if span['text'] not in primary]
    result = primary + (_SEPARATOR + '\n…\n'.join(additional) if additional else '')
    from .contracts import Observation
    return (Observation(observation.requested_url, observation.final_url,
                        observation.title, result, observation.captured_at),
            {'full_text_sha256': sha256(text.encode('utf8')).hexdigest(),
             'attribution_spans': spans})


def _capture(url, query, discoveries, *, kind='navigation_capture_v3'):
    from .comparison_evidence_discovery import cached_page
    from .project_documents import extract
    page = cached_page(url, discoveries)
    if page is None:
        return None
    observation = extract(url, page['mime'], page['content'], query, page['captured_at'])
    proof = {'kind': kind, 'url': url, 'query': query,
             'prior_sha256': digest(discoveries), 'ref': page['ref'],
             'source_sha256': page['source_sha256']}
    if kind in ('navigation_capture_v2', 'navigation_capture_v3'):
        observation, spans = _literal_extraction(page, observation,
                                                primary_limit=5600 if kind == 'navigation_capture_v3' else 9000)
        proof.update(spans)
    elif kind != 'navigation_capture_v1':
        raise ComputerError('agent_evidence_changed')
    return observation, proof


class CachedNavigationReader:
    def __init__(self, fallback, query, discoveries):
        self.fallback, self.query = fallback, query
        self.discoveries = deepcopy(discoveries)
        self.last_document_proof = None

    def read(self, url, *, should_stop=lambda: False):
        if should_stop():
            raise ComputerError('agent_cancelled')
        result = _capture(url, self.query, self.discoveries)
        if result is not None:
            observation, self.last_document_proof = result
            return observation
        observation = self.fallback.read(url, should_stop=should_stop)
        self.last_document_proof = getattr(self.fallback, 'last_document_proof', None)
        return observation


def validate_record(record, url, query, discoveries):
    proof = record.get('document_proof')
    kind = proof.get('kind') if isinstance(proof, dict) else None
    if kind not in ('navigation_capture_v1', 'navigation_capture_v2', 'navigation_capture_v3'):
        if _capture(url, query, discoveries) is None:
            return False
        raise ComputerError('agent_evidence_changed')
    expected = _capture(url, query, discoveries, kind=kind)
    if expected is None:
        raise ComputerError('agent_evidence_changed')
    observation, expected_proof = expected
    if proof != expected_proof or record.get('observation') != observation.to_dict():
        raise ComputerError('agent_evidence_changed')
    return True
