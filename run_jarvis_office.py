"""CLI for owned, durable Office workflows. No model or voice runtime is invoked."""

import argparse
import json
from pathlib import Path
import re
import uuid

from agent_service.office_workflow import run_office_workflow, validate_steps


ROOT = Path(__file__).resolve().parent
_WORKFLOW = re.compile(r"workflow-[0-9a-f]{32}\Z")
_TASK = re.compile(r"task-[0-9a-f]{32}\Z")
_MAX_REQUEST = 1_000_000


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments_invalid")

    def print_help(self, file=None):
        # This JSON CLI reserves stdout for exactly one machine-readable result.
        return None

    def exit(self, status=0, message=None):
        raise ValueError("arguments_invalid")


def _linked(path):
    """Inspect each lexical ancestor before resolving it."""
    current = Path(path).absolute()
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            return True
        if current == current.parent:
            return False
        current = current.parent


def _resume_root(value, repo_root):
    candidate = Path(value).absolute()
    if _linked(candidate) or not candidate.is_dir() or not _WORKFLOW.fullmatch(candidate.name):
        raise ValueError("resume_path_invalid")
    resolved = candidate.resolve()
    base = Path(repo_root).resolve()
    office = base / "data" / "office_runs"
    tasks = base / "data" / "assistant_tasks"
    if resolved.parent != office and not (
            resolved.parent.parent == tasks and _TASK.fullmatch(resolved.parent.name)):
        raise ValueError("resume_path_invalid")
    manifest = resolved / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > _MAX_REQUEST:
        raise ValueError("manifest_invalid")
    return resolved, manifest


def _request_steps(value):
    if type(value) is not str or len(value.encode("utf-8")) > _MAX_REQUEST:
        raise ValueError("request_invalid")
    try:
        request = json.loads(value)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("request_invalid") from exc
    if type(request) is not dict or set(request) != {"steps"}:
        raise ValueError("request_invalid")
    return validate_steps(request["steps"])


def _manifest_steps(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("manifest_invalid") from exc
    if type(value) is not dict or set(value) != {"version", "steps", "steps_sha256"} or value["version"] != 1:
        raise ValueError("manifest_invalid")
    steps = validate_steps(value["steps"])
    if steps != value["steps"]:
        raise ValueError("manifest_invalid")
    return steps


def _read_result(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_REQUEST:
        raise ValueError("result_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("result_invalid") from exc
    if type(value) is not dict:
        raise ValueError("result_invalid")
    return value


def _list_workflows(base):
    roots = [base / "data" / "office_runs"]
    tasks = base / "data" / "assistant_tasks"
    if tasks.is_dir() and not _linked(tasks):
        roots.extend(path for path in tasks.iterdir()
                     if path.is_dir() and _TASK.fullmatch(path.name) and not _linked(path))
    workspaces = []
    for parent in roots:
        if not parent.is_dir() or _linked(parent):
            continue
        for path in parent.iterdir():
            if not path.is_dir() or not _WORKFLOW.fullmatch(path.name) or _linked(path):
                continue
            manifest = path / "manifest.json"
            if not manifest.is_file() or manifest.is_symlink():
                continue
            try:
                _manifest_steps(manifest)
                latest = _read_result(path / "result.json")
            except ValueError:
                continue
            workspaces.append({"root": str(path.resolve()),
                               "state": latest.get("state", "pending") if latest else "pending"})
    return sorted(workspaces, key=lambda item: item["root"])


def main(argv=None, *, execute=None, repo_root=None):
    parser = _Parser(description="Run or resume one bounded Office workflow", add_help=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--request", help='JSON object with exactly one "steps" array')
    group.add_argument("--resume", metavar="WORKFLOW", help="Existing owned workflow directory")
    group.add_argument("--show", metavar="WORKFLOW", help="Read an owned workflow without executing it")
    group.add_argument("--list", action="store_true", help="List owned workflows without executing them")
    try:
        args = parser.parse_args(argv)
        base = Path(repo_root).resolve() if repo_root is not None else ROOT
        if args.list:
            result = {"ok": True, "state": "listed", "workflows": _list_workflows(base)}
        elif args.show is not None:
            workflow_root, manifest = _resume_root(args.show, base)
            steps = _manifest_steps(manifest)
            result = {"ok": True, "state": "shown", "root": str(workflow_root),
                      "steps": steps, "latest_result": _read_result(workflow_root / "result.json")}
        elif args.resume is not None:
            workflow_root, manifest = _resume_root(args.resume, base)
            steps = _manifest_steps(manifest)
            result = run_office_workflow(steps, workflow_root, execute=execute, resume=True)
        else:
            steps = _request_steps(args.request)
            output = base / "data" / "office_runs"
            if _linked(output):
                raise ValueError("output_path_invalid")
            output.mkdir(parents=True, exist_ok=True)
            workflow_root = output / ("workflow-" + uuid.uuid4().hex)
            result = run_office_workflow(steps, workflow_root, execute=execute)
    except KeyboardInterrupt:
        # The durable workflow intent determines whether an external effect ran.
        result = {"ok": False, "state": "unknown", "error": "interrupted"}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else "office_cli_failed"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "office_cli_failed"
        result = {"ok": False, "state": "failed", "error": code}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True and result.get("state") in {"succeeded", "listed", "shown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
