import hashlib

from run_jarvis_assistant import _office_artifacts, _task_command


def _artifact(path, step="s1"):
    return {"step": step, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_office_artifacts_accepts_verified_docx_and_xlsx_under_task_root(tmp_path):
    document = tmp_path / "workflow-1" / "s1" / "report.docx"
    spreadsheet = tmp_path / "workflow-1" / "s2" / "summary.xlsx"
    document.parent.mkdir(parents=True)
    spreadsheet.parent.mkdir(parents=True)
    document.write_bytes(b"docx package")
    spreadsheet.write_bytes(b"xlsx package")

    result = {"kind": "office", "ok": True, "state": "succeeded",
              "artifacts": [_artifact(document, "s1"), _artifact(spreadsheet, "s2")]}

    assert _office_artifacts(result, tmp_path) == [
        ("s1 · 文档 (.docx)", document.resolve()),
        ("s2 · 表格 (.xlsx)", spreadsheet.resolve()),
    ]


def test_office_artifacts_keeps_verified_partial_results_when_workflow_failed(tmp_path):
    partial = tmp_path / "workflow-2" / "s1" / "partial.docx"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"verified completed step")
    result = {"kind": "office", "ok": False, "state": "failed",
              "completed": ["s1"], "artifacts": [_artifact(partial)]}

    assert _office_artifacts(result, tmp_path) == [("s1 · 文档 (.docx)", partial.resolve())]


def test_office_artifacts_rejects_unverified_paths_formats_and_steps(tmp_path):
    valid = tmp_path / "inside.docx"
    outside = tmp_path.parent / "outside.xlsx"
    valid.write_bytes(b"valid")
    outside.write_bytes(b"outside")
    wrong_hash = _artifact(valid)
    wrong_hash["sha256"] = "0" * 64

    result = {"kind": "office", "artifacts": [
        _artifact(outside), wrong_hash,
        {"step": "step-2", "path": str(valid), "sha256": _artifact(valid)["sha256"]},
        {"step": "../s1", "path": str(valid), "sha256": _artifact(valid)["sha256"]},
        {"step": "s3", "path": str(valid.with_suffix(".txt")), "sha256": "0" * 64},
    ]}

    assert _office_artifacts(result, tmp_path) == []


def test_office_artifacts_rejects_files_over_twenty_megabytes(tmp_path):
    large = tmp_path / "large.xlsx"
    with large.open("wb") as file:
        file.truncate(20_000_001)
    result = {"kind": "office", "artifacts": [
        {"step": "s1", "path": str(large),
         "sha256": hashlib.sha256(large.read_bytes()).hexdigest()}
    ]}

    assert _office_artifacts(result, tmp_path) == []


def test_task_command_only_adds_exact_selected_source_file_when_present(tmp_path):
    request = tmp_path / "request.txt"
    task_root = tmp_path / ("task-" + "a" * 32)
    source = tmp_path / "My source data.xlsx"

    without_source = _task_command(request, task_root)
    with_source = _task_command(request, task_root, str(source))

    assert "--source-file" not in without_source
    assert with_source[-2:] == ["--source-file", str(source)]
