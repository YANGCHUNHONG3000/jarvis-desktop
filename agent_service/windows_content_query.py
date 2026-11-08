"""Literal content-query authorization and deterministic, read-only matching.

Native callers must independently attest the returned range/cell before any
selection. Excel occurrences are matching cells, in visible-sheet row order;
Word occurrences are non-overlapping literal spans in the main story.
"""
import json
import math
from pathlib import Path
import re

from .computer import ComputerError
from . import windows_file_dialog as files


def _query(query, occurrence, scope):
    if (type(query) is not str or not 0 < len(query) <= 300
            or not all(char.isprintable() for char in query)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in query)
            or type(occurrence) is not int or not 1 <= occurrence <= 100
            or type(scope) is not str or scope not in {'text', 'formula'}):
        raise ComputerError('windows_content_query_invalid')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def encode(file, query, occurrence=1, scope='text'):
    """Bind a literal query to current authorized bytes, never refresh a snapshot."""
    _query(query, occurrence, scope)
    snapshot = files.decode_token(files.encode_token(file))
    if Path(snapshot['path']).suffix.casefold() == '.docx' and scope != 'text':
        raise ComputerError('windows_content_query_invalid')
    return _json({'schema': 1, 'file': snapshot, 'query': query,
                  'occurrence': occurrence, 'scope': scope})


def decode(token):
    """Validate strict schema and the original file bytes on every use."""
    if type(token) is not str or not 0 < len(token) <= 8000:
        raise ComputerError('windows_content_query_invalid')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        record = json.loads(token, object_pairs_hook=unique)
        if (type(record) is not dict
                or set(record) != {'schema', 'file', 'query', 'occurrence', 'scope'}
                or type(record['schema']) is not int or record['schema'] != 1):
            raise ValueError()
        _query(record['query'], record['occurrence'], record['scope'])
        snapshot = files.decode_token(_json(record['file']))
        if Path(snapshot['path']).suffix.casefold() == '.docx' and record['scope'] != 'text':
            raise ValueError()
        return dict(record, file=snapshot)
    except ComputerError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        raise ComputerError('windows_content_query_invalid') from exc


encode_token = encode
decode_token = decode


def _utf16_length(value):
    try:
        return len(value.encode('utf-16-le')) // 2
    except UnicodeError as exc:
        raise ComputerError('windows_content_span_invalid') from exc


def word_match(text, start, end, query, occurrence=1):
    """Return ((native UTF-16 span with literal text), complete match count)."""
    _query(query, occurrence, 'text')
    if (type(text) is not str or len(text) > 100_000
            or type(start) is not int or type(end) is not int
            or start < 0 or end < start or end - start != _utf16_length(text)):
        raise ComputerError('windows_content_span_invalid')
    count, offset, selected = 0, 0, None
    while True:
        offset = text.find(query, offset)
        if offset < 0:
            break
        count += 1
        if count == occurrence:
            position = start + _utf16_length(text[:offset])
            selected = {'start': position, 'end': position + _utf16_length(query), 'text': query}
        offset += len(query)
    if selected is None:
        raise ComputerError('windows_content_match_missing')
    return selected, count


def scalar_text(value):
    """Literal Value2 representation, independent of display formatting/locale."""
    if value is None:
        return None
    if type(value) is str:
        return value
    if type(value) is bool:
        return 'TRUE' if value else 'FALSE'
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return str(int(value)) if value.is_integer() else _json(value)
    raise ComputerError('windows_content_model_invalid')


def excel_match(model, query, occurrence=1, scope='text'):
    """Find a matching visible cell; formula caches never supply text evidence.

    ``model`` comes from windows_workbook_model.disk_model/capture. Validate the
    consumed structure as well so malformed/off-boundary cells cannot select.
    """
    _query(query, occurrence, scope)
    if (type(model) is not dict or type(model.get('sheets')) is not list
            or not 1 <= len(model['sheets']) <= 8):
        raise ComputerError('windows_content_model_invalid')
    count, selected, names = 0, None, set()
    for sheet in model['sheets']:
        if (type(sheet) is not dict or type(sheet.get('name')) is not str
                or not sheet['name'] or sheet['name'].casefold() in names
                or type(sheet.get('state')) is not str
                or sheet['state'] not in {'visible', 'hidden', 'veryHidden'}
                or type(sheet.get('values')) is not list
                or not 1 <= len(sheet['values']) <= 201
                or type(sheet.get('formulas')) is not list):
            raise ComputerError('windows_content_model_invalid')
        names.add(sheet['name'].casefold())
        values = sheet['values']
        if type(values[0]) is not list or not 1 <= len(values[0]) <= 12:
            raise ComputerError('windows_content_model_invalid')
        columns = len(values[0])
        if (any(type(row) is not list or len(row) != columns for row in values)
                or sheet.get('range') != f'A1:{chr(64 + columns)}{len(values)}'):
            raise ComputerError('windows_content_model_invalid')
        formulas = {}
        for entry in sheet['formulas']:
            if (type(entry) is not dict or type(entry.get('cell')) is not str
                    or re.fullmatch(r'[A-L][1-9][0-9]{0,2}', entry['cell']) is None
                    or entry['cell'] in formulas
                    or type(entry.get('expression')) is not str
                    or not entry['expression'].startswith('=')
                    or len(entry['expression']) > 3000):
                raise ComputerError('windows_content_model_invalid')
            column, row = ord(entry['cell'][0]) - 64, int(entry['cell'][1:])
            if row > len(values) or column > columns:
                raise ComputerError('windows_content_model_invalid')
            formulas[entry['cell']] = entry['expression']
        for row_index, row in enumerate(values, 1):
            for column, value in enumerate(row, 1):
                address = f'{chr(64 + column)}{row_index}'
                literal = scalar_text(value)
                text = formulas.get(address) if scope == 'formula' else (None if address in formulas else literal)
                if sheet['state'] != 'visible' or text is None or query not in text:
                    continue
                count += 1
                if count == occurrence:
                    selected = {'sheet': sheet['name'], 'address': address,
                                'text': text, 'field': 'formula' if scope == 'formula' else 'value'}
    if selected is None:
        raise ComputerError('windows_content_match_missing')
    return selected, count
