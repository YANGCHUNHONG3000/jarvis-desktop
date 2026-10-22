"""python -m agent_service --root PATH {submit,work,list,show,events,...}"""
import argparse
import json
from pathlib import Path
import time

from .browser import BrowserProcess
from .store import JobStore
from .worker import Worker, verify_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jarvis independent source-document jobs (no paid APIs)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "agent_service")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("topic")
    submit.add_argument("--url", action="append", default=[])
    submit.add_argument("--output", help="New ASCII .md basename; never overwritten")
    submit.add_argument("--request-id", help="Stable caller key for idempotent submission")
    work = commands.add_parser("work")
    work.add_argument("--once", action="store_true")
    commands.add_parser("list")
    for name in ("show", "events", "cancel", "pause", "resume", "verify"):
        command = commands.add_parser(name)
        command.add_argument("job_id")
        if name == "events":
            command.add_argument("--after", type=int, default=0)
    args = parser.parse_args(argv)
    store = JobStore(args.root)
    try:
        if args.command == "submit":
            result = store.submit(args.topic, args.url, args.output, args.request_id)
        elif args.command == "list":
            result = store.list()
        elif args.command == "show":
            result = store.get(args.job_id)
        elif args.command == "events":
            result = store.events(args.job_id, args.after)
        elif args.command == "verify":
            result = verify_artifact(store, args.job_id)
        elif args.command == "work":
            worker = Worker(store, BrowserProcess())
            while True:
                result = worker.run_once()
                if args.once:
                    break
                if result:
                    print(json.dumps({"id": result["id"], "state": result["state"], "reason": result["reason"]}, ensure_ascii=False), flush=True)
                time.sleep(0.25)
        else:
            result = store.control(args.job_id, args.command)
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        # Running jobs remain recoverable; don't silently report cancellation.
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and (result.get("state") == "failed" or result.get("ok") is False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
