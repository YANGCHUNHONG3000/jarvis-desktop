"""One-shot JSON CLI for the bounded natural-language assistant."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path

from agent_service.assistant import run_task


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one bounded Jarvis text task")
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent / "data" / "agent_service")
    parser.add_argument("--task-root", type=Path, help="Preallocated task identity under --root")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source-file", type=Path, help="Optional CSV, XLSX, or DOCX source for this task")
    sources.add_argument("--source-artifact-json", help="Verified prior Office artifact reference")
    sources.add_argument("--source-retry-json", help="Verified unsupported-task retry reference")
    parser.add_argument("--mail-understanding", action="store_true",
                        help="Explicitly analyze a verified local mail-export Word artifact")
    parser.add_argument("--mail-review-resume-json", help="Explicit one-shot prior mail-plan REVIEW continuation")
    args = parser.parse_args(argv)
    try:
        if args.text_file.stat().st_size > 48_000:
            raise ValueError("oversize")
        instruction = args.text_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        result = {"ok": False, "state": "failed", "error": "text_file_invalid"}
    else:
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
                options = {"task_root": args.task_root}
                if args.mail_review_resume_json is not None:
                    if (not args.mail_understanding or args.source_artifact_json is None
                            or len(args.mail_review_resume_json) > 2_000):
                        raise ValueError("mail_review_resume_reference_invalid")
                    reference = json.loads(args.mail_review_resume_json)
                    if type(reference) is not dict or set(reference) != {"task_root", "token"}:
                        raise ValueError("mail_review_resume_reference_invalid")
                    options["review_resume"] = reference
                if args.source_file is not None:
                    options["source_file"] = args.source_file
                if args.source_artifact_json is not None:
                    if len(args.source_artifact_json) > 6_000:
                        raise ValueError("artifact_source_reference_invalid")
                    options["source_artifact"] = json.loads(args.source_artifact_json)
                if args.source_retry_json is not None:
                    if len(args.source_retry_json) > 2_000:
                        raise ValueError("gap_reference_invalid")
                    options["source_retry"] = json.loads(args.source_retry_json)
                if args.mail_understanding:
                    if "source_artifact" not in options:
                        raise ValueError("mail_understanding_source_required")
                    from agent_service import mail_understanding
                    result = mail_understanding.run_task(instruction, args.root, **options)
                else:
                    result = run_task(instruction, args.root, **options)
        except KeyboardInterrupt:
            result = {"ok": False, "state": "cancelled", "error": "cancelled"}
        except OSError:
            result = {"ok": False, "state": "failed", "error": "task_root_unavailable"}
        except Exception:
            result = {"ok": False, "state": "failed", "error": "task_failed"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
