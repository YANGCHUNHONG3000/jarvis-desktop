"""Complete, receipt-bound main-body intake for bounded Word edit chains."""

from hashlib import sha256
import json
from pathlib import Path
import time

from . import artifact_source, office_source
from .computer import safe_root
from .computer_agent_tools import _load
from .office_edit import _read, verify_edit_source


def _managed_document(path, base):
    from .generated_edit_table import _WORKFLOW

    return (path.name == 'document.docx'
            and artifact_source._STEP.fullmatch(path.parent.name) is not None
            and _WORKFLOW.fullmatch(path.parent.parent.name) is not None
            and artifact_source._TASK.fullmatch(path.parent.parent.parent.name) is not None
            and path.parent.parent.parent.parent == base)


def _document(path):
    parts = office_source._package_parts(path)
    # Parse the complete body first. The generic parser rejects hidden content,
    # fields, extra tables and nonempty text after a table before we omit layout.
    observed, parsed_coverage = office_source._document(parts)
    body = office_source._xml(parts, 'word/document.xml').find(f'{{{office_source._W}}}body')
    if body is None:
        raise ValueError('edited_document_body_invalid')
    table_seen = False
    tails = []
    for child in body:
        if child.tag == f'{{{office_source._W}}}tbl':
            table_seen = True
        elif table_seen and child.tag == f'{{{office_source._W}}}p':
            tails.append(child)
    if len(tails) > 1:
        raise ValueError('edited_document_order_unsupported')
    if tails:
        # A native Word table may need a final, formatting-only empty paragraph.
        # Restrict this to known formatting nodes; _word_text ignores pPr, so
        # checking its empty return alone would miss a disguised object there.
        allowed = {f'{{{office_source._W}}}{name}' for name in (
            'p', 'pPr', 'spacing', 'rPr', 'rFonts', 'sz', 'szCs', 'lang', 'pStyle',
            'jc', 'ind', 'color', 'b', 'i', 'u', 'keepNext', 'keepLines',
            'widowControl', 'contextualSpacing')}
        if (office_source._word_text(tails[0]) != ''
                or any(node.tag not in allowed or (node.text or '').strip()
                       or (node.tail or '').strip() for node in tails[0].iter())):
            raise ValueError('edited_document_order_unsupported')
    if len(json.dumps(observed, ensure_ascii=False, separators=(',', ':'))) > 32_000:
        raise office_source.SourceError('source_observation_oversize')
    office_source._writer_compatible(path.name, 'document', observed)
    # Keep earlier complete-body snapshots byte-compatible when no tail exists.
    if tails:
        parsed_coverage = {**parsed_coverage, 'omitted_terminal_empty_paragraphs': 1}
    return observed, parsed_coverage


def _derive(ref, base):
    from .edited_artifact_source import prepare_reference

    base = safe_root(base)
    edits, seen, current = [], set(), ref
    while True:
        artifact = safe_root(current['artifact'])
        if artifact in seen or len(edits) >= 4:
            raise ValueError('edited_document_chain_invalid')
        seen.add(artifact)
        if artifact.name != 'edited-document.docx' or prepare_reference(
                artifact, current['sha256'], base) != current:
            raise ValueError('edited_document_ref_changed')
        manifest, _ = _load(artifact.parent / 'edit-manifest.json')
        if manifest['spec']['kind'] != 'document':
            raise ValueError('edited_document_kind_invalid')
        source_root = safe_root(manifest['source_root'])
        source = verify_edit_source(source_root)
        original = safe_root(_read(source_root / 'source.json')['original'])
        edits.append(current)
        if original.name != 'edited-document.docx':
            break
        current = prepare_reference(original, source['sha256'], base)
    root_hash = source['sha256']
    if sha256(original.read_bytes()).hexdigest() != root_hash:
        raise ValueError('edited_document_root_changed')
    root_ref = {'artifact': str(original), 'sha256': root_hash}
    if _managed_document(original, base):
        # A managed creation root must also retain its creation receipt.
        root_ref['creation'] = artifact_source.prepare_artifact_source(original, root_hash, base)
    final = safe_root(ref['artifact'])
    try:
        observed, parsed = _document(final)
    except (office_source.SourceError, ValueError):
        from .structured_document_source import parse
        observed, parsed = parse(final)
    else:
        parsed = {**parsed, 'scope': 'verified_edited_document_complete_main_body'}
    coverage = {**parsed, 'edit_depth': len(edits), 'layout_preserved': False}
    return root_ref, list(reversed(edits)), observed, coverage


def snapshot_reference(ref, root, base, *, stop=lambda: False, deadline=None):
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError('source_cancelled')
    derived = _derive(ref, base)
    root_ref, edits, observed, coverage = derived
    artifact = safe_root(ref['artifact'])
    sources = root / 'sources'
    sources.mkdir(exist_ok=False)
    copied, size = office_source._copy_stable(artifact, sources / 'source.docx')
    if copied != ref['sha256'] or _derive(ref, base) != derived:
        raise ValueError('edited_document_changed')
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError('source_cancelled')
    kind = ('document_structure' if coverage['scope'] == 'verified_edited_document_structure'
            else 'document')
    result = {'source_id': 'selected', 'kind': kind, 'sha256': copied,
              'bytes': size, 'display_name': artifact.name,
              'coverage': coverage, 'observed': observed}
    office_source._write_json_new(sources / 'provenance.json',
                                  {'kind': 'verified_edited_document', 'edited': ref,
                                   'edits': edits, 'root': root_ref})
    office_source._write_json_new(sources / 'source.json', result)
    return result


def verify_snapshot(task, snapshot, provenance, base):
    if (type(provenance) is not dict
            or set(provenance) != {'kind', 'edited', 'edits', 'root'}
            or provenance['kind'] != 'verified_edited_document'
            or type(snapshot) is not dict):
        raise ValueError('edited_document_provenance_invalid')
    ref = provenance['edited']
    if (type(ref) is not dict or set(ref) != {
            'version', 'kind', 'artifact', 'sha256', 'manifest_sha256', 'receipt_sha256'}):
        raise ValueError('edited_document_provenance_invalid')
    source = task / 'sources' / 'source.docx'
    if (not source.is_file() or office_source._linked(source)
            or snapshot.get('sha256') != ref.get('sha256')
            or source.stat().st_size != snapshot.get('bytes')
            or sha256(source.read_bytes()).hexdigest() != ref['sha256']):
        raise ValueError('edited_document_snapshot_changed')
    root_ref, edits, observed, coverage = _derive(ref, base)
    kind = ('document_structure' if coverage['scope'] == 'verified_edited_document_structure'
            else 'document')
    if (root_ref != provenance['root'] or edits != provenance['edits']
            or snapshot.get('kind') != kind or snapshot.get('source_id') != 'selected'
            or snapshot.get('display_name') != Path(ref['artifact']).name
            or snapshot.get('observed') != observed or snapshot.get('coverage') != coverage):
        raise ValueError('edited_document_snapshot_changed')
    return kind, observed, coverage
