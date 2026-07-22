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
from .drift import detect_work_drift
from .external_validation import (
    ExternalValidationError,
    list_external_validations,
    read_external_status,
    register_external_validation,
    validate_external_task,
)
from .merger import merge_approved_run
from .post_merge import (
    list_recovery_proposals,
    resolve_post_merge_failure,
    verify_merged_run,
)
from .committability import EvidenceNotCommittableError
from .project_setup import initialize_repository
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
from .work_reconcile import apply_reconcile, plan_reconcile
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
        choices=(
            "list",
            "ready",
            "verify",
            "drift",
            "approve",
            "proposals",
            "ingest",
            "reconcile",
            "reconcile-apply",
        ),
    )
    work_parser.add_argument("--repository", type=Path, default=Path("."))
    work_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "for `work drift`: exit nonzero when any drift finding exists "
            "(default observe mode reports findings but always exits 0)"
        ),
    )
    work_parser.add_argument(
        "--approved-by",
        help=(
            "record a human approval of the current Work Graph content "
            "(required for `work approve`)"
        ),
    )
    work_parser.add_argument(
        "--plan",
        type=Path,
        help=(
            "for `work reconcile-apply`: the confirmed reconcile plan JSON "
            "(the edited output of `work reconcile`)"
        ),
    )
    work_parser.add_argument(
        "--confirmed-by",
        help=(
            "for `work reconcile-apply`: the human attributing the confirmed "
            "dispositions (required)"
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
    external_parser = subcommands.add_parser(
        "external",
        help=(
            "validate a caller-owned candidate revision without owning the "
            "worktree, agent, approval, or delivery: register an externally "
            "managed task, run the Repository Profile checks, and persist "
            "replayable validated-or-failed evidence"
        ),
    )
    external_parser.add_argument(
        "mode", choices=("register", "validate", "status", "list")
    )
    external_parser.add_argument(
        "target",
        nargs="?",
        help=(
            "the task summary for `register`, or the External Validation id "
            "for `validate` and `status`"
        ),
    )
    external_parser.add_argument(
        "--worktree",
        type=Path,
        help="caller-owned clean git worktree checked out at the candidate SHA",
    )
    external_parser.add_argument(
        "--repository",
        type=Path,
        help=(
            "caller-owned repository the worktree belongs to; defaults to the "
            "worktree's own top level when omitted"
        ),
    )
    external_parser.add_argument(
        "--candidate-sha",
        help="exact candidate revision; must equal the worktree HEAD",
    )
    external_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        dest="acceptance_criteria",
    )
    external_parser.add_argument(
        "--external-ref",
        help="opaque caller-side task handle recorded as evidence (e.g. a "
        "Firstmate task id)",
    )
    external_parser.add_argument(
        "--validated-by",
        help="identity attributed to the validation run (for validate)",
    )
    external_parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()

    if args.command == "init":
        try:
            result = initialize_repository(
                args.repository, enforcement=args.enforcement
            )
        except EvidenceNotCommittableError as error:
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
            try:
                record = approve_work_graph(
                    repository=args.repository,
                    data_dir=agentflow_home(args.data_dir),
                    approved_by=args.approved_by,
                )
            except EvidenceNotCommittableError as error:
                print(str(error), file=sys.stderr)
                return 1
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
        if args.mode == "drift":
            # A read-only reconciliation report of work landed outside the
            # Work Graph. Reads the Target Repository and its git history
            # alone (no Agentflow Home state, no network) and mutates nothing.
            # In default observe mode it always exits 0 so CI stays green;
            # --strict exits nonzero only when blocking findings exist.
            try:
                report = detect_work_drift(args.repository)
            except ContractError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(
                json.dumps(
                    {
                        "approval_boundary": report.approval_boundary,
                        "analyzed_commits": list(report.analyzed_commits),
                        "findings": [
                            {
                                "commit": finding.commit,
                                "kind": finding.kind,
                                "subject": finding.subject,
                                **(
                                    {"work_item_id": finding.work_item_id}
                                    if finding.work_item_id is not None
                                    else {}
                                ),
                            }
                            for finding in report.findings
                        ],
                        "state": "drift" if report.has_findings else "clean",
                    },
                    sort_keys=True,
                )
            )
            if args.strict and report.has_findings:
                return 1
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
            try:
                result = ingest_proposals(args.repository)
            except EvidenceNotCommittableError as error:
                print(str(error), file=sys.stderr)
                return 1
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
        if args.mode == "reconcile":
            # Planning: walk drift findings and completion-claim proposals into
            # proposed dispositions. Read-only — every disposition is unconfirmed
            # until a human edits this plan and runs `work reconcile-apply`.
            try:
                plan = plan_reconcile(args.repository)
            except ContractError as error:
                print(str(error), file=sys.stderr)
                return 1
            print(
                json.dumps(
                    {
                        "approval_boundary": plan.approval_boundary,
                        "dispositions": plan.dispositions,
                        "ineligible": plan.ineligible,
                        "state": "reconcile_planned",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.mode == "reconcile-apply":
            if args.confirmed_by is None:
                parser.error("work reconcile-apply requires --confirmed-by")
            if args.plan is None:
                parser.error("work reconcile-apply requires --plan <file>")
            try:
                raw = json.loads(args.plan.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                print(f"could not read reconcile plan: {error}", file=sys.stderr)
                return 1
            # Accept either a full `work reconcile` plan document or a bare list
            # of disposition records.
            dispositions = raw["dispositions"] if isinstance(raw, dict) else raw
            try:
                result = apply_reconcile(
                    args.repository,
                    dispositions,
                    confirmed_by=args.confirmed_by,
                )
            except EvidenceNotCommittableError as error:
                print(str(error), file=sys.stderr)
                return 1
            except (ContractError, KeyError, TypeError) as error:
                print(str(error), file=sys.stderr)
                return 1
            print(
                json.dumps(
                    {
                        "applied": result.applied,
                        "external_completions": result.external_completions,
                        "pending_claims": result.pending_claims,
                        "removed_claims": result.removed_claims,
                        "skipped_unconfirmed": result.skipped_unconfirmed,
                        "state": "reconciled",
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

    if args.command == "external":
        data_dir = agentflow_home(args.data_dir)
        if args.mode == "register":
            if args.target is None:
                parser.error("external register requires a task summary")
            if args.worktree is None:
                parser.error("external register requires --worktree")
            if args.candidate_sha is None:
                parser.error("external register requires --candidate-sha")
            try:
                registered = register_external_validation(
                    summary=args.target,
                    worktree=args.worktree,
                    candidate_sha=args.candidate_sha,
                    repository=args.repository,
                    acceptance_criteria=args.acceptance_criteria,
                    external_ref=args.external_ref,
                    data_dir=data_dir,
                )
            except ExternalValidationError as error:
                print(str(error), file=sys.stderr)
                return 1
            response = {
                "candidate_sha": registered.candidate_sha,
                "external_id": registered.external_id,
                "repository": registered.repository,
                "repository_profile_path": registered.repository_profile_path,
                "state": registered.state,
                "worktree": registered.worktree,
            }
            if registered.external_ref is not None:
                response["external_ref"] = registered.external_ref
            print(json.dumps(response, sort_keys=True))
            return 0
        if args.mode == "validate":
            if args.target is None:
                parser.error("external validate requires an external id")
            try:
                result = validate_external_task(
                    external_id=args.target,
                    validated_by=args.validated_by,
                    data_dir=data_dir,
                )
            except ExternalValidationError as error:
                print(str(error), file=sys.stderr)
                return 1
            response = {
                "artifact": str(result.artifact),
                "candidate_sha": result.candidate_sha,
                "external_id": result.external_id,
                "passed": result.passed,
                "state": result.state,
            }
            if result.validated_by is not None:
                response["validated_by"] = result.validated_by
            print(json.dumps(response, sort_keys=True))
            return 0 if result.passed else 1
        if args.mode == "status":
            if args.target is None:
                parser.error("external status requires an external id")
            try:
                status = read_external_status(
                    external_id=args.target, data_dir=data_dir
                )
            except ExternalValidationError as error:
                print(str(error), file=sys.stderr)
                return 1
            response = {
                "candidate_sha": status.candidate_sha,
                "external_id": status.external_id,
                "repository": status.repository,
                "state": status.state,
                "summary": status.summary,
                "worktree": status.worktree,
            }
            if status.repository_profile_path is not None:
                response["repository_profile_path"] = status.repository_profile_path
            if status.external_ref is not None:
                response["external_ref"] = status.external_ref
            if status.acceptance_criteria is not None:
                response["acceptance_criteria"] = status.acceptance_criteria
            if status.validated_by is not None:
                response["validated_by"] = status.validated_by
            if status.checks_artifact is not None:
                response["checks_artifact"] = status.checks_artifact
            if status.source is not None:
                response["source"] = status.source
            print(json.dumps(response, sort_keys=True))
            return 0
        entries = []
        for status in list_external_validations(data_dir=data_dir):
            entry = {
                "candidate_sha": status.candidate_sha,
                "external_id": status.external_id,
                "repository": status.repository,
                "state": status.state,
                "summary": status.summary,
                "worktree": status.worktree,
            }
            if status.external_ref is not None:
                entry["external_ref"] = status.external_ref
            entries.append(entry)
        print(json.dumps(entries, sort_keys=True))
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
