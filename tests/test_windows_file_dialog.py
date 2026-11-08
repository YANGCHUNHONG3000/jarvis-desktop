"""Passive DOCX and fake-COM contracts; no Office launch or native input."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import weakref
import zipfile

import pytest

from agent_service import windows_file_dialog as fd
from agent_service import computer_uia as uia
from agent_service.computer import ComputerError

CONTENT = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Override PartName="/word/document.xml" ContentType="'
           'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
           '</Types>')
DOCUMENT = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'


def docx(path, extra=None):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', CONTENT)
        archive.writestr('word/document.xml', DOCUMENT)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return path


@pytest.fixture
def source(tmp_path):
    return docx(tmp_path / 'existing.docx')


def test_snapshot_token_round_trip_and_passive_media(source):
    docx(source, {'word/media/image1.png': b'\x89PNG\r\n\x1a\n'})
    record = fd.snapshot_file(source)
    assert record == {'path': str(source.resolve()), 'size': source.stat().st_size,
                      'sha256': sha256(source.read_bytes()).hexdigest()}
    token = fd.encode_token(record)
    assert len(token) < 4000 and fd.decode_token(token) == record
    assert fd.decode_token(fd.encode_token(source)) == record


def test_changed_file_never_refreshes_old_token(source):
    record = fd.snapshot_file(source)
    token = fd.encode_token(record)
    docx(source, {'docProps/core.xml': '<core>changed</core>'})
    for operation in (lambda: fd.decode_token(token), lambda: fd.encode_token(record)):
        with pytest.raises(ComputerError, match='windows_file_changed'):
            operation()


@pytest.mark.parametrize('value', ['relative.docx', r'\\server\share\a.docx', '//server/share/a.docx',
                                  r'C:\example.docx:stream', 123, '', '.', 'file.docm'])
def test_paths_must_be_absolute_local_docx(value):
    with pytest.raises(ComputerError, match='windows_file_path_invalid'):
        fd.snapshot_file(value)


def test_symlink_is_refused(source, tmp_path):
    link = tmp_path / 'linked.docx'
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip('host does not permit symlinks')
    with pytest.raises(ComputerError, match='windows_file_path_invalid'):
        fd.snapshot_file(link)


def test_parent_reparse_attribute_is_refused(source, monkeypatch):
    original = Path.lstat
    def lstat(path):
        actual = original(path)
        return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400) if path == source.parent else actual
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ComputerError, match='windows_file_path_invalid'):
        fd.snapshot_file(source)


@pytest.mark.parametrize('extra,code', [
    ({'word/vbaProject.bin': b'x'}, 'active_content'),
    ({'word/embeddings/oleObject1.bin': b'x'}, 'active_content'),
    ({'word/media/disguised.png': b'MZfake'}, 'active_content'),
    ({'word/media/ole.png': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'}, 'active_content'),
    ({'word/run.exe': b'x'}, 'active_content'),
    ({'word/run.ps1': b'x'}, 'active_content'),
    ({'../escape.xml': '<x/>'}, 'package_unsafe'),
    ({'word/unsafe.xml': '<!DOCTYPE x [<!ENTITY y "z">]><x/>'}, 'package_unsafe'),
    ({'word/unsafe.xml': '<x/>'.encode('utf-16')}, 'package_unsafe'),
    ({'word/malformed.xml': '<x>'}, 'package_invalid'),
    ({'word/_rels/document.xml.rels': '<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>'}, 'external_relationship'),
    ({'word/_rels/document.xml.rels': '<Relationships><Relationship Target="https://example.invalid"/></Relationships>'}, 'external_relationship'),
    ({'word/_rels/document.xml.rels': '<Relationships><Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject" Target="object.dat"/></Relationships>'}, 'active_content'),
])
def test_package_security_policy(source, extra, code):
    docx(source, extra)
    with pytest.raises(ComputerError, match='windows_file_' + code):
        fd.snapshot_file(source)


def test_docx_must_be_real_word_package(source):
    source.write_bytes(b'not a zip')
    with pytest.raises(ComputerError, match='windows_file_package_invalid'):
        fd.snapshot_file(source)

    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('[Content_Types].xml', CONTENT)
    with pytest.raises(ComputerError, match='windows_file_package_invalid'):
        fd.snapshot_file(source)


def test_standard_root_core_properties_relationship_is_passive(source):
    docx(source, {
        '_rels/.rels': '<Relationships><Relationship Type="' + fd._CORE_PROPERTIES_REL + '" Target="docProps/core.xml"/></Relationships>',
        'docProps/core.xml': '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"/>',
    })
    assert fd.snapshot_file(source)['path'] == str(source)


@pytest.mark.parametrize('name,kind,target,mode,code', [
    ('_rels/.rels', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/package', 'docProps/core.xml', '', 'active_content'),
    ('_rels/.rels', fd._CORE_PROPERTIES_REL, 'word/other.xml', '', 'active_content'),
    ('word/_rels/document.xml.rels', fd._CORE_PROPERTIES_REL, 'docProps/core.xml', '', 'active_content'),
    ('_rels/.rels', fd._CORE_PROPERTIES_REL, 'docProps/core.xml', 'External', 'external_relationship'),
    ('_rels/.rels', fd._CORE_PROPERTIES_REL + '/package', 'docProps/core.xml', '', 'active_content'),
])
def test_core_properties_exception_never_allows_embedded_packages_or_external_links(source, name, kind, target, mode, code):
    docx(source, {name: f'<Relationships><Relationship Type="{kind}" Target="{target}" TargetMode="{mode or "Internal"}"/></Relationships>'})
    with pytest.raises(ComputerError, match='windows_file_' + code):
        fd.snapshot_file(source)


@pytest.mark.parametrize('core_xml,code', [
    ('<wrongRoot/>', 'active_content'),
    ('<!DOCTYPE x [<!ENTITY y "unsafe">]><x/>', 'package_unsafe'),
    (None, 'package_invalid'),
])
def test_passive_core_relationship_requires_actual_safe_standard_core_xml(source, core_xml, code):
    parts = {'_rels/.rels': '<Relationships><Relationship Type="' + fd._CORE_PROPERTIES_REL + '" Target="docProps/core.xml"/></Relationships>'}
    if core_xml is not None:
        parts['docProps/core.xml'] = core_xml
    docx(source, parts)
    with pytest.raises(ComputerError, match='windows_file_' + code):
        fd.snapshot_file(source)


def test_snapshot_rejects_change_while_validating(source, monkeypatch):
    def mutate(data):
        source.write_bytes(b'changed')
    monkeypatch.setattr(fd, '_package', mutate)
    with pytest.raises(ComputerError, match='windows_file_changed'):
        fd.snapshot_file(source)


@pytest.mark.parametrize('body', ['<w:instrText>DDEAUTO command</w:instrText>',
                                  '<w:fldSimple w:instr="INCLUDETEXT remote"/>',
                                  '<w:object/>', '<w:altChunk/>'])
def test_active_word_instructions_and_embedded_objects_refused(source, body):
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('[Content_Types].xml', CONTENT)
        archive.writestr('word/document.xml', DOCUMENT.replace('<w:body/>', '<w:body>' + body + '</w:body>'))
    with pytest.raises(ComputerError, match='windows_file_active_content'):
        fd.snapshot_file(source)


def test_size_limit_precedes_package_decode(source, monkeypatch):
    monkeypatch.setattr(fd, 'MAX_FILE', 10)
    with pytest.raises(ComputerError, match='windows_file_size_invalid'):
        fd.snapshot_file(source)


@pytest.mark.parametrize('value', ['', '[]', 'null', '{}', 'x' * 4001,
                                  '{"path":"x","sha256":"bad","size":1}'])
def test_malformed_token_refused(value):
    with pytest.raises(ComputerError, match='windows_file_token_invalid'):
        fd.decode_token(value)


def test_boolean_size_is_not_integer_size(source):
    record = fd.snapshot_file(source)
    record['size'] = True
    with pytest.raises(ComputerError, match='windows_file_token_invalid'):
        fd.decode_token(json.dumps(record))


@pytest.fixture
def com(source, monkeypatch):
    window = dict(hwnd=100, pid=123, created='2026', image='wps.exe', class_name='OpusApp',
                  profile='observe_only', title='existing.docx - WPS')
    document = SimpleNamespace(FullName=str(source))
    app = SimpleNamespace(Hwnd=100, ActiveWindow=SimpleNamespace(Hwnd=101), ActiveDocument=document)
    monkeypatch.setattr(uia, '_recheck', lambda identity: dict(window))
    monkeypatch.setattr(uia, '_identity', lambda hwnd: dict(window))
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        GetAncestor=lambda hwnd, kind: 100 if hwnd in (100, 101) else hwnd,
        IsWindowVisible=lambda hwnd: True))
    return window, app


def test_reused_root_process_refused(active, source, monkeypatch):
    window, _, _ = active
    monkeypatch.setattr(uia, '_identity', lambda hwnd: {**window, 'created': 'reused'})
    assert fd.document_is_active(window, source, lambda: None) is False


def test_existing_dispatch_facade_only_gets_approved_properties():
    calls = []
    class Pointer:
        def GetIDsOfNames(self, lcid, name):
            calls.append(('name', lcid, name))
            return 123
        def Invoke(self, member, lcid, flags, want_result):
            calls.append(('get', member, lcid, flags, want_result))
            return 100
    existing = fd._ExistingObject(Pointer())
    assert existing.Hwnd == 100
    with pytest.raises(AttributeError):
        existing.Open
    with pytest.raises(AttributeError):
        existing.Save
    assert calls == [('name', 0, 'Hwnd'), ('get', 123, 0, 2, 1)]


@pytest.fixture
def active(com, monkeypatch):
    window, app = com
    document = app.ActiveDocument
    document.Application = app
    document.ActiveWindow = SimpleNamespace(Hwnd=101)
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: document)
    return window, app, document


def test_exact_rot_document_and_active_tab_double_attestation(active, source):
    window, _, _ = active
    checks = []
    assert fd.document_is_active(window, source, lambda: checks.append(1)) is True
    assert len(checks) >= 8


def test_hidden_word_component_cannot_claim_active_tab(active, source, monkeypatch):
    window, _, _ = active
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: False)
    assert fd.document_is_active(window, source, lambda: None) is False
    assert fd.document_status(window, source, lambda: None)['active'] is False


def test_shared_root_only_does_not_prove_visible_document(active, source):
    window, app, document = active
    document.ActiveWindow.Hwnd = window['hwnd']
    app.ActiveWindow.Hwnd = window['hwnd']
    with pytest.raises(ComputerError, match='windows_document_visibility_unavailable'):
        fd.document_is_active(window, source, lambda: None)


def test_visibility_change_between_word_reads_fails_closed(active, source, monkeypatch):
    window, _, _ = active
    sequence=iter([True, True, False])
    monkeypatch.setattr(sys.modules['win32gui'], 'IsWindowVisible', lambda hwnd: next(sequence))
    assert fd.document_is_active(window, source, lambda: None) is False


def test_same_basename_in_different_folder_is_not_accepted(active, source):
    window, app, _ = active
    other = source.parent / 'different' / source.name
    other.parent.mkdir()
    docx(other)
    app.ActiveDocument = SimpleNamespace(FullName=str(other))
    assert fd.document_is_active(window, source, lambda: None) is False


@pytest.mark.parametrize('which', ['document', 'application'])
def test_document_and_application_must_both_bind_selected_root(active, source, which):
    window, app, document = active
    if which == 'document':
        document.ActiveWindow.Hwnd = 999
    else:
        app.ActiveWindow.Hwnd = 999
    assert fd.document_is_active(window, source, lambda: None) is False


def test_active_tab_switch_during_second_read_is_not_success(active, source):
    window, app, document = active
    other = docx(source.parent / 'other.docx')
    class RacingApp:
        ActiveWindow = app.ActiveWindow
        reads = 0
        @property
        def ActiveDocument(self):
            self.reads += 1
            return document if self.reads == 1 else SimpleNamespace(FullName=str(other))
    document.Application = RacingApp()
    assert fd.document_is_active(window, source, lambda: None) is False


def test_unknown_active_tab_com_error_is_a_failure(active, source):
    window, app, _ = active
    del app.ActiveDocument
    with pytest.raises(ComputerError, match='^windows_document_com_unreadable$'):
        fd.document_is_active(window, source, lambda: None)


def test_no_exact_rot_document_is_not_active(active, source, monkeypatch):
    window, _, _ = active
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: None)
    assert fd.document_is_active(window, source, lambda: None) is False


@pytest.mark.parametrize('read_only,saved', [(False, False), (False, True), (True, False), (True, True)])
def test_status_preserves_boolean_flags_and_returns_same_bound_object(active, source, read_only, saved):
    window, _, document = active
    document.ReadOnly, document.Saved = read_only, saved
    expected = {'active': True, 'read_only': read_only, 'saved': saved}
    assert fd.document_status(window, source, lambda: None) == expected
    bound, status = fd.bound_document(window, source, lambda: None)
    assert bound is document and status == expected


@pytest.mark.parametrize('flag', ['ReadOnly', 'Saved'])
@pytest.mark.parametrize('value', [None, 0, 1, -1, 'False', [], {}])
def test_status_never_assumes_or_coerces_unknown_flag_types(active, source, flag, value):
    window, _, document = active
    document.ReadOnly, document.Saved = False, True
    setattr(document, flag, value)
    with pytest.raises(ComputerError, match='^windows_document_flag_invalid$'):
        fd.document_status(window, source, lambda: None)


@pytest.mark.parametrize('flag', ['ReadOnly', 'Saved'])
def test_missing_status_capability_is_explicit_failure(active, source, flag):
    window, _, document = active
    document.ReadOnly, document.Saved = False, True
    delattr(document, flag)
    with pytest.raises(ComputerError, match='^windows_document_com_unreadable$'):
        fd.document_status(window, source, lambda: None)


def test_not_found_status_is_false_with_unknown_flags(active, source, monkeypatch):
    window, _, _ = active
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: None)
    assert fd.document_status(window, source, lambda: None) == {'active': False, 'read_only': None, 'saved': None}
    with pytest.raises(ComputerError, match='^windows_document_not_active$'):
        fd.bound_document(window, source, lambda: None)


@pytest.mark.parametrize('flag', ['ReadOnly', 'Saved'])
def test_status_flag_change_between_reads_is_not_a_stable_status(active, source, monkeypatch, flag):
    window, app, document = active
    class RacingDocument:
        FullName = document.FullName
        Application = app
        ActiveWindow = document.ActiveWindow
        reads = 0
        def __getattr__(self, name):
            if name in {'ReadOnly', 'Saved'}:
                if name == flag:
                    self.reads += 1
                    return self.reads == 1
                return True
            raise AttributeError(name)
    racing = RacingDocument()
    app.ActiveDocument = racing
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: racing)
    with pytest.raises(ComputerError, match='^windows_document_state_changed$') as raised:
        fd.document_status(window, source, lambda: None)
    diagnostic = raised.value.document_diagnostic
    assert diagnostic['flags_changed'] is True
    assert diagnostic['name_changed'] is False and diagnostic['hwnd_changed'] is False
    assert all(type(value) is bool for value in diagnostic.values())


def test_status_diagnoses_changed_child_hwnd_without_exporting_document_names(active, source, monkeypatch):
    window, app, document = active
    document.ReadOnly, document.Saved = False, True
    class RacingWindow:
        reads = 0
        @property
        def Hwnd(self):
            self.reads += 1
            return 101 if self.reads == 1 else 102
    document.ActiveWindow = RacingWindow()
    monkeypatch.setattr(sys.modules['win32gui'], 'GetAncestor', lambda hwnd, kind: 100 if hwnd in (100, 101, 102) else hwnd)
    with pytest.raises(ComputerError, match='^windows_document_state_changed$') as raised:
        fd.document_status(window, source, lambda: None)
    assert raised.value.document_diagnostic == {'name_changed': False, 'hwnd_changed': True, 'flags_changed': False,
        'first_read_only': False, 'first_saved': True, 'second_read_only': False, 'second_saved': True}
    assert source.name not in json.dumps(raised.value.document_diagnostic)


def test_status_does_not_read_flags_from_another_active_tab(active, source):
    window, app, _ = active
    other = docx(source.parent / 'other.docx')
    app.ActiveDocument = SimpleNamespace(FullName=str(other))
    assert fd.document_status(window, source, lambda: None) == {'active': False, 'read_only': None, 'saved': None}


@pytest.mark.parametrize('present', [False, True])
def test_document_presence_uses_exact_rot_not_active_tab(active, source, monkeypatch, present):
    _, app, document = active
    app.ActiveDocument = SimpleNamespace(FullName='another tab')
    calls = []
    def exact(path, check):
        calls.append(path)
        return document if present else None
    monkeypatch.setattr(fd, '_exact_rot_document', exact)
    assert fd.document_present(source, lambda: None) is present
    assert calls == [source, source]


def test_document_presence_transition_is_not_closure_proof(active, source, monkeypatch):
    _, _, document = active
    values = iter([document, None])
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: next(values))
    with pytest.raises(ComputerError, match='^windows_document_state_changed$') as raised:
        fd.document_present(source, lambda: None)
    assert raised.value.document_diagnostic == {'first_present': True, 'second_present': False}


def test_document_presence_keeps_first_acquisition_alive_only_during_this_call(source, monkeypatch):
    references = []
    class Document:
        FullName = str(source)
    def exact(path, check):
        if references:
            assert references[0]() is not None
        document = Document()
        references.append(weakref.ref(document))
        return document
    monkeypatch.setattr(fd, '_exact_rot_document', exact)
    assert fd.document_present(source, lambda: None) is True
    assert len(references) == 2
    assert all(reference() is None for reference in references)


def test_document_presence_appearing_between_reads_has_boolean_diagnostic(source, monkeypatch):
    values = iter([None, SimpleNamespace(FullName=str(source))])
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: next(values))
    with pytest.raises(ComputerError, match='^windows_document_state_changed$') as raised:
        fd.document_present(source, lambda: None)
    assert raised.value.document_diagnostic == {'first_present': False, 'second_present': True}


def test_document_presence_validates_complete_reported_path(active, source, monkeypatch):
    _, _, document = active
    other = docx(source.parent / 'other.docx')
    document.FullName = str(other)
    monkeypatch.setattr(fd, '_exact_rot_document', lambda path, check: document)
    with pytest.raises(ComputerError, match='^windows_document_identity_mismatch$'):
        fd.document_present(source, lambda: None)


@pytest.fixture
def collection_application(com, monkeypatch):
    window, app = com
    class Collection:
        names = []
        calls = []
        @property
        def Count(self): return len(self.names)
        def item(self, index):
            self.calls.append(index)
            return SimpleNamespace(FullName=self.names[index - 1])
    collection = Collection()
    app.Documents = collection
    anchor = SimpleNamespace(FullName='anchor metadata only')
    monkeypatch.setattr(fd, '_document_application', lambda win, check: (anchor, app))
    return window, app, collection


@pytest.mark.parametrize('present', [False, True])
def test_window_bound_complete_collection_proves_presence_without_reading_other_files(collection_application, source, present):
    window, _, collection = collection_application
    # The second file does not exist; its FullName must remain metadata only.
    collection.names = [str(source) if present else 'Unsaved Document1', str(source.parent / 'nonexistent.docx')]
    assert fd.document_present(source, lambda: None, window=window) is present
    assert collection.calls == [1, 2, 1, 2]


@pytest.mark.parametrize('count', [0, 65, True, '2', None])
def test_window_bound_collection_never_assumes_absence_from_invalid_or_excessive_count(collection_application, source, count):
    window, app, _ = collection_application
    app.Documents = SimpleNamespace(Count=count)
    with pytest.raises(ComputerError, match='^windows_document_collection_invalid$'):
        fd.document_present(source, lambda: None, window=window)


def test_changed_complete_collection_is_not_closed_even_when_target_missing(collection_application, source):
    window, app, _ = collection_application
    class RacingCollection:
        Count = 1
        calls = 0
        def item(self, index):
            self.calls += 1
            return SimpleNamespace(FullName=str(source) if self.calls == 1 else 'Other unsaved document')
    app.Documents = RacingCollection()
    with pytest.raises(ComputerError, match='^windows_document_state_changed$') as raised:
        fd.document_present(source, lambda: None, window=window)
    assert raised.value.document_diagnostic == {'collection_changed': True, 'first_present': True, 'second_present': False}
    assert source.name not in json.dumps(raised.value.document_diagnostic)


@pytest.mark.parametrize('after_count,code', [(True, 'windows_document_collection_invalid'),
                                           (2, 'windows_document_state_changed')])
def test_each_round_revalidates_count_type_and_value_after_items(collection_application, source, after_count, code):
    window, app, _ = collection_application
    class Collection:
        reads = 0
        @property
        def Count(self):
            self.reads += 1
            return 1 if self.reads == 1 else after_count
        def item(self, index): return SimpleNamespace(FullName='Other unsaved document')
    app.Documents = Collection()
    with pytest.raises(ComputerError, match='^' + code + '$'):
        fd.document_present(source, lambda: None, window=window)


def test_collection_of_another_root_is_not_queried(collection_application, source):
    window, app, collection = collection_application
    app.ActiveWindow.Hwnd = 999
    with pytest.raises(ComputerError, match='^windows_document_identity_mismatch$'):
        fd.document_present(source, lambda: None, window=window)
    assert not collection.calls


def test_missing_application_anchor_never_means_closed(com, source, monkeypatch):
    window, _ = com
    def unavailable(win, check): raise ComputerError('windows_document_anchor_unavailable')
    monkeypatch.setattr(fd, '_document_application', unavailable)
    with pytest.raises(ComputerError, match='^windows_document_anchor_unavailable$'):
        fd.document_present(source, lambda: None, window=window)


def test_collection_item_facade_uses_only_bounded_propertyget():
    calls = []
    class Pointer:
        def GetIDsOfNames(self, lcid, name):
            assert name == 'Item'
            return 55
        def Invoke(self, *args):
            calls.append(args)
            return self
    wrapped = fd._ExistingObject(Pointer())
    assert isinstance(wrapped.item(1), fd._ExistingObject)
    assert calls == [(55, 0, 2, 1, 1)]
    for bad in (0, 65, True, '1'):
        with pytest.raises(ComputerError, match='windows_document_collection_invalid'):
            wrapped.item(bad)
    assert len(calls) == 1


@pytest.mark.parametrize('valid_root', [False, True])
def test_rot_application_anchor_requires_exact_full_name_and_both_hwnd_bindings(com, source, monkeypatch, valid_root):
    window, _ = com
    reads = []
    class Pointer:
        def __init__(self, **values): self.values = values
        def QueryInterface(self, iid): return self
        def GetIDsOfNames(self, lcid, name): return name
        def Invoke(self, name, lcid, flags, want_result):
            assert flags == 2
            reads.append(name)
            return self.values[name]
    native_window = Pointer(Hwnd=101 if valid_root else 999)
    app = Pointer(ActiveWindow=native_window)
    document = Pointer(FullName=str(source), ActiveWindow=native_window, Application=app)
    moniker = SimpleNamespace(GetDisplayName=lambda context, unused: str(source))
    table = SimpleNamespace(EnumRunning=lambda: iter([moniker]), GetObject=lambda current: document)
    monkeypatch.setitem(sys.modules, 'pythoncom', SimpleNamespace(GetRunningObjectTable=lambda: table,
                       CreateBindCtx=lambda reserved: None, IID_IDispatch='existing'))
    if valid_root:
        anchor, application = fd._document_application(window, lambda: None)
        assert isinstance(anchor, fd._ExistingObject) and isinstance(application, fd._ExistingObject)
    else:
        with pytest.raises(ComputerError, match='^windows_document_anchor_unavailable$'):
            fd._document_application(window, lambda: None)
    assert set(reads) <= {'FullName', 'Application', 'ActiveWindow', 'Hwnd'}


@pytest.mark.parametrize('mode', ['none', 'exact', 'duplicate', 'error'])
def test_production_rot_reads_only_unique_exact_moniker(source, monkeypatch, mode):
    calls = []
    class Moniker:
        def __init__(self, name): self.name = name
        def GetDisplayName(self, context, unused): return self.name
    pointer = SimpleNamespace(QueryInterface=lambda iid: SimpleNamespace())
    class Table:
        def EnumRunning(self):
            names = [str(source.parent / 'unapproved' / source.name)]
            if mode != 'none': names.append(str(source))
            if mode == 'duplicate': names.append(str(source))
            return iter(Moniker(name) for name in names)
        def GetObject(self, moniker):
            calls.append(moniker.name)
            if mode == 'error': raise RuntimeError('private document detail')
            return pointer
    monkeypatch.setitem(sys.modules, 'pythoncom', SimpleNamespace(GetRunningObjectTable=lambda: Table(),
                       CreateBindCtx=lambda reserved: None, IID_IDispatch='existing'))
    if mode == 'duplicate':
        with pytest.raises(ComputerError, match='^windows_document_rot_ambiguous$'):
            fd._exact_rot_document(source, lambda: None)
        assert not calls
    elif mode == 'error':
        with pytest.raises(ComputerError, match='^windows_document_rot_unreadable$'):
            fd._exact_rot_document(source, lambda: None)
        assert calls == [str(source)]
    else:
        result = fd._exact_rot_document(source, lambda: None)
        assert (result is None) == (mode == 'none')
        assert calls == ([] if mode == 'none' else [str(source)])


def test_takeover_check_is_never_swallowed(active, source):
    window, _, _ = active
    def check():
        raise ComputerError('user_takeover')
    with pytest.raises(ComputerError, match='user_takeover'):
        fd.document_is_active(window, source, check)


@pytest.fixture
def observation():
    dialog = {'hwnd': 200, 'title': '打开文件', 'class_name': 'Qt5QWindowIcon', 'dialog_kind': 'open_file'}
    field = {'type': 'ComboBox', 'name': '文件名称(N)', 'automation_id': '', 'id': 'filename',
             'owner_hwnd': 200, 'enabled': True, 'visible': True, 'patterns': ['set_value']}
    edit = {**field, 'type': 'Edit', 'name': '', 'id': 'edit',
            'ancestors': [{k: field[k] for k in ('type', 'name', 'automation_id')}]}
    button = {**field, 'type': 'Button', 'name': '打开(O)', 'id': 'open', 'patterns': ['invoke']}
    return {'application': 'wps', 'dialogs': [dialog], 'controls': [field, edit, button]}


def test_exact_owned_file_controls_only(observation):
    assert fd.filename_field(observation)['id'] == 'filename'
    assert fd.open_button(observation)['id'] == 'open'
    field, edit = fd.filename_edit(observation)
    assert field['id'] == 'filename' and edit['id'] == 'edit'
    assert not fd.is_open_dialog(observation['dialogs'][0])
    assert fd.is_open_dialog(observation['dialogs'][0], ownership_verified=True)


@pytest.mark.parametrize('change', ['duplicate', 'owner', 'disabled', 'wrong_name', 'truncated', 'unknown_class', 'bad_parent'])
def test_ambiguous_unbound_or_unavailable_file_controls_refused(observation, change):
    if change == 'duplicate':
        observation['controls'].append(dict(observation['controls'][0], id='other'))
    elif change == 'owner':
        observation['controls'][0]['owner_hwnd'] = 999
    elif change == 'disabled':
        observation['controls'][0]['enabled'] = False
    elif change == 'wrong_name':
        observation['controls'][0]['name'] = '任意字段'
    elif change == 'truncated':
        observation['truncated'] = True
    elif change == 'unknown_class':
        observation['dialogs'][0]['class_name'] = 'unknown'
    else:
        observation['controls'][1]['ancestors'].append({'type': 'Pane', 'name': '', 'automation_id': ''})
    with pytest.raises(ComputerError, match='windows_file_(?:control_ambiguous|dialog_invalid)'):
        fd.filename_edit(observation)
