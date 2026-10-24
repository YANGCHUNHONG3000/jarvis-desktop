"""Lossless table intake for receipt-verified edits of Jarvis-created workbooks.

The creation receipt defines the title/header/data/totals layout. The edit
receipt proves that only approved cells changed. This route never parses a
general merged workbook as a table.
"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from . import artifact_source, office_source
from .computer import safe_root
from .computer_agent_tools import _load
from .office import _equal_scalar
from .office_edit import _read, verify_edit_source


_CELL = re.compile(r'([A-Z]{1,2})([1-9][0-9]{0,3})\Z')
_WORKFLOW = re.compile(r'workflow-[0-9a-f]{32}\Z')


def _generated_path(path, base):
    """Recognize only the shape of a direct managed creation ancestor."""
    return (path.name == 'spreadsheet.xlsx'
            and artifact_source._STEP.fullmatch(path.parent.name) is not None
            and _WORKFLOW.fullmatch(path.parent.parent.name) is not None
            and artifact_source._TASK.fullmatch(path.parent.parent.parent.name) is not None
            and path.parent.parent.parent.parent == base)


def _apply_changes(observed, changes, sheet_name):
    table = deepcopy(observed)
    width, height = len(table['headers']), len(table['rows'])
    for change in changes:
        match = _CELL.fullmatch(change['cell'])
        if change['sheet'] != sheet_name or match is None:
            raise ValueError('edited_table_scope_invalid')
        column = 0
        for letter in match.group(1):
            column = column * 26 + ord(letter) - 64
        row = int(match.group(2))
        # The creation receipt fixes row 2 as headers and row 3+ as data.
        # Merged title and formula totals remain outside this projection.
        if not 1 <= column <= width or not 2 <= row <= height + 2:
            raise ValueError('edited_table_scope_invalid')
        cells = table['headers'] if row == 2 else table['rows'][row - 3]
        before = cells[column - 1]
        if not _equal_scalar(before, change['before']):
            raise ValueError('edited_table_before_mismatch')
        cells[column - 1] = change['after']
    return table


def _derive(ref, base):
    """Recompute a bounded edit chain from its independently checked creation."""
    from .edited_artifact_source import prepare_reference
    base = safe_root(base)
    edits, seen, current = [], set(), ref
    while True:
        artifact = safe_root(current['artifact'])
        if artifact in seen or len(edits) >= 4:
            raise ValueError('edited_table_chain_invalid')
        seen.add(artifact)
        if prepare_reference(artifact, current['sha256'], base) != current:
            raise ValueError('edited_artifact_ref_changed')
        manifest, _ = _load(artifact.parent / 'edit-manifest.json')
        if manifest['spec']['kind'] != 'spreadsheet':
            return None
        source_root = safe_root(manifest['source_root'])
        source = verify_edit_source(source_root)
        original = safe_root(_read(source_root / 'source.json')['original'])
        edits.append((current, manifest['spec'], source))
        if _generated_path(original, base):
            break
        if original.name != 'edited-spreadsheet.xlsx':
            return None
        current = prepare_reference(original, source['sha256'], base)
    origin_ref, snapshot, receipt, index = artifact_source._verified(
        original, source['sha256'], base)
    kind, observed, origin_coverage = artifact_source._extract(snapshot, receipt, index, original)
    if kind != 'table':
        raise ValueError('edited_table_origin_invalid')
    if origin_coverage.get('derived_totals'):
        raise ValueError('edited_table_totals_unsupported')
    table = observed
    total_changes = header_changes = 0
    for _, spec, source in reversed(edits):
        if len(source['observed']['sheets']) != 1:
            raise ValueError('edited_table_origin_invalid')
        sheet_name = source['observed']['sheets'][0]['name']
        table = _apply_changes(table, spec['changes'], sheet_name)
        total_changes += len(spec['changes'])
        header_changes += sum(int(_CELL.fullmatch(change['cell']).group(2)) == 2
                              for change in spec['changes'])
    if len(json.dumps(table, ensure_ascii=False, separators=(',', ':'))) > 32_000:
        raise office_source.SourceError('source_observation_oversize')
    office_source._writer_compatible(safe_root(ref['artifact']).name, 'table', table)
    coverage = {'scope': ('verified_generated_edited_workbook_data_rows' if len(edits) == 1
                          else 'verified_generated_multi_edit_workbook_data_rows'),
                'rows': len(table['rows']), 'columns': len(table['headers']),
                'title_row_excluded': True, 'totals_row_excluded': True,
                'edited_data_cells': total_changes - header_changes, 'edit_depth': len(edits)}
    if header_changes:
        coverage.update(scope='verified_generated_edited_workbook_headers_and_data',
                        edited_header_cells=header_changes)
    return origin_ref, [item[0] for item in reversed(edits)], table, coverage


def snapshot_if_generated(ref, root, base, *, stop=lambda: False, deadline=None):
    """Return a verified table snapshot, or None for a non-creation ancestor."""
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError('source_cancelled')
    derived = _derive(ref, base)
    if derived is None:
        return None
    origin_ref, edits, table, coverage = derived
    artifact = safe_root(ref['artifact'])

    sources = root / 'sources'
    sources.mkdir(exist_ok=False)
    copied, size = office_source._copy_stable(artifact, sources / 'source.xlsx')
    if copied != ref['sha256']:
        raise ValueError('edited_artifact_changed')
    # Recheck both full receipt chains after copying, including their bytes.
    if _derive(ref, base) != derived:
        raise ValueError('edited_table_origin_changed')
    if stop() or deadline is not None and time.monotonic() >= deadline:
        raise office_source.SourceError('source_cancelled')
    result = {'source_id': 'selected', 'kind': 'table', 'sha256': copied,
              'bytes': size, 'display_name': artifact.name,
              'coverage': coverage,
              'observed': table}
    office_source._write_json_new(sources / 'provenance.json',
                                  {'kind': 'verified_generated_edited_table',
                                   'edited': ref, 'edits': edits, 'origin': origin_ref})
    office_source._write_json_new(sources / 'source.json', result)
    return result


def verify_snapshot(task, snapshot, provenance, base):
    """Recheck copied bytes and both receipts before an enabled skill runs."""
    legacy = type(provenance) is dict and set(provenance) == {'kind', 'edited', 'origin'}
    if (type(provenance) is not dict
            or not (legacy or set(provenance) == {'kind', 'edited', 'edits', 'origin'})
            or provenance['kind'] != 'verified_generated_edited_table'):
        raise ValueError('edited_table_provenance_invalid')
    ref = provenance['edited']
    if (type(ref) is not dict or set(ref) != {
            'version', 'kind', 'artifact', 'sha256', 'manifest_sha256', 'receipt_sha256'}
            or type(snapshot) is not dict):
        raise ValueError('edited_table_provenance_invalid')
    source = task / 'sources' / 'source.xlsx'
    if (not source.is_file() or office_source._linked(source)
            or snapshot.get('sha256') != ref.get('sha256')
            or source.stat().st_size != snapshot.get('bytes')
            or sha256(source.read_bytes()).hexdigest() != ref['sha256']):
        raise ValueError('edited_table_snapshot_changed')
    derived = _derive(ref, base)
    if derived is None:
        raise ValueError('edited_table_origin_invalid')
    origin_ref, edits, observed, coverage = derived
    if legacy:
        if len(edits) != 1 or edits[0] != ref:
            raise ValueError('edited_table_provenance_invalid')
        coverage = {key: value for key, value in coverage.items() if key != 'edit_depth'}
    elif edits != provenance['edits']:
        raise ValueError('edited_table_provenance_invalid')
    if (origin_ref != provenance['origin']
            or snapshot.get('kind') != 'table'
            or snapshot.get('source_id') != 'selected'
            or snapshot.get('display_name') != Path(ref['artifact']).name
            or snapshot.get('observed') != observed or snapshot.get('coverage') != coverage):
        raise ValueError('edited_table_snapshot_changed')
    return 'table', observed, coverage
