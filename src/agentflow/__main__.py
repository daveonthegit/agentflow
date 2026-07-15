from __future__ import annotations

import argparse
import json
from pathlib import Path
import uuid

from .project_setup import initialize_repository


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init")
    init_parser.add_argument("repository", nargs="?", type=Path, default=Path.cwd())
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("task", type=Path)
    run_parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "init":
        result = initialize_repository(args.repository)
        print(
            json.dumps(
                {"repository": str(result.repository), "state": "initialized"},
                sort_keys=True,
            )
        )
        return 0

    json.loads(args.task.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex
    events_path = args.data_dir / "runs" / run_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)

    events = (
        {"type": "run_created", "run_id": run_id},
        {"type": "workspace_ready"},
        {"type": "plan_ready", "artifact": "fake-plan"},
        {"type": "checks_passed", "artifact": "fake-checks"},
        {"type": "awaiting_human"},
    )
    events_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    print(json.dumps({"run_id": run_id, "state": "awaiting_human"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
