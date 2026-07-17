"""Single-pass reconciliation: dispatch ready Work Items and advance their Runs.

Reconcile reads the Target Repository's Work Graph and Run Evidence, captures
each ready Work Item that has no live Run into a new gated Run, and advances
every graph-backed Run toward the next human gate — never through it. It acts
only by issuing the same application-service calls the CLI uses (`start_run`,
`advance_run`), so all execution truth stays in the per-Run event logs; the
returned report is a summary of the decisions it made this pass.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .agent_adapter import AgentAdapter
from .run_kernel import LIVE_RUN_STATES, list_runs, read_run_status, start_run
from .work_graph import (
    compute_ready_work,
    completed_work_item_ids,
    load_work_graph,
    require_approved_work_graph,
    work_item_content_hash,
)
from .workflow import advance_run

# States from which `advance` performs another stage. Reconcile keeps advancing
# a Run while it is in one of these.
_ADVANCEABLE = frozenset(
    {
        "ready",
        "planned",
        "built",
        "verified",
        "tested",
        "changes_requested",
        "tests_failed",
    }
)
# Defensive cap so a malformed state can never spin the per-Run loop forever;
# a real Run reaches a human gate or terminal state well within this.
_MAX_STEPS_PER_RUN = 24


def _runs_by_work_item(data_dir: Path) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for run in list_runs(data_dir=data_dir):
        source = run.source
        if isinstance(source, dict) and source.get("work_item_id"):
            grouped.setdefault(source["work_item_id"], []).append(run)
    return grouped


def _advance_to_human_gate(
    *,
    run_id: str,
    data_dir: Path,
    adapter: AgentAdapter | None,
) -> str:
    """Advance a Run until it needs a human or terminates; never approve."""
    status = read_run_status(run_id=run_id, data_dir=data_dir)
    steps = 0
    while status.state in _ADVANCEABLE and steps < _MAX_STEPS_PER_RUN:
        advance_run(run_id=run_id, data_dir=data_dir, adapter=adapter)
        status = read_run_status(run_id=run_id, data_dir=data_dir)
        steps += 1
    return status.state


def reconcile(
    *,
    repository: Path,
    data_dir: Path,
    adapter: AgentAdapter | None,
    now: datetime | None = None,
) -> dict:
    """Run one reconciliation pass and return a decision report.

    Dispatches each ready Work Item that has no live Run into a new Run and
    advances every graph-backed Run to its next human gate. Records nothing
    directly: every action is a `start_run`/`advance_run` call whose evidence
    lands in the Run's own event log. Never advances a Run through approval.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    graph = load_work_graph(repository)
    completed = completed_work_item_ids(data_dir)
    ready = compute_ready_work(graph, completed)
    runs_by_item = _runs_by_work_item(data_dir)

    dispatched: list[dict] = []
    advanced: list[dict] = []
    blocked_ids = sorted(
        item["id"] for item in graph if item not in ready and item["id"] not in completed
    )

    # Capture gate, checked once up front so an unapproved or since-edited
    # Work Graph refuses the whole pass before any Run is dispatched, rather
    # than failing partway through. `start_run` re-enforces the same gate.
    if any(
        not any(
            run.state in LIVE_RUN_STATES
            for run in runs_by_item.get(item["id"], [])
        )
        for item in ready
    ):
        require_approved_work_graph(repository=repository, data_dir=data_dir)

    for item in ready:
        live = [
            run
            for run in runs_by_item.get(item["id"], [])
            if run.state in LIVE_RUN_STATES
        ]
        if live:
            # A Run already exists for this item; advance it (unless it is
            # already parked at the human gate) rather than dispatching a second.
            run = live[0]
            if run.state in _ADVANCEABLE:
                final = _advance_to_human_gate(
                    run_id=run.run_id, data_dir=data_dir, adapter=adapter
                )
                advanced.append({"work_item_id": item["id"], "run_id": run.run_id, "state": final})
            continue
        started = start_run(
            summary=item["summary"],
            acceptance_criteria=item["acceptance_criteria"],
            source={
                "provider": "work-graph",
                "work_item_id": item["id"],
                "captured_at": now.isoformat(),
                "content_hash": work_item_content_hash(item),
            },
            repository=repository,
            data_dir=data_dir,
        )
        final = _advance_to_human_gate(
            run_id=started.run_id, data_dir=data_dir, adapter=adapter
        )
        dispatched.append(
            {"work_item_id": item["id"], "run_id": started.run_id, "state": final}
        )

    return {
        "dispatched": dispatched,
        "advanced": advanced,
        "blocked": blocked_ids,
        "completed": sorted(completed),
    }
