"""Read-only, native active-document identities for visual window migration."""
from hashlib import sha256
import os
from pathlib import Path
import re
import stat

from .computer import ComputerError, read_record, safe_root
from . import windows_file_dialog as files


def _file(path):
    path = files._local_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= files.MAX_FILE:
        raise ComputerError('windows_visual_document_file_invalid')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if files._stamp(before) != files._stamp(opened):
            raise ComputerError('windows_visual_document_changed')
        raw = stream.read(files.MAX_FILE + 1)
        if (len(raw) != opened.st_size or files._stamp(os.fstat(stream.fileno())) != files._stamp(opened)
                or files._stamp(path.stat()) != files._stamp(opened)):
            raise ComputerError('windows_visual_document_changed')
    return {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'size': len(raw)}


def identity(document):
    """Stable document facts; observation-specific native proof is excluded."""
    return {key: value for key, value in document.items() if key != 'native_proof'}


def same_document(first, second):
    return identity(first) == identity(second)


def _proof(document, window):
    proof = document.get('native_proof')
    if type(proof) is not dict or set(proof) != {'root', 'request_sha256', 'outcome_sha256'}:
        raise ComputerError('windows_visual_document_proof_required')
    root = safe_root(proof['root'])
    request_path, outcome_path = root / 'native-request.json', root / 'native-outcome.json'
    request, outcome = read_record(request_path), read_record(outcome_path)
    if (sha256(request_path.read_bytes()).hexdigest() != proof['request_sha256']
            or sha256(outcome_path.read_bytes()).hexdigest() != proof['outcome_sha256']
            or type(request) is not dict or request.get('op') != 'visual_document_identity'
            or set(request) not in ({'op', 'window'}, {'op', 'window', 'input_epoch'})
            or type(outcome) is not dict or outcome.get('ok') is not True
            or set(outcome) not in ({'ok', 'document'}, {'ok', 'document', 'input_epoch'})
            or outcome.get('document') != identity(document)):
        raise ComputerError('windows_visual_document_proof_changed')
    from .windows_visual_memory import _window
    _window(request.get('window'))
    if window is not None and request['window'] != window:
        raise ComputerError('windows_visual_document_proof_window_changed')


def verify(document, *, window=None, require_native=True):
    """Verify persisted identity and present bytes, without observing any UI."""
    keys = {'schema', 'path', 'sha256', 'size', 'format', 'application', 'registered_app', 'read_only', 'saved'}
    if (type(document) is not dict or set(document) - {'native_proof'} != keys or document['schema'] != 1
            or document['application'] != 'wps' or document['format'] not in {'docx', 'xlsx'}
            or type(document['read_only']) is not bool or document['saved'] is not True
            or type(document['size']) is not int or document['size'] <= 0
            or not re.fullmatch('[0-9a-f]{64}', str(document['sha256']))
            or type(document['registered_app']) is not str
            or not Path(document['registered_app']).is_absolute()
            or document['registered_app'] != str(safe_root(document['registered_app']))
            or Path(document['path']).suffix.casefold() != '.' + document['format']):
        raise ComputerError('windows_visual_document_identity_invalid')
    if _file(document['path']) != {key: document[key] for key in ('path', 'sha256', 'size')}:
        raise ComputerError('windows_visual_document_changed')
    if require_native or 'native_proof' in document:
        _proof(document, window)
    return document


def capture(window, check):
    """Discover a path through existing ROT objects; attest its visible active tab."""
    from . import computer_uia as uia, windows_access as access
    from . import windows_workbook_binding as books
    check()
    fresh = uia._recheck(window)
    if access._kind(fresh) != 'wps' or fresh['title'] != window['title']:
        raise ComputerError('windows_visual_document_identity_unavailable')
    observation, _ = access._observe(window, check)
    if observation.get('blocked_reason') or observation.get('dialogs') or observation.get('truncated'):
        raise ComputerError('windows_visual_document_identity_unavailable')
    paths = set()
    for kind in ('docx', 'xlsx'):
        check()
        try:
            if kind == 'docx':
                _, app = files._document_application(window, check)
                path = files._local_path(app.ActiveDocument.FullName)
            else:
                _, app, _ = books._workbook_application(window, check)
                path = files._local_path(app.ActiveWorkbook.FullName)
        except ComputerError as exc:
            if str(exc) not in {'windows_document_anchor_unavailable', 'windows_workbook_anchor_unavailable'}:
                raise
            continue
        if path.suffix.casefold() != '.' + kind:
            continue
        status = files.document_status(window, path, check)
        if status.get('active') is True:
            if status.get('saved') is not True or type(status.get('read_only')) is not bool:
                raise ComputerError('windows_visual_document_unsaved')
            paths.add((str(path), status['read_only']))
    if len(paths) != 1:
        raise ComputerError('windows_visual_document_identity_unavailable')
    path, read_only = next(iter(paths))
    before = _file(path)
    check()
    status = files.document_status(window, path, check)
    if status != {'active': True, 'read_only': read_only, 'saved': True} or _file(path) != before:
        raise ComputerError('windows_visual_document_changed')
    check()
    if uia._recheck(window) != fresh:
        raise ComputerError('windows_visual_document_changed')
    result = {'schema': 1, **before, 'format': Path(path).suffix[1:].casefold(),
              'application': 'wps', 'registered_app': str(safe_root(fresh['image'])),
              'read_only': read_only, 'saved': True}
    return verify(result, require_native=False)
