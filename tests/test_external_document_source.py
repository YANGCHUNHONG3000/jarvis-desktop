"""External complex Word snapshots and read-only native supervision."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import zipfile

from lxml import etree as ET
import pytest

from agent_service import external_document_source as external, office_source, source_intake
from agent_service import external_document_source_host as host


COMPLEX = (Path(__file__).resolve().parents[1] / "data/assistant_tasks"
           / "task-1d52d21e59a241e693601607106075fc/edit/edited-document.docx")


def _complex(tmp_path):
    if not COMPLEX.is_file():
        pytest.skip("preserved complex Word evidence unavailable")
    target = tmp_path / "selected.docx"
    target.write_bytes(COMPLEX.read_bytes())
    return target


def _fake_native(snapshot, directory, *, stop=lambda: False, deadline=None):
    observed, _ = external._parse(snapshot)
    receipt = {"version": 1, "source_sha256": external._digest(snapshot),
               "observed_sha256": external._observation_hash(observed),
               "application_verified": True, "file_unchanged": True,
               "app": {"progid": "KWPS.Application", "name": "Controlled WPS", "version": "1"}}
    office_source._write_json_new(directory / "native-request.json", {"sha256": receipt["source_sha256"]})
    office_source._write_json_new(directory / "source-native.json", receipt)
    return receipt


def test_complex_selected_word_snapshot_and_repeat_verify_without_native(tmp_path, monkeypatch):
    original = _complex(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr("agent_service.external_document_source_host.native_snapshot_supervised", _fake_native)
    public = source_intake.snapshot_selected_source(original, task)
    assert public["kind"] == "document_structure"
    assert public["coverage"] == {"scope": "verified_external_document_structure", "records": 27,
                                  "tables": 3, "cells": 10, "paragraphs": 14, "layout_preserved": False}
    assert public["sha256"] == sha256(original.read_bytes()).hexdigest()
    assert public["bytes"] == original.stat().st_size
    assert (task / "sources/source.docx").read_bytes() == original.read_bytes()
    assert not (task / "sources/provenance.json").exists()
    original.unlink()
    monkeypatch.setattr("agent_service.external_document_source_host.native_snapshot_supervised",
                        lambda *args, **kwargs: pytest.fail("native called during verification"))
    assert external.verify_external_document(task) == public


@pytest.mark.parametrize("damage", ["source", "public", "native", "request", "link"])
def test_complex_snapshot_rejects_changed_evidence(tmp_path, monkeypatch, damage):
    original = _complex(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr("agent_service.external_document_source_host.native_snapshot_supervised", _fake_native)
    source_intake.snapshot_selected_source(original, task)
    directory = task / "sources"
    if damage == "source":
        with (directory / "source.docx").open("ab") as stream:
            stream.write(b"changed")
    elif damage == "public":
        record = directory / "source.json"
        public = json.loads(record.read_text(encoding="utf-8"))
        public["observed"]["records"][0]["text"] = "changed"
        record.write_text(json.dumps(public), encoding="utf-8")
    elif damage == "native":
        record = directory / "source-native.json"
        receipt = json.loads(record.read_text(encoding="utf-8"))
        receipt["app"]["name"] = "changed"
        record.write_text(json.dumps(receipt), encoding="utf-8")
    elif damage == "request":
        (directory / "native-request.json").write_text('{"sha256":"0"}', encoding="utf-8")
    else:
        record = directory / "source-native.json"
        copy = tmp_path / "outside-native.json"
        copy.write_bytes(record.read_bytes())
        record.unlink()
        record.symlink_to(copy)
    with pytest.raises(ValueError):
        external.verify_external_document(task)


def test_simple_word_uses_existing_intake(tmp_path, monkeypatch):
    complex_path = _complex(tmp_path)
    simple = tmp_path / "simple.docx"
    with zipfile.ZipFile(complex_path) as source, zipfile.ZipFile(simple, "w") as output:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == "word/document.xml":
                tree = ET.fromstring(data)
                body = tree.find(f"{{{office_source._W}}}body")
                for node in list(body):
                    if node.tag == f"{{{office_source._W}}}tbl":
                        body.remove(node)
                data = ET.tostring(tree, encoding="UTF-8", xml_declaration=True)
            output.writestr(member, data)
    assert source_intake.needs_document_source(simple) is False
    monkeypatch.setattr("agent_service.external_document_source.snapshot_external_document",
                        lambda *args, **kwargs: pytest.fail("complex route used for simple Word"))
    task = tmp_path / "task"
    task.mkdir()
    public = source_intake.snapshot_selected_source(simple, task)
    assert public["kind"] == "document"
    assert set(public) == {"source_id", "kind", "sha256", "bytes", "display_name", "coverage", "observed"}


def test_unrepresented_word_field_rejected_before_snapshot(tmp_path):
    original = _complex(tmp_path)
    malformed = tmp_path / "field.docx"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(malformed, "w") as output:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == "word/document.xml":
                tree = ET.fromstring(data)
                run = tree.find(f".//{{{office_source._W}}}r")
                ET.SubElement(run, f"{{{office_source._W}}}fldChar")
                data = ET.tostring(tree, encoding="UTF-8", xml_declaration=True)
            output.writestr(member, data)
    task = tmp_path / "task"
    task.mkdir()
    with pytest.raises(ValueError):
        source_intake.snapshot_selected_source(malformed, task)
    assert not (task / "sources").exists()


def test_native_check_closes_only_owned_document(tmp_path, monkeypatch):
    snapshot = _complex(tmp_path)
    events = []
    class Owned:
        def Close(self, *, SaveChanges):
            events.append(("close", SaveChanges))
    owned = Owned()
    app = SimpleNamespace(Documents=SimpleNamespace(Open=lambda path, ReadOnly: owned),
                          Name="Controlled WPS", Version="1")
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(
        CoInitialize=lambda: events.append("init"), CoUninitialize=lambda: events.append("uninit")))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=SimpleNamespace(
        DispatchEx=lambda progid: app)))
    monkeypatch.setitem(sys.modules, "win32com.client", sys.modules["win32com"].client)
    monkeypatch.setattr(external.office_edit, "_native_complex_word",
                        lambda document, body, check: (events.append("checked"), check()))
    receipt = external._native_snapshot(snapshot)
    assert receipt["source_sha256"] == external._digest(snapshot)
    assert events == ["init", "checked", ("close", False), "uninit"]


def test_supervisor_cancels_owned_worker_without_accepting_receipt(tmp_path, monkeypatch):
    snapshot = _complex(tmp_path)
    directory = tmp_path / "sources"
    directory.mkdir()
    owned = directory / "source.docx"
    owned.write_bytes(snapshot.read_bytes())
    events = []

    class Input:
        def write(self, data):
            events.append(("start", data))
        def flush(self):
            pass
        def close(self):
            pass

    class Process:
        stdin = Input()
        returncode = None
        def poll(self):
            return None
        def wait(self, timeout):
            events.append(("wait", timeout))
            return 1

    monkeypatch.setattr("run_jarvis_desktop.desktop_python", lambda: "python")
    monkeypatch.setattr(host.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(host, "_stop_worker", lambda process: events.append("stop_worker"))
    calls = iter((False, True))
    with pytest.raises(external.ExternalDocumentError, match="external_document_cancelled"):
        host.native_snapshot_supervised(owned, directory, stop=lambda: next(calls))
    assert (directory / "native-cancel.flag").exists()
    assert events == [("start", b"1"), ("wait", 2), "stop_worker"]


def _desktop_python_or_skip():
    from run_jarvis_desktop import desktop_python
    try:
        return desktop_python()
    except RuntimeError:
        pytest.skip("desktop Python unavailable")


def test_desktop_child_import_smoke():
    result = subprocess.run([_desktop_python_or_skip(), "-B", "-c",
                             "import agent_service.external_document_source_host; print('import_ok')"],
                            cwd=host.ROOT, env=host._child_environment(),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import_ok"


def test_desktop_child_exits_cleanly_after_receipt_with_parent_pipe_open(tmp_path):
    source = _complex(tmp_path)
    directory = tmp_path / "sources"
    directory.mkdir()
    snapshot = directory / "source.docx"
    shutil.copyfile(source, snapshot)
    office_source._write_json_new(directory / "native-request.json", {"sha256": external._digest(snapshot)})
    script = ("import sys; from agent_service import external_document_source_host as h; "
              "h._native_snapshot=lambda path,stop: {'controlled':True}; "
              "raise SystemExit(h.worker(sys.argv[1],10))")
    process = subprocess.Popen([_desktop_python_or_skip(), "-B", "-c", script, str(directory)],
                               cwd=host.ROOT, env=host._child_environment(), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b"1")
        process.stdin.flush()
        process.wait(timeout=15)
        stderr = process.stderr.read().decode("utf-8", "replace")
        assert process.returncode == 0, stderr
        assert json.loads((directory / "source-native.json").read_text(encoding="utf-8")) == {
            "controlled": True}
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
