"""Read-only bridge from verified native literal locations to copied Office edits.

No observation, selection, execution or source refresh occurs in this module.
The returned change still needs the ordinary Office preview and adapter gates.
"""
from copy import deepcopy
from pathlib import Path
import re

from .computer import ComputerError, digest, safe_root
from . import windows_task as wt
from . import windows_content_find as find
from . import windows_content_query as query

_FIELDS = {'schema', 'parent_root', 'task_sha256', 'evidence_sha256',
           'condition_id', 'event', 'event_sha256', 'token', 'match'}
_SHA = re.compile(r'[0-9a-f]{64}')


def _invalid(code='reference_invalid'):
    raise ComputerError('windows_located_edit_' + code)


def _state(value):
    if (type(value) is not dict or set(value) != {'hit', 'total', 'content_sha256', 'selected'}
            or value['selected'] is not True or type(value['total']) is not int
            or not 1 <= value['total'] <= 100_000 or type(value['hit']) is not dict
            or type(value['content_sha256']) is not str or not _SHA.fullmatch(value['content_sha256'])):
        _invalid('match_invalid')
    hit = value['hit']
    word = (set(hit) == {'start', 'end', 'text'} and type(hit['start']) is int
            and type(hit['end']) is int and 0 <= hit['start'] < hit['end']
            and type(hit['text']) is str and query._utf16_length(hit['text']) == hit['end']-hit['start'])
    cell = (set(hit) == {'sheet', 'address', 'text', 'field'} and type(hit['sheet']) is str
            and bool(hit['sheet']) and type(hit['address']) is str
            and re.fullmatch(r'[A-L][1-9][0-9]{0,2}', hit['address']) is not None
            and type(hit['text']) is str and type(hit['field']) is str
            and hit['field'] in {'value', 'formula'})
    if not (word or cell):
        _invalid('match_invalid')
    return deepcopy(value)


def capture(parent_root, condition_id):
    """Pin one complete native match from a wholly verified immutable task."""
    from .windows_handoff import _local_absolute
    if (not isinstance(parent_root, (str, Path)) or not _local_absolute(str(parent_root))
            or type(condition_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', condition_id)):
        _invalid()
    root = safe_root(parent_root)
    try:
        with wt._own(root):
            scan = wt._scan(root)
            wt._verify_sources(scan)
            if scan['uncertain'] or (scan['last_outcome'] or {}).get('state') == 'unknown':
                _invalid('source_unknown')
            if ((scan['last_outcome'] or {}).get('state') != 'verified'
                    or len(scan['completed']) != len(scan['goal']['conditions'])):
                _invalid('source_incomplete')
            condition = next((c for c in scan['goal']['conditions'] if c['id'] == condition_id), None)
            if (condition is None or condition['selector'] != find.SELECTOR or condition['state'] != 'value'
                    or condition_id not in scan['completed']):
                _invalid('condition_invalid')
            token = condition['value']
            query.decode(token)  # Revalidate the original byte fingerprint, never renew it.
            events = []
            for path in sorted(root.glob('attempts/*/events/*.json')):
                event, event_sha = wt._load(path)
                if event.get('kind') == 'complete' and event.get('condition_id') == condition_id:
                    events.append((path, event, event_sha))
            if len(events) != 1:
                _invalid('completion_invalid')
            path, event, event_sha = events[0]
            observed = event['observation']
            if (observed.get('application') != 'wps' or observed.get('target_selector') != find.SELECTOR
                    or observed.get('content_token') != token or observed.get('dialogs')
                    or observed.get('blocked_reason') or observed.get('truncated')
                    or not find.verified(observed, token) or not wt.satisfied(observed, condition)):
                _invalid('match_invalid')
            match = _state(observed['content_match'])
            return {'schema': 1, 'parent_root': str(root), 'task_sha256': scan['task_sha256'],
                    'evidence_sha256': wt._inventory(root), 'condition_id': condition_id,
                    'event': path.relative_to(root).as_posix(), 'event_sha256': event_sha,
                    'token': token, 'match': match}
    except ComputerError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ComputerError('windows_located_edit_source_invalid') from exc


def verify(ref):
    if (type(ref) is not dict or set(ref) != _FIELDS or type(ref['schema']) is not int
            or ref['schema'] != 1 or any(type(ref[k]) is not str or not _SHA.fullmatch(ref[k])
                for k in ('task_sha256', 'evidence_sha256', 'event_sha256'))):
        _invalid()
    _state(ref['match'])
    rebuilt = capture(ref['parent_root'], ref['condition_id'])
    if rebuilt != ref:
        _invalid('source_changed')
    return rebuilt


def _word_change(path, request, match, after):
    from . import office_edit as edit
    observed = edit._observe(path, 'document', 2)
    body = edit._word_body(edit._parts(path))
    raw = edit._word_native_stream(body)
    # The complete recorded body hash pins BOTH marker representation and span.
    # Only WPS's exact CR+cell collapse is permitted; no guessed text offsets.
    representations = []
    for text in (raw, raw.replace('\r\x07', '\x07')):
        end = query._utf16_length(text)
        if digest({'text': raw, 'start': 0, 'end': end}) == match['content_sha256']:
            hit, total = query.word_match(text, 0, end, request['query'], request['occurrence'])
            if hit == match['hit'] and total == match['total']:
                representations.append(text)
    representations = list(dict.fromkeys(representations))
    if len(representations) != 1:
        _invalid('body_mismatch')
    text = representations[0]
    collapsed = text != raw
    coordinates = {p: {'paragraph_index': i} for i, p in enumerate(body.findall(f'{{{edit.W}}}p'))}
    for ti, table in enumerate(edit._word_layout(body)):
        for ri, row in enumerate(table['rows']):
            for cell in row:
                if cell['merge'] != 'continue':
                    for pi, paragraph in enumerate(cell['element'].findall(f'{{{edit.W}}}p')):
                        coordinates[paragraph] = dict(zip(edit._WORD_COORDINATES,
                            (ti, ri, cell['column'], pi)))
    offset, targets = 0, []

    def append(value):
        nonlocal offset
        offset += query._utf16_length(value.replace('\r\x07', '\x07') if collapsed else value)

    def visit(parent, in_cell=False):
        blocks = [c for c in parent if c.tag in {f'{{{edit.W}}}p', f'{{{edit.W}}}tbl'}]
        for index, child in enumerate(blocks):
            if child.tag == f'{{{edit.W}}}p':
                value = edit.office_source._word_text(child)
                start, end = offset, offset + query._utf16_length(value)
                hit = match['hit']
                if start <= hit['start'] < hit['end'] <= end and child in coordinates:
                    targets.append((child, value, hit['start'] - start, hit['end'] - start))
                append(value + ('\r\x07' if in_cell and index == len(blocks)-1 else '\r'))
            else:
                for row in child.findall(f'{{{edit.W}}}tr'):
                    for cell in row.findall(f'{{{edit.W}}}tc'):
                        visit(cell, True)
                    append('\r\x07')
    visit(body)
    if offset != query._utf16_length(text) or len(targets) != 1:
        _invalid('range_unsupported')
    paragraph, before, start, end = targets[0]
    item = edit._word_observed_item(observed, coordinates[paragraph])
    if item is None or item['editable'] is not True or item['text'] != before:
        _invalid('range_unsupported')
    encoded = before.encode('utf-16-le')
    if encoded[2*start:2*end].decode('utf-16-le') != request['query']:
        _invalid('body_mismatch')
    replacement = encoded[:2*start].decode('utf-16-le') + after + encoded[2*end:].decode('utf-16-le')
    edit._word_edit_target(paragraph, before, replacement)
    return {**coordinates[paragraph], 'before': before, 'after': replacement}


def _excel_change(path, request, match, after):
    from . import office_edit as edit, windows_workbook_model as model
    if request['scope'] != 'text' or match['hit'].get('field') == 'formula':
        _invalid('formula_forbidden')
    actual = model.disk_model(path)
    hit, total = query.excel_match(actual, request['query'], request['occurrence'], request['scope'])
    # capture() already compared the entire native model with these fixed source
    # bytes. The old receipt hashes its RAW Value2/formula spelling, whereas
    # disk_model() normalizes integral floats and native formula identities.
    # The raw hash stays bound by verify(ref), but cannot be reconstructed here.
    # Re-derive the exact cell/field/literal/count from those same source bytes;
    # never equate an arbitrary same-text cell or refresh the source fingerprint.
    if hit != match['hit'] or total != match['total']:
        _invalid('body_mismatch')
    if hit['field'] != 'value':
        _invalid('formula_forbidden')
    observed = edit._observe(path, 'spreadsheet')
    item = next((c for s in observed['sheets'] if s['name'] == hit['sheet']
                 for c in s['cells'] if c['cell'] == hit['address']), None)
    if item is None or item['editable'] is not True or query.scalar_text(item['value']) != hit['text']:
        _invalid('range_unsupported')
    return {'sheet': hit['sheet'], 'cell': hit['address'], 'before': item['value'], 'after': after}


def derive(ref, after):
    """Derive one exact Office change; do not create snapshots or run files."""
    from . import office_edit as edit
    checked = verify(ref)
    request = query.decode(checked['token'])
    path = request['file']['path']
    if Path(path).suffix.casefold() == '.docx':
        if type(after) is not str:
            _invalid('replacement_invalid')
        change = _word_change(path, request, checked['match'], after)
        kind = 'document'
    else:
        change = _excel_change(path, request, checked['match'], after)
        kind = 'spreadsheet'
    edit.validate_edit_spec({'kind': kind, 'source_id': 'selected-edit',
                            'source_sha256': request['file']['sha256'], 'changes': [change]})
    # Guard the passive reads above against replacement while deriving.
    verify(checked)
    return {'path': path, 'changes': [change]}


def choices(parent_root):
    """List completed matches, retaining an explicit refusal for formula cells."""
    root = safe_root(parent_root)
    with wt._own(root):
        scan = wt._scan(root)
        wt._verify_sources(scan)
        identifiers = [c['id'] for c in scan['goal']['conditions'] if c['selector'] == find.SELECTOR]
    result = []
    for identifier in identifiers:
        ref = capture(root, identifier)
        request = query.decode(ref['token'])
        formula = request['scope'] == 'formula' or ref['match']['hit'].get('field') == 'formula'
        result.append({'condition_id': identifier, 'ref': ref, 'editable': not formula,
                       'reason': 'windows_located_edit_formula_forbidden' if formula else ''})
    return result


def _word_interval(ref, change):
    """Map a proven native range to original paragraph UTF-16 coordinates."""
    from . import office_edit as edit
    request = query.decode(ref['token'])
    body = edit._word_body(edit._parts(request['file']['path']))
    # Resolve target and traverse the same parsed tree for element identity.
    tree = body.getroottree().getroot()
    paragraph = edit._word_paragraph(tree, change)
    raw = edit._word_native_stream(body)
    representations = {text for text in (raw, raw.replace('\r\x07', '\x07'))
        if digest({'text': raw, 'start': 0, 'end': query._utf16_length(text)}) == ref['match']['content_sha256']}
    if len(representations) != 1:
        _invalid('body_mismatch')
    collapsed = representations.pop() != raw
    pieces, starts = [], []

    def visit(parent, in_cell=False):
        blocks = [c for c in parent if c.tag in {f'{{{edit.W}}}p', f'{{{edit.W}}}tbl'}]
        for index, child in enumerate(blocks):
            if child.tag == f'{{{edit.W}}}p':
                if child is paragraph:
                    prefix = ''.join(pieces)
                    starts.append(query._utf16_length(prefix.replace('\r\x07', '\x07') if collapsed else prefix))
                pieces.append(edit.office_source._word_text(child) +
                    ('\r\x07' if in_cell and index == len(blocks)-1 else '\r'))
            else:
                for row in child.findall(f'{{{edit.W}}}tr'):
                    for cell in row.findall(f'{{{edit.W}}}tc'):
                        visit(cell, True)
                    pieces.append('\r\x07')
    visit(body)
    if len(starts) != 1 or ''.join(pieces) != raw:
        _invalid('range_unsupported')
    hit = ref['match']['hit']
    start, end = hit['start'] - starts[0], hit['end'] - starts[0]
    before = change['before']
    if (not 0 <= start < end <= query._utf16_length(before)
            or before.encode('utf-16-le')[2*start:2*end].decode('utf-16-le') != request['query']):
        _invalid('body_mismatch')
    return start, end, paragraph


def derive_many(selections):
    """Group 1..20 pinned locations into at most four copied Office edits.

    Original ranges are never shifted by earlier replacements. Multiple Word
    locations in one paragraph still must produce one unambiguous w:t edit.
    """
    from . import office_edit as edit
    if (type(selections) is not list or not 1 <= len(selections) <= 20
            or any(type(s) is not dict or set(s) != {'ref', 'after'} for s in selections)):
        _invalid('selections_invalid')
    selections = deepcopy(selections)
    grouped = {}
    for selection in selections:
        derived = derive(selection['ref'], selection['after'])
        request = query.decode(selection['ref']['token'])
        path = str(safe_root(derived['path']))
        key = path.casefold()
        if key not in grouped:
            if len(grouped) >= 4:
                _invalid('file_limit')
            grouped[key] = {'path': path, 'sha256': request['file']['sha256'],
                            'changes': {}, 'locations': []}
        group = grouped[key]
        if group['sha256'] != request['file']['sha256']:
            _invalid('source_mismatch')
        group['locations'].append(selection)
        change = derived['changes'][0]
        if Path(path).suffix.casefold() == '.xlsx':
            target = (change['sheet'], change['cell'])
            if target in group['changes']:
                _invalid('duplicate_range')
            group['changes'][target] = {'change': change}
        else:
            names = edit._WORD_COORDINATES if 'table_index' in change else ('paragraph_index',)
            target = tuple((name, change[name]) for name in names)
            start, end, paragraph = _word_interval(selection['ref'], change)
            record = group['changes'].setdefault(target, {'change': change, 'ranges': [], 'paragraph': paragraph})
            if record['change']['before'] != change['before']:
                _invalid('source_mismatch')
            if any(start < old_end and old_start < end for old_start, old_end, _ in record['ranges']):
                _invalid('overlapping_ranges')
            record['ranges'].append((start, end, selection['after']))
    result = []
    for group in grouped.values():
        changes = []
        for record in group['changes'].values():
            change = deepcopy(record['change'])
            if 'ranges' in record:
                encoded = change['before'].encode('utf-16-le')
                for start, end, after in sorted(record['ranges'], reverse=True):
                    encoded = encoded[:2*start] + after.encode('utf-16-le') + encoded[2*end:]
                change['after'] = encoded.decode('utf-16-le')
                edit._word_edit_target(record['paragraph'], change['before'], change['after'])
            changes.append(change)
        kind = 'document' if Path(group['path']).suffix.casefold() == '.docx' else 'spreadsheet'
        edit.validate_edit_spec({'kind': kind, 'source_id': 'selected-edit',
            'source_sha256': group['sha256'], 'changes': changes})
        result.append({'path': group['path'], 'changes': changes, 'locations': group['locations']})
    # A later passive read must not invalidate an earlier selected receipt.
    for selection in selections:
        verify(selection['ref'])
    return result
