"""Local developer CLI for catalog lookup and bounded Office skill publication."""

import argparse
import json
from pathlib import Path
import uuid

from agent_service import local_skills as skills
from agent_service.capabilities import list_capabilities, capability_gap

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jarvis 本地能力查询与受限办公技能")
    parser.add_argument("--store", type=Path, default=ROOT / "data" / "capabilities")
    parser.add_argument("--base", type=Path, default=ROOT / "data" / "assistant_tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog")
    catalog.add_argument("--query")
    gap = commands.add_parser("gap")
    gap.add_argument("query")
    register = commands.add_parser("register")
    register.add_argument("manifest", type=Path)
    for name in ("offline", "review", "live", "activate", "status", "rollback", "reconcile", "run"):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name in {"offline", "review", "live", "activate"}:
            command.add_argument("sha256")
        if name == "review":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--note", required=True)
        if name == "live":
            command.add_argument("--task-root", type=Path, required=True)
        if name == "activate":
            command.add_argument("--runs", type=int, default=10)
        if name == "run":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--source-file", type=Path)
            source.add_argument("--source-artifact-json")
            command.add_argument("--trial-sha256")
            command.add_argument("--expected-sha256")
            command.add_argument("--task-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "catalog":
            result = {"ok": True, "capabilities": list_capabilities(args.query)}
        elif args.command == "gap":
            result = {"ok": True, "gap": capability_gap(args.query)}
        elif args.command == "register":
            if args.manifest.stat().st_size > 10_000:
                raise ValueError("skill_manifest_oversize")
            result = skills.register_candidate(args.store, json.loads(args.manifest.read_text(encoding="utf-8")))
        elif args.command == "offline":
            result = skills.verify_offline(args.store, args.id, args.sha256)
        elif args.command == "review":
            result = skills.record_review(args.store, args.id, args.sha256, args.reviewer, args.note)
        elif args.command == "live":
            result = skills.record_live(args.store, args.id, args.sha256, args.task_root, args.base)
        elif args.command == "activate":
            result = skills.activate(args.store, args.id, args.sha256, args.base, runs=args.runs)
        elif args.command == "status":
            result = skills.status(args.store, args.id, args.base)
        elif args.command == "rollback":
            result = skills.rollback(args.store, args.id, args.base)
        elif args.command == "reconcile":
            result = skills.reconcile(args.store, args.id, args.base)
        else:
            reference = None
            if args.source_artifact_json is not None:
                if len(args.source_artifact_json) > 16_000:
                    raise ValueError("artifact_reference_oversize")
                reference = json.loads(args.source_artifact_json)
            task_root = args.task_root or args.base / ("task-" + uuid.uuid4().hex)
            result = skills.run_skill(args.store, args.id, task_root, args.base, source_file=args.source_file,
                                      source_artifact=reference, trial_digest=args.trial_sha256,
                                      expected_digest=args.expected_sha256)
        result.setdefault("ok", True)
    except (ValueError, TypeError, KeyError, OSError):
        result = {"ok": False, "state": "blocked", "error": "skill_operation_rejected"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
