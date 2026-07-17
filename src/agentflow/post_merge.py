"""Post-Merge Verification and human-reviewed Recovery Proposals.

After the Merge Agent completes a merge, :func:`verify_merged_run` runs the
Repository Profile's authoritative checks against the exact ``merged_sha`` in
an isolated detached checkout of that commit — never a Run Workspace and never
the Target Repository's primary checkout — and records the result as immutable
Run Evidence (a write-once ``post-merge-verification.json`` artifact plus a
``post_merge_verified`` or ``post_merge_failed`` event).

A failure stops further shipping: while any Run for a Target Repository sits in
``merge_failed`` (a ``post_merge_failed`` event without a subsequent
``post_merge_resolved``), :func:`unresolved_post_merge_failures` reports it and
the Merge Agent deterministically refuses every further merge into that
repository, recording ``merge_refused`` evidence.

A failure also produces a Recovery Proposal: a *record* under
``<data_dir>/proposals/<proposal_id>/recovery-proposal.json`` following the
Improvement Proposal conventions (content-derived id, state derived from files
on disk), naming what failed, the merged SHA, and proposed recovery options
such as a revert or forward fix. Agentflow never executes any option; a human
reviews the proposal and records an attributed resolution with
:func:`resolve_post_merge_failure`, which appends ``post_merge_resolved`` and
lifts the shipping block. Nothing here ever modifies the Target Repository.

Like the Merge Agent, this module is a constrained executor: its only event
writer, :func:`append_post_merge_event`, refuses every event type outside
``POST_MERGE_EVENT_TYPES``, and the module never imports the approval or merge
commands, so it can neither grant approval nor perform a merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .repository_profile import PROFILE_RELATIVE_PATH
from .run_kernel import (
    acquire_claim,
    append_event,
    default_claim_holder,
    list_runs,
    read_run_status,
    release_claim,
)
from .workflow import _run_profile_checks, default_check_environment_fingerprint

# The only Run Evidence this module may append. Approval and merge events are
# not in this set, so no code path here can grant approval or record a merge.
POST_MERGE_EVENT_TYPES = frozenset(
    {
        "post_merge_verified",
        "post_merge_failed",
        "post_merge_refused",
        "post_merge_resolved",
    }
)

RECOVERY_KIND_POST_MERGE_FAILURE = "post_merge_failure"
RECOVERY_PROPOSAL_ID_LENGTH = 16
RECOVERY_PROPOSAL_FILENAME = "recovery-proposal.json"
RECOVERY_RESOLUTION_FILENAME = "resolution.json"


@dataclass(frozen=True)
class PostMergeVerification:
    run_id: str
    state: str
    passed: bool
    merged_sha: str
    verified_by: str
    artifact: Path
    recovery_proposal_id: str | None = None


@dataclass(frozen=True)
class PostMergeResolution:
    run_id: str
    state: str
    resolved_by: str
    resolution: str
    recovery_proposal_id: str


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def append_post_merge_event(
    *,
    data_dir: Path,
    run_id: str,
    event_type: str,
    holder: str,
    **fields: object,
) -> None:
    """Append post-merge evidence; structurally refuse every other event type.

    This is this module's sole writer. Because it validates the event type
    against ``POST_MERGE_EVENT_TYPES`` before touching the log, no caller here
    can append ``human_approved``, ``merge_completed``, or any other record.
    """
    if event_type not in POST_MERGE_EVENT_TYPES:
        raise ValueError(
            "Post-Merge Verification may append only "
            f"{sorted(POST_MERGE_EVENT_TYPES)} events, not {event_type!r}"
        )
    append_event(
        data_dir=data_dir,
        run_id=run_id,
        event_type=event_type,
        holder=holder,
        **fields,
    )


def recovery_proposal_id_for(run_id: str, merged_sha: str) -> str:
    """Content-derived stable id, mirroring Improvement Proposal ids."""
    payload = json.dumps(
        {
            "kind": RECOVERY_KIND_POST_MERGE_FAILURE,
            "merged_sha": merged_sha,
            "run_id": run_id,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[
        :RECOVERY_PROPOSAL_ID_LENGTH
    ]


def _recovery_dir(data_dir: Path, proposal_id: str) -> Path:
    return data_dir / "proposals" / proposal_id


def read_recovery_proposal(*, data_dir: Path, proposal_id: str) -> dict:
    """Return the Recovery Proposal record with its derived state.

    State is derived from evidence on disk, mirroring Run State and
    Improvement Proposals: ``proposed`` until an attributed resolution record
    exists, then ``resolved``.
    """
    proposal_dir = _recovery_dir(data_dir, proposal_id)
    proposal_path = proposal_dir / RECOVERY_PROPOSAL_FILENAME
    if not proposal_path.is_file():
        raise ValueError(f"no Recovery Proposal {proposal_id}")
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    resolution_path = proposal_dir / RECOVERY_RESOLUTION_FILENAME
    if resolution_path.is_file():
        record["resolution"] = json.loads(
            resolution_path.read_text(encoding="utf-8")
        )
        record["state"] = "resolved"
    else:
        record["state"] = "proposed"
    return record


def list_recovery_proposals(*, data_dir: Path) -> list[dict]:
    proposals_dir = data_dir / "proposals"
    if not proposals_dir.is_dir():
        return []
    return [
        read_recovery_proposal(data_dir=data_dir, proposal_id=entry.name)
        for entry in sorted(proposals_dir.iterdir())
        if (entry / RECOVERY_PROPOSAL_FILENAME).is_file()
    ]


def unresolved_post_merge_failures(
    *, data_dir: Path, repository: Path
) -> list[str]:
    """Run ids whose post-merge verification failed and is unresolved.

    A Run counts while its replayed state is ``merge_failed`` — a
    ``post_merge_failed`` event without a subsequent ``post_merge_resolved`` —
    and it targeted this Target Repository. The Merge Agent refuses every
    merge into the repository while this list is non-empty.
    """
    repository_key = str(repository)
    return sorted(
        run.run_id
        for run in list_runs(data_dir=data_dir)
        if run.state == "merge_failed" and run.repository == repository_key
    )


def _write_recovery_proposal(
    *,
    data_dir: Path,
    run_id: str,
    merged_sha: str,
    repository: str,
    target_branch: str,
    artifact: Path,
    failed_checks: list[str],
) -> str:
    proposal_id = recovery_proposal_id_for(run_id, merged_sha)
    record = {
        "evidence": [{"artifact": str(artifact), "run_id": run_id}],
        "failed_checks": failed_checks,
        "kind": RECOVERY_KIND_POST_MERGE_FAILURE,
        "merged_sha": merged_sha,
        "options": [
            {
                "kind": "revert",
                "description": (
                    f"Revert merged commit {merged_sha} on branch "
                    f"{target_branch!r} and start a new Run for the change"
                ),
            },
            {
                "kind": "forward_fix",
                "description": (
                    "Start a new Run that repairs the target branch forward "
                    f"from {merged_sha}"
                ),
            },
        ],
        "proposal_id": proposal_id,
        "repository": repository,
        "requires_human_review": True,
        "run_id": run_id,
    }
    proposal_dir = _recovery_dir(data_dir, proposal_id)
    proposal_dir.mkdir(parents=True, exist_ok=True)
    (proposal_dir / RECOVERY_PROPOSAL_FILENAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return proposal_id


def verify_merged_run(
    *,
    run_id: str,
    verified_by: str,
    data_dir: Path,
) -> PostMergeVerification:
    """Run the authoritative checks against the exact merged commit.

    Claim-guarded like every other mutating command. The checks run in an
    isolated detached checkout of ``merged_sha`` under the Artifact Store —
    never a Run Workspace and never the Target Repository's primary checkout —
    against the Repository Profile committed at that exact commit. Every gate
    failure appends a ``post_merge_refused`` event and raises. A completed
    verification records write-once evidence and exactly one of
    ``post_merge_verified`` (the Run stays ``merged``) or ``post_merge_failed``
    (the Run becomes ``merge_failed``, shipping stops, and a Recovery Proposal
    is recorded for human review).
    """
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        run_dir = data_dir / "runs" / run_id

        def _refuse(reason: str, **fields: object) -> ValueError:
            append_post_merge_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="post_merge_refused",
                holder=holder,
                reason=reason,
                refused_by=verified_by,
                **fields,
            )
            return ValueError(
                f"run {run_id} post-merge verification refused: {reason}"
            )

        if status.state != "merged":
            raise _refuse(
                f"cannot verify from state {status.state}; a completed merge "
                "is required"
            )
        merge_artifact = run_dir / "merge.json"
        if not merge_artifact.is_file():
            raise _refuse("run has no merge evidence to verify")
        merge_evidence = json.loads(merge_artifact.read_text(encoding="utf-8"))
        merged_sha = merge_evidence["merged_sha"]
        repository = Path(merge_evidence["repository"])
        target_branch = merge_evidence["policy"]["target_branch"]

        # Verification evidence is write-once, like merge evidence.
        artifact = run_dir / "post-merge-verification.json"
        if artifact.exists():
            raise _refuse(
                "post-merge verification evidence already recorded for this run",
                merged_sha=merged_sha,
            )
        if not repository.is_dir():
            raise _refuse(
                f"Target Repository {repository} is not available",
                merged_sha=merged_sha,
            )

        # Isolated checkout of the exact merged commit: a detached worktree
        # under the Artifact Store, so the primary checkout and every Run
        # Workspace stay untouched even if the target branch has moved on.
        checkout = data_dir / "verifications" / run_id
        checkout.parent.mkdir(parents=True, exist_ok=True)
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(checkout), merged_sha],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if added.returncode != 0:
            raise _refuse(
                f"cannot check out merged commit {merged_sha}: "
                + added.stderr.strip(),
                merged_sha=merged_sha,
            )
        try:
            if _git("rev-parse", "HEAD", cwd=checkout) != merged_sha:
                raise _refuse(
                    "verification checkout is not at the merged commit",
                    merged_sha=merged_sha,
                )
            profile_path = checkout / PROFILE_RELATIVE_PATH
            if not profile_path.is_file():
                raise _refuse(
                    "merged commit carries no Repository Profile; there are "
                    "no authoritative checks to run",
                    merged_sha=merged_sha,
                )
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            check_env = {
                **os.environ,
                "LANG": "C.UTF-8",
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
            }
            fingerprint = {
                **default_check_environment_fingerprint(),
                "LANG": check_env["LANG"],
                "PYTHONHASHSEED": check_env["PYTHONHASHSEED"],
                "TZ": check_env["TZ"],
            }
            checks, all_passed = _run_profile_checks(
                commands=profile["checks"],
                workspace=checkout,
                attempt=1,
                environment=check_env,
                environment_fingerprint=fingerprint,
                clock=lambda: datetime.now(timezone.utc),
                monotonic=time.monotonic,
            )
            checkout_clean = not _git(
                "status", "--porcelain", "--untracked-files=all", cwd=checkout
            )
            if not checkout_clean:
                all_passed = False
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )

        evidence = {
            "checkout_clean": checkout_clean,
            "checks": checks,
            "merged_sha": merged_sha,
            "passed": all_passed,
            "repository": str(repository),
            "run_id": run_id,
            "target_branch": target_branch,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verified_by": verified_by,
        }
        artifact.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if all_passed:
            append_post_merge_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="post_merge_verified",
                holder=holder,
                artifact=str(artifact),
                merged_sha=merged_sha,
                verified_by=verified_by,
            )
            return PostMergeVerification(
                run_id=run_id,
                state="merged",
                passed=True,
                merged_sha=merged_sha,
                verified_by=verified_by,
                artifact=artifact,
            )

        failed_checks = sorted(
            " ".join(check["command"])
            if isinstance(check["command"], list)
            else str(check["command"])
            for check in checks
            if check["returncode"] != 0
        )
        if not checkout_clean:
            failed_checks.append(
                "(verification checkout was not clean after the checks)"
            )
        proposal_id = _write_recovery_proposal(
            data_dir=data_dir,
            run_id=run_id,
            merged_sha=merged_sha,
            repository=str(repository),
            target_branch=target_branch,
            artifact=artifact,
            failed_checks=failed_checks,
        )
        append_post_merge_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="post_merge_failed",
            holder=holder,
            artifact=str(artifact),
            merged_sha=merged_sha,
            recovery_proposal_id=proposal_id,
            verified_by=verified_by,
        )
        return PostMergeVerification(
            run_id=run_id,
            state="merge_failed",
            passed=False,
            merged_sha=merged_sha,
            verified_by=verified_by,
            artifact=artifact,
            recovery_proposal_id=proposal_id,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


def resolve_post_merge_failure(
    *,
    run_id: str,
    resolved_by: str,
    resolution: str,
    data_dir: Path,
) -> PostMergeResolution:
    """Record a human-attributed resolution of a failed verification.

    Only this command lifts the shipping block: it appends
    ``post_merge_resolved`` (returning the Run to ``merged``) and records the
    attributed resolution on the Recovery Proposal. It executes nothing — the
    human performs the chosen recovery (revert, forward fix, ...) outside
    Agentflow or through new Runs; this records that they reviewed and decided.
    """
    if not resolution.strip():
        raise ValueError("a resolution requires a non-empty description")
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)

        def _refuse(reason: str, **fields: object) -> ValueError:
            append_post_merge_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="post_merge_refused",
                holder=holder,
                reason=reason,
                refused_by=resolved_by,
                **fields,
            )
            return ValueError(
                f"run {run_id} post-merge resolution refused: {reason}"
            )

        if status.state != "merge_failed":
            raise _refuse(
                f"cannot resolve from state {status.state}; an unresolved "
                "post-merge failure is required"
            )
        events = [
            json.loads(line)
            for line in (data_dir / "runs" / run_id / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        failure = next(
            event
            for event in reversed(events)
            if event["type"] == "post_merge_failed"
        )
        proposal_id = failure["recovery_proposal_id"]
        merged_sha = failure["merged_sha"]
        proposal_dir = _recovery_dir(data_dir, proposal_id)
        if not (proposal_dir / RECOVERY_PROPOSAL_FILENAME).is_file():
            raise _refuse(
                f"Recovery Proposal {proposal_id} is missing",
                recovery_proposal_id=proposal_id,
            )
        resolution_path = proposal_dir / RECOVERY_RESOLUTION_FILENAME
        if resolution_path.exists():
            raise _refuse(
                f"Recovery Proposal {proposal_id} is already resolved",
                recovery_proposal_id=proposal_id,
            )
        resolution_path.write_text(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "resolution": resolution,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                    "resolved_by": resolved_by,
                    "run_id": run_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        append_post_merge_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="post_merge_resolved",
            holder=holder,
            merged_sha=merged_sha,
            recovery_proposal_id=proposal_id,
            resolution=resolution,
            resolved_by=resolved_by,
        )
        return PostMergeResolution(
            run_id=run_id,
            state="merged",
            resolved_by=resolved_by,
            resolution=resolution,
            recovery_proposal_id=proposal_id,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)
