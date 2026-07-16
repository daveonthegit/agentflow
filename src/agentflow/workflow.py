from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import time
from typing import Callable, Mapping

from .agent_adapter import AgentAdapter
from .contracts import (
    validate_builder_report,
    validate_review,
    validate_tester_report,
)
from .reviewer import GATE_BLOCKED, gate_decision
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


def _git_optional(*args: str, cwd: Path) -> str:
    """Run a git command that may legitimately exit nonzero, returning stdout.

    Used for reads like ``config --get`` where an unset key exits 1 rather than
    signalling an error.
    """
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip()


def _capture_workspace_guard(workspace: Path) -> dict:
    """Fingerprint the parts of a Workspace that ``git status`` cannot see.

    Covers the two enforcement blind spots: git hooks (which execute at the next
    in-Workspace commit, so a planted hook is arbitrary code execution) and
    ignored files (which affect the authoritative checks yet never enter the
    committed candidate). The resolved hooks directory is read via
    ``rev-parse --git-path hooks`` so it is correct for both plain checkouts and
    Git worktrees, and ``core.hooksPath`` is captured explicitly so repointing it
    is detected even before the new directory has any content.
    """
    hooks_dir_raw = _git("rev-parse", "--git-path", "hooks", cwd=workspace)
    hooks_dir = Path(hooks_dir_raw)
    if not hooks_dir.is_absolute():
        hooks_dir = (workspace / hooks_dir).resolve()
    hook_files: dict[str, str] = {}
    if hooks_dir.is_dir():
        for path in sorted(hooks_dir.rglob("*")):
            if path.is_file():
                hook_files[str(path.relative_to(hooks_dir))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    ignored = sorted(
        line[3:]
        for line in _git(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
            cwd=workspace,
        ).splitlines()
        if line.startswith("!!")
    )
    return {
        "hooks_dir": str(hooks_dir),
        "hooks_path_config": _git_optional(
            "config", "--get", "core.hooksPath", cwd=workspace
        ),
        "hook_files": hook_files,
        "ignored": ignored,
    }


def _assert_workspace_guard(workspace: Path, before: dict) -> None:
    """Fail the stage if git hooks, ``core.hooksPath``, or ignored files changed.

    Applies to every enforcement point (builder, tester, reviewer): no Agent
    Role may alter git-hook execution or leave ignored state that could sway the
    authoritative checks without entering the candidate.
    """
    after = _capture_workspace_guard(workspace)
    if after["hook_files"] != before["hook_files"] or (
        after["hooks_dir"] != before["hooks_dir"]
        or after["hooks_path_config"] != before["hooks_path_config"]
    ):
        raise ValueError(
            "Workspace git hooks or core.hooksPath changed during the stage"
        )
    introduced = sorted(set(after["ignored"]) - set(before["ignored"]))
    if introduced:
        raise ValueError(
            f"stage introduced ignored files not in the candidate: {introduced}"
        )


def _read_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _latest_candidate_sha(events: list[dict]) -> str:
    for event in reversed(events):
        if event["type"] == "candidate_rebased":
            return event["new_candidate_sha"]
        if event["type"] in (
            "build_ready",
            "repair_ready",
            "tests_ready",
            "tests_failed",
        ):
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


def _enforce_builder_report(
    *,
    report: dict,
    workspace: Path,
) -> list[str]:
    """Confine the builder by self-consistency, not a pre-declared file list.

    The builder's reported ``files_changed`` must equal the authoritative Git
    diff, so it cannot misreport its scope, and it must report no unresolved
    issues. What the change may touch is bounded by the real gate — authoritative
    checks, the tester's acceptance-criteria tests, the read-only review of the
    full diff, and exact-SHA human approval — not by a planner's guess.
    """
    changed_files = _changed_files(workspace)
    if sorted(report["files_changed"]) != changed_files:
        raise ValueError(
            "builder report files_changed does not match the authoritative Git diff"
        )
    if report["unresolved_issues"]:
        raise ValueError("builder reported unresolved issues")
    return changed_files


def _is_under_test_paths(path: str, test_paths: list[str]) -> bool:
    candidate = PurePosixPath(path)
    for test_path in test_paths:
        base = PurePosixPath(test_path)
        if candidate == base:
            return True
        try:
            candidate.relative_to(base)
        except ValueError:
            continue
        return True
    return False


def _enforce_tester_report(
    *,
    report: dict,
    workspace: Path,
    test_paths: list[str],
) -> list[str]:
    changed_files = _changed_files(workspace)
    if sorted(report["files_changed"]) != changed_files:
        raise ValueError(
            "tester report files_changed does not match the authoritative Git diff"
        )
    offending = sorted(
        path for path in changed_files if not _is_under_test_paths(path, test_paths)
    )
    if offending:
        raise ValueError(
            f"tester changed files outside the declared test paths: {offending}"
        )
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
            holder=holder,
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
    holder: str,
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
        # 'planned' remains advanceable only so a legacy Run created before the
        # planner was retired can still be built; new Runs go ready -> built.
        "planned",
        "built",
        "verified",
        "tested",
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
            holder=holder,
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
            raise ValueError("the tester stage requires an Agent Adapter")
        test_paths = profile.get("test_paths")
        if not test_paths:
            raise ValueError(
                f"run {run_id} Repository Profile declares no test_paths; "
                "regenerate the profile with --test-path, commit it, and start "
                "a new Run"
            )
        events = _read_events(run_dir)
        checks_event = next(
            event for event in reversed(events) if event["type"] == "checks_passed"
        )
        candidate_sha = checks_event["candidate_sha"]
        if _git("rev-parse", "HEAD", cwd=workspace) != candidate_sha:
            raise ValueError(
                "verified Workspace HEAD no longer matches the candidate SHA"
            )
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError("verified Workspace is not clean at the candidate SHA")
        checks_path = _artifact_path(run_dir, checks_event, "checks.json")
        # G is fixed across the tester commit: tests_ready is excluded from
        # _candidate_generation, so post-tests checks land in a distinct
        # checks-<G>-post-tests.json without overwriting checks-<G>.json.
        generation = _candidate_generation(events)
        transcript_path = run_dir / f"tester-{generation}-transcript.jsonl"
        workspace_guard = _capture_workspace_guard(workspace)
        report = validate_tester_report(
            adapter.invoke(
                role="tester",
                request={
                    "checks": json.loads(checks_path.read_text(encoding="utf-8")),
                    "profile": profile,
                    "base_sha": status.base_sha,
                    "candidate_sha": candidate_sha,
                    "task": task,
                    "test_paths": test_paths,
                },
                workspace=workspace,
                transcript_path=transcript_path,
            )
        )
        _assert_workspace_guard(workspace, workspace_guard)
        changed_files = _enforce_tester_report(
            report=report, workspace=workspace, test_paths=test_paths
        )
        artifact = run_dir / f"tester-report-{generation}.json"
        artifact.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not changed_files:
            # The tester wrote no tests; the existing checks evidence still
            # proves the unchanged candidate, so checks are not re-run.
            append_event(
                data_dir=data_dir,
                holder=holder,
                run_id=run_id,
                event_type="tests_ready",
                adapter=adapter.name,
                artifact=str(artifact),
                candidate_sha=candidate_sha,
                checks_artifact=str(checks_path),
                **_transcript_field(transcript_path),
                **_model_provenance(adapter),
            )
            return AdvancedRun(
                run_id=run_id,
                state="tested",
                artifact=artifact,
                candidate_sha=candidate_sha,
            )
        _git("add", "--all", cwd=workspace)
        _git("commit", "-m", f"Agentflow run {run_id} tests {generation}", cwd=workspace)
        new_candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
        check_env = {
            **os.environ,
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
        }
        fingerprint = {
            **environment_fingerprint(),
            "LANG": check_env["LANG"],
            "PYTHONHASHSEED": check_env["PYTHONHASHSEED"],
            "TZ": check_env["TZ"],
        }
        checks, all_passed = _run_profile_checks(
            commands=profile["checks"],
            workspace=workspace,
            attempt=generation,
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
        post_artifact = run_dir / f"checks-{generation}-post-tests.json"
        post_artifact.write_text(
            json.dumps(
                {
                    "candidate_sha": new_candidate_sha,
                    "checks": checks,
                    "workspace_clean": workspace_clean,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if all_passed:
            append_event(
                data_dir=data_dir,
                holder=holder,
                run_id=run_id,
                event_type="tests_ready",
                adapter=adapter.name,
                artifact=str(artifact),
                candidate_sha=new_candidate_sha,
                checks_artifact=str(post_artifact),
                **_transcript_field(transcript_path),
                **_model_provenance(adapter),
            )
            return AdvancedRun(
                run_id=run_id,
                state="tested",
                artifact=artifact,
                candidate_sha=new_candidate_sha,
            )
        append_event(
            data_dir=data_dir,
            holder=holder,
            run_id=run_id,
            event_type="tests_failed",
            adapter=adapter.name,
            artifact=str(artifact),
            candidate_sha=new_candidate_sha,
            checks_artifact=str(post_artifact),
            findings=report["findings"],
            **_transcript_field(transcript_path),
            **_model_provenance(adapter),
        )
        return AdvancedRun(
            run_id=run_id,
            state="failed",
            artifact=post_artifact,
            candidate_sha=new_candidate_sha,
        )

    if status.state == "tested":
        if adapter is None:
            raise ValueError("the reviewer stage requires an Agent Adapter")
        events = _read_events(run_dir)
        tests_event = next(
            event for event in reversed(events) if event["type"] == "tests_ready"
        )
        candidate_sha = tests_event["candidate_sha"]
        checks_path = Path(tests_event["checks_artifact"])
        before_head = _git("rev-parse", "HEAD", cwd=workspace)
        before_status = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if before_head != candidate_sha or before_status:
            raise ValueError("tested Workspace is not clean at the candidate SHA")
        workspace_guard = _capture_workspace_guard(workspace)
        generation = _candidate_generation(events)
        tester_report_path = _artifact_path(
            run_dir, tests_event, f"tester-report-{generation}.json"
        )
        tester_report = json.loads(tester_report_path.read_text(encoding="utf-8"))
        transcript_path = run_dir / f"reviewer-{generation}-transcript.jsonl"
        review = validate_review(
            adapter.invoke(
                role="reviewer",
                request={
                    "checks": json.loads(checks_path.read_text(encoding="utf-8")),
                    "base_sha": status.base_sha,
                    "candidate_sha": candidate_sha,
                    "task": task,
                    "tester_findings": tester_report["findings"],
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
        _assert_workspace_guard(workspace, workspace_guard)
        artifact = run_dir / f"review-{generation}.json"
        artifact.write_text(
            json.dumps(review, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if gate_decision(review) == GATE_BLOCKED:
            append_event(
                data_dir=data_dir,
                holder=holder,
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
            holder=holder,
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
            holder=holder,
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
                holder=holder,
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
        review_event = next(
            event for event in reversed(events) if event["type"] == "review_blocked"
        )
        review_path = _artifact_path(run_dir, review_event, "review.json")
        review = json.loads(review_path.read_text(encoding="utf-8"))
        transcript_path = run_dir / f"builder-repair-{repair_attempt}-transcript.jsonl"
        workspace_guard = _capture_workspace_guard(workspace)
        report = validate_builder_report(
            adapter.invoke(
                role="builder",
                request={
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
        _assert_workspace_guard(workspace, workspace_guard)
        _enforce_builder_report(report=report, workspace=workspace)
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
            holder=holder,
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
    events = _read_events(run_dir)
    generation = _candidate_generation(events) + 1
    transcript_path = run_dir / f"builder-{generation}-transcript.jsonl"
    workspace_guard = _capture_workspace_guard(workspace)
    report = validate_builder_report(
        adapter.invoke(
            role="builder",
            request={"profile": profile, "task": task},
            workspace=workspace,
            transcript_path=transcript_path,
        )
    )
    _assert_workspace_guard(workspace, workspace_guard)
    _enforce_builder_report(report=report, workspace=workspace)
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
        holder=holder,
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
