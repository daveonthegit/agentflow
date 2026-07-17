"""Improvement Proposals: deterministic learning from repeated Run Evidence.

A proposal is a *record*, never an applied change. This module covers the two
stages that precede the Adoption Gate:

1. **Generation** — :func:`generate_proposals` replays stored Run Evidence,
   detects patterns that recur across distinct Runs (the same Repository
   Profile check failing, builder repair loops fired by the same trigger), and
   persists one Improvement Proposal per pattern under
   ``<data_dir>/proposals/<proposal_id>/proposal.json``. Proposal ids are
   content-derived from the pattern, so regeneration is idempotent.
2. **Evaluation** — :func:`evaluate_proposal` re-checks the proposal against
   live evidence and replays the detector over the fixed fixture cases in
   :data:`DEFAULT_FIXTURES_DIR` (recorded evidence corpora, including
   historical false-positive cases that must never detect). The pass/fail
   result and its reasons are recorded as evidence in ``evaluation.json``.

Baselines — Repository Profiles, skills, workflow configuration — are never
touched here. :func:`apply_proposal_to_baseline` enforces that a proposal
which has not passed evaluation cannot change any baseline, and that even a
passing one stops at the ``evaluated`` state: actually changing a baseline is
the Adoption Gate's job (human approval), a deliberately unimplemented seam.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

# A pattern must recur across at least this many distinct Runs before it
# motivates a proposal. Repetition inside a single Run (e.g. one Run failing
# the same check on every repair attempt) never counts as recurrence.
MIN_RECURRENCE_RUNS = 3

# Proposal kinds and the baseline each one targets. The target names what the
# Adoption Gate would eventually be allowed to change, never what this module
# changes.
KIND_RECURRING_CHECK_FAILURE = "recurring_check_failure"
KIND_RECURRING_REPAIR_LOOP = "recurring_repair_loop"
TARGET_BY_KIND = {
    KIND_RECURRING_CHECK_FAILURE: "repository_profile",
    KIND_RECURRING_REPAIR_LOOP: "workflow_config",
}

# Repair loops are keyed by the evidence event that triggered them.
REPAIR_TRIGGER_EVENTS = ("review_blocked", "tests_failed")
UNKNOWN_REPAIR_TRIGGER = "unknown"

PROPOSAL_ID_LENGTH = 16

# The fixed fixture corpus shipped with Agentflow: recorded evidence cases,
# including historical failures the detector must not regress on.
DEFAULT_FIXTURES_DIR = Path(__file__).parent / "eval_fixtures" / "improvement"


def _proposals_dir(data_dir: Path) -> Path:
    return data_dir / "proposals"


def _proposal_dir(data_dir: Path, proposal_id: str) -> Path:
    return _proposals_dir(data_dir) / proposal_id


def proposal_id_for(kind: str, subject: str) -> str:
    """Content-derived stable id: the same pattern always gets the same id."""
    payload = json.dumps({"kind": kind, "subject": subject}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:PROPOSAL_ID_LENGTH]


def summarize_run_events(
    run_id: str,
    events: list[dict],
    read_artifact: Callable[[str], dict | None],
) -> dict:
    """Reduce one Run's event log to the pattern-relevant evidence.

    ``read_artifact`` resolves an event's ``artifact`` reference to its JSON
    content (or ``None`` when unavailable); disk and fixture replays supply
    different resolvers so the same reduction runs against both. Repetition
    within the Run is collapsed: each failed check command and each repair
    trigger appears at most once per Run.
    """
    failed_checks: set[str] = set()
    repair_triggers: set[str] = set()
    last_trigger = UNKNOWN_REPAIR_TRIGGER
    for event in events:
        event_type = event.get("type")
        if event_type in REPAIR_TRIGGER_EVENTS:
            last_trigger = event_type
        if event_type == "checks_failed":
            artifact_ref = event.get("artifact")
            artifact = (
                read_artifact(artifact_ref) if isinstance(artifact_ref, str) else None
            )
            if artifact is None:
                continue
            for check in artifact.get("checks", []):
                if check.get("returncode", 0) != 0 and isinstance(
                    check.get("command"), str
                ):
                    failed_checks.add(check["command"])
        if event_type == "repair_ready":
            repair_triggers.add(last_trigger)
    return {
        "failed_checks": sorted(failed_checks),
        "repair_triggers": sorted(repair_triggers),
        "run_id": run_id,
    }


def summarize_stored_run_evidence(data_dir: Path) -> list[dict]:
    """Summarize every stored Run's evidence, read-only and order-stable."""
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        return []
    summaries: list[dict] = []
    for run_dir in sorted(runs_dir.iterdir()):
        events_path = run_dir / "events.jsonl"
        if not events_path.is_file():
            continue
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        def _read_artifact(reference: str) -> dict | None:
            path = Path(reference)
            if not path.is_file():
                return None
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            return loaded if isinstance(loaded, dict) else None

        summaries.append(summarize_run_events(run_dir.name, events, _read_artifact))
    return summaries


def detect_patterns(summaries: list[dict], *, min_runs: int) -> list[dict]:
    """Detect patterns recurring across at least ``min_runs`` distinct Runs.

    Purely a function of its inputs: the same summaries always yield the same
    patterns in the same order, which is what lets evaluation replay this
    detector over fixed fixture corpora.
    """
    check_runs: dict[str, set[str]] = {}
    trigger_runs: dict[str, set[str]] = {}
    for summary in summaries:
        run_id = summary["run_id"]
        for command in summary["failed_checks"]:
            check_runs.setdefault(command, set()).add(run_id)
        for trigger in summary["repair_triggers"]:
            trigger_runs.setdefault(trigger, set()).add(run_id)

    patterns: list[dict] = []
    for command in sorted(check_runs):
        run_ids = check_runs[command]
        if len(run_ids) >= min_runs:
            patterns.append(
                {
                    "kind": KIND_RECURRING_CHECK_FAILURE,
                    "run_ids": sorted(run_ids),
                    "subject": command,
                }
            )
    for trigger in sorted(trigger_runs):
        run_ids = trigger_runs[trigger]
        if len(run_ids) >= min_runs:
            patterns.append(
                {
                    "kind": KIND_RECURRING_REPAIR_LOOP,
                    "run_ids": sorted(run_ids),
                    "subject": trigger,
                }
            )
    return patterns


def _change_description(kind: str, subject: str, run_count: int) -> str:
    if kind == KIND_RECURRING_CHECK_FAILURE:
        return (
            f"Review the Repository Profile check {subject!r}: it failed in "
            f"{run_count} distinct Runs."
        )
    return (
        f"Review the workflow repair configuration: builder repair loops "
        f"triggered by {subject!r} occurred in {run_count} distinct Runs."
    )


def generate_proposals(
    *,
    data_dir: Path,
    min_runs: int = MIN_RECURRENCE_RUNS,
) -> list[dict]:
    """Detect recurring patterns in stored Run Evidence and persist proposals.

    Each detected pattern becomes one proposal record under
    ``<data_dir>/proposals/<proposal_id>/proposal.json``. Regeneration is
    idempotent: a still-``proposed`` record is refreshed with the current
    evidence references, while an already-evaluated proposal is left exactly
    as evaluated. Nothing outside the proposals directory is written.
    """
    summaries = summarize_stored_run_evidence(data_dir)
    proposals: list[dict] = []
    for pattern in detect_patterns(summaries, min_runs=min_runs):
        proposal_id = proposal_id_for(pattern["kind"], pattern["subject"])
        record = {
            "change": _change_description(
                pattern["kind"], pattern["subject"], len(pattern["run_ids"])
            ),
            "evidence": [{"run_id": run_id} for run_id in pattern["run_ids"]],
            "kind": pattern["kind"],
            "min_runs": min_runs,
            "proposal_id": proposal_id,
            "subject": pattern["subject"],
            "target": TARGET_BY_KIND[pattern["kind"]],
        }
        proposal_dir = _proposal_dir(data_dir, proposal_id)
        if not (proposal_dir / "evaluation.json").exists():
            proposal_dir.mkdir(parents=True, exist_ok=True)
            (proposal_dir / "proposal.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        proposals.append(read_proposal(data_dir=data_dir, proposal_id=proposal_id))
    return proposals


def read_proposal(*, data_dir: Path, proposal_id: str) -> dict:
    """Return the proposal record with its derived state and any evaluation.

    State is derived from evidence on disk, mirroring Run State: ``proposed``
    until an evaluation record exists, then ``evaluated``. There is no state
    beyond ``evaluated`` here — adoption belongs to the future Adoption Gate.
    """
    proposal_dir = _proposal_dir(data_dir, proposal_id)
    proposal_path = proposal_dir / "proposal.json"
    if not proposal_path.is_file():
        raise ValueError(f"no proposal {proposal_id}")
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    evaluation_path = proposal_dir / "evaluation.json"
    if evaluation_path.is_file():
        record["evaluation"] = json.loads(evaluation_path.read_text(encoding="utf-8"))
        record["state"] = "evaluated"
    else:
        record["state"] = "proposed"
    return record


def list_proposals(*, data_dir: Path) -> list[dict]:
    proposals_dir = _proposals_dir(data_dir)
    if not proposals_dir.is_dir():
        return []
    return [
        read_proposal(data_dir=data_dir, proposal_id=entry.name)
        for entry in sorted(proposals_dir.iterdir())
        if (entry / "proposal.json").is_file()
    ]


def _load_fixture_cases(fixtures_dir: Path) -> list[tuple[str, dict]]:
    if not fixtures_dir.is_dir():
        raise ValueError(f"no fixtures directory at {fixtures_dir}")
    cases = [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(fixtures_dir.glob("*.json"))
    ]
    if not cases:
        raise ValueError(f"no fixture cases in {fixtures_dir}")
    return cases


def _replay_fixture_case(case: dict) -> list[str]:
    """Replay the detector over one recorded evidence corpus.

    Fixture runs embed their check artifacts by name, so the same reduction
    that reads disk evidence runs against the recorded corpus.
    """
    summaries = []
    for run in case["runs"]:
        artifacts = run.get("artifacts", {})
        summaries.append(
            summarize_run_events(run["run_id"], run["events"], artifacts.get)
        )
    patterns = detect_patterns(summaries, min_runs=case["min_runs"])
    return sorted(
        pattern["subject"] for pattern in patterns if pattern["kind"] == case["kind"]
    )


def evaluate_proposal(
    *,
    data_dir: Path,
    proposal_id: str,
    fixtures_dir: Path | None = None,
) -> dict:
    """Evaluate a proposal against fixed fixtures and live historical evidence.

    Three deterministic checks, all of which must pass:

    1. Every evidence reference still resolves to a stored Run.
    2. The pattern still recurs in stored Run Evidence at the proposal's own
       recurrence threshold.
    3. Replaying the detector over every fixture case of the proposal's kind
       reproduces exactly the recorded expected subjects — including the
       historical-failure cases that must detect nothing.

    The result is recorded as evidence in the proposal's ``evaluation.json``
    and moves the proposal to the ``evaluated`` state. Passing evaluation
    never changes a baseline; that requires the future Adoption Gate.
    """
    if fixtures_dir is None:
        fixtures_dir = DEFAULT_FIXTURES_DIR
    proposal = read_proposal(data_dir=data_dir, proposal_id=proposal_id)
    reasons: list[str] = []

    runs_dir = data_dir / "runs"
    for reference in proposal["evidence"]:
        if not (runs_dir / reference["run_id"] / "events.jsonl").is_file():
            reasons.append(
                f"evidence reference {reference['run_id']} does not resolve "
                "to a stored Run"
            )

    summaries = summarize_stored_run_evidence(data_dir)
    live_patterns = detect_patterns(summaries, min_runs=proposal["min_runs"])
    if not any(
        pattern["kind"] == proposal["kind"]
        and pattern["subject"] == proposal["subject"]
        for pattern in live_patterns
    ):
        reasons.append(
            "pattern no longer recurs in stored Run Evidence at "
            f"min_runs={proposal['min_runs']}"
        )

    cases = _load_fixture_cases(fixtures_dir)
    replayed_case_names: list[str] = []
    for name, case in cases:
        if case["kind"] != proposal["kind"]:
            continue
        replayed_case_names.append(name)
        detected = _replay_fixture_case(case)
        expected = sorted(case["expected_subjects"])
        if detected != expected:
            reasons.append(
                f"fixture case {name} expected subjects {expected}, "
                f"detector found {detected}"
            )
    if not replayed_case_names:
        reasons.append(
            f"no fixture case covers proposal kind {proposal['kind']!r}"
        )

    evaluation = {
        "fixture_cases": replayed_case_names,
        "fixtures_dir": str(fixtures_dir),
        "passed": not reasons,
        "proposal_id": proposal_id,
        "reasons": reasons,
    }
    (_proposal_dir(data_dir, proposal_id) / "evaluation.json").write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evaluation


def apply_proposal_to_baseline(*, data_dir: Path, proposal_id: str) -> None:
    """Refuse to change any baseline via the proposal path.

    A proposal that has not passed evaluation is rejected outright. A proposal
    that has passed still cannot change a baseline: adoption requires the
    Adoption Gate (explicit verification plus human approval), which is a
    separate, deliberately unimplemented seam. This function is the single
    choke point the future gate will replace, so no baseline write can ever
    bypass it.
    """
    proposal = read_proposal(data_dir=data_dir, proposal_id=proposal_id)
    evaluation = proposal.get("evaluation")
    if evaluation is None or not evaluation["passed"]:
        raise ValueError(
            f"proposal {proposal_id} has not passed evaluation; "
            "baselines remain unchanged"
        )
    raise NotImplementedError(
        f"proposal {proposal_id} passed evaluation but the Adoption Gate "
        "(human approval) is not implemented; a passing evaluation alone "
        "never changes a baseline"
    )
