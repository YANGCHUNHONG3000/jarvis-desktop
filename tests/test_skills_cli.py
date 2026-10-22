"""CLI publication contract without Office, model, or network."""

import json

from run_jarvis_skills import main


def call(capsys, *args):
    code = main(list(map(str, args)))
    output = capsys.readouterr().out.strip()
    return code, json.loads(output)


def test_cli_register_offline_review_status_remains_disabled(tmp_path, capsys):
    store, base = tmp_path / "skills", tmp_path / "tasks"
    candidate = {"schema": 1, "id": "copy-doc", "version": 1, "name": "副本",
                 "recipe": "document_copy", "sum_columns": [], "draft": False}
    manifest = tmp_path / "candidate.json"
    manifest.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    options = ("--store", store, "--base", base)
    code, registered = call(capsys, *options, "register", manifest)
    assert code == 0 and registered["enabled"] is False
    digest = registered["sha256"]
    code, offline = call(capsys, *options, "offline", "copy-doc", digest)
    assert code == 0 and offline["kind"] == "offline_contract"
    code, review = call(capsys, *options, "review", "copy-doc", digest,
                        "--reviewer", "maintainer", "--note", "read-only review")
    assert code == 0 and review["kind"] == "maintainer_review"
    code, status = call(capsys, *options, "status", "copy-doc")
    assert code == 0 and status["enabled"] is False
    code, blocked = call(capsys, *options, "activate", "copy-doc", digest)
    assert code == 1 and blocked == {"ok": False, "state": "blocked", "error": "skill_operation_rejected"}


def test_cli_rejects_candidate_attempt_to_add_code(tmp_path, capsys):
    store, base = tmp_path / "skills", tmp_path / "tasks"
    candidate = {"schema": 1, "id": "copy-doc", "version": 1, "name": "副本",
                 "recipe": "document_copy", "sum_columns": [], "draft": False,
                 "command": "run arbitrary code"}
    manifest = tmp_path / "candidate.json"
    manifest.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    code, result = call(capsys, "--store", store, "--base", base, "register", manifest)
    assert code == 1 and result["state"] == "blocked"
    assert not store.exists()


def test_cli_run_digest_pin_rejects_before_task_creation(tmp_path, capsys):
    store, base = tmp_path / "skills", tmp_path / "tasks"
    candidate = {"schema": 1, "id": "copy-doc", "version": 1, "name": "副本",
                 "recipe": "document_copy", "sum_columns": [], "draft": False}
    manifest = tmp_path / "candidate.json"
    manifest.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    options = ("--store", store, "--base", base)
    _, registered = call(capsys, *options, "register", manifest)
    digest = registered["sha256"]
    call(capsys, *options, "offline", "copy-doc", digest)
    call(capsys, *options, "review", "copy-doc", digest,
         "--reviewer", "maintainer", "--note", "fixed recipe")
    target = base / ("task-" + "a" * 32)
    code, result = call(capsys, *options, "run", "copy-doc",
                        "--source-file", tmp_path / "unused.docx", "--trial-sha256", digest,
                        "--expected-sha256", "f" * 64, "--task-root", target)
    assert code == 1 and result["state"] == "blocked" and not target.exists()
