"""Constrained Merge Agent: merges only a current Approved Revision.

The Merge Agent is a constrained executor, not a model-driven Agent Role:
every gate and the merge mechanics themselves are deterministic engine code,
so no Agent Adapter can weaken the gates or reinterpret repository policy.

Its approval constraint is structural rather than instructional. This module's
only event writer, :func:`append_merge_event`, refuses every event type outside
``MERGE_EVENT_TYPES``, and the module never imports the approval command, so
the code path that grants approval (``run_kernel.approve_run`` appending a
``human_approved`` event) is unreachable from the merger role. The Merge Agent
records merge evidence; it can never create or modify approval records.

Before any merge action the deterministic gates verify, under the Run's stage
claim:

1. the Run is exactly ``human_approved`` and the Workspace still sits clean at
   the Approved Revision — any drift makes the approval stale and refuses the
   merge;
2. the Target Repository's committed merge policy
   (``merge_policy`` in ``.agentflow/repository-profile.json``) permits the
   operation for the currently checked-out target branch;
3. shipping for the Target Repository is not stopped by an unresolved
   Post-Merge Verification failure (see :mod:`agentflow.post_merge`).

Every refusal is recorded as an immutable ``merge_refused`` event before the
command fails, and a completed merge is recorded as a ``merge_completed`` event
plus a write-once ``merge.json`` evidence artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from .post_merge import unresolved_post_merge_failures
from .repository_profile import MERGE_STRATEGIES, PROFILE_RELATIVE_PATH
from .run_kernel import (
    acquire_claim,
    append_event,
    default_claim_holder,
    read_run_status,
    release_claim,
)

# The only Run Evidence the Merge Agent may append. Approval events are not in
# this set, so the merger role has no code path that grants approval.
MERGE_EVENT_TYPES = frozenset({"merge_completed", "merge_refused"})


@dataclass(frozen=True)
class MergeResult:
    run_id: str
    state: str
    merged_by: str
    approved_sha: str
    merged_sha: str
    target_branch: str
    strategy: str
    artifact: Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def append_merge_event(
    *,
    data_dir: Path,
    run_id: str,
    event_type: str,
    holder: str,
    **fields: object,
) -> None:
    """Append merge evidence; structurally refuse every non-merge event type.

    This is the merger role's sole writer. Because it validates the event type
    against ``MERGE_EVENT_TYPES`` before touching the log, no caller inside the
    merger can append ``human_approved`` or any other approval record.
    """
    if event_type not in MERGE_EVENT_TYPES:
        raise ValueError(
            "the Merge Agent may append only "
            f"{sorted(MERGE_EVENT_TYPES)} events, not {event_type!r}"
        )
    append_event(
        data_dir=data_dir,
        run_id=run_id,
        event_type=event_type,
        holder=holder,
        **fields,
    )


def read_merge_policy(repository: Path) -> dict | None:
    """Read the committed ``merge_policy`` from the Repository Profile."""
    profile_path = repository / PROFILE_RELATIVE_PATH
    if not profile_path.exists():
        return None
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    policy = profile.get("merge_policy")
    return policy if isinstance(policy, dict) else None


def evaluate_merge_policy(
    policy: dict | None, *, current_branch: str
) -> str | None:
    """Return the refusal reason for a merge under ``policy``, or None.

    Absent or malformed policy refuses the merge: merging must be explicitly
    permitted by the Target Repository, never assumed.
    """
    if policy is None:
        return (
            "Repository Profile declares no merge_policy; "
            "merging is not permitted"
        )
    if policy.get("allow") is not True:
        return "repository merge_policy does not permit merging"
    target_branch = policy.get("target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        return "repository merge_policy declares no target_branch"
    if current_branch != target_branch:
        return (
            f"Target Repository is on branch {current_branch!r} but "
            f"merge_policy targets {target_branch!r}"
        )
    strategy = policy.get("strategy", "fast-forward")
    if strategy not in MERGE_STRATEGIES:
        return (
            f"unknown merge_policy strategy {strategy!r}; expected one of "
            + ", ".join(MERGE_STRATEGIES)
        )
    return None


def merge_approved_run(
    *,
    run_id: str,
    merged_by: str,
    data_dir: Path,
) -> MergeResult:
    """Merge a Run's current Approved Revision into the target branch.

    Claim-guarded like every other mutating command: the gates re-read state
    under the stage claim so a concurrent mutation cannot slip between the
    check and the merge. Every gate failure appends a ``merge_refused`` event
    and raises, leaving the Target Repository untouched.
    """
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)

        def _refuse(reason: str, **fields: object) -> ValueError:
            append_merge_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="merge_refused",
                holder=holder,
                reason=reason,
                refused_by=merged_by,
                **fields,
            )
            return ValueError(f"run {run_id} merge refused: {reason}")

        if status.state != "human_approved":
            raise _refuse(
                f"cannot merge from state {status.state}; a current human "
                "approval is required"
            )
        if (
            status.approved_sha is None
            or status.worktree is None
            or status.repository is None
        ):
            raise _refuse("run has no Approved Revision to merge")

        # Gate 1: the approval must be current for the exact merge candidate.
        # Approval binds to an exact commit SHA; any drift in the Workspace
        # (a new commit or uncommitted content) means the candidate no longer
        # matches what the human approved.
        workspace = Path(status.worktree)
        candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
        dirty = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if candidate_sha != status.approved_sha or dirty:
            raise _refuse(
                "approval is stale: the merge candidate no longer matches "
                f"the Approved Revision (approved {status.approved_sha}, "
                f"candidate {candidate_sha}"
                + (", Workspace dirty)" if dirty else ")"),
                approved_sha=status.approved_sha,
                candidate_sha=candidate_sha,
            )

        # Gate 2: the Target Repository's committed policy must permit the
        # merge for the currently checked-out target branch.
        repository = Path(status.repository)
        policy = read_merge_policy(repository)
        current_branch = _git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=repository
        )
        policy_reason = evaluate_merge_policy(
            policy, current_branch=current_branch
        )
        if policy_reason is not None:
            raise _refuse(
                policy_reason,
                approved_sha=status.approved_sha,
                candidate_sha=candidate_sha,
            )
        if _git(
            "status", "--porcelain", "--untracked-files=all", cwd=repository
        ):
            raise _refuse(
                "Target Repository must be clean before merging",
                approved_sha=status.approved_sha,
                candidate_sha=candidate_sha,
            )

        # Gate 3: shipping stop. While any Run's Post-Merge Verification for
        # this Target Repository has failed and is unresolved, every further
        # merge is refused with evidence until a human records a resolution.
        blocking = unresolved_post_merge_failures(
            data_dir=data_dir, repository=repository
        )
        if blocking:
            raise _refuse(
                "shipping is blocked for this Target Repository: post-merge "
                f"verification failed for run(s) {', '.join(blocking)} and "
                "is unresolved",
                approved_sha=status.approved_sha,
                blocked_by_runs=blocking,
                candidate_sha=candidate_sha,
            )

        # Merge evidence is write-once; a second merge of the same Run is
        # already refused by the state gate, and this guards damaged logs.
        artifact = data_dir / "runs" / run_id / "merge.json"
        if artifact.exists():
            raise _refuse("merge evidence already recorded for this run")

        events = [
            json.loads(line)
            for line in (data_dir / "runs" / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        approval = next(
            event
            for event in reversed(events)
            if event["type"] == "human_approved"
        )

        assert policy is not None  # evaluate_merge_policy refused None above
        strategy = policy.get("strategy", "fast-forward")
        if strategy == "fast-forward":
            merge_arguments = ["merge", "--ff-only", status.approved_sha]
        else:
            merge_arguments = [
                "merge",
                "--no-ff",
                "-m",
                f"Agentflow run {run_id} merge",
                status.approved_sha,
            ]
        merged = subprocess.run(
            ["git", *merge_arguments],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if merged.returncode != 0:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            raise _refuse(
                f"git merge failed: {merged.stderr.strip()}",
                approved_sha=status.approved_sha,
                candidate_sha=candidate_sha,
            )
        merged_sha = _git("rev-parse", "HEAD", cwd=repository)

        evidence = {
            "approval": {
                "approved_by": approval["approved_by"],
                "approved_sha": approval["approved_sha"],
                "sequence": approval["sequence"],
            },
            "candidate_sha": status.approved_sha,
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "merged_by": merged_by,
            "merged_sha": merged_sha,
            "policy": {
                "allow": True,
                "strategy": strategy,
                "target_branch": current_branch,
            },
            "repository": str(repository),
            "run_id": run_id,
        }
        artifact.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        append_merge_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="merge_completed",
            holder=holder,
            approval_sequence=approval["sequence"],
            artifact=str(artifact),
            candidate_sha=status.approved_sha,
            merged_by=merged_by,
            merged_sha=merged_sha,
            strategy=strategy,
            target_branch=current_branch,
        )
        return MergeResult(
            run_id=run_id,
            state="merged",
            merged_by=merged_by,
            approved_sha=status.approved_sha,
            merged_sha=merged_sha,
            target_branch=current_branch,
            strategy=strategy,
            artifact=artifact,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)
