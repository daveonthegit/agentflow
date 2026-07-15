from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Callable, TextIO
import uuid

from .contracts import validate_task_spec
from .repository_profile import inspect_repository_profile

# Run states that require external action; `follow_run` prints a final status
# line and returns once the Run reaches one of them.
FOLLOW_TERMINAL_STATES = frozenset(
    {
        "awaiting_human",
        "changes_requested",
        "failed",
        "abandoned",
        "human_approved",
        "plan_rejected",
        "human_rejected",
    }
)

# Terminal rejection and approval states that must not be abandoned or mutated.
TERMINAL_IMMUTABLE_STATES = frozenset(
    {
        "abandoned",
        "human_approved",
        "plan_rejected",
        "human_rejected",
    }
)

# Must strictly exceed the 3600-second adapter subprocess timeout in
# agent_adapter.py so a live stage cannot lose its claim while its adapter
# subprocess is still permitted to run.
DEFAULT_CLAIM_LEASE_SECONDS = 7200


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    state: str
    worktree: Path


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: str
    summary: str | None
    repository: str | None
    base_sha: str | None
    worktree: str | None
    repository_profile_path: str | None
    candidate_sha: str | None
    approved_sha: str | None
    acceptance_criteria: list[str] | None = None
    source: dict[str, str] | None = None


@dataclass(frozen=True)
class Approval:
    run_id: str
    state: str
    approved_by: str
    approved_sha: str


@dataclass(frozen=True)
class Abandonment:
    run_id: str
    state: str
    abandoned_by: str
    reason: str | None


@dataclass(frozen=True)
class Rejection:
    run_id: str
    state: str
    rejected_by: str
    reason: str | None
    rejected_sha: str | None = None


@dataclass(frozen=True)
class RebasedRun:
    run_id: str
    state: str
    rebased: bool
    base_sha: str
    old_base_sha: str | None = None
    new_base_sha: str | None = None
    old_candidate_sha: str | None = None
    new_candidate_sha: str | None = None


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def start_run(
    *,
    summary: str,
    repository: Path,
    data_dir: Path,
    acceptance_criteria: list[str] | None = None,
    source: dict[str, str] | None = None,
) -> StartedRun:
    task_input: dict[str, Any] = {
        "summary": summary,
        "acceptance_criteria": (
            [] if acceptance_criteria is None else acceptance_criteria
        ),
    }
    if source is not None:
        task_input["source"] = source
    task = validate_task_spec(task_input)
    repository = Path(_git("rev-parse", "--show-toplevel", cwd=repository))
    if _git("status", "--porcelain", "--untracked-files=all", cwd=repository):
        raise ValueError("Target Repository must be clean before starting a Run")
    base_sha = _git("rev-parse", "HEAD", cwd=repository)
    run_id = uuid.uuid4().hex
    run_dir = data_dir / "runs" / run_id
    worktree = data_dir / "worktrees" / run_id
    run_dir.mkdir(parents=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)

    persisted: dict[str, Any] = {
        "acceptance_criteria": task["acceptance_criteria"],
        "summary": task["summary"],
    }
    if "source" in task:
        persisted["source"] = task["source"]
    _write_json(run_dir / "task.json", persisted)
    _write_json(
        run_dir / "repository.json",
        {"base_sha": base_sha, "repository": str(repository)},
    )
    profile = inspect_repository_profile(repository)
    if profile is not None:
        _write_json(
            run_dir / "profile.json",
            {
                "fresh": profile.fresh,
                "path": profile.path,
                "profile_sha256": profile.profile_sha256,
                "source_fingerprint": profile.source_fingerprint,
            },
        )

    branch = f"agentflow/{run_id}"
    _git(
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        base_sha,
        cwd=repository,
    )
    events = [
        {"run_id": run_id, "sequence": 1, "type": "run_created"},
        {
            "base_sha": base_sha,
            "repository": str(repository),
            "sequence": 2,
            "type": "repository_snapshotted",
        },
    ]
    if profile is not None:
        events.append(
            {
                "fresh": profile.fresh,
                "path": profile.path,
                "profile_sha256": profile.profile_sha256,
                "sequence": len(events) + 1,
                "source_fingerprint": profile.source_fingerprint,
                "type": "repository_profile_captured",
            }
        )
    events.append(
        {
            "sequence": len(events) + 1,
            "type": "workspace_ready",
            "worktree": str(worktree),
        }
    )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return StartedRun(run_id=run_id, state="ready", worktree=worktree)


def read_run_status(*, run_id: str, data_dir: Path) -> RunStatus:
    run_dir = data_dir / "runs" / run_id
    state = "unknown"
    worktree: str | None = None
    repository_profile_path: str | None = None
    candidate_sha: str | None = None
    approved_sha: str | None = None
    rebased_base_sha: str | None = None
    state_by_event = {
        "run_created": "created",
        "workspace_ready": "ready",
        "plan_ready": "planned",
        "build_ready": "built",
        "repair_ready": "built",
        "candidate_rebased": "built",
        "checks_passed": "verified",
        "checks_failed": "failed",
        "repair_exhausted": "failed",
        "review_ready": "reviewed",
        "review_blocked": "changes_requested",
        "awaiting_human": "awaiting_human",
        "human_approved": "human_approved",
        "run_abandoned": "abandoned",
        "plan_rejected": "plan_rejected",
        "human_rejected": "human_rejected",
    }
    for line_number, line in enumerate(
        (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        event = json.loads(line)
        sequence = event.get("sequence")
        if sequence is not None and sequence != line_number:
            raise ValueError(
                f"invalid event sequence for run {run_id}: "
                f"expected {line_number}, got {sequence}"
            )
        state = state_by_event.get(event["type"], state)
        if event["type"] == "workspace_ready":
            worktree = event.get("worktree")
        if event["type"] == "repository_profile_captured":
            repository_profile_path = event.get("path")
        if event.get("candidate_sha") is not None:
            candidate_sha = event["candidate_sha"]
        if event["type"] == "candidate_rebased":
            candidate_sha = event["new_candidate_sha"]
            rebased_base_sha = event["new_base_sha"]
        if event["type"] == "human_approved":
            approved_sha = event.get("approved_sha")

    task_path = run_dir / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8")) if task_path.exists() else {}
    repository_path = run_dir / "repository.json"
    repository = (
        json.loads(repository_path.read_text(encoding="utf-8"))
        if repository_path.exists()
        else {}
    )
    criteria = task.get("acceptance_criteria")
    source = task.get("source")
    return RunStatus(
        run_id=run_id,
        state=state,
        summary=task.get("summary"),
        repository=repository.get("repository"),
        base_sha=rebased_base_sha
        if rebased_base_sha is not None
        else repository.get("base_sha"),
        worktree=worktree,
        repository_profile_path=repository_profile_path,
        candidate_sha=candidate_sha,
        approved_sha=approved_sha,
        acceptance_criteria=criteria if criteria else None,
        source=source if isinstance(source, dict) else None,
    )


def _emit_new_lines(path: Path, offset: int, out: TextIO) -> int:
    """Print any complete lines appended to ``path`` past ``offset``.

    Tracks a byte offset and only emits up to the final newline so a
    concurrently-growing file never yields a partial line. Read-only.
    """
    if not path.exists():
        return offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    if not data:
        return offset
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return offset
    complete = data[: last_newline + 1]
    out.write(complete.decode("utf-8"))
    out.flush()
    return offset + len(complete)


def follow_run(
    *,
    run_id: str,
    data_dir: Path,
    out: TextIO | None = None,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Tail a Run's events and live role transcript until it needs a human.

    Prints each new line appended to ``events.jsonl`` and to whichever
    ``<role>-transcript.jsonl`` is growing, projecting Run State each poll, and
    returns after printing a final status line once the Run reaches a state
    requiring external action. Exits immediately when that is already true.
    This function is strictly read-only: it never creates or modifies evidence.
    """
    if out is None:
        out = sys.stdout
    run_dir = data_dir / "runs" / run_id
    events_path = run_dir / "events.jsonl"
    events_offset = 0
    transcript_offsets: dict[Path, int] = {}
    while True:
        events_offset = _emit_new_lines(events_path, events_offset, out)
        for transcript_path in sorted(run_dir.glob("*-transcript.jsonl")):
            transcript_offsets[transcript_path] = _emit_new_lines(
                transcript_path,
                transcript_offsets.get(transcript_path, 0),
                out,
            )
        state = read_run_status(run_id=run_id, data_dir=data_dir).state
        if state in FOLLOW_TERMINAL_STATES:
            out.write(f"run {run_id} {state}\n")
            out.flush()
            return state
        sleep(poll_interval)


def list_runs(*, data_dir: Path, state: str | None = None) -> list[RunStatus]:
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        return []
    keyed: list[tuple[str, RunStatus]] = []
    for run_dir in runs_dir.iterdir():
        events_path = run_dir / "events.jsonl"
        if not events_path.is_file():
            continue
        lines = events_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        status = read_run_status(run_id=run_dir.name, data_dir=data_dir)
        keyed.append((lines[0], status))
    keyed.sort(key=lambda pair: pair[0])
    return [
        status
        for _, status in keyed
        if state is None or status.state == state
    ]


def approve_run(*, run_id: str, approved_by: str, data_dir: Path) -> Approval:
    status = read_run_status(run_id=run_id, data_dir=data_dir)
    if status.state != "awaiting_human":
        raise ValueError(
            f"run {run_id} cannot be approved from state {status.state}"
        )
    if status.candidate_sha is None or status.worktree is None:
        raise ValueError(f"run {run_id} has no approvable candidate SHA")
    workspace = Path(status.worktree)
    head = _git("rev-parse", "HEAD", cwd=workspace)
    dirty = _git("status", "--porcelain", "--untracked-files=all", cwd=workspace)
    if head != status.candidate_sha or dirty:
        raise ValueError(
            f"run {run_id} Workspace no longer matches the verified candidate"
        )
    append_event(
        data_dir=data_dir,
        run_id=run_id,
        event_type="human_approved",
        approved_by=approved_by,
        approved_sha=status.candidate_sha,
    )
    return Approval(
        run_id=run_id,
        state="human_approved",
        approved_by=approved_by,
        approved_sha=status.candidate_sha,
    )


def abandon_run(
    *,
    run_id: str,
    abandoned_by: str,
    reason: str | None = None,
    data_dir: Path,
) -> Abandonment:
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        if status.state in TERMINAL_IMMUTABLE_STATES:
            raise ValueError(
                f"run {run_id} cannot be abandoned from state {status.state}"
            )
        fields = {"abandoned_by": abandoned_by}
        if reason is not None:
            fields["reason"] = reason
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="run_abandoned",
            **fields,
        )
        return Abandonment(
            run_id=run_id,
            state="abandoned",
            abandoned_by=abandoned_by,
            reason=reason,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


def reject_run(
    *,
    run_id: str,
    rejected_by: str,
    reason: str | None = None,
    data_dir: Path,
) -> Rejection:
    """Record an explicit plan or human rejection.

    Conversation text is never rejection evidence: only this command appends
    ``plan_rejected`` or ``human_rejected``. Both states are terminal.
    """
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        fields: dict[str, str] = {"rejected_by": rejected_by}
        if reason is not None:
            fields["reason"] = reason
        if status.state == "planned":
            append_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="plan_rejected",
                **fields,
            )
            return Rejection(
                run_id=run_id,
                state="plan_rejected",
                rejected_by=rejected_by,
                reason=reason,
            )
        if status.state == "awaiting_human":
            if status.candidate_sha is None:
                raise ValueError(f"run {run_id} has no rejectable candidate SHA")
            append_event(
                data_dir=data_dir,
                run_id=run_id,
                event_type="human_rejected",
                rejected_sha=status.candidate_sha,
                **fields,
            )
            return Rejection(
                run_id=run_id,
                state="human_rejected",
                rejected_by=rejected_by,
                reason=reason,
                rejected_sha=status.candidate_sha,
            )
        raise ValueError(f"run {run_id} cannot be rejected from state {status.state}")
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


# Replayed states with a committed candidate that a rebase may refresh.
REBASEABLE_STATES = frozenset(
    {"built", "verified", "changes_requested", "awaiting_human"}
)


def append_event(
    *,
    data_dir: Path,
    run_id: str,
    event_type: str,
    **fields: object,
) -> None:
    events_path = data_dir / "runs" / run_id / "events.jsonl"
    sequence = len(events_path.read_text(encoding="utf-8").splitlines()) + 1
    event = {**fields, "sequence": sequence, "type": event_type}
    with events_path.open("a", encoding="utf-8") as events_file:
        events_file.write(json.dumps(event, sort_keys=True) + "\n")


def rebase_run(*, run_id: str, data_dir: Path) -> RebasedRun:
    """Refresh a committed candidate onto the Target Repository's current main.

    Performs a read-only up-to-date check before acquiring any claim: if the
    Run's recorded base already equals the Target Repository's current main
    head, it returns without appending a single event. Otherwise it acquires
    the Run's stage claim, re-validates the replayed state, rebases the
    Workspace branch onto the new main head, and on clean application appends
    one ``candidate_rebased`` event. On conflict it aborts the rebase, restores
    the prior Workspace HEAD, and raises, leaving state, base, candidate, and
    the Workspace unchanged. It never touches the Target Repository's primary
    checkout, never pushes, and never merges.
    """
    status = read_run_status(run_id=run_id, data_dir=data_dir)
    if status.repository is None:
        raise ValueError(f"run {run_id} has no recorded Target Repository")
    if status.worktree is None:
        raise ValueError(f"run {run_id} has no Workspace")
    if status.state in TERMINAL_IMMUTABLE_STATES or status.state == "failed":
        raise ValueError(
            f"run {run_id} cannot be rebased from state {status.state}"
        )
    new_base_sha = _git("rev-parse", "HEAD", cwd=Path(status.repository))
    if status.base_sha == new_base_sha:
        return RebasedRun(
            run_id=run_id,
            state=status.state,
            rebased=False,
            base_sha=status.base_sha,
        )

    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        if status.state not in REBASEABLE_STATES:
            raise ValueError(
                f"run {run_id} cannot be rebased from state {status.state}"
            )
        if status.worktree is None:
            raise ValueError(f"run {run_id} has no Workspace")
        workspace = Path(status.worktree)
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError(
                f"run {run_id} Workspace must be clean before rebasing"
            )
        old_base_sha = status.base_sha
        old_candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
        rebased = subprocess.run(
            ["git", "rebase", new_base_sha],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if rebased.returncode != 0:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if _git("rev-parse", "HEAD", cwd=workspace) != old_candidate_sha:
                _git("reset", "--hard", old_candidate_sha, cwd=workspace)
            raise ValueError(
                f"run {run_id} rebase onto {new_base_sha} conflicts; "
                "Workspace restored and left unchanged"
            )
        if _git("status", "--porcelain", "--untracked-files=all", cwd=workspace):
            raise ValueError(
                f"run {run_id} Workspace is not clean after rebasing"
            )
        new_candidate_sha = _git("rev-parse", "HEAD", cwd=workspace)
        append_event(
            data_dir=data_dir,
            run_id=run_id,
            event_type="candidate_rebased",
            new_base_sha=new_base_sha,
            new_candidate_sha=new_candidate_sha,
            old_base_sha=old_base_sha,
            old_candidate_sha=old_candidate_sha,
        )
        return RebasedRun(
            run_id=run_id,
            state="built",
            rebased=True,
            base_sha=new_base_sha,
            old_base_sha=old_base_sha,
            new_base_sha=new_base_sha,
            old_candidate_sha=old_candidate_sha,
            new_candidate_sha=new_candidate_sha,
        )
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


def default_claim_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _active_claim(events: list[dict]) -> dict | None:
    active: dict | None = None
    for event in events:
        if event.get("type") == "claim_acquired":
            active = event
        elif event.get("type") in ("claim_released", "claim_expired"):
            active = None
    return active


def acquire_claim(
    *,
    data_dir: Path,
    run_id: str,
    holder: str,
    lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    events_path = data_dir / "runs" / run_id / "events.jsonl"
    with events_path.open("r+", encoding="utf-8") as events_file:
        fcntl.flock(events_file.fileno(), fcntl.LOCK_EX)
        lines = events_file.read().splitlines()
        active = _active_claim([json.loads(line) for line in lines])
        sequence = len(lines)
        if active is not None:
            if now < datetime.fromisoformat(active["expires_at"]):
                raise ValueError(
                    f"run {run_id} is already claimed by {active['holder']} "
                    f"until {active['expires_at']}"
                )
            sequence += 1
            expired = {
                "expires_at": active["expires_at"],
                "holder": active["holder"],
                "sequence": sequence,
                "type": "claim_expired",
            }
            events_file.write(json.dumps(expired, sort_keys=True) + "\n")
        acquired = {
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            "holder": holder,
            "sequence": sequence + 1,
            "type": "claim_acquired",
        }
        events_file.write(json.dumps(acquired, sort_keys=True) + "\n")


def release_claim(*, data_dir: Path, run_id: str, holder: str) -> None:
    events_path = data_dir / "runs" / run_id / "events.jsonl"
    with events_path.open("r+", encoding="utf-8") as events_file:
        fcntl.flock(events_file.fileno(), fcntl.LOCK_EX)
        lines = events_file.read().splitlines()
        active = _active_claim([json.loads(line) for line in lines])
        if active is None or active["holder"] != holder:
            return
        released = {
            "expires_at": active["expires_at"],
            "holder": holder,
            "sequence": len(lines) + 1,
            "type": "claim_released",
        }
        events_file.write(json.dumps(released, sort_keys=True) + "\n")
