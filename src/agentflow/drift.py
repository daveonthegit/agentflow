"""Read-only Work-Graph drift report: work that landed outside the graph.

``agentflow work drift`` reconciles the commits a repository has *actually*
landed since its last Work Graph approval against the work the graph *intended*.
It is a diagnosis aid, not a mutation: it reads the Target Repository and its
git history alone — no Agentflow Home state, no network — and never writes
anything. That makes it safe to run anywhere the repository is checked out,
including a CI runner, exactly like ``agentflow work verify``.

The last approval is the boundary. The git-tracked approval mirror
(``.agentflow/approvals.jsonl``) is appended and committed whenever a human
approves the Work Graph, so the commit that most recently touched that file is
the "since the last approval" boundary; every commit after it is a candidate
for drift. When the repository has no approval commit at all, the whole history
reachable from ``HEAD`` is analyzed instead, because nothing has been reconciled.

Three independently-evaluated conditions surface as findings, matching the three
ways work escapes the graph:

* ``untracked`` — a commit carries no ``Work-Item`` trailer, so nothing ties it
  to an intended Work Item.
* ``unknown_item`` / ``closed_item`` — a commit's ``Work-Item`` trailer names an
  id that is not an item in the *current* graph. It is ``closed_item`` when that
  id appeared somewhere in the Work Graph's git history (a completed item that
  was edited out, now receiving more work) and ``unknown_item`` when it never
  did (a typo or a fabricated id).
* ``scope_drift`` — a commit touches a path inside an open Work Item's declared
  ``files`` scope without carrying that item's trailer, so scoped work landed
  unattributed.

A single commit can raise more than one finding: an untracked commit that also
edits an open item's declared scope is both ``untracked`` and ``scope_drift``,
because both statements are independently true and each points at a different
remediation. Findings are emitted deterministically — commits in chronological
order, and within a commit: the untracked finding, then trailer findings by
sorted id, then scope-drift findings in Work Graph order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess

from .work_graph import (
    WorkGraphBackend,
    changed_paths_for_commit,
    items_touching,
    load_work_graph,
)

APPROVALS_RELATIVE = ".agentflow/approvals.jsonl"
WORK_DIR_RELATIVE = ".agentflow/work"

# Matches an ``"id":"<value>"`` field in a JSONL Work Graph line, tolerant of
# the optional whitespace ``json.dumps(indent=...)`` may introduce. Used only to
# scan git history for ids the Work Graph has ever carried.
_ID_FIELD = re.compile(r'"id"\s*:\s*"([^"]+)"')

# Drift finding kinds, listed here as the single source of truth.
KIND_UNTRACKED = "untracked"
KIND_UNKNOWN_ITEM = "unknown_item"
KIND_CLOSED_ITEM = "closed_item"
KIND_SCOPE_DRIFT = "scope_drift"


@dataclass(frozen=True)
class DriftFinding:
    """One reconciliation finding against a single commit.

    ``kind`` is one of the ``KIND_*`` constants. ``work_item_id`` names the Work
    Item the finding is about for every kind except ``untracked`` (which is
    about the commit as a whole and carries ``None``).
    """

    commit: str
    subject: str
    kind: str
    work_item_id: str | None = None


@dataclass(frozen=True)
class CommitRecord:
    """A commit reduced to just what drift classification needs.

    Pure data so ``classify_drift`` can be exercised without git.
    """

    sha: str
    subject: str
    trailers: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriftReport:
    """The deterministic result of a drift analysis.

    ``approval_boundary`` is the commit whose approval the analysis reconciles
    against, or ``None`` when the repository has never recorded an approval.
    ``analyzed_commits`` are the shas examined, in chronological order.
    """

    approval_boundary: str | None
    analyzed_commits: tuple[str, ...] = ()
    findings: tuple[DriftFinding, ...] = field(default_factory=tuple)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


def classify_drift(
    commits: list[CommitRecord],
    graph_items: list[dict],
    ever_known_ids: set[str],
) -> tuple[DriftFinding, ...]:
    """Classify commits into drift findings — pure and deterministic.

    ``graph_items`` is the current, validated Work Graph. ``ever_known_ids`` is
    every id the Work Graph has carried across git history, used only to tell a
    ``closed_item`` (an id that once existed) from an ``unknown_item`` (an id
    that never did). Given the same inputs this always yields the same findings
    in the same order, so callers never sort the result.
    """
    current_ids = {item["id"] for item in graph_items}
    findings: list[DriftFinding] = []
    for commit in commits:
        trailer_ids = set(commit.trailers)
        if not trailer_ids:
            findings.append(
                DriftFinding(commit.sha, commit.subject, KIND_UNTRACKED)
            )
        else:
            for item_id in sorted(trailer_ids):
                if item_id in current_ids:
                    continue
                kind = (
                    KIND_CLOSED_ITEM
                    if item_id in ever_known_ids
                    else KIND_UNKNOWN_ITEM
                )
                findings.append(
                    DriftFinding(commit.sha, commit.subject, kind, item_id)
                )
        for item in items_touching(graph_items, list(commit.changed_paths)):
            if item["id"] not in trailer_ids:
                findings.append(
                    DriftFinding(
                        commit.sha,
                        commit.subject,
                        KIND_SCOPE_DRIFT,
                        item["id"],
                    )
                )
    return tuple(findings)


def _git(repository: Path, *args: str) -> str:
    """Run a read-only git command in ``repository`` and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _approval_boundary_commit(repository: Path) -> str | None:
    """The commit that most recently recorded a Work Graph approval, if any."""
    try:
        out = _git(
            repository, "rev-list", "-n", "1", "HEAD", "--", APPROVALS_RELATIVE
        ).strip()
    except subprocess.CalledProcessError:
        return None
    return out or None


def _commit_records(
    repository: Path, boundary: str | None
) -> list[CommitRecord]:
    """Build a ``CommitRecord`` for every commit since ``boundary`` (exclusive).

    With no boundary the whole history reachable from ``HEAD`` is walked.
    Commits are returned oldest-first so the report reads in landing order.
    """
    range_spec = f"{boundary}..HEAD" if boundary else "HEAD"
    try:
        out = _git(repository, "rev-list", "--reverse", range_spec)
    except subprocess.CalledProcessError:
        return []
    records: list[CommitRecord] = []
    for sha in (line.strip() for line in out.splitlines() if line.strip()):
        subject = _git(repository, "show", "-s", "--format=%s", sha).strip()
        trailers_raw = _git(
            repository,
            "show",
            "-s",
            "--format=%(trailers:key=Work-Item,valueonly,separator=%x0A)",
            sha,
        )
        trailers = tuple(
            value.strip()
            for value in trailers_raw.splitlines()
            if value.strip()
        )
        changed = tuple(changed_paths_for_commit(repository, sha))
        records.append(CommitRecord(sha, subject, trailers, changed))
    return records


def _ever_known_ids(repository: Path) -> set[str]:
    """Every Work Item id that has ever appeared in the Work Graph's history.

    Scans the diff history of ``.agentflow/work`` for ``id`` fields, so an id
    edited out of the current graph is still recognized as one that once
    existed. Reads history only; nothing is mutated.
    """
    try:
        out = _git(repository, "log", "--format=", "-p", "--", WORK_DIR_RELATIVE)
    except subprocess.CalledProcessError:
        return set()
    return set(_ID_FIELD.findall(out))


def detect_work_drift(
    repository: Path,
    *,
    backend: WorkGraphBackend | None = None,
) -> DriftReport:
    """Reconcile landed commits against the Work Graph, from the repo alone.

    Reads the Target Repository's git history, its current Work Graph, and the
    git-tracked approval mirror — no Agentflow Home state and no network — and
    returns a ``DriftReport``. It mutates nothing. Raises ``ContractError`` (via
    ``load_work_graph``) only when the current Work Graph itself is invalid, so
    the caller can report a clean, actionable message and exit nonzero.
    """
    repository = Path(repository)
    boundary = _approval_boundary_commit(repository)
    commits = _commit_records(repository, boundary)
    graph = load_work_graph(repository, backend=backend)
    ever_known = _ever_known_ids(repository)
    findings = classify_drift(commits, graph, ever_known)
    return DriftReport(
        approval_boundary=boundary,
        analyzed_commits=tuple(record.sha for record in commits),
        findings=findings,
    )
