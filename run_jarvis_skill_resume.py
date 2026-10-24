"""Explicit continuation of a permitted cancelled or pre-dispatch blocked skill."""

import argparse
import json
from pathlib import Path
import re

from agent_service import skill_resume


ROOT = Path(__file__).resolve().parent
_SAFE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments_invalid")

    def exit(self, status=0, message=None):
        raise ValueError("arguments_invalid")

    def print_help(self, file=None):
        return None


def main(argv=None, *, repo_root=None):
    parser = _Parser(description="继续已获许可的办公技能任务")
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--attempt-id", required=True)
    try:
        args = parser.parse_args(argv)
        if not Path(args.task_root).is_absolute():
            raise ValueError("task_root_invalid")
        repository = Path(repo_root).resolve() if repo_root is not None else ROOT
        result = skill_resume.resume_task(args.task_root, repository / "data" / "assistant_tasks",
                                          expected_token=args.token, attempt_id=args.attempt_id,
                                          store=repository / "data" / "capabilities")
    except KeyboardInterrupt:
        result = {"ok": False, "state": "unknown", "kind": "skill_resume",
                  "error": "interrupted"}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else "skill_resume_rejected"
        result = {"ok": False, "state": "blocked", "kind": "skill_resume",
                  "error": code if _SAFE.fullmatch(code) else "skill_resume_rejected"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True and result.get("state") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
