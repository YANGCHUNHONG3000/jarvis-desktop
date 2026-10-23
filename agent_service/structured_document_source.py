"""Bounded text and table topology for receipt-verified complex Word edits.

This is an audit projection. It preserves every main-body paragraph, table and
physical cell in reading order, including merge continuations and empty text.
It does not claim to reproduce Word layout or styling.
"""

import json
from pathlib import Path

from . import office_edit, office_source


_W = office_source._W
_TAG = lambda name: f'{{{_W}}}{name}'
_ALLOWED = { _TAG(name) for name in (
    'body', 'p', 'pPr', 'r', 'rPr', 't', 'tab', 'br', 'cr', 'noBreakHyphen',
    'softHyphen', 'bookmarkStart', 'bookmarkEnd', 'proofErr', 'tbl', 'tblPr',
    'tblPrEx', 'tblGrid', 'gridCol', 'tr', 'trPr', 'tc', 'tcPr', 'gridSpan',
    'vMerge', 'sectPr', 'pStyle', 'rStyle', 'b', 'i', 'u', 'color', 'rFonts',
    'sz', 'szCs', 'lang', 'spacing', 'jc', 'ind', 'keepNext', 'keepLines',
    'widowControl', 'contextualSpacing', 'numPr', 'ilvl', 'numId', 'shd',
    'tblStyle', 'tblW', 'tblInd', 'tblLayout', 'tcW', 'tblBorders',
    'top', 'bottom', 'left', 'right', 'insideH', 'insideV', 'tblCellMar',
    'pgSz', 'pgMar', 'cols', 'docGrid')}
_TEXT_NODES = {_TAG(name) for name in ('t', 'tab', 'br', 'cr', 'noBreakHyphen', 'softHyphen')}
_LABELS = {'paragraph': '段落', 'table': '表格', 'cell': '单元格'}
_HEADERS = ['位置', '类型', '结构', '原文']
_MAX_RECORDS = 180
_MAX_BYTES = 32_000


def _known_content(body):
    for node in body.iter():
        if node.tag not in _ALLOWED:
            raise ValueError('structured_document_content_unsupported')
        parent = node.getparent()
        if node.tag in _TEXT_NODES and (parent is None or parent.tag != _TAG('r')):
            raise ValueError('structured_document_content_unsupported')
        if node.tag == _TAG('r') and (parent is None or parent.tag != _TAG('p')):
            raise ValueError('structured_document_content_unsupported')
        if node.tag == _TAG('p') and (parent is None or parent.tag not in {_TAG('body'), _TAG('tc')}):
            raise ValueError('structured_document_content_unsupported')
        if node.tag == _TAG('pPr') and (parent is None or parent.tag != _TAG('p')):
            raise ValueError('structured_document_content_unsupported')
        if node.tag == _TAG('rPr') and (parent is None or parent.tag not in {_TAG('pPr'), _TAG('r')}):
            raise ValueError('structured_document_content_unsupported')
        # Raw text outside w:t must not disappear in the projection. Property
        # attributes affect layout only; layout_preserved is explicitly false.
        if node.tag != _TAG('t') and (node.text or '').strip():
            raise ValueError('structured_document_content_unsupported')
        if (node.tail or '').strip():
            raise ValueError('structured_document_content_unsupported')


def audit_table(observed):
    """One auditable row per source record, with no truncation or row selection."""
    if type(observed) is not dict or set(observed) != {'records'}:
        raise ValueError('structured_document_observation_invalid')
    records = observed['records']
    if type(records) is not list or not 1 <= len(records) <= _MAX_RECORDS:
        raise ValueError('structured_document_observation_invalid')
    rows = []
    for item in records:
        if (type(item) is not dict or set(item) != {'position', 'type', 'structure', 'text'}
                or item['type'] not in _LABELS or type(item['position']) is not str
                or type(item['structure']) is not dict or type(item['text']) is not str):
            raise ValueError('structured_document_observation_invalid')
        structure = json.dumps(item['structure'], ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'))
        rows.append([item['position'], _LABELS[item['type']], structure, item['text']])
    table = {'headers': _HEADERS[:], 'rows': rows}
    office_source._writer_compatible('structure.xlsx', 'table', table)
    return table


def parse(path):
    """Parse only the bounded layout already accepted by office_edit."""
    path = Path(path)
    parts = office_edit._parts(path)
    body = office_edit._word_body(parts)
    ordered_layouts = office_edit._word_layout(body)
    layouts = {item['element']: (index, item) for index, item in enumerate(ordered_layouts)}
    _known_content(body)
    records = []
    counts = {'tables': 0, 'cells': 0, 'paragraphs': 0}

    def add(position, kind, structure, value=''):
        if len(records) >= _MAX_RECORDS or len(value) > 2000:
            raise ValueError('structured_document_limit')
        records.append({'position': position, 'type': kind,
                        'structure': structure, 'text': value})
        counts[{'table': 'tables', 'cell': 'cells', 'paragraph': 'paragraphs'}[kind]] += 1

    def blocks(parent, prefix, depth):
        index = 0
        paragraph_index = 0
        for child in parent:
            if child.tag not in {_TAG('p'), _TAG('tbl')}:
                continue
            position = f'{prefix}/{index}' if prefix == 'body' else f'{prefix}/block/{index}'
            index += 1
            if child.tag == _TAG('p'):
                add(position, 'paragraph',
                    {'parent': 'body' if prefix == 'body' else 'cell',
                     'paragraph_index': paragraph_index},
                    office_source._word_text(child))
                paragraph_index += 1
                continue
            table_index, layout = layouts[child]
            add(position, 'table', {'width': layout['width'],
                                    'rows': len(layout['rows']), 'depth': depth,
                                    'table_index': table_index})
            for row_index, row in enumerate(layout['rows']):
                for cell_index, descriptor in enumerate(row):
                    cell_position = f'{position}/row/{row_index}/cell/{cell_index}'
                    add(cell_position, 'cell', {'row': row_index, 'cell': cell_index,
                        'column': descriptor['column'], 'span': descriptor['span'],
                        'merge': descriptor['merge'] or 'none'})
                    blocks(descriptor['element'], cell_position, depth + 1)

    blocks(body, 'body', 1)
    observed = {'records': records}
    if not records or len(json.dumps(observed, ensure_ascii=False,
                                     separators=(',', ':'))) > _MAX_BYTES:
        raise ValueError('structured_document_limit')
    audit_table(observed)
    return observed, {'scope': 'verified_edited_document_structure',
                      'records': len(records), **counts, 'layout_preserved': False}
