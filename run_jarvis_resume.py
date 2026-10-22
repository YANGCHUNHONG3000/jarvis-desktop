"""CLI for an explicit Office history continuation; stdout is one JSON object."""

import argparse
import json
from pathlib import Path
import re

from agent_service.office_resume import resume_task


ROOT = Path(__file__).resolve().parent
_SAFE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments_invalid")

    def exit(self, status=0, message=None):
        raise ValueError("arguments_invalid")

    def print_help(self, file=None):
        return None


def main(argv=None, *, execute=None, repo_root=None):
    parser = _Parser(description="Continue one verified Office task")
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--attempt-id", required=True)
    try:
        args = parser.parse_args(argv)
        if not Path(args.task_root).is_absolute():
            raise ValueError("task_root_invalid")
        repository = Path(repo_root).resolve() if repo_root is not None else ROOT
        base = repository / "data" / "assistant_tasks"
        result = resume_task(args.task_root, base, args.token, args.attempt_id,
                             execute=execute)
    except KeyboardInterrupt:
        result = {"ok": False, "state": "unknown", "error": "interrupted"}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else "resume_failed"
        result = {"ok": False, "state": "blocked",
                  "error": code if _SAFE.fullmatch(code) else "resume_failed"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True and result.get("state") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
