"""Copy lifecycle with real packages and fake existing COM; never starts Office."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from agent_service import windows_document_copy as copy
from agent_service import windows_file_dialog as files
from agent_service import computer_uia as uia
from agent_service.computer import ComputerError, digest, read_record, write_new

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
P = 'http://schemas.microsoft.com/office/2006/xmlPackage'
CONTENT = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Override PartName="/word/document.xml" ContentType="'
           'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')


def document_xml(text):
    return f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'


def docx(path, text='original'):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', CONTENT)
        archive.writestr('word/document.xml', document_xml(text))
    return path


def flat(text):
    return (f'<pkg:package xmlns:pkg="{P}"><pkg:part pkg:name="/word/document.xml">'
            f'<pkg:xmlData>{document_xml(text)}</pkg:xmlData></pkg:part></pkg:package>')


@pytest.fixture
def prepared(tmp_path):
    source = docx(tmp_path / 'source.docx')
    token = copy.prepare(source, tmp_path / 'copies')
    manifest = copy.decode(token)
    return source, token, manifest


@pytest.fixture
def native(prepared, tmp_path, monkeypatch):
    source, token, manifest = prepared
    window = dict(hwnd=100, pid=123, created='2026', image='wps.exe', class_name='OpusApp', profile='observe_only')
    document = SimpleNamespace(FullName=str(source), ReadOnly=True, Saved=False,
                               ActiveWindow=SimpleNamespace(Hwnd=101), text='current dirty', calls=[],
                               fallback=False, fail=False, wrong_output=False, mutate_source=False)
    app = SimpleNamespace(ActiveDocument=document, ActiveWindow=document.ActiveWindow)
    document.Application = app
    class MissingMethod(Exception): hresult = -2147352570
    class Content:
        def GetIDsOfNames(self, lcid, name): return {'WordOpenXML': 1, 'Text': 2}[name]
        def Invoke(self, member, lcid, flags, want_result):
            assert flags == 2
            return flat(document.text) if member == 1 else document.text + '\r'
    class Pointer:
        def GetIDsOfNames(self, lcid, name):
            if name == 'SaveAs2' and document.fallback: raise MissingMethod()
            return {'Content': 10, 'SaveAs2': 11, 'SaveAs': 12}[name]
        def Invoke(self, member, lcid, flags, want_result, *args):
            if flags == 2:
                assert member == 10
                return Content()
            assert flags == 1 and want_result == 0 and member in (11, 12)
            output, file_format = args
            assert file_format == 16
            document.calls.append((member, output))
            docx(output, 'different' if document.wrong_output else document.text)
            document.FullName, document.Saved, document.ReadOnly = output, True, False
            if document.mutate_source: docx(source, 'modified original')
            if document.fail: raise RuntimeError('private unknown save error')
    document.pointer = Pointer()
    monkeypatch.setattr(files, 'bound_document', lambda win, path, check: (document,
        {'active': True, 'read_only': document.ReadOnly, 'saved': document.Saved}))
    monkeypatch.setattr(files, '_bound_hwnd', lambda hwnd, win: None if hwnd == 101 else (_ for _ in ()).throw(ComputerError('windows_document_identity_mismatch')))
    monkeypatch.setattr(files, '_visible_bound_hwnd', lambda hwnd, win: files._bound_hwnd(hwnd, win))
    monkeypatch.setattr(uia, '_recheck', lambda win: dict(window))
    operation = tmp_path / 'operation'
    operation.mkdir()
    request = {'op': 'act', 'action': 'save_document_copy', 'value': token, 'document_token': token,
               'selector': copy.SAVE_SELECTOR, 'window': window}
    write_new(operation / 'native-request.json', request)
    write_new(operation / 'native-intent.json', {'request_sha': digest(request), 'at': 1})
    return document, window, operation


def test_prepare_binds_pristine_directory_manifest_source_and_lease(prepared):
    source, token, manifest = prepared
    assert len(token) <= 4000 and Path(manifest['directory']).name == manifest['id']
    assert manifest['source'] == files.snapshot_file(source)
    assert copy.saved_receipt(token) is None


def test_manifest_change_invalidates_approved_token(prepared):
    _, token, manifest = prepared
    path = Path(manifest['directory']) / 'manifest.json'
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ComputerError, match='windows_copy_token_invalid'):
        copy.decode(token)


def test_source_bytes_and_mtime_are_independently_bound(prepared):
    source, token, _ = prepared
    info = source.stat()
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 10000))
    with pytest.raises(ComputerError, match='windows_copy_source_changed'):
        copy.decode(token)


@pytest.mark.parametrize('name', ['staging.docx', 'saved-document.docx', 'save-started.json', 'memory-before.xml'])
def test_decode_allows_existing_outputs_but_partial_work_never_retries(prepared, name):
    _, token, manifest = prepared
    (Path(manifest['directory']) / name).write_bytes(b'preserve this evidence')
    assert copy.decode(token) == manifest
    with pytest.raises(ComputerError, match='windows_copy_incomplete'):
        copy.saved_receipt(token)


def test_save_copy_persists_dirty_memory_without_changing_original(prepared, native):
    source, token, manifest = prepared
    document, window, operation = native
    before = source.read_bytes(), source.stat().st_mtime_ns
    receipt = copy.save_copy(window, token, lambda: None, operation)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert len(document.calls) == 1 and receipt['saved_before'] is False and receipt['saved_after'] is True
    assert receipt['staging']['sha256'] == receipt['final']['sha256']
    assert receipt['models']['memory_before'] != receipt['source_model']
    assert receipt['models']['memory_before'] == receipt['models']['final']
    assert copy.saved_receipt(token) == receipt
    assert copy.save_copy(window, token, lambda: None, operation) == receipt
    assert len(document.calls) == 1


def test_fallback_is_selected_before_the_only_save_invocation(prepared, native):
    _, token, _ = prepared
    document, window, operation = native
    document.fallback = True
    receipt = copy.save_copy(window, token, lambda: None, operation)
    assert receipt['method'] == 'SaveAs' and document.calls[0][0] == 12 and len(document.calls) == 1


def test_unknown_save_failure_is_retained_without_fallback_or_replay(prepared, native):
    _, token, manifest = prepared
    document, window, operation = native
    document.fail = True
    with pytest.raises(copy.CopyError, match='^windows_copy_save_unknown$') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is False
    directory = Path(manifest['directory'])
    assert (directory / 'save-started.json').is_file() and (directory / 'staging.docx').is_file()
    assert not (directory / 'saved.json').exists()
    with pytest.raises(ComputerError, match='windows_copy_incomplete'):
        copy.save_copy(window, token, lambda: None, operation)
    assert len(document.calls) == 1


def test_saved_output_semantics_must_match_captured_native_memory(prepared, native):
    _, token, manifest = prepared
    document, window, operation = native
    document.wrong_output = True
    with pytest.raises(ComputerError, match='windows_copy_saved_content_mismatch'):
        copy.save_copy(window, token, lambda: None, operation)
    assert not (Path(manifest['directory']) / 'saved.json').exists() and len(document.calls) == 1


def test_original_change_during_save_cannot_produce_completion(prepared, native):
    _, token, manifest = prepared
    document, window, operation = native
    document.mutate_source = True
    with pytest.raises(ComputerError, match='windows_copy_source_changed'):
        copy.save_copy(window, token, lambda: None, operation)
    assert not (Path(manifest['directory']) / 'saved.json').exists()


def test_modified_final_bytes_do_not_reuse_old_saved_receipt(prepared, native):
    _, token, _ = prepared
    _, window, operation = native
    receipt = copy.save_copy(window, token, lambda: None, operation)
    docx(receipt['final']['path'], 'tampered')
    with pytest.raises(ComputerError, match='windows_copy_receipt_invalid'):
        copy.saved_receipt(token)


def test_unrelated_or_missing_native_intent_never_calls_save(prepared, native):
    _, token, _ = prepared
    document, window, operation = native
    intent = operation / 'native-intent.json'
    intent.write_text('{"request_sha":"not-the-request"}', encoding='utf-8')
    with pytest.raises(ComputerError, match='windows_copy_native_intent_invalid'):
        copy.save_copy(window, token, lambda: None, operation)
    assert not document.calls


def test_lease_excludes_concurrent_copies(prepared):
    _, _, manifest = prepared
    with copy._lease(manifest):
        with pytest.raises(ComputerError, match='run_owner_active'):
            with copy._lease(manifest):
                pytest.fail('second lease acquired')


def test_content_readback_covers_changed_table_structure():
    first = flat('text')
    second = first.replace('<w:p><w:r><w:t>text</w:t></w:r></w:p>',
        '<w:tbl><w:tblGrid><w:gridCol w:w="100"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    assert copy._flat_model(first.encode())[0] != copy._flat_model(second.encode())[0]


@pytest.mark.parametrize('xml', [b'<!DOCTYPE x><x/>', b'<x/>',
    f'<pkg:package xmlns:pkg="{P}"><pkg:part pkg:name="/word/document.xml"><pkg:binaryData>YQ==</pkg:binaryData></pkg:part></pkg:package>'.encode(),
    flat('x').replace('<w:t>x</w:t>', '<w:instrText>PAGE</w:instrText>').encode()])
def test_unsupported_native_structures_fail_before_save(xml):
    with pytest.raises(ComputerError, match='windows_copy_(?:native_content_invalid|content_unsupported)'):
        copy._flat_model(xml)


def test_takeover_before_effect_leaves_no_save_attempt(prepared, native):
    _, token, manifest = prepared
    document, window, operation = native
    def check(): raise ComputerError('user_takeover')
    with pytest.raises(ComputerError, match='user_takeover'):
        copy.save_copy(window, token, check, operation)
    assert not document.calls and not (Path(manifest['directory']) / 'save-started.json').exists()


def test_atomic_publish_preserves_a_file_created_during_the_race(prepared, native, monkeypatch):
    _, token, manifest = prepared
    document, window, operation = native
    final = Path(manifest['directory']) / 'saved-document.docx'
    original_new_bytes = copy._new_bytes
    def race(path, data):
        original_new_bytes(path, data)
        if path.name == 'memory-after.xml':
            final.write_bytes(b'external file must remain')
    monkeypatch.setattr(copy, '_new_bytes', race)
    with pytest.raises(copy.CopyError, match='windows_copy_failed') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is False
    assert final.read_bytes() == b'external file must remain'
    assert len(document.calls) == 1 and not (final.parent / 'saved.json').exists()


def test_wrong_request_window_never_authorizes_copy(prepared, native):
    _, token, _ = prepared
    document, window, operation = native
    request = read_record(operation / 'native-request.json')
    request['window'] = {**request['window'], 'pid': 999}
    (operation / 'native-request.json').write_text(json.dumps(request), encoding='utf-8')
    (operation / 'native-intent.json').write_text(json.dumps({'request_sha': digest(request)}), encoding='utf-8')
    with pytest.raises(ComputerError, match='windows_copy_native_intent_invalid'):
        copy.save_copy(window, token, lambda: None, operation)
    assert not document.calls


def test_missing_lxml_in_both_runtime_and_base_is_explicit_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path))
    monkeypatch.setattr(copy.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(copy.importlib.machinery.PathFinder, 'find_spec', lambda name, roots: None)
    before = list(sys.path)
    with pytest.raises(ComputerError, match='^windows_copy_dependency_unavailable$'):
        copy._ensure_lxml()
    assert sys.path == before


def test_lxml_fallback_reuses_only_same_base_package_without_site_path(tmp_path, monkeypatch):
    package = tmp_path / 'Lib' / 'site-packages' / 'lxml'
    package.mkdir(parents=True)
    origin = package / '__init__.py'
    origin.write_text('loaded_from_base = True\n', encoding='utf-8')
    spec = copy.importlib.util.spec_from_file_location('lxml', origin, submodule_search_locations=[str(package)])
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path))
    monkeypatch.setattr(copy.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(copy.importlib.machinery.PathFinder, 'find_spec', lambda name, roots: spec)
    monkeypatch.setitem(sys.modules, 'lxml', sys.modules.get('lxml'))
    before = list(sys.path)
    copy._ensure_lxml()
    assert sys.modules['lxml'].loaded_from_base is True
    assert Path(sys.modules['lxml'].__file__) == origin
    assert sys.path == before and str(package.parent) not in sys.path


def test_lxml_fallback_rejects_spec_from_another_installation(tmp_path, monkeypatch):
    outside = tmp_path / 'elsewhere' / 'lxml'
    outside.mkdir(parents=True)
    origin = outside / '__init__.py'
    origin.write_text('raise RuntimeError("must not execute")\n', encoding='utf-8')
    spec = copy.importlib.util.spec_from_file_location('lxml', origin, submodule_search_locations=[str(outside)])
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path / 'base'))
    monkeypatch.setattr(copy.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(copy.importlib.machinery.PathFinder, 'find_spec', lambda name, roots: spec)
    with pytest.raises(ComputerError, match='^windows_copy_dependency_source_invalid$'):
        copy._ensure_lxml()


def test_lxml_load_failure_restores_existing_module_namespace(tmp_path, monkeypatch):
    package = tmp_path / 'Lib' / 'site-packages' / 'lxml'
    package.mkdir(parents=True)
    origin = package / '__init__.py'
    origin.write_text('raise RuntimeError("private import failure")\n', encoding='utf-8')
    spec = copy.importlib.util.spec_from_file_location('lxml', origin, submodule_search_locations=[str(package)])
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path))
    monkeypatch.setattr(copy.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(copy.importlib.machinery.PathFinder, 'find_spec', lambda name, roots: spec)
    previous = {key: value for key, value in sys.modules.items() if key == 'lxml' or key.startswith('lxml.')}
    with pytest.raises(ComputerError, match='^windows_copy_dependency_invalid$'):
        copy._ensure_lxml()
    assert {key: value for key, value in sys.modules.items() if key == 'lxml' or key.startswith('lxml.')} == previous


def test_volatile_raw_flat_xml_does_not_override_stable_native_semantics(prepared, native, monkeypatch):
    _, token, _ = prepared
    document, window, operation = native
    original_capture = copy._capture
    calls = []
    def capture(bound, check):
        data, model = original_capture(bound, check)
        calls.append(1)
        return data.replace(b'<pkg:package ', f'<pkg:package probe-sequence="{len(calls)}" '.encode()), model
    monkeypatch.setattr(copy, '_capture', capture)
    receipt = copy.save_copy(window, token, lambda: None, operation)
    assert receipt['memory_before']['sha256'] != receipt['memory_after']['sha256']
    assert receipt['models']['memory_before'] == receipt['models']['memory_after']
    assert len(document.calls) == 1


def test_semantic_change_before_save_has_trusted_no_effect_error(prepared, native, monkeypatch):
    _, token, manifest = prepared
    document, window, operation = native
    original_capture = copy._capture
    calls = []
    def capture(bound, check):
        if calls: document.text = 'different current content'
        calls.append(1)
        return original_capture(bound, check)
    monkeypatch.setattr(copy, '_capture', capture)
    with pytest.raises(copy.CopyError, match='windows_copy_document_changed') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is True
    assert raised.value.copy_diagnostic['model_changed'] is True
    assert raised.value.copy_diagnostic['save_method_invoke_started'] is False
    assert 'different current content' not in json.dumps(raised.value.copy_diagnostic)
    assert not document.calls and not (Path(manifest['directory']) / 'save-started.json').exists()


@pytest.fixture
def workbook_copy(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from test_windows_workbook_model import fixture, native as native_workbook
    source = tmp_path / 'source.xlsx'
    fixture(source)
    token = copy.prepare(source, tmp_path / 'copies')
    manifest = copy.decode(token)
    window = dict(hwnd=100, pid=123, created='2026', image='wps.exe', class_name='XLMAIN', profile='observe_only')
    document = SimpleNamespace(FullName=str(source), ReadOnly=True, Saved=False, calls=[], members=[],
                               fail=False, wrong_output=None, wrong_memory=False, mutate_source=False)
    document.Windows = SimpleNamespace(Count=1, item=lambda index: SimpleNamespace(Hwnd=101))
    document.Application = SimpleNamespace(ActiveWorkbook=document, Hwnd=101,
                                           ActiveWindow=SimpleNamespace(Hwnd=101))
    raw = native_workbook(manifest['source_model'])
    document.raw = raw
    class Pointer:
        def GetIDsOfNames(self, lcid, name):
            document.members.append(name)
            assert name != 'SaveAs2', 'XLSX never probes SaveAs2'
            return 51 if name == 'SaveAs' else raw.GetIDsOfNames(lcid, name)
        def Invoke(self, member, lcid, flags, want_result, *args):
            if flags == 2:
                return raw.Invoke(member, lcid, flags, want_result, *args)
            assert (member, lcid, flags, want_result) == (51, 0, 1, 0)
            output, file_format = args
            assert file_format == 51 and Path(output).name == 'staging.xlsx'
            document.calls.append((member, output))
            if document.wrong_output:
                book = load_workbook(source)
                book['明细']['B4'] = None if document.wrong_output == 'lost' else 30
                book.save(output)
                book.close()
            else:
                Path(output).write_bytes(source.read_bytes())
            document.FullName, document.Saved, document.ReadOnly = output, True, False
            if document.wrong_memory:
                raw.sheets[0].cells['B4'].properties.update(HasFormula=False, Formula=30, Value2=30)
            if document.mutate_source:
                book = load_workbook(source)
                book['明细']['B2'] = 11
                book.save(source)
                book.close()
            if document.fail:
                raise RuntimeError('unknown workbook save effect')
    document.pointer = Pointer()
    monkeypatch.setattr(files, 'bound_document', lambda win, path, check: (document,
        {'active': True, 'read_only': document.ReadOnly, 'saved': document.Saved}))
    monkeypatch.setattr(files, '_bound_hwnd', lambda hwnd, win: None if hwnd == 101 else
                        (_ for _ in ()).throw(ComputerError('windows_document_identity_mismatch')))
    monkeypatch.setattr(files, '_visible_bound_hwnd', lambda hwnd, win: files._bound_hwnd(hwnd, win))
    monkeypatch.setattr(uia, '_recheck', lambda win: dict(window))
    operation = tmp_path / 'operation'
    operation.mkdir()
    request = {'op': 'act', 'action': 'save_document_copy', 'value': token, 'document_token': token,
               'selector': copy.SAVE_SELECTOR, 'window': window}
    write_new(operation / 'native-request.json', request)
    write_new(operation / 'native-intent.json', {'request_sha': digest(request), 'at': 1})
    return source, token, manifest, document, window, operation


def test_workbook_save_preserves_formulas_and_original_and_recovers_once(workbook_copy):
    source, token, manifest, document, window, operation = workbook_copy
    before = source.read_bytes(), source.stat().st_mtime_ns
    receipt = copy.save_copy(window, token, lambda: None, operation)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert receipt['method'] == 'SaveAs' and len(document.calls) == 1
    assert all(value == manifest['source_model'] for value in receipt['models'].values())
    assert Path(receipt['final']['path']).name == copy.output_filename_from_token(token) == 'saved-workbook.xlsx'
    assert Path(receipt['memory_before']['path']).name == 'memory-before.json'
    assert Path(receipt['memory_after']['path']).name == 'memory-after.json'
    assert receipt['staging']['sha256'] == receipt['final']['sha256']
    directory = Path(manifest['directory'])
    assert not (directory / 'memory-before.xml').exists() and not (directory / 'staging.docx').exists()
    assert copy.saved_receipt(token) == receipt
    assert copy.save_copy(window, token, lambda: None, operation) == receipt
    assert len(document.calls) == 1 and 'SaveAs2' not in document.members


@pytest.mark.parametrize('change', ['lost', 'static', 'different_formula'])
def test_workbook_formula_loss_in_memory_is_rejected_before_save(workbook_copy, change):
    _, token, manifest, document, window, operation = workbook_copy
    cell = document.raw.sheets[0].cells['B4'].properties
    if change == 'different_formula':
        cell['Formula'] = '=B2+B3'
    else:
        value = None if change == 'lost' else 30
        cell.update(HasFormula=False, Formula=value, Value2=value)
    with pytest.raises(copy.CopyError, match='windows_copy_native_content_mismatch') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is True and not document.calls
    assert not (Path(manifest['directory']) / 'save-started.json').exists()


@pytest.mark.parametrize('change', ['lost', 'static'])
def test_workbook_formula_loss_in_saved_package_never_completes_or_replays(workbook_copy, change):
    source, token, manifest, document, window, operation = workbook_copy
    before = source.read_bytes()
    document.wrong_output = change
    with pytest.raises(copy.CopyError, match='windows_copy_saved_content_mismatch') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is False and source.read_bytes() == before
    assert len(document.calls) == 1 and not (Path(manifest['directory']) / 'saved.json').exists()
    with pytest.raises(copy.CopyError, match='windows_copy_incomplete'):
        copy.save_copy(window, token, lambda: None, operation)
    assert len(document.calls) == 1


def test_workbook_memory_changed_during_save_never_completes(workbook_copy):
    _, token, manifest, document, window, operation = workbook_copy
    document.wrong_memory = True
    with pytest.raises(copy.CopyError, match='windows_copy_native_content_mismatch') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is False
    assert len(document.calls) == 1 and not (Path(manifest['directory']) / 'saved.json').exists()


def test_workbook_unknown_save_is_nonretryable(workbook_copy):
    _, token, manifest, document, window, operation = workbook_copy
    document.fail = True
    with pytest.raises(copy.CopyError, match='windows_copy_save_unknown') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is False
    directory = Path(manifest['directory'])
    assert (directory / 'save-started.json').is_file() and (directory / 'staging.xlsx').is_file()
    with pytest.raises(copy.CopyError, match='windows_copy_incomplete'):
        copy.save_copy(window, token, lambda: None, operation)
    assert len(document.calls) == 1


@pytest.mark.parametrize('name', ['staging.xlsx', 'saved-workbook.xlsx', 'memory-before.json', 'memory-after.json'])
def test_workbook_partial_output_never_replays(workbook_copy, name):
    _, token, manifest, document, window, operation = workbook_copy
    (Path(manifest['directory']) / name).write_bytes(b'preserve partial evidence')
    with pytest.raises(copy.CopyError, match='windows_copy_incomplete') as raised:
        copy.save_copy(window, token, lambda: None, operation)
    assert raised.value.no_external_effect is True and not document.calls


def test_workbook_original_changed_during_save_cannot_complete(workbook_copy):
    _, token, manifest, document, window, operation = workbook_copy
    document.mutate_source = True
    with pytest.raises(copy.CopyError, match='windows_copy_source_changed'):
        copy.save_copy(window, token, lambda: None, operation)
    assert len(document.calls) == 1 and not (Path(manifest['directory']) / 'saved.json').exists()


def test_workbook_raw_evidence_is_revalidated_on_resume(workbook_copy):
    _, token, _, _, window, operation = workbook_copy
    receipt = copy.save_copy(window, token, lambda: None, operation)
    path = Path(receipt['memory_after']['path'])
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ComputerError, match='windows_copy_receipt_invalid'):
        copy.saved_receipt(token)


def test_word_output_paths_and_receipt_contract_remain_unchanged(prepared, native):
    _, token, manifest = prepared
    _, window, operation = native
    assert copy.output_filename_from_token(token) == 'saved-document.docx'
    assert {key: path.name for key, path in copy.output_paths(manifest).items()} == {
        'staging': 'staging.docx', 'final': 'saved-document.docx',
        'memory_before': 'memory-before.xml', 'memory_after': 'memory-after.xml'}
    receipt = copy.save_copy(window, token, lambda: None, operation)
    assert receipt['schema'] == 1 and copy.saved_receipt(token) == receipt
