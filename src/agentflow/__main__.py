from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent_adapter import CodexAdapter, DeterministicFakeAdapter
from .project_setup import initialize_repository
from .paths import agentflow_home
from .repository_profile import create_repository_profile
from .run_kernel import approve_run, read_run_status, start_run
from .workflow import advance_run


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init")
    init_parser.add_argument("repository", nargs="?", type=Path, default=Path.cwd())
    profile_parser = subcommands.add_parser("profile")
    profile_parser.add_argument("--check", action="append", required=True)
    start_parser = subcommands.add_parser("start")
    start_parser.add_argument("summary")
    start_parser.add_argument("--data-dir", type=Path)
    status_parser = subcommands.add_parser("status")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--data-dir", type=Path)
    approve_parser = subcommands.add_parser("approve")
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--data-dir", type=Path)
    advance_parser = subcommands.add_parser("advance")
    advance_parser.add_argument("run_id")
    advance_parser.add_argument("--adapter", choices=("codex", "fake"))
    advance_parser.add_argument("--adapter-fixture", type=Path)
    advance_parser.add_argument("--data-dir", type=Path)
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("task", type=Path)
    run_parser.add_argument("--data-dir", type=Path)
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

    if args.command == "profile":
        result = create_repository_profile(
            repository=Path.cwd(),
            checks=args.check,
        )
        print(
            json.dumps(
                {
                    "profile": str(result.path),
                    "source_fingerprint": result.source_fingerprint,
                    "state": "profile_ready",
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "start":
        result = start_run(
            summary=args.summary,
            repository=Path.cwd(),
            data_dir=agentflow_home(args.data_dir),
        )
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "state": result.state,
                    "worktree": str(result.worktree),
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "status":
        result = read_run_status(
            run_id=args.run_id,
            data_dir=agentflow_home(args.data_dir),
        )
        response = {
            "base_sha": result.base_sha,
            "repository": result.repository,
            "run_id": result.run_id,
            "state": result.state,
            "summary": result.summary,
            "worktree": result.worktree,
        }
        if result.candidate_sha is not None:
            response["candidate_sha"] = result.candidate_sha
        if result.approved_sha is not None:
            response["approved_sha"] = result.approved_sha
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "approve":
        result = approve_run(
            run_id=args.run_id,
            approved_by=args.approved_by,
            data_dir=agentflow_home(args.data_dir),
        )
        print(
            json.dumps(
                {
                    "approved_by": result.approved_by,
                    "approved_sha": result.approved_sha,
                    "run_id": result.run_id,
                    "state": result.state,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "advance":
        adapter = None
        if args.adapter == "fake":
            if args.adapter_fixture is None:
                parser.error("--adapter-fixture is required for the fake adapter")
            adapter = DeterministicFakeAdapter(args.adapter_fixture)
        elif args.adapter == "codex":
            adapter = CodexAdapter()
        result = advance_run(
            run_id=args.run_id,
            data_dir=agentflow_home(args.data_dir),
            adapter=adapter,
        )
        response = {
            "artifact": str(result.artifact),
            "run_id": result.run_id,
            "state": result.state,
        }
        if result.candidate_sha is not None:
            response["candidate_sha"] = result.candidate_sha
        print(json.dumps(response, sort_keys=True))
        return 0

    task = json.loads(args.task.read_text(encoding="utf-8"))
    result = start_run(
        summary=task["summary"],
        repository=Path.cwd(),
        data_dir=agentflow_home(args.data_dir),
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "state": result.state,
                "worktree": str(result.worktree),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
