"""Disposition-based recovery: absorb external work in one human review.

``agentflow work reconcile`` closes the gap between what a Target Repository's
Work Graph *intended* and what its git history and quarantined proposals show
*actually happened*. It is deliberately two commands, split at the human:

* ``work reconcile`` (planning, :func:`plan_reconcile`) reads the drift report
  and the completion-claim proposals and walks them into a *proposed*
  disposition per affected Work Item — one of the four
  ``RECONCILE_DISPOSITIONS``. It mutates nothing; every disposition it emits
  carries ``confirmed: false``. A human reviews the plan, edits or confirms each
  disposition, and hands the edited plan to the apply command.

* ``work reconcile-apply`` (:func:`apply_reconcile`) applies only the
  dispositions a human ``confirmed``, in one pass, producing a single updated
  Work Graph ready for one re-approval. It re-validates eligibility against the
  live graph itself — it never trusts the plan — and refuses any disposition
  that names a ``proposed`` or unknown Work Item.

A ``completed_externally`` disposition never invents Run Evidence. Completion by
outside work is recorded as its own attributed evidence in a git-tracked log
(``.agentflow/external-completions.jsonl``), explicitly typed apart from the
Run-backed completion that :func:`work_graph.completed_work_item_ids` derives, so
the two can never be confused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

from .contracts import (
    DISPOSITION_COMPLETED_EXTERNALLY,
    DISPOSITION_INVALIDATED,
    DISPOSITION_PARTIALLY_DONE,
    DISPOSITION_STILL_VALID,
    ContractError,
    PROPOSAL_KIND_COMPLETION_CLAIM,
    WORK_ITEM_STATUS_PROPOSED,
    validate_reconcile_disposition,
)
from .drift import KIND_CLOSED_ITEM, KIND_SCOPE_DRIFT, detect_work_drift
from .proposals import scan_proposals
from .work_graph import WorkGraphBackend, load_work_graph, save_work_graph

# Git-tracked external-completion evidence. Deliberately outside
# ``.agentflow/work/`` (like the approval mirror) so it is never folded into the
# Work Graph content hash, and deliberately *not* in Agentflow Home Run Evidence
# so it can never be mistaken for a Run-backed completion.
EXTERNAL_COMPLETIONS_RELATIVE = Path(".agentflow") / "external-completions.jsonl"
EXTERNAL_COMPLETION_TYPE = "external_completion"


@dataclass(frozen=True)
class ReconcilePlan:
    """A read-only reconcile plan: proposed dispositions awaiting confirmation.

    ``approval_boundary`` is the drift boundary the plan reconciles against, or
    ``None`` when the repository has never recorded an approval. ``dispositions``
    are proposed disposition records (each ``confirmed: false``) in
    ``work_item_id`` order, valid input to :func:`apply_reconcile` once a human
    confirms them. ``ineligible`` names ``proposed`` Work Items that carried a
    drift or claim signal but cannot be dispositioned until they pass Framing.
    """

    approval_boundary: str | None
    dispositions: list[dict] = field(default_factory=list)
    ineligible: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class ReconcileApplyResult:
    """Deterministic outcome of one confirmed apply pass.

    ``applied`` lists the confirmed dispositions acted on (``work_item_id`` and
    ``disposition``). ``skipped_unconfirmed`` lists work-item ids whose
    dispositions were left unconfirmed and so untouched. ``external_completions``
    are the evidence records appended for ``completed_externally`` items.
    ``removed_claims`` are completion-claim files consumed because every item
    they reference was dispositioned; ``pending_claims`` are the claims kept in
    place because at least one referenced item was not, preserving their pending
    signal. ``graph`` is the single validated Work Graph after the pass.
    """

    applied: list[dict]
    skipped_unconfirmed: list[str]
    external_completions: list[dict]
    removed_claims: list[str]
    pending_claims: list[dict]
    graph: list[dict]


def _commits_by_item(
    repository: Path, backend: WorkGraphBackend | None
) -> tuple[str | None, dict[str, list[str]]]:
    """Outside commits attributable to each Work Item, from the drift report.

    Only ``scope_drift`` and ``closed_item`` findings attribute a commit to a
    specific item; ``untracked`` and ``unknown_item`` name no graph item and are
    ignored here. Commits are de-duplicated per item in first-seen order.
    Returns the drift ``approval_boundary`` alongside the mapping.
    """
    report = detect_work_drift(repository, backend=backend)
    commits: dict[str, list[str]] = {}
    for finding in report.findings:
        if finding.work_item_id is None:
            continue
        if finding.kind not in (KIND_SCOPE_DRIFT, KIND_CLOSED_ITEM):
            continue
        bucket = commits.setdefault(finding.work_item_id, [])
        if finding.commit not in bucket:
            bucket.append(finding.commit)
    return report.approval_boundary, commits


def plan_reconcile(
    repository: Path,
    *,
    backend: WorkGraphBackend | None = None,
) -> ReconcilePlan:
    """Walk drift and completion-claims into proposed dispositions. Read-only.

    For every eligible Work Item that a drift finding or a completion-claim
    references, propose a disposition: ``completed_externally`` (with the outside
    commits named) when a completion-claim references the item *and* outside
    commits are attributable to it — the only honest way to claim external
    completion — and ``still_valid`` otherwise, the safe default a human upgrades
    to ``partially_done`` or ``invalidated`` as they see fit. ``proposed`` Work
    Items are ineligible for any disposition and are surfaced separately. Mutates
    nothing: every emitted disposition carries ``confirmed: false``.
    """
    repository = Path(repository)
    graph = load_work_graph(repository, backend=backend)
    by_id = {item["id"]: item for item in graph}
    boundary, commits_by_item = _commits_by_item(repository, backend)

    inbox = scan_proposals(repository)
    claims_by_item: dict[str, list[str]] = {}
    for proposal in inbox.valid:
        if proposal.kind != PROPOSAL_KIND_COMPLETION_CLAIM:
            continue
        for work_item_id in proposal.relates_to:
            claims_by_item.setdefault(work_item_id, []).append(proposal.filename)

    affected = set(commits_by_item) | set(claims_by_item)
    dispositions: list[dict] = []
    ineligible: list[dict] = []
    for work_item_id in sorted(affected):
        item = by_id.get(work_item_id)
        if item is None:
            # Named by drift or a claim but not an item in the current graph
            # (an already-closed id, or a claim referencing an unknown id).
            # There is nothing in the graph to disposition.
            continue
        if item.get("status") == WORK_ITEM_STATUS_PROPOSED:
            ineligible.append({"work_item_id": work_item_id, "reason": "proposed"})
            continue
        commits = sorted(commits_by_item.get(work_item_id, []))
        if work_item_id in claims_by_item and commits:
            proposed = {
                "work_item_id": work_item_id,
                "disposition": DISPOSITION_COMPLETED_EXTERNALLY,
                "confirmed": False,
                "external_commits": commits,
                "amended_acceptance_criteria": [],
            }
        else:
            proposed = {
                "work_item_id": work_item_id,
                "disposition": DISPOSITION_STILL_VALID,
                "confirmed": False,
                "external_commits": [],
                "amended_acceptance_criteria": [],
            }
        # Validate here so the plan is guaranteed to round-trip into apply.
        dispositions.append(validate_reconcile_disposition(proposed))
    return ReconcilePlan(
        approval_boundary=boundary,
        dispositions=dispositions,
        ineligible=ineligible,
    )


def _strip_removed_dependencies(item: dict, removed_ids: set[str]) -> dict:
    """Drop dependencies on externally-completed items so the graph stays valid.

    An item completed externally is removed from the graph, so a dependent that
    still names it would fail ``validate_work_graph``'s resolvable-dependency
    check. Removing the id is also semantically right: the dependency is done.
    """
    if not removed_ids:
        return item
    depends_on = item.get("depends_on", [])
    if not any(dep in removed_ids for dep in depends_on):
        return item
    clone = dict(item)
    clone["depends_on"] = [dep for dep in depends_on if dep not in removed_ids]
    return clone


def _append_external_completions(
    repository: Path,
    records: list[dict],
    *,
    confirmed_by: str,
    now: datetime,
) -> list[dict]:
    """Append attributed external-completion evidence under append-lock discipline.

    Mirrors the approval-log convention: each record's sequence is the file's
    line count plus one, computed while holding an exclusive lock so concurrent
    writers serialize and sequences stay contiguous. Every record is explicitly
    typed ``external_completion`` — never a Run completion — and attributed to
    the confirming human.
    """
    path = repository / EXTERNAL_COMPLETIONS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    written: list[dict] = []
    with path.open("r+", encoding="utf-8") as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        sequence = len([line for line in log.read().splitlines() if line.strip()])
        log.seek(0, os.SEEK_END)
        for record in records:
            sequence += 1
            entry = {
                "type": EXTERNAL_COMPLETION_TYPE,
                "work_item_id": record["work_item_id"],
                "external_commits": record["external_commits"],
                "confirmed_by": confirmed_by,
                "recorded_at": now.isoformat(),
                "sequence": sequence,
            }
            log.write(json.dumps(entry, sort_keys=True) + "\n")
            written.append(entry)
    return written


def apply_reconcile(
    repository: Path,
    dispositions: list[dict],
    *,
    confirmed_by: str,
    now: datetime | None = None,
    backend: WorkGraphBackend | None = None,
) -> ReconcileApplyResult:
    """Apply confirmed dispositions in one pass; re-validate eligibility itself.

    Every disposition is schema-validated, then each *confirmed* one is
    re-checked against the live Work Graph — never against the plan: a confirmed
    disposition naming an unknown or ``proposed`` Work Item aborts the whole pass
    before anything persists, so a stale or hand-edited plan can never mutate the
    graph. The confirmed dispositions are then applied to a single graph:

    * ``still_valid`` leaves the item untouched;
    * ``completed_externally`` removes the item and records attributed external
      evidence naming its outside commits (never a fabricated Run);
    * ``partially_done`` replaces the item's acceptance criteria with the amended
      ones;
    * ``invalidated`` marks the item ``proposed`` so it re-enters Framing.

    Completion-claim files are consumed only when every item they reference was
    dispositioned this pass; a multi-item claim with any undispositioned item is
    left in place so its pending signal survives. Unconfirmed dispositions are
    reported and left for a later confirmation pass. ``confirmed_by`` attributes
    the pass and is required.
    """
    repository = Path(repository)
    if not isinstance(confirmed_by, str) or not confirmed_by.strip():
        raise ContractError("reconcile apply requires a non-empty confirmed_by")
    if now is None:
        now = datetime.now(timezone.utc)
    validated = [validate_reconcile_disposition(entry) for entry in dispositions]

    # Reject any Work Item named by more than one disposition in the pass --
    # even a confirmed/unconfirmed pair. Such a plan is ambiguous about the
    # human's decision and would otherwise report the item as *both* applied and
    # skipped_unconfirmed, an incoherent single-pass outcome.
    all_ids = [entry["work_item_id"] for entry in validated]
    duplicates = sorted({item_id for item_id in all_ids if all_ids.count(item_id) > 1})
    if duplicates:
        raise ContractError(
            "reconcile apply has duplicate dispositions for the same Work Item: "
            f"{duplicates}"
        )

    confirmed = [entry for entry in validated if entry["confirmed"]]
    skipped_unconfirmed = sorted(
        entry["work_item_id"] for entry in validated if not entry["confirmed"]
    )

    graph = load_work_graph(repository, backend=backend)
    by_id = {item["id"]: item for item in graph}
    # Re-validate eligibility against the live graph; never trust the plan.
    for entry in confirmed:
        item = by_id.get(entry["work_item_id"])
        if item is None:
            raise ContractError(
                "reconcile apply refuses a disposition for unknown Work Item "
                f"{entry['work_item_id']!r}"
            )
        if item.get("status") == WORK_ITEM_STATUS_PROPOSED:
            raise ContractError(
                "reconcile apply refuses a disposition for proposed Work Item "
                f"{entry['work_item_id']!r}; it must pass Framing first"
            )
    confirmed_by_id = {entry["work_item_id"]: entry for entry in confirmed}

    removed_ids = {
        entry["work_item_id"]
        for entry in confirmed
        if entry["disposition"] == DISPOSITION_COMPLETED_EXTERNALLY
    }
    updated: list[dict] = []
    applied: list[dict] = []
    external_records: list[dict] = []
    for item in graph:
        entry = confirmed_by_id.get(item["id"])
        if entry is None:
            updated.append(_strip_removed_dependencies(item, removed_ids))
            continue
        disposition = entry["disposition"]
        applied.append({"work_item_id": item["id"], "disposition": disposition})
        if disposition == DISPOSITION_STILL_VALID:
            updated.append(_strip_removed_dependencies(item, removed_ids))
        elif disposition == DISPOSITION_COMPLETED_EXTERNALLY:
            external_records.append(
                {
                    "work_item_id": item["id"],
                    "external_commits": entry["external_commits"],
                }
            )
        elif disposition == DISPOSITION_PARTIALLY_DONE:
            amended = dict(item)
            amended["acceptance_criteria"] = list(entry["amended_acceptance_criteria"])
            updated.append(_strip_removed_dependencies(amended, removed_ids))
        elif disposition == DISPOSITION_INVALIDATED:
            invalidated = _strip_removed_dependencies(dict(item), removed_ids)
            invalidated["status"] = WORK_ITEM_STATUS_PROPOSED
            updated.append(invalidated)

    changed = any(
        entry["disposition"] != DISPOSITION_STILL_VALID for entry in confirmed
    )
    saved = save_work_graph(updated, repository, backend=backend) if changed else graph

    external_completions: list[dict] = []
    if external_records:
        external_completions = _append_external_completions(
            repository, external_records, confirmed_by=confirmed_by, now=now
        )

    dispositioned_ids = set(confirmed_by_id)
    removed_claims: list[str] = []
    pending_claims: list[dict] = []
    for proposal in scan_proposals(repository).valid:
        if proposal.kind != PROPOSAL_KIND_COMPLETION_CLAIM:
            continue
        related = proposal.relates_to
        if related and all(item_id in dispositioned_ids for item_id in related):
            proposal.path.unlink()
            removed_claims.append(proposal.filename)
        else:
            pending_claims.append(
                {
                    "filename": proposal.filename,
                    "relates_to": related,
                    "pending": sorted(set(related) - dispositioned_ids),
                }
            )
    return ReconcileApplyResult(
        applied=applied,
        skipped_unconfirmed=skipped_unconfirmed,
        external_completions=external_completions,
        removed_claims=sorted(removed_claims),
        pending_claims=pending_claims,
        graph=saved,
    )
