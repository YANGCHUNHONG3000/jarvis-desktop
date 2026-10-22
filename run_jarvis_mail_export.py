"""Run one explicit local mail-to-Word request; stdout is one JSON object."""

import argparse
import json
from pathlib import Path
import re

from agent_service.mail_export import run_mail_export


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
    parser = _Parser(description="Export one already-read mail to a local document")
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--request-sha256", required=True)
    try:
        args = parser.parse_args(argv)
        if not Path(args.task_root).is_absolute():
            raise ValueError("mail_export_task_scope_invalid")
        repository = Path(repo_root).resolve() if repo_root is not None else ROOT
        result = run_mail_export(args.task_root, repository / "data" / "assistant_tasks",
                                 args.request_sha256, execute=execute)
    except KeyboardInterrupt:
        result = {"ok": False, "state": "unknown", "error": "interrupted"}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else "mail_export_failed"
        result = {"ok": False, "state": "blocked",
                  "error": code if _SAFE.fullmatch(code) else "mail_export_failed"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True and result.get("state") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
