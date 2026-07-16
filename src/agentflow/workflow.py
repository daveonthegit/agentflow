from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time
from typing import Callable, Mapping

from .agent_adapter import AgentAdapter
from .contracts import (
    validate_builder_report,
    validate_plan,
    validate_planned_paths,
    validate_review,
)
from .run_kernel import (
    DEFAULT_CLAIM_LEASE_SECONDS,
    acquire_claim,
    append_event,
    default_claim_holder,
    read_run_status,
    release_claim,
)

# Bounded repairs after the initial build: advance from changes_requested may
# invoke the builder at most this many times before repair_exhausted.
MAX_REPAIR_ATTEMPTS = 2

CHECK_ENV_ALLOWLIST = ("LANG", "PYTHONHASHSEED", "TZ")


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


def _model_provenance(adapter: AgentAdapter) -> dict[str, str]:
    # Read the single value the adapter resolved during invoke; never
    # re-resolve here, so the CLI argument and the event provenance cannot
    # diverge. Adapters that route no models leave this unset.
    model = getattr(adapter, "last_resolved_model", None)
    if model is None:
        return {}
    return {"model": model}


def _transcript_field(transcript_path: Path) -> dict[str, str]:
    if transcript_path.exists():
        return {"transcript": str(transcript_path)}
    return {}


def _changed_files(workspace: Path) -> list[str]:
    status = _git("status", "--porcelain", "--untracked-files=all", cwd=workspace)
    changed: list[str] = []
    for line in status.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed.append(path)
    return sorted(changed)


def _read_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _latest_candidate_sha(events: list[dict]) -> str:
    for event in reversed(events):
        if event["type"] == "candidate_rebased":
            return event["new_candidate_sha"]
        if event["type"] in ("build_ready", "repair_ready"):
            return event["candidate_sha"]
    raise ValueError("no candidate SHA recorded")


def _candidate_generation(events: list[dict]) -> int:
    """1-based generation for the latest candidate-producing event.

    Every new candidate generation advances the counter: ``build_ready``,
    ``repair_ready``, and ``candidate_rebased``. Checks and reviews after a
    rebase therefore write distinct attempt artifacts and never overwrite
    pre-rebase evidence.
    """
    return sum(
        1
        for event in events
        if event["type"] in ("build_ready", "repair_ready", "candidate_rebased")
    )


def _artifact_path(run_dir: Path, event: dict, legacy_name: str) -> Path:
    artifact = event.get("artifact")
    if artifact:
        return Path(artifact)
    return run_dir / legacy_name


def _effective_plan(run_dir: Path) -> dict:
    """Return the plan with ``files_to_modify`` widened by every amendment.

    Loads and validates immutable ``plan.json``, then returns a NEW plan dict
    whose ``files_to_modify`` is the sorted union of the original list and the
    ``added_paths`` of every ``plan_amended`` event. Never writes ``plan.json``.
    """
    plan = validate_plan(
        json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    )
    added: set[str] = set()
    for event in _read_events(run_dir):
        if event["type"] == "plan_amended":
            added.update(event["added_paths"])
    if not added:
        return plan
    effective = dict(plan)
    effective["files_to_modify"] = sorted(set(plan["files_to_modify"]) | added)
    return effective


def _enforce_builder_report(
    *,
    plan: dict,
    report: dict,
    workspace: Path,
) -> list[str]:
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
    return changed_files



def default_check_environment_fingerprint(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Capture the allowlisted check-environment fingerprint.

    Never records arbitrary process environment variables or secrets.
    """
    env = os.environ if environ is None else environ
    fingerprint = {key: env.get(key, "") for key in CHECK_ENV_ALLOWLIST}
    fingerprint.update(
        {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "os_system": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
        }
    )
    return fingerprint


def _run_profile_checks(
    *,
    commands: list,
    workspace: Path,
    attempt: int,
    environment: dict[str, str],
    environment_fingerprint: dict[str, str],
    clock: Callable[[], datetime],
    monotonic: Callable[[], float],
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[list[dict], bool]:
    checks: list[dict] = []
    all_passed = True
    for command in commands:
        started_at = clock()
        started_mono = monotonic()
        completed = run_command(
            command,
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=1800,
            check=False,
        )
        duration_ms = max(0, int(round((monotonic() - started_mono) * 1000)))
        checks.append(
            {
                "attempt": attempt,
                "command": command,
                "duration_ms": duration_ms,
                "environment": environment_fingerprint,
                "returncode": completed.returncode,
                "started_at": started_at.isoformat(),
                "stderr": completed.stderr,
                "stdout": completed.stdout,
            }
        )
        if completed.returncode != 0:
            all_passed = False
    return checks, all_passed


def advance_run(
    *,
    run_id: str,
    data_dir: Path,
    adapter: AgentAdapter | None,
    claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    environment_fingerprint: Callable[[], dict[str, str]] | None = None,
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
            clock=clock,
            monotonic=monotonic,
            environment_fingerprint=environment_fingerprint,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


def _advance_claimed_run(
    *,
    run_id: str,
    data_dir: Path,
    adapter: AgentAdapter | None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    environment_fingerprint: Callable[[], dict[str, str]] | None = None,
) -> AdvancedRun:
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)
    if monotonic is None:
        monotonic = time.monotonic
    if environment_fingerprint is None:
        environment_fingerprint = default_check_environment_fingerprint

    status = read_run_status(run_id=run_id, data_dir=data_dir)
    if status.state not in {
        "ready",
        "planned",
        "built",
        "verified",
        "changes_requested",
    }:
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
        transcript_path = run_dir / "planner-transcript.jsonl"
        plan = validate_plan(
            adapter.invoke(
                role="planner",
                request={"profile": profile, "task": task},
                workspace=workspace,
                transcript_path=transcript_path,
            )
        )
        validate_planned_paths(plan=plan, workspace=workspace)
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
            **_transcript_field(transcript_path),
            **_model_provenance(adapter),
        )
        return AdvancedRun(run_id=run_id, state="planned", artifact=artifact)

    if status.state == "built":
        events = _read_events(run_dir)
        candidate_sha = _latest_candidate_sha(events)
        if _git("rev-parse", "HEAD", cwd=workspace) != candidate_sha:
            raise ValueError("Workspace HEAD no longer matches the candidate SHA")
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError("Workspace is not clean at the candidate SHA")
        check_env = {
            **os.environ,
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
        attempt = _candidate_generation(events)
        fingerprint = {
            **environment_fingerprint(),
            "LANG": check_env["LANG"],
            "PYTHONHASHSEED": check_env["PYTHONHASHSEED"],
            "TZ": check_env["TZ"],
        }
        checks, all_passed = _run_profile_checks(
            commands=profile["checks"],
            workspace=workspace,
            attempt=attempt,
            environment=check_env,
            environment_fingerprint=fingerprint,
            clock=clock,
            monotonic=monotonic,
        )
        workspace_clean = not _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if not workspace_clean:
            all_passed = False
        generation = attempt
        artifact = run_dir / f"checks-{generation}.json"
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
        events = _read_events(run_dir)
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
        checks_event = next(
            event for event in reversed(events) if event["type"] == "checks_passed"
        )
        checks_path = _artifact_path(run_dir, checks_event, "checks.json")
        generation = _candidate_generation(events)
        transcript_path = run_dir / f"reviewer-{generation}-transcript.jsonl"
        review = validate_review(
            adapter.invoke(
                role="reviewer",
                request={
                    "checks": json.loads(checks_path.read_text(encoding="utf-8")),
                    "plan": _effective_plan(run_dir),
                    "base_sha": status.base_sha,
                    "candidate_sha": candidate_sha,
                    "task": task,
                },
                workspace=workspace,
                transcript_path=transcript_path,
            )
        )
        after_head = _git("rev-parse", "HEAD", cwd=workspace)
        after_status = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if after_head != before_head or after_status != before_status:
            raise ValueError("reviewer modified the read-only Workspace")
        artifact = run_dir / f"review-{generation}.json"
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
                **_transcript_field(transcript_path),
                **_model_provenance(adapter),
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
            **_transcript_field(transcript_path),
            **_model_provenance(adapter),
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

    if status.state == "changes_requested":
        events = _read_events(run_dir)
        repair_count = sum(1 for event in events if event["type"] == "repair_ready")
        if repair_count >= MAX_REPAIR_ATTEMPTS:
            artifact = run_dir / "repair-exhausted.json"
            artifact.write_text(
                json.dumps(
                    {
                        "max_repair_attempts": MAX_REPAIR_ATTEMPTS,
                        "repair_ready_count": repair_count,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            append_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="repair_exhausted",
                artifact=str(artifact),
            )
            return AdvancedRun(run_id=run_id, state="failed", artifact=artifact)
        if adapter is None:
            raise ValueError("the builder stage requires an Agent Adapter")
        repair_attempt = repair_count + 1
        candidate_sha = _latest_candidate_sha(events)
        if _git("rev-parse", "HEAD", cwd=workspace) != candidate_sha:
            raise ValueError("Workspace HEAD no longer matches the candidate SHA")
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError("Workspace is not clean at the candidate SHA")
        plan = _effective_plan(run_dir)
        review_event = next(
            event for event in reversed(events) if event["type"] == "review_blocked"
        )
        review_path = _artifact_path(run_dir, review_event, "review.json")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        transcript_path = run_dir / f"builder-repair-{repair_attempt}-transcript.jsonl"
        report = validate_builder_report(
            adapter.invoke(
                role="builder",
                request={
                    "plan": plan,
                    "profile": profile,
                    "task": task,
                    "review": review,
                    "candidate_sha": candidate_sha,
                    "repair_attempt": repair_attempt,
                },
                workspace=workspace,
                transcript_path=transcript_path,
            )
        )
        _enforce_builder_report(plan=plan, report=report, workspace=workspace)
        artifact = run_dir / f"repair-report-{repair_attempt}.json"
        artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _git("add", "--all", cwd=workspace)
        _git(
            "commit",
            "-m",
            f"Agentflow run {run_id} repair {repair_attempt}",
            cwd=workspace,
        )
        new_candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="repair_ready",
            adapter=adapter.name,
            artifact=str(artifact),
            candidate_sha=new_candidate_sha,
            repair_attempt=repair_attempt,
            **_transcript_field(transcript_path),
            **_model_provenance(adapter),
        )
        return AdvancedRun(
            run_id=run_id,
            state="built",
            artifact=artifact,
            candidate_sha=new_candidate_sha,
        )

    if adapter is None:
        raise ValueError("the builder stage requires an Agent Adapter")
    plan = _effective_plan(run_dir)
    events = _read_events(run_dir)
    generation = _candidate_generation(events) + 1
    transcript_path = run_dir / f"builder-{generation}-transcript.jsonl"
    report = validate_builder_report(
        adapter.invoke(
            role="builder",
            request={"plan": plan, "profile": profile, "task": task},
            workspace=workspace,
            transcript_path=transcript_path,
        )
    )
    _enforce_builder_report(plan=plan, report=report, workspace=workspace)
    artifact = run_dir / f"build-report-{generation}.json"
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
        **_transcript_field(transcript_path),
        **_model_provenance(adapter),
    )
    return AdvancedRun(
        run_id=run_id,
        state="built",
        artifact=artifact,
        candidate_sha=candidate_sha,
    )
