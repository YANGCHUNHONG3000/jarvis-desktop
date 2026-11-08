"""Literal content locations verified against the actual bound Office selection.

Observation only reads existing COM objects. Only select_match changes UI state,
and its caller must already have persisted a native intent and input guards.
"""
from pathlib import Path
import json

from .computer import ComputerError, digest
from . import windows_file_dialog as files
from . import windows_content_query as query

SELECTOR = {'type': 'Document', 'name': '已定位内容命中', 'automation_id': 'jarvis.content_match'}
ACTION = 'select_content_match'


def is_goal(selector):
    return selector == SELECTOR


def _invoke(pointer, name, *args, method=False):
    try:
        member = pointer.GetIDsOfNames(0, name)
        return pointer.Invoke(member, 0, 1 if method else 2, 1, *args)
    except Exception as exc:
        error = ComputerError('windows_content_native_unavailable')
        error.content_diagnostic = {'property': name, 'method': method}
        raise error from exc


def _get(pointer, name, *args):
    return _invoke(pointer, name, *args)


def _path(pointer):
    return files._local_path(_get(pointer, 'FullName'))


def _word_location(document, request, check):
    pointer = document.pointer
    content = _get(pointer, 'Content')
    check()
    text, start, end = (_get(content, key) for key in ('Text', 'Start', 'End'))
    # WPS emits CR+end-of-cell in Text for one native range position. Only
    # accept that exact representation when it explains the complete span;
    # the candidate native Range must still independently equal the literal.
    offset_text = text
    if (type(text) is str and len(text) <= 100_000 and type(start) is int and type(end) is int
            and query._utf16_length(text) != end-start and '\r\x07' in text):
        collapsed = text.replace('\r\x07', '\x07')
        if query._utf16_length(collapsed) == end-start:
            offset_text = collapsed
    try:
        hit, total = query.word_match(offset_text, start, end, request['query'], request['occurrence'])
    except ComputerError as exc:
        if str(exc) == 'windows_content_span_invalid':
            exc.content_diagnostic = {'stage': 'body_span', 'start': start, 'end': end,
                'text_characters': len(text) if type(text) is str else None,
                'utf16_units': query._utf16_length(text)
                    if type(text) is str and len(text) <= 100_000 else None}
        raise
    if offset_text != text:
        # Full-span agreement alone does not prove which repeated literal a
        # mixed marker representation addresses. Attest the complete native
        # prefix before accepting the compatibility mapping or any selection.
        prefix = _invoke(pointer, 'Range', start, hit['start'], method=True)
        prefix_text = _get(prefix, 'Text')
        expected_prefix = offset_text.encode('utf-16-le')[:2 * (hit['start'] - start)].decode('utf-16-le')
        if (type(prefix_text) is not str or len(prefix_text) > 100_000
                or _get(prefix, 'Start') != start or _get(prefix, 'End') != hit['start']
                or _get(prefix, 'StoryType') != 1
                or _path(_get(prefix, 'Document')) != Path(request['file']['path'])
                or prefix_text.replace('\r\x07', '\x07') != expected_prefix):
            error = ComputerError('windows_content_span_invalid')
            error.content_diagnostic = {'stage': 'prefix_range', 'expected_start': start,
                                        'expected_end': hit['start']}
            raise error
        check()
    found = _invoke(pointer, 'Range', hit['start'], hit['end'], method=True)
    native = {key.lower(): _get(found, key) for key in ('Start', 'End', 'Text')}
    if native != hit or _get(found, 'StoryType') != 1:
        error = ComputerError('windows_content_span_invalid')
        error.content_diagnostic = {'stage': 'found_range', 'expected_start':hit['start'],
            'expected_end':hit['end'],'native_start':native['start'],'native_end':native['end'],
            'text_matches':native['text']==hit['text']}
        raise error
    if _path(_get(found, 'Document')) != Path(request['file']['path']):
        raise ComputerError('windows_content_selection_wrong_file')
    app = _get(pointer, 'Application')
    selection = _get(app, 'Selection')
    actual = {key.lower(): _get(selection, key) for key in ('Start', 'End', 'Text')}
    selected = (_path(_get(selection, 'Document')) == Path(request['file']['path'])
                and _get(selection, 'StoryType') == 1 and actual == hit)
    check()
    return {'hit': hit, 'total': total, 'content_sha256': digest({'text': text, 'start': start, 'end': end}),
            'selected': selected}, (found, None)


def _cell_location(cell):
    sheet = _get(cell, 'Worksheet')
    return {'path': str(_path(_get(sheet, 'Parent'))), 'sheet': _get(sheet, 'Name'),
            'address': _get(cell, 'Address'), 'count': _get(cell, 'Count'),
            'areas': _get(_get(cell, 'Areas'), 'Count')}


def _excel_location(document, request, check):
    from . import windows_workbook_model as model
    pointer = document.pointer
    raw, _ = model.capture(pointer, request['file']['path'], check)
    actual_model = json.loads(raw)
    hit, total = query.excel_match(actual_model, request['query'], request['occurrence'], request['scope'])
    sheets = _get(pointer, 'Worksheets')
    sheet = _get(sheets, 'Item', hit['sheet'])
    if _get(sheet, 'Visible') != -1 or _path(_get(sheet, 'Parent')) != Path(request['file']['path']):
        raise ComputerError('windows_content_sheet_invalid')
    found = _get(sheet, 'Range', hit['address'])
    has_formula = _get(found, 'HasFormula')
    value = _get(found, 'Formula' if request['scope'] == 'formula' else 'Value2')
    if (type(has_formula) is not bool or has_formula != (request['scope'] == 'formula')
            or query.scalar_text(value) != hit['text']):
        raise ComputerError('windows_content_changed')
    app = _get(pointer, 'Application')
    selection, active = _get(app, 'Selection'), _get(app, 'ActiveCell')
    actual_selection, actual_active = _cell_location(selection), _cell_location(active)
    expected = {'path': request['file']['path'], 'sheet': hit['sheet'],
                'address': '$' + ''.join(c for c in hit['address'] if c.isalpha()) + '$' +
                ''.join(c for c in hit['address'] if c.isdigit()), 'count': 1, 'areas': 1}
    selected = (actual_selection == expected and actual_active == expected
                and _get(_get(app, 'ActiveSheet'), 'Name') == hit['sheet'])
    if selected:
        for cell in (selection, active):
            selected = (selected and _get(cell, 'HasFormula') is has_formula and
                        query.scalar_text(_get(cell, 'Formula' if has_formula else 'Value2')) == hit['text'])
    check()
    return {'hit': hit, 'total': total, 'content_sha256': digest(actual_model),
            'selected': selected}, (found, sheet)


def _once(window, token, check):
    request = query.decode(token)
    document, flags = files.bound_document(window, request['file']['path'], check)
    if flags['saved'] is not True:
        raise ComputerError('windows_document_unsaved_changes')
    if Path(request['file']['path']).suffix.casefold() == '.docx':
        result, native = _word_location(document, request, check)
    else:
        result, native = _excel_location(document, request, check)
    # Pin this exact object again, not another ROT object of the same basename.
    from .windows_document_copy import _same_document
    facts = _same_document(document, window, Path(request['file']['path']), check)
    if facts[2][1] is not True:
        raise ComputerError('windows_document_unsaved_changes')
    query.decode(token)
    check()
    return result, native


def inspect(window, token, check):
    first, _ = _once(window, token, check)
    second, _ = _once(window, token, check)
    if first != second:
        raise ComputerError('windows_content_changed')
    return second


def observe(window, token, check, native_observe):
    request = query.decode(token)
    from .windows_access import _file_application
    if not _file_application(window, request['file']['path']):
        raise ComputerError('windows_file_application_invalid')
    result, wrappers = native_observe()
    if result['application'] != 'wps':
        raise ComputerError('windows_file_application_invalid')
    state = None
    if not result.get('dialogs') and not result.get('blocked_reason'):
        state = inspect(window, token, check)
    result['content_token'], result['content_match'] = token, state
    result['controls'].append(dict(SELECTOR, id='jarvis-content-match', runtime_id=[],
        enabled=True, visible=True, focused=False, selected=None, expanded=None, patterns=[],
        value=token if state and state['selected'] else ''))
    if len(result['controls']) > 200:
        result['truncated'] = True
    return result, wrappers


def allowed(observation, control, action, token):
    state = observation.get('content_match')
    return (control is None and action == ACTION and observation.get('application') == 'wps'
            and observation.get('target_selector') == SELECTOR
            and not observation.get('dialogs') and not observation.get('blocked_reason')
            and not observation.get('truncated') and token == observation.get('content_token')
            and type(state) is dict and state.get('selected') is False)


def candidates(observation, condition):
    return ([{'control_id': None, 'action': ACTION, 'value': condition['value']}]
            if condition['state'] == 'value' and allowed(observation, None, ACTION, condition['value']) else [])


def preflight(window, token, expected, check):
    current = inspect(window, token, check)
    if current != expected:
        raise ComputerError('screen_changed_reobserve')


def select_match(window, token, expected, check):
    current, (found, sheet) = _once(window, token, check)
    if current != expected:
        raise ComputerError('screen_changed_reobserve')
    check()
    if sheet is not None:
        _invoke(sheet, 'Activate', method=True)
        check()
        request = query.decode(token)
        document, flags = files.bound_document(window, request['file']['path'], check)
        if flags['saved'] is not True:
            raise ComputerError('windows_document_unsaved_changes')
        app = _get(document.pointer, 'Application')
        if _get(_get(app, 'ActiveSheet'), 'Name') != expected['hit']['sheet']:
            raise ComputerError('windows_content_selection_wrong_sheet')
    _invoke(found, 'Select', method=True)
    check()


def verified(after, token):
    state = after.get('content_match')
    return (not after.get('blocked_reason') and not after.get('dialogs') and not after.get('truncated')
            and after.get('content_token') == token and type(state) is dict and state.get('selected') is True)
