from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .agent_adapter import AgentAdapter
from .contracts import validate_builder_report, validate_plan, validate_review
from .run_kernel import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    acquire_claim,
    append_event,
    default_claim_holder,
    read_run_status,
    release_claim,
)


@dataclass(frozen=True)
class AdvancedRun:
    run_id: str
    state: str
    artifact: Path
    candidate_sha: str | None = None


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.rstrip("\n")


def _changed_files(workspace: Path) -> list[str]:
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=workspace)
    changed: list[str] = []
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(path)
    return sorted(changed)


def advance_run(
    *,
    run_id: str,
    data_dir: Path,
    adapter: AgentAdapter | None,
    claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
) -> AdvancedRun:
    holder = default_claim_holder()
    acquire_claim(
        data_dir=data_dir,
        run_id=run_id,
        holder=holder,
        lease_seconds=claim_lease_seconds,
    )
    try:
        return _advance_claimed_run(
            run_id=run_id,
            data_dir=data_dir,
            adapter=adapter,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


def _advance_claimed_run(
    *,
    run_id: str,
    data_dir: Path,
    adapter: AgentAdapter | None,
) -> AdvancedRun:
    status = read_run_status(run_id=run_id, data_dir=data_dir)
    if status.state not in {"ready", "planned", "built", "verified"}:
        raise ValueError(f"run {run_id} cannot advance from state {status.state}")
    if status.worktree is None:
        raise ValueError(f"run {run_id} has no Workspace")
    run_dir = data_dir / "runs" / run_id
    profile_evidence_path = run_dir / "profile.json"
    if not profile_evidence_path.exists():
        raise ValueError(f"run {run_id} has no Repository Profile evidence")
    profile_evidence = json.loads(profile_evidence_path.read_text(encoding="utf-8"))
    if profile_evidence["fresh"] is not True:
        raise ValueError(f"run {run_id} captured a stale Repository Profile")
    workspace = Path(status.worktree)
    profile_path = workspace / profile_evidence["path"]
    profile_bytes = profile_path.read_bytes()
    profile_hash = hashlib.sha256(profile_bytes).hexdigest()
    if profile_hash != profile_evidence["profile_sha256"]:
        raise ValueError(f"run {run_id} Repository Profile integrity check failed")

    task = json.loads((run_dir / "task.json").read_text(encoding="utf-8"))
    profile = json.loads(profile_bytes)
    if status.state == "ready":
        if adapter is None:
            raise ValueError("the planner stage requires an Agent Adapter")
        plan = validate_plan(
            adapter.invoke(
                role="planner",
                request={"profile": profile, "task": task},
                workspace=workspace,
            )
        )
        artifact = run_dir / "plan.json"
        artifact.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="plan_ready",
            adapter=adapter.name,
            artifact=str(artifact),
        )
        return AdvancedRun(run_id=run_id, state="planned", artifact=artifact)

    if status.state == "built":
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        candidate_sha = next(
            event["candidate_sha"]
            for event in reversed(events)
            if event["type"] == "build_ready"
        )
        if _git("rev-parse", "HEAD", cwd=workspace) != candidate_sha:
            raise ValueError("Workspace HEAD no longer matches the candidate SHA")
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError("Workspace is not clean at the candidate SHA")
        checks = []
        all_passed = True
        environment = {
            **os.environ,
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
        for command in profile["checks"]:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                text=True,
                capture_output=True,
                timeout=1800,
                check=False,
            )
            checks.append(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "stderr": completed.stderr,
                    "stdout": completed.stdout,
                }
            )
            if completed.returncode != 0:
                all_passed = False
        workspace_clean = not _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if not workspace_clean:
            all_passed = False
        artifact = run_dir / "checks.json"
        artifact.write_text(
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "checks": checks,
                    "workspace_clean": workspace_clean,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        event_type = "checks_passed" if all_passed else "checks_failed"
        state = "verified" if all_passed else "failed"
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type=event_type,
            artifact=str(artifact),
            candidate_sha=candidate_sha,
        )
        return AdvancedRun(
            run_id=run_id,
            state=state,
            artifact=artifact,
            candidate_sha=candidate_sha,
        )

    if status.state == "verified":
        if adapter is None:
            raise ValueError("the reviewer stage requires an Agent Adapter")
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        candidate_sha = next(
            event["candidate_sha"]
            for event in reversed(events)
            if event["type"] == "checks_passed"
        )
        before_head = _git("rev-parse", "HEAD", cwd=workspace)
        before_status = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if before_head != candidate_sha or before_status:
            raise ValueError("verified Workspace is not clean at the candidate SHA")
        review = validate_review(
            adapter.invoke(
                role="reviewer",
                request={
                    "checks": json.loads(
                        (run_dir / "checks.json").read_text(encoding="utf-8")
                    ),
                    "plan": json.loads(
                        (run_dir / "plan.json").read_text(encoding="utf-8")
                    ),
                    "base_sha": status.base_sha,
                    "candidate_sha": candidate_sha,
                    "task": task,
                },
                workspace=workspace,
            )
        )
        after_head = _git("rev-parse", "HEAD", cwd=workspace)
        after_status = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if after_head != before_head or after_status != before_status:
            raise ValueError("reviewer modified the read-only Workspace")
        artifact = run_dir / "review.json"
        artifact.write_text(
            json.dumps(review, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        has_blocker = any(
            finding["severity"] == "blocker" for finding in review["findings"]
        )
        if review["disposition"] != "approve" or has_blocker:
            append_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="review_blocked",
                adapter=adapter.name,
                artifact=str(artifact),
                candidate_sha=candidate_sha,
            )
            return AdvancedRun(
                run_id=run_id,
                state="changes_requested",
                artifact=artifact,
                candidate_sha=candidate_sha,
            )
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="review_ready",
            adapter=adapter.name,
            artifact=str(artifact),
            candidate_sha=candidate_sha,
        )
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="awaiting_human",
            candidate_sha=candidate_sha,
        )
        return AdvancedRun(
            run_id=run_id,
            state="awaiting_human",
            artifact=artifact,
            candidate_sha=candidate_sha,
        )

    if adapter is None:
        raise ValueError("the builder stage requires an Agent Adapter")
    plan = validate_plan(
        json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    )
    report = validate_builder_report(
        adapter.invoke(
            role="builder",
            request={"plan": plan, "profile": profile, "task": task},
            workspace=workspace,
        )
    )
    changed_files = _changed_files(workspace)
    unexpected = sorted(set(changed_files) - set(plan["files_to_modify"]))
    if unexpected:
        raise ValueError(f"builder changed files outside the plan: {unexpected}")
    if sorted(report["files_changed"]) != changed_files:
        raise ValueError(
            "builder report files_changed does not match the authoritative Git diff"
        )
    if report["unresolved_issues"]:
        raise ValueError("builder reported unresolved issues")
    artifact = run_dir / "build-report.json"
    artifact.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git("add", "--all", cwd=workspace)
    _git("commit", "-m", f"Agentflow run {run_id} build", cwd=workspace)
    candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
    append_event(
        data_dir=data_dir,
        run_id=run_id,
        event_type="build_ready",
        adapter=adapter.name,
        artifact=str(artifact),
        candidate_sha=candidate_sha,
    )
    return AdvancedRun(
        run_id=run_id,
        state="built",
        artifact=artifact,
        candidate_sha=candidate_sha,
    )
