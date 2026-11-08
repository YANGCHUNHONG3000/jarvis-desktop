"""Read-only local DOCX/XLSX authorization and existing WPS attestation.

No input, Office startup, document mutation, model calls or persistent writes.
The adapter is responsible for proving OS dialog ownership before recognition.
"""
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import xml.etree.ElementTree as ET
import zipfile

from .computer import ComputerError, safe_root, same_identity

OPEN_DOCUMENT_SELECTOR = {'type': 'Document', 'name': '已打开本地文件',
                          'automation_id': 'jarvis.open_document'}
OPEN_NAMES = {'打开文件', '打开', 'Open', 'Open File'}
OPEN_CLASSES = {'Qt5QWindow', 'Qt5QWindowIcon', '#32770'}
FILENAME_NAMES = {'文件名称(N)', '文件名(N):', 'File name:', 'File name'}
OPEN_BUTTON_NAMES = {'打开(O)', '打开(O):', 'Open', '&Open'}
MAX_FILE = 20_000_000
_MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
_WORKBOOK_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
_CORE_PROPERTIES_REL = 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties'
_CORE_PROPERTIES_ROOT = '{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties'
_BAD_SUFFIXES = {'.exe', '.dll', '.com', '.scr', '.msi', '.bat', '.cmd', '.ps1', '.vbs',
                 '.js', '.jse', '.hta', '.html', '.htm', '.mhtml', '.mht', '.bin'}


def _local_path(value):
    if not isinstance(value, (str, Path)) or not str(value) or len(str(value)) > 3000:
        raise ComputerError('windows_file_path_invalid')
    raw = str(value)
    path = Path(value)
    if (not path.is_absolute() or raw.startswith(('\\\\', '//'))
            or any(ord(c) < 32 for c in raw) or path.suffix.casefold() not in {'.docx', '.xlsx'}
            or ':' in raw[2:] or '..' in path.parts):
        raise ComputerError('windows_file_path_invalid')
    try:
        for item in (path, *path.parents):
            info = item.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise ComputerError('windows_file_path_invalid')
        return safe_root(path)
    except (OSError, ValueError) as exc:
        raise ComputerError('windows_file_path_invalid') from exc


def _stamp(info):
    # Windows path.stat() and CRT fstat() expose different ctime semantics.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _package(data, *, spreadsheet=False):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(i.file_size for i in entries) > 40_000_000:
                raise ComputerError('windows_file_package_size')
            seen, content_types, document = set(), None, False
            for entry in entries:
                name, low = entry.filename, entry.filename.casefold()
                relative = PurePosixPath(name)
                if (not name or '\\' in name or relative.is_absolute() or '..' in relative.parts
                        or ':' in name or low in seen or entry.flag_bits & 1
                        or name.rstrip('/') != str(relative) or entry.file_size > 10_000_000
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.file_size > 100_000 and entry.file_size > 200 * max(1, entry.compress_size)):
                    raise ComputerError('windows_file_package_unsafe')
                seen.add(low)
                if (any(x in low for x in ('vba', 'activex', 'embeddings/', 'externallinks/', 'afchunk'))
                        or relative.suffix.casefold() in _BAD_SUFFIXES):
                    raise ComputerError('windows_file_active_content')
                if entry.is_dir():
                    continue
                part = archive.read(entry)
                if part.startswith((b'MZ', b'\x7fELF', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1')):
                    raise ComputerError('windows_file_active_content')
                if low.endswith(('.xml', '.rels')):
                    if b'\x00' in part or b'<!DOCTYPE' in part.upper() or b'<!ENTITY' in part.upper():
                        raise ComputerError('windows_file_package_unsafe')
                    xml = ET.fromstring(part)
                    for node in xml.iter():
                        local = node.tag.rsplit('}', 1)[-1].casefold()
                        if local in {'object', 'oleobject', 'altchunk', 'control'}:
                            raise ComputerError('windows_file_active_content')
                        instructions = ([node.text or ''] if local == 'instrtext' else [])
                        instructions.extend(v for k, v in node.attrib.items() if k.rsplit('}', 1)[-1].casefold() == 'instr')
                        if any(re.match(r'\s*(DDE(?:AUTO)?|INCLUDETEXT|INCLUDEPICTURE|LINK)\b', value, re.I)
                               for value in instructions):
                            raise ComputerError('windows_file_active_content')
                    if low.endswith('.rels'):
                        for relation in xml:
                            attrs = {k.casefold(): v.casefold() for k, v in relation.attrib.items()}
                            target, kind = attrs.get('target', ''), attrs.get('type', '')
                            if (attrs.get('targetmode', 'internal') != 'internal' or target.startswith(('//', '\\\\'))
                                    or re.match(r'[a-z][a-z0-9+.-]*:', target)):
                                raise ComputerError('windows_file_external_relationship')
                            passive_core = (name == '_rels/.rels'
                                            and relation.attrib.get('Type') == _CORE_PROPERTIES_REL
                                            and relation.attrib.get('Target') == 'docProps/core.xml')
                            if passive_core:
                                try:
                                    core_info = archive.getinfo('docProps/core.xml')
                                except KeyError as exc:
                                    raise ComputerError('windows_file_package_invalid') from exc
                                if core_info.file_size > 10_000_000:
                                    raise ComputerError('windows_file_package_size')
                                core_data = archive.read(core_info)
                                if (b'\x00' in core_data or b'<!DOCTYPE' in core_data.upper()
                                        or b'<!ENTITY' in core_data.upper()):
                                    raise ComputerError('windows_file_package_unsafe')
                                if ET.fromstring(core_data).tag != _CORE_PROPERTIES_ROOT:
                                    raise ComputerError('windows_file_active_content')
                            if (any(x in kind for x in ('oleobject', 'vbaproject', 'activex', 'afchunk'))
                                    or 'package' in kind and not passive_core):
                                raise ComputerError('windows_file_active_content')
                    if name == '[Content_Types].xml':
                        content_types = xml
                    if spreadsheet and name == 'xl/workbook.xml':
                        document = xml.tag == '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook'
                    elif not spreadsheet and name == 'word/document.xml':
                        document = xml.tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document'
            if content_types is None or not document:
                raise ComputerError('windows_file_package_invalid')
            main = False
            for item in content_types:
                kind = item.attrib.get('ContentType', '').casefold()
                if any(x in kind for x in ('macroenabled', 'vba', 'oleobject', 'activex', 'x-msdownload')):
                    raise ComputerError('windows_file_active_content')
                if (item.attrib.get('PartName') == ('/xl/workbook.xml' if spreadsheet else '/word/document.xml')
                        and kind == (_WORKBOOK_TYPE if spreadsheet else _MAIN_TYPE)):
                    main = True
            if not main:
                raise ComputerError('windows_file_package_invalid')
    except ComputerError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ComputerError('windows_file_package_invalid') from exc


def snapshot_file(path):
    """Authorize one existing passive Office file, including formula safety."""
    path = _local_path(path)
    try:
        initial = path.stat()
        if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= MAX_FILE:
            raise ComputerError('windows_file_size_invalid')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _stamp(initial) != _stamp(opened):
                raise ComputerError('windows_file_changed')
            data = stream.read(MAX_FILE + 1)
            if len(data) != opened.st_size:
                raise ComputerError('windows_file_changed')
            if path.suffix.casefold() == '.xlsx':
                _package(data, spreadsheet=True)
                from .windows_workbook_model import disk_model
                disk_model(path)  # Validate bounded formulas before Office opens.
            else:
                _package(data)
            stream.seek(0)
            if stream.read(MAX_FILE + 1) != data or _stamp(os.fstat(stream.fileno())) != _stamp(opened):
                raise ComputerError('windows_file_changed')
        if _local_path(path) != path or _stamp(path.stat()) != _stamp(opened):
            raise ComputerError('windows_file_changed')
    except OSError as exc:
        raise ComputerError('windows_file_unavailable') from exc
    return {'path': str(path), 'sha256': sha256(data).hexdigest(), 'size': len(data)}


def _token_record(value):
    if (type(value) is not dict or set(value) != {'path', 'sha256', 'size'}
            or type(value['path']) is not str or type(value['sha256']) is not str
            or re.fullmatch('[a-f0-9]{64}', value['sha256']) is None
            or type(value['size']) is not int or not 0 < value['size'] <= MAX_FILE):
        raise ComputerError('windows_file_token_invalid')
    path = _local_path(value['path'])
    if str(path) != value['path']:
        raise ComputerError('windows_file_token_invalid')
    return value


def encode_token(value):
    """Encode a snapshot or explicit local path; always revalidate current bytes."""
    record = snapshot_file(value) if isinstance(value, (str, Path)) else _token_record(value)
    if snapshot_file(record['path']) != record:
        raise ComputerError('windows_file_changed')
    result = json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    if len(result) > 4000:
        raise ComputerError('windows_file_token_invalid')
    return result


def decode_token(value):
    """Reject stale approved bytes; decoding never silently refreshes a token."""
    if type(value) is not str or not 0 < len(value) <= 4000:
        raise ComputerError('windows_file_token_invalid')
    try:
        record = _token_record(json.loads(value))
    except (TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, ComputerError):
            raise
        raise ComputerError('windows_file_token_invalid') from exc
    if snapshot_file(record['path']) != record:
        raise ComputerError('windows_file_changed')
    return dict(record)


def is_open_dialog(identity, *, ownership_verified=False):
    """Label recognition only, after caller proves OS owner and process lineage."""
    return (ownership_verified is True and type(identity) is dict
            and identity.get('title') in OPEN_NAMES and identity.get('class_name') in OPEN_CLASSES)


def _dialog_control(observation, kind, names):
    """Select a unique enabled control from the already OS-bound open dialog."""
    dialogs = observation.get('dialogs', [])
    if (observation.get('application') != 'wps' or observation.get('truncated')
            or observation.get('blocked_reason') or len(dialogs) != 1
            or dialogs[0].get('dialog_kind') != 'open_file'
            or not is_open_dialog(dialogs[0], ownership_verified=True)):
        raise ComputerError('windows_file_dialog_invalid')
    matches = [c for c in observation.get('controls', [])
               if c.get('owner_hwnd') == dialogs[0]['hwnd'] and c.get('type') == kind
               and c.get('name') in names and c.get('automation_id') == '']
    if len(matches) != 1 or not matches[0].get('enabled') or not matches[0].get('visible'):
        raise ComputerError('windows_file_control_ambiguous')
    return matches[0]


def filename_field(observation):
    """Return the exact WPS file-name ComboBox; no arbitrary Edit fallback."""
    return _dialog_control(observation, 'ComboBox', FILENAME_NAMES)


def open_button(observation):
    return _dialog_control(observation, 'Button', OPEN_BUTTON_NAMES)


def filename_edit(observation):
    """Return (ComboBox, Edit) for native direct-parent RuntimeId verification.

    Semantic ancestry is a preliminary filter. The caller MUST additionally use
    the native ControlViewWalker parent and compare it with the ComboBox before
    SetValue, as a truncated ancestor list is not proof of direct parenthood.
    """
    field = filename_field(observation)
    selector = {key: field[key] for key in ('type', 'name', 'automation_id')}
    matches = [c for c in observation.get('controls', [])
               if c.get('owner_hwnd') == field['owner_hwnd'] and c.get('type') == 'Edit'
               and c.get('name') == '' and c.get('automation_id') == ''
               and c.get('ancestors', []) and c['ancestors'][-1] == selector]
    if (len(matches) != 1 or not matches[0].get('enabled') or not matches[0].get('visible')
            or 'set_value' not in matches[0].get('patterns', [])):
        raise ComputerError('windows_file_control_ambiguous')
    return field, matches[0]


class _ExistingObject:
    """PROPERTYGET-only facade over an already acquired IDispatch pointer."""
    _names = {'Hwnd', 'Application', 'Document', 'ActiveWindow', 'ActiveDocument', 'FullName',
              'ReadOnly', 'Saved', 'Documents', 'Count'}

    def __init__(self, pointer):
        self.pointer = pointer

    def __getattr__(self, name):
        if name not in self._names:
            raise AttributeError(name)
        member = self.pointer.GetIDsOfNames(0, name)
        value = self.pointer.Invoke(member, 0, 2, 1)  # DISPATCH_PROPERTYGET only.
        return _ExistingObject(value) if hasattr(value, 'Invoke') else value

    def item(self, index):
        """Read one bounded collection item without exposing COM method calls."""
        if type(index) is not int or not 1 <= index <= 64:
            raise ComputerError('windows_document_collection_invalid')
        member = self.pointer.GetIDsOfNames(0, 'Item')
        value = self.pointer.Invoke(member, 0, 2, 1, index)
        if not hasattr(value, 'Invoke'):
            raise ComputerError('windows_document_collection_invalid')
        return _ExistingObject(value)


def _bound_hwnd(hwnd, window):
    from . import computer_uia as uia
    import win32gui
    if type(hwnd) is not int or hwnd <= 0:
        raise ComputerError('windows_document_identity_mismatch')
    root = int(win32gui.GetAncestor(hwnd, 2) or 0)  # GA_ROOT: child windows only.
    if root != window['hwnd']:
        raise ComputerError('windows_document_identity_mismatch')
    if not same_identity(uia._identity(root), window):
        raise ComputerError('windows_document_identity_mismatch')


def _visible_bound_hwnd(hwnd, window):
    """A component's stale ActiveDocument/Workbook is not an active UI tab."""
    import win32gui
    _bound_hwnd(hwnd, window)
    if hwnd == window['hwnd']:
        raise ComputerError('windows_document_visibility_unavailable')
    try:
        visible = win32gui.IsWindowVisible(hwnd)
    except Exception as exc:
        raise ComputerError('windows_document_visibility_unavailable') from exc
    if not visible:
        raise ComputerError('windows_document_not_visible')


def _exact_rot_document(approved, check):
    """Acquire only an exact already-running file moniker, without activation."""
    import pythoncom
    check()
    try:
        table = pythoncom.GetRunningObjectTable()
        iterator = iter(table.EnumRunning())
        context = pythoncom.CreateBindCtx(0)
    except Exception as exc:
        raise ComputerError('windows_document_rot_unavailable') from exc
    matches = []
    count = 0
    try:
        for moniker in iterator:
            check()
            count += 1
            if count > 128:
                raise ComputerError('windows_document_rot_scope_exceeded')
            name = moniker.GetDisplayName(context, None)
            if (type(name) is str and Path(name).is_absolute()
                    and os.path.normcase(os.path.normpath(name)) == os.path.normcase(str(approved))):
                matches.append(moniker)
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_document_rot_unreadable') from exc
    if len(matches) > 1:
        raise ComputerError('windows_document_rot_ambiguous')
    if not matches:
        return None
    check()
    try:
        pointer = table.GetObject(matches[0]).QueryInterface(pythoncom.IID_IDispatch)
    except Exception as exc:
        raise ComputerError('windows_document_rot_unreadable') from exc
    check()
    return _ExistingObject(pointer)


def _attested_document(window, approved_path, check, *, include_status):
    from . import computer_uia as uia
    check()
    fresh = uia._recheck(window)
    approved = _local_path(approved_path)
    document = _exact_rot_document(approved, check)
    inactive = {'active': False, 'read_only': None, 'saved': None}
    if document is None:
        check()
        uia._recheck(window)
        return None, inactive

    def read():
        check()
        try:
            full_name = document.FullName
            app = document.Application
            active_name = app.ActiveDocument.FullName
            document_hwnd = document.ActiveWindow.Hwnd
            app_hwnd = app.ActiveWindow.Hwnd
        except Exception as exc:
            raise ComputerError('windows_document_com_unreadable') from exc
        check()
        if _local_path(full_name) != approved or _local_path(active_name) != approved:
            return None
        try:
            _visible_bound_hwnd(document_hwnd, fresh)
            _visible_bound_hwnd(app_hwnd, fresh)
        except ComputerError as exc:
            if str(exc) in {'windows_document_identity_mismatch', 'windows_document_not_visible'}:
                return None
            raise
        flags = ()
        if include_status:
            check()
            try:
                read_only, saved = document.ReadOnly, document.Saved
            except Exception as exc:
                raise ComputerError('windows_document_com_unreadable') from exc
            if type(read_only) is not bool or type(saved) is not bool:
                raise ComputerError('windows_document_flag_invalid')
            flags = read_only, saved
        check()
        return full_name, active_name, document_hwnd, app_hwnd, *flags

    first = read()
    if first is None:
        check()
        uia._recheck(window)
        return None, inactive
    second = read()
    check()
    if not same_identity(uia._recheck(window), fresh):
        raise ComputerError('windows_document_identity_mismatch')
    if second is None:
        return None, inactive
    if first != second:
        if include_status:
            error = ComputerError('windows_document_state_changed')
            error.document_diagnostic = {
                'name_changed': first[:2] != second[:2],
                'hwnd_changed': first[2:4] != second[2:4],
                'flags_changed': first[4:] != second[4:],
                'first_read_only': first[4], 'first_saved': first[5],
                'second_read_only': second[4], 'second_saved': second[5],
            }
            raise error
        return None, inactive
    status = ({'active': True, 'read_only': first[4], 'saved': first[5]} if include_status
              else {'active': True, 'read_only': None, 'saved': None})
    return document, status


def document_is_active(window, approved_path, check):
    """Attest the exact existing ROT document, active tab and root/process twice.

    This preserves the original open-document contract without requiring the
    newer Saved/ReadOnly capabilities. No COM activation or mutation occurs.
    """
    if Path(approved_path).suffix.casefold() == '.xlsx':
        from .windows_workbook_binding import workbook_status
        return workbook_status(window, approved_path, check)['active']
    return _attested_document(window, approved_path, check, include_status=False)[1]['active']


def document_status(window, approved_path, check):
    """Read stable active/ReadOnly/Saved facts from one exactly bound document.

    No matching active document returns false/None/None; missing or untyped COM
    capabilities fail explicitly. Saved is an application fact, not proof of
    durable disk bytes. The caller separately authorizes and verifies bytes.
    """
    if Path(approved_path).suffix.casefold() == '.xlsx':
        from .windows_workbook_binding import workbook_status
        return workbook_status(window, approved_path, check)
    return _attested_document(window, approved_path, check, include_status=True)[1]


def bound_document(window, approved_path, check):
    """Return (existing document, status) from the same double attestation.

    Caller keeps this object in its supervised COM worker and must recheck its
    input/state guards immediately before any separately authorized effect.
    Never substitutes a global application object or a same-basename document.
    """
    if Path(approved_path).suffix.casefold() == '.xlsx':
        from .windows_workbook_binding import bound_workbook
        return bound_workbook(window, approved_path, check)
    document, status = _attested_document(window, approved_path, check, include_status=True)
    if not status['active']:
        raise ComputerError('windows_document_not_active')
    return document, status


def _document_application(window, check):
    """Anchor a running application to this exact selected WPS root."""
    from . import computer_uia as uia
    import pythoncom
    check()
    uia._recheck(window)
    try:
        table = pythoncom.GetRunningObjectTable()
        iterator = iter(table.EnumRunning())
        context = pythoncom.CreateBindCtx(0)
        count = 0
        for moniker in iterator:
            check()
            count += 1
            if count > 128:
                raise ComputerError('windows_document_rot_scope_exceeded')
            try:
                name = moniker.GetDisplayName(context, None)
                if (type(name) is not str or not Path(name).is_absolute()
                        or Path(name).suffix.casefold() != '.docx'):
                    continue
                approved = _local_path(name)
                pointer = table.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
                document = _ExistingObject(pointer)
                if _local_path(document.FullName) != approved:
                    continue
                app = document.Application
                _bound_hwnd(document.ActiveWindow.Hwnd, window)
                _bound_hwnd(app.ActiveWindow.Hwnd, window)
            except Exception:
                # A candidate can belong to another app/root or be unsupported.
                # Only a fully proven anchor authorizes collection metadata.
                continue
            check()
            uia._recheck(window)
            return document, app
    except ComputerError:
        raise
    except Exception as exc:
        raise ComputerError('windows_document_rot_unreadable') from exc
    raise ComputerError('windows_document_anchor_unavailable')


def _collection_presence(approved, window, check):
    from . import computer_uia as uia
    check()
    uia._recheck(window)
    anchor, app = _document_application(window, check)
    acquired = [anchor, app]

    def read():
        check()
        try:
            _bound_hwnd(app.ActiveWindow.Hwnd, window)
            collection = app.Documents
            count = collection.Count
            if type(count) is not int or not 1 <= count <= 64:
                raise ComputerError('windows_document_collection_invalid')
            acquired.append(collection)
            names = []
            for index in range(1, count + 1):
                check()
                document = collection.item(index)
                acquired.append(document)
                name = document.FullName
                if (type(name) is not str or not 0 < len(name) <= 3000
                        or any(ord(char) < 32 for char in name)
                        or name.startswith(('\\\\?\\', '\\\\.\\'))):
                    raise ComputerError('windows_document_collection_invalid')
                # Other document names are metadata only. Do not read their
                # paths, bytes or bodies, including unsaved relative names.
                names.append(os.path.normcase(os.path.normpath(name)))
            after_count = collection.Count
            if type(after_count) is not int:
                raise ComputerError('windows_document_collection_invalid')
            if after_count != count:
                error = ComputerError('windows_document_state_changed')
                error.document_diagnostic = {'collection_count_changed': True}
                raise error
            _bound_hwnd(app.ActiveWindow.Hwnd, window)
        except ComputerError:
            raise
        except Exception as exc:
            raise ComputerError('windows_document_collection_unavailable') from exc
        check()
        uia._recheck(window)
        return tuple(names)

    first, second = read(), read()
    check()
    uia._recheck(window)
    target = os.path.normcase(str(approved))
    if first != second:
        error = ComputerError('windows_document_state_changed')
        error.document_diagnostic = {'collection_changed': True,
                                     'first_present': target in first, 'second_present': target in second}
        raise error
    return target in second


def document_present(approved_path, check, *, window=None):
    """Check presence; production closure requires the selected ``window``.

    With window, an exact-root ROT anchor proves Application identity and two
    complete stable Documents metadata lists prove presence or absence. An
    unavailable anchor/collection fails closed. The legacy no-window route only
    checks exact ROT registration twice and is not authoritative for closure.
    """
    approved = _local_path(approved_path)
    if approved.suffix.casefold() == '.xlsx':
        if window is None:
            raise ComputerError('windows_workbook_window_required')
        from .windows_workbook_binding import workbook_present
        return workbook_present(approved, check, window=window)
    if window is not None:
        return _collection_presence(approved, window, check)
    acquired = []  # Keep this call's COM references alive across both enumerations.
    def read():
        check()
        document = _exact_rot_document(approved, check)
        if document is None:
            return False
        acquired.append(document)
        try:
            full_name = document.FullName
        except Exception as exc:
            raise ComputerError('windows_document_com_unreadable') from exc
        if _local_path(full_name) != approved:
            raise ComputerError('windows_document_identity_mismatch')
        check()
        return True
    first, second = read(), read()
    check()
    if first != second:
        error = ComputerError('windows_document_state_changed')
        error.document_diagnostic = {'first_present': first, 'second_present': second}
        raise error
    return second
