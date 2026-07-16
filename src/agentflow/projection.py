"""Read-only observability projection over Run Evidence and the Work Graph.

The projection is rebuildable from events at any time and is never consulted as
workflow authority. Workflow ``advance`` / ``approve`` / ``start`` derive Run
State only from event replay in the run kernel; this module is a separate read
path for operators and later read-only surfaces. It never writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .run_kernel import short_run_id
from .work_graph import compute_ready_work, load_work_graph

# Mirrors read_run_status's projection table so the observability view stays
# consistent with status without becoming an authority the workflow consults.
_STATE_BY_EVENT = {
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


def _real_path(path: Path) -> Path | None:
    """Fully-resolved real path of ``path``, or None when resolution fails.

    ``os.path.realpath`` does not raise on a symlink loop — it returns the loop
    path left in place, which still resolves within its own directory — so a
    circular/self-referential symlink survives this check and is caught only
    when the path is opened. Every read here catches ``OSError`` so that loop is
    skipped rather than raised. Any other resolution error yields None.
    """
    try:
        return Path(os.path.realpath(path))
    except OSError:
        return None


def _within(base_real: Path, real: Path) -> bool:
    """True when ``real`` is ``base_real`` itself or nested beneath it."""
    return real == base_real or base_real in real.parents


def _is_confined_component(name: str) -> bool:
    """True when ``name`` is a single, non-traversing path component.

    Rejects ``.``/``..`` and anything containing a path separator or NUL, so a
    run id can only ever name a direct child of the runs directory.
    """
    if name in ("", ".", ".."):
        return False
    if "\x00" in name:
        return False
    return name == Path(name).name


def confined_run_dir(runs_dir: Path, run_id: str) -> Path | None:
    """Run directory for ``run_id`` if it is confined to ``runs_dir``.

    Rejects run ids that are not a single path component (``.``, ``..``, or any
    id with a path separator), and omits a symlinked run directory whose real
    path escapes ``runs_dir``. Returns None on any confinement failure; never
    raises.
    """
    if not _is_confined_component(run_id):
        return None
    runs_real = _real_path(runs_dir)
    if runs_real is None:
        return None
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        return None
    real = _real_path(run_dir)
    if real is None or not _within(runs_real, real):
        return None
    return run_dir


def confined_file(run_dir: Path, filename: str) -> Path | None:
    """Path to ``filename`` inside ``run_dir`` if confined there, else None.

    Refuses an evidence or transcript symlink whose real path escapes
    ``run_dir``. A circular symlink passes this containment check — its loop
    resolves within the directory — but fails when opened; callers read through
    helpers that treat that ``OSError`` as a skip, so a hostile file never
    aborts reads of its siblings. Never raises.
    """
    run_real = _real_path(run_dir)
    if run_real is None:
        return None
    path = run_dir / filename
    real = _real_path(path)
    if real is None or not _within(run_real, real):
        return None
    return path


def read_events_tolerant(events_path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Load event dicts from ``events.jsonl``, isolating corruption.

    Invalid JSON lines and undecodable bytes stop further reading of that file
    but do not raise. Valid event lines before the damage are returned.
    Returns ``(events, truncated)`` where ``truncated`` is True when reading
    stopped early because of damage.
    """
    if not events_path.is_file():
        return [], False
    try:
        raw = events_path.read_bytes()
    except OSError:
        # An unreadable evidence file (for example a circular symlink that
        # slipped past is_file) is a confinement/damage signal, not a crash:
        # report it as truncated with no recovered events.
        return [], True
    events: list[dict[str, Any]] = []
    # Split on newlines in bytes so a bad UTF-8 segment cannot poison earlier
    # lines. A trailing incomplete line without a newline is ignored, matching
    # how a concurrently growing log is read.
    if not raw:
        return [], False
    parts = raw.split(b"\n")
    # Drop the final empty segment produced by a trailing newline, or keep an
    # incomplete last line for attempted decode (it may be incomplete JSON).
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    for part in parts:
        if not part.strip():
            continue
        try:
            line = part.decode("utf-8")
        except UnicodeDecodeError:
            return events, True
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return events, True
        if not isinstance(event, dict):
            return events, True
        events.append(event)
    return events, False


def _project_fields_from_events(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    state = "unknown"
    worktree: str | None = None
    repository_profile_path: str | None = None
    candidate_sha: str | None = None
    approved_sha: str | None = None
    rebased_base_sha: str | None = None
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        state = _STATE_BY_EVENT.get(event_type, state)
        if event_type == "workspace_ready":
            worktree = event.get("worktree")
        if event_type == "repository_profile_captured":
            repository_profile_path = event.get("path")
        if event.get("candidate_sha") is not None:
            candidate_sha = event["candidate_sha"]
        if event_type == "candidate_rebased":
            candidate_sha = event.get("new_candidate_sha")
            rebased_base_sha = event.get("new_base_sha")
        if event_type == "human_approved":
            approved_sha = event.get("approved_sha")
    return {
        "state": state,
        "worktree": worktree,
        "repository_profile_path": repository_profile_path,
        "candidate_sha": candidate_sha,
        "approved_sha": approved_sha,
        "rebased_base_sha": rebased_base_sha,
    }


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _project_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = run_dir.name
    # Every evidence file is confined to the run directory: an escaping symlink
    # is refused (read as absent) and a circular symlink is skipped when opened,
    # so a hostile file never aborts this Run or its siblings.
    events_path = confined_file(run_dir, "events.jsonl")
    events, truncated = (
        read_events_tolerant(events_path)
        if events_path is not None
        else ([], False)
    )
    fields = _project_fields_from_events(events)
    task = _read_json_object(confined_file(run_dir, "task.json"))
    repository = _read_json_object(confined_file(run_dir, "repository.json"))
    source = task.get("source")
    run_entry: dict[str, Any] = {
        "run_id": run_id,
        "short_id": short_run_id(run_id),
        "state": fields["state"],
        "summary": task.get("summary"),
        "repository": repository.get("repository"),
        "base_sha": fields["rebased_base_sha"]
        if fields["rebased_base_sha"] is not None
        else repository.get("base_sha"),
        "evidence_truncated": truncated,
    }
    if fields["candidate_sha"] is not None:
        run_entry["candidate_sha"] = fields["candidate_sha"]
    if fields["approved_sha"] is not None:
        run_entry["approved_sha"] = fields["approved_sha"]
    if fields["worktree"] is not None:
        run_entry["worktree"] = fields["worktree"]
    if isinstance(source, dict) and source.get("work_item_id"):
        run_entry["work_item_id"] = source["work_item_id"]
    evidence_entry: dict[str, Any] = {
        "run_id": run_id,
        "events": events,
        "truncated": truncated,
    }
    return run_entry, evidence_entry


def build_projection(
    *,
    data_dir: Path,
    repository: Path,
) -> dict[str, Any]:
    """Rebuild the observability projection from Run Evidence and the Work Graph.

    Returns a JSON-serializable dict with ``runs``, ``work``, and ``evidence``.
    Never writes files. Safe to call repeatedly; each call re-reads sources.
    """
    runs_dir = data_dir / "runs"
    run_entries: list[dict[str, Any]] = []
    evidence_entries: list[dict[str, Any]] = []
    keyed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir(), key=lambda path: path.name):
            # Confine the run directory: reject traversing names and omit a
            # symlinked run dir whose real path escapes runs/.
            run_dir = confined_run_dir(runs_dir, entry.name)
            if run_dir is None:
                continue
            # Confine the evidence file to the run dir; an escaping or circular
            # events symlink is refused so the Run is skipped, never raised.
            events_path = confined_file(run_dir, "events.jsonl")
            if events_path is None or not events_path.is_file():
                continue
            # Sort key: first UTF-8-decodable line when present, else run id.
            sort_key = run_dir.name
            try:
                first_line = events_path.read_bytes().split(b"\n", 1)[0]
                sort_key = first_line.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                pass
            run_entry, evidence_entry = _project_run(run_dir)
            keyed.append((sort_key, run_entry, evidence_entry))
    keyed.sort(key=lambda item: item[0])
    for _, run_entry, evidence_entry in keyed:
        run_entries.append(run_entry)
        evidence_entries.append(evidence_entry)

    # Completion is derived from projected Run Evidence (not list_runs) so a
    # corrupt sibling Run cannot abort work projection.
    completed = {
        entry["work_item_id"]
        for entry in run_entries
        if entry.get("state") == "human_approved" and entry.get("work_item_id")
    }
    graph = load_work_graph(repository)
    ready = compute_ready_work(graph, completed)
    work = {
        "items": graph,
        "ready": ready,
        "completed_ids": sorted(completed),
    }
    return {
        "runs": run_entries,
        "work": work,
        "evidence": evidence_entries,
    }
