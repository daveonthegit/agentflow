"""External-worker validation: validate a caller-owned candidate revision.

This is Agentflow's narrow integration point for an outside live-worker
orchestrator (for example Firstmate). The outside supervisor owns the worker
session, the worktree, supervision, recovery, human approval, and delivery.
Agentflow owns only what it is authoritative for: verifying the Repository
Profile and candidate identity, running the configured authoritative checks,
and persisting replayable evidence and a clear validated-or-failed outcome.

The boundary is enforced structurally, not by instruction:

- This module never creates or deletes a worktree, launches an agent, merges,
  pushes, opens a PR, or records approval. It imports no Agent Adapter and no
  merge/deploy code, so those code paths are unreachable from here.
- An External Validation lives in its own event log under
  ``<Agentflow Home>/external/<external-id>/``, never under ``runs/``, and has
  its own event vocabulary and state projection. Its states are
  ``registered`` → ``validated`` | ``validation_failed``; ``awaiting_human`` is
  not in the projection and can never be reached.
- The caller's repository and worktree are referenced by path and the
  candidate by exact SHA. Agentflow stores references and integrity metadata,
  never a copy of the caller's state.

Evidence is append-only and replayable: the event log carries one-based
sequence numbers under the same locked append the run kernel uses, and each
check execution is written to a write-once ``checks-<n>.json`` artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable
import uuid

from .contracts import ContractError, validate_task_spec
from .repository_profile import inspect_repository_profile
from .run_kernel import (
    acquire_claim,
    append_event,
    default_claim_holder,
    release_claim,
)
from .workflow import run_authoritative_checks

# The External Validation event vocabulary and its state projection. This map
# is deliberately separate from the run kernel's ``STATE_BY_EVENT`` so the
# external path can never reach a Run state such as ``awaiting_human``.
# ``external_candidate_identified``, ``external_profile_captured``,
# ``external_validation_refused``, and the shared claim events have no entry, so
# the ``.get(type, state)`` fallback leaves the replayed state unchanged.
EXTERNAL_STATE_BY_EVENT = {
    "external_registered": "registered",
    "external_checks_passed": "validated",
    "external_checks_failed": "validation_failed",
}

# Only from ``registered`` may a validation run. ``validated`` and
# ``validation_failed`` are terminal for a given candidate: a caller that wants
# to re-check a new candidate registers a new External Validation.
VALIDATABLE_STATES = frozenset({"registered"})

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RegisteredValidation:
    external_id: str
    state: str
    repository: str
    worktree: str
    candidate_sha: str
    repository_profile_path: str
    external_ref: str | None = None


@dataclass(frozen=True)
class ExternalValidationResult:
    external_id: str
    state: str
    candidate_sha: str
    passed: bool
    artifact: Path
    validated_by: str | None = None


@dataclass(frozen=True)
class ExternalValidationStatus:
    external_id: str
    state: str
    summary: str | None
    repository: str | None
    worktree: str | None
    candidate_sha: str | None
    repository_profile_path: str | None
    external_ref: str | None = None
    acceptance_criteria: list[str] | None = None
    validated_by: str | None = None
    checks_artifact: str | None = None
    source: dict[str, str] | None = None


class ExternalValidationError(ValueError):
    """A recognized, actionable External Validation failure.

    Raised for every rejection the contract promises to handle safely — missing
    or stale profiles, dirty worktrees, mismatched SHAs, invalid paths, and
    unsupported state transitions — so the CLI reports a clean message rather
    than a traceback.
    """


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _git_ok(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _resolve_git_dir(path: Path) -> str:
    """Return the shared git dir for ``path`` or raise for a non-repository."""
    result = _git_ok("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
    if result.returncode != 0:
        # Older git without --path-format: fall back to a plain query.
        result = _git_ok("rev-parse", "--git-common-dir", cwd=path)
        if result.returncode != 0:
            raise ExternalValidationError(
                f"invalid path: {path} is not inside a git repository"
            )
        return str((path / result.stdout.strip()).resolve())
    return str(Path(result.stdout.strip()).resolve())


def _require_worktree(worktree: Path) -> Path:
    if not worktree.exists():
        raise ExternalValidationError(f"invalid worktree path: {worktree} does not exist")
    result = _git_ok("rev-parse", "--show-toplevel", cwd=worktree)
    if result.returncode != 0:
        raise ExternalValidationError(
            f"invalid worktree path: {worktree} is not a git worktree"
        )
    return Path(result.stdout.strip())


def _require_clean(worktree: Path) -> None:
    dirty = _git("status", "--porcelain", "--untracked-files=all", cwd=worktree)
    if dirty:
        raise ExternalValidationError(
            f"dirty worktree: {worktree} has uncommitted changes; the candidate "
            "must be validated against a clean worktree"
        )


def _resolve_candidate(worktree: Path, candidate_sha: str) -> str:
    """Resolve the caller-supplied candidate to a full SHA and require HEAD match.

    Accepts a full or abbreviated SHA. The resolved commit must equal the
    worktree HEAD, so the validation is bound to exactly the revision the caller
    presented and nothing else can have moved underneath it.
    """
    if not isinstance(candidate_sha, str) or not candidate_sha.strip():
        raise ExternalValidationError("candidate SHA must be a non-empty string")
    resolved = _git_ok(
        "rev-parse", "--verify", "--quiet", f"{candidate_sha.strip()}^{{commit}}",
        cwd=worktree,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise ExternalValidationError(
            f"mismatched SHA: {candidate_sha!r} is not a commit in {worktree}"
        )
    full = resolved.stdout.strip()
    head = _git("rev-parse", "HEAD", cwd=worktree)
    if full != head:
        raise ExternalValidationError(
            f"mismatched SHA: worktree HEAD is {head} but the candidate is "
            f"{full}; check out the exact candidate before validating"
        )
    return full


def _inspect_fresh_profile(worktree: Path):
    profile = inspect_repository_profile(worktree)
    if profile is None:
        raise ExternalValidationError(
            "missing Repository Profile: the candidate worktree commits no "
            ".agentflow/repository-profile.json; profile the repository and "
            "commit it before validating"
        )
    if not profile.fresh:
        raise ExternalValidationError(
            "stale Repository Profile: the committed profile's source "
            "fingerprint does not match the candidate worktree; regenerate and "
            "commit the profile before validating"
        )
    return profile


def _read_external_events(external_dir: Path) -> list[dict]:
    events_path = external_dir / "events.jsonl"
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]


def register_external_validation(
    *,
    summary: str,
    worktree: Path,
    candidate_sha: str,
    data_dir: Path,
    repository: Path | None = None,
    acceptance_criteria: list[str] | None = None,
    external_ref: str | None = None,
    source: dict[str, str] | None = None,
) -> RegisteredValidation:
    """Register an externally managed candidate and verify it is validatable.

    Verifies, without ever mutating the caller's checkout: the worktree is a
    clean git worktree, its HEAD is exactly ``candidate_sha``, and it commits a
    fresh Repository Profile. When ``repository`` is given it must share the
    worktree's git store, so the recorded repository reference is trustworthy.
    Records the immutable registration, the verified candidate identity, and the
    profile evidence, and returns the new External Validation in ``registered``.
    """
    task_input: dict[str, Any] = {
        "summary": summary,
        "acceptance_criteria": (
            [] if acceptance_criteria is None else acceptance_criteria
        ),
    }
    if source is not None:
        task_input["source"] = source
    try:
        task = validate_task_spec(task_input)
    except ContractError as error:
        raise ExternalValidationError(str(error)) from error
    if external_ref is not None and (
        not isinstance(external_ref, str) or not external_ref.strip()
    ):
        raise ExternalValidationError("external ref must be a non-empty string")

    worktree = worktree.expanduser()
    worktree_root = _require_worktree(worktree)
    worktree_git_dir = _resolve_git_dir(worktree_root)

    if repository is not None:
        repository = repository.expanduser()
        if not repository.exists():
            raise ExternalValidationError(
                f"invalid repository path: {repository} does not exist"
            )
        repository_result = _git_ok("rev-parse", "--show-toplevel", cwd=repository)
        if repository_result.returncode != 0:
            raise ExternalValidationError(
                f"invalid repository path: {repository} is not a git repository"
            )
        repository_root = Path(repository_result.stdout.strip())
        if _resolve_git_dir(repository_root) != worktree_git_dir:
            raise ExternalValidationError(
                f"worktree {worktree_root} does not belong to repository "
                f"{repository_root}; they use different git stores"
            )
    else:
        repository_root = worktree_root

    _require_clean(worktree_root)
    resolved_sha = _resolve_candidate(worktree_root, candidate_sha)
    profile = _inspect_fresh_profile(worktree_root)

    external_id = uuid.uuid4().hex
    external_dir = data_dir / "external" / external_id
    external_dir.mkdir(parents=True)

    registration: dict[str, Any] = {
        "acceptance_criteria": task["acceptance_criteria"],
        "candidate_sha": resolved_sha,
        "repository": str(repository_root),
        "summary": task["summary"],
        "worktree": str(worktree_root),
    }
    if external_ref is not None:
        registration["external_ref"] = external_ref.strip()
    if "source" in task:
        registration["source"] = task["source"]
    _write_json(external_dir / "registration.json", registration)

    registered_event: dict[str, Any] = {
        "acceptance_criteria": task["acceptance_criteria"],
        "candidate_sha": resolved_sha,
        "repository": str(repository_root),
        "sequence": 1,
        "summary": task["summary"],
        "type": "external_registered",
        "worktree": str(worktree_root),
    }
    if external_ref is not None:
        registered_event["external_ref"] = external_ref.strip()
    if "source" in task:
        registered_event["source"] = task["source"]
    events = [
        registered_event,
        {
            "candidate_sha": resolved_sha,
            "sequence": 2,
            "type": "external_candidate_identified",
            "worktree": str(worktree_root),
        },
        {
            "fresh": profile.fresh,
            "path": profile.path,
            "profile_sha256": profile.profile_sha256,
            "sequence": 3,
            "source_fingerprint": profile.source_fingerprint,
            "type": "external_profile_captured",
        },
    ]
    (external_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return RegisteredValidation(
        external_id=external_id,
        state="registered",
        repository=str(repository_root),
        worktree=str(worktree_root),
        candidate_sha=resolved_sha,
        repository_profile_path=profile.path,
        external_ref=external_ref.strip() if external_ref is not None else None,
    )


def validate_external_task(
    *,
    external_id: str,
    data_dir: Path,
    validated_by: str | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    environment_fingerprint: Callable[[], dict[str, str]] | None = None,
) -> ExternalValidationResult:
    """Run the authoritative checks for a registered External Validation.

    Claim-guarded with the same mechanism the run kernel uses, so two callers
    cannot double-run one validation. Re-verifies the worktree is clean at the
    exact recorded candidate SHA and that the committed profile is fresh and
    unchanged, then runs the profile's checks in place — it never creates a
    worktree. On success appends ``external_checks_passed`` (``validated``); on
    failure ``external_checks_failed`` (``validation_failed``). Every recognized
    refusal is recorded as an ``external_validation_refused`` event that leaves
    state unchanged, so the caller can fix the worktree and re-validate.
    """
    external_dir = data_dir / "external" / external_id
    if not (external_dir / "events.jsonl").exists():
        raise ExternalValidationError(f"no External Validation {external_id!r}")
    events_path = external_dir / "events.jsonl"

    holder = default_claim_holder()
    acquire_claim(
        data_dir=data_dir,
        run_id=external_id,
        holder=holder,
        events_path=events_path,
    )
    try:
        status = read_external_status(external_id=external_id, data_dir=data_dir)

        def _refuse(reason: str) -> ExternalValidationError:
            append_event(
                data_dir=data_dir,
                run_id=external_id,
                event_type="external_validation_refused",
                holder=holder,
                events_path=events_path,
                reason=reason,
            )
            return ExternalValidationError(
                f"External Validation {external_id} refused: {reason}"
            )

        if status.state not in VALIDATABLE_STATES:
            raise _refuse(
                f"cannot validate from state {status.state}; only a registered "
                "validation may run"
            )
        if (
            status.worktree is None
            or status.candidate_sha is None
            or status.repository_profile_path is None
        ):
            raise _refuse("registration is missing the worktree or candidate")

        worktree = Path(status.worktree)
        if not worktree.exists():
            raise _refuse(f"worktree {worktree} no longer exists")
        if _git_ok("rev-parse", "--show-toplevel", cwd=worktree).returncode != 0:
            raise _refuse(f"worktree {worktree} is no longer a git worktree")
        head = _git("rev-parse", "HEAD", cwd=worktree)
        if head != status.candidate_sha:
            raise _refuse(
                f"mismatched SHA: worktree HEAD is {head} but the registered "
                f"candidate is {status.candidate_sha}"
            )
        if _git("status", "--porcelain", "--untracked-files=all", cwd=worktree):
            raise _refuse(
                f"dirty worktree: {worktree} has uncommitted changes at "
                "validation time"
            )

        profile = inspect_repository_profile(worktree)
        if profile is None:
            raise _refuse("Repository Profile is missing from the candidate worktree")
        if not profile.fresh:
            raise _refuse("Repository Profile is stale for the candidate worktree")
        captured = next(
            event
            for event in reversed(_read_external_events(external_dir))
            if event["type"] == "external_profile_captured"
        )
        if profile.profile_sha256 != captured["profile_sha256"]:
            raise _refuse(
                "Repository Profile changed since registration; register a new "
                "validation for the current candidate"
            )

        profile_body = json.loads(
            (worktree / profile.path).read_text(encoding="utf-8")
        )
        commands = profile_body.get("checks")
        if not isinstance(commands, list) or not commands:
            raise _refuse("Repository Profile declares no authoritative checks")

        checks, all_passed = run_authoritative_checks(
            commands=commands,
            workspace=worktree,
            attempt=1,
            clock=clock,
            monotonic=monotonic,
            environment_fingerprint=environment_fingerprint,
        )
        workspace_clean = not _git(
            "status", "--porcelain", "--untracked-files=all", cwd=worktree
        )
        if not workspace_clean:
            all_passed = False

        artifact = external_dir / "checks-1.json"
        _write_json(
            artifact,
            {
                "candidate_sha": status.candidate_sha,
                "checks": checks,
                "workspace_clean": workspace_clean,
            },
        )
        event_type = (
            "external_checks_passed" if all_passed else "external_checks_failed"
        )
        fields: dict[str, Any] = {
            "artifact": str(artifact),
            "candidate_sha": status.candidate_sha,
        }
        if validated_by is not None:
            fields["validated_by"] = validated_by
        append_event(
            data_dir=data_dir,
            run_id=external_id,
            event_type=event_type,
            holder=holder,
            events_path=events_path,
            **fields,
        )
        return ExternalValidationResult(
            external_id=external_id,
            state="validated" if all_passed else "validation_failed",
            candidate_sha=status.candidate_sha,
            passed=all_passed,
            artifact=artifact,
            validated_by=validated_by,
        )
    finally:
        release_claim(
            data_dir=data_dir,
            run_id=external_id,
            holder=holder,
            events_path=events_path,
        )


def read_external_status(
    *, external_id: str, data_dir: Path
) -> ExternalValidationStatus:
    external_dir = data_dir / "external" / external_id
    events_path = external_dir / "events.jsonl"
    if not events_path.exists():
        raise ExternalValidationError(f"no External Validation {external_id!r}")

    state = "unknown"
    worktree: str | None = None
    repository: str | None = None
    candidate_sha: str | None = None
    repository_profile_path: str | None = None
    external_ref: str | None = None
    summary: str | None = None
    acceptance_criteria: list[str] | None = None
    validated_by: str | None = None
    checks_artifact: str | None = None
    source: dict[str, str] | None = None

    for line_number, line in enumerate(
        events_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        event = json.loads(line)
        sequence = event.get("sequence")
        if sequence is not None and sequence != line_number:
            raise ExternalValidationError(
                f"invalid event sequence for External Validation {external_id}: "
                f"expected {line_number}, got {sequence}"
            )
        state = EXTERNAL_STATE_BY_EVENT.get(event["type"], state)
        if event["type"] == "external_registered":
            summary = event.get("summary")
            repository = event.get("repository")
            worktree = event.get("worktree")
            candidate_sha = event.get("candidate_sha")
            external_ref = event.get("external_ref")
            criteria = event.get("acceptance_criteria")
            acceptance_criteria = criteria if criteria else None
            event_source = event.get("source")
            source = event_source if isinstance(event_source, dict) else None
        if event["type"] == "external_profile_captured":
            repository_profile_path = event.get("path")
        if event["type"] in ("external_checks_passed", "external_checks_failed"):
            checks_artifact = event.get("artifact")
            validated_by = event.get("validated_by")

    return ExternalValidationStatus(
        external_id=external_id,
        state=state,
        summary=summary,
        repository=repository,
        worktree=worktree,
        candidate_sha=candidate_sha,
        repository_profile_path=repository_profile_path,
        external_ref=external_ref,
        acceptance_criteria=acceptance_criteria,
        validated_by=validated_by,
        checks_artifact=checks_artifact,
        source=source,
    )


def list_external_validations(*, data_dir: Path) -> list[ExternalValidationStatus]:
    external_root = data_dir / "external"
    if not external_root.is_dir():
        return []
    keyed: list[tuple[str, ExternalValidationStatus]] = []
    for external_dir in external_root.iterdir():
        events_path = external_dir / "events.jsonl"
        if not events_path.is_file():
            continue
        lines = events_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        try:
            status = read_external_status(
                external_id=external_dir.name, data_dir=data_dir
            )
        except Exception:
            # One damaged log must never hide every other validation.
            status = ExternalValidationStatus(
                external_id=external_dir.name,
                state="unreadable",
                summary=None,
                repository=None,
                worktree=None,
                candidate_sha=None,
                repository_profile_path=None,
            )
        keyed.append((lines[0], status))
    keyed.sort(key=lambda pair: pair[0])
    return [status for _, status in keyed]
