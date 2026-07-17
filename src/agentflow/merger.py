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
3. when the policy marks the target branch ``protected``, the branch has not
   diverged out of band: its current head must already be part of the merge
   candidate's history, because a protected branch advances only through this
   gated merge path;
4. the clean-environment CI gate passes: the candidate's own committed
   Repository Profile checks are re-run against the exact candidate SHA in a
   freshly created, isolated checkout (never the Run's Workspace, which may
   carry ignored local state), and every check must pass.

Every refusal is recorded as an immutable ``merge_refused`` event before the
command fails, and a completed merge is recorded as a ``merge_completed`` event
plus a write-once ``merge.json`` evidence artifact. Each CI gate execution is
recorded as an indexed ``merge-ci-<n>.json`` evidence artifact whether it
passes or fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

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

# Ceiling for one required check in the clean-environment CI gate, matching
# the authoritative-check timeout used by the workflow stages.
CI_CHECK_TIMEOUT_SECONDS = 1800


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
    ci_artifact: Path


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
    if not isinstance(policy.get("protected", False), bool):
        return "merge_policy protected must be a boolean"
    return None


def evaluate_branch_protection(
    policy: dict, *, repository: Path, candidate_sha: str
) -> str | None:
    """Return the protected-branch refusal reason, or None.

    ``protected: true`` means the target branch advances only through this
    gated merge path, so its current head must already be part of the merge
    candidate's history. A head the candidate does not contain means the
    branch moved out of band after the Run's base was captured, and the merge
    must refuse rather than silently absorb or bypass that divergence.
    """
    if policy.get("protected", False) is not True:
        return None
    head_sha = _git("rev-parse", "HEAD", cwd=repository)
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", head_sha, candidate_sha],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        return (
            f"target branch {policy.get('target_branch')!r} is protected and "
            f"has diverged: its head {head_sha} is not in the merge "
            f"candidate's history ({candidate_sha}); a protected branch "
            "advances only through the gated merge path"
        )
    return None


def run_clean_environment_checks(
    *, workspace: Path, candidate_sha: str
) -> tuple[list[dict], str | None]:
    """Run the candidate's committed required checks in a clean environment.

    The environment is a temporary detached ``git worktree`` created at
    exactly ``candidate_sha`` and torn down afterward, so nothing local to
    the Run's Workspace — ignored files, caches, uncommitted tooling — can
    influence the result. The checks themselves come from the Repository
    Profile committed inside the candidate, so the gate verifies exactly what
    would be merged. Returns per-check evidence records and the refusal
    reason, or ``None`` when every required check passed.
    """
    checkout = Path(tempfile.mkdtemp(prefix="agentflow-merge-ci-"))
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(checkout), candidate_sha],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=True,
        )
        profile_path = checkout / PROFILE_RELATIVE_PATH
        if not profile_path.exists():
            return [], (
                "merge candidate commits no Repository Profile; required "
                "checks cannot be verified in a clean environment"
            )
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        commands = profile.get("checks")
        if not isinstance(commands, list) or not commands:
            return [], (
                "merge candidate's Repository Profile declares no required "
                "checks"
            )
        checks: list[dict] = []
        failure: str | None = None
        for command in commands:
            started_at = datetime.now(timezone.utc)
            completed = subprocess.run(
                command,
                cwd=checkout,
                text=True,
                capture_output=True,
                timeout=CI_CHECK_TIMEOUT_SECONDS,
                check=False,
            )
            checks.append(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "started_at": started_at.isoformat(),
                    "stderr": completed.stderr,
                    "stdout": completed.stdout,
                }
            )
            if completed.returncode != 0 and failure is None:
                summary = (
                    completed.stderr.strip() or completed.stdout.strip()
                )[-400:]
                failure = (
                    "required check failed in the clean-environment CI gate: "
                    f"{shlex.join(command)} (exit {completed.returncode})"
                    + (f": {summary}" if summary else "")
                )
        return checks, failure
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(checkout)],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        shutil.rmtree(checkout, ignore_errors=True)


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

        # Gate 3: a protected target branch must not have diverged out of
        # band; it advances only through this gated merge path.
        assert policy is not None  # evaluate_merge_policy refused None above
        protection_reason = evaluate_branch_protection(
            policy, repository=repository, candidate_sha=candidate_sha
        )
        if protection_reason is not None:
            raise _refuse(
                protection_reason,
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

        # Merge evidence is write-once; a second merge of the same Run is
        # already refused by the state gate, and this guards damaged logs.
        run_dir = data_dir / "runs" / run_id
        artifact = run_dir / "merge.json"
        if artifact.exists():
            raise _refuse("merge evidence already recorded for this run")

        # Gate 4: clean-environment CI — the required checks must pass for
        # the exact merge candidate in a freshly created, isolated checkout.
        # Each execution's evidence is an indexed write-once artifact so a
        # refused-then-retried merge never overwrites earlier CI evidence.
        ci_checks, ci_failure = run_clean_environment_checks(
            workspace=workspace, candidate_sha=status.approved_sha
        )
        ci_index = 1 + sum(1 for _ in run_dir.glob("merge-ci-*.json"))
        ci_artifact = run_dir / f"merge-ci-{ci_index}.json"
        ci_artifact.write_text(
            json.dumps(
                {
                    "candidate_sha": status.approved_sha,
                    "checks": ci_checks,
                    "environment": "clean-checkout",
                    "passed": ci_failure is None,
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                    "run_id": run_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if ci_failure is not None:
            raise _refuse(
                ci_failure,
                approved_sha=status.approved_sha,
                candidate_sha=candidate_sha,
                ci_artifact=str(ci_artifact),
            )

        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        approval = next(
            event
            for event in reversed(events)
            if event["type"] == "human_approved"
        )

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
            "ci": {
                "artifact": str(ci_artifact),
                "candidate_sha": status.approved_sha,
                "passed": True,
            },
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "merged_by": merged_by,
            "merged_sha": merged_sha,
            "policy": {
                "allow": True,
                "protected": policy.get("protected", False),
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
            ci_artifact=str(ci_artifact),
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
            ci_artifact=ci_artifact,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)
