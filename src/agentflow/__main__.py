from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent_adapter import (
    ROLES,
    SUGGESTED_MODELS,
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    DeterministicFakeAdapter,
    read_model_routing,
    record_model_routing,
)
from .contracts import validate_task_spec
from .project_setup import initialize_repository
from .paths import agentflow_home
from .repository_profile import create_repository_profile
from .run_kernel import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    abandon_run,
    amend_plan,
    approve_run,
    follow_run,
    list_runs,
    read_run_status,
    rebase_run,
    reject_run,
    start_run,
)
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
    start_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        dest="acceptance_criteria",
    )
    start_parser.add_argument("--data-dir", type=Path)
    status_parser = subcommands.add_parser("status")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--data-dir", type=Path)
    watch_parser = subcommands.add_parser("watch")
    watch_parser.add_argument("run_id")
    watch_parser.add_argument("--data-dir", type=Path)
    list_parser = subcommands.add_parser("list")
    list_parser.add_argument("--state")
    list_parser.add_argument("--data-dir", type=Path)
    abandon_parser = subcommands.add_parser("abandon")
    abandon_parser.add_argument("run_id")
    abandon_parser.add_argument("--abandoned-by", required=True)
    abandon_parser.add_argument("--reason")
    abandon_parser.add_argument("--data-dir", type=Path)
    reject_parser = subcommands.add_parser("reject")
    reject_parser.add_argument("run_id")
    reject_parser.add_argument("--rejected-by", required=True)
    reject_parser.add_argument("--reason")
    reject_parser.add_argument("--data-dir", type=Path)
    approve_parser = subcommands.add_parser("approve")
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--data-dir", type=Path)
    amend_plan_parser = subcommands.add_parser("amend-plan")
    amend_plan_parser.add_argument("run_id")
    amend_plan_parser.add_argument(
        "--add-path",
        action="append",
        required=True,
        dest="added_paths",
    )
    amend_plan_parser.add_argument("--amended-by", required=True)
    amend_plan_parser.add_argument("--reason")
    amend_plan_parser.add_argument("--data-dir", type=Path)
    rebase_parser = subcommands.add_parser("rebase")
    rebase_parser.add_argument("run_id")
    rebase_parser.add_argument("--data-dir", type=Path)
    advance_parser = subcommands.add_parser("advance")
    advance_parser.add_argument("run_id")
    advance_parser.add_argument(
        "--adapter", choices=("claude", "codex", "cursor", "fake")
    )
    advance_parser.add_argument("--adapter-fixture", type=Path)
    advance_parser.add_argument(
        "--claim-lease-seconds",
        type=int,
        default=DEFAULT_CLAIM_LEASE_SECONDS,
    )
    advance_parser.add_argument("--model")
    advance_parser.add_argument("--data-dir", type=Path)
    models_parser = subcommands.add_parser("models")
    models_parser.add_argument("--adapter", choices=tuple(SUGGESTED_MODELS))
    models_parser.add_argument(
        "--set",
        action="append",
        dest="set_entries",
        metavar="role=model",
    )
    models_parser.add_argument("--data-dir", type=Path)
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
            acceptance_criteria=args.acceptance_criteria,
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
        if result.repository_profile_path is not None:
            response["repository_profile_path"] = result.repository_profile_path
        if result.acceptance_criteria is not None:
            response["acceptance_criteria"] = result.acceptance_criteria
        if result.source is not None:
            response["source"] = result.source
        if result.plan_amendments is not None:
            response["plan_amendments"] = result.plan_amendments
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "watch":
        follow_run(
            run_id=args.run_id,
            data_dir=agentflow_home(args.data_dir),
        )
        return 0

    if args.command == "list":
        results = list_runs(
            data_dir=agentflow_home(args.data_dir),
            state=args.state,
        )
        entries = []
        for result in results:
            entry = {
                "base_sha": result.base_sha,
                "repository": result.repository,
                "run_id": result.run_id,
                "state": result.state,
                "summary": result.summary,
            }
            if result.candidate_sha is not None:
                entry["candidate_sha"] = result.candidate_sha
            if result.approved_sha is not None:
                entry["approved_sha"] = result.approved_sha
            entries.append(entry)
        print(json.dumps(entries, sort_keys=True))
        return 0

    if args.command == "abandon":
        result = abandon_run(
            run_id=args.run_id,
            abandoned_by=args.abandoned_by,
            reason=args.reason,
            data_dir=agentflow_home(args.data_dir),
        )
        response = {
            "abandoned_by": result.abandoned_by,
            "run_id": result.run_id,
            "state": result.state,
        }
        if result.reason is not None:
            response["reason"] = result.reason
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "reject":
        result = reject_run(
            run_id=args.run_id,
            rejected_by=args.rejected_by,
            reason=args.reason,
            data_dir=agentflow_home(args.data_dir),
        )
        response = {
            "rejected_by": result.rejected_by,
            "run_id": result.run_id,
            "state": result.state,
        }
        if result.reason is not None:
            response["reason"] = result.reason
        if result.rejected_sha is not None:
            response["rejected_sha"] = result.rejected_sha
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

    if args.command == "amend-plan":
        result = amend_plan(
            run_id=args.run_id,
            added_paths=args.added_paths,
            amended_by=args.amended_by,
            reason=args.reason,
            data_dir=agentflow_home(args.data_dir),
        )
        response = {
            "added_paths": result.added_paths,
            "amended_by": result.amended_by,
            "run_id": result.run_id,
            "state": result.state,
        }
        if result.reason is not None:
            response["reason"] = result.reason
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "rebase":
        result = rebase_run(
            run_id=args.run_id,
            data_dir=agentflow_home(args.data_dir),
        )
        if result.rebased:
            response = {
                "base_sha": result.new_base_sha,
                "new_base_sha": result.new_base_sha,
                "new_candidate_sha": result.new_candidate_sha,
                "old_base_sha": result.old_base_sha,
                "old_candidate_sha": result.old_candidate_sha,
                "rebased": True,
                "run_id": result.run_id,
                "state": result.state,
            }
        else:
            response = {
                "base_sha": result.base_sha,
                "rebased": False,
                "run_id": result.run_id,
                "state": result.state,
            }
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "models":
        data_dir = agentflow_home(args.data_dir)
        if args.set_entries:
            if args.adapter is None:
                parser.error("--set requires --adapter")
            updates: dict[str, str] = {}
            for entry in args.set_entries:
                role, separator, model = entry.partition("=")
                if not separator or not role or not model:
                    parser.error(
                        f"--set expects role=model, got {entry!r}"
                    )
                if role not in ROLES:
                    parser.error(
                        f"unknown role {role!r}; expected one of "
                        + ", ".join(ROLES)
                    )
                updates[role] = model
            record_model_routing(data_dir, args.adapter, updates)
        routing = read_model_routing(data_dir)
        print(
            json.dumps(
                {
                    adapter_name: {
                        "recorded": routing.get(adapter_name, {}),
                        "suggested": suggested,
                    }
                    for adapter_name, suggested in SUGGESTED_MODELS.items()
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "advance":
        if args.model is not None and args.adapter not in {"claude", "cursor"}:
            parser.error("--model requires --adapter claude or cursor")
        adapter = None
        if args.adapter == "fake":
            if args.adapter_fixture is None:
                parser.error("--adapter-fixture is required for the fake adapter")
            adapter = DeterministicFakeAdapter(args.adapter_fixture)
        elif args.adapter == "claude":
            adapter = ClaudeAdapter(
                data_dir=agentflow_home(args.data_dir),
                model=args.model,
            )
        elif args.adapter == "codex":
            adapter = CodexAdapter()
        elif args.adapter == "cursor":
            adapter = CursorAdapter(
                data_dir=agentflow_home(args.data_dir),
                model=args.model,
            )
        result = advance_run(
            run_id=args.run_id,
            data_dir=agentflow_home(args.data_dir),
            adapter=adapter,
            claim_lease_seconds=args.claim_lease_seconds,
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

    task = validate_task_spec(json.loads(args.task.read_text(encoding="utf-8")))
    result = start_run(
        summary=task["summary"],
        acceptance_criteria=task["acceptance_criteria"],
        source=task.get("source"),
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
