"""Save one exactly bound existing WPS document to a new, leased local copy.

The caller owns COM initialization, native intent, deadlines and input guards.
No application startup, original overwrite, document close or uncertain retry.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from .computer import ComputerError, digest, read_record, safe_root, same_identity, write_new
from .run_lifecycle import _lock, _unlock
from . import windows_file_dialog as files

BASE = Path(__file__).resolve().parents[1] / 'data' / 'windows_documents'
SAVE_SELECTOR = {'type': 'Document', 'name': '已保存本地副本', 'automation_id': 'jarvis.saved_copy'}
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_LIMIT = 4_000_000


class CopyError(ComputerError):
    """Only this module can attest whether its save method invocation began."""
    def __init__(self, code, *, no_external_effect, diagnostic=None):
        super().__init__(code)
        self.no_external_effect = no_external_effect
        self.copy_diagnostic = diagnostic or {}


def _ensure_lxml():
    """Reuse only lxml from this interpreter's base installation if necessary.

    Do not add base site-packages to sys.path or expose other host packages to
    the desktop venv. No installation, environment edit or subprocess occurs.
    """
    if importlib.util.find_spec('lxml') is not None:
        return
    site = _path(Path(sys.base_prefix) / 'Lib' / 'site-packages')
    package = _path(site / 'lxml')
    spec = importlib.machinery.PathFinder.find_spec('lxml', [str(site)])
    if spec is None:
        raise ComputerError('windows_copy_dependency_unavailable')
    try:
        locations = list(spec.submodule_search_locations or [])
        if (spec.name != 'lxml' or spec.loader is None
                or _path(spec.origin) != package / '__init__.py'
                or locations != [str(package)]):
            raise ValueError()
    except (ComputerError, TypeError, ValueError) as exc:
        raise ComputerError('windows_copy_dependency_source_invalid') from exc
    previous = {key: value for key, value in sys.modules.items() if key == 'lxml' or key.startswith('lxml.')}
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules['lxml'] = module
        spec.loader.exec_module(module)
        if list(module.__path__) != [str(package)] or _path(module.__file__) != package / '__init__.py':
            raise ValueError()
    except Exception as exc:
        for key in list(sys.modules):
            if (key == 'lxml' or key.startswith('lxml.')) and key not in previous:
                sys.modules.pop(key, None)
        sys.modules.update(previous)
        raise ComputerError('windows_copy_dependency_invalid') from exc


def _office_edit():
    _ensure_lxml()
    try:
        from . import office_edit
    except ImportError as exc:
        raise ComputerError('windows_copy_dependency_invalid') from exc
    return office_edit


def _path(value):
    if not isinstance(value, (str, Path)):
        raise ComputerError('windows_copy_path_invalid')
    raw, path = str(value), Path(value)
    if (not path.is_absolute() or len(raw) > 3000 or raw.startswith(('\\\\', '//'))
            or ':' in raw[2:] or '..' in path.parts or any(ord(c) < 32 for c in raw)):
        raise ComputerError('windows_copy_path_invalid')
    try:
        for item in (path, *path.parents):
            if os.path.lexists(item):
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ComputerError('windows_copy_path_invalid')
        return safe_root(path)
    except OSError as exc:
        raise ComputerError('windows_copy_path_invalid') from exc


def _identity(path):
    info = path.stat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def _source(path):
    path = files._local_path(path)
    before = files._stamp(path.stat())
    snapshot = files.snapshot_file(path)
    after = files._stamp(path.stat())
    if before != after:
        raise ComputerError('windows_file_changed')
    return snapshot, {'device': after[0], 'inode': after[1], 'size': after[2], 'mtime_ns': after[3]}


def _read_bytes(path, limit=300_000):
    path = _path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ComputerError('windows_copy_record_invalid')
    data = path.read_bytes()
    if not 0 < len(data) <= limit:
        raise ComputerError('windows_copy_record_invalid')
    return data


def _hash(path, limit=300_000):
    return sha256(_read_bytes(path, limit)).hexdigest()


def _new_bytes(path, data):
    with path.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _model(parts):
    office_edit = _office_edit()
    try:
        body = office_edit._word_body(parts)
        semantics = office_edit._word_native_semantics(body)
        stream = office_edit._word_native_stream(body)
    except Exception as exc:
        raise ComputerError('windows_copy_content_unsupported') from exc
    return {'semantic_sha256': digest({'version': 1, 'content': semantics}),
            'stream_sha256': sha256(stream.encode('utf-8')).hexdigest()}, stream


def _disk_model(path):
    if Path(path).suffix.casefold() == '.xlsx':
        from .windows_workbook_model import disk_model
        return disk_model(path)
    office_edit = _office_edit()
    try:
        parts = office_edit._parts(path)
    except Exception as exc:
        raise ComputerError('windows_copy_content_unsupported') from exc
    return _model(parts)[0]


def _flat_model(data):
    office_edit = _office_edit()
    if (type(data) is not bytes or not 0 < len(data) <= _LIMIT
            or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper() or b'\x00' in data):
        raise ComputerError('windows_copy_native_content_invalid')
    try:
        package = office_edit._xml(data)
        ns = '{http://schemas.microsoft.com/office/2006/xmlPackage}'
        if package.tag != ns + 'package':
            raise ValueError()
        parts = {}
        for part in package:
            if part.tag != ns + 'part':
                raise ValueError()
            name = part.get(ns + 'name')
            children = list(part)
            if (type(name) is not str or not name.startswith('/') or name[1:] in parts
                    or len(children) != 1 or children[0].tag != ns + 'xmlData'
                    or len(children[0]) != 1):
                raise ValueError()
            xml = children[0][0]
            raw = office_edit.ET.tostring(xml, encoding='utf-8')
            if name.casefold().endswith('.rels'):
                for relationship in xml:
                    attrs = {key.casefold(): value.casefold() for key, value in relationship.attrib.items()}
                    target = attrs.get('target', '')
                    if (attrs.get('targetmode', 'internal') != 'internal' or target.startswith(('//', '\\\\'))
                            or re.match(r'[a-z][a-z0-9+.-]*:', target)):
                        raise ValueError()
            parts[name[1:]] = raw
        return _model(parts)
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_copy_native_content_invalid') from exc


def _property(pointer, name):
    member = pointer.GetIDsOfNames(0, name)
    return pointer.Invoke(member, 0, 2, 1)


def _capture(document, check):
    check()
    try:
        content = _property(document.pointer, 'Content')
        xml = _property(content, 'WordOpenXML')
        text = _property(content, 'Text')
    except Exception as exc:
        raise ComputerError('windows_copy_native_content_unavailable') from exc
    check()
    if type(xml) is not str or type(text) is not str:
        raise ComputerError('windows_copy_native_content_invalid')
    data = xml.encode('utf-8')
    model, stream = _flat_model(data)
    if text.replace('\v', '\n') != stream:
        raise ComputerError('windows_copy_native_content_mismatch')
    check()
    return data, model


def _workbook(manifest):
    return Path(manifest['source']['path']).suffix.casefold() == '.xlsx'


def output_paths(manifest):
    """Stable output names, keeping all schema-1 Word artifacts unchanged."""
    directory = Path(manifest['directory'])
    if _workbook(manifest):
        names = ('staging.xlsx', 'saved-workbook.xlsx', 'memory-before.json', 'memory-after.json')
    else:
        names = ('staging.docx', 'saved-document.docx', 'memory-before.xml', 'memory-after.xml')
    return dict(zip(('staging', 'final', 'memory_before', 'memory_after'),
                    (directory / name for name in names)))


def output_filename_from_token(token):
    return output_paths(decode(token))['final'].name


def _capture_for(document, manifest, check):
    if _workbook(manifest):
        from .windows_workbook_model import capture
        return capture(document.pointer, manifest['source_model'], check)
    return _capture(document, check)


def prepare(source_path, parent=BASE):
    """Bind current source bytes and a new exclusive directory into a short token."""
    snapshot, source_identity = _source(source_path)
    source_model = _disk_model(snapshot['path'])
    parent = _path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    parent = _path(parent)
    directory = parent / uuid.uuid4().hex
    directory.mkdir(exist_ok=False)
    nonce = uuid.uuid4().hex.encode('ascii')
    _new_bytes(directory / 'copy.lease', b'!' + nonce)
    manifest = {'schema': 1, 'id': directory.name, 'directory': str(directory),
                'directory_identity': _identity(directory), 'source': snapshot,
                'source_identity': source_identity, 'source_model': source_model,
                'lease_identity': _identity(directory / 'copy.lease'),
                'lease_sha256': sha256(b'!' + nonce).hexdigest()}
    write_new(directory / 'manifest.json', manifest)
    token = json.dumps({'schema': 1, 'id': directory.name, 'directory': str(directory),
                        'manifest_sha256': _hash(directory / 'manifest.json')},
                       ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(token) > 4000:
        raise ComputerError('windows_copy_token_invalid')
    decode(token)
    return token


def decode(token):
    """Validate immutable source/manifest/lease identity; allow recovery outputs."""
    if type(token) is not str or not 0 < len(token) <= 4000:
        raise ComputerError('windows_copy_token_invalid')
    try:
        value = json.loads(token)
        if (type(value) is not dict or set(value) != {'schema', 'id', 'directory', 'manifest_sha256'}
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['id']) is not str or not _ID.fullmatch(value['id'])
                or type(value['manifest_sha256']) is not str or not _SHA.fullmatch(value['manifest_sha256'])):
            raise ValueError()
        directory = _path(value['directory'])
        if str(directory) != value['directory'] or directory.name != value['id'] or not directory.is_dir():
            raise ValueError()
        if _hash(directory / 'manifest.json') != value['manifest_sha256']:
            raise ValueError()
        manifest = read_record(directory / 'manifest.json')
        fields = {'schema', 'id', 'directory', 'directory_identity', 'source', 'source_identity',
                  'source_model', 'lease_identity', 'lease_sha256'}
        if (type(manifest) is not dict or set(manifest) != fields or manifest['schema'] != 1
                or manifest['id'] != value['id'] or manifest['directory'] != value['directory']
                or manifest['directory_identity'] != _identity(directory)):
            raise ValueError()
        lease = _path(directory / 'copy.lease')
        if not lease.is_file() or lease.stat().st_size != 33 or _identity(lease) != manifest['lease_identity']:
            raise ValueError()
        with lease.open('rb') as stream:
            stream.seek(1)  # The lease locks byte zero; identity remains readable.
            nonce = stream.read(33)
        if len(nonce) != 32 or sha256(b'!' + nonce).hexdigest() != manifest['lease_sha256']:
            raise ValueError()
        current, current_identity = _source(manifest['source']['path'])
        if current != manifest['source'] or current_identity != manifest['source_identity']:
            raise ComputerError('windows_copy_source_changed')
        if _disk_model(current['path']) != manifest['source_model']:
            raise ValueError()
        return manifest
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ComputerError('windows_copy_token_invalid') from exc


def _operation(root, token, window=None):
    root = _path(root)
    try:
        request = read_record(_path(root / 'native-request.json'))
        intent = read_record(_path(root / 'native-intent.json'))
        if (request.get('op') != 'act' or request.get('action') != 'save_document_copy'
                or request.get('value') != token or request.get('document_token') != token
                or request.get('selector') != SAVE_SELECTOR or intent.get('request_sha') != digest(request)):
            raise ValueError()
        if window is not None and (type(request.get('window')) is not dict or not same_identity(request['window'], window)):
            raise ValueError()
    except (OSError, ValueError, AttributeError) as exc:
        raise ComputerError('windows_copy_native_intent_invalid') from exc
    return root, intent['request_sha']


@contextmanager
def _lease(manifest):
    path = _path(Path(manifest['directory']) / 'copy.lease')
    with path.open('r+b', buffering=0) as stream:
        if _identity(path) != manifest['lease_identity']:
            raise ComputerError('windows_copy_token_invalid')
        _lock(stream)
        try:
            yield
        finally:
            _unlock(stream)


def _method(document, *, workbook=False):
    for name in (('SaveAs',) if workbook else ('SaveAs2', 'SaveAs')):
        try:
            member = document.pointer.GetIDsOfNames(0, name)
        except Exception as exc:
            if name == 'SaveAs2' and getattr(exc, 'hresult', None) in {-2147352570, -2147352573}:
                continue
            raise ComputerError('windows_copy_save_method_unavailable') from exc
        if type(member) is not int:
            raise ComputerError('windows_copy_save_method_unavailable')
        return name, member
    raise ComputerError('windows_copy_save_method_unavailable')


def _same_document(document, window, path, check):
    if Path(path).suffix.casefold() == '.xlsx':
        from .windows_workbook_binding import same_workbook
        return same_workbook(document, window, path, check)
    from . import computer_uia as uia
    check()
    uia._recheck(window)
    try:
        app = document.Application
        names = document.FullName, app.ActiveDocument.FullName
        handles = document.ActiveWindow.Hwnd, app.ActiveWindow.Hwnd
        flags = document.ReadOnly, document.Saved
    except Exception as exc:
        raise ComputerError('windows_copy_document_unreadable') from exc
    if any(files._local_path(name) != path for name in names):
        raise ComputerError('windows_copy_document_changed')
    if any(type(value) is not bool for value in flags):
        raise ComputerError('windows_document_flag_invalid')
    for handle in handles:
        files._visible_bound_hwnd(handle, window)
    check()
    return names, handles, flags


def saved_receipt(token):
    """Return a fully reverified immutable receipt, None only for pristine work."""
    manifest = decode(token)
    directory = Path(manifest['directory'])
    paths = output_paths(manifest)
    if not os.path.lexists(directory / 'saved.json'):
        partials = ('save-started.json', 'memory-before.xml', 'memory-after.xml',
                    'staging.docx', 'saved-document.docx', 'memory-before.json',
                    'memory-after.json', 'staging.xlsx', 'saved-workbook.xlsx')
        if any(os.path.lexists(directory / name) for name in partials):
            raise ComputerError('windows_copy_incomplete')
        return None
    try:
        receipt = read_record(_path(directory / 'saved.json'))
        attempt = read_record(_path(directory / 'save-started.json'))
        expected = {'schema', 'token_sha256', 'id', 'source', 'source_identity', 'source_model',
                    'staging', 'final', 'memory_before', 'memory_after', 'models', 'attempt_sha256',
                    'operation_root', 'request_sha', 'method', 'read_only_before', 'saved_before', 'saved_after'}
        if (type(receipt) is not dict or set(receipt) != expected or receipt['schema'] != 1
                or receipt['token_sha256'] != sha256(token.encode()).hexdigest()
                or receipt['id'] != manifest['id'] or receipt['source'] != manifest['source']
                or receipt['source_identity'] != manifest['source_identity']
                or receipt['source_model'] != manifest['source_model']
                or receipt['attempt_sha256'] != _hash(directory / 'save-started.json')
                or receipt['method'] not in ({'SaveAs'} if _workbook(manifest) else {'SaveAs2', 'SaveAs'})
                or any(type(receipt[key]) is not bool for key in ('read_only_before', 'saved_before', 'saved_after'))
                or receipt['saved_after'] is not True):
            raise ValueError()
        root, request_sha = _operation(receipt['operation_root'], token)
        if receipt['request_sha'] != request_sha or attempt != {
                'schema': 1, 'token_sha256': receipt['token_sha256'], 'id': manifest['id'],
                'operation_root': str(root), 'request_sha': request_sha, 'method': receipt['method'],
                'memory_before': receipt['memory_before'], 'model': receipt['models']['memory_before'],
                'read_only_before': receipt['read_only_before'], 'saved_before': receipt['saved_before']}:
            raise ValueError()
        models = {}
        for name in ('staging', 'final'):
            current = files.snapshot_file(paths[name])
            if receipt[name] != current:
                raise ValueError()
            models[name] = _disk_model(current['path'])
        if receipt['staging']['sha256'] != receipt['final']['sha256']:
            raise ValueError()
        for name in ('memory_before', 'memory_after'):
            path = paths[name]
            data = _read_bytes(path, _LIMIT)
            if receipt[name] != {'path': str(path), 'sha256': sha256(data).hexdigest(), 'size': len(data)}:
                raise ValueError()
            if _workbook(manifest):
                from .windows_workbook_model import serialized_model
                models[name] = serialized_model(data)
            else:
                models[name] = _flat_model(data)[0]
        if receipt['models'] != models or any(value != models['memory_before'] for value in models.values()):
            raise ValueError()
        if _workbook(manifest) and models['memory_before'] != manifest['source_model']:
            raise ValueError()
        decode(token)
        return receipt
    except ComputerError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ComputerError('windows_copy_receipt_invalid') from exc


def save_copy(window, token, check, operation_root):
    """Save once, with explicit pre-invocation versus uncertain-effect errors."""
    effect = {'invoke_started': False}
    try:
        return _save_copy(window, token, check, operation_root, effect)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ComputerError) else 'windows_copy_failed'
        diagnostic = {'save_method_invoke_started': effect['invoke_started'],
                      **getattr(exc, 'copy_diagnostic', {})}
        raise CopyError(code, no_external_effect=not effect['invoke_started'], diagnostic=diagnostic) from exc


def _save_copy(window, token, check, operation_root, effect):
    """Every interrupted or uncertain intermediate remains nonretryable."""
    check()
    manifest = decode(token)
    root, request_sha = _operation(operation_root, token, window)
    with _lease(manifest):
        existing = saved_receipt(token)
        if existing is not None:
            return existing
        directory = Path(manifest['directory'])
        source = Path(manifest['source']['path'])
        paths = output_paths(manifest)
        staging, final = paths['staging'], paths['final']
        document, status = files.bound_document(window, source, check)
        before_xml, before_model = _capture_for(document, manifest, check)
        method_name, member = (_method(document, workbook=True) if _workbook(manifest) else _method(document))
        first = _same_document(document, window, source, check)
        repeated_xml, repeated_model = _capture_for(document, manifest, check)
        second = _same_document(document, window, source, check)
        if (first != second or repeated_model != before_model
                or first[2] != (status['read_only'], status['saved'])):
            error = ComputerError('windows_copy_document_changed')
            error.copy_diagnostic = {'binding_changed': first != second,
                'model_changed': repeated_model != before_model,
                'initial_flags_changed': first[2] != (status['read_only'], status['saved']),
                'raw_xml_changed': repeated_xml != before_xml,
                'first_xml_sha256': sha256(before_xml).hexdigest(),
                'second_xml_sha256': sha256(repeated_xml).hexdigest(),
                'first_semantic_sha256': before_model['semantic_sha256'],
                'second_semantic_sha256': repeated_model['semantic_sha256'],
                'first_stream_sha256': before_model.get('stream_sha256'),
                'second_stream_sha256': repeated_model.get('stream_sha256')}
            raise error
        decode(token)
        _operation(root, token, window)
        if os.path.lexists(staging) or os.path.lexists(final):
            raise ComputerError('windows_copy_output_exists')
        _new_bytes(paths['memory_before'], before_xml)
        before_snapshot = {'path': str(paths['memory_before']),
                           'sha256': sha256(before_xml).hexdigest(), 'size': len(before_xml)}
        token_sha = sha256(token.encode()).hexdigest()
        attempt = {'schema': 1, 'token_sha256': token_sha, 'id': manifest['id'],
                   'operation_root': str(root), 'request_sha': request_sha, 'method': method_name,
                   'memory_before': before_snapshot, 'model': before_model,
                   'read_only_before': status['read_only'], 'saved_before': status['saved']}
        write_new(directory / 'save-started.json', attempt)
        check()
        # No fallback or second call is possible after this point.
        effect['invoke_started'] = True
        try:
            document.pointer.Invoke(member, 0, 1, 0, str(staging), 51 if _workbook(manifest) else 16)
        except Exception as exc:
            raise ComputerError('windows_copy_save_unknown') from exc
        check()
        decode(token)
        first_after = _same_document(document, window, staging, check)
        after_xml, after_model = _capture_for(document, manifest, check)
        second_after = _same_document(document, window, staging, check)
        if (first_after != second_after or first_after[2][1] is not True or after_model != before_model):
            raise ComputerError('windows_copy_saved_content_mismatch')
        staging_snapshot = files.snapshot_file(staging)
        staging_model = _disk_model(staging)
        if staging_model != before_model:
            raise ComputerError('windows_copy_saved_content_mismatch')
        _new_bytes(paths['memory_after'], after_xml)
        with staging.open('rb') as input_file, final.open('xb') as output_file:
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        final_snapshot = files.snapshot_file(final)
        final_model = _disk_model(final)
        if final_snapshot['sha256'] != staging_snapshot['sha256'] or final_model != before_model:
            raise ComputerError('windows_copy_publish_mismatch')
        check()
        decode(token)
        receipt = {'schema': 1, 'token_sha256': token_sha, 'id': manifest['id'],
                   'source': manifest['source'], 'source_identity': manifest['source_identity'],
                   'source_model': manifest['source_model'], 'staging': staging_snapshot, 'final': final_snapshot,
                   'memory_before': before_snapshot,
                   'memory_after': {'path': str(paths['memory_after']),
                                    'sha256': sha256(after_xml).hexdigest(), 'size': len(after_xml)},
                   'models': {'memory_before': before_model, 'memory_after': after_model,
                              'staging': staging_model, 'final': final_model},
                   'attempt_sha256': _hash(directory / 'save-started.json'),
                   'operation_root': str(root), 'request_sha': request_sha, 'method': method_name,
                   'read_only_before': status['read_only'], 'saved_before': status['saved'], 'saved_after': True}
        write_new(directory / 'saved.json', receipt)
        return saved_receipt(token)
