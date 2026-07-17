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
from .contracts import ContractError, validate_task_spec
from .improvement import (
    MIN_RECURRENCE_RUNS,
    SKILL_BASELINE_RELATIVE_DIR,
    adopt_skill_file,
    apply_proposal_to_baseline,
    approve_adoption,
    compare_skill_baseline,
    evaluate_proposal,
    generate_proposals,
    list_proposals,
)
from .deployment import deploy_run
from .merger import merge_approved_run
from .post_merge import (
    list_recovery_proposals,
    resolve_post_merge_failure,
    verify_merged_run,
)
from .project_setup import PolicyNotCommittableError, initialize_repository
from .paths import agentflow_home
from .proposals import ingest_proposals, scan_proposals
from .repository_profile import (
    DEPLOYMENT_ADAPTERS,
    MERGE_STRATEGIES,
    create_repository_profile,
)
from .work_graph import (
    approve_work_graph,
    check_work_graph_health,
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
    init_parser.add_argument(
        "--enforcement",
        choices=["observe", "strict"],
        default=None,
        help=(
            "enforcement mode recorded as committed policy; defaults to observe "
            "on first init and otherwise preserves the repository's current mode"
        ),
    )
    profile_parser = subcommands.add_parser("profile")
    profile_parser.add_argument("--check", action="append", required=True)
    profile_parser.add_argument(
        "--test-path",
        action="append",
        default=[],
        dest="test_paths",
    )
    profile_parser.add_argument(
        "--allow-merge",
        action="store_true",
        help=(
            "record a merge_policy permitting the Merge Agent to merge an "
            "Approved Revision into the target branch"
        ),
    )
    profile_parser.add_argument(
        "--merge-target-branch",
        help="branch merge_policy targets (default: the current branch)",
    )
    profile_parser.add_argument(
        "--merge-strategy",
        choices=MERGE_STRATEGIES,
        default="fast-forward",
    )
    profile_parser.add_argument(
        "--merge-protected",
        action="store_true",
        help=(
            "mark merge_policy's target branch protected: it advances only "
            "through the gated merge path and a merge is refused if the "
            "branch has diverged out of band"
        ),
    )
    profile_parser.add_argument(
        "--deploy-adapter",
        choices=DEPLOYMENT_ADAPTERS,
        help=(
            "record a deployment configuration naming the Deployment Adapter "
            "`agentflow deploy` may ship a verified revision through; absent "
            "configuration refuses deployment by default"
        ),
    )
    profile_parser.add_argument(
        "--deploy-target",
        help=(
            "directory adapter: path the verified revision's content is "
            "published to (relative paths resolve against the repository; "
            "the target must lie outside it)"
        ),
    )
    profile_parser.add_argument(
        "--deploy-command",
        help=(
            "command adapter: deploy command run inside an isolated checkout "
            "of the exact verified revision"
        ),
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
    merge_parser = subcommands.add_parser("merge")
    merge_parser.add_argument("run_id")
    merge_parser.add_argument("--merged-by", required=True)
    merge_parser.add_argument("--data-dir", type=Path)
    verify_merge_parser = subcommands.add_parser(
        "verify-merge",
        help=(
            "run Post-Merge Verification: the authoritative checks against "
            "the exact merged commit in an isolated checkout, recorded as "
            "Run Evidence; a failure stops further shipping and records a "
            "Recovery Proposal for human review"
        ),
    )
    verify_merge_parser.add_argument("run_id")
    verify_merge_parser.add_argument("--verified-by", required=True)
    verify_merge_parser.add_argument("--data-dir", type=Path)
    deploy_parser = subcommands.add_parser(
        "deploy",
        help=(
            "ship a merged, post-merge-verified revision through the "
            "Repository Profile's Deployment Adapter; deterministic gates "
            "refuse everything else and every refusal is recorded as evidence"
        ),
    )
    deploy_parser.add_argument("run_id")
    deploy_parser.add_argument("--deployed-by", required=True)
    deploy_parser.add_argument("--data-dir", type=Path)
    resolve_merge_parser = subcommands.add_parser(
        "resolve-merge",
        help=(
            "record a human-attributed resolution of a failed Post-Merge "
            "Verification, lifting the shipping block; nothing is executed"
        ),
    )
    resolve_merge_parser.add_argument("run_id")
    resolve_merge_parser.add_argument("--resolved-by", required=True)
    resolve_merge_parser.add_argument("--resolution", required=True)
    resolve_merge_parser.add_argument("--data-dir", type=Path)
    recovery_parser = subcommands.add_parser(
        "recovery",
        help=(
            "list recorded Recovery Proposals and their resolution state "
            "(records only; Agentflow never executes a recovery)"
        ),
    )
    recovery_parser.add_argument("--data-dir", type=Path)
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
    work_parser.add_argument(
        "mode",
        choices=("list", "ready", "verify", "approve", "proposals", "ingest"),
    )
    work_parser.add_argument("--repository", type=Path, default=Path("."))
    work_parser.add_argument(
        "--approved-by",
        help=(
            "record a human approval of the current Work Graph content "
            "(required for `work approve`)"
        ),
    )
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
    adopt_parser = subcommands.add_parser(
        "adopt",
        help=(
            "record an attributed Adoption Gate approval of a passing "
            "Improvement Proposal and apply it to its baseline"
        ),
    )
    adopt_parser.add_argument("proposal_id")
    adopt_parser.add_argument("--approved-by", required=True)
    adopt_parser.add_argument(
        "--repository",
        type=Path,
        default=Path("."),
        help="Target Repository (required for repository_profile proposals)",
    )
    adopt_parser.add_argument("--data-dir", type=Path)
    skill_diff_parser = subcommands.add_parser(
        "skill-diff",
        help=(
            "compare the local skill baseline against an upstream copy and "
            "record a reviewable per-file diff (nothing is adopted)"
        ),
    )
    skill_diff_parser.add_argument("--upstream", type=Path, required=True)
    skill_diff_parser.add_argument(
        "--repository",
        type=Path,
        default=Path("."),
        help="Agentflow repository containing the skill baseline",
    )
    skill_diff_parser.add_argument("--data-dir", type=Path)
    adopt_skill_parser = subcommands.add_parser(
        "adopt-skill",
        help=(
            "selectively adopt one reviewed upstream skill file through the "
            "Adoption Gate (attributed, content-hashed, evidence-recorded)"
        ),
    )
    adopt_skill_parser.add_argument(
        "path", help="skill file to adopt, relative to the skill baseline"
    )
    adopt_skill_parser.add_argument("--upstream", type=Path, required=True)
    adopt_skill_parser.add_argument("--approved-by", required=True)
    adopt_skill_parser.add_argument(
        "--repository",
        type=Path,
        default=Path("."),
        help="Agentflow repository containing the skill baseline",
    )
    adopt_skill_parser.add_argument("--data-dir", type=Path)
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
        try:
            result = initialize_repository(
                args.repository, enforcement=args.enforcement
            )
        except PolicyNotCommittableError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "repository": str(result.repository),
                    "state": "initialized",
                    "enforcement": result.enforcement,
                    "policy": result.policy_relative,
                    "hooks_installed": list(result.hooks_installed),
                    "hooks_preserved": list(result.hooks_preserved),
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "profile":
        merge_policy = None
        if args.allow_merge:
            merge_policy = {
                "allow": True,
                "protected": args.merge_protected,
                "strategy": args.merge_strategy,
            }
            if args.merge_target_branch is not None:
                merge_policy["target_branch"] = args.merge_target_branch
        elif args.merge_target_branch is not None:
            parser.error("--merge-target-branch requires --allow-merge")
        elif args.merge_protected:
            parser.error("--merge-protected requires --allow-merge")
        deployment = None
        if args.deploy_adapter is not None:
            if args.deploy_adapter == "directory":
                if args.deploy_target is None:
                    parser.error(
                        "--deploy-adapter directory requires --deploy-target"
                    )
                if args.deploy_command is not None:
                    parser.error(
                        "--deploy-command requires --deploy-adapter command"
                    )
                deployment = {
                    "adapter": "directory",
                    "config": {"target": args.deploy_target},
                }
            else:
                if args.deploy_command is None:
                    parser.error(
                        "--deploy-adapter command requires --deploy-command"
                    )
                if args.deploy_target is not None:
                    parser.error(
                        "--deploy-target requires --deploy-adapter directory"
                    )
                deployment = {
                    "adapter": "command",
                    "config": {"command": args.deploy_command},
                }
        elif args.deploy_target is not None or args.deploy_command is not None:
            parser.error(
                "--deploy-target/--deploy-command require --deploy-adapter"
            )
        result = create_repository_profile(
            repository=Path.cwd(),
            checks=args.check,
            test_paths=args.test_paths,
            merge_policy=merge_policy,
            deployment=deployment,
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

    if args.command == "merge":
        result = merge_approved_run(
            run_id=args.run_id,
            merged_by=args.merged_by,
            data_dir=agentflow_home(args.data_dir),
        )
        print(
            json.dumps(
                {
                    "approved_sha": result.approved_sha,
                    "artifact": str(result.artifact),
                    "ci_artifact": str(result.ci_artifact),
                    "merged_by": result.merged_by,
                    "merged_sha": result.merged_sha,
                    "run_id": result.run_id,
                    "state": result.state,
                    "strategy": result.strategy,
                    "target_branch": result.target_branch,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "verify-merge":
        verification = verify_merged_run(
            run_id=args.run_id,
            verified_by=args.verified_by,
            data_dir=agentflow_home(args.data_dir),
        )
        response = {
            "artifact": str(verification.artifact),
            "merged_sha": verification.merged_sha,
            "passed": verification.passed,
            "run_id": verification.run_id,
            "state": verification.state,
            "verified_by": verification.verified_by,
        }
        if verification.recovery_proposal_id is not None:
            response["recovery_proposal_id"] = verification.recovery_proposal_id
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "deploy":
        deployed = deploy_run(
            run_id=args.run_id,
            deployed_by=args.deployed_by,
            data_dir=agentflow_home(args.data_dir),
        )
        print(
            json.dumps(
                {
                    "adapter": deployed.adapter,
                    "artifact": str(deployed.artifact),
                    "attempt_artifact": str(deployed.attempt_artifact),
                    "deployed_by": deployed.deployed_by,
                    "merged_sha": deployed.merged_sha,
                    "run_id": deployed.run_id,
                    "state": deployed.state,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "resolve-merge":
        resolved = resolve_post_merge_failure(
            run_id=args.run_id,
            resolved_by=args.resolved_by,
            resolution=args.resolution,
            data_dir=agentflow_home(args.data_dir),
        )
        print(
            json.dumps(
                {
                    "recovery_proposal_id": resolved.recovery_proposal_id,
                    "resolution": resolved.resolution,
                    "resolved_by": resolved.resolved_by,
                    "run_id": resolved.run_id,
                    "state": resolved.state,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "recovery":
        print(
            json.dumps(
                list_recovery_proposals(data_dir=agentflow_home(args.data_dir)),
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
        if args.mode == "approve":
            if args.approved_by is None:
                parser.error("work approve requires --approved-by")
            record = approve_work_graph(
                repository=args.repository,
                data_dir=agentflow_home(args.data_dir),
                approved_by=args.approved_by,
            )
            print(
                json.dumps(
                    {
                        "approved_by": record["approved_by"],
                        "graph_hash": record["graph_hash"],
                        "repository": record["repository"],
                        "state": "work_graph_approved",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.mode == "verify":
            # A deterministic, CI-runnable health check that reads the Target
            # Repository alone (no network, no Agentflow Home state). Every
            # corruption or malformation mode is a distinct, actionable message
            # and a nonzero exit — never a traceback.
            try:
                health = check_work_graph_health(args.repository)
            except ContractError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(
                json.dumps(
                    {
                        "approved_by": health.approval["approved_by"],
                        "graph_hash": health.graph_hash,
                        "sequence": health.approval["sequence"],
                        "state": "verified",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.mode == "proposals":
            scanned = scan_proposals(args.repository)
            print(
                json.dumps(
                    {
                        "valid": [
                            {
                                "acceptance_criteria": proposal.acceptance_criteria,
                                "filename": proposal.filename,
                                "kind": proposal.kind,
                                "relates_to": proposal.relates_to,
                                "summary": proposal.summary,
                                "work_item_id": proposal.work_item_id,
                            }
                            for proposal in scanned.valid
                        ],
                        "invalid": scanned.invalid,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.mode == "ingest":
            result = ingest_proposals(args.repository)
            print(
                json.dumps(
                    {
                        "applied": result.applied,
                        "completion_claims": result.completion_claims,
                        "invalid": result.invalid,
                        "removed": result.removed,
                        "skipped_duplicate": result.skipped_duplicate,
                        "skipped_existing": result.skipped_existing,
                        "skipped_over_cap": result.skipped_over_cap,
                        "state": "ingested",
                    },
                    sort_keys=True,
                )
            )
            return 0
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

    if args.command == "adopt":
        data_dir = agentflow_home(args.data_dir)
        adoption = approve_adoption(
            data_dir=data_dir,
            proposal_id=args.proposal_id,
            approved_by=args.approved_by,
        )
        applied = apply_proposal_to_baseline(
            data_dir=data_dir,
            proposal_id=args.proposal_id,
            repository=args.repository,
        )
        print(
            json.dumps(
                {
                    "adoption": adoption,
                    "applied": applied,
                    "state": "applied",
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "skill-diff":
        record = compare_skill_baseline(
            baseline_dir=args.repository / SKILL_BASELINE_RELATIVE_DIR,
            upstream_dir=args.upstream,
            data_dir=agentflow_home(args.data_dir),
        )
        print(json.dumps(record, sort_keys=True))
        return 0

    if args.command == "adopt-skill":
        record = adopt_skill_file(
            baseline_dir=args.repository / SKILL_BASELINE_RELATIVE_DIR,
            upstream_dir=args.upstream,
            data_dir=agentflow_home(args.data_dir),
            path=args.path,
            approved_by=args.approved_by,
        )
        print(json.dumps(record, sort_keys=True))
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
