"""Quarantined proposals inbox: a filesystem entry door to the Work Graph.

Any agent — including a foreign one with no Agentflow knowledge — may drop a
JSON file into ``.agentflow/proposals/``. These files are quarantined: they live
outside ``.agentflow/work/``, so ``work_graph_content_hash`` never sees them and
writing one can neither invalidate an approved Work Graph nor block a commit.

A proposal reaches the Work Graph only through :func:`ingest_proposals`, which
applies the same deterministic validation the in-band Discoveries machinery uses:
valid ``new-work`` proposals become ``proposed`` Work Items through
``apply_discoveries`` (capped, deduped against the graph and each other, and
re-validated by ``save_work_graph`` before anything persists). Completion-claims
are never applied — they surface for human disposition and their files stay in
place for a future reconcile command. Invalid files are reported, never applied,
and never fatal.

This is Discoveries-from-foreign-agents: the same trust model, reached through a
filesystem door instead of a validated role-output contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .contracts import (
    MAX_PROPOSALS_PER_INGEST,
    PROPOSAL_KIND_COMPLETION_CLAIM,
    PROPOSAL_KIND_NEW_WORK,
    ContractError,
    proposal_work_item_id,
    validate_proposal,
)
from .work_graph import WorkGraphBackend, apply_discoveries

PROPOSALS_RELATIVE_DIR = Path(".agentflow/proposals")


@dataclass(frozen=True)
class ScannedProposal:
    """One validated proposal file, with its content-derived Work-Item id.

    ``work_item_id`` is set only for ``new-work`` proposals — the id ingest
    would append as a ``proposed`` Work Item. Completion-claims never become
    Work Items, so theirs is ``None``.
    """

    filename: str
    path: Path
    kind: str
    summary: str
    acceptance_criteria: list[str]
    relates_to: list[str]
    work_item_id: str | None


@dataclass(frozen=True)
class ScannedInbox:
    """Non-mutating read of the proposals inbox.

    ``valid`` are validated proposals in deterministic (filename) order.
    ``invalid`` are ``{"filename", "reason"}`` records for files that failed to
    parse or validate; a bad file is reported here and never aborts the scan.
    """

    valid: list[ScannedProposal]
    invalid: list[dict]


def _proposals_dir(repository: Path) -> Path:
    return repository / PROPOSALS_RELATIVE_DIR


def scan_proposals(repository: Path) -> ScannedInbox:
    """Read and validate every ``*.json`` file in the inbox, without mutating.

    Each file is validated independently: a parse or schema failure yields a
    reported defect (filename and reason) and never aborts the scan of the
    remaining files. A missing inbox is an empty scan.
    """
    directory = _proposals_dir(repository)
    valid: list[ScannedProposal] = []
    invalid: list[dict] = []
    if not directory.is_dir():
        return ScannedInbox(valid=valid, invalid=invalid)
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            invalid.append({"filename": path.name, "reason": str(error)})
            continue
        try:
            proposal = validate_proposal(raw)
        except ContractError as error:
            invalid.append({"filename": path.name, "reason": str(error)})
            continue
        work_item_id = (
            proposal_work_item_id(proposal)
            if proposal["kind"] == PROPOSAL_KIND_NEW_WORK
            else None
        )
        valid.append(
            ScannedProposal(
                filename=path.name,
                path=path,
                kind=proposal["kind"],
                summary=proposal["summary"],
                acceptance_criteria=proposal["acceptance_criteria"],
                relates_to=proposal["relates_to"],
                work_item_id=work_item_id,
            )
        )
    return ScannedInbox(valid=valid, invalid=invalid)


@dataclass(frozen=True)
class IngestResult:
    """Deterministic outcome of one ingest pass over the proposals inbox.

    ``applied`` are ids newly appended as ``proposed`` Work Items, in ingest
    order. ``skipped_existing`` are new-work ids already present in the graph
    (an idempotent re-drop). ``skipped_duplicate`` are new-work ids that
    collided with an earlier file in the same pass. ``skipped_over_cap`` are
    new-work ids beyond the per-ingest cap, left in place for a later pass.
    ``completion_claims`` are surfaced for human disposition and never applied.
    ``invalid`` are reported defects. ``removed`` are the proposal filenames
    consumed this pass. ``graph`` is the validated Work Graph afterwards.
    """

    applied: list[str]
    skipped_existing: list[str]
    skipped_duplicate: list[str]
    skipped_over_cap: list[str]
    completion_claims: list[dict]
    invalid: list[dict]
    removed: list[str]
    graph: list[dict]


def ingest_proposals(
    repository: Path,
    *,
    backend: WorkGraphBackend | None = None,
) -> IngestResult:
    """Deterministically ingest the proposals inbox into the Work Graph.

    Valid ``new-work`` proposals become ``proposed`` Work Items through
    ``apply_discoveries`` — the sole path by which anything reaches
    ``.agentflow/work/`` — so ingest cannot mint a non-proposed item and the
    whole graph is re-validated before it persists. New-work proposals are
    deduped by content-derived id against the graph and against one another, and
    capped at ``MAX_PROPOSALS_PER_INGEST`` per pass; the overflow is left in the
    inbox for a later pass. Every consumed file (applied, already-present, or a
    same-pass duplicate) is removed so re-running is idempotent.

    Completion-claims are never applied: closing a Work Item requires human
    disposition. They are surfaced in ``completion_claims`` and their files are
    left in place for the future reconcile command. Invalid files are reported
    and left in place for their author to fix; nothing here is ever fatal.
    """
    scanned = scan_proposals(repository)
    new_work = [p for p in scanned.valid if p.kind == PROPOSAL_KIND_NEW_WORK]
    claims = [p for p in scanned.valid if p.kind == PROPOSAL_KIND_COMPLETION_CLAIM]

    # Dedup new-work by content-derived id, keeping the first file (already
    # filename-sorted) so ``apply_discoveries`` never sees duplicate keys.
    seen: set[str] = set()
    unique: list[ScannedProposal] = []
    duplicates: list[ScannedProposal] = []
    for proposal in new_work:
        assert proposal.work_item_id is not None
        if proposal.work_item_id in seen:
            duplicates.append(proposal)
        else:
            seen.add(proposal.work_item_id)
            unique.append(proposal)

    # Cap per pass; the overflow stays in the inbox for a later ingest.
    admitted = unique[:MAX_PROPOSALS_PER_INGEST]
    over_cap = unique[MAX_PROPOSALS_PER_INGEST:]

    # ``relates_to`` is advisory metadata, not a hard graph edge: a foreign
    # proposal must not silently inject dependencies. Proposed items carry no
    # ``depends_on`` so a human wires real dependencies during Framing.
    discoveries = [
        {
            "key": proposal.work_item_id,
            "summary": proposal.summary,
            "acceptance_criteria": proposal.acceptance_criteria,
            "depends_on": [],
        }
        for proposal in admitted
    ]
    result = apply_discoveries(discoveries, repository, backend=backend)
    applied = set(result.applied)
    existing = set(result.skipped_existing)

    removed: list[str] = []
    for proposal in admitted:
        if proposal.work_item_id in applied or proposal.work_item_id in existing:
            proposal.path.unlink()
            removed.append(proposal.filename)
    for proposal in duplicates:
        proposal.path.unlink()
        removed.append(proposal.filename)

    completion_claims = [
        {
            "filename": proposal.filename,
            "summary": proposal.summary,
            "relates_to": proposal.relates_to,
        }
        for proposal in claims
    ]
    return IngestResult(
        applied=result.applied,
        skipped_existing=result.skipped_existing,
        skipped_duplicate=[p.work_item_id for p in duplicates],
        skipped_over_cap=[p.work_item_id for p in over_cap],
        completion_claims=completion_claims,
        invalid=scanned.invalid,
        removed=sorted(removed),
        graph=result.graph,
    )
