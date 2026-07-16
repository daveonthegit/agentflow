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
        "tests_failed",
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

# Runs that are still in flight or waiting on a human — the watch picker and
# reconcile's "live" notion. Failed / abandoned / approved / rejected are out.
LIVE_RUN_STATES = frozenset(
    {
        "ready",
        "planned",
        "built",
        "verified",
        "tested",
        "changes_requested",
        "tests_failed",
        "awaiting_human",
    }
)

SHORT_RUN_ID_LENGTH = 8
SUMMARY_DISPLAY_LENGTH = 60


def short_run_id(run_id: str) -> str:
    """Return a stable short prefix of a Run id for human display."""
    return run_id[:SHORT_RUN_ID_LENGTH]


def truncate_summary(
    summary: str | None, *, max_length: int = SUMMARY_DISPLAY_LENGTH
) -> str:
    """Truncate a task summary for one-line display; full summary stays on status."""
    text = (summary or "").strip() or "(no summary)"
    if len(text) <= max_length:
        return text
    if max_length <= 1:
        return "…"
    return text[: max_length - 1] + "…"


def format_run_choice(status: "RunStatus") -> str:
    """One-line human label: state, truncated summary, short id."""
    return (
        f"{status.state}  {truncate_summary(status.summary)}  "
        f"{short_run_id(status.run_id)}"
    )

# Must strictly exceed the adapter subprocess timeout plus the authoritative
# check budget for a single stage, because the tester stage runs an adapter
# invocation (3600-second timeout in agent_adapter.py) and then re-runs the
# profile checks (1800-second per-command timeout in workflow.py) within one
# claim, so a live stage cannot lose its claim while its work is permitted to
# run.
DEFAULT_CLAIM_LEASE_SECONDS = 14400


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
    # Legacy 'plan_ready'/'plan_rejected'/'plan_amended' events (from before the
    # planner was retired) remain replayable: 'plan_ready'/'plan_rejected' keep
    # their entries below, and 'plan_amended' has none, so the
    # state_by_event.get(type, state) fallback leaves state unchanged for it.
    state_by_event = {
        "run_created": "created",
        "workspace_ready": "ready",
        "plan_ready": "planned",
        "build_ready": "built",
        "repair_ready": "built",
        "candidate_rebased": "built",
        "checks_passed": "verified",
        "checks_failed": "failed",
        "tests_ready": "tested",
        "tests_failed": "tests_failed",
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


def _read_appended_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Return complete lines appended to ``path`` past ``offset``.

    Tracks a byte offset and only yields up to the final newline so a
    concurrently-growing file never returns a partial line. Read-only.
    """
    if not path.exists():
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    if not data:
        return [], offset
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], offset
    complete = data[: last_newline + 1]
    text = complete.decode("utf-8")
    lines = text.splitlines()
    return lines, offset + len(complete)


def _short_sha(value: object) -> str:
    if isinstance(value, str) and len(value) >= 12:
        return value[:12]
    return str(value)


def _truncate(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_watch_event(event: dict[str, Any]) -> str | None:
    """Render one Run event as a single human-readable watch line."""
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    # Claim bookkeeping is high-volume noise while stages run.
    if event_type in {"claim_acquired", "claim_released", "claim_expired"}:
        return None
    parts = [f"event  {event_type}"]
    for key in (
        "candidate_sha",
        "approved_sha",
        "rejected_sha",
        "new_candidate_sha",
        "model",
        "approved_by",
        "abandoned_by",
        "rejected_by",
        "reason",
    ):
        value = event.get(key)
        if value is None or value == "":
            continue
        if key.endswith("_sha"):
            parts.append(f"{key}={_short_sha(value)}")
        else:
            parts.append(f"{key}={_truncate(str(value), 60)}")
    return "  ".join(parts)


def _display_shell_command(command: str) -> str:
    """Prefer the meaningful part of a shell command for watch output."""
    text = command.strip()
    # Builders often wrap work as: cd "<worktree>" && <real command>
    for separator in (" && ", "\n"):
        if separator in text:
            head, _, tail = text.partition(separator)
            if head.lstrip().startswith("cd ") and tail.strip():
                text = tail.strip()
                break
    return _truncate(text, 90)


def _tool_summary(name: str, tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return name
    if name in {"Bash", "Shell", "shell"}:
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return f"{name}  {_display_shell_command(command)}"
    for key in ("file_path", "path", "target_notebook", "uri"):
        path = tool_input.get(key)
        if isinstance(path, str) and path:
            return f"{name}  {Path(path).name}"
    return name


def _assistant_text_blocks(content: object) -> list[str]:
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else []
    if not isinstance(content, list):
        return []
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
        elif block_type == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                lines.append(f"→ {_tool_summary(name, block.get('input'))}")
    return lines


def format_watch_transcript_line(line: str, *, label: str) -> list[str]:
    """Render one transcript JSONL line as zero or more human watch lines."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        text = line.strip()
        return [f"{label}  {text}"] if text else []
    if not isinstance(payload, dict):
        return []
    # Cursor attempt markers are adapter bookkeeping.
    if payload.get("type") == "agentflow_adapter_attempt":
        attempt = payload.get("attempt")
        return [f"{label}  attempt {attempt}"] if attempt is not None else []

    event_type = payload.get("type")
    subtype = payload.get("subtype")

    # Skip stream noise: system init, rate limits, thinking tokens/deltas.
    if event_type in {"system", "rate_limit_event", "thinking"}:
        return []
    if event_type == "content_block_delta":
        return []
    if subtype in {"delta", "thinking_tokens", "init"}:
        return []

    if event_type == "assistant":
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return [f"{label}  {message.strip()}"]
        if isinstance(message, dict):
            return [
                f"{label}  {text}" for text in _assistant_text_blocks(message.get("content"))
            ]
        # Some stubs put content at the top level.
        return [f"{label}  {text}" for text in _assistant_text_blocks(payload.get("content"))]

    if event_type == "tool_call" and subtype == "started":
        tool = payload.get("tool_call") or payload.get("tool") or {}
        if isinstance(tool, dict):
            name = tool.get("name") or tool.get("toolName")
            if isinstance(name, str) and name:
                return [f"{label}  → {name}"]
        name = payload.get("name")
        if isinstance(name, str) and name:
            return [f"{label}  → {name}"]
        return []

    if event_type in {"tool_call", "user"}:
        # Tool results and user echoes are usually huge and not useful live.
        return []

    if event_type == "result":
        status = subtype if isinstance(subtype, str) else "done"
        return [f"{label}  finished ({status})"]

    return []


def _transcript_label(path: Path) -> str:
    name = path.name
    suffix = "-transcript.jsonl"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name


def _emit_formatted_lines(
    path: Path,
    offset: int,
    out: TextIO,
    *,
    formatter: Callable[[str], list[str]],
) -> int:
    lines, new_offset = _read_appended_lines(path, offset)
    for line in lines:
        for rendered in formatter(line):
            out.write(rendered + "\n")
    if lines:
        out.flush()
    return new_offset


def select_live_run(
    *,
    data_dir: Path,
    inp: TextIO,
    err: TextIO,
) -> str:
    """Prompt for a live Run and return its full ``run_id``.

    Lists non-terminal Runs with state, truncated summary, and short id.
    A single candidate is selected automatically. Selection is by 1-based
    index or unambiguous short-id prefix. Read-only: creates no evidence.
    """
    candidates = [
        run
        for run in list_runs(data_dir=data_dir)
        if run.state in LIVE_RUN_STATES
    ]
    if not candidates:
        raise RuntimeError("no live runs to watch")
    if len(candidates) == 1:
        chosen = candidates[0]
        err.write(f"watching {format_run_choice(chosen)}\n")
        err.flush()
        return chosen.run_id
    for index, run in enumerate(candidates, start=1):
        err.write(f"{index}. {format_run_choice(run)}\n")
    err.write(f"Select run [1-{len(candidates)}]: ")
    err.flush()
    selection = inp.readline()
    if selection == "":
        raise RuntimeError("no run selected")
    token = selection.strip()
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(candidates):
            return candidates[index - 1].run_id
        raise RuntimeError(
            f"selection out of range; expected 1-{len(candidates)}"
        )
    matches = [
        run
        for run in candidates
        if run.run_id.startswith(token) or short_run_id(run.run_id) == token
    ]
    if len(matches) == 1:
        return matches[0].run_id
    if not matches:
        raise RuntimeError(f"no live run matches {token!r}")
    raise RuntimeError(f"ambiguous selection {token!r}; use the list index")


def _format_event_line(line: str) -> list[str]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        text = line.strip()
        return [text] if text else []
    if not isinstance(event, dict):
        return []
    rendered = format_watch_event(event)
    return [rendered] if rendered is not None else []


def follow_run(
    *,
    run_id: str,
    data_dir: Path,
    out: TextIO | None = None,
    poll_interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Tail a Run's events and live role transcript until it needs a human.

    Prints human-readable lines for new Run events and role transcript activity
    (assistant text and tool calls; not raw stream-json), projecting Run State
    each poll, and returns after printing a final status line once the Run
    reaches a state requiring external action. Exits immediately when that is
    already true. This function is strictly read-only: it never creates or
    modifies evidence. Evidence files on disk remain raw JSONL.
    """
    if out is None:
        out = sys.stdout
    run_dir = data_dir / "runs" / run_id
    events_path = run_dir / "events.jsonl"
    events_offset = 0
    transcript_offsets: dict[Path, int] = {}
    seen_transcripts: set[Path] = set()
    while True:
        events_offset = _emit_formatted_lines(
            events_path,
            events_offset,
            out,
            formatter=_format_event_line,
        )
        for transcript_path in sorted(run_dir.glob("*-transcript.jsonl")):
            label = _transcript_label(transcript_path)
            if transcript_path not in seen_transcripts:
                seen_transcripts.add(transcript_path)
                out.write(f"--- {label} ---\n")
                out.flush()

            def _format(line: str, *, _label: str = label) -> list[str]:
                return format_watch_transcript_line(line, label=_label)

            transcript_offsets[transcript_path] = _emit_formatted_lines(
                transcript_path,
                transcript_offsets.get(transcript_path, 0),
                out,
                formatter=_format,
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
        try:
            status = read_run_status(run_id=run_dir.name, data_dir=data_dir)
        except Exception:
            # A single unreadable Run must never hide every other Run. Surface
            # it as an error entry rather than propagating. The atomic append is
            # what prevents such corruption in the first place; this is defense
            # in depth for pre-existing or externally damaged logs.
            status = RunStatus(
                run_id=run_dir.name,
                state="unreadable",
                summary=None,
                repository=None,
                base_sha=None,
                worktree=None,
                repository_profile_path=None,
                candidate_sha=None,
                approved_sha=None,
            )
        keyed.append((lines[0], status))
    keyed.sort(key=lambda pair: pair[0])
    return [
        status
        for _, status in keyed
        if state is None or status.state == state
    ]


def approve_run(*, run_id: str, approved_by: str, data_dir: Path) -> Approval:
    # Approval is claim-guarded like every other mutating command: acquire the
    # stage claim, then re-read state and re-verify the Workspace is still clean
    # at the current Candidate Revision under the claim before binding the
    # approval. This prevents a concurrent rebase from moving the candidate
    # between the check and the append, which would otherwise bind approval to a
    # stale SHA.
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        if status.state != "awaiting_human":
            raise ValueError(
                f"run {run_id} cannot be approved from state {status.state}"
            )
        if status.candidate_sha is None or status.worktree is None:
            raise ValueError(f"run {run_id} has no approvable candidate SHA")
        workspace = Path(status.worktree)
        head = _git("rev-parse", "HEAD", cwd=workspace)
        dirty = _git(
            "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if head != status.candidate_sha or dirty:
            raise ValueError(
                f"run {run_id} Workspace no longer matches the verified candidate"
            )
        append_event(
            data_dir=data_dir,
            holder=holder,
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
    finally:
        release_claim(data_dir=data_dir, run_id=run_id, holder=holder)


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
            holder=holder,
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
    """Record an explicit human rejection of a candidate.

    Conversation text is never rejection evidence: only this command appends
    ``human_rejected``, bound to the candidate SHA and terminal.
    """
    holder = default_claim_holder()
    acquire_claim(data_dir=data_dir, run_id=run_id, holder=holder)
    try:
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        fields: dict[str, str] = {"rejected_by": rejected_by}
        if reason is not None:
            fields["reason"] = reason
        if status.state == "awaiting_human":
            if status.candidate_sha is None:
                raise ValueError(f"run {run_id} has no rejectable candidate SHA")
            append_event(
                data_dir=data_dir,
                holder=holder,
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
    holder: str | None = None,
    **fields: object,
) -> None:
    events_path = data_dir / "runs" / run_id / "events.jsonl"
    # Compute the sequence number and write the record under the same advisory
    # lock the claim operations use, so the read-count-then-append window is
    # closed. Concurrent writers to one Run's log are serialized and sequence
    # numbers stay contiguous and equal to line position.
    with events_path.open("r+", encoding="utf-8") as events_file:
        fcntl.flock(events_file.fileno(), fcntl.LOCK_EX)
        lines = events_file.read().splitlines()
        if holder is not None:
            # A stage result may be appended only by the process that still owns
            # the active claim. If another process took over an expired claim,
            # the current owner differs and this stale holder is refused, so it
            # cannot overwrite the new holder's work.
            active = _active_claim([json.loads(line) for line in lines])
            if active is None or active.get("holder") != holder:
                current = None if active is None else active.get("holder")
                raise ValueError(
                    f"run {run_id}: {holder} no longer holds the stage claim "
                    f"(current holder: {current})"
                )
        sequence = len(lines) + 1
        event = {**fields, "sequence": sequence, "type": event_type}
        events_file.seek(0, os.SEEK_END)
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
            holder=holder,
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
