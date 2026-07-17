from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

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
from .improvement import (
    MIN_RECURRENCE_RUNS,
    evaluate_proposal,
    generate_proposals,
    list_proposals,
)
from .project_setup import initialize_repository
from .paths import agentflow_home
from .repository_profile import create_repository_profile
from .work_graph import (
    compute_ready_work,
    completed_work_item_ids,
    load_work_graph,
    work_item_content_hash,
)
from .run_kernel import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    abandon_run,
    approve_run,
    follow_run,
    list_runs,
    read_run_status,
    rebase_run,
    reject_run,
    select_live_run,
    short_run_id,
    start_run,
)
from .projection import build_projection
from .reconcile import reconcile
from .web_ui import create_web_server
from .workflow import advance_run


def _build_adapter(args, parser):
    """Construct the Agent Adapter selected on the command line, or None."""
    if args.model is not None and args.adapter not in {"claude", "cursor"}:
        parser.error("--model requires --adapter claude or cursor")
    if args.adapter is None:
        return None
    if args.adapter == "fake":
        if args.adapter_fixture is None:
            parser.error("--adapter-fixture is required for the fake adapter")
        return DeterministicFakeAdapter(args.adapter_fixture)
    if args.adapter == "claude":
        return ClaudeAdapter(data_dir=agentflow_home(args.data_dir), model=args.model)
    if args.adapter == "codex":
        return CodexAdapter()
    return CursorAdapter(data_dir=agentflow_home(args.data_dir), model=args.model)


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init")
    init_parser.add_argument("repository", nargs="?", type=Path, default=Path.cwd())
    profile_parser = subcommands.add_parser("profile")
    profile_parser.add_argument("--check", action="append", required=True)
    profile_parser.add_argument(
        "--test-path",
        action="append",
        default=[],
        dest="test_paths",
    )
    start_parser = subcommands.add_parser("start")
    start_parser.add_argument("summary", nargs="?")
    start_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        dest="acceptance_criteria",
    )
    start_parser.add_argument(
        "--work-item",
        help=(
            "capture this Work Item from the Target Repository's Work Graph "
            "as the Task Spec (by id and content hash)"
        ),
    )
    start_parser.add_argument("--data-dir", type=Path)
    status_parser = subcommands.add_parser("status")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--data-dir", type=Path)
    watch_parser = subcommands.add_parser("watch")
    watch_parser.add_argument(
        "run_id",
        nargs="?",
        help=(
            "Run to follow; omit to pick interactively among live Runs "
            "(state, truncated summary, short id)"
        ),
    )
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
    reconcile_parser = subcommands.add_parser("reconcile")
    reconcile_parser.add_argument("--repository", type=Path, default=Path("."))
    reconcile_parser.add_argument(
        "--adapter", choices=("claude", "codex", "cursor", "fake")
    )
    reconcile_parser.add_argument("--adapter-fixture", type=Path)
    reconcile_parser.add_argument("--model")
    reconcile_parser.add_argument("--data-dir", type=Path)
    run_parser = subcommands.add_parser("run")
    run_parser.add_argument("task", type=Path)
    run_parser.add_argument("--data-dir", type=Path)
    work_parser = subcommands.add_parser("work")
    work_parser.add_argument("mode", choices=("list", "ready"))
    work_parser.add_argument("--repository", type=Path, default=Path("."))
    work_parser.add_argument("--data-dir", type=Path)
    project_parser = subcommands.add_parser(
        "project",
        help=(
            "rebuild a read-only observability projection over Run Evidence "
            "and the Work Graph"
        ),
    )
    project_parser.add_argument("--repository", type=Path, default=Path("."))
    project_parser.add_argument("--data-dir", type=Path)
    propose_parser = subcommands.add_parser(
        "propose",
        help=(
            "detect recurring patterns in stored Run Evidence and record "
            "Improvement Proposals (records only; nothing is applied)"
        ),
    )
    propose_parser.add_argument(
        "--min-runs",
        type=int,
        default=MIN_RECURRENCE_RUNS,
        help="distinct Runs a pattern must recur across before it is proposed",
    )
    propose_parser.add_argument("--data-dir", type=Path)
    proposals_parser = subcommands.add_parser(
        "proposals",
        help="list recorded Improvement Proposals and their evaluation state",
    )
    proposals_parser.add_argument("--data-dir", type=Path)
    evaluate_parser = subcommands.add_parser(
        "evaluate",
        help=(
            "evaluate an Improvement Proposal against the fixed fixtures and "
            "historical Run Evidence, recording the result as evidence"
        ),
    )
    evaluate_parser.add_argument("proposal_id")
    evaluate_parser.add_argument(
        "--fixtures",
        type=Path,
        help="fixture directory override (defaults to the shipped fixed fixtures)",
    )
    evaluate_parser.add_argument("--data-dir", type=Path)
    serve_parser = subcommands.add_parser(
        "serve",
        help=(
            "serve a local read-only web UI over the observability projection "
            "(runs, work, evidence, and live role transcripts)"
        ),
    )
    serve_parser.add_argument("--repository", type=Path, default=Path("."))
    serve_parser.add_argument("--data-dir", type=Path)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
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
            test_paths=args.test_paths,
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
        source = None
        summary = args.summary
        acceptance_criteria = args.acceptance_criteria
        response: dict[str, object] = {}
        if args.work_item is not None:
            if args.summary is not None or args.acceptance_criteria:
                parser.error(
                    "--work-item takes the summary and acceptance criteria "
                    "from the Work Item; do not also pass them"
                )
            graph = load_work_graph(Path.cwd())
            item = next(
                (entry for entry in graph if entry["id"] == args.work_item), None
            )
            if item is None:
                parser.error(
                    f"no Work Item {args.work_item!r} in the Work Graph"
                )
            summary = item["summary"]
            acceptance_criteria = item["acceptance_criteria"]
            source = {
                "provider": "work-graph",
                "work_item_id": item["id"],
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "content_hash": work_item_content_hash(item),
            }
            response["work_item_id"] = item["id"]
        elif args.summary is None:
            parser.error("either a summary or --work-item is required")
        result = start_run(
            summary=summary,
            acceptance_criteria=acceptance_criteria,
            source=source,
            repository=Path.cwd(),
            data_dir=agentflow_home(args.data_dir),
        )
        response.update(
            {
                "run_id": result.run_id,
                "state": result.state,
                "worktree": str(result.worktree),
            }
        )
        print(json.dumps(response, sort_keys=True))
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
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "watch":
        data_dir = agentflow_home(args.data_dir)
        run_id = args.run_id
        if run_id is None:
            try:
                run_id = select_live_run(
                    data_dir=data_dir,
                    inp=sys.stdin,
                    err=sys.stderr,
                )
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 2
        follow_run(run_id=run_id, data_dir=data_dir)
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
                "short_id": short_run_id(result.run_id),
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
        adapter = _build_adapter(args, parser)
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

    if args.command == "work":
        graph = load_work_graph(args.repository)
        if args.mode == "ready":
            completed = completed_work_item_ids(agentflow_home(args.data_dir))
            graph = compute_ready_work(graph, completed)
        print(json.dumps(graph, sort_keys=True))
        return 0

    if args.command == "project":
        projection = build_projection(
            data_dir=agentflow_home(args.data_dir),
            repository=args.repository,
        )
        print(json.dumps(projection, sort_keys=True))
        return 0

    if args.command == "propose":
        proposals = generate_proposals(
            data_dir=agentflow_home(args.data_dir),
            min_runs=args.min_runs,
        )
        print(json.dumps(proposals, sort_keys=True))
        return 0

    if args.command == "proposals":
        print(
            json.dumps(
                list_proposals(data_dir=agentflow_home(args.data_dir)),
                sort_keys=True,
            )
        )
        return 0

    if args.command == "evaluate":
        evaluation = evaluate_proposal(
            data_dir=agentflow_home(args.data_dir),
            proposal_id=args.proposal_id,
            fixtures_dir=args.fixtures,
        )
        print(json.dumps(evaluation, sort_keys=True))
        return 0

    if args.command == "serve":
        server = create_web_server(
            data_dir=agentflow_home(args.data_dir),
            repository=args.repository,
            host=args.host,
            port=args.port,
        )
        host, port = server.server_address[0], server.server_address[1]
        print(
            json.dumps(
                {"url": f"http://{host}:{port}", "state": "serving"},
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0

    if args.command == "reconcile":
        adapter = _build_adapter(args, parser)
        report = reconcile(
            repository=args.repository,
            data_dir=agentflow_home(args.data_dir),
            adapter=adapter,
        )
        print(json.dumps(report, sort_keys=True))
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
