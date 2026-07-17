"""Constrained deployment: ships only a merged, post-merge-verified revision.

Deployment is the final shipping step after the merge-safety chain
(``human_approved`` -> ``agentflow merge`` -> ``agentflow verify-merge``).
Like the Merge Agent and Post-Merge Verification, this module is a constrained
executor, not a model-driven Agent Role: every gate is deterministic engine
code, and the provider-specific mechanics live behind a Deployment Adapter
boundary that — mirroring the Agent Adapter rule — must not change workflow
state semantics, verification rules, or approval authority.

Before any deployment action the deterministic gates verify, under the Run's
stage claim:

1. the Run is exactly ``merged`` — an unverified or failed merge cannot ship;
2. write-once merge evidence (``merge.json``) exists and names the exact
   ``merged_sha`` that will ship;
3. write-once Post-Merge Verification evidence
   (``post-merge-verification.json``) exists, passed, and verified that exact
   ``merged_sha`` — a resolved failure lifts the shipping block for *merges*
   but never makes the failed revision deployable;
4. shipping for the Target Repository is not stopped by an unresolved
   Post-Merge Verification failure of any Run (see
   :mod:`agentflow.post_merge`);
5. the Target Repository's committed Repository Profile declares a
   ``deployment`` configuration (adapter name plus its config) — absent or
   malformed configuration refuses deployment by default, exactly like
   ``merge_policy`` refuses merging.

The adapter then receives the revision identity and an isolated detached
checkout of exactly ``merged_sha`` — never a Run Workspace and never the
Target Repository's primary checkout — so what ships is provably the verified
commit. Every adapter execution is recorded as an indexed write-once
``deployment-attempt-<n>.json`` evidence artifact whether it succeeds or
fails; a completed deployment additionally records a ``deployment_completed``
event plus write-once ``deployment.json`` evidence, and a second deployment of
the same Run is deterministically refused. Every refusal is recorded as an
immutable ``deployment_refused`` event before the command fails.

Deployment changes no workflow state semantics: neither
``deployment_completed`` nor ``deployment_refused`` has a ``STATE_BY_EVENT``
entry, so — like ``merge_refused`` and ``post_merge_refused`` — both leave the
Run's replayed state exactly where it was (``merged``, still delivered). The
authority constraint is structural: this module's only event writer,
:func:`append_deployment_event`, refuses every event type outside
``DEPLOYMENT_EVENT_TYPES``, and the module never imports the approval, merge,
or post-merge resolution commands, so it can neither grant approval, perform
or unblock a merge, nor alter verification outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Protocol

from .post_merge import unresolved_post_merge_failures
from .repository_profile import DEPLOYMENT_ADAPTERS, PROFILE_RELATIVE_PATH
from .run_kernel import (
    acquire_claim,
    append_event,
    default_claim_holder,
    read_run_status,
    release_claim,
)

# The only Run Evidence deployment may append. Approval, merge, and post-merge
# events are not in this set, so no code path here can grant approval, record
# a merge, or change a verification outcome.
DEPLOYMENT_EVENT_TYPES = frozenset({"deployment_completed", "deployment_refused"})

# Ceiling for one deploy command, matching the merge CI check timeout.
DEPLOY_COMMAND_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class DeploymentResult:
    run_id: str
    state: str
    adapter: str
    deployed_by: str
    merged_sha: str
    artifact: Path
    attempt_artifact: Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def append_deployment_event(
    *,
    data_dir: Path,
    run_id: str,
    event_type: str,
    holder: str,
    **fields: object,
) -> None:
    """Append deployment evidence; structurally refuse every other event type.

    This is this module's sole writer. Because it validates the event type
    against ``DEPLOYMENT_EVENT_TYPES`` before touching the log, no caller here
    can append ``human_approved``, ``merge_completed``, ``post_merge_resolved``,
    or any other record.
    """
    if event_type not in DEPLOYMENT_EVENT_TYPES:
        raise ValueError(
            "deployment may append only "
            f"{sorted(DEPLOYMENT_EVENT_TYPES)} events, not {event_type!r}"
        )
    append_event(
        data_dir=data_dir,
        run_id=run_id,
        event_type=event_type,
        holder=holder,
        **fields,
    )


class DeploymentAdapter(Protocol):
    """Provider-specific shipping mechanics behind the adapter boundary.

    ``deploy`` receives the revision identity and an isolated checkout of
    exactly that revision — never a Run Workspace or the Target Repository's
    primary checkout — and returns per-step evidence records plus the failure
    reason, or ``None`` when every step succeeded. An adapter must not change
    workflow state semantics, verification rules, or approval authority.
    """

    name: str

    def deploy(
        self,
        *,
        revision: str,
        checkout: Path,
        config: dict,
    ) -> tuple[list[dict], str | None]:
        ...


class DirectoryDeploymentAdapter:
    """Publish the verified revision's content to a target directory.

    Deterministic and observable: the target's previous content is fully
    replaced by the checkout's tracked content (``.git`` excluded), so the
    target always mirrors exactly one revision. A marker file records which
    revision is published.
    """

    name = "directory"

    REVISION_MARKER = ".agentflow-deployed-revision"

    def deploy(
        self,
        *,
        revision: str,
        checkout: Path,
        config: dict,
    ) -> tuple[list[dict], str | None]:
        target = Path(config["target"])
        steps: list[dict] = []
        target.mkdir(parents=True, exist_ok=True)
        removed = sorted(entry.name for entry in target.iterdir())
        for entry in target.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        steps.append(
            {"ok": True, "removed_entries": removed, "step": "prepare_target"}
        )
        published: list[str] = []
        for source in sorted(checkout.rglob("*")):
            relative = source.relative_to(checkout)
            if relative.parts and relative.parts[0] == ".git":
                continue
            destination = target / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                published.append(str(relative))
        (target / self.REVISION_MARKER).write_text(
            revision + "\n", encoding="utf-8"
        )
        steps.append(
            {
                "ok": True,
                "published_files": published,
                "revision_marker": self.REVISION_MARKER,
                "step": "publish",
                "target": str(target),
            }
        )
        return steps, None


class CommandDeploymentAdapter:
    """Run a deploy command declared in the Repository Profile.

    The command runs inside the isolated checkout of the exact verified
    revision, with the revision identity exposed as
    ``AGENTFLOW_DEPLOY_REVISION``. Its full result is recorded as step
    evidence; a non-zero exit fails the deployment.
    """

    name = "command"

    def deploy(
        self,
        *,
        revision: str,
        checkout: Path,
        config: dict,
    ) -> tuple[list[dict], str | None]:
        command = shlex.split(config["command"])
        started_at = datetime.now(timezone.utc)
        completed = subprocess.run(
            command,
            cwd=checkout,
            env={**os.environ, "AGENTFLOW_DEPLOY_REVISION": revision},
            text=True,
            capture_output=True,
            timeout=DEPLOY_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        steps = [
            {
                "command": command,
                "ok": completed.returncode == 0,
                "returncode": completed.returncode,
                "started_at": started_at.isoformat(),
                "stderr": completed.stderr,
                "stdout": completed.stdout,
                "step": "deploy_command",
            }
        ]
        if completed.returncode != 0:
            summary = (completed.stderr.strip() or completed.stdout.strip())[-400:]
            return steps, (
                f"deploy command failed: {shlex.join(command)} "
                f"(exit {completed.returncode})"
                + (f": {summary}" if summary else "")
            )
        return steps, None


def build_deployment_adapter(name: str) -> DeploymentAdapter:
    """Construct the engine-shipped adapter for a validated adapter name."""
    if name == "directory":
        return DirectoryDeploymentAdapter()
    if name == "command":
        return CommandDeploymentAdapter()
    raise ValueError(
        f"unknown deployment adapter {name!r}; expected one of "
        + ", ".join(DEPLOYMENT_ADAPTERS)
    )


def read_deployment_config(repository: Path) -> dict | None:
    """Read the committed ``deployment`` block from the Repository Profile."""
    profile_path = repository / PROFILE_RELATIVE_PATH
    if not profile_path.exists():
        return None
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    deployment = profile.get("deployment")
    return deployment if isinstance(deployment, dict) else None


def evaluate_deployment_config(
    deployment: dict | None, *, repository: Path
) -> tuple[dict | None, str | None]:
    """Return the resolved adapter config, or the refusal reason.

    Absent or malformed configuration refuses deployment: shipping must be
    explicitly permitted by the Target Repository, never assumed — the same
    default-refuse posture as ``merge_policy``. A directory target given as a
    relative path resolves against the repository root, and a target inside
    the Target Repository is refused so deployment can never modify the
    repository it ships from.
    """
    if deployment is None:
        return None, (
            "Repository Profile declares no deployment configuration; "
            "deployment is not permitted"
        )
    adapter = deployment.get("adapter")
    if adapter not in DEPLOYMENT_ADAPTERS:
        return None, (
            f"unknown deployment adapter {adapter!r}; expected one of "
            + ", ".join(DEPLOYMENT_ADAPTERS)
        )
    config = deployment.get("config")
    if not isinstance(config, dict):
        return None, "deployment configuration declares no config object"
    if adapter == "directory":
        target = config.get("target")
        if not isinstance(target, str) or not target.strip():
            return None, "directory deployment declares no target path"
        resolved = Path(target)
        if not resolved.is_absolute():
            resolved = (repository / resolved).resolve()
        else:
            resolved = resolved.resolve()
        repository_root = repository.resolve()
        if resolved == repository_root or repository_root in resolved.parents:
            return None, (
                "directory deployment target must lie outside the Target "
                f"Repository, got {resolved}"
            )
        return {"target": str(resolved)}, None
    command = config.get("command")
    if not isinstance(command, str) or not shlex.split(command):
        return None, "command deployment declares no deploy command"
    return {"command": command}, None


def deploy_run(
    *,
    run_id: str,
    deployed_by: str,
    data_dir: Path,
) -> DeploymentResult:
    """Ship a Run's merged, post-merge-verified revision via its adapter.

    Claim-guarded like every other mutating command: the gates re-read state
    under the stage claim so a concurrent mutation cannot slip between the
    check and the deployment. Every gate failure appends a
    ``deployment_refused`` event and raises. The Run's replayed state is never
    changed: the Run stays ``merged`` whether deployment completes or refuses.
    """
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        run_dir = data_dir / "runs" / run_id

        def _refuse(reason: str, **fields: object) -> ValueError:
            append_deployment_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="deployment_refused",
                holder=holder,
                reason=reason,
                refused_by=deployed_by,
                **fields,
            )
            return ValueError(f"run {run_id} deployment refused: {reason}")

        # Gate 1: only a merged Run can ship. merge_failed, human_approved,
        # and every earlier state refuse.
        if status.state != "merged":
            raise _refuse(
                f"cannot deploy from state {status.state}; a merged and "
                "post-merge-verified revision is required"
            )

        # Gate 2: write-once merge evidence names the exact revision to ship.
        merge_artifact = run_dir / "merge.json"
        if not merge_artifact.is_file():
            raise _refuse("run has no merge evidence to deploy")
        merge_evidence = json.loads(merge_artifact.read_text(encoding="utf-8"))
        merged_sha = merge_evidence["merged_sha"]
        repository = Path(merge_evidence["repository"])
        target_branch = merge_evidence["policy"]["target_branch"]

        # Gate 3: the exact merged commit must be post-merge verified and
        # passing. A recorded failure never becomes deployable — a human
        # resolution lifts the merge shipping stop for *other* work, but the
        # failed revision itself stays unshippable.
        verification_artifact = run_dir / "post-merge-verification.json"
        if not verification_artifact.is_file():
            raise _refuse(
                "run has no post-merge verification evidence; run "
                "`agentflow verify-merge` before deploying",
                merged_sha=merged_sha,
            )
        verification = json.loads(
            verification_artifact.read_text(encoding="utf-8")
        )
        if verification.get("passed") is not True:
            raise _refuse(
                "post-merge verification did not pass for merged commit "
                f"{merged_sha}; a failed revision is never deployable",
                merged_sha=merged_sha,
            )
        if verification.get("merged_sha") != merged_sha:
            raise _refuse(
                "post-merge verification evidence does not match the merged "
                f"commit (verified {verification.get('merged_sha')}, "
                f"merged {merged_sha})",
                merged_sha=merged_sha,
            )

        # Gate 4: shipping stop. While any Run's Post-Merge Verification for
        # this Target Repository has failed and is unresolved, deployments
        # are refused with evidence, exactly like merges.
        blocking = unresolved_post_merge_failures(
            data_dir=data_dir, repository=repository
        )
        if blocking:
            raise _refuse(
                "shipping is blocked for this Target Repository: post-merge "
                f"verification failed for run(s) {', '.join(blocking)} and "
                "is unresolved",
                blocked_by_runs=blocking,
                merged_sha=merged_sha,
            )

        # Gate 5: the Target Repository must explicitly permit deployment.
        deployment_config = read_deployment_config(repository)
        adapter_config, config_reason = evaluate_deployment_config(
            deployment_config, repository=repository
        )
        if config_reason is not None:
            raise _refuse(config_reason, merged_sha=merged_sha)
        assert deployment_config is not None and adapter_config is not None
        adapter = build_deployment_adapter(deployment_config["adapter"])

        # Deployment evidence is write-once: a second deployment of the same
        # Run is deterministically refused, mirroring merge.json.
        artifact = run_dir / "deployment.json"
        if artifact.exists():
            raise _refuse(
                "deployment evidence already recorded for this run",
                merged_sha=merged_sha,
            )
        if not repository.is_dir():
            raise _refuse(
                f"Target Repository {repository} is not available",
                merged_sha=merged_sha,
            )

        # Isolated checkout of the exact verified commit: a detached worktree
        # under the Artifact Store, so the adapter never sees a Run Workspace
        # or the Target Repository's primary checkout, and what ships is
        # provably the merged_sha even if the target branch has moved on.
        checkout = data_dir / "deployments" / run_id
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
                    "deployment checkout is not at the merged commit",
                    merged_sha=merged_sha,
                )
            steps, failure = adapter.deploy(
                revision=merged_sha,
                checkout=checkout,
                config=adapter_config,
            )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            shutil.rmtree(checkout, ignore_errors=True)

        # Each adapter execution is an indexed write-once artifact, pass or
        # fail, so a refused-then-retried deployment never overwrites earlier
        # attempt evidence — the merge-ci-<n>.json convention.
        attempt_index = 1 + sum(
            1 for _ in run_dir.glob("deployment-attempt-*.json")
        )
        attempt_artifact = run_dir / f"deployment-attempt-{attempt_index}.json"
        attempt_artifact.write_text(
            json.dumps(
                {
                    "adapter": adapter.name,
                    "config": adapter_config,
                    "merged_sha": merged_sha,
                    "passed": failure is None,
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                    "run_id": run_id,
                    "steps": steps,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if failure is not None:
            raise _refuse(
                failure,
                adapter=adapter.name,
                attempt_artifact=str(attempt_artifact),
                merged_sha=merged_sha,
            )

        evidence = {
            "adapter": adapter.name,
            "attempt_artifact": str(attempt_artifact),
            "config": adapter_config,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "deployed_by": deployed_by,
            "merged_sha": merged_sha,
            "repository": str(repository),
            "run_id": run_id,
            "steps": steps,
            "target_branch": target_branch,
            "verification_artifact": str(verification_artifact),
        }
        artifact.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        append_deployment_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="deployment_completed",
            holder=holder,
            adapter=adapter.name,
            artifact=str(artifact),
            attempt_artifact=str(attempt_artifact),
            deployed_by=deployed_by,
            merged_sha=merged_sha,
        )
        return DeploymentResult(
            run_id=run_id,
            state="merged",
            adapter=adapter.name,
            deployed_by=deployed_by,
            merged_sha=merged_sha,
            artifact=artifact,
            attempt_artifact=attempt_artifact,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)
