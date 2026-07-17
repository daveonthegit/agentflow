"""Target-repository Work Graph: git-tracked Work Items and computed ready work.

The Work Graph is owned by the Target Repository and stored as git-tracked JSONL
under ``.agentflow/work/``. Work-intent truth lives here; execution truth lives
in Run Evidence. The two keep only references to each other — a Run records the
``work_item_id`` it captured, and completion is derived from Run Evidence rather
than stored back into the graph. Ready work is computed from dependency
relationships whenever it is needed, never persisted as a mutable value.

Persistence goes through a replaceable ``WorkGraphBackend``. The default is the
native JSONL store; an in-memory backend exists for tests and adapters. Backend
swaps change only storage — validation and ready-work semantics stay in this
module.

Work Graph approvals are recorded in two places. Agentflow Home holds the
authoritative evidence log that the capture gate reads
(``require_approved_work_graph``): it governs whether a Run may capture a Work
Item, and its behavior is unchanged by anything here. Every approval is *also*
mirrored into a git-tracked ``.agentflow/approvals.jsonl`` in the Target
Repository so approval currency is verifiable from the repository alone — on a
CI runner or a teammate's machine that has no Agentflow Home state
(``verify_repo_work_graph_approval``). The repo mirror deliberately lives
*outside* ``.agentflow/work/``: everything under that directory is loaded as
Work Graph content, so an approval record there would be folded into the graph
content hash and invalidate the very approval it records. The two logs share the
same append-only shape and advisory-lock discipline but carry independent
sequence numbers. When the two disagree (for example, a repo mirror pulled from
git without the matching home evidence), home evidence governs the gate; the
repo-only check is an additional, portable source of truth, never a weaker
substitute for the gate.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Protocol

from .contracts import (
    ContractError,
    WORK_ITEM_STATUS_PROPOSED,
    validate_discoveries,
    validate_work_graph,
)
from .run_kernel import COMPLETED_RUN_STATES, list_runs

WORK_RELATIVE_DIR = Path(".agentflow/work")
DEFAULT_GRAPH_FILENAME = "graph.jsonl"


class WorkGraphBackend(Protocol):
    """Replaceable persistence for Work Graph items.

    Backends own storage only. Callers validate with ``validate_work_graph``
    before write and after read so swapping implementations cannot change
    Work Graph semantics.
    """

    def read_items(self) -> list[dict]:
        """Return stored Work Item dicts, without validating the graph."""
        ...

    def write_items(self, items: list[dict]) -> None:
        """Fully replace the stored Work Item set with ``items``."""
        ...


class InMemoryWorkGraphBackend:
    """Full-replace in-memory Work Graph store.

    Deep-copies on read and write so nested fields such as ``depends_on`` and
    ``acceptance_criteria`` stay isolated from callers, matching JSONL round-trip
    isolation via serialization.
    """

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items: list[dict] = [copy.deepcopy(item) for item in (items or [])]

    def read_items(self) -> list[dict]:
        return [copy.deepcopy(item) for item in self._items]

    def write_items(self, items: list[dict]) -> None:
        self._items = [copy.deepcopy(item) for item in items]


class JsonlWorkGraphBackend:
    """Native JSONL Work Graph store under ``.agentflow/work/``.

    Reads every ``*.jsonl`` file in deterministic order. ``write_items`` deletes
    all existing ``*.jsonl`` files and writes the replacement set to
    ``graph.jsonl``, matching the in-memory backend's full-replace semantics.
    """

    def __init__(self, repository: Path) -> None:
        self._repository = repository
        self._work_dir = repository / WORK_RELATIVE_DIR

    def read_items(self) -> list[dict]:
        if not self._work_dir.is_dir():
            return []
        items: list[dict] = []
        for path in sorted(self._work_dir.glob("*.jsonl")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ContractError(
                        f"{path.name}:{line_number} is not valid JSON"
                    ) from error
        return items

    def write_items(self, items: list[dict]) -> None:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        for path in self._work_dir.glob("*.jsonl"):
            path.unlink()
        if not items:
            return
        target = self._work_dir / DEFAULT_GRAPH_FILENAME
        target.write_text(
            "".join(
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                for item in items
            ),
            encoding="utf-8",
        )


def default_work_graph_backend(repository: Path) -> JsonlWorkGraphBackend:
    """Default Work Graph persistence for a Target Repository."""
    return JsonlWorkGraphBackend(repository)


def work_item_content_hash(item: dict) -> str:
    """Stable content hash of a Work Item for Run capture-by-reference.

    A Run records this alongside the ``work_item_id`` so later edits to the Work
    Item are detectable and never silently alter an in-flight Run.
    """
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def work_graph_content_hash(items: list[dict]) -> str:
    """Stable content hash of the entire validated Work Graph.

    A Work Graph approval binds to this hash, so any later edit to any Work
    Item — including reordering — is detectable and invalidates the approval,
    mirroring how an Approved Revision is invalidated by any code change.
    """
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _work_graph_approvals_path(data_dir: Path) -> Path:
    """Authoritative evidence log of Work Graph approvals in Agentflow Home."""
    return data_dir / "work" / "graph-approvals.jsonl"


def _repo_work_graph_approvals_path(repository: Path) -> Path:
    """Git-tracked mirror of Work Graph approvals in the Target Repository.

    Deliberately outside ``.agentflow/work/`` so it is never parsed as Work
    Graph content or folded into the graph content hash.
    """
    return Path(repository) / ".agentflow" / "approvals.jsonl"


def _read_approvals(path: Path) -> list[dict]:
    """Return append-ordered approval records from an approvals log. Read-only."""
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_work_graph_approvals(data_dir: Path) -> list[dict]:
    """Return authoritative home-dir Work Graph approvals in append order."""
    return _read_approvals(_work_graph_approvals_path(data_dir))


def read_repo_work_graph_approvals(repository: Path) -> list[dict]:
    """Return the repo-tracked Work Graph approval mirror in append order."""
    return _read_approvals(_repo_work_graph_approvals_path(repository))


def _append_approval_record(
    path: Path,
    *,
    approved_by: str,
    graph_hash: str,
    repository: str,
    now: datetime,
) -> dict:
    """Append one approval record under advisory-lock append-sequence discipline.

    Mirrors the Run event log convention: the sequence number is the file's
    line count plus one, computed while holding an exclusive lock so concurrent
    approvals to the same log serialize and sequences stay contiguous. Each log
    (home and repo mirror) carries its own independent sequence.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+", encoding="utf-8") as approvals_file:
        fcntl.flock(approvals_file.fileno(), fcntl.LOCK_EX)
        lines = approvals_file.read().splitlines()
        record = {
            "approved_at": now.isoformat(),
            "approved_by": approved_by,
            "graph_hash": graph_hash,
            "repository": repository,
            "sequence": len(lines) + 1,
            "type": "work_graph_approved",
        }
        approvals_file.seek(0, os.SEEK_END)
        approvals_file.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def approve_work_graph(
    *,
    repository: Path,
    data_dir: Path,
    approved_by: str,
    backend: WorkGraphBackend | None = None,
    now: datetime | None = None,
) -> dict:
    """Record an attributed, content-hashed human approval of the Work Graph.

    This is a distinct evidence record (``work_graph_approved``) from candidate
    approval (``human_approved``): it approves work intent, not a revision. The
    record is appended under the same advisory-lock append-sequence discipline
    as Run event logs, so concurrent approvals serialize and sequence numbers
    stay contiguous and equal to line position.

    The approval is recorded twice: the authoritative evidence log in Agentflow
    Home (returned here and read by the capture gate) and a git-tracked mirror
    in the Target Repository (``.agentflow/approvals.jsonl``) so approval
    currency is verifiable from the repository alone. Both records bind to the
    same graph content hash and attribution; they keep independent sequence
    numbers because they are separate append-only logs.
    """
    graph = load_work_graph(repository, backend=backend)
    if not graph:
        raise ValueError("cannot approve an empty Work Graph")
    if now is None:
        now = datetime.now(timezone.utc)
    graph_hash = work_graph_content_hash(graph)
    resolved_repository = str(Path(repository).resolve())
    record = _append_approval_record(
        _work_graph_approvals_path(data_dir),
        approved_by=approved_by,
        graph_hash=graph_hash,
        repository=resolved_repository,
        now=now,
    )
    _append_approval_record(
        _repo_work_graph_approvals_path(repository),
        approved_by=approved_by,
        graph_hash=graph_hash,
        repository=resolved_repository,
        now=now,
    )
    return record


def require_approved_work_graph(
    *,
    repository: Path,
    data_dir: Path,
    backend: WorkGraphBackend | None = None,
) -> str:
    """Capture gate: the current Work Graph must match its latest approval.

    Returns the current graph hash when the most recent approval binds to it.
    Raises when the graph was never approved or changed after approval — like
    an Approved Revision, any subsequent change invalidates the approval — so
    Run capture is refused until a human re-approves.
    """
    graph = load_work_graph(repository, backend=backend)
    graph_hash = work_graph_content_hash(graph)
    approvals = read_work_graph_approvals(data_dir)
    if not approvals:
        raise ValueError(
            "Work Graph is not approved; approve it with "
            "`agentflow work approve --approved-by <name>` before a Run "
            "captures a Work Item"
        )
    approved_hash = approvals[-1]["graph_hash"]
    if approved_hash != graph_hash:
        raise ValueError(
            "Work Graph changed after its approval "
            f"(approved {approved_hash[:12]}, current {graph_hash[:12]}); "
            "re-approve it with `agentflow work approve` before a Run "
            "captures a Work Item"
        )
    return graph_hash


@dataclass(frozen=True)
class RepoApprovalStatus:
    """Result of a repo-only Work Graph approval check.

    ``graph_hash`` is the current content hash of the repository's Work Graph.
    ``approved_graph_hash`` is the hash bound by the latest repo-tracked
    approval, or ``None`` when the repository has no approval mirror.
    ``is_current`` is true only when the latest mirrored approval binds the
    current graph. ``approval`` is that latest record, or ``None``.
    """

    is_current: bool
    graph_hash: str
    approved_graph_hash: str | None
    approval: dict | None


def verify_repo_work_graph_approval(
    repository: Path,
    *,
    backend: WorkGraphBackend | None = None,
) -> RepoApprovalStatus:
    """Answer "is the current graph approved?" from the repository alone.

    Reads only the Target Repository: the Work Graph under ``.agentflow/work/``
    and the git-tracked approval mirror ``.agentflow/approvals.jsonl``. It needs
    no Agentflow Home state, so it runs identically in CI, on a teammate's
    machine, or anywhere the repository is checked out. The latest mirrored
    approval binds, mirroring the home-dir latest-approval semantics of
    ``require_approved_work_graph``.

    This is a portable, additional source of truth for approval currency; it is
    not the capture gate and never weakens it. When the repo mirror and home
    evidence disagree, home evidence governs Run capture and this check is only
    for machines that have no home state. It raises ``ContractError`` if the
    Work Graph itself is invalid, exactly as ``load_work_graph`` does.
    """
    graph = load_work_graph(repository, backend=backend)
    graph_hash = work_graph_content_hash(graph)
    approvals = read_repo_work_graph_approvals(repository)
    if not approvals:
        return RepoApprovalStatus(
            is_current=False,
            graph_hash=graph_hash,
            approved_graph_hash=None,
            approval=None,
        )
    latest = approvals[-1]
    approved_hash = latest["graph_hash"]
    return RepoApprovalStatus(
        is_current=approved_hash == graph_hash,
        graph_hash=graph_hash,
        approved_graph_hash=approved_hash,
        approval=latest,
    )


def load_work_graph(
    repository: Path | None = None,
    *,
    backend: WorkGraphBackend | None = None,
) -> list[dict]:
    """Load and validate the Work Graph via a Work Graph backend.

    Uses the JSONL store for ``repository`` when ``backend`` is omitted.
    Validation (unique ids, resolvable dependencies, no cycles) always runs
    here so backend swaps cannot change graph semantics. A missing store is an
    empty graph.
    """
    store = backend if backend is not None else default_work_graph_backend(
        repository if repository is not None else Path.cwd()
    )
    return validate_work_graph(store.read_items())


def save_work_graph(
    items: list[dict],
    repository: Path | None = None,
    *,
    backend: WorkGraphBackend | None = None,
) -> list[dict]:
    """Validate then fully replace the Work Graph via a Work Graph backend.

    Uses the JSONL store for ``repository`` when ``backend`` is omitted.
    ``write_items`` replaces the entire stored set; validation runs before the
    write so invalid graphs never persist and backend swaps cannot change
    semantics.
    """
    validated = validate_work_graph(items)
    store = backend if backend is not None else default_work_graph_backend(
        repository if repository is not None else Path.cwd()
    )
    store.write_items(validated)
    return validated


@dataclass(frozen=True)
class AppliedDiscoveries:
    """Deterministic result of applying validated Discoveries to a Work Graph.

    ``applied`` lists the keys appended as proposed Work Items, in Discovery
    order. ``skipped_existing`` lists keys dropped because a Work Item with
    that id already exists. ``skipped_unresolved`` lists keys dropped because
    a dependency resolves to neither an existing Work Item nor an applied
    Discovery. ``graph`` is the validated Work Graph after application.
    """

    applied: list[str]
    skipped_existing: list[str]
    skipped_unresolved: list[str]
    graph: list[dict]


def apply_discoveries(
    discoveries: list[dict],
    repository: Path | None = None,
    *,
    backend: WorkGraphBackend | None = None,
) -> AppliedDiscoveries:
    """Apply role-output Discoveries to the Work Graph, deterministically.

    This is the only path by which Discoveries reach ``.agentflow/work/``:
    engine code validates the Discoveries (cap and per-output dedup keys),
    dedups against existing Work Item ids by dropping duplicates, and appends
    the remainder as ``proposed`` Work Items through ``save_work_graph`` so the
    whole graph is re-validated before anything persists. Agents never write
    the store directly. Re-applying the same Discoveries is a no-op, because
    every key then already exists in the graph.

    A Discovery may depend on existing Work Items or on other Discoveries in
    the same output. A Discovery whose dependencies never resolve (including
    dependency cycles within the output) is dropped deterministically and
    reported in ``skipped_unresolved``; nothing is written for it.
    """
    validated = validate_discoveries(discoveries)
    store = backend if backend is not None else default_work_graph_backend(
        repository if repository is not None else Path.cwd()
    )
    graph = validate_work_graph(store.read_items())
    existing_ids = {item["id"] for item in graph}
    skipped_existing = [
        discovery["key"] for discovery in validated
        if discovery["key"] in existing_ids
    ]
    pending = [
        discovery for discovery in validated
        if discovery["key"] not in existing_ids
    ]
    admitted: list[dict] = []
    admitted_keys: set[str] = set()
    # Fixpoint admission: a Discovery enters once all its dependencies resolve
    # to an existing id or an already-admitted key. Cycles within the output
    # never resolve, so they are dropped rather than persisted.
    progressed = True
    while progressed:
        progressed = False
        unresolved: list[dict] = []
        for discovery in pending:
            if all(
                dep in existing_ids or dep in admitted_keys
                for dep in discovery["depends_on"]
            ):
                admitted.append(discovery)
                admitted_keys.add(discovery["key"])
                progressed = True
            else:
                unresolved.append(discovery)
        pending = unresolved
    skipped_unresolved = [discovery["key"] for discovery in pending]
    if not admitted:
        return AppliedDiscoveries(
            applied=[],
            skipped_existing=skipped_existing,
            skipped_unresolved=skipped_unresolved,
            graph=graph,
        )
    proposals = [
        {
            "id": discovery["key"],
            "summary": discovery["summary"],
            "acceptance_criteria": discovery["acceptance_criteria"],
            "depends_on": discovery["depends_on"],
            "status": WORK_ITEM_STATUS_PROPOSED,
        }
        for discovery in admitted
    ]
    updated = save_work_graph(graph + proposals, backend=store)
    return AppliedDiscoveries(
        applied=[discovery["key"] for discovery in admitted],
        skipped_existing=skipped_existing,
        skipped_unresolved=skipped_unresolved,
        graph=updated,
    )


def completed_work_item_ids(data_dir: Path) -> set[str]:
    """Work-item ids a human-approved Run has already delivered.

    Completion is read from Run Evidence: a Work Item is done when a
    ``human_approved`` (or subsequently ``merged``) Run captured it (its Task
    Spec ``source.work_item_id`` names the item). Nothing is written back to
    the Work Graph.
    """
    completed: set[str] = set()
    for run in list_runs(data_dir=data_dir):
        if run.state not in COMPLETED_RUN_STATES:
            continue
        source = run.source
        if isinstance(source, dict) and source.get("work_item_id"):
            completed.add(source["work_item_id"])
    return completed


def compute_ready_work(
    graph: list[dict], completed_ids: set[str]
) -> list[dict]:
    """Work Items that are not yet complete and whose dependencies all are.

    Deterministic: the result preserves the graph's order. Ready work is derived
    on demand from the dependency relationships and the completion set; it is
    never stored. Items still marked ``proposed`` (appended from Discoveries but
    not yet human-approved) are excluded: a proposal becomes ready work only
    after a human removes the marker in a Framing decision.
    """
    ready: list[dict] = []
    for item in graph:
        if item.get("status") == WORK_ITEM_STATUS_PROPOSED:
            continue
        if item["id"] in completed_ids:
            continue
        if all(dep in completed_ids for dep in item["depends_on"]):
            ready.append(item)
    return ready


# --- File-scope overlap detection ------------------------------------------
#
# A Work Item's optional ``files`` field declares its scope as repository-
# relative glob patterns (validated in ``contracts.py``). Matching uses glob
# semantics with ``**`` as a recursive path-segment wildcard and a bare ``*``
# restricted to a single path segment (it never crosses ``/``) — the same
# convention as ``git`` pathspecs and ``pathlib``/``fnmatch`` glob patterns in
# general. This is deliberately a hand-rolled translator rather than
# ``PurePosixPath.full_match``/``fnmatch.fnmatch``: both of those treat a bare
# ``*`` as matching ``/`` too (there is no distinct recursive wildcard),
# which would make ``*`` and ``**`` behave identically and defeat the point
# of declaring ``**`` explicitly.


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one repository-relative glob pattern to an anchored regex.

    ``**/`` (and a trailing ``**``) matches zero or more path segments,
    including none — so ``src/**/test.py`` matches both ``src/test.py`` and
    ``src/a/b/test.py``. A bare ``*`` matches within a single path segment
    only. ``?`` matches one non-``/`` character. ``[...]`` character classes
    are passed through, with a leading ``!`` negated like ``fnmatch``.
    """
    i, length = 0, len(pattern)
    regex = ""
    while i < length:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i : i + 3] == "**/":
                    regex += "(?:.*/)?"
                    i += 3
                else:
                    regex += ".*"
                    i += 2
            else:
                regex += "[^/]*"
                i += 1
        elif char == "?":
            regex += "[^/]"
            i += 1
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                regex += re.escape(char)
                i += 1
            else:
                chunk = pattern[i + 1 : end]
                if chunk.startswith("!"):
                    chunk = "^" + chunk[1:]
                regex += "[" + chunk + "]"
                i = end + 1
        else:
            regex += re.escape(char)
            i += 1
    return re.compile("^" + regex + "$")


def _path_matches_any_scope(path: str, scopes: list[str]) -> bool:
    return any(_glob_to_regex(pattern).match(path) for pattern in scopes)


def items_touching(
    graph_items: list[dict], changed_paths: list[str]
) -> list[dict]:
    """Open Work Items whose declared ``files`` scope matches a changed path.

    Pure and deterministic: given the Work Graph's items and a list of
    repository-relative changed paths (as produced by
    ``changed_paths_for_commit``), returns the items in graph order whose
    ``files`` glob scope matches at least one changed path. ``proposed``
    items are excluded, matching the "open" Work Item notion used elsewhere
    (e.g. ``compute_ready_work``). Items without a ``files`` field have no
    scope and are never returned, matching current behavior exactly.
    """
    touched: list[dict] = []
    for item in graph_items:
        if item.get("status") == WORK_ITEM_STATUS_PROPOSED:
            continue
        scopes = item.get("files") or []
        if not scopes:
            continue
        if any(_path_matches_any_scope(path, scopes) for path in changed_paths):
            touched.append(item)
    return touched


def changed_paths_for_commit(repository: Path, sha: str) -> list[str]:
    """Repository-relative paths a commit changed, via ``git show``.

    A thin wrapper only: it shells out to
    ``git show --name-only --format= <sha>`` and parses the output into a
    list of paths, kept separate from the pure ``items_touching`` matcher so
    callers (the future work-drift-report command) can go from a commit SHA
    to overlapping Work Items without coupling the two concerns.
    """
    result = subprocess.run(
        ["git", "show", "--no-color", "--name-only", "--format=", sha],
        cwd=repository,
        text=True,
        capture_output=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
